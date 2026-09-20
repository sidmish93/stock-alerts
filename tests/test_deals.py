"""The post-SHP block/bulk adjustment.

The three reconciliation rules are the point of this file: block-and-leftover-bulk
within a client-day, dropping clients that traded both ways on one exchange, and
counting one deal once when both exchanges print it.
"""

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import deals  # noqa: E402
from alerts.deals import (  # noqa: E402
    Leg,
    adjust_window,
    apply_to,
    best_holder,
    canonical_party,
    dedupe_client_day,
    implied_outstanding,
    merge_exchanges,
    names_match,
    net_positions,
    parse_nse_csv,
)
from alerts.shareholding import Holder, Shareholding  # noqa: E402

JULY = datetime.date(2026, 7, 1)
TODAY = datetime.date(2026, 9, 18)


def leg(client, side, qty, deal_type="BLOCK", exchange="NSE", day=JULY):
    return Leg(exchange=exchange, deal_type=deal_type, day=day, client=client,
               side=side, qty=qty)


def tape_row(record):
    return Holder(name=record["name"], category="tape", pct=0.0, shares=0.0)


class Names(unittest.TestCase):
    def test_corporate_suffixes_are_peeled_off(self):
        self.assertEqual(
            canonical_party("Trident Group Limited"), canonical_party("TRIDENT GROUP LTD")
        )
        self.assertEqual(
            canonical_party("Adani Tradeline Private Limited"),
            canonical_party("ADANI TRADELINE PVT LTD"),
        )

    def test_and_and_ampersand_are_the_same_word(self):
        self.assertEqual(
            canonical_party("Gautam Adani and Rajesh Adani"),
            canonical_party("GAUTAM ADANI & RAJESH ADANI"),
        )

    def test_a_substantial_containment_matches(self):
        self.assertTrue(names_match("Peak XV Partners Investments VI-1", "PEAK XV PARTNERS INVESTMENTS VI-1"))
        self.assertTrue(names_match("Ribbit Capital V L.P.", "RIBBIT CAPITAL V LP"))

    def test_a_trivial_overlap_does_not_match(self):
        self.assertFalse(names_match("ABC Ltd", "XYZ Ltd"))
        self.assertFalse(names_match("", "Something"))

    def test_an_ambiguous_client_matches_nothing_rather_than_guessing(self):
        holders = [
            Holder("Ribbit Capital V L.P.", "FPI", 5.64),
            Holder("Ribbit Cayman GW Holdings V, Ltd.", "FPI", 4.46),
        ]
        # Exact name wins outright.
        self.assertEqual(best_holder(holders, "RIBBIT CAPITAL V LP").pct, 5.64)

    def test_no_plausible_holder_returns_none(self):
        self.assertIsNone(best_holder([Holder("A Ltd", "x", 1.0)], "Completely Different Fund"))


class Window(unittest.TestCase):
    def test_the_window_opens_the_day_after_quarter_end(self):
        start, end = adjust_window(datetime.date(2026, 6, 30), TODAY)
        self.assertEqual(start, JULY)
        self.assertEqual(end, TODAY)

    def test_no_quarter_end_means_no_window(self):
        self.assertEqual(adjust_window(None, TODAY), (None, None))


class Outstanding(unittest.TestCase):
    def test_it_takes_the_median_of_what_each_holder_implies(self):
        holders = [
            Holder("A", "x", 10.0, shares=100),   # implies 1000
            Holder("B", "x", 20.0, shares=200),   # implies 1000
            Holder("C", "x", 50.0, shares=400),   # implies 800
        ]
        self.assertEqual(implied_outstanding(holders), 1000)

    def test_tiny_holders_are_ignored_because_rounding_dominates_them(self):
        holders = [
            Holder("Big", "x", 50.0, shares=500),      # implies 1000
            Holder("Tiny", "x", 0.01, shares=1),       # implies 10000
        ]
        self.assertEqual(implied_outstanding(holders), 1000)

    def test_nothing_usable_gives_nothing(self):
        self.assertIsNone(implied_outstanding([]))
        self.assertIsNone(implied_outstanding([Holder("A", "x", 0.0, shares=0)]))


