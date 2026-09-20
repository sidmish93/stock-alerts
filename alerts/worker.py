"""The watcher.

One pass and exit, which is how it runs on a schedule such as GitHub Actions:

    python -m alerts.worker --once
    python -m alerts.worker --digest      send the after-close summary
    python -m alerts.worker --test        send a test message and exit
    python -m alerts.worker --dry-run     print alerts instead of sending them
    python -m alerts.worker --force       ignore market hours

Or continuously, for a machine that is always on:

    python -m alerts.worker
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import market_hours, notify, shareholding, universe
from .config import (
    BLIND_WARNING_SHARE,
    POLL_SECONDS,
    QUOTE_WORKERS,
    SEND_CLOSE_DIGEST,
    SEND_OPEN_PING,
    SHAREHOLDING_AT_PCT,
    SHAREHOLDING_MAX_PER_DAY,
    STALE_QUOTE_MINUTES,
)
from .quotes import fetch_history, fetch_quote
from .state import AlertLog
from .triggers import DAILY, INTRADAY, measure


def _use_utf8_stdout() -> None:
    """A Windows console defaults to cp1252, which cannot encode the alert icons,
    so logging a message would raise instead of printing it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def log(message: str) -> None:
    print(f"[{market_hours.now_ist():%Y-%m-%d %H:%M:%S} IST] {message}", flush=True)


def wants_shareholding(trigger) -> bool:
    """True for a price move big enough to be worth the ownership context."""
    return (
        SHAREHOLDING_AT_PCT > 0
        and trigger.kind in (DAILY, INTRADAY)
        and trigger.level >= SHAREHOLDING_AT_PCT
    )


def _send_shareholding(watched, alert_log, dry_run: bool) -> None:
    """Follow a big move with the named shareholders, as a second message.

    Sent separately and after the alert, so a slow or unavailable BSE response
    can never delay or swallow the alert itself.
    """
    if watched.symbol in alert_log.shared:
        return
    # Zero means no ceiling, so the comparison has to be guarded: len() >= 0 is
    # always true and would otherwise suppress every note.
    if SHAREHOLDING_MAX_PER_DAY and len(alert_log.shared) >= SHAREHOLDING_MAX_PER_DAY:
        log(f"Shareholding cap of {SHAREHOLDING_MAX_PER_DAY} reached; skipping {watched.symbol}.")
        return
    alert_log.shared.append(watched.symbol)
    try:
        found = shareholding.fetch(watched.symbol)
    except Exception as exc:
        log(f"Shareholding lookup failed for {watched.symbol}: {exc}")
        return
    if not found:
        log(f"No BSE shareholding pattern for {watched.symbol}.")
        return
    message = shareholding.format_message(found, watched.name)
    if dry_run:
        log(f"would send:\n{message}\n")
        return
    try:
        notify.send(message)
    except Exception as exc:
        log(f"Shareholding send failed for {watched.symbol}: {exc}")


def _snapshot(watched):
    """Live quote plus the once-a-day history for one company."""
    quote = fetch_quote(watched.symbol)
    history = fetch_history(watched.symbol, quote.session_date) if not quote.error else None
    return watched, quote, history


def _deliver(message: str, label: str, dry_run: bool) -> bool:
    if dry_run:
        log(f"would send:\n{message}\n")
        return True
    try:
        notify.send(message)
        return True
    except Exception as exc:
        log(f"Telegram send failed for {label}: {exc}")
        return False


