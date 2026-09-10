import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.browser.market import (
    MARKET_BUTTON_SELECTOR,
    MARKET_GROUP_SELECTOR,
    MARKET_GROUP_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
    MARKET_VALUE_SELECTOR,
    NEXT_GOAL_SEARCH_SELECTOR,
    MarketNotAvailable,
    read_total_even_market,
)
from backend.app.demo.engine import DemoEngine, ModeConflictError
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import Score, ScoreboardSnapshot, TotalEvenMarket
from backend.app.demo.strategy import StrategyConfig
from backend.app.demo.strategies.total_even import (
    is_match_finished,
    settle_total_even,
    total_goals,
    total_parity,
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

    def locator(self, selector):
        return FakeLocatorList(self.children.get(selector, []))

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.classes if name == "class" else None

    async def is_disabled(self):
        return self.disabled


def market_button(selection, odds, *, locked=False):
    return FakeNode(
        children={
            MARKET_NAME_SELECTOR: [FakeNode(selection)],
            MARKET_VALUE_SELECTOR: [FakeNode(odds)],
        },
        classes="market ui-market--locked" if locked else "market",
        disabled=locked,
    )


def market_group(title, buttons):
    return FakeNode(
        children={
            MARKET_GROUP_TITLE_SELECTOR: [FakeNode(title)],
            MARKET_BUTTON_SELECTOR: buttons,
        }
    )


class FakePage:
    def __init__(self, groups):
        self.search = FakeNode()
        self.groups = groups

    def locator(self, selector):
        if selector == NEXT_GOAL_SEARCH_SELECTOR:
            return FakeLocatorList([self.search])
        if selector == MARKET_GROUP_SELECTOR:
            return FakeLocatorList(self.groups)
        raise AssertionError(f"Unexpected selector: {selector}")


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class FakeLeagueBrowser:
    last_scan_stats = {"total": 1, "started": 0, "upcoming": 1}
    skipped_started = []

    def __init__(self, _page, *, exclude_teams_enabled=True):
        self.exclude_teams_enabled = exclude_teams_enabled

    async def open(self):
        return None

    async def scan(self):
        return [
            {
                "match_id": "match-1",
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


class TotalEvenRuleTests(unittest.TestCase):
    def test_parity_examples(self):
        expected = {
            (0, 0): "EVEN",
            (1, 0): "ODD",
            (1, 1): "EVEN",
            (3, 2): "ODD",
            (4, 2): "EVEN",
        }
        for values, parity in expected.items():
            with self.subTest(score=values):
                score = Score(*values)
                self.assertEqual(total_goals(score), sum(values))
                self.assertEqual(total_parity(score), parity)

    def test_settlement_examples(self):
        for values, result in {
            (5, 3): "WIN",
            (5, 4): "LOSE",
            (6, 4): "WIN",
            (6, 5): "LOSE",
        }.items():
            with self.subTest(score=values):
                self.assertEqual(settle_total_even(Score(*values)), result)

    def test_score_is_not_final_without_terminal_scoreboard_marker(self):
        self.assertFalse(is_match_finished(snapshot(5, 3)))
        self.assertTrue(is_match_finished(snapshot(5, 3, period="Матч завершен", timer="")))

class TotalEvenMarketTests(unittest.IsolatedAsyncioTestCase):
    async def test_parser_ignores_yes_from_other_market(self):
        wrong = market_group("Обе забьют", [market_button("Да", "9.99")])
        target = market_group(
            "Тотал чёт",
            [market_button("Нет", "1.80"), market_button("Да", "1.92")],
        )
        page = FakePage([wrong, target])

        result = await read_total_even_market(page)

        self.assertEqual(page.search.filled, "тотал чет")
        self.assertEqual(result.market, "Тотал чёт")
        self.assertEqual(result.selection, "Да")
        self.assertEqual(result.odds, 1.92)

    async def test_locked_market_is_not_treated_as_a_loss(self):
        page = FakePage(
            [market_group("Тотал чёт", [market_button("Да", "1.92", locked=True)])]
        )

        with self.assertRaises(MarketNotAvailable) as raised:
            await read_total_even_market(page)

        self.assertEqual(raised.exception.status, "MARKET_LOCKED")


class TotalEvenConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_strategy_selection_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_config({"strategy_type": "TOTAL_EVEN"})
            restored = DemoRepository(Path(directory))
            self.assertEqual((await restored.get_config())["strategy_type"], "TOTAL_EVEN")

    async def test_total_even_live_start_is_rejected_before_browser_start(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_config({"strategy_type": "TOTAL_EVEN"})
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                with self.assertRaisesRegex(ModeConflictError, "только в DEMO"):
                    await engine.start("LIVE")


class TotalEvenLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _run_result(self, final_score):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", FakeLeagueBrowser),
            ):
                engine = DemoEngine(FakeManager())
                engine._config = StrategyConfig.from_payload(
                    {
                        "strategy_type": "TOTAL_EVEN",
                        "initial_stake": 20,
                        "progression_multiplier": 2.2,
                        "max_steps": 2,
                        "stakes": [20, 44],
                    }
                )
                initial = snapshot(0, 0, period="", timer="00:30")
                final = snapshot(*final_score, period="Матч завершен", timer="")
                engine._sleep_or_stop = AsyncMock()
                engine._wait_for_initial_zero_score = AsyncMock(return_value=initial)
                engine._wait_for_total_even_market = AsyncMock(
                    return_value=(initial, TotalEvenMarket(odds=1.92), 1, 2.5, 4.0)
                )
                engine._wait_for_total_even_finish = AsyncMock(return_value=final)

                await engine._process_next_match(object())

                return (
                    await repository.history(mode="DEMO"),
                    await repository.get_budget(),
                    await repository.get_sequence(),
                )

    async def test_even_final_score_wins_and_resets_shared_sequence(self):
        history, budget, sequence = await self._run_result((5, 3))

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["strategy_type"], "TOTAL_EVEN")
        self.assertEqual(history[0]["market"], "Тотал чёт")
        self.assertEqual(history[0]["market_selection"], "Да")
        self.assertEqual(history[0]["result"], "WIN")
        self.assertEqual(history[0]["score_after"], "5:3")
        self.assertEqual(history[0]["market_locked_count"], 1)
        self.assertEqual(budget["current_budget"], 4160.4)
        self.assertEqual(sequence["current_step"], 1)
        self.assertEqual(sequence["status"], "WAITING_FOR_MATCH")

    async def test_odd_final_score_loses_and_moves_to_next_shared_step(self):
        history, budget, sequence = await self._run_result((5, 4))

        self.assertEqual(history[0]["result"], "LOSE")
        self.assertEqual(history[0]["total_parity"], "ODD")
        self.assertEqual(budget["current_budget"], 4122.0)
        self.assertEqual(sequence["current_step"], 2)
        self.assertEqual(sequence["status"], "WAITING_NEXT_MATCH")


if __name__ == "__main__":
    unittest.main()
