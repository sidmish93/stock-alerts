"""Named shareholders from BSE, following the ipo-tool in this workspace.

That tool does not read aggregate percentages off a summary page. It goes to
BSE's shareholding-pattern APIs and pulls out the individual holders: every
promoter and promoter-group entity, and every named public shareholder, each with
its percentage of (A+B+C2) and its share count. This module reproduces that:

  CorporatesSHPSecuritybeta   the latest published shareholding quarter
  Corp_shpPromoterNGroup_ng   named promoter / promoter group holders
  Corp_shpSec_SHPPubShold_ng  named public holders, under their category heading

Each response carries several tables; the one that matters is the longest list
whose rows name a holder, which is the rule the ipo tool uses. Promoter rows are
then kept only where the shareholder type is Promoter or Promoter Group, and any
holder at 0% is dropped.

The ipo tool reaches these endpoints through a real browser because BSE blocks
plain HTTP clients. A Chrome-impersonating curl handle clears the same bar, so no
browser is needed here.
"""

import calendar
import datetime
import html
import re
from dataclasses import dataclass, field

from . import deals
from .config import (
    POST_SHP_ADJUSTMENT,
    SHAREHOLDING_PROMOTERS,
    SHAREHOLDING_PUBLIC_HOLDERS,
)
from .http import bse_get_json, read_cache, write_cache

SCRIP_MASTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
QUARTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/CorporatesSHPSecuritybeta/w"
    "?scripcode={code}&qtrid="
)
PROMOTERS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/Corp_shpPromoterNGroup_ng/w"
    "?SCRIPCODE={code}&QtrCode={quarter}"
)
PUBLIC_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/Corp_shpSec_SHPPubShold_ng/w"
    "?SCRIPCODE={code}&QtrCode={quarter}"
)
STATEMENT_URL = (
    "https://www.bseindia.com/corporates/ShpPromoterNGroup"
    "?scripcd={code}&qtrid={quarter}&QtrName={name}"
)

# Telegram rejects anything past 4,096 characters, so the note is kept a little
# under that rather than risking a send that fails outright.
MESSAGE_BUDGET = 3900

MASTER_TTL_SECONDS = 24 * 3600
# Shareholding is published quarterly, so a day is a very short cache by its
# standards; it exists to stop repeated triggers on one name refetching.
SHP_TTL_SECONDS = 12 * 3600

PROMOTER_TYPES = ("Promoter", "Promoter Group")

# BSE mixes named entities and category subtotals into the same table. The ipo
# tool separates them by whether the rendered page prints the row in bold, which
# is the reason it needs a real browser. Two signals replace that here:
#
#   1. A named entity is a single shareholder, so Fld_NoOfShareHolders is 1.
#      A subtotal counts its members - 6,601 for Reliance's HUF line.
#   2. A handful of subtotals do report a count of 1, so their labels are named
#      outright. Matched whole, never as a substring: "Insurance Companies" is a
#      category, while "Life Insurance Corporation Of India" is a real holder.
_GENERIC_LABELS = {
    "huf", "llp", "trust", "trusts", "employees", "employee",
    "clearing member", "clearing members", "bodies corporate", "body corporate",
    "body corp-ltd liability partnership", "non resident indians",
    "non resident indian", "nri", "nris", "resident individuals", "public",
    "others", "any other", "any other (specify)", "foreign nationals",
    "foreign institutional investors", "foreign portfolio investors",
    "mutual funds", "insurance companies", "banks", "financial institutions",
    "alternate investment funds", "qualified institutional buyer",
    "foreign companies", "foreign company", "directors and their relatives",
    "key managerial personnel", "unclaimed shares", "overseas corporate bodies",
    "investor education and protection fund", "government", "central government",
    "state government", "president of india", "market maker",
}

