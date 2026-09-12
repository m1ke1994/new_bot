import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from backend.app.table_tennis.fork_math import zero_zero_candidate_payload
from backend.app.table_tennis.forks_scanner import (
    ForksTableTennisScanner,
    infer_second_set_winner,
)
from backend.app.table_tennis.models import TableTennisMatch
from backend.app.table_tennis.monitoring import TargetMarketOdds


class RecordingState:
    def __init__(self):
        self.values = {}
        self.active_updates = []
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
        if candidate is not None:
            self.active_updates.append(deepcopy(candidate))


class FakePage:
    url = "https://example.test/event/42"

    async def goto(self, *_args, **_kwargs):
        return None


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


class PartyTwoStrategyTests(unittest.IsolatedAsyncioTestCase):
    def test_winner_inference_supports_both_scoreboard_layouts(self):
        self.assertEqual(infer_second_set_winner((1, 0), (1, 0), (1, 1)), "p2")
        self.assertEqual(infer_second_set_winner((0, 0), (11, 8), (0, 0)), "p1")
        self.assertIsNone(infer_second_set_winner((1, 0), (1, 0), None))

    async def test_party_two_favorite_lock_recovery_and_single_hedge(self):
        state = RecordingState()
        scanner = ForksTableTennisScanner(browser_manager=object(), state=state)
        await scanner.configure(budget=1000, initial_stake=100)
        markets = iter([
            TargetMarketOdds("1X2. 2-я Партия", 3.75, 1.29, "MARKET_AVAILABLE"),
            TargetMarketOdds("1X2. 2-я Партия", None, None, "MARKET_LOCKED"),
            TargetMarketOdds("1X2. 2-я Партия", 3.00, 1.10, "MARKET_AVAILABLE"),
            TargetMarketOdds("1X2. 2-я Партия", 5.00, 1.10, "MARKET_AVAILABLE"),
        ])

        async def next_market(*_args, **_kwargs):
            return next(markets)

        with (
            patch("backend.app.table_tennis.forks_scanner.ensure_same_event"),
            patch(
                "backend.app.table_tennis.forks_scanner.open_target_party",
                new=AsyncMock(),
            ) as open_party,
            patch(
                "backend.app.table_tennis.forks_scanner.read_selected_party",
                new=AsyncMock(return_value=2),
            ),
            patch(
                "backend.app.table_tennis.forks_scanner.read_target_market_odds",
                new=next_market,
            ),
            patch.object(scanner, "_sleep_or_stop", new=AsyncMock()),
            patch.object(
                scanner,
                "_read_second_set_winner",
                new=AsyncMock(side_effect=[None, None, None, None, "p1"]),
            ),
        ):
            outcome = await scanner._observe_zero_zero(FakePage(), make_match())

        self.assertEqual(outcome, "FINISHED")
        open_party.assert_awaited_with(
            unittest.mock.ANY,
            "42",
            2,
            scanner._stop_event,
            timeout=2.0,
        )
        final = state.values["active_match"]
        self.assertEqual(state.active_updates[0]["monitoring_status"], "OPENING_MATCH")
        self.assertEqual(state.active_updates[0]["player1"], "Player 1")
        self.assertEqual(state.active_updates[0]["player2"], "Player 2")
        self.assertEqual(final["party"], 2)
        self.assertEqual(final["first_bet_player"], "p2")
        self.assertEqual(final["opposite_player"], "p1")
        self.assertEqual(final["first_leg"]["accepted_odd"], 1.29)
        self.assertEqual(final["second_leg"]["side"], "p1")
        self.assertEqual(final["monitoring_status"], "FINISHED")
        self.assertEqual(final["first_leg"]["sequence_id"], final["sequence_id"])
        self.assertEqual(final["second_leg"]["sequence_id"], final["sequence_id"])
        self.assertEqual(len(await scanner.forks()), 1)

        second_placed = next(
            item for item in state.active_updates
            if item["monitoring_status"] == "SECOND_BET_PLACED"
        )
        self.assertEqual(second_placed["available_balance"], 874.2)
        self.assertEqual(second_placed["reserved_balance"], 125.8)

        statuses = [item["monitoring_status"] for item in state.active_updates]
        expected = [
            "FIRST_BET_PLACED",
            "WAITING_FOR_ARB",
            "MARKET_LOCKED",
            "WAITING_FOR_ARB",
            "ARB_FOUND",
            "SECOND_BET_PLACED",
            "ARB_LOCKED",
            "WAITING_RESULT",
            "FINISHED",
        ]
        position = -1
        for value in expected:
            position = statuses.index(value, position + 1)

        second_leg = final["second_leg"]
        completed_again = await scanner._complete_second_leg(final, odds=6.0)
        self.assertTrue(completed_again)
        self.assertEqual(final["second_leg"], second_leg)
        self.assertEqual(len(await scanner.forks()), 1)
        self.assertEqual(state.values["reserved_balance"], 0.0)
        self.assertEqual(state.values["current_balance"], 1003.2)

    async def test_equal_odds_wait_then_first_leg_can_finish_without_hedge(self):
        state = RecordingState()
        scanner = ForksTableTennisScanner(browser_manager=object(), state=state)
        await scanner.configure(budget=10_000, initial_stake=1000)
        markets = iter([
            TargetMarketOdds("1X2. 2-я Партия", 1.87, 1.87, "MARKET_AVAILABLE"),
            TargetMarketOdds("1X2. 2-я Партия", 1.65, 2.15, "MARKET_AVAILABLE"),
        ])

        async def next_market(*_args, **_kwargs):
            return next(markets)

        with (
            patch("backend.app.table_tennis.forks_scanner.ensure_same_event"),
            patch(
                "backend.app.table_tennis.forks_scanner.open_target_party",
                new=AsyncMock(),
            ),
            patch(
                "backend.app.table_tennis.forks_scanner.read_selected_party",
                new=AsyncMock(return_value=2),
            ),
            patch(
                "backend.app.table_tennis.forks_scanner.read_target_market_odds",
                new=next_market,
            ),
            patch.object(scanner, "_sleep_or_stop", new=AsyncMock()),
            patch.object(
                scanner,
                "_read_second_set_winner",
                new=AsyncMock(side_effect=[None, None, "p1"]),
            ),
        ):
            outcome = await scanner._observe_zero_zero(FakePage(), make_match())

        self.assertEqual(outcome, "HEDGE_NOT_FOUND")
        statuses = [item["monitoring_status"] for item in state.active_updates]
        self.assertIn("WAITING_ODDS_DIVERGENCE", statuses)
        equal_state = next(
            item for item in state.active_updates
            if item["monitoring_status"] == "WAITING_ODDS_DIVERGENCE"
            and item.get("first_bet_wait_reason")
        )
        self.assertIsNone(equal_state["first_leg"])
        first_placed = next(
            item for item in state.active_updates
            if item["monitoring_status"] == "FIRST_BET_PLACED"
        )
        self.assertEqual(first_placed["available_balance"], 9000.0)
        self.assertEqual(first_placed["reserved_balance"], 1000.0)

        history = await scanner.forks()
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["second_leg"])
        self.assertEqual(history[0]["status"], "HEDGE_NOT_FOUND")
        self.assertEqual(history[0]["profit_loss"], 650.0)
        self.assertEqual(state.values["current_balance"], 10650.0)

    async def test_direct_first_leg_only_loss_is_settled_once(self):
        state = RecordingState()
        scanner = ForksTableTennisScanner(browser_manager=object(), state=state)
        await scanner.configure(budget=1000, initial_stake=100)
        payload = zero_zero_candidate_payload(make_match())
        await scanner._record_first_leg(payload, side="p1", odds=1.50)

        first_result = await scanner._settle_series(payload, "p2")
        second_result = await scanner._settle_series(payload, "p2")

        self.assertEqual(first_result["profit_loss"], -100.0)
        self.assertEqual(second_result, first_result)
        self.assertEqual(state.values["current_balance"], 900.0)
        self.assertEqual(state.values["reserved_balance"], 0.0)
        self.assertEqual(len(await scanner.forks()), 1)

        next_payload = zero_zero_candidate_payload(make_match())
        await scanner._record_first_leg(next_payload, side="p2", odds=1.40)
        self.assertEqual(next_payload["balance_before_sequence"], 900.0)
        self.assertEqual(state.values["available_balance"], 800.0)


if __name__ == "__main__":
    unittest.main()
