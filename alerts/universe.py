"""The list of companies to watch.

One NSE symbol per line, optionally followed by a comma and a display name:

    RELIANCE, Reliance Industries
    JSWINFRA
    # anything after a hash is ignored

A BSE-only listing keeps its Yahoo suffix, which is dropped from the symbol
the alert shows:

    NSE.BO, National Stock Exchange

Older lines may still carry `| noncoverage | Firm`. Alerts do not mention it.
"""

from dataclasses import dataclass, field

from .config import UNIVERSE_FILE

_NONCOVERAGE = {"noncoverage", "non-coverage", "non coverage"}


@dataclass
class Watched:
    symbol: str
    name: str = ""
    coverage: bool = True
    buckets: tuple[str, ...] = field(default_factory=tuple)
    # Yahoo series. NS for an NSE equity, BO when the name is listed only on BSE.
    exchange: str = "NS"

    def coverage_label(self) -> str:
        """The old coverage line. Alerts no longer include it."""
        if self.coverage:
            return ""
        if self.buckets:
            return "non coverage company · " + ", ".join(self.buckets)
        return "non coverage company"


def _split_exchange(symbol: str) -> tuple[str, str]:
    """('NSE', 'BO') from NSE.BO. Anything else stays on the NSE series."""
    text = (symbol or "").strip().upper()
    base, dot, suffix = text.rpartition(".")
    if dot and base and suffix in {"NS", "BO"}:
        return base, suffix
    return text, "NS"


def load(path=None) -> list[Watched]:
    path = path or UNIVERSE_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read the universe file at {path}: {exc}") from exc

    watched: list[Watched] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        symbol, _, rest = text.partition(",")
        symbol, exchange = _split_exchange(symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        watched.append(_parse_rest(symbol, rest, exchange))
    return watched


def _parse_rest(symbol: str, rest: str, exchange: str = "NS") -> Watched:
    parts = [part.strip() for part in rest.split("|")]
    name = parts[0] if parts else ""
    coverage = True
    buckets: list[str] = []
    for part in parts[1:]:
        if not part:
            continue
        if part.lower() in _NONCOVERAGE:
            coverage = False
            continue
        buckets.extend(piece.strip() for piece in part.split(";") if piece.strip())
    # Deduplicate while keeping the order written in the file.
    unique = tuple(dict.fromkeys(buckets))
    return Watched(
        symbol=symbol, name=name, coverage=coverage, buckets=unique, exchange=exchange
    )
