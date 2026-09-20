"""Named shareholders from BSE, and when the note gets sent.

Fixtures mirror the real payloads: several tables per response, only one of which
names holders, and named entities mixed in with category subtotals.
"""

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import worker  # noqa: E402
from alerts.shareholding import (  # noqa: E402
    MESSAGE_BUDGET,
    Holder,
    Shareholding,
    _trim,
    format_message,
    is_named_holder,
    promoter_holders,
    public_holders,
    quarter_end,
    share_table,
)
from alerts.triggers import DAILY, INTRADAY, VOLUME, Trigger  # noqa: E402


def row(name, pct, kind="", level="", holders=1, shares=1000):
    return {
        "Fld_ShareHolderName": name,
        "Fld_TotalPercentageOf_A_B_C2": pct,
        "Fld_TotalNoOfShares": shares,
        "Fld_NoOfShareHolders": holders,
        "FLd_ShareholderType": kind,
        "Fld_Level": level,
    }


class PickingTheRightTable(unittest.TestCase):
    def test_it_takes_the_longest_table_that_names_holders(self):
        payload = {
            "Table": [{"Fld_Total": 1}],
            "Table1": [row("A", "1.0"), row("B", "2.0"), row("C", "3.0")],
            "Table2": [{"Fld_Other": 2}],
        }
        self.assertEqual(len(share_table(payload)), 3)

    def test_a_response_without_holder_names_yields_nothing(self):
        self.assertEqual(share_table({"Table": [{"Fld_Total": 1}]}), [])
        self.assertEqual(share_table({}), [])
        self.assertEqual(share_table(None), [])


class TellingHoldersFromSubtotals(unittest.TestCase):
    """The distinction the ipo tool gets from bold text on the rendered page."""

    def test_a_subtotal_counting_many_shareholders_is_rejected(self):
        self.assertFalse(is_named_holder(row("HUF", "0.16", holders=6601), "HUF"))
        self.assertFalse(is_named_holder(row("Employees", "0.62", holders=709), "Employees"))

    def test_a_single_entity_is_kept(self):
        self.assertTrue(
            is_named_holder(row("SBI Contra Fund", "2.37"), "SBI Contra Fund")
        )

    def test_a_generic_label_reporting_one_holder_is_still_rejected(self):
        # Both of these really do come back with a count of 1.
        self.assertFalse(
            is_named_holder(
                row("FOREIGN INSTITUTIONAL INVESTORS", "0.02"),
                "FOREIGN INSTITUTIONAL INVESTORS",
            )
        )
        self.assertFalse(is_named_holder(row("Clearing Members", "0.02"), "Clearing Members"))

    def test_a_category_name_does_not_reject_a_real_holder_containing_it(self):
        # "Insurance Companies" is a category; this is a shareholder.
        name = "Life Insurance Corporation of India"
        self.assertTrue(is_named_holder(row(name, "6.88"), name))

    def test_a_government_promoter_is_a_real_holder_not_a_category(self):
        """Every PSU reports its state holding this way.

        The President of India holds 96.5% of LIC, 58.89% of ONGC and 65% of
        SAIL. Read as a category label these promoters vanished entirely, which
        is what the deny list did until it was scoped to public rows only.
        """
        for name in ("President of India", "PRESIDENT OF INDIA",
                     "THE PRESIDENT OF INDIA",
                     "President of India Ministry of Railways"):
            with self.subTest(name=name):
                self.assertTrue(is_named_holder(row(name, "96.5"), name, promoter=True))
                # Still a category when it turns up on the public side.
                if name.lower() == "president of india":
                    self.assertFalse(is_named_holder(row(name, "96.5"), name))

    def test_a_promoter_subtotal_is_still_rejected_by_its_holder_count(self):
        # Scoping the deny list out must not let aggregate rows through.
        self.assertFalse(
            is_named_holder(row("Promoter Group Total", "4.33", holders=10),
                            "Promoter Group Total", promoter=True)
        )

    def test_holding_accounts_are_rejected_by_pattern(self):
        for name in (
            "Unclaimed or Suspense or Escrow Account",
            "Investor Education and Protection Fund",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_named_holder(row(name, "0.22"), name))


