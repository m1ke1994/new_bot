import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import NextGoalOdds, Score, Scorer, ScoreboardSnapshot, TeamSelection
from backend.app.demo.strategy import StrategyConfig
from backend.app.long_series import LongSeriesDecision, LongSeriesGate


class OneMatchLeagueBrowser:
    last_scan_stats = {
        "total": 1,
        "started": 0,
        "finished": 0,
        "excluded": 0,
        "unclassified": 0,
        "upcoming": 1,
    }
    skipped_started = []
    skipped_excluded = []
    skipped_unclassified = []

    def __init__(self, _page, *, exclude_teams_enabled=True):
        self.exclude_teams_enabled = exclude_teams_enabled

    async def open(self):
        return None

    async def scan(self):
        return [
            {
                "match_id": "B",
                "team1": "Gamma",
                "team2": "Delta",
                "time": "00:30",
                "period": "",
                "url": "https://example.test/B",
            }
        ]

    async def open_match(self, selected):
        return {"url": selected["url"]}

    async def wait_match_content_ready(self):
        return {
            "score": "0:0",
            "period": "1-й тайм",
            "market_selector": "canvas",
            "elapsed_ms": 10_000,
            "attempts": 1,
        }


class LongSeriesGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate = LongSeriesGate()
        await self.gate.load(enabled=True, mode="LIVE")

    async def _finish(self, match_id: str, match_name: str, winning_step: int):
        self.assertEqual(
            await self.gate.claim_match(match_id, match_name),
            LongSeriesDecision.SHADOW,
        )
        await self.gate.start_observation(
            match_id=match_id,
            match_name=match_name,
            selected_team="A1",
            selected_side="TEAM_1",
            initial_odds=2.05,
        )
        for step in range(1, winning_step):
            await self.gate.begin_shadow_step(
                match_id=match_id,
                step=step,
                current_odds=2.05,
                score_before=f"0:{step - 1}",
            )
            await self.gate.record_shadow_step(
                match_id=match_id,
                match_name=match_name,
                step=step,
                result="LOSE",
                current_odds=2.05,
                score_before=f"0:{step - 1}",
                score_after=f"0:{step}",
                scorer="A2",
            )
        await self.gate.begin_shadow_step(
            match_id=match_id,
            step=winning_step,
            current_odds=1.95,
            score_before=f"0:{winning_step - 1}",
        )
        return await self.gate.record_shadow_step(
            match_id=match_id,
            match_name=match_name,
            step=winning_step,
            result="WIN",
            current_odds=1.95,
            score_before=f"0:{winning_step - 1}",
            score_after=f"1:{winning_step - 1}",
            scorer="A1",
        )

    async def test_short_three_steps_keeps_next_match_in_shadow(self):
        runtime = await self._finish("A", "A1 — A2", 3)
        self.assertEqual(runtime["state"], "WAITING_FOR_LONG")
        self.assertFalse(runtime["last_observed_is_long"])
        self.assertIsNone(runtime["active_observation"])
        self.assertEqual(runtime["observations"][-1]["classification"], "SHORT")
        self.assertEqual(runtime["observations"][-1]["series_length"], 3)
        self.assertEqual(len(runtime["observations"][-1]["steps"]), 3)
        self.assertEqual(
            await self.gate.claim_match("B", "B1 — B2"),
            LongSeriesDecision.SHADOW,
        )

    async def test_step_four_unlocks_exactly_one_next_match(self):
        runtime = await self._finish("A", "A1 — A2", 4)
        self.assertEqual(runtime["state"], "NEXT_MATCH_ALLOWED")
        self.assertEqual(runtime["observations"][-1]["classification"], "LONG")
        self.assertEqual(runtime["observations"][-1]["series_length"], 4)
        self.assertEqual(
            await self.gate.claim_match("B", "B1 — B2"),
            LongSeriesDecision.ALLOW,
        )
        self.assertEqual(
            await self.gate.claim_match("C", "C1 — C2"),
            LongSeriesDecision.DUPLICATE,
        )
        runtime = await self.gate.finish_allowed_match("B")
        self.assertEqual(runtime["state"], "WAITING_FOR_LONG")

    async def test_step_six_is_long_and_unlocks_next_match(self):
        runtime = await self._finish("A", "A1 — A2", 6)
        self.assertTrue(runtime["last_observed_is_long"])
        self.assertEqual(runtime["last_observed_series_length"], 6)
        self.assertEqual(
            await self.gate.claim_match("B", "B1 — B2"),
            LongSeriesDecision.ALLOW,
        )

    async def test_three_settled_losses_make_long_sticky_before_win(self):
        self.assertEqual(
            await self.gate.claim_match("A", "A1 — A2"),
            LongSeriesDecision.SHADOW,
        )
        await self.gate.start_observation(
            match_id="A",
            match_name="A1 — A2",
            selected_team="A1",
            selected_side="TEAM_1",
            initial_odds=2.05,
        )
        runtime = None
        for step in range(1, 4):
            await self.gate.begin_shadow_step(
                match_id="A",
                step=step,
                current_odds=2.05,
                score_before=f"0:{step - 1}",
            )
            runtime = await self.gate.record_shadow_step(
                match_id="A",
                match_name="A1 — A2",
                step=step,
                result="LOSE",
                current_odds=2.05,
                score_before=f"0:{step - 1}",
                score_after=f"0:{step}",
                scorer="A2",
            )

        self.assertIsNotNone(runtime)
        observation = runtime["active_observation"]
        self.assertTrue(observation["long_detected"])
        self.assertEqual(observation["long_detected_at_step"], 3)
        self.assertEqual(observation["classification"], "LONG")
        self.assertEqual(observation["completion_status"], "ACTIVE")

        runtime = await self.gate.begin_shadow_step(
            match_id="A",
            step=4,
            current_odds=1.98,
            score_before="0:3",
        )
        self.assertEqual(runtime["active_observation"]["classification"], "LONG")
        self.assertTrue(runtime["active_observation"]["long_detected"])

    async def test_interrupted_after_six_observed_steps_stays_long_and_unlocks_next_match(self):
        self.assertEqual(
            await self.gate.claim_match("A", "A1 — A2"),
            LongSeriesDecision.SHADOW,
        )
        await self.gate.start_observation(
            match_id="A",
            match_name="A1 — A2",
            selected_team="A1",
            selected_side="TEAM_1",
            initial_odds=2.05,
        )
        for step in range(1, 6):
            await self.gate.begin_shadow_step(
                match_id="A",
                step=step,
                current_odds=2.05,
                score_before=f"0:{step - 1}",
            )
            await self.gate.record_shadow_step(
                match_id="A",
                match_name="A1 — A2",
                step=step,
                result="LOSE",
                current_odds=2.05,
                score_before=f"0:{step - 1}",
                score_after=f"0:{step}",
                scorer="A2",
            )

        await self.gate.begin_shadow_step(
            match_id="A",
            step=6,
            current_odds=1.95,
            score_before="0:5",
        )
        runtime = await self.gate.finish_shadow_without_win(
            match_id="A",
            match_name="A1 — A2",
            steps=6,
        )

        self.assertEqual(runtime["state"], "NEXT_MATCH_ALLOWED")
        self.assertTrue(runtime["last_observed_is_long"])
        self.assertEqual(runtime["last_observed_series_length"], 6)
        self.assertEqual(runtime["observations"][-1]["classification"], "LONG")
        self.assertEqual(
            runtime["observations"][-1]["completion_status"], "INTERRUPTED"
        )
        self.assertEqual(
            await self.gate.claim_match("B", "B1 — B2"),
            LongSeriesDecision.ALLOW,
        )

    async def test_interrupted_before_three_losses_does_not_unlock(self):
        self.assertEqual(
            await self.gate.claim_match("A", "A1 — A2"),
            LongSeriesDecision.SHADOW,
        )
        await self.gate.start_observation(
            match_id="A",
            match_name="A1 — A2",
            selected_team="A1",
            selected_side="TEAM_1",
            initial_odds=2.05,
        )
        for step in range(1, 3):
            await self.gate.begin_shadow_step(
                match_id="A",
                step=step,
                current_odds=2.05,
                score_before=f"0:{step - 1}",
            )
            await self.gate.record_shadow_step(
                match_id="A",
                match_name="A1 — A2",
                step=step,
                result="LOSE",
                current_odds=2.05,
                score_before=f"0:{step - 1}",
                score_after=f"0:{step}",
                scorer="A2",
            )

        runtime = await self.gate.finish_shadow_without_win(
            match_id="A",
            match_name="A1 — A2",
            steps=2,
        )
        self.assertEqual(runtime["state"], "WAITING_FOR_LONG")
        self.assertIsNone(runtime["last_observed_is_long"])
        self.assertEqual(
            runtime["observations"][-1]["classification"], "INTERRUPTED"
        )

    async def test_parallel_candidates_consume_only_one_permission(self):
        await self._finish("A", "A1 — A2", 4)
        decisions = await asyncio.gather(
            self.gate.claim_match("B", "B1 — B2"),
            self.gate.claim_match("C", "C1 — C2"),
        )
        self.assertEqual(decisions.count(LongSeriesDecision.ALLOW), 1)
        self.assertEqual(decisions.count(LongSeriesDecision.DUPLICATE), 1)

    async def test_disabled_filter_is_a_noop(self):
        gate = LongSeriesGate()
        runtime = await gate.load(enabled=False, mode="LIVE")
        self.assertEqual(runtime["state"], "OFF")
        self.assertEqual(
            await gate.claim_match("A", "A1 — A2"),
            LongSeriesDecision.BYPASS,
        )

    async def test_restart_preserves_proven_long_from_active_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            gate = LongSeriesGate(repository.save_long_series_runtime)
            await gate.load(enabled=True, mode="LIVE")
            self.assertEqual(
                await gate.claim_match("A", "A1 — A2"),
                LongSeriesDecision.SHADOW,
            )
            await gate.start_observation(
                match_id="A",
                match_name="A1 — A2",
                selected_team="A1",
                selected_side="TEAM_1",
                initial_odds=2.05,
            )
            for step in range(1, 4):
                await gate.begin_shadow_step(
                    match_id="A",
                    step=step,
                    current_odds=2.05,
                    score_before=f"0:{step - 1}",
                )
                await gate.record_shadow_step(
                    match_id="A",
                    match_name="A1 — A2",
                    step=step,
                    result="LOSE",
                    current_odds=2.05,
                    score_before=f"0:{step - 1}",
                    score_after=f"0:{step}",
                    scorer="A2",
                )

            restarted = LongSeriesGate(repository.save_long_series_runtime)
            runtime = await restarted.load(
                enabled=True,
                mode="LIVE",
                persisted=await repository.get_long_series_runtime("LIVE"),
            )

        self.assertEqual(runtime["state"], "NEXT_MATCH_ALLOWED")
        self.assertTrue(runtime["last_observed_is_long"])
        self.assertEqual(runtime["observations"][-1]["classification"], "LONG")
        self.assertEqual(
            runtime["observations"][-1]["completion_status"], "INTERRUPTED"
        )

    async def test_runtime_persists_unlock_but_not_consumed_active_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            gate = LongSeriesGate(repository.save_long_series_runtime)
            await gate.load(enabled=True, mode="LIVE")
            self.gate = gate
            await self._finish("A", "A1 — A2", 4)

            restored = LongSeriesGate(repository.save_long_series_runtime)
            runtime = await restored.load(
                enabled=True,
                mode="LIVE",
                persisted=await repository.get_long_series_runtime("LIVE"),
            )
            self.assertEqual(runtime["state"], "NEXT_MATCH_ALLOWED")
            self.assertEqual(
                await restored.claim_match("B", "B1 — B2"),
                LongSeriesDecision.ALLOW,
            )

            restarted = LongSeriesGate(repository.save_long_series_runtime)
            runtime = await restarted.load(
                enabled=True,
                mode="LIVE",
                persisted=await repository.get_long_series_runtime("LIVE"),
            )
            self.assertEqual(runtime["state"], "WAITING_FOR_LONG")
            self.assertFalse(runtime["next_match_after_long_allowed"])


