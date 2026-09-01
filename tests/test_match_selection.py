import unittest

from matches import is_upcoming_match, sort_upcoming_matches


class MatchSelectionTests(unittest.TestCase):
    def test_period_rule(self):
        self.assertTrue(is_upcoming_match(period="", finished=False, time_seconds=42))
        self.assertFalse(
            is_upcoming_match(period="1-й тайм", finished=False, time_seconds=42)
        )
        self.assertFalse(
            is_upcoming_match(period="2-й тайм", finished=False, time_seconds=42)
        )

    def test_nearest_upcoming_is_first(self):
        matches = [
            {"id": "late", "is_upcoming": True, "time_seconds": 120},
            {"id": "live", "is_upcoming": False, "time_seconds": 10},
            {"id": "nearest", "is_upcoming": True, "time_seconds": 25},
        ]
        result = sort_upcoming_matches(matches)
        self.assertEqual([item["id"] for item in result], ["nearest", "late"])


if __name__ == "__main__":
    unittest.main()