def poll(watched_list, alert_log: AlertLog, today: date, dry_run: bool = False) -> int:
    """One pass over the universe. Returns how many messages were sent."""
    workers = max(1, min(QUOTE_WORKERS, len(watched_list)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        snapshots = list(pool.map(_snapshot, watched_list))

    now = market_hours.now_ist()
    failed = []
    lagging = []
    changed = False
    sent = 0

    for watched, quote, history in snapshots:
        if quote.error or quote.cmp is None:
            failed.append(watched.symbol)
            continue
        # Yahoo serves the last close once trading ends, so a quote is only ever
        # judged against the session it actually belongs to.
        if quote.session_date != today:
            failed.append(f"{watched.symbol} (stale: {quote.session_date})")
            continue
        if quote.quote_at and (now - quote.quote_at) > timedelta(minutes=STALE_QUOTE_MINUTES):
            lagging.append(int((now - quote.quote_at).total_seconds() / 60))

        reading = measure(quote, history, now=quote.quote_at)
        for trigger in alert_log.advance(reading):
            message = notify.format_alert(trigger, quote, reading, watched.name)
            if not _deliver(message, watched.symbol, dry_run):
                continue
            alert_log.record(trigger, quote)
            sent += 1
            if wants_shareholding(trigger):
                _send_shareholding(watched, alert_log, dry_run)
        changed = True

    if lagging:
        # One line for the whole universe: a lagging feed is usually lagging for
        # everything at once, and 300 identical lines bury the real output.
        log(f"Feed behind for {len(lagging)} names, worst {max(lagging)} min.")

    # A re-armed latch is a state change worth persisting even with nothing sent.
    if changed or sent:
        alert_log.save()

    if failed:
        log(f"No usable quote for {', '.join(failed)}")
        share = len(failed) / max(1, len(watched_list))
        if share >= BLIND_WARNING_SHARE and not alert_log.warned_blind:
            alert_log.warned_blind = True
            alert_log.save()
            warning = notify.format_blind_warning(failed, len(watched_list))
            if dry_run:
                log(f"would send:\n{warning}\n")
            else:
                try:
                    notify.send(warning)
                except Exception as exc:
                    log(f"Blind warning failed: {exc}")
    return sent


def _universe_or_exit():
    watched_list = universe.load()
    if not watched_list:
        log("The universe file is empty; nothing to watch.")
    return watched_list


def run_once(dry_run: bool = False, force: bool = False) -> int:
    watched_list = _universe_or_exit()
    if not watched_list:
        return 0

    moment = market_hours.now_ist()
    if not force and not market_hours.is_open(moment):
        log("Market is closed; nothing to poll. Use --force to poll anyway.")
        return 0

    alert_log = AlertLog()
    today = moment.date()
    if alert_log.start_day(today):
        log(f"New session {today}.")
        if SEND_OPEN_PING and not dry_run:
            try:
                notify.send(
                    f"Watching {len(watched_list)} names today ({today:%d %b})."
                )
            except Exception as exc:
                log(f"Open ping failed: {exc}")

    log(f"Polling {len(watched_list)} names.")
    sent = poll(watched_list, alert_log, today, dry_run=dry_run)
    log(f"{sent} alert(s) {'would be sent' if dry_run else 'sent'}.")
    return sent


def send_digest(dry_run: bool = False) -> int:
    """The after-close summary, for the session the state file describes."""
    watched_list = universe.load()
    alert_log = AlertLog()
    if not alert_log.day:
        log("No session recorded; nothing to summarise.")
        return 0
    if alert_log.digested:
        log("Digest already sent for this session.")
        return 0

    day = date.fromisoformat(alert_log.day)
    text = notify.format_digest(day, alert_log.fired, len(watched_list))
    alert_log.digested = True
    alert_log.save()
    if dry_run:
        log(f"would send:\n{text}\n")
        return 0
    try:
        notify.send(text)
        log("Digest sent.")
    except Exception as exc:
        log(f"Digest failed: {exc}")
    return 1


def watch(dry_run: bool = False) -> None:
    watched_list = _universe_or_exit()
    if not watched_list:
        return
    alert_log = AlertLog()
    log(
        f"Watching {len(watched_list)} names, polling every {POLL_SECONDS}s "
        f"between {market_hours.OPEN_TIME:%H:%M} and {market_hours.CLOSE_TIME:%H:%M} IST."
    )
    announced = alert_log.day == market_hours.now_ist().date().isoformat()

    while True:
        moment = market_hours.now_ist()
        today = moment.date()

        if market_hours.is_open(moment):
            if alert_log.start_day(today):
                announced = False
            if not announced:
                announced = True
                if SEND_OPEN_PING and not dry_run:
                    try:
                        notify.send(
                            f"Watching {len(watched_list)} names today ({today:%d %b})."
                        )
                    except Exception as exc:
                        log(f"Open ping failed: {exc}")
            try:
                sent = poll(watched_list, alert_log, today, dry_run=dry_run)
                if sent:
                    log(f"{sent} alert(s) sent.")
            except Exception as exc:
                log(f"Poll failed: {exc}")
            time.sleep(POLL_SECONDS)
            continue

        if alert_log.day == today.isoformat() and not alert_log.digested:
            if SEND_CLOSE_DIGEST:
                send_digest(dry_run=dry_run)
            else:
                alert_log.digested = True
                alert_log.save()

        nap = min(market_hours.seconds_until_open(moment), 1800)
        log(f"Market closed. Sleeping {int(nap / 60)} min.")
        time.sleep(max(30.0, nap))


def main(argv=None) -> int:
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Live NSE trigger alerts to Telegram.")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--digest", action="store_true", help="send the close summary and exit")
    parser.add_argument("--test", action="store_true", help="send a test message and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="print alerts instead of sending them"
    )
    parser.add_argument(
        "--force", action="store_true", help="poll even when the market is closed"
    )
    args = parser.parse_args(argv)

    if args.test:
        notify.send("Test alert from your NSE trigger watcher. Delivery works.")
        log("Test message sent.")
        return 0

    if not args.dry_run and not notify.configured():
        log("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set. See the README.")
        return 1

    if args.digest:
        send_digest(dry_run=args.dry_run)
        return 0

    if args.once:
        run_once(dry_run=args.dry_run, force=args.force)
        return 0

    try:
        watch(dry_run=args.dry_run)
    except KeyboardInterrupt:
        log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