class Promoters(unittest.TestCase):
    PAYLOAD = {
        "Table": [{"Fld_Total": 1}],
        "Table1": [
            row("Sajjan Jindal (Trustee)", "69.52", kind="Promoter"),
            row("JSL Limited", "2.20", kind="Promoter Group"),
            row("Siddeshwari Tradex Private Limited", "2.20", kind="Promoter Group"),
            row("Somebody Else", "5.00", kind="Public"),
            row("Zero Holder", "0", kind="Promoter Group"),
            row("Blank Type", "1.00", kind=""),
        ],
    }

    def setUp(self):
        self.original = worker.shareholding.bse_get_json
        worker.shareholding.bse_get_json = lambda url, **kw: self.PAYLOAD

    def tearDown(self):
        worker.shareholding.bse_get_json = self.original

    def test_only_promoter_and_promoter_group_rows_are_kept(self):
        found = promoter_holders("543994", 130.0)
        self.assertEqual(
            [holder.name for holder in found],
            ["Sajjan Jindal (Trustee)", "JSL Limited", "Siddeshwari Tradex Private Limited"],
        )

    def test_they_come_back_largest_first(self):
        found = promoter_holders("543994", 130.0)
        self.assertEqual([h.pct for h in found], sorted([h.pct for h in found], reverse=True))

    def test_a_holder_at_zero_percent_is_dropped(self):
        names = [holder.name for holder in promoter_holders("543994", 130.0)]
        self.assertNotIn("Zero Holder", names)

    def test_the_percentages_sum_to_the_reported_promoter_stake(self):
        found = Shareholding(symbol="JSWINFRA", promoters=promoter_holders("543994", 130.0))
        # Screener reports 73.93% for the same quarter.
        self.assertAlmostEqual(found.promoter_pct, 73.92, places=2)


class PublicHolders(unittest.TestCase):
    PAYLOAD = {
        "Table1": [
            row("SBI CONTRA FUND", "2.37", level="Mutual Funds/"),
            row("GOVERNMENT OF SINGAPORE", "1.82", level="Foreign Portfolio Investors"),
            row("HUF", "0.16", level="Any Other (specify)", holders=6601),
            row("Clearing Members", "0.02", level="Any Other (specify)"),
        ]
    }

    def setUp(self):
        self.original = worker.shareholding.bse_get_json
        worker.shareholding.bse_get_json = lambda url, **kw: self.PAYLOAD

    def tearDown(self):
        worker.shareholding.bse_get_json = self.original

    def test_subtotals_are_stripped_out(self):
        found = public_holders("543994", 130.0)
        self.assertEqual(
            [holder.name for holder in found], ["SBI CONTRA FUND", "GOVERNMENT OF SINGAPORE"]
        )

    def test_the_category_heading_loses_its_trailing_slash(self):
        found = public_holders("543994", 130.0)
        self.assertEqual(found[0].category, "Mutual Funds")

    def test_long_category_names_shorten_for_the_phone(self):
        found = public_holders("543994", 130.0)
        self.assertEqual(found[1].short_category, "FPI")


class QuarterEnd(unittest.TestCase):
    def test_a_quarter_name_resolves_to_its_last_day(self):
        self.assertEqual(quarter_end("June 2026"), datetime.date(2026, 6, 30))
        self.assertEqual(quarter_end("March 2026"), datetime.date(2026, 3, 31))
        self.assertEqual(quarter_end("December 2025"), datetime.date(2025, 12, 31))

    def test_an_explicit_date_is_read_as_given(self):
        self.assertEqual(quarter_end("30-Jun-2026"), datetime.date(2026, 6, 30))

    def test_february_respects_the_leap_year(self):
        self.assertEqual(quarter_end("February 2028"), datetime.date(2028, 2, 29))
        self.assertEqual(quarter_end("February 2026"), datetime.date(2026, 2, 28))

    def test_nonsense_gives_nothing(self):
        self.assertIsNone(quarter_end(""))
        self.assertIsNone(quarter_end("whenever"))


