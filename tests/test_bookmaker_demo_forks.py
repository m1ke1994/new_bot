import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.bookmaker_demo_forks import (
    BookmakerDemoPlacer, allocate_integer_pair, next_second_amount,
    require_bookmaker_demo,
)


class DemoForkRoundingTests(TestCase):
    def test_two_integer_stakes_cover_alternative_next_goal(self):
        result = allocate_integer_pair(500.99, "2.20", "2.10")
        self.assertIsNotNone(result)
        self.assertEqual(result["total_stake"], 500)
        self.assertEqual(result["stake1"] % 1, 0)
        self.assertEqual(result["stake2"] % 1, 0)
        self.assertEqual(result["stake1"] + result["stake2"], 500)
        self.assertGreater(result["minimum_conditional_profit"], 0)

    def test_rounding_does_not_fake_a_fork(self):
        self.assertIsNone(allocate_integer_pair(500, 1.85, 1.95))
        self.assertIsNone(allocate_integer_pair(19, 2.2, 2.2))
        self.assertIsNone(next_second_amount(250, 2.01, 1.10, 500))

    def test_second_is_repriced_in_integer_rubles(self):
        self.assertEqual(next_second_amount(250, 2.20, 2.15, 510), 256)


class BookmakerDemoSafetyTests(IsolatedAsyncioTestCase):
    def page(self, label: str):
        page = MagicMock()
        badge = page.locator.return_value.first
        badge.count = AsyncMock(return_value=1)
        badge.is_visible = AsyncMock(return_value=True)
        badge.inner_text = AsyncMock(return_value=label)
        badge.get_attribute = AsyncMock(return_value=None)
        return page

    async def test_rub_balance_uses_existing_authorization(self):
        page = self.page("RUB")
        with patch("auth.check_balance", new_callable=AsyncMock) as check:
            check.return_value = True
            self.assertEqual(await require_bookmaker_demo(page), "RUB")
            check.assert_awaited_once_with(page)

    async def test_rub_missing_blocks_all_coupons(self):
        page = self.page("")
        with patch("auth.check_balance", new_callable=AsyncMock) as check:
            check.return_value = False
            with self.assertRaisesRegex(RuntimeError, "RUB-баланс не найден"):
                await require_bookmaker_demo(page)

    async def test_accepted_pair_is_sequential_lower_odds_first(self):
        page = self.page("Демо-счёт")
        odds = SimpleNamespace(
            team1=2.20, team2=2.10, next_goal_number=5,
            locked_sides=(), recent_locked_sides=(),
            locator_for_side=lambda side: MagicMock(),
        )
        snap = SimpleNamespace(
            team1="А", team2="Б",
            score=SimpleNamespace(team1=2, team2=2, text=lambda: "2:2"),
            timer="05:10", period="2-й тайм",
        )
        proposed = allocate_integer_pair(500, 2.2, 2.1)
        position = {
            **proposed,
            "id":"p1", "match_id":"m1", "team1":"А", "team2":"Б",
            "goal_number":5, "score_before":"2:2",
            "status":"BOOKMAKER_DEMO_PREPARING",
        }
        log = AsyncMock()
        save = AsyncMock()
        worker = BookmakerDemoPlacer(log)
        worker.executor.prepare = AsyncMock()
        worker.executor.manual_click_seen = AsyncMock(return_value=True)
        worker.executor.wait_for_manual_confirmation = AsyncMock(
            return_value=SimpleNamespace(placed=True, signal="success modal")
        )
        worker.executor._read_balance = AsyncMock(return_value=Decimal("1000"))
        worker.executor._success_modal_is_visible = AsyncMock(return_value=False)
        with patch("auth.check_balance", new_callable=AsyncMock, return_value=True), \
             patch("backend.app.bookmaker_demo_forks.read_next_goal_odds",
                   new_callable=AsyncMock) as scan:
            scan.return_value = odds
            result = await worker.place(
                page, position, odds, asyncio.Event(), save,
                AsyncMock(return_value=snap), lambda snapshot: True,
            )
        self.assertTrue(result)
        self.assertEqual(position["placement_order"], [2,1])
        self.assertEqual(position["accepted_legs"], 2)
        self.assertEqual(
            [call.args[1].team for call in worker.executor.prepare.await_args_list],
            ["Б","А"],
        )
        self.assertEqual(position["team1_placement"], "ACCEPTED")
        self.assertEqual(position["team2_placement"], "ACCEPTED")
        self.assertEqual(position["team1_accepted_stake"] % 1, 0)
        self.assertEqual(position["team2_accepted_stake"] % 1, 0)
        self.assertGreater(position["minimum_conditional_profit"], 0)

    async def test_rejected_first_leg_stops_without_second_click(self):
        page = self.page("Демо-счёт")
        odds = SimpleNamespace(
            team1=2.10, team2=2.20, next_goal_number=5,
            locked_sides=(), recent_locked_sides=(),
            locator_for_side=lambda side: MagicMock(),
        )
        proposed = allocate_integer_pair(500, 2.1, 2.2)
        position = {
            **proposed, "id":"reject", "match_id":"m1",
            "team1":"А", "team2":"Б", "goal_number":5,
            "score_before":"2:2",
        }
        snap = SimpleNamespace(
            score=SimpleNamespace(text=lambda: "2:2"), timer="05:05",
            period="2-й тайм",
        )
        worker = BookmakerDemoPlacer(AsyncMock())
        worker.executor.prepare = AsyncMock()
        worker.executor.manual_click_seen = AsyncMock(return_value=False)
        worker.executor.clear_unaccepted_coupon = AsyncMock()
        worker.executor._read_balance = AsyncMock(return_value=Decimal("1000"))
        with patch("auth.check_balance", new_callable=AsyncMock, return_value=True):
            result = await worker.place(
                page, position, odds, asyncio.Event(), AsyncMock(),
                AsyncMock(return_value=snap), lambda snapshot: True,
            )
        self.assertFalse(result)
        self.assertEqual(worker.executor.prepare.await_count, 1)
        self.assertEqual(position["accepted_legs"], 0)
        self.assertEqual(position["status"], "BOOKMAKER_DEMO_REJECTED")
