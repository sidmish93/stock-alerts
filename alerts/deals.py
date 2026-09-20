"""Post-SHP block and bulk adjustment, ported from deals.py in the ipo-tool.

A shareholding pattern is as-of its quarter end: a June pattern reports the book
on 30 June. Disclosed NSE and BSE block and bulk trades from the next day through
today are then applied in share counts, sells subtracting and buys adding, so a
holder's position reads as it stands now rather than as it stood at quarter end.

Three cleanups stand between the raw feeds and a net position, all of them from
the ipo-tool:

  * The bulk and block feeds often describe the same client-day, so block
    quantity is counted once and only leftover bulk is added on top.
  * A client appearing on both sides of the same day on one exchange is trading
    against itself; those legs say nothing about a change in holding.
  * The same deal is frequently printed by both exchanges. Near-equal quantities
    are one deal counted once; genuinely different ones are two prints summed.
"""

import csv
import datetime
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .http import bse_get_json, nse_get_text, read_cache, write_cache

NSE_DEALS_URL = (
    "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals"
    "?optionType={option}&from={start}&to={end}&csv=true&symbol={symbol}"
)
BSE_DEALS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/BulkDealData_ng/w"
    "?DealType={deal_type}&sc_code={scrip}&FDate={start}&TDate={end}"
)

# The window moves on every day, so this is short.
DEALS_TTL_SECONDS = 3600

# A tape-only buyer is only worth naming above this share of the company.
MIN_TAPE_STAKE_PCT = 0.5
# Two prints of the same deal rarely match to the share.
SAME_DEAL_TOLERANCE = 0.02

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_TOKEN_ALIASES = {
    "LIMITED": "LTD",
    "PRIVATE": "PVT",
    "COMPANY": "CO",
    "CORPORATION": "CORP",
    "INVESTMENTS": "INVESTMENT",
    "SECURITIES": "SEC",
}
# "AND" is dropped rather than folded to "&" as the ipo tool does. Its own
# tokeniser strips "&" as punctuation before aliases are applied, so "X and Y"
# became "X & Y" while "X & Y" became "X Y" and the two never matched. Dropping
# the connector on both sides makes them agree, which matters for joint holdings
# like "Gautambhai Adani & Rajeshbhai Adani".
_DROP = {"THE", "AND"}
_SUFFIXES = ("LTD", "PVT LTD", "PVT", "CORP", "INC", "PLC")


@dataclass
class Leg:
    exchange: str
    deal_type: str
    day: datetime.date
    client: str
    side: str
    qty: float


@dataclass
class Adjustment:
    """What the tape says happened since the pattern was struck."""

    start: datetime.date | None = None
    end: datetime.date | None = None
    outstanding: float | None = None
    legs: int = 0
    # Feeds that actually answered. NSE refuses most cloud addresses, and a
    # missing feed means the adjustment is partial, not that nothing traded.
    feeds: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing


def canonical(text) -> str:
    if not text:
        return ""
    cleaned = _NON_ALNUM.sub(" ", str(text).upper()).strip()
    tokens = [_TOKEN_ALIASES.get(token, token) for token in cleaned.split()]
    return " ".join(token for token in tokens if token not in _DROP)


def canonical_party(name) -> str:
    """Company names with the corporate suffixes peeled off, so
    "Trident Group Limited" and "TRIDENT GROUP LTD" are one party."""
    text = canonical(name)
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
    return text


