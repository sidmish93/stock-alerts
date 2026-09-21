"""Session gating and the after-close digest."""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import market_hours  # noqa: E402
from alerts.notify import format_digest  # noqa: E402
from alerts.state import AlertLog  # noqa: E402
from alerts.triggers import DAILY, VOLUME  # noqa: E402

IST = market_hours.IST


def moment(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST)


class SessionWindow(unittest.TestCase):
    def setUp(self):
        # Friday 18 Sep 2026 is a trading day; 19-20 Sep is the weekend.
        market_hours._holidays = {date(2026, 10, 2)}

    def tearDown(self):
        market_hours._holidays = None

    def test_open_only_between_nine_fifteen_and_three_thirty(self):
        self.assertFalse(market_hours.is_open(moment(2026, 9, 18, 9, 14)))
        self.assertTrue(market_hours.is_open(moment(2026, 9, 18, 9, 15)))
        self.assertTrue(market_hours.is_open(moment(2026, 9, 18, 12, 0)))
        self.assertTrue(market_hours.is_open(moment(2026, 9, 18, 15, 30)))
        self.assertFalse(market_hours.is_open(moment(2026, 9, 18, 15, 31)))

    def test_weekends_and_holidays_are_shut(self):
        self.assertFalse(market_hours.is_open(moment(2026, 9, 19, 12, 0)))  # Saturday
        self.assertFalse(market_hours.is_open(moment(2026, 9, 20, 12, 0)))  # Sunday
        self.assertFalse(market_hours.is_open(moment(2026, 10, 2, 12, 0)))  # holiday

    def test_sleep_from_friday_evening_lands_on_monday_open(self):
        friday_evening = moment(2026, 9, 18, 18, 0)
        seconds = market_hours.seconds_until_open(friday_evening)
        target = friday_evening + timedelta(seconds=seconds)
        self.assertEqual(target, moment(2026, 9, 21, 9, 15))

    def test_sleep_before_the_bell_waits_for_the_same_morning(self):
        early = moment(2026, 9, 18, 7, 0)
        target = early + timedelta(seconds=market_hours.seconds_until_open(early))
        self.assertEqual(target, moment(2026, 9, 18, 9, 15))

    def test_sleep_skips_a_holiday_that_falls_on_a_weekday(self):
        # 2 Oct 2026 is a Friday holiday, so Thursday evening waits for Monday.
        thursday = moment(2026, 10, 1, 17, 0)
        target = thursday + timedelta(seconds=market_hours.seconds_until_open(thursday))
        self.assertEqual(target, moment(2026, 10, 5, 9, 15))


class Digest(unittest.TestCase):
    def test_a_quiet_day_says_so(self):
        text = format_digest(date(2026, 9, 18), [], watched=12)
        self.assertIn("Nothing triggered", text)
        self.assertIn("12", text)

    def test_a_busy_day_lists_every_reading(self):
        entries = [
            {"symbol": "JSWINFRA", "kind": DAILY, "value": 6.61, "at": "15:30"},
            {"symbol": "TRAVELFOOD", "kind": VOLUME, "value": 1.97, "at": "15:29"},
        ]
        text = format_digest(date(2026, 9, 18), entries, watched=5)
        self.assertIn("JSWINFRA", text)
        self.assertIn("+6.61%", text)
        self.assertIn("volume 1.97x avg", text)
        self.assertIn("2 alerts", text)


class DigestRidesOnThePollSchedule(unittest.TestCase):
    """One trigger has to cover both jobs.

    The close summary used to need its own scheduled trigger. With the schedule
    living outside GitHub that would mean a second cron job to create and a
    second thing to fail, so the first poll after the close sends it instead.
    """

    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_ride_state.json"
        self.path.unlink(missing_ok=True)
        from alerts import worker

        self.worker = worker
        self.sent = []
        self.original_send = worker.notify.send
        self.original_load = worker.universe.load
        self.original_now = market_hours.now_ist
        worker.notify.send = lambda text: self.sent.append(text)
        worker.universe.load = lambda: [
            type("W", (), {"symbol": "TEST", "name": "Test"})()
        ]
        worker.AlertLog = lambda path=self.path: AlertLog(path=self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        self.worker.notify.send = self.original_send
        self.worker.universe.load = self.original_load
        market_hours.now_ist = self.original_now
        self.worker.AlertLog = AlertLog

    def _at(self, hour, minute=0):
        market_hours.now_ist = lambda: moment(2026, 9, 21, hour, minute)

    def test_the_first_poll_after_the_close_sends_the_summary(self):
        log = AlertLog(path=self.path)
        log.start_day(date(2026, 9, 21))
        log.record(
            type("T", (), {"symbol": "TEST", "kind": DAILY, "level": 5.0,
                           "value": 6.1, "direction": "up"})()
        )
        log.save()

        self._at(16, 0)  # after the 15:30 close
        self.worker.run_once()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Close summary", self.sent[0])
        self.assertTrue(AlertLog(path=self.path).digested)

    def test_it_is_not_sent_twice_by_later_polls(self):
        log = AlertLog(path=self.path)
        log.start_day(date(2026, 9, 21))
        log.save()
        self._at(16, 0)
        self.worker.run_once()
        self.worker.run_once()
        self.assertEqual(len(self.sent), 1)

    def test_a_closed_market_with_nothing_recorded_stays_silent(self):
        self._at(7, 0)  # before the open, no session recorded
        self.worker.run_once()
        self.assertEqual(self.sent, [])


class DigestOnce(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_digest_test.json"
        self.path.unlink(missing_ok=True)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_the_digest_flag_survives_a_restart_after_the_close(self):
        log = AlertLog(path=self.path)
        log.start_day(date(2026, 9, 18))
        self.assertFalse(log.digested)
        log.digested = True
        log.save()
        reopened = AlertLog(path=self.path)
        self.assertTrue(reopened.digested)
        # The next session clears it again.
        reopened.start_day(date(2026, 9, 21))
        self.assertFalse(reopened.digested)


if __name__ == "__main__":
    unittest.main(verbosity=2)