class LongSeriesEngineTests(unittest.IsolatedAsyncioTestCase):
    async def _run_shadow_series(self, winning_step: int):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            manager = AsyncMock()
            manager.ensure_page.return_value = object()
            manager.set_logger = lambda _logger: None
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {"long_series_enabled": True, "max_steps": 7}
                )
                await engine._long_series.load(enabled=True, mode="DEMO")
                await engine._long_series.claim_match("A", "Alpha — Beta")
                await engine._long_series.start_observation(
                    match_id="A",
                    match_name="Alpha — Beta",
                    selected_team="Alpha",
                    selected_side="TEAM_1",
                    initial_odds=2.1,
                )
                snapshots = [
                    ScoreboardSnapshot(
                        "Alpha", "Beta", Score(0, 0), "00:10", "1-й тайм"
                    )
                ]
                for loss_number in range(1, winning_step):
                    snapshots.append(
                        ScoreboardSnapshot(
                            "Alpha",
                            "Beta",
                            Score(0, loss_number),
                            f"0{loss_number}:00",
                            "1-й тайм",
                        )
                    )
                snapshots.append(
                    ScoreboardSnapshot(
                        "Alpha",
                        "Beta",
                        Score(1, winning_step - 1),
                        "09:00",
                        "1-й тайм",
                    )
                )
                engine._wait_for_goal = AsyncMock(
                    side_effect=[
                        *[(snapshots[index], Scorer.TEAM_2) for index in range(1, winning_step)],
                        (snapshots[-1], Scorer.TEAM_1),
                    ]
                )
                engine._wait_for_odds = AsyncMock(
                    side_effect=[
                        (
                            snapshots[index],
                            NextGoalOdds(
                                2.0,
                                1.8,
                                next_goal_number=index + 1,
                            ),
                        )
                        for index in range(1, winning_step)
                    ]
                )
                engine._prepare_live_until_placed = AsyncMock()
                engine._prepare_demo_virtual_until_ready = AsyncMock()
                budget_before = await repository.get_budget()
                with patch("backend.app.demo.engine.STATE.update", AsyncMock()):
                    await engine._process_long_series_shadow_match(
                        selected_match={
                            "match_id": "A",
                            "team1": "Alpha",
                            "team2": "Beta",
                        },
                        match_name="Alpha — Beta",
                        snapshot=snapshots[0],
                        initial_odds=NextGoalOdds(2.1, 1.8, next_goal_number=1),
                        selection=TeamSelection(
                            "Alpha", Scorer.TEAM_1, 2.1, "Beta", 1.8
                        ),
                    )
                return (
                    await engine._long_series.snapshot(),
                    await repository.history(mode="DEMO"),
                    budget_before,
                    await repository.get_budget(),
                    engine,
                )

    async def test_shadow_short_is_only_in_observation_history(self):
        runtime, history, before, after, engine = await self._run_shadow_series(2)
        self.assertEqual(history, [])
        self.assertEqual(after, before)
        self.assertEqual(runtime["observations"][-1]["classification"], "SHORT")
        self.assertEqual(runtime["observations"][-1]["series_length"], 2)
        engine._prepare_demo_virtual_until_ready.assert_not_awaited()

    async def test_shadow_step_six_does_not_touch_bets_balance_or_pnl(self):
        runtime, history, before, after, engine = await self._run_shadow_series(6)
        self.assertEqual(history, [])
        self.assertEqual(after, before)
        self.assertEqual(runtime["state"], "NEXT_MATCH_ALLOWED")
        self.assertEqual(runtime["observations"][-1]["classification"], "LONG")
        self.assertEqual(runtime["observations"][-1]["series_length"], 6)
        engine._prepare_demo_virtual_until_ready.assert_not_awaited()

    async def test_allowed_match_uses_real_steps_and_returns_to_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            manager = AsyncMock()
            manager.ensure_page.return_value = object()
            manager.set_logger = lambda _logger: None
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", OneMatchLeagueBrowser),
                patch("backend.app.demo.engine.STATE.update", AsyncMock()),
            ):
                engine = DemoEngine(manager)
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {
                        "long_series_enabled": True,
                        "min_initial_odds_enabled": False,
                        "max_steps": 2,
                        "stakes": [20, 44],
                    }
                )
                await engine._long_series.load(enabled=True, mode="DEMO")
                await engine._long_series.claim_match("A", "Alpha — Beta")
                await engine._long_series.start_observation(
                    match_id="A",
                    match_name="Alpha — Beta",
                    selected_team="Alpha",
                    selected_side="TEAM_1",
                    initial_odds=2.1,
                )
                for step in range(1, 4):
                    await engine._long_series.record_shadow_step(
                        match_id="A",
                        match_name="Alpha — Beta",
                        step=step,
                        result="LOSE",
                    )
                await engine._long_series.record_shadow_step(
                    match_id="A",
                    match_name="Alpha — Beta",
                    step=4,
                    result="WIN",
                )

                zero = ScoreboardSnapshot(
                    "Gamma", "Delta", Score(0, 0), "00:10", "1-й тайм"
                )
                loss = ScoreboardSnapshot(
                    "Gamma", "Delta", Score(0, 1), "01:00", "1-й тайм"
                )
                win = ScoreboardSnapshot(
                    "Gamma", "Delta", Score(1, 1), "02:00", "1-й тайм"
                )
                first_odds = NextGoalOdds(2.1, 1.8, next_goal_number=1)
                second_odds = NextGoalOdds(2.0, 1.85, next_goal_number=2)
                engine._sleep_or_stop = AsyncMock()
                engine._wait_for_initial_zero_score = AsyncMock(return_value=zero)
                engine._wait_for_odds = AsyncMock(
                    side_effect=[(zero, first_odds), (loss, second_odds)]
                )
                engine._read_fresh_score = AsyncMock(
                    side_effect=[zero, loss, loss]
                )
                engine._wait_for_goal = AsyncMock(
                    side_effect=[(loss, Scorer.TEAM_2), (win, Scorer.TEAM_1)]
                )

                async def virtual_ready(**kwargs):
                    odds = kwargs["current_odds"]
                    return kwargs["snapshot"], odds, odds.team1, odds.team2

                engine._prepare_demo_virtual_until_ready = AsyncMock(
                    side_effect=virtual_ready
                )

                await engine._process_next_match(object())

                history = await repository.history(mode="DEMO")
                runtime = await engine._long_series.snapshot()

        self.assertEqual([item["result"] for item in history], ["LOSE", "WIN"])
        self.assertEqual([item["step"] for item in history], [1, 2])
        self.assertEqual([item["match_id"] for item in history], ["B", "B"])
        self.assertEqual(runtime["state"], "WAITING_FOR_LONG")
        self.assertEqual(len(runtime["observations"]), 1)
        self.assertEqual(runtime["observations"][0]["match_id"], "A")

    async def test_live_shadow_market_read_is_forced_read_only(self):
        page = object()
        manager = AsyncMock()
        manager.ensure_page.return_value = page
        manager.set_logger = lambda _logger: None
        engine = DemoEngine(manager)
        engine._mode = "LIVE"
        current = ScoreboardSnapshot(
            "Alpha", "Beta", Score(0, 0), "00:10", "1-й тайм"
        )
        market = NextGoalOdds(2.1, 1.8, next_goal_number=1)
        browser = AsyncMock()
        browser.snapshot.return_value = current
        reader = AsyncMock(return_value=market)

        with (
            patch("backend.app.demo.engine.MatchBrowser", return_value=browser),
            patch("backend.app.demo.engine.read_next_goal_odds", reader),
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()),
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            result = await engine._wait_for_odds(
                current,
                {"match_id": "A", "team1": "Alpha", "team2": "Beta"},
                read_only=True,
            )

        self.assertEqual(result, (current, market))
        self.assertTrue(reader.await_args.kwargs["read_only"])

    async def test_allowed_match_does_not_use_cross_match_max_three_switch(self):
        manager = AsyncMock()
        manager.set_logger = lambda _logger: None
        engine = DemoEngine(manager)
        engine._config = StrategyConfig.from_payload(
            {
                "long_series_enabled": True,
                "max_three_steps_enabled": True,
                "max_steps": 7,
            }
        )
        self.assertFalse(
            engine._max_three_switch_ready(
                result="LOSE",
                switch_already_used=False,
                accepted_losses_in_current_match=3,
                step=3,
            )
        )

    async def test_live_shadow_observation_never_enters_bet_placement(self):
        manager = AsyncMock()
        manager.ensure_page.return_value = object()
        manager.set_logger = lambda _logger: None
        engine = DemoEngine(manager)
        engine._mode = "LIVE"
        engine._config = StrategyConfig.from_payload(
            {"long_series_enabled": True, "max_steps": 4}
        )
        engine._long_series = LongSeriesGate()
        await engine._long_series.load(enabled=True, mode="LIVE")
        await engine._long_series.claim_match("A", "Alpha — Beta")
        await engine._long_series.start_observation(
            match_id="A",
            match_name="Alpha — Beta",
            selected_team="Alpha",
            selected_side="TEAM_1",
            initial_odds=2.1,
        )
        engine._prepare_live_until_placed = AsyncMock()
        engine._prepare_demo_virtual_until_ready = AsyncMock()
        engine._wait_for_goal = AsyncMock(
            return_value=(
                ScoreboardSnapshot("Alpha", "Beta", Score(1, 0), "01:00", "1-й тайм"),
                Scorer.TEAM_1,
            )
        )
        snapshot = ScoreboardSnapshot(
            "Alpha", "Beta", Score(0, 0), "00:10", "1-й тайм"
        )
        selection = TeamSelection("Alpha", Scorer.TEAM_1, 2.1, "Beta", 1.8)

        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()),
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            await engine._process_long_series_shadow_match(
                selected_match={"match_id": "A", "team1": "Alpha", "team2": "Beta"},
                match_name="Alpha — Beta",
                snapshot=snapshot,
                initial_odds=NextGoalOdds(2.1, 1.8, next_goal_number=1),
                selection=selection,
            )

        engine._prepare_live_until_placed.assert_not_awaited()
        engine._prepare_demo_virtual_until_ready.assert_not_awaited()
        runtime = await engine._long_series.snapshot()
        self.assertEqual(runtime["last_observed_series_length"], 1)
        self.assertEqual(runtime["state"], "WAITING_FOR_LONG")
        self.assertEqual(runtime["observations"][-1]["classification"], "SHORT")


if __name__ == "__main__":
    unittest.main()
