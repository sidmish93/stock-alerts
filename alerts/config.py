"""Settings for the live trigger watcher, read from the environment."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)


def _load_env_file(path: Path) -> None:
    """Read KEY=value lines from .env, for running locally without exporting.

    Real environment variables win, so a hosted deployment that sets them in its
    own dashboard is never overridden by a file that happened to get committed.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


_load_env_file(BASE_DIR / ".env")


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _levels(name: str, default: str) -> list[float]:
    """A comma separated list of trigger levels, smallest first."""
    raw = os.environ.get(name, "").strip() or default
    found = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            found.append(float(part))
        except ValueError:
            continue
    return sorted(set(found))


# Telegram. Both are required; the worker refuses to start without them.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Which price rules alert. Both are always shown in the message; this only
# decides which one is allowed to trigger one.
#   daily    CMP against yesterday's close. What Screener's "1day return %",
#            NSE, BSE and Google Finance all quote, so it is the figure you can
#            cross-check anywhere. Includes the overnight gap.
#   intraday CMP against today's open. The move made during the session only,
#            with the gap excluded.
def _price_rules() -> set[str]:
    raw = os.environ.get("PRICE_RULES", "").strip() or "daily"
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    valid = wanted & {"daily", "intraday"}
    # An unreadable setting must not silently switch every price alert off.
    return valid or {"daily"}


PRICE_RULES = _price_rules()

# Price levels, in percent. Each fires on its own, so a stock running to 5.4%
# alerts once at 3% and again at 5%.
MOVE_LEVELS = _levels("MOVE_LEVELS", "3,5")

# Volume levels, as a multiple of the average. Empty by default: with ~300 names
# a 1.5x threshold was crossed by 16% of the universe on an ordinary day and 47%
# on an index-rebalance day, which reports the market rather than a stock.
# Volume still appears in every alert as context, it just does not raise one.
# Set e.g. VOLUME_LEVELS=2.5,4 to turn the trigger back on.
VOLUME_LEVELS = _levels("VOLUME_LEVELS", "")

# How far a reading must fall back before that level can fire again. Without a
# margin a stock sitting on 3.00% would alert every time it wobbled a hundredth
# of a point either side.
REARM_MARGIN_PCT = _number("REARM_MARGIN_PCT", 0.1)
VOLUME_REARM_MARGIN = _number("VOLUME_REARM_MARGIN", 0.1)

# Number of completed sessions averaged for the volume baseline.
BASELINE_SESSIONS = int(_number("BASELINE_SESSIONS", 5))

# Compare today's volume against the share of a day that has normally traded by
# this time, rather than against a whole day. Without this a surge cannot reach
# 1.5x until the afternoon, because by 10am a stock has only traded a fraction
# of its day.
VOLUME_PACING = _flag("VOLUME_PACING", True)
# Sessions used for the shape of the intraday curve. More than the baseline on
# purpose: the shape is a slow-moving property, and a wider sample stops one
# expiry day with a huge closing auction from distorting it.
PROFILE_SESSIONS = int(_number("PROFILE_SESSIONS", 10))
INTRADAY_INTERVAL = os.environ.get("INTRADAY_INTERVAL", "15m").strip()
# Projecting a full day from the first few minutes divides by almost nothing, so
# the volume trigger stays quiet until this much of a normal day has traded.
MIN_PROFILE_FRACTION = _number("MIN_PROFILE_FRACTION", 0.03)

# Price alerts at or above this level are followed by the company's named
# shareholders from BSE, as a second message. Set to 0 to switch it off.
SHAREHOLDING_AT_PCT = _number("SHAREHOLDING_AT_PCT", 5.0)
# Holders listed individually, 0 meaning all of them, which is the default. Even
# the widest registers are comfortable: Infosys reports 24 promoter entities and
# Policybazaar 22 named public holders, and a full note for either runs about
# 2,100 characters against Telegram's 4,096 limit. The note is trimmed only if it
# would actually breach that, never on a fixed count.
SHAREHOLDING_PUBLIC_HOLDERS = int(_number("SHAREHOLDING_PUBLIC_HOLDERS", 0))
SHAREHOLDING_PROMOTERS = int(_number("SHAREHOLDING_PROMOTERS", 0))
# Optional ceiling on shareholding notes per day. 0 means no limit, which is the
# default: every name that crosses the level gets its ownership note.
SHAREHOLDING_MAX_PER_DAY = int(_number("SHAREHOLDING_MAX_PER_DAY", 0))
# A pattern is as-of its quarter end, so block and bulk deals printed since then
# are applied in share counts to restate each holder's position.
POST_SHP_ADJUSTMENT = _flag("POST_SHP_ADJUSTMENT", True)

# Beyond this, a move is more likely bad data or an unadjusted corporate action
# than a real price change. It is still sent, but flagged for a second look.
IMPLAUSIBLE_MOVE_PCT = _number("IMPLAUSIBLE_MOVE_PCT", 25.0)
# A quote this far behind the clock means the feed has stalled.
STALE_QUOTE_MINUTES = int(_number("STALE_QUOTE_MINUTES", 30))
# Warn once a day if this share of the universe cannot be priced, so silence is
# never mistaken for calm.
BLIND_WARNING_SHARE = _number("BLIND_WARNING_SHARE", 0.5)

# How often to re-poll when running as a long-lived process.
POLL_SECONDS = int(_number("POLL_SECONDS", 60))
QUOTE_WORKERS = int(_number("QUOTE_WORKERS", 8))
REQUEST_TIMEOUT = int(_number("REQUEST_TIMEOUT", 25))

# Daily bars and the intraday profile only change once a day.
HISTORY_TTL_SECONDS = int(_number("HISTORY_TTL_SECONDS", 6 * 3600))
HISTORY_RANGE = os.environ.get("HISTORY_RANGE", "1mo").strip()

UNIVERSE_FILE = Path(os.environ.get("UNIVERSE_FILE", BASE_DIR / "universe.txt"))
HOLIDAYS_FILE = Path(os.environ.get("HOLIDAYS_FILE", BASE_DIR / "holidays.txt"))
# Alert history, so a restart does not replay the day's alerts. Kept outside
# .cache because on GitHub Actions it is committed back to the repository.
STATE_FILE = Path(os.environ.get("STATE_FILE", BASE_DIR / "state" / "alerts.json"))

SEND_CLOSE_DIGEST = _flag("SEND_CLOSE_DIGEST", True)
SEND_OPEN_PING = _flag("SEND_OPEN_PING", True)
