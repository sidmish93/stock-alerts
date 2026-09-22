"""Which levels are currently latched, so you are told once per crossing.

Each level of each rule is a latch. It fires when the reading first goes above
the level, and then stays quiet however long the reading sits there. It only
arms again once the reading has fallen back below the level by a small margin,
so a stock that eases to 4.9% and pushes through 5% again is reported again,
while one hovering on 4.999/5.001 is not.

Up and down are separate latches, so a stock that runs +5% in the morning and
reverses to -5% after lunch reports both, and its upside latch re-arms on the
way down ready for a second attempt.

State is written to disk because every run would otherwise replay the day's
alerts from scratch. On GitHub Actions each poll is a fresh machine, so this
file is committed back to the repository between runs.
"""

import json
from datetime import date

from .config import (
    REARM_MARGIN_PCT,
    STATE_FILE,
    VOLUME_REARM_MARGIN,
)
from .triggers import VOLUME, Trigger, candidates


def rearm_margin(kind: str) -> float:
    return VOLUME_REARM_MARGIN if kind == VOLUME else REARM_MARGIN_PCT


class AlertLog:
    def __init__(self, path=STATE_FILE):
        self.path = path
        self.day: str = ""
        # Latched levels, keyed symbol|kind|direction|level, holding the reading
        # that fired them. Present means latched.
        self.latched: dict[str, float] = {}
        self.fired: list[dict] = []
        # Persisted so a run started after the close does not send a second
        # digest for a day that has already been summarised.
        self.digested: bool = False
        self.warned_blind: bool = False
        # Symbols whose shareholding has already been sent today. The pattern is
        # quarterly data, so once is enough however many levels fire.
        self.shared: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        self.day = payload.get("day", "") or ""
        latched = payload.get("latched")
        if isinstance(latched, dict):
            self.latched = {key: float(value) for key, value in latched.items()}
        fired = payload.get("fired")
        self.fired = fired if isinstance(fired, list) else []
        self.digested = bool(payload.get("digested"))
        self.warned_blind = bool(payload.get("warned_blind"))
        shared = payload.get("shared")
        self.shared = [str(item) for item in shared] if isinstance(shared, list) else []

    def save(self) -> None:
        payload = {
            "day": self.day,
            "latched": self.latched,
            "fired": self.fired,
            "digested": self.digested,
            "warned_blind": self.warned_blind,
            "shared": self.shared,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass

    def start_day(self, day: date) -> bool:
        """Clear the log when the session changes. True if this is a new day."""
        stamp = day.isoformat()
        if self.day == stamp:
            return False
        self.day = stamp
        self.latched = {}
        self.fired = []
        self.digested = False
        self.warned_blind = False
        self.shared = []
        self.save()
        return True

    @staticmethod
    def _key(symbol: str, candidate) -> str:
        return f"{symbol}|{candidate.kind}|{candidate.direction}|{candidate.level:g}"

    def advance(self, reading) -> list[Trigger]:
        """Update the latches from a fresh reading and return what to send."""
        to_send = []
        for candidate in candidates(reading):
            key = self._key(reading.symbol, candidate)
            latched = key in self.latched

            if candidate.magnitude > candidate.level:
                if not latched:
                    self.latched[key] = candidate.magnitude
                    to_send.append(
                        Trigger(
                            symbol=reading.symbol,
                            kind=candidate.kind,
                            level=candidate.level,
                            value=candidate.value,
                            direction=candidate.direction,
                        )
                    )
            elif latched and candidate.magnitude <= candidate.level - rearm_margin(candidate.kind):
                # Fallen clear of the level: ready to report the next crossing.
                # Inclusive, so a 5% level with a 0.1 margin re-arms at exactly
                # 2.9% rather than needing to undershoot it.
                del self.latched[key]
        return to_send

    def record(self, trigger, quote=None) -> None:
        self.fired.append(
            {
                "symbol": trigger.symbol,
                "kind": trigger.kind,
                "level": trigger.level,
                "value": trigger.value,
                "direction": trigger.direction,
                "at": getattr(quote, "quote_time", None),
            }
        )
