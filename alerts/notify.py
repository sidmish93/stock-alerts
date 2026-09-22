"""Telegram delivery and the wording of each alert."""

import html

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .http import post_json
from .triggers import DAILY, INTRADAY, VOLUME

API = "https://api.telegram.org/bot{token}/sendMessage"

UP = "\U0001f7e2"
DOWN = "\U0001f534"
SURGE = "\U0001f4ca"
WARN = "\u26a0\ufe0f"


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send(text: str) -> None:
    """Post one message. Raises if Telegram rejects it."""
    if not configured():
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set.")
    post_json(
        API.format(token=TELEGRAM_BOT_TOKEN),
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def indian_number(value) -> str:
    """1,51,22,715 rather than 15,122,715."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return "-"
    sign, digits = ("-" if number < 0 else ""), str(abs(number))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups + [tail])


def shares(value) -> str:
    """Volume in crore or lakh, which is how the figure is actually read."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1_00_00_000:
        return f"{number / 1_00_00_000:.2f} cr"
    if number >= 1_00_000:
        return f"{number / 1_00_000:.2f} lakh"
    return indian_number(number)


def _signed(value) -> str:
    return f"{value:+.2f}%"


def coverage_note(coverage: bool = True, buckets=()) -> str:
    """The line that marks a PE-book name that is not on the coverage list."""
    if coverage:
        return ""
    firms = ", ".join(html.escape(str(bucket)) for bucket in buckets if bucket)
    if firms:
        return f"<i>non coverage company · {firms}</i>"
    return "<i>non coverage company</i>"


def format_alert(
    trigger,
    quote,
    reading,
    name: str = "",
    coverage: bool = True,
    buckets=(),
) -> str:
    """One alert, as it appears on the phone."""
    symbol = html.escape(trigger.symbol)
    title = html.escape(name) if name and name.upper() != trigger.symbol else ""

    # The headline names the rule that fired, because three different things can
    # send a message and the number alone does not say which one did.
    if trigger.kind == VOLUME:
        icon = SURGE
        headline = f"volume past {trigger.level:g}x the 5-day average"
    else:
        icon = UP if trigger.direction == "up" else DOWN
        basis = "from today's open" if trigger.kind == INTRADAY else "vs prev close"
        headline = f"{trigger.direction} past {trigger.level:g}% {basis}"

    lines = [f"{icon} <b>{symbol}</b>  {headline}"]
    if title:
        lines.append(f"<i>{title}</i>")
    note = coverage_note(coverage, buckets)
    if note:
        lines.append(note)

    if quote.cmp is not None:
        detail = f"CMP Rs {quote.cmp:,.2f}"
        if quote.previous_close:
            detail += f"   prev close Rs {quote.previous_close:,.2f}"
        lines.append(detail)

    # Both readings appear whichever rule fired, so the one that did not trigger
    # is still there as context.
    moves = []
    if quote.daily_return_pct is not None:
        moves.append(f"vs prev close {_signed(quote.daily_return_pct)}")
    if quote.intraday_return_pct is not None:
        moves.append(f"from open {_signed(quote.intraday_return_pct)}")
    if moves:
        lines.append("   ".join(moves))

    lines.extend(_volume_lines(quote, reading))

    for caution in reading.cautions or []:
        lines.append(f"{WARN} <i>{html.escape(caution)}</i>")

    stamp = []
    if quote.quote_time:
        stamp.append(f"{quote.quote_time} IST")
    if quote.session_date:
        stamp.append(quote.session_date.strftime("%d %b"))
    if stamp:
        lines.append(f"<i>{' * '.join(stamp)}</i>")

    return "\n".join(lines)


def _volume_lines(quote, reading) -> list[str]:
    """Volume so far, and what it implies for the full day."""
    if quote.volume is None:
        return []
    lines = [f"Volume {shares(quote.volume)} so far"]
    if reading.volume_multiple is None or not reading.baseline_volume:
        return lines

    average = shares(reading.baseline_volume)
    if reading.paced and reading.projected_volume and reading.pace_fraction:
        lines.append(
            f"On pace for {shares(reading.projected_volume)} "
            f"vs {average} 5-day avg  ({reading.volume_multiple:.2f}x)"
        )
        lines.append(
            f"<i>{reading.pace_fraction * 100:.0f}% of a normal day has traded by now</i>"
        )
    else:
        # No intraday profile, so this is raw volume against a whole day and will
        # read low in the morning. Say so rather than imply a paced number.
        lines.append(
            f"{reading.volume_multiple:.2f}x the {average} 5-day avg, unpaced"
        )
    return lines


_KIND_ORDER = {DAILY: 0, INTRADAY: 1, VOLUME: 2}


def _reading_text(entry) -> str:
    kind, value = entry.get("kind"), entry.get("value")
    if kind == VOLUME:
        return f"volume {value:.2f}x avg"
    basis = "from open" if kind == INTRADAY else "vs prev close"
    return f"{value:+.2f}% {basis}"


def format_digest(day, entries, watched: int, notes: dict | None = None) -> str:
    """The after-close summary of everything that fired."""
    heading = f"<b>Close summary * {day:%d %b %Y}</b>"
    if not entries:
        return f"{heading}\nNothing triggered across {watched} names."

    labels = notes or {}
    lines = [heading, f"{len(entries)} alerts across {watched} names watched.", ""]
    ordered = sorted(
        entries,
        key=lambda item: (item.get("symbol", ""), _KIND_ORDER.get(item.get("kind"), 9)),
    )
    for entry in ordered:
        symbol = html.escape(str(entry.get("symbol", "")))
        at = entry.get("at")
        line = f"* <b>{symbol}</b>  {_reading_text(entry)}" + (f"  ({at})" if at else "")
        extra = labels.get(entry.get("symbol")) or labels.get(str(entry.get("symbol", "")).upper())
        if extra:
            line += f"  <i>{html.escape(str(extra))}</i>"
        lines.append(line)
    return "\n".join(lines)


def format_blind_warning(failed, watched: int) -> str:
    """Sent when too much of the universe cannot be priced.

    Without this, a broken feed looks exactly like a calm market.
    """
    names = ", ".join(html.escape(symbol) for symbol in sorted(failed)[:12])
    if len(failed) > 12:
        names += f" and {len(failed) - 12} more"
    return (
        f"{WARN} <b>Watcher is partly blind</b>\n"
        f"No quote for {len(failed)} of {watched} names: {names}\n"
        "<i>Treat quiet alerts with suspicion until this clears.</i>"
    )
