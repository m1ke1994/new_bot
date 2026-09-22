import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.app.demo.engine import (
    BlockedMatchSwitch,
    DemoEngine,
    MissedSelectedTeamGoal,
    selected_team_scored,
)
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import (
    NextGoalOdds,
    Score,
    ScoreboardSnapshot,
    Scorer,
    TeamSelection,
)
from backend.app.demo.strategy import StrategyConfig
from backend.app.live.executor import BLOCKED_EVENT_SIGNAL
from backend.app.live.models import ActiveLiveBet, PendingLiveBet, PlacementObservation


def match(match_id: str) -> dict:
    return {
        "match_id": match_id,
        "url": f"https://example.test/{match_id}",
        "href": f"/{match_id}",
        "team1": f"{match_id} TEAM 1",
        "team2": f"{match_id} TEAM 2",
        "time": "00:30",
        "period": "",
    }


MATCHES = [match("A"), match("B"), match("C"), match("D"), match("E")]


def snapshot(selected_match: dict, score1: int = 0, score2: int = 0):
    return ScoreboardSnapshot(
        selected_match["team1"],
        selected_match["team2"],
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


class SwitchingLeagueBrowser:
    opened_match_ids: list[str] = []
    last_scan_stats = {"total": 5, "started": 0, "upcoming": 5}
    skipped_started: list[dict] = []
    skipped_excluded: list[dict] = []

    def __init__(self, _page, *, exclude_teams_enabled=True):
        self.exclude_teams_enabled = exclude_teams_enabled

    async def open(self):
        pass

    async def scan(self):
        return [dict(item) for item in MATCHES]

    async def open_match(self, selected_match):
        self.opened_match_ids.append(selected_match["match_id"])
        return {"url": selected_match["url"]}


class NextGoalBlockedSwitchTests(unittest.IsolatedAsyncioTestCase):
    def test_checkbox_false_keeps_legacy_blocked_recovery(self):
        engine = DemoEngine(FakeManager())
        engine._config = StrategyConfig.from_payload(
            {"blocked_events_switch_enabled": False}
        )

        enabled = engine._blocked_score_monitoring_enabled(
            observation=PlacementObservation(False, BLOCKED_EVENT_SIGNAL, True),
            selected_match=MATCHES[0],
        )

        self.assertFalse(enabled)

    def test_checkbox_true_monitors_blocked_score_from_step_one(self):
        engine = DemoEngine(FakeManager())
        engine._config = StrategyConfig.from_payload(
            {"blocked_events_switch_enabled": True}
        )

        enabled = engine._blocked_score_monitoring_enabled(
            observation=PlacementObservation(False, BLOCKED_EVENT_SIGNAL, True),
            selected_match=MATCHES[0],
        )

        self.assertTrue(enabled)

    def test_selected_team_score_delta_is_side_specific(self):
        attempt = Score(2, 3)

        self.assertTrue(selected_team_scored(attempt, Score(3, 3), Scorer.TEAM_1))
        self.assertTrue(selected_team_scored(attempt, Score(2, 5), Scorer.TEAM_2))
        self.assertFalse(selected_team_scored(attempt, Score(4, 3), Scorer.TEAM_2))
        self.assertFalse(selected_team_scored(attempt, Score(2, 6), Scorer.TEAM_1))

    async def _run_scenario(
        self,
        outcomes: list[str],
        *,
        start_step: int,
        stakes: list[float],
    ):
        SwitchingLeagueBrowser.opened_match_ids = []
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_sequence(
                current_step=start_step,
                status="WAITING_NEXT_MATCH",
                cumulative_pnl="-23.00",
                cumulative_losses="23.00",
            )
            budget_before = await repository.get_budget()
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch(
                    "backend.app.demo.engine.LeagueBrowser",
                    SwitchingLeagueBrowser,
                ),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {
                        "initial_stake": stakes[0],
                        "progression_multiplier": 2,
                        "max_steps": len(stakes),
                        "stakes": stakes,
                        "blocked_events_switch_enabled": True,
                    }
                )
                engine._sleep_or_stop = AsyncMock()

                async def initial_score(selected_match):
                    return snapshot(selected_match)

                async def read_odds(previous, selected_match):
                    current = snapshot(selected_match, previous.score.team1, previous.score.team2)
                    return current, NextGoalOdds(
                        2.10,
                        1.80,
                        next_goal_number=current.score.team1 + current.score.team2 + 1,
                        team1_locator=object(),
                        team2_locator=object(),
                    )

                async def fresh_score(_browser, selected_match, previous):
                    return snapshot(
                        selected_match,
                        previous.score.team1,
                        previous.score.team2,
                    )

                accepted_result = None

                async def prepare(**kwargs):
                    nonlocal accepted_result
                    outcome = outcomes.pop(0)
                    selected_match = kwargs["selected_match"]
                    current = kwargs["snapshot"]
                    if outcome == "MISSED":
                        attempt_record = {
                            **kwargs["record"],
                            "id": f"blocked-{selected_match['match_id']}",
                            "attempt_id": f"blocked-{selected_match['match_id']}",
                            "mode": "LIVE",
                            "odds": 2.10,
                        }
                        return await engine._finish_missed_selected_team_goal(
                            attempt_record=attempt_record,
                            selected_match=selected_match,
                            cycle_id=kwargs["cycle_id"],
                            step=kwargs["step"],
                            amount=kwargs["amount"],
                            score_after_removal=snapshot(selected_match, 1, 0),
                        )

                    accepted_result = outcome
                    record = {
                        **kwargs["record"],
                        "id": f"accepted-{selected_match['match_id']}",
                        "attempt_id": f"accepted-{selected_match['match_id']}",
                        "result": "ACTIVE",
                        "status": "ACTIVE",
                        "placement_confirmed_at": "now",
                    }
                    engine._active_live_bet = ActiveLiveBet(
                        attempt_id=record["id"],
                        match_id=selected_match["match_id"],
                        team=selected_match["team1"],
                        side=Scorer.TEAM_1,
                        strategy_step=kwargs["step"],
                        amount=kwargs["amount"],
                        coefficient=2.10,
                        goal_number=1,
                        score_before=current.score,
                    )
                    engine._pending_live_bet = None
                    return record, current, kwargs["current_odds"], 2.10, 1.80

                async def wait_for_goal(_browser, selected_match, _previous):
                    engine._stop_event.set()
                    if accepted_result == "ACCEPTED_WIN":
                        return snapshot(selected_match, 1, 0), Scorer.TEAM_1
                    return snapshot(selected_match, 0, 1), Scorer.TEAM_2

                engine._wait_for_initial_zero_score = initial_score
                engine._wait_for_odds = read_odds
                engine._read_fresh_score = fresh_score
                engine._prepare_live_until_placed = prepare
                engine._wait_for_goal = wait_for_goal

                expected_calls = len(outcomes)
                for _ in range(expected_calls):
                    engine._stop_event.clear()
                    await engine._process_next_match(object())

                history = await repository.history(mode="LIVE")
                sequence = await repository.get_sequence()
                budget_after = await repository.get_budget()
                stats = await repository.stats()

        return {
            "history": history,
            "sequence": sequence,
            "budget_before": budget_before,
            "budget_after": budget_after,
            "stats": stats,
            "opened": list(SwitchingLeagueBrowser.opened_match_ids),
        }

    async def test_step_three_blocked_switches_match_without_consuming_step(self):
        result = await self._run_scenario(
            ["MISSED", "ACCEPTED_LOSE"],
            start_step=3,
            stakes=[20, 44, 96, 211, 464],
        )

        self.assertEqual(result["opened"], ["A", "B"])
        self.assertEqual([item["step"] for item in result["history"]], [3, 3])
        self.assertEqual([item["amount"] for item in result["history"]], [96, 96])
        self.assertEqual(result["history"][0]["result"], "MISSED_SELECTED_TEAM_GOAL")
        self.assertEqual(result["history"][0]["budget_change"], 0)
        self.assertIn("A", result["sequence"]["blocked_match_ids"])

    async def test_selected_goal_on_first_step_switches_and_preserves_first_step(self):
        result = await self._run_scenario(
            ["MISSED"],
            start_step=1,
            stakes=[20, 45, 102],
        )

        self.assertEqual(result["opened"], ["A"])
        self.assertEqual(result["history"][0]["result"], "MISSED_SELECTED_TEAM_GOAL")
        self.assertEqual(result["history"][0]["step"], 1)
        self.assertEqual(result["history"][0]["amount"], 20)
        self.assertEqual(result["sequence"]["current_step"], 1)
        self.assertEqual(result["sequence"]["status"], "WAITING_NEXT_MATCH")

    async def test_selected_goal_on_last_step_does_not_exhaust_sequence(self):
        result = await self._run_scenario(
            ["MISSED"],
            start_step=3,
            stakes=[20, 45, 102],
        )

        self.assertEqual(result["history"][0]["result"], "MISSED_SELECTED_TEAM_GOAL")
        self.assertEqual(result["history"][0]["step"], 3)
        self.assertEqual(result["history"][0]["amount"], 102)
        self.assertEqual(result["sequence"]["current_step"], 3)
        self.assertEqual(result["sequence"]["status"], "WAITING_NEXT_MATCH")

    async def test_real_live_blocked_signal_removes_coupon_and_returns_switch(self):
        selected_match = MATCHES[0]
        current = snapshot(selected_match, 0, 2)
        missed = snapshot(selected_match, 1, 2)
        current_odds = NextGoalOdds(
            2.10,
            1.80,
            next_goal_number=3,
            team1_locator=object(),
            team2_locator=object(),
        )
        selection = TeamSelection(
            selected_match["team1"],
            Scorer.TEAM_1,
            2.10,
            selected_match["team2"],
            1.80,
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_sequence(
                current_step=3,
                status="ACTIVE",
                cumulative_losses="44.00",
            )
            budget_before = await repository.get_budget()
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {
                        "initial_stake": 20,
                        "progression_multiplier": 2,
                        "max_steps": 5,
                        "stakes": [20, 44, 96, 211, 464],
                        "blocked_events_switch_enabled": True,
                    }
                )
                engine._pending_live_bet = PendingLiveBet(
                    "A_TEAM_1_step3",
                    "A",
                    selected_match["team1"],
                    Scorer.TEAM_1,
                    3,
                    96,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.invalidate = AsyncMock()
                engine.live_executor.remove_blocked_coupon = AsyncMock(return_value=True)
                engine.live_executor.blocked_event_exists = AsyncMock(
                    return_value=False
                )
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    return_value=(
                        PlacementObservation(False, BLOCKED_EVENT_SIGNAL, True),
                        missed,
                    )
                )
                engine._read_fresh_score = AsyncMock(
                    side_effect=[current, missed]
                )
                engine._wait_for_odds = AsyncMock()

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match=selected_match,
                    selection=selection,
                    match_name="A TEAM 1 — A TEAM 2",
                    cycle_id=(await repository.get_sequence())["sequence_id"],
                    step=3,
                    amount=96,
                    snapshot=current,
                    current_odds=current_odds,
                    record={"mode": "LIVE"},
                )
                history = await repository.history(mode="LIVE")
                sequence = await repository.get_sequence()
                budget_after = await repository.get_budget()
                logs = await repository.logs()

        self.assertIsInstance(result, BlockedMatchSwitch)
        self.assertEqual(history[0]["result"], "MISSED_SELECTED_TEAM_GOAL")
        self.assertTrue(history[0]["settled"])
        self.assertEqual(history[0]["amount"], 96)
        self.assertEqual(history[0]["budget_change"], 0)
        self.assertEqual(sequence["current_step"], 3)
        self.assertEqual(sequence["cumulative_losses"], "44.00")
        self.assertEqual(budget_after, budget_before)
        self.assertIn("A", sequence["blocked_match_ids"])
        engine.live_executor.remove_blocked_coupon.assert_awaited_once()
        engine.live_executor.prepare.assert_awaited_once()
        engine._wait_for_odds.assert_not_awaited()
        self.assertIn(
            "NEXT_GOAL_BLOCKED_SELECTED_TEAM_SCORED",
            [item["event"] for item in logs],
        )

    async def test_guard_detects_selected_goal_after_opponent_goals(self):
        engine = DemoEngine(FakeManager())

        with self.assertRaises(MissedSelectedTeamGoal) as caught:
            await engine._guard_blocked_recovery_score(
                attempt_score=Score(1, 1),
                current=snapshot(MATCHES[0], 2, 4),
                selected_side=Scorer.TEAM_1,
            )

        self.assertEqual(caught.exception.snapshot.score, Score(2, 4))

    def test_both_teams_scoring_still_detects_selected_team_delta(self):
        self.assertTrue(
            selected_team_scored(Score(2, 0), Score(3, 1), Scorer.TEAM_2)
        )

    async def _run_confirmed_blocked_recovery(
        self,
        latest_score: ScoreboardSnapshot,
        *,
        selected_team_scored: bool,
    ):
        selected_match = MATCHES[0]
        before = snapshot(selected_match, 2, 0)
        old_team2_locator = object()
        fresh_team2_locator = object()
        initial_odds = NextGoalOdds(
            1.80,
            2.03,
            next_goal_number=3,
            team1_locator=object(),
            team2_locator=old_team2_locator,
        )
        fresh_goal_number = (
            latest_score.score.team1 + latest_score.score.team2 + 1
        )
        fresh_odds = NextGoalOdds(
            1.81,
            2.04,
            next_goal_number=fresh_goal_number,
            team1_locator=object(),
            team2_locator=fresh_team2_locator,
        )
        selection = TeamSelection(
            selected_match["team2"],
            Scorer.TEAM_2,
            2.03,
            selected_match["team1"],
            1.80,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            sequence = await repository.save_sequence(
                current_step=2,
                status="ACTIVE",
                current_match_id="A",
                selected_team=selected_match["team2"],
            )
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {
                        "stakes": [20, 45, 102],
                        "max_steps": 3,
                        "blocked_events_switch_enabled": True,
                    }
                )
                engine._pending_live_bet = PendingLiveBet(
                    "A_TEAM_2_step2",
                    "A",
                    selected_match["team2"],
                    Scorer.TEAM_2,
                    2,
                    45,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.invalidate = AsyncMock()
                engine.live_executor.remove_blocked_coupon = AsyncMock(return_value=True)
                engine.live_executor.blocked_event_exists = AsyncMock(
                    return_value=False
                )
                engine._publish_pending_bet = AsyncMock()
                engine._read_fresh_score = AsyncMock(
                    side_effect=[
                        before,
                        latest_score,
                        latest_score,
                        latest_score,
                    ]
                )
                engine._wait_for_odds = AsyncMock(
                    return_value=(latest_score, fresh_odds)
                )

                if selected_team_scored:
                    engine._wait_for_live_confirmation_or_score = AsyncMock(
                        return_value=(
                            PlacementObservation(False, BLOCKED_EVENT_SIGNAL, True),
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
                                    True,
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
                    selected_match=selected_match,
                    selection=selection,
                    match_name="A TEAM 1 — A TEAM 2",
                    cycle_id=sequence["sequence_id"],
                    step=2,
                    amount=45,
                    snapshot=before,
                    current_odds=initial_odds,
                    record={"mode": "LIVE"},
                )
                final_sequence = await repository.get_sequence()
                history = await repository.history(mode="LIVE")
                logs = await repository.logs()

        decisions = [
            call.args[1]
            for call in engine.live_executor.prepare.await_args_list
        ]

        if selected_team_scored:
            self.assertIsInstance(result, BlockedMatchSwitch)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(final_sequence["current_step"], 2)
            self.assertEqual(final_sequence["status"], "WAITING_NEXT_MATCH")
            self.assertIsNone(final_sequence["selected_team"])
            self.assertIn("A", final_sequence["blocked_match_ids"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["result"], "MISSED_SELECTED_TEAM_GOAL")
            engine._wait_for_odds.assert_not_awaited()
        else:
            self.assertNotIsInstance(result, BlockedMatchSwitch)
            self.assertIsNotNone(result)
            self.assertEqual(len(decisions), 2)
            self.assertEqual([item.strategy_step for item in decisions], [2, 2])
            self.assertEqual([item.amount for item in decisions], [45, 45])
            self.assertEqual(
                [item.team for item in decisions],
                [selected_match["team2"], selected_match["team2"]],
            )
            self.assertEqual(
                [item.goal_number for item in decisions],
                [3, fresh_goal_number],
            )
            self.assertIs(decisions[0].coefficient_locator, old_team2_locator)
            self.assertIs(decisions[1].coefficient_locator, fresh_team2_locator)
            self.assertEqual(final_sequence["current_step"], 2)
            self.assertEqual(final_sequence["selected_team"], selected_match["team2"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["result"], "ACTIVE")
            self.assertEqual(history[0]["score_before"], latest_score.score.text())
            self.assertIn(
                "LIVE_BLOCKED_RETRYING_SAME_MATCH",
                [item["event"] for item in logs],
            )

        return logs

    async def test_opponent_goal_during_confirmed_coupon_lock_retries_same_match(self):
        logs = await self._run_confirmed_blocked_recovery(
            snapshot(MATCHES[0], 3, 0),
            selected_team_scored=False,
        )
        self.assertIn(
            "NEXT_GOAL_BLOCKED_OPPONENT_SCORED",
            [item["event"] for item in logs],
        )

    async def test_unchanged_score_during_confirmed_coupon_lock_retries_same_match(self):
        logs = await self._run_confirmed_blocked_recovery(
            snapshot(MATCHES[0], 2, 0),
            selected_team_scored=False,
        )
        self.assertIn(
            "NEXT_GOAL_BLOCKED_SCORE_UNCHANGED",
            [item["event"] for item in logs],
        )

    async def test_selected_team_goal_during_confirmed_coupon_lock_switches_match(self):
        logs = await self._run_confirmed_blocked_recovery(
            snapshot(MATCHES[0], 2, 1),
            selected_team_scored=True,
        )
        self.assertIn(
            "NEXT_GOAL_BLOCKED_SELECTED_TEAM_SCORED",
            [item["event"] for item in logs],
        )

    async def test_accepted_confirmation_has_priority_over_later_selected_goal(self):
        selected_match = MATCHES[0]
        before = snapshot(selected_match, 0, 2)
        after = snapshot(selected_match, 1, 2)
        odds = NextGoalOdds(
            2.10,
            1.80,
            next_goal_number=3,
            team1_locator=object(),
            team2_locator=object(),
        )
        selection = TeamSelection(
            selected_match["team1"],
            Scorer.TEAM_1,
            2.10,
            selected_match["team2"],
            1.80,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {"blocked_events_switch_enabled": True}
                )
                engine._pending_live_bet = PendingLiveBet(
                    "A_TEAM_1_step1",
                    "A",
                    selected_match["team1"],
                    Scorer.TEAM_1,
                    1,
                    20,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine._read_fresh_score = AsyncMock(return_value=before)
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    return_value=(PlacementObservation(True, "accepted"), before)
                )

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match=selected_match,
                    selection=selection,
                    match_name="A TEAM 1 — A TEAM 2",
                    cycle_id="cycle",
                    step=1,
                    amount=20,
                    snapshot=before,
                    current_odds=odds,
                    record={"mode": "LIVE"},
                )
                blocked_ids = (await repository.get_sequence())["blocked_match_ids"]
                history = await repository.history(mode="LIVE")

        self.assertIsNotNone(result)
        self.assertEqual(engine._active_live_bet.score_before, before.score)
        self.assertTrue(selected_team_scored(before.score, after.score, Scorer.TEAM_1))
        self.assertEqual(history[0]["result"], "ACTIVE")
        self.assertEqual(blocked_ids, [])

    async def test_submission_unknown_does_not_switch_or_create_second_attempt(self):
        selected_match = MATCHES[0]
        before = snapshot(selected_match, 0, 2)
        odds = NextGoalOdds(
            2.10,
            1.80,
            next_goal_number=3,
            team1_locator=object(),
            team2_locator=object(),
        )
        selection = TeamSelection(
            selected_match["team1"],
            Scorer.TEAM_1,
            2.10,
            selected_match["team2"],
            1.80,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {"blocked_events_switch_enabled": True}
                )
                engine._pending_live_bet = PendingLiveBet(
                    "A_TEAM_1_step2",
                    "A",
                    selected_match["team1"],
                    Scorer.TEAM_1,
                    2,
                    45,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine._read_fresh_score = AsyncMock(return_value=before)
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    return_value=(None, before)
                )

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match=selected_match,
                    selection=selection,
                    match_name="A TEAM 1 — A TEAM 2",
                    cycle_id="cycle",
                    step=2,
                    amount=45,
                    snapshot=before,
                    current_odds=odds,
                    record={"mode": "LIVE"},
                )
                history = await repository.history(mode="LIVE")
                blocked_ids = (await repository.get_sequence())["blocked_match_ids"]

        self.assertIsNone(result)
        self.assertEqual(history[0]["result"], "SUBMISSION_UNKNOWN")
        self.assertEqual(blocked_ids, [])
        engine.live_executor.prepare.assert_awaited_once()

    async def test_two_blocked_matches_then_real_loss_advances_only_once(self):
        result = await self._run_scenario(
            ["MISSED", "MISSED", "ACCEPTED_LOSE"],
            start_step=3,
            stakes=[20, 44, 96, 211, 464],
        )

        self.assertEqual(result["opened"], ["A", "B", "C"])
        self.assertEqual(
            [item["result"] for item in result["history"]],
            ["MISSED_SELECTED_TEAM_GOAL", "MISSED_SELECTED_TEAM_GOAL", "LOSE"],
        )
        self.assertEqual([item["step"] for item in result["history"]], [3, 3, 3])
        self.assertEqual([item["amount"] for item in result["history"]], [96, 96, 96])
        self.assertEqual(result["sequence"]["current_step"], 4)

    async def test_blocked_then_win_resets_step_and_blocked_matches(self):
        result = await self._run_scenario(
            ["MISSED", "ACCEPTED_WIN"],
            start_step=5,
            stakes=[20, 44, 96, 211, 464],
        )

        self.assertEqual(result["opened"], ["A", "B"])
        self.assertEqual(
            [item["result"] for item in result["history"]],
            ["MISSED_SELECTED_TEAM_GOAL", "WIN"],
        )
        self.assertEqual([item["amount"] for item in result["history"]], [464, 464])
        self.assertEqual(result["sequence"]["current_step"], 1)
        self.assertEqual(result["sequence"]["blocked_match_ids"], [])

    async def test_blocked_attempt_never_changes_budget_or_loss_statistics(self):
        result = await self._run_scenario(
            ["MISSED", "ACCEPTED_WIN"],
            start_step=3,
            stakes=[10, 13, 17],
        )

        self.assertEqual(result["budget_after"], result["budget_before"])
        self.assertEqual(result["stats"]["losses"], 0)
        self.assertEqual(result["history"][0]["pnl"], 0)

    async def test_blocked_match_is_not_selected_again_in_same_sequence(self):
        result = await self._run_scenario(
            ["MISSED", "MISSED", "ACCEPTED_WIN"],
            start_step=3,
            stakes=[10, 13, 17],
        )

        self.assertEqual(result["opened"], ["A", "B", "C"])
        self.assertEqual(len(result["opened"]), len(set(result["opened"])))

    def test_submission_unknown_never_enables_match_switch(self):
        engine = DemoEngine(FakeManager())
        engine._config = StrategyConfig.from_payload(
            {"blocked_events_switch_enabled": True}
        )

        no_observation = engine._blocked_score_monitoring_enabled(
            observation=None,
            selected_match=MATCHES[0],
        )
        ambiguous_observation = engine._blocked_score_monitoring_enabled(
            observation=PlacementObservation(False, "SUBMISSION_UNKNOWN"),
            selected_match=MATCHES[0],
        )

        self.assertFalse(no_observation)
        self.assertFalse(ambiguous_observation)


if __name__ == "__main__":
    unittest.main()
