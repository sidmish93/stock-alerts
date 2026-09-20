"""The list of companies to watch.

One NSE symbol per line, optionally followed by a comma and a display name:

    RELIANCE, Reliance Industries
    JSWINFRA
    # anything after a hash is ignored
"""

from dataclasses import dataclass

from .config import UNIVERSE_FILE


@dataclass
class Watched:
    symbol: str
    name: str = ""


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
        symbol, _, name = text.partition(",")
        symbol = symbol.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        watched.append(Watched(symbol=symbol, name=name.strip()))
    return watched
