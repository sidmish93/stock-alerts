"""When NSE is open, in IST.

The holiday list is a convenience only. It saves pointless polling on a closed
day, but it is not what keeps a stale quote from alerting: that is the session
date check in triggers.evaluate, which holds even if this list goes out of date.
"""

from datetime import date, datetime, time, timedelta, timezone

from .config import HOLIDAYS_FILE

IST = timezone(timedelta(hours=5, minutes=30))

OPEN_TIME = time(9, 15)
CLOSE_TIME = time(15, 30)

_holidays: set[date] | None = None


def now_ist() -> datetime:
    return datetime.now(IST)


def holidays() -> set[date]:
    """Trading holidays, one ISO date per line. Blank lines and # are ignored."""
    global _holidays
    if _holidays is None:
        found: set[date] = set()
        try:
            for line in HOLIDAYS_FILE.read_text(encoding="utf-8").splitlines():
                text = line.split("#", 1)[0].strip()
                if not text:
                    continue
                try:
                    found.add(date.fromisoformat(text))
                except ValueError:
                    continue
        except OSError:
            pass
        _holidays = found
    return _holidays


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays()


def is_open(moment: datetime | None = None) -> bool:
    moment = moment or now_ist()
    if not is_trading_day(moment.date()):
        return False
    return OPEN_TIME <= moment.time() <= CLOSE_TIME


def seconds_until_open(moment: datetime | None = None) -> float:
    """How long to sleep before the next session starts."""
    moment = moment or now_ist()
    candidate = moment
    for _ in range(0, 30):
        opening = datetime.combine(candidate.date(), OPEN_TIME, tzinfo=IST)
        if is_trading_day(candidate.date()) and opening > moment:
            return (opening - moment).total_seconds()
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=0, minute=0, second=0, microsecond=0)
    # A month of holidays is not a real calendar; fall back to an hourly recheck.
    return 3600.0
