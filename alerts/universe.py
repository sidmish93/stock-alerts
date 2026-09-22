"""The list of companies to watch.

One NSE symbol per line, optionally followed by a comma and a display name:

    RELIANCE, Reliance Industries
    JSWINFRA
    # anything after a hash is ignored

Non-coverage PE names add a flag and the book(s) they came from, so the alert
can say so without treating them as Spark coverage:

    MEESHO, Meesho | noncoverage | Steadview
    CLEANMAX, Clean Max | noncoverage | Steadview; Temasek
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

    def coverage_label(self) -> str:
        """Empty for coverage names; otherwise the line that goes on the alert."""
        if self.coverage:
            return ""
        if self.buckets:
            return "non coverage company · " + ", ".join(self.buckets)
        return "non coverage company"


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
        symbol = symbol.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        watched.append(_parse_rest(symbol, rest))
    return watched


def _parse_rest(symbol: str, rest: str) -> Watched:
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
    return Watched(symbol=symbol, name=name, coverage=coverage, buckets=unique)
