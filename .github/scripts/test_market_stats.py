"""Check ranking semantics and preserve cards when market data is invalid."""

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from update_market_stats import REPOSITORY, update_cards


class MarketStatsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.market = {
            "$meta": {"updated_at": "2026-10-25T08:30:00+08:00"},
            "target": {"repo": REPOSITORY, "download_count": 1000, "stars": 50},
            "above": {"repo": "other/a", "download_count": 2000, "stars": 100},
            "tie": {"repo": "other/b", "download_count": 1000, "stars": 50},
            "below": {"repo": "other/c", "download_count": 0, "stars": 75},
        }

    def test_ties_and_independent_rankings(self):
        stats = update_cards(self.market, self.output)
        self.assertEqual(stats["download_rank"], 2)
        self.assertEqual(stats["star_rank"], 3)
        self.assertEqual(stats["download_count"], 1000)
        self.assertEqual(stats["market_entries"], 4)
        self.assertEqual(json.loads((self.output / "snapshot.json").read_text()), stats)
        for name in ("downloads", "download-rank", "star-rank"):
            svg = ET.parse(self.output / f"{name}.svg")
            title = svg.find("{http://www.w3.org/2000/svg}title").text
            self.assertIn("2026-10-25 00:30 UTC", title)
        self.assertIn(
            "1,000", (self.output / "downloads.svg").read_text(encoding="utf-8")
        )

    def test_invalid_metric_preserves_previous_cards(self):
        update_cards(self.market, self.output)
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        for invalid in (None, -1, "1000", True):
            with self.subTest(invalid=invalid):
                self.market["above"]["download_count"] = invalid
                with self.assertRaises(ValueError):
                    update_cards(self.market, self.output)
                after = {path.name: path.read_bytes() for path in self.output.iterdir()}
                self.assertEqual(before, after)

    def test_missing_or_duplicate_target_does_not_create_cards(self):
        target = self.market.pop("target")
        with self.assertRaises(ValueError):
            update_cards(self.market, self.output)
        self.market.update({"target": target, "duplicate": target})
        with self.assertRaises(ValueError):
            update_cards(self.market, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_timestamp_does_not_create_cards(self):
        self.market["$meta"] = {}
        with self.assertRaises(ValueError):
            update_cards(self.market, self.output)
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
