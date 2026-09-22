import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.app.browser.market import MarketNotAvailable
from backend.app.demo.engine import (
    LIVE_SCORE_ACCEPTED_SIGNAL,
    BlockedMatchSwitch,
    DemoEngine,
    ModeConflictError,
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


def snapshot(score1, score2):
    return ScoreboardSnapshot("TEAM 1", "TEAM 2", Score(score1, score2), "01:00", "1-й тайм")


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class LivePendingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_blocked_recovery(
        self,
        latest_score: ScoreboardSnapshot,
        *,
        selected_team_scored: bool,
    ):
        initial = snapshot(1, 1)
        initial_odds = NextGoalOdds(
            1.80,
            2.03,
            next_goal_number=3,
            team1_locator=object(),
            team2_locator=object(),
        )
        next_goal = latest_score.score.team1 + latest_score.score.team2 + 1
        retry_odds = NextGoalOdds(
            1.82,
            2.04,
            next_goal_number=next_goal,
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
                current_step=3,
                status="ACTIVE",
                current_match_id="match",
                selected_team="TEAM 2",
            )
            budget_before = (await repository.get_budget())["current_budget"]
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._pending_live_bet = PendingLiveBet(
                    "match_TEAM_2_step3",
                    "match",
                    "TEAM 2",
                    Scorer.TEAM_2,
                    3,
                    288,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.invalidate = AsyncMock()
                engine.live_executor.remove_blocked_coupon = AsyncMock(
                    return_value=True
                )
                engine.live_executor.blocked_event_exists = AsyncMock(
                    return_value=False
                )
                engine._publish_pending_bet = AsyncMock()
                engine._read_fresh_score = AsyncMock(
                    side_effect=[
                        initial,
                        latest_score,
                        latest_score,
                        latest_score,
                    ]
                )
                engine._wait_for_odds = AsyncMock(
                    return_value=(latest_score, retry_odds)
                )
                if selected_team_scored:
                    engine._wait_for_live_confirmation_or_score = AsyncMock(
                        return_value=(
                            PlacementObservation(
                                False,
                                BLOCKED_EVENT_SIGNAL,
                                retryable=True,
                            ),
                            latest_score,
                        )
                    )
                else:
                    engine._wait_for_live_confirmation_or_score = AsyncMock(
                        side_effect=[
                            (
                                PlacementObservation(
                                    False,
                                    BLOCKED_EVENT_SIGNAL,
                                    retryable=True,
                                ),
                                latest_score,
                            ),
                            (
                                PlacementObservation(True, "accepted"),
                                latest_score,
                            ),
                        ]
                    )

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match={"match_id": "match"},
                    selection=selection,
                    match_name="TEAM 1 — TEAM 2",
                    cycle_id=sequence["sequence_id"],
                    step=3,
                    amount=288,
                    snapshot=initial,
                    current_odds=initial_odds,
                    record={"mode": "LIVE"},
                )

                history = await repository.history(mode="LIVE")
                final_sequence = await repository.get_sequence()
                budget_after = (await repository.get_budget())["current_budget"]
                logs = await repository.logs()

        decisions = [
            call.args[1]
            for call in engine.live_executor.prepare.await_args_list
        ]
        self.assertEqual(budget_after, budget_before)
        engine.live_executor.remove_blocked_coupon.assert_awaited_once()

        if selected_team_scored:
            self.assertIsInstance(result, BlockedMatchSwitch)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(final_sequence["current_step"], 3)
            self.assertEqual(final_sequence["status"], "WAITING_NEXT_MATCH")
            self.assertIsNone(final_sequence["selected_team"])
            self.assertIn("match", final_sequence["blocked_match_ids"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["result"], "MISSED_SELECTED_TEAM_GOAL")
            self.assertEqual(history[0]["step"], 3)
            self.assertEqual(history[0]["budget_change"], 0)
            engine._wait_for_odds.assert_not_awaited()
            self.assertIn(
                "NEXT_GOAL_BLOCKED_SELECTED_TEAM_SCORED",
                [item["event"] for item in logs],
            )
        else:
            self.assertNotIsInstance(result, BlockedMatchSwitch)
            self.assertIsNotNone(result)
            self.assertEqual(len(decisions), 2)
            self.assertEqual([item.strategy_step for item in decisions], [3, 3])
            self.assertEqual([item.amount for item in decisions], [288, 288])
            self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 2"])
            self.assertEqual([item.goal_number for item in decisions], [3, next_goal])
            self.assertEqual(final_sequence["current_step"], 3)
            self.assertEqual(final_sequence["selected_team"], "TEAM 2")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["result"], "ACTIVE")
            self.assertEqual(history[0]["step"], 3)
            self.assertEqual(history[0]["next_goal_number"], next_goal)
            engine._wait_for_odds.assert_awaited()
            self.assertIn(
                "LIVE_BLOCKED_RETRYING_SAME_MATCH",
                [item["event"] for item in logs],
            )

        return engine, logs

    async def test_blocked_pending_without_score_change_retries_same_match(self):
        await self._run_blocked_recovery(
            snapshot(1, 1),
            selected_team_scored=False,
        )

    async def test_blocked_pending_opponent_goal_retries_same_match(self):
        _engine, logs = await self._run_blocked_recovery(
            snapshot(2, 1),
            selected_team_scored=False,
        )
        self.assertIn(
            "NEXT_GOAL_BLOCKED_OPPONENT_SCORED",
            [item["event"] for item in logs],
        )

    async def test_blocked_pending_selected_team_goal_switches_match(self):
        await self._run_blocked_recovery(
            snapshot(1, 2),
            selected_team_scored=True,
        )

    async def test_one_goal_after_confirm_click_is_live_acceptance_signal(self):
        engine = DemoEngine(FakeManager())
        before = snapshot(1, 1)
        changed = snapshot(2, 1)

        async def wait_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        engine.live_executor.wait_for_manual_confirmation = AsyncMock(
            side_effect=wait_forever
        )
        engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
        engine.live_executor.blocked_event_exists = AsyncMock(return_value=False)
        engine._wait_for_pending_score_change = AsyncMock(return_value=changed)

        observation, latest = await engine._wait_for_live_confirmation_or_score(
            page=object(),
            browser=object(),
            selected_match={"match_id": "match"},
            snapshot=before,
            decision=SimpleNamespace(attempt_id="attempt"),
            publish=AsyncMock(),
        )

        self.assertTrue(observation.placed)
        self.assertEqual(observation.signal, LIVE_SCORE_ACCEPTED_SIGNAL)
        self.assertEqual(latest.score, Score(2, 1))

    async def test_coupon_lock_has_priority_over_score_acceptance_signal(self):
        engine = DemoEngine(FakeManager())
        before = snapshot(1, 1)
        changed = snapshot(2, 1)

        async def wait_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        engine.live_executor.wait_for_manual_confirmation = AsyncMock(
            side_effect=wait_forever
        )
        engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
        engine.live_executor.blocked_event_exists = AsyncMock(return_value=True)
        engine._wait_for_pending_score_change = AsyncMock(return_value=changed)

        observation, latest = await engine._wait_for_live_confirmation_or_score(
            page=object(),
            browser=object(),
            selected_match={"match_id": "match"},
            snapshot=before,
            decision=SimpleNamespace(attempt_id="attempt"),
            publish=AsyncMock(),
        )

        self.assertFalse(observation.placed)
        self.assertEqual(observation.signal, BLOCKED_EVENT_SIGNAL)
        self.assertEqual(latest.score, Score(2, 1))

    async def test_score_based_acceptance_keeps_original_bet_baseline(self):
        initial = snapshot(1, 1)
        changed = snapshot(2, 1)
        odds = NextGoalOdds(
            1.80,
            2.03,
            next_goal_number=3,
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
                status="ACTIVE",
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
                    42,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.blocked_event_exists = AsyncMock(return_value=False)
                engine._read_fresh_score = AsyncMock(return_value=initial)
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    return_value=(
                        PlacementObservation(True, LIVE_SCORE_ACCEPTED_SIGNAL),
                        changed,
                    )
                )

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match={"match_id": "match"},
                    selection=selection,
                    match_name="TEAM 1 — TEAM 2",
                    cycle_id=sequence["sequence_id"],
                    step=2,
                    amount=42,
                    snapshot=initial,
                    current_odds=odds,
                    record={"mode": "LIVE"},
                )

        self.assertIsNotNone(result)
        _record, accepted_baseline, _odds, _selected, _opponent = result
        self.assertEqual(accepted_baseline.score, Score(1, 1))
        self.assertEqual(engine._active_live_bet.score_before, Score(1, 1))

    def test_score_updates_only_goal_number_of_pending_decision(self):
        pending = PendingLiveBet(
            "match_TEAM_2_step2",
            "match",
            "TEAM 2",
            Scorer.TEAM_2,
            2,
            42,
            2,
        )

        pending = pending.with_score(Score(2, 0)).with_score(Score(2, 1))

        self.assertEqual(pending.strategy_step, 2)
        self.assertEqual(pending.amount, 42)
        self.assertEqual(pending.team, "TEAM 2")
        self.assertEqual(pending.target_goal_number, 4)

    async def test_market_locked_keeps_step_and_uses_current_goal_when_available(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._pending_live_bet = PendingLiveBet(
            "match_TEAM_2_step2",
            "match",
            "TEAM 2",
            Scorer.TEAM_2,
            2,
            42,
            2,
        )
        engine._sleep_or_stop = AsyncMock()
        snapshots = iter([snapshot(1, 0), snapshot(2, 0), snapshot(2, 1), snapshot(2, 1)])

        class FakeMatchBrowser:
            def __init__(self, _page):
                pass

            async def snapshot(self):
                return next(snapshots)

        async def read_odds(
            _page,
            _team1,
            _team2,
            score1,
            score2,
            _logger,
            **_kwargs,
        ):
            goal = score1 + score2 + 1
            if goal < 4:
                raise MarketNotAvailable("locked", status="MARKET_LOCKED")
            return NextGoalOdds(1.8, 2.03, next_goal_number=goal)

        with (
            patch("backend.app.demo.engine.MatchBrowser", FakeMatchBrowser),
            patch("backend.app.demo.engine.read_next_goal_odds", new=read_odds),
            patch("backend.app.demo.engine.REPOSITORY.log", new=AsyncMock()),
            patch("backend.app.demo.engine.REPOSITORY.save_bet", new=AsyncMock()) as save_bet,
        ):
            current, odds = await engine._wait_for_odds(snapshot(1, 0), {"match_id": "match"})

        self.assertEqual(current.score, Score(2, 1))
        self.assertEqual(odds.next_goal_number, 4)
        self.assertEqual(engine._pending_live_bet.strategy_step, 2)
        self.assertEqual(engine._pending_live_bet.amount, 42)
        self.assertEqual(engine._pending_live_bet.team, "TEAM 2")
        self.assertEqual(engine._pending_live_bet.target_goal_number, 4)
        save_bet.assert_not_awaited()

    async def test_unresolved_manual_submission_blocks_live_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet(
                {
                    "id": "unknown",
                    "mode": "LIVE",
                    "result": "SUBMISSION_UNKNOWN",
                    "status": "AWAITING_PLACEMENT_RESULT",
                }
            )
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())

                with self.assertRaises(ModeConflictError):
                    await engine.start("LIVE")

    async def test_crash_after_dom_active_signal_blocks_duplicate_live_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet(
                {
                    "id": "pending-after-active-signal",
                    "mode": "LIVE",
                    "result": "PENDING",
                    "status": "ACTIVE",
                }
            )
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())

                with self.assertRaises(ModeConflictError):
                    await engine.start("LIVE")


if __name__ == "__main__":
    unittest.main()
