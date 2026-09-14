import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.browser.first_half import (
    SCOREBOARD_TIMER_STATUS_SELECTOR,
    first_half_end_signal,
    first_half_timer_flag,
)
from backend.app.browser.market import (
    MARKET_BUTTON_SELECTOR,
    MARKET_GROUP_SELECTOR,
    MARKET_GROUP_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
    MARKET_VALUE_SELECTOR,
    NEXT_GOAL_SEARCH_SELECTOR,
    read_first_half_draw_market,
)
from backend.app.demo.engine import DemoEngine
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import FirstHalfDrawMarket, Score, ScoreboardSnapshot
from backend.app.live.models import LiveStatus, PlacementObservation
from backend.app.demo.strategy import StrategyConfig, StrategyType
from backend.app.demo.strategies.first_half_draw import (
    FirstHalfPhase,
    classify_first_half_phase,
    is_first_half_finished,
    settle_first_half_draw,
)


class FakeLocatorList:
    def __init__(self, items):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0]

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeNode:
    def __init__(self, text="", *, children=None, classes="", disabled=False):
        self.text = text
        self.children = children or {}
        self.classes = classes
        self.disabled = disabled
        self.filled = None

    async def wait_for(self, **_kwargs):
        return None

    async def count(self):
        return 1

    async def fill(self, value):
        self.filled = value

    async def is_visible(self):
        return True

    def locator(self, selector):
        return FakeLocatorList(self.children.get(selector, []))

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.classes if name == "class" else None

    async def is_disabled(self):
        return self.disabled


def market_button(selection, odds):
    return FakeNode(
        children={
            MARKET_NAME_SELECTOR: [FakeNode(selection)],
            MARKET_VALUE_SELECTOR: [FakeNode(odds)],
        }
    )


def market_group(title, buttons):
    return FakeNode(
        children={
            MARKET_GROUP_TITLE_SELECTOR: [FakeNode(title)],
            MARKET_BUTTON_SELECTOR: buttons,
        }
    )


class FakeMarketPage:
    def __init__(self, groups):
        self.search = FakeNode()
        self.search_requested = False
        self.groups = groups

    def locator(self, selector):
        if selector == NEXT_GOAL_SEARCH_SELECTOR:
            self.search_requested = True
            return FakeLocatorList([self.search])
        if selector == MARKET_GROUP_SELECTOR:
            return FakeLocatorList(self.groups)
        raise AssertionError(f"Unexpected selector: {selector}")


class FakeStatusPage:
    def __init__(self, status_text):
        self.status_text = status_text

    def locator(self, selector):
        if selector == SCOREBOARD_TIMER_STATUS_SELECTOR:
            return FakeLocatorList([FakeNode(self.status_text)])
        return FakeLocatorList([])


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class FakeLeagueBrowser:
    last_scan_stats = {"total": 1, "started": 0, "excluded": 0, "upcoming": 1}

    def __init__(self, _page, *, exclude_teams_enabled=True):
        self.exclude_teams_enabled = exclude_teams_enabled

    async def open(self):
        return None

    async def scan(self):
        return [
            {
                "match_id": "match-1",
                "href": "/match-1",
                "url": "https://example.test/match-1",
                "team1": "A",
                "team2": "B",
                "time": "00:30",
                "period": "",
            }
        ]

    async def open_match(self, selected):
        return {"url": selected["url"]}


def snapshot(score1, score2, period="1-й тайм", timer="01:00"):
    return ScoreboardSnapshot("A", "B", Score(score1, score2), timer, period)