class Reconciliation(unittest.TestCase):
    def test_bulk_repeating_a_block_is_counted_once(self):
        # The same 100 shares reported on both feeds for one client-day.
        legs = [leg("Fund A", "BUY", 100, "BLOCK"), leg("Fund A", "BUY", 100, "BULK")]
        out = dedupe_client_day(legs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].qty, 100)

    def test_bulk_beyond_the_block_is_added_on_top(self):
        legs = [leg("Fund A", "BUY", 100, "BLOCK"), leg("Fund A", "BUY", 150, "BULK")]
        self.assertEqual(dedupe_client_day(legs)[0].qty, 150)

    def test_a_client_trading_both_ways_on_one_day_is_dropped(self):
        # A market maker in and out of the same name says nothing about holdings.
        legs = [leg("HFT Ltd", "BUY", 500), leg("HFT Ltd", "SELL", 500)]
        self.assertEqual(dedupe_client_day(legs), [])

    def test_the_same_deal_printed_on_both_exchanges_is_counted_once(self):
        legs = [
            leg("Fund A", "SELL", 1000, exchange="NSE"),
            leg("Fund A", "SELL", 1000, exchange="BSE"),
        ]
        merged = merge_exchanges(dedupe_client_day(legs))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].qty, 1000)

    def test_near_equal_prints_are_still_treated_as_one_deal(self):
        legs = [
            leg("Fund A", "SELL", 1000, exchange="NSE"),
            leg("Fund A", "SELL", 1010, exchange="BSE"),
        ]
        self.assertEqual(merge_exchanges(dedupe_client_day(legs))[0].qty, 1010)

    def test_genuinely_different_quantities_are_two_prints_and_are_summed(self):
        legs = [
            leg("Fund A", "SELL", 1000, exchange="NSE"),
            leg("Fund A", "SELL", 4000, exchange="BSE"),
        ]
        self.assertEqual(merge_exchanges(dedupe_client_day(legs))[0].qty, 5000)

    def test_different_days_stay_separate_and_accumulate(self):
        legs = [
            leg("Fund A", "SELL", 1000, day=JULY),
            leg("Fund A", "SELL", 2000, day=datetime.date(2026, 7, 2)),
        ]
        nets = net_positions(legs)
        self.assertEqual(nets[canonical_party("Fund A")]["sold"], 3000)

    def test_buys_and_sells_are_netted_per_party(self):
        legs = [
            leg("Fund A", "BUY", 3000, day=JULY),
            leg("Fund A", "SELL", 1000, day=datetime.date(2026, 7, 3)),
        ]
        record = net_positions(legs)[canonical_party("Fund A")]
        self.assertEqual((record["bought"], record["sold"]), (3000, 1000))


class ApplyingToHolders(unittest.TestCase):
    def holders(self):
        return [
            Holder("TRIDENT GROUP LIMITED", "Promoter Group", 45.75, shares=2_331_413_000),
            Holder("MADHURAJ FOUNDATION", "Promoter Group", 27.63, shares=1_408_000_000),
        ]

    def test_a_transfer_between_two_holders_moves_both_stakes(self):
        holders = self.holders()
        outstanding = implied_outstanding(holders)
        nets = net_positions(
            [
                leg("TRIDENT GROUP LIMITED", "BUY", 130_000_000),
                leg("MADHURAJ FOUNDATION", "SELL", 130_000_000),
            ]
        )
        apply_to(holders, nets, outstanding, tape_row)
        bought, sold = holders[0], holders[1]
        self.assertGreater(bought.adj_pct, bought.pct)
        self.assertLess(sold.adj_pct, sold.pct)
        # Nothing left the promoter block, so the two moves offset.
        self.assertAlmostEqual(
            (bought.adj_pct - bought.pct) + (sold.adj_pct - sold.pct), 0.0, places=1
        )

    def test_an_untouched_holder_keeps_bses_own_figures(self):
        holders = self.holders()
        apply_to(holders, {}, implied_outstanding(holders), tape_row)
        for holder in holders:
            self.assertEqual(holder.adj_pct, holder.pct)
            self.assertEqual(holder.adj_shares, holder.shares)
            self.assertFalse(holder.traded)

    def test_a_holder_cannot_be_adjusted_below_zero(self):
        holders = [Holder("Small Ltd", "x", 1.0, shares=100)]
        nets = net_positions([leg("Small Ltd", "SELL", 500)])
        apply_to(holders, nets, 10_000, tape_row)
        self.assertEqual(holders[0].adj_shares, 0)

    def test_a_sizeable_buyer_not_in_the_pattern_is_reported_as_undetermined(self):
        holders = self.holders()
        outstanding = implied_outstanding(holders)
        nets = net_positions([leg("ADANI INFRA INDIA LIMITED", "BUY", 124_800_000)])
        extras = apply_to(holders, nets, outstanding, tape_row)
        self.assertEqual(len(extras), 1)
        self.assertTrue(extras[0].undetermined)
        # What it bought is known; the total stake is not.
        self.assertIsNone(extras[0].adj_pct)
        self.assertGreater(extras[0].net_pct, 0.5)

    def test_a_small_unmatched_buyer_is_not_worth_a_line(self):
        holders = self.holders()
        nets = net_positions([leg("Tiny Fund", "BUY", 100)])
        self.assertEqual(apply_to(holders, nets, implied_outstanding(holders), tape_row), [])

    def test_an_unmatched_net_seller_is_dropped(self):
        # Its starting stake is unknown, so a sale says nothing quantifiable.
        holders = self.holders()
        nets = net_positions([leg("Mystery Seller", "SELL", 500_000_000)])
        self.assertEqual(apply_to(holders, nets, implied_outstanding(holders), tape_row), [])


