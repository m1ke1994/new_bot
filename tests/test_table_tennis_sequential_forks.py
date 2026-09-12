import unittest
from copy import deepcopy

from backend.app.table_tennis.fork_math import zero_zero_candidate_payload
from backend.app.table_tennis.models import TableTennisMatch
from backend.app.table_tennis.sequential_forks_scanner import (
    ArbitrageLocked,
    SequentialForksTableTennisScanner,
    event_url_matches,
    select_favorite_immediately,
    select_zero_zero_in_scan_order,
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


def make_match(event_id="42", *, score="0:0", time=None):
    return TableTennisMatch(
        event_id=event_id,
        league_id="league",
        league_name="League",
        player_1=f"Player 1-{event_id}",
        player_2=f"Player 2-{event_id}",
        score=score,
        sets_score="0:0",
        current_set=1,
        points_player_1=0,
        points_player_2=0,
        status="LIVE",
        started=True,
        time=time,
        href=f"/event/{event_id}",
        url=f"https://example.test/event/{event_id}",
    )


class SequentialForksTests(unittest.IsolatedAsyncioTestCase):
    def test_first_leg_is_immediate_and_stable_on_equal_odds(self):
        self.assertEqual(select_favorite_immediately(1.34, 4.11), "p1")
        self.assertEqual(select_favorite_immediately(3.75, 1.29), "p2")
        self.assertEqual(select_favorite_immediately(1.87, 1.87), "p1")

    def test_same_event_accepts_party_subroute(self):
        event = "752166402"
        self.assertTrue(event_url_matches(
            "https://1xbet-start.cc/ru/live/table-tennis/3085062-league/"
            "752166402-player-a-player-b",
            event,
        ))
        self.assertTrue(event_url_matches(
            "https://1xbet-start.cc/ru/live/table-tennis/3085062-league/"
            "752166402-player-a-player-b/2-partiya?foo=1",
            event,
        ))
        self.assertFalse(event_url_matches(
            "https://1xbet-start.cc/ru/live/table-tennis/3085062-league/752166999-other",
            event,
        ))

    def test_zero_zero_queue_keeps_scanner_dom_order(self):
        # Time values are intentionally out of chronological order: selection must
        # still be first league/card order, not a re-sort by clock.
        first = make_match("100", time="23:59")
        second = make_match("200", time="00:01")
        not_zero = make_match("300", score="1:0", time="00:00")
        selected = select_zero_zero_in_scan_order([first, second, not_zero])
        self.assertEqual([item.event_id for item in selected], ["100", "200"])

    async def test_locked_arb_releases_budget_and_signals_next_match(self):
        state = RecordingState()
        scanner = SequentialForksTableTennisScanner(browser_manager=object(), state=state)
        await scanner.configure(budget=10_000, initial_stake=1000)
        payload = zero_zero_candidate_payload(make_match())

        await scanner._record_first_leg(payload, side="p1", odds=1.50)
        self.assertEqual(state.values["available_balance"], 9000.0)
        self.assertEqual(state.values["reserved_balance"], 1000.0)

        # Exact mathematical zero boundary for 1.50 is 3.00 and must be accepted.
        with self.assertRaises(ArbitrageLocked):
            await scanner._complete_second_leg(payload, odds=3.00)

        self.assertIsNotNone(payload["second_leg"])
        self.assertEqual(payload["second_leg"]["accepted_odd"], 3.0)
        self.assertEqual(payload["monitoring_status"], "ARB_LOCKED")
        self.assertEqual(state.values["reserved_balance"], 0.0)
        self.assertEqual(state.values["current_balance"], 10000.0)
        self.assertEqual(state.values["available_balance"], 10000.0)

        history = await scanner.forks()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "ARB_LOCKED")
        self.assertEqual(history[0]["profit_loss"], 0.0)

        # A retry cannot create a second hedge or duplicate the history row.
        second_leg = deepcopy(payload["second_leg"])
        self.assertTrue(await scanner._complete_second_leg(payload, odds=4.0))
        self.assertEqual(payload["second_leg"], second_leg)
        self.assertEqual(len(await scanner.forks()), 1)


if __name__ == "__main__":
    unittest.main()
