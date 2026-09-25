import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import (
    DemoEngine,
    UNACCEPTED_FLIP_SIDE,
    UNACCEPTED_INVALID_SCORE,
    UNACCEPTED_KEEP_SIDE,
    UNACCEPTED_NO_CHANGE,
    resolve_unaccepted_score_transition,
)
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import (
    NextGoalOdds,
    Score,
    ScoreboardSnapshot,
    Scorer,
    TeamSelection,
)
from backend.app.live.executor import BLOCKED_EVENT_SIGNAL
from backend.app.live.models import PendingLiveBet, PlacementObservation


def snapshot(score1: int, score2: int) -> ScoreboardSnapshot:
    return ScoreboardSnapshot(
        "TEAM 1",
        "TEAM 2",
        Score(score1, score2),
        "01:00",
        "1-й тайм",
    )


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class LivePendingTests(unittest.IsolatedAsyncioTestCase):
    def test_score_transition_no_change(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2, Score(1, 0), Score(1, 0)
            ),
            UNACCEPTED_NO_CHANGE,
        )

    def test_score_transition_keeps_side_when_only_opponent_scores(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2, Score(1, 0), Score(3, 0)
            ),
            UNACCEPTED_KEEP_SIDE,
        )

    def test_score_transition_flips_when_selected_team_scores(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2, Score(1, 0), Score(3, 1)
            ),
            UNACCEPTED_FLIP_SIDE,
        )

    def test_score_transition_flips_when_both_teams_score(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2, Score(1, 0), Score(3, 2)
            ),
            UNACCEPTED_FLIP_SIDE,
        )

    def test_score_transition_rejects_backwards_score(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2, Score(3, 1), Score(2, 1)
            ),
            UNACCEPTED_INVALID_SCORE,
        )

    async def _run_unaccepted_recovery(
        self,
        latest_score: ScoreboardSnapshot,
        *,
        signal: str = "NOT_ACCEPTED_NO_CONFIRMATION",
    ):
        initial = snapshot(1, 0)
        initial_odds = NextGoalOdds(
            1.80,
            2.03,
            next_goal_number=2,
            team1_locator=object(),
            team2_locator=object(),
        )
        retry_goal = latest_score.score.team1 + latest_score.score.team2 + 1
        retry_odds = NextGoalOdds(
            1.91,
            2.08,
            next_goal_number=retry_goal,
            team1_locator=object(),
            team2_locator=object(),
        )
        selection = TeamSelection(
            "TEAM 2",
            Scorer.TEAM_2,
            2.03,
            "TEAM 1",
            1.80,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            sequence = await repository.save_sequence(
                current_step=2,
                status="PENDING",
                current_match_id="match",
                selected_team="TEAM 2",
            )
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._pending_live_bet = PendingLiveBet(
                    "match_TEAM_2_step2",
                    "match",
                    "TEAM 2",
                    Scorer.TEAM_2,
                    2,
                    22,
                    2,
                )
                engine._sleep_or_stop = AsyncMock()
                engine._publish_pending_bet = AsyncMock()
                engine.live_executor.prepare = AsyncMock(side_effect=[None, None])
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.invalidate = AsyncMock()
                engine.live_executor.clear_unaccepted_coupon = AsyncMock(
                    return_value=True
                )
                engine._read_fresh_score = AsyncMock(
                    side_effect=[
                        latest_score,
                        latest_score,
                        latest_score,
                    ]
                )
                engine._wait_for_odds = AsyncMock(
                    return_value=(latest_score, retry_odds)
                )
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    side_effect=[
                        (PlacementObservation(False, signal, retryable=True), latest_score),
                        (PlacementObservation(True, "accepted"), latest_score),
                    ]
                )

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match={"match_id": "match"},
                    selection=selection,
                    match_name="TEAM 1 — TEAM 2",
                    cycle_id=sequence["sequence_id"],
                    step=2,
                    amount=22,
                    snapshot=initial,
                    current_odds=initial_odds,
                    record={"mode": "LIVE"},
                )

                history = await repository.history(mode="LIVE")
                final_sequence = await repository.get_sequence()
                logs = await repository.logs()

        decisions = [call.args[1] for call in engine.live_executor.prepare.await_args_list]
        return engine, result, decisions, history, final_sequence, logs

    async def test_not_accepted_without_score_change_retries_same_team_and_step(self):
        engine, result, decisions, history, sequence, logs = (
            await self._run_unaccepted_recovery(snapshot(1, 0))
        )
        self.assertIsNotNone(result)
        self.assertEqual([item.strategy_step for item in decisions], [2, 2])
        self.assertEqual([item.amount for item in decisions], [22, 22])
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 2"])
        self.assertEqual(sequence["current_step"], 2)
        self.assertEqual(sequence["selected_team"], "TEAM 2")
        self.assertFalse(engine._stop_event.is_set())
        self.assertEqual([item["result"] for item in history], ["ACTIVE"])
        self.assertIn(
            "LIVE_UNACCEPTED_KEEP_SIDE",
            [item["event"] for item in logs],
        )

    async def test_not_accepted_opponent_goals_keep_selected_team(self):
        _engine, _result, decisions, _history, sequence, _logs = (
            await self._run_unaccepted_recovery(snapshot(3, 0))
        )
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 2"])
        self.assertEqual([item.strategy_step for item in decisions], [2, 2])
        self.assertEqual(sequence["current_step"], 2)

    async def test_not_accepted_selected_team_goal_flips_side_same_step(self):
        engine, result, decisions, history, sequence, logs = (
            await self._run_unaccepted_recovery(snapshot(3, 1))
        )
        self.assertIsNotNone(result)
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 1"])
        self.assertEqual([item.side for item in decisions], [Scorer.TEAM_2, Scorer.TEAM_1])
        self.assertEqual([item.strategy_step for item in decisions], [2, 2])
        self.assertEqual([item.amount for item in decisions], [22, 22])
        self.assertEqual(sequence["current_step"], 2)
        self.assertEqual(sequence["selected_team"], "TEAM 1")
        self.assertEqual([item["result"] for item in history], ["ACTIVE"])
        self.assertFalse(engine._stop_event.is_set())
        self.assertIn(
            "LIVE_UNACCEPTED_FLIP_SIDE",
            [item["event"] for item in logs],
        )
        _record, _baseline, _odds, _selected, _opponent, final_selection = result
        self.assertEqual(final_selection.selected_side, Scorer.TEAM_1)

    async def test_not_accepted_both_teams_score_still_flips(self):
        _engine, _result, decisions, _history, sequence, _logs = (
            await self._run_unaccepted_recovery(snapshot(3, 2))
        )
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 1"])
        self.assertEqual(sequence["current_step"], 2)

    async def test_blocked_signal_is_generic_unaccepted_recovery_not_match_switch(self):
        engine, result, decisions, history, sequence, logs = (
            await self._run_unaccepted_recovery(
                snapshot(3, 1),
                signal=BLOCKED_EVENT_SIGNAL,
            )
        )
        self.assertIsNotNone(result)
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 1"])
        self.assertEqual(sequence["current_step"], 2)
        self.assertEqual(sequence["selected_team"], "TEAM 1")
        self.assertEqual([item["result"] for item in history], ["ACTIVE"])
        self.assertFalse(engine._stop_event.is_set())
        self.assertNotIn(
            "MISSED_SELECTED_TEAM_GOAL",
            [item.get("result") for item in history],
        )
        self.assertIn(
            "LIVE_UNACCEPTED_FLIP_SIDE",
            [item["event"] for item in logs],
        )

    async def test_unaccepted_attempt_does_not_increment_or_advance_strategy_step(self):
        _engine, _result, decisions, _history, sequence, _logs = (
            await self._run_unaccepted_recovery(snapshot(3, 0))
        )
        self.assertEqual([item.strategy_step for item in decisions], [2, 2])
        self.assertEqual(sequence["current_step"], 2)

    async def test_accepted_after_recovery_keeps_fresh_baseline_and_selection(self):
        _engine, result, _decisions, _history, _sequence, _logs = (
            await self._run_unaccepted_recovery(snapshot(3, 1))
        )
        record, baseline, odds, selected, opponent, final_selection = result
        self.assertEqual(record["result"], "ACTIVE")
        self.assertEqual(baseline.score, Score(3, 1))
        self.assertEqual(final_selection.selected_team, "TEAM 1")
        self.assertEqual(selected, odds.team1)
        self.assertEqual(opponent, odds.team2)

    def test_pending_score_update_preserves_step_and_amount(self):
        pending = PendingLiveBet(
            "match_TEAM_2_step2",
            "match",
            "TEAM 2",
            Scorer.TEAM_2,
            2,
            22,
            2,
        )
        pending = pending.with_score(Score(2, 0)).with_score(Score(3, 0))
        self.assertEqual(pending.strategy_step, 2)
        self.assertEqual(pending.amount, 22)
        self.assertEqual(pending.team, "TEAM 2")
        self.assertEqual(pending.target_goal_number, 4)

    async def test_cleanup_is_called_before_same_step_retry(self):
        engine, _result, _decisions, _history, _sequence, _logs = (
            await self._run_unaccepted_recovery(snapshot(3, 0))
        )
        self.assertGreaterEqual(
            engine.live_executor.clear_unaccepted_coupon.await_count,
            1,
        )

    async def test_live_worker_stop_event_not_set_by_unaccepted_recovery(self):
        engine, _result, _decisions, _history, _sequence, _logs = (
            await self._run_unaccepted_recovery(snapshot(3, 1))
        )
        self.assertFalse(engine._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
