"""Readings, levels, latching and message formatting."""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import config, triggers  # noqa: E402
from alerts.notify import format_alert, indian_number, shares  # noqa: E402
from alerts.quotes import IST, History, Profile, Session, build_profile  # noqa: E402
from alerts.state import AlertLog  # noqa: E402
from alerts.triggers import (  # noqa: E402
    DAILY,
    INTRADAY,
    VOLUME,
    average_volume,
    candidates,
    measure,
)

FRIDAY = date(2026, 9, 18)


def at(hour, minute=0, day=FRIDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def session(day, volume, close=100.0):
    return Session(day=day, close=close, volume=volume)


class Quote:
    """Stands in for quotes.Quote without reaching the network."""

    def __init__(self, **fields):
        self.symbol = fields.get("symbol", "TEST")
        self.cmp = fields.get("cmp", 100.0)
        self.open = fields.get("open")
        self.previous_close = fields.get("previous_close")
        self.volume = fields.get("volume")
        self.session_date = fields.get("session_date", FRIDAY)
        self.quote_at = fields.get("quote_at", at(12, 0))
        self.intraday_return_pct = fields.get("intraday_return_pct")
        self.daily_return_pct = fields.get("daily_return_pct")
        self.error = fields.get("error")

    @property
    def quote_time(self):
        return f"{self.quote_at:%H:%M}" if self.quote_at else None


def flat_profile():
    """A day where volume arrives evenly: 25 quarter-hours from 09:15 to 15:30."""
    return Profile(points=[(minute, minute / 375) for minute in range(15, 376, 15)], sessions=5)


def history(sessions=None, profile=None, splits=None, dividends=None):
    return History(
        sessions=sessions if sessions is not None else [],
        profile=profile if profile is not None else Profile(),
        splits=splits or {},
        dividends=dividends or {},
    )


class Baseline(unittest.TestCase):
    def test_average_uses_the_last_five_completed_sessions(self):
        sessions = [session(date(2026, 9, day), day * 100) for day in (8, 9, 10, 11, 15, 16, 17)]
        sessions.append(session(FRIDAY, 99999))
        expected = (1000 + 1100 + 1500 + 1600 + 1700) / 5
        self.assertAlmostEqual(average_volume(sessions, FRIDAY), expected)

    def test_today_is_never_part_of_its_own_baseline(self):
        sessions = [session(date(2026, 9, 17), 100), session(FRIDAY, 10**9)]
        self.assertAlmostEqual(average_volume(sessions, FRIDAY), 100)

    def test_a_short_history_uses_what_it_has(self):
        sessions = [session(date(2026, 9, 16), 100), session(date(2026, 9, 17), 300)]
        self.assertAlmostEqual(average_volume(sessions, FRIDAY), 200)

    def test_no_history_gives_no_baseline(self):
        self.assertIsNone(average_volume([], FRIDAY))


class IntradayProfile(unittest.TestCase):
    def test_a_bar_counts_only_once_it_has_finished(self):
        # One session, two bars: 09:15-09:30 carries 40%, 09:30-09:45 the rest.
        built = build_profile({date(2026, 9, 17): [(0, 40.0), (15, 60.0)]}, bar_minutes=15)
        self.assertEqual(built.points, [(15, 0.4), (30, 1.0)])
        # At 09:15 nothing has traded yet, not 40%.
        self.assertEqual(built.fraction_by(at(9, 15)), 0.0)
        # Halfway through the first bar, half of its 40%.
        self.assertAlmostEqual(built.fraction_by(at(9, 22)), 0.4 * (7 / 15), places=3)
        self.assertAlmostEqual(built.fraction_by(at(9, 30)), 0.4)

    def test_the_curve_is_a_median_so_one_freak_day_cannot_skew_it(self):
        # Four ordinary days plus an expiry day whose closing bar is most of it.
        ordinary = {"a": [(0, 50.0), (15, 50.0)]}
        days = {}
        for index, day in enumerate([13, 14, 15, 16]):
            days[date(2026, 9, day)] = list(ordinary["a"])
        days[date(2026, 9, 17)] = [(0, 1.0), (15, 99.0)]
        built = build_profile(days, bar_minutes=15)
        # Median of [0.5, 0.5, 0.5, 0.5, 0.01] is 0.5, not the 0.40 mean.
        self.assertAlmostEqual(built.fraction_by(at(9, 30)), 0.5)

    def test_todays_partial_session_is_excluded(self):
        days = {
            date(2026, 9, 17): [(0, 50.0), (15, 50.0)],
            FRIDAY: [(0, 5.0)],
        }
        built = build_profile(days, skip=FRIDAY, bar_minutes=15)
        self.assertEqual(built.sessions, 1)

    def test_the_curve_never_goes_backwards(self):
        built = build_profile(
            {date(2026, 9, d): [(0, 10.0), (15, 10.0), (30, 80.0)] for d in (15, 16, 17)},
            bar_minutes=15,
        )
        shares_seen = [share for _, share in built.points]
        self.assertEqual(shares_seen, sorted(shares_seen))
        self.assertAlmostEqual(shares_seen[-1], 1.0)

    def test_after_the_close_the_whole_day_has_traded(self):
        self.assertEqual(flat_profile().fraction_by(at(15, 30)), 1.0)
        self.assertEqual(flat_profile().fraction_by(at(16, 0)), 1.0)

    def test_an_empty_profile_reports_nothing_rather_than_zero(self):
        self.assertIsNone(Profile().fraction_by(at(12, 0)))


class PacedVolume(unittest.TestCase):
    """The reason for departing from the tracker: a morning surge must be
    visible in the morning."""

    def setUp(self):
        self.sessions = [session(date(2026, 9, day), 1_000_000) for day in (10, 11, 15, 16, 17)]

    def measure_at(self, moment, volume):
        quote = Quote(volume=volume, quote_at=moment, previous_close=100.0)
        return measure(quote, history(self.sessions, flat_profile()), now=moment)

    def test_a_morning_surge_is_caught_in_the_morning(self):
        # By 10:15 an even day has traded 16% of its volume, so 160k is normal.
        # 400k is two and a half times the pace.
        reading = self.measure_at(at(10, 15), 400_000)
        self.assertTrue(reading.paced)
        self.assertAlmostEqual(reading.pace_fraction, 60 / 375, places=3)
        self.assertAlmostEqual(reading.volume_multiple, 2.5, places=1)

    def test_the_unpaced_rule_would_have_missed_it(self):
        # The same 400k against a full day's million is 0.4x: silent until 3pm.
        # Patched on the triggers module, which binds the setting at import.
        triggers.VOLUME_PACING = False
        try:
            reading = self.measure_at(at(10, 15), 400_000)
            self.assertFalse(reading.paced)
            self.assertAlmostEqual(reading.volume_multiple, 0.4)
        finally:
            triggers.VOLUME_PACING = True

    def test_a_normal_day_tracks_close_to_one(self):
        reading = self.measure_at(at(12, 0), int(1_000_000 * (165 / 375)))
        self.assertAlmostEqual(reading.volume_multiple, 1.0, places=1)

    def test_the_first_minutes_are_too_thin_to_project(self):
        reading = self.measure_at(at(9, 17), 5_000)
        self.assertIsNone(reading.volume_multiple)
        self.assertIsNotNone(reading.pace_fraction)

    def test_without_a_profile_it_falls_back_to_the_raw_comparison(self):
        moment = at(10, 15)
        quote = Quote(volume=400_000, quote_at=moment)
        reading = measure(quote, history(self.sessions, Profile()), now=moment)
        self.assertFalse(reading.paced)
        self.assertAlmostEqual(reading.volume_multiple, 0.4)


class Cautions(unittest.TestCase):
    def test_an_implausible_move_is_flagged_but_still_reported(self):
        quote = Quote(daily_return_pct=-49.8, previous_close=100.0)
        reading = measure(quote, history())
        self.assertEqual(reading.daily_return_pct, -49.8)
        self.assertTrue(any("corporate action" in note for note in reading.cautions))

    def test_an_ex_split_day_is_flagged(self):
        quote = Quote(daily_return_pct=-4.0, previous_close=100.0)
        reading = measure(quote, history(splits={FRIDAY: "2:1"}))
        self.assertTrue(any("ex-split" in note for note in reading.cautions))

    def test_a_big_dividend_is_flagged_and_a_trivial_one_is_not(self):
        quote = Quote(daily_return_pct=-3.5, previous_close=100.0)
        big = measure(quote, history(dividends={FRIDAY: 4.0}))
        self.assertTrue(any("ex-dividend" in note for note in big.cautions))
        small = measure(quote, history(dividends={FRIDAY: 0.4}))
        self.assertEqual(small.cautions, [])

    def test_an_ordinary_move_carries_no_caution(self):
        reading = measure(Quote(daily_return_pct=-3.5, previous_close=100.0), history())
        self.assertEqual(reading.cautions, [])


class Latching(unittest.TestCase):
    """Alert on the crossing, stay quiet while it holds, report a fresh crossing."""

    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_latch_test.json"
        self.path.unlink(missing_ok=True)
        self.log = AlertLog(path=self.path)
        self.log.start_day(FRIDAY)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def reading(self, daily):
        return measure(Quote(daily_return_pct=daily, previous_close=100.0), history())

    def fire(self, daily):
        return self.log.advance(self.reading(daily))

    def test_it_alerts_on_the_crossing_and_then_holds_quiet(self):
        first = self.fire(5.2)
        self.assertEqual([(t.kind, t.level) for t in first], [(DAILY, 5.0)])
        for value in (5.3, 5.9, 7.0, 5.1):
            self.assertEqual(self.fire(value), [])

    def test_a_three_percent_move_does_not_alert(self):
        self.assertEqual(self.fire(3.4), [])
        self.assertEqual(self.fire(4.8), [])

    def test_falling_back_and_crossing_again_alerts_again(self):
        self.assertEqual(len(self.fire(5.2)), 1)
        self.assertEqual(self.fire(4.9), [])  # re-arms, no alert on the way down
        again = self.fire(5.1)
        self.assertEqual([t.level for t in again], [5.0])

    def test_hovering_on_the_level_does_not_re_alert(self):
        self.assertEqual(len(self.fire(5.01)), 1)
        # Inside the re-arm margin, so the latch holds.
        for value in (4.99, 5.02, 4.95, 5.05):
            self.assertEqual(self.fire(value), [])

    def test_drifting_higher_after_five_says_nothing_more(self):
        self.assertEqual([t.level for t in self.fire(5.2)], [5.0])
        self.assertEqual(self.fire(6.0), [])

    def test_a_jump_straight_past_five_reports_once(self):
        levels = sorted(t.level for t in self.fire(6.1))
        self.assertEqual(levels, [5.0])

    def test_a_reversal_reports_the_other_side(self):
        self.assertEqual([t.direction for t in self.fire(5.4)], ["up"])
        fired = self.fire(-5.6)
        self.assertEqual([t.direction for t in fired], ["down"])
        # The upside latch released on the way through, so a renewed rally alerts.
        self.assertEqual([t.direction for t in self.fire(5.7)], ["up"])

    def test_latches_survive_a_restart(self):
        self.fire(5.4)
        self.log.save()
        reopened = AlertLog(path=self.path)
        self.assertFalse(reopened.start_day(FRIDAY))
        self.assertEqual(reopened.advance(self.reading(5.4)), [])

    def test_a_new_session_clears_every_latch(self):
        self.fire(5.4)
        self.assertTrue(self.log.start_day(date(2026, 9, 21)))
        self.assertEqual(len(self.log.advance(self.reading(5.4))), 1)

    def test_intraday_and_daily_latch_separately(self):
        original = triggers.PRICE_RULES
        triggers.PRICE_RULES = {"daily", "intraday"}
        try:
            reading = measure(
                Quote(daily_return_pct=5.4, intraday_return_pct=5.6, previous_close=100.0),
                history(),
            )
            kinds = sorted(t.kind for t in self.log.advance(reading))
            self.assertEqual(kinds, [DAILY, INTRADAY])
        finally:
            triggers.PRICE_RULES = original


class VolumeLatching(unittest.TestCase):
    """The volume trigger ships switched off, but the machinery stays covered so
    that turning VOLUME_LEVELS back on is not a leap of faith."""

    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_vol_test.json"
        self.path.unlink(missing_ok=True)
        self.log = AlertLog(path=self.path)
        self.log.start_day(FRIDAY)
        self.sessions = [session(date(2026, 9, day), 1_000_000) for day in (10, 11, 15, 16, 17)]
        self.original = triggers.VOLUME_LEVELS
        triggers.VOLUME_LEVELS = [1.5, 3.0]

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        triggers.VOLUME_LEVELS = self.original

    def advance(self, moment, volume):
        quote = Quote(volume=volume, quote_at=moment)
        reading = measure(quote, history(self.sessions, flat_profile()), now=moment)
        return self.log.advance(reading)

    def test_volume_levels_fire_once_each(self):
        # 10:15 is 16% of an even day, so 260k projects to 1.63m against a 1m
        # average: past 1.5x.
        fired = self.advance(at(10, 15), 260_000)
        self.assertEqual([t.level for t in fired], [1.5])
        # 10:30 is 20%: 300k projects to exactly 1.5m, neither past the level
        # nor far enough below it to re-arm.
        self.assertEqual(self.advance(at(10, 30), 300_000), [])
        # 11:00 is 28%: 900k projects to 3.21m, so the 3x level fires too.
        tripled = self.advance(at(11, 0), 900_000)
        self.assertEqual([t.level for t in tripled], [3.0])

    def test_a_pace_that_cools_off_can_fire_again(self):
        self.assertEqual(len(self.advance(at(10, 15), 260_000)), 1)
        # Volume flat while the day runs on, so the projection decays to 0.34x
        # by 14:00 and the level re-arms.
        self.assertEqual(self.advance(at(14, 0), 260_000), [])
        # A late burst: 1.4m by 14:15 (80% of a day) projects to 1.75m.
        self.assertEqual(len(self.advance(at(14, 15), 1_400_000)), 1)


class Candidates(unittest.TestCase):
    def test_both_directions_are_tracked_so_the_other_side_can_re_arm(self):
        reading = measure(Quote(daily_return_pct=4.0, previous_close=100.0), history())
        directions = {candidate.direction for candidate in candidates(reading)}
        self.assertEqual(directions, {"up", "down"})

    def test_a_missing_reading_produces_no_candidates(self):
        self.assertEqual(candidates(measure(Quote(), history())), [])


class WhichPriceRule(unittest.TestCase):
    """Only the enabled rule may trigger; both are still measured and shown."""

    def setUp(self):
        self.original = triggers.PRICE_RULES

    def tearDown(self):
        triggers.PRICE_RULES = self.original

    def reading(self):
        # Gapped up 4% at the open and then drifted 3.5% lower: the two rules
        # disagree completely, which is exactly when the choice matters.
        return measure(
            Quote(daily_return_pct=0.4, intraday_return_pct=-3.5, previous_close=100.0),
            history(),
        )

    def kinds(self):
        return {candidate.kind for candidate in candidates(self.reading())}

    def test_daily_only_is_the_default(self):
        self.assertEqual(config._price_rules(), {"daily"})

    def test_daily_only_ignores_the_intraday_fade(self):
        triggers.PRICE_RULES = {"daily"}
        self.assertEqual(self.kinds(), {DAILY})

    def test_intraday_only_catches_the_fade_the_daily_rule_misses(self):
        triggers.PRICE_RULES = {"intraday"}
        self.assertEqual(self.kinds(), {INTRADAY})

    def test_both_can_be_enabled_together(self):
        triggers.PRICE_RULES = {"daily", "intraday"}
        self.assertEqual(self.kinds(), {DAILY, INTRADAY})

    def test_the_disabled_reading_is_still_measured(self):
        triggers.PRICE_RULES = {"daily"}
        reading = self.reading()
        self.assertEqual(reading.intraday_return_pct, -3.5)

    def test_an_unreadable_setting_falls_back_to_daily(self):
        for raw in ("", "   ", "nonsense", "weekly,monthly"):
            with self.subTest(raw=raw):
                os.environ["PRICE_RULES"] = raw
                try:
                    self.assertEqual(config._price_rules(), {"daily"})
                finally:
                    del os.environ["PRICE_RULES"]

    def test_the_setting_is_read_case_and_space_insensitively(self):
        os.environ["PRICE_RULES"] = " Daily , INTRADAY "
        try:
            self.assertEqual(config._price_rules(), {"daily", "intraday"})
        finally:
            del os.environ["PRICE_RULES"]


class Formatting(unittest.TestCase):
    def test_volume_is_grouped_the_indian_way(self):
        self.assertEqual(indian_number(15122715), "1,51,22,715")
        self.assertEqual(indian_number(999), "999")

    def test_volume_reads_in_crore_and_lakh(self):
        self.assertEqual(shares(15122715), "1.51 cr")
        self.assertEqual(shares(250000), "2.50 lakh")

    def test_a_price_alert_names_the_level_it_crossed(self):
        quote = Quote(
            symbol="JSWINFRA",
            cmp=348.25,
            open=329.9,
            previous_close=326.65,
            volume=15122715,
            quote_at=at(11, 42),
            intraday_return_pct=5.56,
            daily_return_pct=6.61,
        )
        reading = measure(quote, history())
        trigger = next(t for t in AlertLog(path=Path("nonexistent")).advance(reading)
                       if t.kind == DAILY and t.level == 5.0)
        text = format_alert(trigger, quote, reading, "JSW Infrastructure")
        self.assertIn("JSWINFRA", text)
        self.assertIn("5%", text)
        self.assertIn("+6.61%", text)
        self.assertIn("11:42 IST", text)

    def test_a_paced_volume_alert_explains_the_projection(self):
        original = triggers.VOLUME_LEVELS
        triggers.VOLUME_LEVELS = [1.5]
        try:
            moment = at(10, 15)
            sessions = [session(date(2026, 9, d), 1_000_000) for d in (10, 11, 15, 16, 17)]
            quote = Quote(symbol="TRAVELFOOD", volume=400_000, quote_at=moment)
            reading = measure(quote, history(sessions, flat_profile()), now=moment)
            trigger = next(t for t in AlertLog(path=Path("nonexistent")).advance(reading)
                           if t.kind == VOLUME)
            text = format_alert(trigger, quote, reading, "Travel Food Services")
            self.assertIn("On pace for", text)
            self.assertNotIn("of a normal day has traded", text)
            # The headline has to name the rule that fired, not just the number.
            self.assertIn("volume past 1.5x", text)
        finally:
            triggers.VOLUME_LEVELS = original

    def test_volume_appears_as_context_on_a_price_alert(self):
        """The trigger is off by default, but the figure is still worth seeing."""
        moment = at(10, 15)
        sessions = [session(date(2026, 9, d), 1_000_000) for d in (10, 11, 15, 16, 17)]
        quote = Quote(
            symbol="JSWINFRA", cmp=110.0, previous_close=100.0, open=100.0,
            volume=400_000, quote_at=moment, daily_return_pct=10.0,
        )
        reading = measure(quote, history(sessions, flat_profile()), now=moment)
        fired = AlertLog(path=Path("nonexistent")).advance(reading)
        self.assertNotIn(VOLUME, [t.kind for t in fired])
        text = format_alert(fired[0], quote, reading, "JSW Infrastructure")
        self.assertIn("On pace for", text)
        self.assertIn("2.50x", text)

    def test_a_caution_reaches_the_message(self):
        quote = Quote(symbol="NESTLEIND", cmp=1100.0, previous_close=2200.0,
                      daily_return_pct=-50.0)
        reading = measure(quote, history(splits={FRIDAY: "2:1"}))
        trigger = next(t for t in AlertLog(path=Path("nonexistent")).advance(reading)
                       if t.kind == DAILY)
        text = format_alert(trigger, quote, reading, "Nestle India")
        self.assertIn("ex-split", text)
        self.assertIn("corporate action", text)

    def test_a_non_coverage_alert_names_every_pe_book(self):
        quote = Quote(symbol="CLEANMAX", cmp=1400.0, daily_return_pct=5.4,
                      previous_close=1328.0)
        reading = measure(quote, history())
        trigger = AlertLog(path=Path("nonexistent")).advance(reading)[0]
        text = format_alert(
            trigger, quote, reading, "Clean Max",
            coverage=False, buckets=("Steadview", "Temasek"),
        )
        self.assertIn("non coverage company", text)
        self.assertIn("Steadview", text)
        self.assertIn("Temasek", text)
        covered = format_alert(trigger, quote, reading, "Clean Max")
        self.assertNotIn("non coverage company", covered)

    def test_an_ampersand_in_a_name_cannot_break_the_markup(self):
        quote = Quote(symbol="ADANIPORTS", cmp=1400.0, daily_return_pct=5.4,
                      previous_close=1328.0)
        reading = measure(quote, history())
        trigger = AlertLog(path=Path("nonexistent")).advance(reading)[0]
        self.assertIn("&amp;", format_alert(trigger, quote, reading, "Adani Ports & SEZ"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