# Holding accounts rather than holders. Safe to match loosely: no real
# shareholder is named after a suspense or escrow account.
_GENERIC_PATTERNS = re.compile(
    r"unclaimed|suspense|escrow|investor education|\biepf\b", re.I
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Category headings BSE prints in full, shortened to fit a phone screen.
_CATEGORY_SHORT = (
    ("foreign portfolio investor", "FPI"),
    ("mutual fund", "Mutual Fund"),
    ("insurance", "Insurance"),
    ("alternate investment", "AIF"),
    ("financial institution", "Bank/FI"),
    ("bank", "Bank/FI"),
    ("foreign institutional investor", "FII"),
    ("bodies corporate", "Corporate"),
    ("any other", "Other"),
)


@dataclass
class Holder:
    """One named shareholder in the latest published pattern.

    The adjusted fields are filled in by the post-SHP pass in deals.py: what this
    party has bought and sold on the tape since the quarter ended, and where that
    leaves the stake.
    """

    name: str
    category: str
    pct: float
    shares: float | None = None
    bought: float = 0.0
    sold: float = 0.0
    adj_pct: float | None = None
    adj_shares: float | None = None
    # A tape-only buyer: what it bought is known, its total stake is not.
    undetermined: bool = False
    net_pct: float | None = None

    @property
    def short_category(self) -> str:
        lowered = self.category.lower()
        for needle, label in _CATEGORY_SHORT:
            if needle in lowered:
                return label
        return self.category

    @property
    def traded(self) -> bool:
        return bool(self.bought or self.sold)

    @property
    def net_shares(self) -> float:
        return self.bought - self.sold


@dataclass
class Shareholding:
    symbol: str
    scrip_code: str = ""
    quarter: str = ""
    as_of: datetime.date | None = None
    promoters: list[Holder] = field(default_factory=list)
    public: list[Holder] = field(default_factory=list)
    url: str = ""
    # Set when the post-SHP block/bulk pass has run.
    adjustment: object | None = None

    @property
    def promoter_pct(self) -> float:
        return round(sum(holder.pct for holder in self.promoters), 2)

    @property
    def adjusted_promoter_pct(self) -> float:
        return round(
            sum(
                holder.adj_pct if holder.adj_pct is not None else holder.pct
                for holder in self.promoters
            ),
            2,
        )

    @property
    def traded(self) -> list[Holder]:
        """Holders the tape has moved since the quarter ended, largest first."""
        moved = [h for h in self.promoters + self.public if h.traded]
        return sorted(moved, key=lambda h: -abs(h.net_shares))


def _to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def quarter_end(label: str) -> datetime.date | None:
    """Last day covered by a quarter labelled 'June 2026' or '30-Jun-2026'.

    A June pattern is as-of 30 June, which is what dates the figures.
    """
    text = (label or "").strip()
    if not text:
        return None
    match = re.search(r"(\d{1,2})[-/ ]([A-Za-z]{3,})[-/ ](\d{2,4})", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        year = int(match.group(3))
        year += 2000 if year < 100 else 0
        if month:
            last = calendar.monthrange(year, month)[1]
            return datetime.date(year, month, min(int(match.group(1)), last))
    match = re.search(r"([A-Za-z]{3,})\s+(\d{4})", text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            year = int(match.group(2))
            return datetime.date(year, month, calendar.monthrange(year, month)[1])
    return None


def _master() -> dict:
    """BSE equity scrip master, indexed by ticker and by ISIN."""
    cached = read_cache("bse_scrip_master.json", MASTER_TTL_SECONDS)
    if cached is None:
        payload = bse_get_json(SCRIP_MASTER_URL)
        rows = payload if isinstance(payload, list) else (payload or {}).get("Table") or []
        index = {}
        for row in rows:
            code = str(row.get("SCRIP_CD") or "").strip()
            if not code:
                continue
            ticker = str(row.get("scrip_id") or "").strip().upper()
            isin = str(row.get("ISIN_NUMBER") or "").strip().upper()
            if ticker:
                index.setdefault(ticker, code)
            if isin:
                index.setdefault(isin, code)
        cached = index
        write_cache("bse_scrip_master.json", cached)
    return cached


def scrip_code(symbol: str) -> str:
    """BSE scrip code for an NSE symbol. BSE's ticker matches it for almost
    every ordinary equity; an ISIN works too."""
    wanted = (symbol or "").strip().upper()
    if not wanted:
        return ""
    try:
        return _master().get(wanted, "")
    except Exception:
        return ""


def latest_quarter(code: str) -> tuple[float, str] | None:
    """(quarter id, quarter name) of the most recent published pattern."""
    payload = bse_get_json(QUARTER_URL.format(code=code))
    rows = (payload or {}).get("Table") or []
    if not rows:
        return None
    row = rows[0]
    quarter_id = _to_float(row.get("Qtr_Id"))
    if quarter_id is None:
        return None
    return quarter_id, str(row.get("Fld_qtrname") or "").strip()


def share_table(payload) -> list:
    """The longest table in the response whose rows name a holder."""
    best, count = None, -1
    for value in (payload or {}).values():
        if not (isinstance(value, list) and value and isinstance(value[0], dict)):
            continue
        if "Fld_ShareHolderName" in value[0] and len(value) > count:
            best, count = value, len(value)
    return best or []


def is_named_holder(row, name: str, promoter: bool = False) -> bool:
    """True when the row is one real entity rather than a category subtotal.

    The label deny list is applied to public rows only. A promoter row has
    already been vouched for by its shareholder type, and some genuine promoters
    are named exactly like a category: the President of India holds 96.5% of LIC,
    and every other PSU reports its government stake the same way.
    """
    count = _to_float(row.get("Fld_NoOfShareHolders"))
    if count is not None and count > 1:
        return False
    if promoter:
        return True
    if _GENERIC_PATTERNS.search(name):
        return False
    label = re.sub(r"[^a-z0-9&\-() ]+", "", name.lower()).strip()
    return label not in _GENERIC_LABELS


def _holder(row, category: str, promoter: bool = False) -> Holder | None:
    name = re.sub(r"\s+", " ", str(row.get("Fld_ShareHolderName") or "").strip())
    pct = _to_float(row.get("Fld_TotalPercentageOf_A_B_C2"))
    if not name or pct is None or pct <= 0:
        return None
    if not is_named_holder(row, name, promoter=promoter):
        return None
    return Holder(
        name=name,
        category=category,
        pct=pct,
        shares=_to_float(row.get("Fld_TotalNoOfShares")),
    )


def promoter_holders(code: str, quarter_id: float) -> list[Holder]:
    payload = bse_get_json(PROMOTERS_URL.format(code=code, quarter=f"{quarter_id:.2f}"))
    found = []
    for row in share_table(payload):
        # Note BSE's spelling: FLd, not Fld.
        kind = str(row.get("FLd_ShareholderType") or "").strip()
        if kind not in PROMOTER_TYPES:
            continue
        holder = _holder(row, kind, promoter=True)
        if holder:
            found.append(holder)
    return sorted(found, key=lambda item: -item.pct)


def public_holders(code: str, quarter_id: float) -> list[Holder]:
    payload = bse_get_json(PUBLIC_URL.format(code=code, quarter=f"{quarter_id:.2f}"))
    found = []
    for row in share_table(payload):
        heading = str(row.get("Fld_Level") or row.get("Fld_SubCategory") or "").strip()
        heading = re.sub(r"/+\s*$", "", heading).strip()
        holder = _holder(row, heading)
        if holder:
            found.append(holder)
    return sorted(found, key=lambda item: -item.pct)


def _as_dict(found: Shareholding) -> dict:
    return {
        "scrip_code": found.scrip_code,
        "quarter": found.quarter,
        "as_of": found.as_of.isoformat() if found.as_of else "",
        "url": found.url,
        "promoters": [vars(h) for h in found.promoters],
        "public": [vars(h) for h in found.public],
    }


def _from_dict(symbol: str, payload: dict) -> Shareholding:
    as_of = None
    if payload.get("as_of"):
        try:
            as_of = datetime.date.fromisoformat(payload["as_of"])
        except ValueError:
            as_of = None
    return Shareholding(
        symbol=symbol,
        scrip_code=payload.get("scrip_code", ""),
        quarter=payload.get("quarter", ""),
        as_of=as_of,
        promoters=[Holder(**item) for item in payload.get("promoters") or []],
        public=[Holder(**item) for item in payload.get("public") or []],
        url=payload.get("url", ""),
    )


def _tape_row(record) -> Holder:
    """A party on the tape that the pattern does not name."""
    net = record["bought"] - record["sold"]
    return Holder(
        name=record["name"],
        category="bought since the quarter" if net > 0 else "sold since the quarter",
        pct=0.0,
        shares=0.0,
    )


def fetch(symbol: str, today: datetime.date | None = None) -> Shareholding | None:
    """Named holders for one symbol, restated for block and bulk deals since the
    pattern was struck. None when BSE has no pattern for it."""
    found = _pattern(symbol)
    if found is None:
        return None
    if POST_SHP_ADJUSTMENT:
        try:
            found.adjustment = deals.adjust(found, today=today, tape_row=_tape_row)
        except Exception:
            # The pattern on its own is still worth sending.
            found.adjustment = None
    return found


def _pattern(symbol: str) -> Shareholding | None:
    """The published pattern, cached for the day. Quarterly data."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return None

    name = f"bse_shp_{symbol}.json"
    cached = read_cache(name, SHP_TTL_SECONDS)
    if cached is not None:
        if not (cached.get("promoters") or cached.get("public")):
            return None
        return _from_dict(symbol, cached)

    empty = {"scrip_code": "", "quarter": "", "as_of": "", "url": "",
             "promoters": [], "public": []}
    code = scrip_code(symbol)
    if not code:
        write_cache(name, empty)
        return None

    try:
        quarter = latest_quarter(code)
    except Exception:
        return None
    if not quarter:
        write_cache(name, empty)
        return None

    quarter_id, quarter_name = quarter
    try:
        promoters = promoter_holders(code, quarter_id)
        public = public_holders(code, quarter_id)
    except Exception:
        return None

    if not promoters and not public:
        write_cache(name, empty)
        return None

    found = Shareholding(
        symbol=symbol,
        scrip_code=code,
        quarter=quarter_name,
        as_of=quarter_end(quarter_name),
        promoters=promoters,
        public=public,
        url=STATEMENT_URL.format(
            code=code, quarter=f"{quarter_id:.2f}", name=quarter_name.replace(" ", "%20")
        ),
    )
    write_cache(name, _as_dict(found))
    return found


def _trim(text: str, width: int = 44) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= width else text[: width - 1].rstrip() + "\u2026"


def _shares(value) -> str:
    """Share counts in crore or lakh, which is how they are read."""
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return "-"
    if number >= 1_00_00_000:
        return f"{number / 1_00_00_000:.2f} cr"
    if number >= 1_00_000:
        return f"{number / 1_00_000:.2f} lakh"
    return f"{number:,.0f}"


def _line(holder: Holder, with_category: bool = True) -> str:
    label = html.escape(_trim(holder.name))
    if holder.traded and holder.adj_pct is not None:
        # Moved on the tape: show where it started and where it now stands.
        line = f"\u2022 <b>{holder.pct:.2f}% \u2192 {holder.adj_pct:.2f}%</b>  {label}"
    else:
        line = f"\u2022 <b>{holder.pct:.2f}%</b>  {label}"
    if with_category and holder.category:
        line += f"  <i>{html.escape(holder.short_category)}</i>"
    return line


def _trade_line(holder: Holder) -> str:
    label = html.escape(_trim(holder.name, 40))
    net = holder.net_shares
    verb = "bought" if net > 0 else "sold"
    line = f"\u2022 {label} <b>{verb} {_shares(net)}</b>"
    if holder.undetermined:
        # Whatever it held below the disclosure line is not published, so the
        # total stake genuinely cannot be worked out from the pattern.
        if holder.net_pct is not None:
            line += f" (\u2248{holder.net_pct:.2f}% of the company; total stake not disclosed)"
    elif holder.adj_pct is not None:
        line += f"  {holder.pct:.2f}% \u2192 <b>{holder.adj_pct:.2f}%</b>"
    return line


def _adjustment_lines(found: Shareholding) -> list[str]:
    """What the tape has done since the pattern was struck."""
    meta = found.adjustment
    if meta is None or not meta.start:
        return []

    moved = found.traded
    window = f"since {meta.start:%d %b}"
    if not moved:
        note = f"\n<i>No block or bulk deals {window}.</i>"
        if meta.missing:
            note = (
                f"\n<i>No block or bulk deals {window} on "
                f"{', '.join(meta.feeds) or 'either feed'} \u2014 "
                f"{'/'.join(meta.missing)} unavailable, so this is partial.</i>"
            )
        return [note]

    lines = [f"\n<b>Block / bulk deals {window}</b>"]
    lines.extend(_trade_line(holder) for holder in moved[:6])
    if len(moved) > 6:
        lines.append(f"\u2022 <i>and {len(moved) - 6} more parties</i>")
    if meta.missing:
        lines.append(
            f"<i>{'/'.join(meta.missing)} feed unavailable, so this may be incomplete.</i>"
        )
    return lines


def _rollup(remaining: list[Holder]) -> str:
    """One line standing in for the holders not listed individually."""
    total = sum(holder.pct for holder in remaining)
    plural = "entity" if len(remaining) == 1 else "entities"
    return f"\u2022 <i>and {len(remaining)} more {plural}, {total:.2f}% together</i>"


def format_message(
    found: Shareholding,
    name: str = "",
    public_limit: int = SHAREHOLDING_PUBLIC_HOLDERS,
    promoter_limit: int = SHAREHOLDING_PROMOTERS,
) -> str:
    """The note as sent: every holder, unless that genuinely will not fit.

    Telegram rejects a message over 4,096 characters outright, so rather than
    trimming to an arbitrary count the whole register is rendered and shrunk only
    if it breaches that. In practice nothing does: the widest register sampled,
    Groww with 16 promoter entities and 12 named public holders, came to 2,244
    characters.
    """
    text = _render(found, name, promoter_limit, public_limit)
    if len(text) <= MESSAGE_BUDGET:
        return text

    # Over budget. Give ground on the public register first: the promoter
    # structure is usually why the move was worth explaining.
    named = len([holder for holder in found.public if not holder.undetermined])
    for limit in range(named - 1, 0, -1):
        text = _render(found, name, promoter_limit, limit)
        if len(text) <= MESSAGE_BUDGET:
            return text
    for limit in range(len(found.promoters) - 1, 0, -1):
        text = _render(found, name, limit, 1)
        if len(text) <= MESSAGE_BUDGET:
            return text
    return text[:MESSAGE_BUDGET]


def _render(
    found: Shareholding,
    name: str = "",
    promoter_limit: int = 0,
    public_limit: int = 0,
) -> str:
    """One rendering of the note. A limit of 0 lists every holder.

    Written as wrapping bullet lines rather than a fixed-width table, because
    holder names run long and a table of them is unreadable on a phone.
    """
    promoter_limit = promoter_limit or len(found.promoters) or 1
    public_limit = public_limit or len(found.public) or 1
    title = html.escape(name) if name and name.upper() != found.symbol else ""
    heading = f"\U0001f4c4 <b>{html.escape(found.symbol)}</b> shareholders"
    if title:
        heading += f" \u2014 <i>{title}</i>"
    lines = [heading]

    if found.promoters:
        shown = found.promoters[:promoter_limit]
        adjusted = found.adjusted_promoter_pct
        if abs(adjusted - found.promoter_pct) >= 0.01:
            header = f"\n<b>Promoters {found.promoter_pct:.2f}% \u2192 {adjusted:.2f}%</b>"
        else:
            header = f"\n<b>Promoters {found.promoter_pct:.2f}%</b>"
        if len(found.promoters) > len(shown):
            header += f" <i>({len(found.promoters)} entities)</i>"
        lines.append(header)
        lines.extend(_line(holder) for holder in shown)
        if len(found.promoters) > len(shown):
            lines.append(_rollup(found.promoters[len(shown):]))
    else:
        lines.append("\n<i>No promoter holding reported.</i>")

    named = [holder for holder in found.public if not holder.undetermined]
    if named:
        shown = named[:public_limit]
        header = "\n<b>Largest public holders</b>"
        if len(named) > len(shown):
            header += f" <i>(top {len(shown)} of {len(named)})</i>"
        lines.append(header)
        lines.extend(_line(holder) for holder in shown)

    lines.extend(_adjustment_lines(found))

    stamp = found.quarter or ""
    if found.as_of:
        stamp = f"{stamp} (as of {found.as_of:%d %b %Y})" if stamp else f"as of {found.as_of:%d %b %Y}"
    footer = f"\nBSE shareholding pattern * {html.escape(stamp)}"
    lines.append(footer)
    return "\n".join(lines)
