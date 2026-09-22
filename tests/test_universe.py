"""Universe file parsing, including non-coverage PE tags."""

import tempfile
import unittest
from pathlib import Path

from alerts.universe import load


class UniverseTests(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "universe.txt"
            path.write_text(text, encoding="utf-8")
            return load(path)

    def test_a_plain_line_is_coverage(self):
        watched = self._load("RELIANCE, Reliance Industries\n")
        self.assertEqual(len(watched), 1)
        self.assertEqual(watched[0].symbol, "RELIANCE")
        self.assertEqual(watched[0].name, "Reliance Industries")
        self.assertTrue(watched[0].coverage)
        self.assertEqual(watched[0].buckets, ())
        self.assertEqual(watched[0].coverage_label(), "")

    def test_a_pe_line_carries_every_book(self):
        watched = self._load(
            "CLEANMAX, Clean Max | noncoverage | Steadview; Temasek\n"
            "MEESHO, Meesho | noncoverage | Steadview\n"
        )
        self.assertEqual(len(watched), 2)
        first = watched[0]
        self.assertEqual(first.symbol, "CLEANMAX")
        self.assertEqual(first.name, "Clean Max")
        self.assertFalse(first.coverage)
        self.assertEqual(first.buckets, ("Steadview", "Temasek"))
        self.assertEqual(
            first.coverage_label(),
            "non coverage company · Steadview, Temasek",
        )
        self.assertEqual(watched[1].buckets, ("Steadview",))

    def test_the_real_file_has_the_pe_overlay(self):
        watched = load()
        by_symbol = {item.symbol: item for item in watched}
        self.assertGreaterEqual(len(watched), 400)
        self.assertTrue(by_symbol["RELIANCE"].coverage)
        self.assertFalse(by_symbol["CLEANMAX"].coverage)
        self.assertEqual(by_symbol["CLEANMAX"].buckets, ("Steadview", "Temasek"))
        self.assertEqual(by_symbol["OLAELEC"].buckets, ("Alpha Wave", "Temasek"))
        self.assertEqual(by_symbol["HCG"].buckets, ("KKR", "Temasek"))
        self.assertEqual(by_symbol["PINELABS"].buckets, ("Alpha Wave", "Temasek"))
        self.assertEqual(by_symbol["PAYTM"].buckets, ("Elevation",))
        self.assertEqual(by_symbol["IPCALAB"].buckets, ("ChrysCapital", "Elevation"))
        self.assertEqual(sum(1 for item in watched if not item.coverage), 101)


if __name__ == "__main__":
    unittest.main(verbosity=2)