class FirstHalfDrawRuleTests(unittest.TestCase):
    def test_strategy_type_is_supported(self):
        config = StrategyConfig.from_payload({"strategy_type": "FIRST_HALF_DRAW"})
        self.assertEqual(config.strategy_type, StrategyType.FIRST_HALF_DRAW)

    def test_score_never_finishes_the_period_by_itself(self):
        self.assertFalse(is_first_half_finished(snapshot(0, 1, period="")))
        self.assertFalse(is_first_half_finished(snapshot(1, 1)))

    def test_exact_three_minutes_finishes_first_half(self):
        self.assertTrue(
            is_first_half_finished(
                snapshot(3, 3, period="1-й тайм", timer="03:00")
            )
        )
        self.assertFalse(
            is_first_half_finished(
                snapshot(3, 3, period="1-й тайм", timer="02:59")
            )
        )

    def test_explicit_period_transitions_finish_first_half(self):
        self.assertEqual(
            classify_first_half_phase("1-й тайм 08:12"), FirstHalfPhase.FIRST_HALF
        )
        for signal in ("Перерыв", "2-й тайм", "First half finished"):
            with self.subTest(signal=signal):
                self.assertEqual(
                    classify_first_half_phase(signal), FirstHalfPhase.FINISHED
                )

    def test_settlement_uses_only_final_first_half_score(self):
        for values, result in {
            (0, 0): "WIN",
            (1, 1): "WIN",
            (2, 2): "WIN",
            (3, 3): "WIN",
            (0, 1): "LOSE",
            (2, 1): "LOSE",
        }.items():
            with self.subTest(score=values):
                self.assertEqual(settle_first_half_draw(Score(*values)), result)


class FirstHalfScoreboardFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_scoreboard_timer_marks_first_half_finished(self):
        page = FakeStatusPage("1-й тайм, 03:00")

        phase, text = await first_half_timer_flag(page)
        finished, evidence = await first_half_end_signal(
            page, snapshot(3, 3, period="1-й тайм", timer="03:00")
        )

        self.assertEqual(phase, FirstHalfPhase.FINISHED)
        self.assertEqual(text, "1-й тайм, 03:00")
        self.assertTrue(finished)
        self.assertIn("scoreboard-timer=1-й тайм, 03:00", evidence)

    async def test_scoreboard_timer_before_three_minutes_is_still_running(self):
        page = FakeStatusPage("1-й тайм, 02:59")

        phase, text = await first_half_timer_flag(page)
        finished, evidence = await first_half_end_signal(
            page, snapshot(3, 3, period="1-й тайм", timer="02:59")
        )

        self.assertEqual(phase, FirstHalfPhase.FIRST_HALF)
        self.assertEqual(text, "1-й тайм, 02:59")
        self.assertFalse(finished)
        self.assertIn("scoreboard-timer=1-й тайм, 02:59", evidence)

    async def test_scoreboard_timer_transition_marks_first_half_finished(self):
        page = FakeStatusPage("2-й тайм, 00:01")

        phase, text = await first_half_timer_flag(page)
        finished, evidence = await first_half_end_signal(
            page, snapshot(3, 3, period="", timer="00:01")
        )

        self.assertEqual(phase, FirstHalfPhase.FINISHED)
        self.assertEqual(text, "2-й тайм, 00:01")
        self.assertTrue(finished)
        self.assertIn("scoreboard-timer=2-й тайм, 00:01", evidence)


class FirstHalfDrawMarketTests(unittest.IsolatedAsyncioTestCase):
    async def test_draw_is_selected_by_text_not_array_index_or_search_input(self):
        wrong_group = market_group("1X2", [market_button("Ничья", "9.99")])
        target_group = market_group(
            "1X2. 1-й тайм",
            [
                market_button("П2", "2.25"),
                market_button("П1", "2.232"),
                market_button("Ничья", "5.33"),
            ],
        )
        page = FakeMarketPage([wrong_group, target_group])

        result = await read_first_half_draw_market(page)

        self.assertFalse(page.search_requested)
        self.assertIsNone(page.search.filled)
        self.assertEqual(result.market, "1X2. 1-й тайм")
        self.assertEqual(result.selection, "Ничья")
        self.assertEqual(result.odds, 5.33)
        self.assertIs(result.locator, target_group.children[MARKET_BUTTON_SELECTOR][2])


class FirstHalfDrawLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _run_result(self, final_score, mode="DEMO"):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", FakeLeagueBrowser),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = mode
                engine._config = StrategyConfig.from_payload(
                    {
                        "strategy_type": "FIRST_HALF_DRAW",
                        "initial_stake": 10,
                        "progression_multiplier": 2.5,
                        "max_steps": 3,
                        "stakes": [10, 25, 60],
                    }
                )
                initial = snapshot(0, 0, period="", timer="00:30")
                final = snapshot(*final_score, period="Перерыв", timer="")
                engine._open_first_half_subgame = AsyncMock(return_value=True)
                engine._wait_for_initial_zero_score = AsyncMock(return_value=initial)
                engine._wait_for_first_half_draw_market = AsyncMock(
                    return_value=(initial, FirstHalfDrawMarket(odds=5.33), 1.5)
                )
                engine._wait_for_first_half_finish = AsyncMock(
                    return_value=(final, "Перерыв")
                )
                if mode == "LIVE":
                    async def place_live(**kwargs):
                        record = kwargs["record"]
                        record.update(
                            result="ACTIVE",
                            status="ACTIVE",
                            placement_confirmed_at="2026-09-14T00:00:00+00:00",
                        )
                        await repository.save_bet(record)
                        return record, kwargs["snapshot"]

                    engine._place_first_half_draw_live = AsyncMock(
                        side_effect=place_live
                    )

                await engine._process_next_match(object())

                return (
                    await repository.processed_match_ids(
                        "FIRST_HALF_DRAW", mode=mode, period="FIRST_HALF"
                    ),
                    await repository.history(mode=mode),
                    await repository.get_budget(),
                    await repository.get_sequence(),
                )

    async def test_loss_advances_to_existing_second_stake(self):
        processed, history, budget, sequence = await self._run_result((0, 1))

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["result"], "LOSE")
        self.assertEqual(history[0]["period"], "FIRST_HALF")
        self.assertEqual(history[0]["score_after"], "0:1")
        self.assertEqual(budget["current_budget"], 4132.0)
        self.assertEqual(sequence["current_step"], 2)
        self.assertEqual(sequence["status"], "WAITING_NEXT_MATCH")
        self.assertEqual(processed, {"match-1"})

    async def test_win_credits_real_odds_and_resets_to_step_one(self):
        _, history, budget, sequence = await self._run_result((1, 1))

        self.assertEqual(history[0]["result"], "WIN")
        self.assertEqual(budget["current_budget"], 4185.3)
        self.assertEqual(sequence["current_step"], 1)
        self.assertEqual(sequence["status"], "WAITING_FOR_MATCH")

    async def test_live_placement_reuses_shared_coupon_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {
                        "strategy_type": "FIRST_HALF_DRAW",
                        "initial_stake": 10,
                        "progression_multiplier": 2.5,
                        "max_steps": 2,
                        "stakes": [10, 25],
                    }
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.state = lambda _attempt_id: LiveStatus.ACTIVE
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    return_value=(PlacementObservation(True, "coupon accepted"), snapshot(0, 0))
                )
                engine._read_fresh_score = AsyncMock(return_value=snapshot(0, 0))
                record = {
                    "id": "live:first-half:attempt1",
                    "mode": "LIVE",
                    "strategy_type": "FIRST_HALF_DRAW",
                    "period": "FIRST_HALF",
                    "match_id": "match-1",
                    "step": 1,
                    "amount": 10.0,
                    "result": "ACTIVE",
                    "status": "PLACING_BET",
                    "created_at": "2026-09-14T00:00:00+00:00",
                }
                selected_match = {
                    "match_id": "match-1",
                    "url": "https://example.test/match-1",
                }
                locator = object()

                placed = await engine._place_first_half_draw_live(
                    selected_match=selected_match,
                    snapshot=snapshot(0, 0),
                    market=FirstHalfDrawMarket(odds=5.33, locator=locator),
                    record=record,
                )

                self.assertIsNotNone(placed)
                engine.live_executor.prepare.assert_awaited_once()
                decision = engine.live_executor.prepare.await_args.args[1]
                self.assertEqual(decision.team, "Ничья")
                self.assertIs(decision.coefficient_locator, locator)
                saved = (await repository.history(mode="LIVE"))[0]
                self.assertEqual(saved["result"], "ACTIVE")
                self.assertIsNotNone(saved["placement_confirmed_at"])

    async def test_live_result_updates_history_and_sequence_without_demo_budget(self):
        _, history, budget, sequence = await self._run_result((0, 1), mode="LIVE")

        self.assertEqual(history[0]["mode"], "LIVE")
        self.assertEqual(history[0]["result"], "LOSE")
        self.assertTrue(history[0]["settled"])
        self.assertIsNone(history[0]["budget_change"])
        self.assertEqual(budget["current_budget"], 4142.0)
        self.assertEqual(sequence["current_step"], 2)


if __name__ == "__main__":
    unittest.main()
