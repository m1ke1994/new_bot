import unittest

from backend.app.table_tennis.stable_forks_scanner import is_party_two_1x2_title


class StablePartyTwoMarketTests(unittest.TestCase):
    def test_short_title_used_inside_selected_party_two_is_supported(self):
        self.assertTrue(is_party_two_1x2_title("1X2"))
        self.assertTrue(is_party_two_1x2_title("1Х2"))

    def test_explicit_party_two_title_is_supported(self):
        self.assertTrue(is_party_two_1x2_title("1X2. 2-я Партия"))

    def test_other_markets_and_other_parties_are_rejected(self):
        self.assertFalse(is_party_two_1x2_title("Тотал. 2-я Партия"))
        self.assertFalse(is_party_two_1x2_title("1X2. 1-я Партия"))


if __name__ == "__main__":
    unittest.main()