class Formatting(unittest.TestCase):
    def found(self, promoters=3, public=3):
        return Shareholding(
            symbol="JSWINFRA",
            quarter="June 2026",
            as_of=datetime.date(2026, 6, 30),
            promoters=[
                Holder(f"Promoter Entity {i}", "Promoter Group", round(10 - i, 2))
                for i in range(promoters)
            ],
            public=[
                Holder(f"Fund {i}", "Mutual Funds", round(5 - i * 0.5, 2))
                for i in range(public)
            ],
        )

    def test_it_names_the_holders_and_the_promoter_total(self):
        text = format_message(self.found(), "JSW Infrastructure")
        self.assertIn("JSWINFRA", text)
        self.assertIn("JSW Infrastructure", text)
        self.assertIn("Promoter Entity 0", text)
        self.assertIn("27.00%", text)  # 10 + 9 + 8
        self.assertIn("June 2026", text)
        self.assertIn("30 Jun 2026", text)

    def test_every_holder_is_listed_by_default(self):
        # Infosys reports 24 promoter entities and Policybazaar 22 public
        # holders; both must appear in full rather than being cut to a count.
        text = format_message(self.found(promoters=24, public=22))
        for index in range(24):
            self.assertIn(f"Promoter Entity {index}", text)
        for index in range(22):
            self.assertIn(f"Fund {index}", text)
        self.assertNotIn("more entities", text)
        self.assertNotIn("top ", text)

    def test_a_full_note_stays_well_inside_telegrams_limit(self):
        text = format_message(self.found(promoters=24, public=22))
        self.assertLess(len(text), MESSAGE_BUDGET)

    def test_a_register_too_large_to_fit_is_trimmed_rather_than_rejected(self):
        # Telegram refuses anything past 4,096 characters outright, so a
        # pathological register must come back shortened, not unsendable.
        huge = Shareholding(
            symbol="HUGE",
            quarter="June 2026",
            promoters=[Holder(f"Promoter Vehicle Number {i}", "Promoter Group", 0.5)
                       for i in range(120)],
            public=[Holder(f"Institutional Holder Number {i}", "Mutual Funds", 0.4)
                    for i in range(120)],
        )
        text = format_message(huge)
        self.assertLessEqual(len(text), MESSAGE_BUDGET)
        # The promoter side is preserved in preference to the public side.
        self.assertIn("Promoter Vehicle Number 0", text)

    def test_an_explicit_limit_is_still_honoured(self):
        text = format_message(self.found(promoters=20), promoter_limit=6)
        self.assertIn("(20 entities)", text)
        self.assertIn("and 14 more entities", text)
        self.assertNotIn("Promoter Entity 19", text)

    def test_the_rollup_accounts_for_the_percentage_it_hides(self):
        found = self.found(promoters=4)
        text = format_message(found, promoter_limit=2)
        # Entities 2 and 3 hold 8 and 7.
        self.assertIn("and 2 more entities, 15.00% together", text)

    def test_a_single_hidden_entity_reads_as_one(self):
        text = format_message(self.found(promoters=3), promoter_limit=2)
        self.assertIn("and 1 more entity,", text)

    def test_public_holders_say_how_many_were_left_out(self):
        text = format_message(self.found(public=25), public_limit=6)
        self.assertIn("top 6 of 25", text)

    def test_a_company_with_no_promoter_says_so(self):
        text = format_message(self.found(promoters=0))
        self.assertIn("No promoter holding reported", text)
        self.assertNotIn("Promoters 0.00%", text)

    def test_a_long_holder_name_is_truncated_not_wrapped_raw(self):
        self.assertTrue(_trim("x" * 80).endswith("\u2026"))
        self.assertEqual(_trim("short name"), "short name")
        self.assertEqual(_trim("spaced    out"), "spaced out")

    def test_an_ampersand_in_a_holder_name_cannot_break_the_markup(self):
        found = Shareholding(
            symbol="X",
            promoters=[Holder("Gautambhai Adani & Rajeshbhai Adani", "Promoter Group", 30.85)],
        )
        self.assertIn("&amp;", format_message(found, "Adani Ports & SEZ"))


