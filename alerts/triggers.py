"""The trigger rules.

The price rules are the tracker's, at two levels instead of one. The volume rule
deliberately departs from it: the tracker compares volume so far today against a
whole day's average, which cannot reach 1.5x before the afternoon however
unusual the morning is. Here today's volume is scaled by the share of a day that
has normally traded by this time, so a stock trading at three times its usual
10am pace is reported at 10am.
"""

from dataclasses import dataclass
from datetime import date

from .config import (
    BASELINE_SESSIONS,
    IMPLAUSIBLE_MOVE_PCT,
    MIN_PROFILE_FRACTION,
    MOVE_LEVELS,
    PRICE_RULES,
    VOLUME_LEVELS,
    VOLUME_PACING,
)

INTRADAY = "intraday"
DAILY = "daily"
VOLUME = "volume"


@dataclass
class Reading:
    """What a symbol currently measures, before any level is applied."""

    symbol: str
    # Signed percentages.
    intraday_return_pct: float | None = None
    daily_return_pct: float | None = None
    # Multiple of the average volume: projected for the full day when pacing is
    # on, else raw cumulative volume over a full day's average.
    volume_multiple: float | None = None
    baseline_volume: float | None = None
    projected_volume: float | None = None
    pace_fraction: float | None = None
    paced: bool = False
    cautions: list[str] = None

    def __post_init__(self):
        if self.cautions is None:
            self.cautions = []

    def value_for(self, kind: str) -> float | None:
        if kind == INTRADAY:
            return self.intraday_return_pct
        if kind == DAILY:
            return self.daily_return_pct
        return self.volume_multiple


@dataclass
class Trigger:
    """One level crossed by one symbol."""

    symbol: str
    kind: str
    level: float
    # Signed percentage for the move triggers, multiple of average for volume.
    value: float
    direction: str = "up"


def average_volume(sessions, before: date, count: int = BASELINE_SESSIONS) -> float | None:
    """Mean volume of the last `count` completed sessions before `before`."""
    if not sessions or before is None or count <= 0:
        return None
    earlier = [item for item in sessions if item.day < before]
    if not earlier:
        return None
    window = earlier[-count:]
    return sum(item.volume for item in window) / len(window)


def measure(quote, history, now=None) -> Reading:
    """Turn a live quote plus a day's history into comparable readings."""
    reading = Reading(
        symbol=quote.symbol,
        intraday_return_pct=quote.intraday_return_pct,
        daily_return_pct=quote.daily_return_pct,
    )

    for value in (quote.intraday_return_pct, quote.daily_return_pct):
        if value is not None and abs(value) >= IMPLAUSIBLE_MOVE_PCT:
            reading.cautions.append(
                f"move beyond {IMPLAUSIBLE_MOVE_PCT:.0f}% - check for a corporate "
                "action or bad tick before acting"
            )
            break

    session_day = quote.session_date
    if session_day is not None and history is not None:
        if session_day in (history.splits or {}):
            reading.cautions.append(
                f"ex-split today ({history.splits[session_day]}) - the move may be "
                "a price adjustment"
            )
        dividend = (history.dividends or {}).get(session_day)
        if dividend and quote.previous_close:
            share = dividend / quote.previous_close * 100
            if share >= 1.0:
                reading.cautions.append(
                    f"ex-dividend today (Rs {dividend:g}, {share:.1f}% of price)"
                )

    baseline = average_volume(getattr(history, "sessions", None) or [], session_day)
    reading.baseline_volume = baseline
    if quote.volume is None or not baseline or baseline <= 0:
        return reading

    moment = now or quote.quote_at
    profile = getattr(history, "profile", None)
    fraction = profile.fraction_by(moment) if (VOLUME_PACING and profile and moment) else None

    if fraction is None:
        # No usable profile: fall back to the tracker's raw comparison rather
        # than going silent, and say so in the alert.
        reading.volume_multiple = round(quote.volume / baseline, 2)
        return reading

    reading.pace_fraction = round(fraction, 4)
    if fraction < MIN_PROFILE_FRACTION:
        # Dividing by almost nothing turns the first trade of the day into a
        # tenfold surge, so nothing is reported until the day is underway.
        return reading

    projected = quote.volume / fraction
    reading.paced = True
    reading.projected_volume = projected
    reading.volume_multiple = round(projected / baseline, 2)
    return reading


@dataclass
class Candidate:
    """One level of one rule, and where the symbol currently stands against it."""

    kind: str
    direction: str
    level: float
    # Compared against the level. Negative when the move is the other way.
    magnitude: float
    # The reading as it should be reported: signed percentage, or the multiple.
    value: float


def candidates(reading: Reading) -> list[Candidate]:
    """Every level being tracked for this symbol, above it or not.

    Levels below the threshold are included too, because the caller has to know
    when a reading has fallen back far enough for that level to fire again. Both
    price directions are always listed, so a stock that fired downwards and then
    recovered upwards re-arms its downside levels instead of latching for the day.
    """
    found = []
    for kind in (INTRADAY, DAILY):
        if kind not in PRICE_RULES:
            continue
        value = reading.value_for(kind)
        if value is None:
            continue
        for direction, magnitude in (("up", value), ("down", -value)):
            for level in MOVE_LEVELS:
                found.append(
                    Candidate(
                        kind=kind,
                        direction=direction,
                        level=level,
                        magnitude=magnitude,
                        value=value,
                    )
                )

    if reading.volume_multiple is not None:
        for level in VOLUME_LEVELS:
            found.append(
                Candidate(
                    kind=VOLUME,
                    direction="up",
                    level=level,
                    magnitude=reading.volume_multiple,
                    value=reading.volume_multiple,
                )
            )
    return found
