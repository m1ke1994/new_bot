import unittest
from copy import deepcopy

from backend.app.table_tennis.fork_math import zero_zero_candidate_payload
from backend.app.table_tennis.idempotent_sequential_forks_scanner import (
    IdempotentSequentialForksTableTennisScanner,
)
from backend.app.table_tennis.models import TableTennisMatch


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


def make_match(event_id="42"):
    return TableTennisMatch(
        event_id=event_id,
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
        href=f"/event/{event_id}",
        url=f"https://example.test/event/{event_id}",
    )


class FirstLegIdempotenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_event_retry_does_not_reserve_initial_stake_twice(self):
        state = RecordingState()
        scanner = IdempotentSequentialForksTableTennisScanner(
            browser_manager=object(),
            state=state,
        )
        await scanner.configure(budget=10_000, initial_stake=1000)

        first_payload = zero_zero_candidate_payload(make_match("42"))
        await scanner._record_first_leg(first_payload, side="p1", odds=1.87)

        self.assertEqual(scanner._available_balance, 9000.0)
        self.assertEqual(scanner._reserved_balance, 1000.0)
        self.assertEqual(state.values["available_balance"], 9000.0)
        self.assertEqual(state.values["reserved_balance"], 1000.0)

        # Simulate a full observation retry: the strategy creates a fresh payload
        # for the same event, exactly the condition that previously double-reserved.
        retry_payload = zero_zero_candidate_payload(make_match("42"))
        await scanner._record_first_leg(retry_payload, side="p1", odds=1.80)

        self.assertEqual(scanner._available_balance, 9000.0)
        self.assertEqual(scanner._reserved_balance, 1000.0)
        self.assertEqual(state.values["available_balance"], 9000.0)
        self.assertEqual(state.values["reserved_balance"], 1000.0)
        self.assertEqual(retry_payload["first_leg"]["accepted_odd"], 1.87)
        self.assertEqual(retry_payload["first_leg"]["stake"], 1000.0)
        self.assertEqual(retry_payload["monitoring_status"], "WAITING_FOR_ARB")

    async def test_preview_amount_does_not_change_bank_before_second_leg(self):
        state = RecordingState()
        scanner = IdempotentSequentialForksTableTennisScanner(
            browser_manager=object(),
            state=state,
        )
        await scanner.configure(budget=10_000, initial_stake=1000)
        payload = zero_zero_candidate_payload(make_match("99"))

        await scanner._record_first_leg(payload, side="p1", odds=1.87)
        # The mathematical second-leg preview may be around 1000 at the initial
        # 1.87/1.87 line, but it is not a placed bet and must not be reserved.
        payload["required_second_stake"] = 1000.0

        self.assertIsNone(payload["second_leg"])
        self.assertEqual(scanner._reserved_balance, 1000.0)
        self.assertEqual(scanner._available_balance, 9000.0)


if __name__ == "__main__":
    unittest.main()
