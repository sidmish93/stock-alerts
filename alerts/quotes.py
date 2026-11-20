"""Live quotes, daily volume history and the intraday volume profile, from Yahoo.

NSE names use the .NS series. A BSE-only listing uses .BO, which is how
National Stock Exchange is priced: it has no series on NSE.

Yahoo rather than NSE's own quote API on purpose: that API answers almost nothing
from a cloud address, and its historical endpoint needs page cookies that expire
constantly. Yahoo carries the prices and volume, works from anywhere, and
already restates history for splits and bonuses, so none of the corporate-action
arithmetic the tracker needs against raw NSE data is required here.

Three different requests, because no single one carries everything:

  interval=1d  range=1d    meta.chartPreviousClose is yesterday's close, which
                           is what the daily return is measured from. Polled
                           every cycle. At any wider range that same field means
                           the close before the whole window, so it must never be
                           read for returns.
  interval=1d  range=1mo   completed daily bars for the volume baseline, plus
                           split and dividend dates. Once a day.
  interval=15m range=1mo   intraday bars, for the share of a day that has
                           normally traded by a given time. Once a day.
"""

import re
import statistics
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .config import (
    HISTORY_RANGE,
    HISTORY_TTL_SECONDS,
    INTRADAY_INTERVAL,
    PROFILE_SESSIONS,
)
from .http import get_json, read_cache, reset_session, write_cache, yahoo_crumb

CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?interval={interval}&range={range}{extra}"
)

IST = timezone(timedelta(hours=5, minutes=30))
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


@dataclass
class Session:
    """One completed trading day, as Yahoo reports it."""

    day: date
    close: float
    volume: float


@dataclass
class Profile:
    """The share of a normal day's volume traded by each point in the session.

    Built from the median across recent sessions rather than the mean: on expiry
    and index-rebalance days the closing auction can be half the day's volume,
    and one such day would badly skew an average.
    """

    # (minutes since 09:15, median cumulative share of the day) ascending.
    points: list[tuple[int, float]] = field(default_factory=list)
    sessions: int = 0

    def fraction_by(self, moment: datetime) -> float | None:
        """Share of a normal day expected to have traded by this time."""
        if not self.points:
            return None
        minutes = (moment.hour * 60 + moment.minute) - (9 * 60 + 15)
        if minutes <= 0:
            return 0.0
        last_minute, _ = self.points[-1]
        if minutes >= last_minute:
            return 1.0
        previous_minute, previous_share = 0, 0.0
        for edge, share in self.points:
            if minutes <= edge:
                # Straight-line inside the bar the clock currently sits in.
                span = edge - previous_minute
                if span <= 0:
                    return share
                weight = (minutes - previous_minute) / span
                return previous_share + (share - previous_share) * weight
            previous_minute, previous_share = edge, share
        return 1.0


@dataclass
class History:
    """Everything about a symbol that only needs fetching once a day."""

    sessions: list[Session] = field(default_factory=list)
    profile: Profile = field(default_factory=Profile)
    splits: dict[date, str] = field(default_factory=dict)
    dividends: dict[date, float] = field(default_factory=dict)


@dataclass
class Quote:
    """A live snapshot of one symbol, plus the returns derived from it."""

    symbol: str
    cmp: float | None = None
    open: float | None = None
    previous_close: float | None = None
    volume: int | None = None
    # IST trading date the quote belongs to. Guards against alerting on a stale
    # Friday close all through the weekend.
    session_date: date | None = None
    quote_at: datetime | None = None
    intraday_return_pct: float | None = None
    daily_return_pct: float | None = None
    # Rupees, from Yahoo's quote endpoint. Filled only when an alert is about
    # to go out: the chart call used for every poll does not carry it.
    market_cap: float | None = None
    error: str | None = None

    @property
    def quote_time(self) -> str | None:
        return f"{self.quote_at:%H:%M}" if self.quote_at else None