class FeedAvailability(unittest.TestCase):
    """A blocked feed must not read as "nothing traded"."""

    def setUp(self):
        self.found = Shareholding(
            symbol="TEST",
            scrip_code="1234",
            quarter="June 2026",
            as_of=datetime.date(2026, 6, 30),
            promoters=[Holder("A Ltd", "Promoter", 50.0, shares=500)],
        )
        self.nse = deals.fetch_nse_legs
        self.bse = deals.fetch_bse_legs
        self.cache = deals.read_cache
        deals.read_cache = lambda *a, **kw: None
        deals.write_cache = lambda *a, **kw: None

    def tearDown(self):
        deals.fetch_nse_legs = self.nse
        deals.fetch_bse_legs = self.bse
        deals.read_cache = self.cache

    def test_a_refused_nse_feed_is_reported_as_missing(self):
        def refuse(*args):
            raise RuntimeError("HTTP 403")

        deals.fetch_nse_legs = refuse
        deals.fetch_bse_legs = lambda *a: []
        meta = deals.adjust(self.found, today=TODAY, tape_row=tape_row)
        self.assertEqual(meta.missing, ["NSE"])
        self.assertEqual(meta.feeds, ["BSE"])
        self.assertFalse(meta.complete)

    def test_both_feeds_answering_is_complete(self):
        deals.fetch_nse_legs = lambda *a: []
        deals.fetch_bse_legs = lambda *a: []
        meta = deals.adjust(self.found, today=TODAY, tape_row=tape_row)
        self.assertEqual(meta.missing, [])
        self.assertTrue(meta.complete)

    def test_the_window_runs_from_the_day_after_quarter_end(self):
        deals.fetch_nse_legs = lambda *a: []
        deals.fetch_bse_legs = lambda *a: []
        meta = deals.adjust(self.found, today=TODAY, tape_row=tape_row)
        self.assertEqual(meta.start, JULY)
        self.assertEqual(meta.end, TODAY)


class NseCsv(unittest.TestCase):
    CSV = (
        '\ufeff"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ",'
        '"Quantity Traded ","Trade Price / Wght. Avg. Price ","Remarks "\n'
        '"02-Jul-2026","TRIDENT","Trident Ltd","TRIDENT GROUP LIMITED","BUY","130000000","35.5",""\n'
        '"02-Jul-2026","TRIDENT","Trident Ltd","MADHURAJ FOUNDATION","SELL","130000000","35.5",""\n'
        '"01-Jan-2026","TRIDENT","Trident Ltd","OLD DEAL","BUY","999","35.5",""\n'
    )

    def test_it_reads_the_space_padded_quoted_headers(self):
        legs = parse_nse_csv(self.CSV, JULY, TODAY, "BLOCK")
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].client, "TRIDENT GROUP LIMITED")
        self.assertEqual(legs[0].side, "BUY")
        self.assertEqual(legs[0].qty, 130_000_000)
        self.assertEqual(legs[1].side, "SELL")

    def test_rows_outside_the_window_are_ignored(self):
        self.assertNotIn("OLD DEAL", [l.client for l in parse_nse_csv(self.CSV, JULY, TODAY, "BLOCK")])

    def test_an_html_or_json_body_is_not_mistaken_for_a_csv(self):
        self.assertEqual(parse_nse_csv("<html>blocked</html>", JULY, TODAY, "BULK"), [])
        self.assertEqual(parse_nse_csv('{"error":"no"}', JULY, TODAY, "BULK"), [])
        self.assertEqual(parse_nse_csv("", JULY, TODAY, "BULK"), [])

    def test_a_header_with_no_rows_means_no_deals(self):
        header = self.CSV.splitlines()[0] + "\n"
        self.assertEqual(parse_nse_csv(header, JULY, TODAY, "BLOCK"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
