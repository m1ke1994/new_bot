import unittest
from copy import deepcopy

from backend.app.table_tennis.fork_math import zero_zero_candidate_payload
from backend.app.table_tennis.models import TableTennisMatch
from backend.app.table_tennis.sequential_forks_scanner import (
    ArbitrageLocked,
    SequentialForksTableTennisScanner,
    select_favorite_immediately,
)


class RecordingState:
    def __init__(self):
        self.values = {}
        self.candidates = []

    async def update(self, **changes):
        self.values.update(deepcopy(changes))
        return deepcopy(self.values)

    async def snapshot(self):
        return deepcopy(self.values)

    async def update_candidate(self, candidate):
        self.candidates.append(deepcopy(candidate))

    async def set_active_match(self, candidate):
        self.values["active_match"] = deepcopy(candidate)


def make_match():
    return TableTennisMatch(
        event_id="42",
        league_id="league",
        league_name="League",
        player_1="Player 1",
        player_2="Player 2",
        score="0:0",
        sets_score="0:0",
        current_set=1,
        points_player_1=0,
        points_player_2=0,
        status="LIVE",
        started=True,
        time=None,
        href="/event/42",
        url="https://example.test/event/42",
    )


class SequentialForksTests(unittest.IsolatedAsyncioTestCase):
    def test_first_leg_is_immediate_and_stable_on_equal_odds(self):
        self.assertEqual(select_favorite_immediately(1.34, 4.11), "p1")
        self.assertEqual(select_favorite_immediately(3.75, 1.29), "p2")
        self.assertEqual(select_favorite_immediately(1.87, 1.87), "p1")

    async def test_locked_arb_releases_budget_and_signals_next_match(self):
        state = RecordingState()
        scanner = SequentialForksTableTennisScanner(browser_manager=object(), state=state)
        await scanner.configure(budget=10_000, initial_stake=1000)
        payload = zero_zero_candidate_payload(make_match())

        await scanner._record_first_leg(payload, side="p1", odds=1.50)
        self.assertEqual(state.values["available_balance"], 9000.0)
        self.assertEqual(state.values["reserved_balance"], 1000.0)

        with self.assertRaises(ArbitrageLocked):
            await scanner._complete_second_leg(payload, odds=3.10)

        self.assertIsNotNone(payload["second_leg"])
        self.assertEqual(payload["monitoring_status"], "ARB_LOCKED")
        self.assertEqual(state.values["reserved_balance"], 0.0)
        self.assertEqual(state.values["current_balance"], 10016.13)
        self.assertEqual(state.values["available_balance"], 10016.13)

        history = await scanner.forks()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "ARB_LOCKED")
        self.assertIsNone(history[0]["winner"])
        self.assertEqual(history[0]["profit_loss"], 16.13)

        # A retry cannot create a second hedge or duplicate the history row.
        second_leg = deepcopy(payload["second_leg"])
        self.assertTrue(await scanner._complete_second_leg(payload, odds=4.0))
        self.assertEqual(payload["second_leg"], second_leg)
        self.assertEqual(len(await scanner.forks()), 1)


if __name__ == "__main__":
    unittest.main()