def _num(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return 0.0


def adjust_window(quarter_end, today=None):
    """(from, to) = the day after quarter end, through today."""
    today = today or datetime.date.today()
    if not quarter_end:
        return None, None
    return quarter_end + datetime.timedelta(days=1), today


def implied_outstanding(holders) -> float | None:
    """Shares outstanding, from the median of shares / (pct/100).

    Each holder implies the whole company's share count; the median of those
    estimates shrugs off a rounded percentage on any one line. Holders under
    0.05% are ignored because their rounding dominates the division.
    """
    estimates = []
    for holder in holders or []:
        shares, pct = holder.shares, holder.pct
        if shares and pct and pct >= 0.05:
            estimates.append(float(shares) * 100.0 / float(pct))
    if not estimates:
        return None
    estimates.sort()
    return round(estimates[len(estimates) // 2])


def names_match(holder_name, client) -> bool:
    left, right = canonical_party(holder_name), canonical_party(client)
    if not left or not right:
        return False
    if left == right:
        return True
    # Ignoring spacing as well catches punctuation variants of the same name,
    # such as "Ribbit Capital V L.P." against "RIBBIT CAPITAL V LP". Full
    # equality of the compact forms, so this cannot match unrelated parties.
    if left.replace(" ", "") == right.replace(" ", ""):
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    # A containment match needs enough substance to not be a coincidence.
    return shorter in longer and (len(shorter) >= 10 or len(shorter.split()) >= 2)


def best_holder(holders, client):
    """The holder a tape client refers to, or None when it is ambiguous."""
    exact, loose = [], []
    client_key = canonical_party(client)
    for holder in holders:
        holder_key = canonical_party(holder.name)
        if not holder_key or not client_key:
            continue
        if holder_key == client_key:
            exact.append(holder)
        elif names_match(holder.name, client):
            loose.append(holder)
    if exact:
        return exact[0]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        # Several loose matches: take the one whose wording differs least, and
        # only when it is strictly closer than the runner-up.
        client_tokens = set(client_key.split())
        scored = []
        for holder in loose:
            tokens = set(canonical_party(holder.name).split())
            scored.append((len(tokens - client_tokens) + len(client_tokens - tokens), holder))
        scored.sort(key=lambda item: item[0])
        if scored[0][0] < scored[1][0]:
            return scored[0][1]
    return None


def dedupe_client_day(legs) -> list[Leg]:
    """One quantity per exchange, client, side and day: block plus leftover bulk."""
    grouped = defaultdict(lambda: {"BULK": 0.0, "BLOCK": 0.0, "name": ""})
    for leg in legs:
        key = (leg.exchange, canonical_party(leg.client), leg.side, leg.day)
        bucket = grouped[key]
        bucket[leg.deal_type] += leg.qty
        if len(leg.client) > len(bucket["name"]):
            bucket["name"] = leg.client

    sides = defaultdict(set)
    for exchange, client, side, day in grouped:
        sides[(exchange, client, day)].add(side)
    self_traders = {key for key, found in sides.items() if len(found) > 1}

    out = []
    for (exchange, client, side, day), bucket in grouped.items():
        if (exchange, client, day) in self_traders:
            continue
        block, bulk = bucket["BLOCK"], bucket["BULK"]
        qty = block + max(0.0, bulk - block)
        if qty <= 0:
            continue
        out.append(
            Leg(
                exchange=exchange,
                deal_type="NET",
                day=day,
                client=bucket["name"] or client,
                side=side,
                qty=qty,
            )
        )
    return out


def merge_exchanges(legs) -> list[Leg]:
    """Collapse the same client, side and day printed on both exchanges."""
    grouped = defaultdict(list)
    for leg in legs:
        grouped[(canonical_party(leg.client), leg.side, leg.day)].append(leg)

    out = []
    for items in grouped.values():
        if len(items) == 1:
            out.append(items[0])
            continue
        by_exchange = defaultdict(float)
        name = ""
        for item in items:
            by_exchange[item.exchange] += item.qty
            if len(item.client or "") > len(name):
                name = item.client
        quantities = [qty for qty in by_exchange.values() if qty > 0]
        if len(quantities) == 2 and abs(quantities[0] - quantities[1]) / max(quantities) <= SAME_DEAL_TOLERANCE:
            qty = max(quantities)
        else:
            qty = sum(quantities)
        out.append(
            Leg(
                exchange="+".join(sorted(by_exchange)),
                deal_type="NET",
                day=items[0].day,
                client=name or items[0].client,
                side=items[0].side,
                qty=qty,
            )
        )
    return out


def net_positions(legs) -> dict:
    """Party to {name, bought, sold} once the feeds have been reconciled."""
    nets = {}
    for leg in merge_exchanges(dedupe_client_day(legs)):
        key = canonical_party(leg.client)
        record = nets.setdefault(key, {"name": leg.client, "bought": 0.0, "sold": 0.0})
        if len(leg.client) > len(record["name"]):
            record["name"] = leg.client
        if leg.side == "BUY":
            record["bought"] += leg.qty
        else:
            record["sold"] += leg.qty
    return nets


def _nse_date(raw):
    text = (raw or "").strip().strip('"')
    for pattern in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def parse_nse_csv(text, start, end, deal_type) -> list[Leg]:
    """NSE's bulk/block CSV. Its headers arrive quoted and space-padded."""
    if not text:
        return []
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.startswith("{") or stripped.startswith("<"):
        return []
    try:
        rows = list(csv.DictReader(io.StringIO(stripped)))
    except Exception:
        return []

    legs = []
    for raw in rows:
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        day = _nse_date(row.get("Date"))
        if day is None or not (start <= day <= end):
            continue
        qty = _num(row.get("Quantity Traded"))
        if qty <= 0:
            continue
        legs.append(
            Leg(
                exchange="NSE",
                deal_type=deal_type,
                day=day,
                client=row.get("Client Name") or "",
                side="BUY" if (row.get("Buy / Sell") or "").upper().startswith("B") else "SELL",
                qty=qty,
            )
        )
    return legs


def fetch_nse_legs(symbol, start, end) -> list[Leg]:
    legs = []
    for option, deal_type in (("block_deals", "BLOCK"), ("bulk_deals", "BULK")):
        url = NSE_DEALS_URL.format(
            option=option,
            symbol=symbol,
            start=start.strftime("%d-%m-%Y"),
            end=end.strftime("%d-%m-%Y"),
        )
        legs.extend(parse_nse_csv(nse_get_text(url), start, end, deal_type))
    return legs


def fetch_bse_legs(scrip_code, start, end) -> list[Leg]:
    legs = []
    for code, deal_type in ((2, "BLOCK"), (1, "BULK")):
        url = BSE_DEALS_URL.format(
            deal_type=code,
            scrip=scrip_code,
            start=start.strftime("%d/%m/%Y"),
            end=end.strftime("%d/%m/%Y"),
        )
        payload = bse_get_json(url)
        rows = payload if isinstance(payload, list) else (payload or {}).get("Table") or []
        for row in rows:
            try:
                day = datetime.date.fromisoformat(str(row.get("DEAL_DATE") or "")[:10])
            except ValueError:
                continue
            if not start <= day <= end:
                continue
            qty = _num(row.get("QUANTITY"))
            if qty <= 0:
                continue
            # BSE marks a purchase with a type beginning "P".
            side = "BUY" if str(row.get("TRANSACTION_TYPE") or "").upper().startswith("P") else "SELL"
            legs.append(
                Leg(
                    exchange="BSE",
                    deal_type=deal_type,
                    day=day,
                    client=str(row.get("CLIENT_NAME") or "").strip(),
                    side=side,
                    qty=qty,
                )
            )
    return legs


def collect_legs(symbol, scrip_code, start, end) -> tuple[list[Leg], list[str], list[str]]:
    """Legs from both feeds, plus which answered and which did not."""
    legs, feeds, missing = [], [], []
    if symbol:
        try:
            legs.extend(fetch_nse_legs(symbol, start, end))
            feeds.append("NSE")
        except Exception:
            missing.append("NSE")
    if scrip_code:
        try:
            legs.extend(fetch_bse_legs(scrip_code, start, end))
            feeds.append("BSE")
        except Exception:
            missing.append("BSE")
    return legs, feeds, missing


def apply_to(holders, nets, outstanding, tape_row) -> list:
    """Record each party's net against its holder. Returns tape-only buyers.

    A client with no holder is either a small holder below the disclosure line or
    somebody new. Its total stake is therefore unknowable from the pattern, so
    only a net *buyer* of real size is reported, and only as an amount bought.
    """
    extras = []
    for record in (nets or {}).values():
        match = best_holder(holders, record["name"])
        if match is None:
            if record["bought"] != record["sold"]:
                extras.append((record, tape_row(record)))
            continue
        match.bought += record["bought"]
        match.sold += record["sold"]

    for holder in holders:
        _finish(holder, outstanding)

    kept = []
    for record, holder in extras:
        if record["sold"] > record["bought"]:
            continue
        net = record["bought"] - record["sold"]
        implied = (net * 100.0 / outstanding) if outstanding else None
        if implied is None or implied < MIN_TAPE_STAKE_PCT:
            continue
        holder.bought = record["bought"]
        holder.sold = record["sold"]
        holder.pct = 0.0
        # Bought this much, but what they held before is not disclosed.
        holder.adj_pct = None
        holder.adj_shares = net
        holder.undetermined = True
        holder.net_pct = round(implied, 2)
        kept.append(holder)
    return kept


def _finish(holder, outstanding) -> None:
    shares = _num(holder.shares)
    adjusted = max(0.0, shares + holder.bought - holder.sold)
    if not holder.bought and not holder.sold:
        # Untouched: keep BSE's own figures rather than recomputing a percentage
        # from an implied share count, which only introduces rounding drift.
        holder.adj_shares = holder.shares
        holder.adj_pct = holder.pct
        return
    holder.adj_shares = adjusted
    holder.adj_pct = round(adjusted * 100.0 / outstanding, 2) if outstanding else None


def adjust(found, today=None, tape_row=None) -> Adjustment:
    """Apply the tape to a shareholding pattern in place."""
    start, end = adjust_window(found.as_of, today)
    outstanding = implied_outstanding(found.promoters + found.public)
    meta = Adjustment(start=start, end=end, outstanding=outstanding)
    holders = found.promoters + found.public
    if not start or not end or start > end:
        for holder in holders:
            _finish(holder, outstanding)
        return meta

    name = f"deals_{found.symbol}_{start:%Y%m%d}_{end:%Y%m%d}.json"
    cached = read_cache(name, DEALS_TTL_SECONDS)
    if cached is None:
        legs, feeds, missing = collect_legs(found.symbol, found.scrip_code, start, end)
        cached = {
            "feeds": feeds,
            "missing": missing,
            "legs": [
                {
                    "exchange": leg.exchange,
                    "deal_type": leg.deal_type,
                    "day": leg.day.isoformat(),
                    "client": leg.client,
                    "side": leg.side,
                    "qty": leg.qty,
                }
                for leg in legs
            ],
        }
        write_cache(name, cached)

    legs = [
        Leg(
            exchange=item["exchange"],
            deal_type=item["deal_type"],
            day=datetime.date.fromisoformat(item["day"]),
            client=item["client"],
            side=item["side"],
            qty=item["qty"],
        )
        for item in cached.get("legs") or []
    ]
    meta.feeds = list(cached.get("feeds") or [])
    meta.missing = list(cached.get("missing") or [])
    meta.legs = len(legs)

    extras = apply_to(holders, net_positions(legs), outstanding, tape_row)
    found.public.extend(extras)
    return meta