class WhenItIsSent(unittest.TestCase):
    def move(self, level, kind=DAILY):
        return Trigger(symbol="X", kind=kind, level=level, value=level + 0.1)

    def test_the_five_percent_level_does(self):
        self.assertTrue(worker.wants_shareholding(self.move(5.0)))
        self.assertTrue(worker.wants_shareholding(self.move(5.0, INTRADAY)))

    def test_the_three_percent_level_does_not(self):
        self.assertFalse(worker.wants_shareholding(self.move(3.0)))
        self.assertFalse(worker.wants_shareholding(self.move(4.9)))

    def test_a_volume_trigger_does_not(self):
        self.assertFalse(
            worker.wants_shareholding(Trigger(symbol="X", kind=VOLUME, level=3.0, value=3.4))
        )

    def test_it_can_be_switched_off(self):
        original = worker.SHAREHOLDING_AT_PCT
        worker.SHAREHOLDING_AT_PCT = 0
        try:
            self.assertFalse(worker.wants_shareholding(self.move(9.0)))
        finally:
            worker.SHAREHOLDING_AT_PCT = original


class SentOncePerDay(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_shp_state.json"
        self.path.unlink(missing_ok=True)
        self.original = worker.shareholding.fetch

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        worker.shareholding.fetch = self.original

    def _log(self, day=datetime.date(2026, 9, 18)):
        from alerts.state import AlertLog

        log = AlertLog(path=self.path)
        log.start_day(day)
        return log

    def test_a_second_trigger_on_the_same_name_does_not_refetch(self):
        calls = []
        worker.shareholding.fetch = lambda symbol: calls.append(symbol) or None
        log = self._log()
        watched = type("W", (), {"symbol": "JSWINFRA", "name": "JSW"})()
        worker._send_shareholding(watched, log, dry_run=True)
        worker._send_shareholding(watched, log, dry_run=True)
        self.assertEqual(calls, ["JSWINFRA"])

    def test_a_failing_lookup_does_not_take_the_alert_down_with_it(self):
        def boom(symbol):
            raise RuntimeError("BSE unavailable")

        worker.shareholding.fetch = boom
        log = self._log()
        watched = type("W", (), {"symbol": "JSWINFRA", "name": "JSW"})()
        worker._send_shareholding(watched, log, dry_run=True)  # must not raise

    def test_a_new_session_allows_it_again(self):
        from alerts.state import AlertLog

        log = self._log()
        log.shared.append("JSWINFRA")
        log.save()
        reopened = AlertLog(path=self.path)
        self.assertEqual(reopened.shared, ["JSWINFRA"])
        reopened.start_day(datetime.date(2026, 9, 21))
        self.assertEqual(reopened.shared, [])

    def _send(self, symbol, log):
        watched = type("W", (), {"symbol": symbol, "name": symbol.title()})()
        worker._send_shareholding(watched, log, dry_run=True)

    def test_every_name_that_crosses_gets_one_by_default(self):
        # Friday 18 Sep had fifteen names past 5%; all fifteen must be covered.
        calls = []
        worker.shareholding.fetch = lambda symbol: calls.append(symbol) or None
        log = self._log()
        for index in range(15):
            self._send(f"SYM{index}", log)
        self.assertEqual(len(calls), 15)

    def test_zero_means_no_ceiling_rather_than_none_at_all(self):
        # len(shared) >= 0 is always true, so an unguarded check would suppress
        # every note instead of allowing every note.
        original = worker.SHAREHOLDING_MAX_PER_DAY
        worker.SHAREHOLDING_MAX_PER_DAY = 0
        calls = []
        worker.shareholding.fetch = lambda symbol: calls.append(symbol) or None
        try:
            log = self._log()
            for index in range(5):
                self._send(f"SYM{index}", log)
            self.assertEqual(len(calls), 5)
        finally:
            worker.SHAREHOLDING_MAX_PER_DAY = original

    def test_a_ceiling_is_honoured_when_one_is_set(self):
        original = worker.SHAREHOLDING_MAX_PER_DAY
        worker.SHAREHOLDING_MAX_PER_DAY = 3
        calls = []
        worker.shareholding.fetch = lambda symbol: calls.append(symbol) or None
        try:
            log = self._log()
            for index in range(10):
                self._send(f"SYM{index}", log)
            self.assertEqual(len(calls), 3)
        finally:
            worker.SHAREHOLDING_MAX_PER_DAY = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