def _number(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _slug(symbol: str) -> str:
    return _UNSAFE.sub("-", (symbol or "").upper()).strip("-") or "unknown"


def _ist(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(IST)
    except (TypeError, ValueError, OSError):
        return None


def _ist_date(stamp) -> date | None:
    moment = _ist(stamp)
    return moment.date() if moment else None


def yahoo_ticker(symbol: str, exchange: str = "NS") -> str:
    """Yahoo symbol. An NSE equity is .NS; a BSE-only listing is .BO."""
    text = (symbol or "").upper().strip()
    if text.endswith(".NS") or text.endswith(".BO"):
        return text
    series = (exchange or "NS").upper().strip().lstrip(".")
    if series not in {"NS", "BO"}:
        series = "NS"
    return f"{text}.{series}"


def _result(ticker: str, interval: str, window: str, ttl: int = 0, events: bool = False):
    """Chart payload for one Yahoo ticker, such as TATAELXSI.NS or NSE.BO."""
    name = f"yahoo_{_slug(ticker)}_{interval}_{window}.json"
    payload = read_cache(name, ttl) if ttl else None
    if payload is None:
        url = CHART_URL.format(
            symbol=urllib.parse.quote((ticker or "").upper(), safe="-."),
            interval=interval,
            range=window,
            extra="&events=div,split" if events else "",
        )
        payload = get_json(url, attempts=2)
        if ttl:
            write_cache(name, payload)
    chart = (payload or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        detail = (chart.get("error") or {}).get("description") or "no chart data"
        raise RuntimeError(detail)
    return results[0]


def _pct_move(cmp_price, base) -> float | None:
    """(CMP - base) / base as a percentage; None when the base is missing."""
    if not cmp_price or not base or base <= 0:
        return None
    return round((cmp_price / base - 1) * 100, 2)


def fetch_quote(symbol: str, exchange: str = "NS") -> Quote:
    """Live CMP, open, previous close and cumulative volume for one symbol."""
    quote = Quote(symbol=(symbol or "").upper())
    try:
        result = _result(yahoo_ticker(symbol, exchange), "1d", "1d")
    except Exception as exc:
        quote.error = str(exc)
        return quote

    meta = result.get("meta") or {}
    bars = ((result.get("indicators") or {}).get("quote") or [{}])[0]

    price = _number(meta.get("regularMarketPrice"))
    if price <= 0:
        quote.error = "no live price"
        return quote

    # Yahoo leaves regularMarketOpen empty on the chart endpoint; the single
    # daily bar in a 1d range carries today's open instead.
    opening = _number(meta.get("regularMarketOpen"))
    if opening <= 0:
        opens = bars.get("open") or []
        opening = _number(opens[-1]) if opens else 0.0

    volume = _number(meta.get("regularMarketVolume"))
    if volume <= 0:
        volumes = bars.get("volume") or []
        volume = _number(volumes[-1]) if volumes else 0.0

    stamp = meta.get("regularMarketTime")
    quote.cmp = round(price, 2)
    quote.open = round(opening, 2) if opening > 0 else None
    previous = _number(meta.get("chartPreviousClose") or meta.get("previousClose"))
    quote.previous_close = round(previous, 2) if previous > 0 else None
    quote.volume = int(volume) if volume > 0 else None
    quote.quote_at = _ist(stamp)
    quote.session_date = quote.quote_at.date() if quote.quote_at else None
    quote.intraday_return_pct = _pct_move(quote.cmp, quote.open)
    quote.daily_return_pct = _pct_move(quote.cmp, quote.previous_close)
    return quote


def market_cap_from_payload(payload) -> float | None:
    """Rupee market cap from a Yahoo v7 quote response, or None when it is absent."""
    rows = ((payload or {}).get("quoteResponse") or {}).get("result") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    currency = str(row.get("currency") or "INR").upper()
    if currency != "INR":
        return None
    value = row.get("marketCap")
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def fetch_market_cap(symbol: str, exchange: str = "NS") -> float | None:
    """Current market cap in rupees. None when Yahoo will not say, so the alert still sends.

    This is a different endpoint from the chart call: that one has the price and
    not the market cap. It needs a crumb, and it is only worth the extra request
    for a name that is actually alerting.
    """
    ticker = yahoo_ticker(symbol, exchange)
    if not ticker or ticker in {".NS", ".BO"}:
        return None
    quoted = urllib.parse.quote(ticker, safe="-.")
    for _ in range(2):
        try:
            crumb = urllib.parse.quote(yahoo_crumb(), safe="")
            url = (
                "https://query1.finance.yahoo.com/v7/finance/quote"
                f"?symbols={quoted}&crumb={crumb}"
            )
            return market_cap_from_payload(get_json(url, attempts=1))
        except Exception:
            reset_session()
    return None


def _daily_sessions(result) -> list[Session]:
    """Completed daily bars, oldest first.

    Yahoo emits a bar for NSE holidays with zero volume and the previous close
    carried forward. Those are not sessions and would drag the average down.
    """
    stamps = result.get("timestamp") or []
    bars = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = bars.get("close") or []
    volumes = bars.get("volume") or []

    sessions = []
    for stamp, close, volume in zip(stamps, closes, volumes):
        day = _ist_date(stamp)
        price, quantity = _number(close), _number(volume)
        if day is None or price <= 0 or quantity <= 0:
            continue
        sessions.append(Session(day=day, close=price, volume=quantity))
    sessions.sort(key=lambda item: item.day)
    return sessions


def _events(result) -> tuple[dict, dict]:
    events = result.get("events") or {}
    splits, dividends = {}, {}
    for item in (events.get("splits") or {}).values():
        day = _ist_date(item.get("date"))
        if day:
            splits[day] = str(item.get("splitRatio") or "split")
    for item in (events.get("dividends") or {}).values():
        day = _ist_date(item.get("date"))
        if day:
            dividends[day] = _number(item.get("amount"))
    return splits, dividends


def interval_minutes(label: str = "") -> int:
    """Width of one intraday bar, from a label such as 15m or 1h."""
    text = (label or INTRADAY_INTERVAL).strip().lower()
    match = re.fullmatch(r"(\d+)\s*(m|h)", text)
    if not match:
        return 15
    size = int(match.group(1))
    return size * 60 if match.group(2) == "h" else size


def build_profile(
    bars_by_day: dict[date, list[tuple[int, float]]],
    skip: date | None = None,
    bar_minutes: int | None = None,
) -> Profile:
    """Median cumulative volume curve across recent complete sessions.

    bars_by_day maps a session to its (minutes since 09:15, bar volume) pairs,
    keyed by the minute each bar *starts*.
    """
    width = bar_minutes or interval_minutes()
    usable = []
    for day in sorted(bars_by_day):
        if skip is not None and day >= skip:
            continue
        bars = sorted(bars_by_day[day])
        total = sum(volume for _, volume in bars)
        if total <= 0 or len(bars) < 2:
            continue
        running, curve = 0.0, []
        for minute, volume in bars:
            running += volume
            # A bar labelled 09:15 covers 09:15-09:30, so the volume it carries
            # has only fully traded by the end of it. Recording the share against
            # the bar's start would credit the whole bar to its first minute and
            # overstate every morning reading.
            curve.append((minute + width, running / total))
        usable.append(curve)

    usable = usable[-PROFILE_SESSIONS:]
    if not usable:
        return Profile()

    # Bar edges are the same every day for a given interval; take them from the
    # longest curve so a short day does not truncate the grid.
    grid = sorted({minute for curve in usable for minute, _ in curve})
    points = []
    for minute in grid:
        shares = []
        for curve in usable:
            share = None
            for edge, value in curve:
                if edge <= minute:
                    share = value
                else:
                    break
            if share is not None:
                shares.append(share)
        if shares:
            points.append((minute, statistics.median(shares)))
    # Cumulative shares can only rise; median across days can wobble slightly.
    cleaned, highest = [], 0.0
    for minute, share in points:
        highest = max(highest, share)
        cleaned.append((minute, min(1.0, highest)))
    return Profile(points=cleaned, sessions=len(usable))


def _intraday_bars(symbol: str) -> dict[date, list[tuple[int, float]]]:
    try:
        result = _result(symbol, INTRADAY_INTERVAL, HISTORY_RANGE, HISTORY_TTL_SECONDS)
    except Exception:
        return {}
    stamps = result.get("timestamp") or []
    bars = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    volumes = bars.get("volume") or []

    grouped: dict[date, list[tuple[int, float]]] = {}
    for stamp, volume in zip(stamps, volumes):
        moment = _ist(stamp)
        quantity = _number(volume)
        if moment is None or quantity <= 0:
            continue
        minutes = (moment.hour * 60 + moment.minute) - (9 * 60 + 15)
        if minutes < 0:
            continue
        grouped.setdefault(moment.date(), []).append((minutes, quantity))
    return grouped


def fetch_history(symbol: str, today: date | None = None, exchange: str = "NS") -> History:
    """Daily bars, the intraday volume profile and corporate actions.

    All of it changes at most once a day, so it is cached for the session.
    """
    ticker = yahoo_ticker(symbol, exchange)
    history = History()
    try:
        result = _result(ticker, "1d", HISTORY_RANGE, HISTORY_TTL_SECONDS, events=True)
    except Exception:
        return history
    history.sessions = _daily_sessions(result)
    history.splits, history.dividends = _events(result)
    history.profile = build_profile(_intraday_bars(ticker), skip=today)
    return history
