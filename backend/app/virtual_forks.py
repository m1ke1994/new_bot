"""Read-only FC25 late-match conditional hedge observer.

This module never clicks market outcomes, creates coupons or calls a LIVE executor.
Two team outcomes are NOT a true arbitrage when the bookmaker also offers
"no more goals"; the entire virtual stake can be lost in that scenario.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from auth import authorize
from backend.app.browser.canvas_2d_adapter import read_next_goal_odds
from backend.app.browser.league import LeagueBrowser
from backend.app.browser.scoreboard import read_scoreboard
from backend.app.browser.manager import BROWSER_MANAGER
from backend.app.demo.config import CONFIG
from backend.app.demo.engine import ENGINE

CENT = Decimal("0.01")
OPEN_SECOND = 300
CLOSE_SECOND = 330
MATCH_DURATION = 360
END_GRACE_SECONDS = 35
FINISHED_WORDS = ("заверш", "окончен", "finished", "full time", "game over")
DATA_FILE = CONFIG.data_dir / "virtual_forks.sqlite3"


def utc_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def cash(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_DOWN)


def clock_seconds(timer: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)\s*", str(timer or ""))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def finished(period: str) -> bool:
    value = str(period or "").casefold()
    return any(word in value for word in FINISHED_WORDS)


def split_virtual_stakes(total: Decimal, odd1: Any, odd2: Any) -> dict[str, Any] | None:
    """Same-event, two-side conditional hedge, NOT a 3-outcome surebet."""
    try:
        a, b = Decimal(str(odd1)), Decimal(str(odd2))
        amount = cash(total)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if a <= 1 or b <= 1 or amount < Decimal("0.02"):
        return None
    inv = Decimal(1) / a + Decimal(1) / b
    if inv >= 1:
        return None
    # Split the same total virtual amount by inverse odds; round only once.
    stake1 = cash(amount * (Decimal(1) / a) / inv)
    stake2 = amount - stake1
    if stake1 <= 0 or stake2 <= 0:
        return None
    payout1 = cash(stake1 * a)
    payout2 = cash(stake2 * b)
    profit1 = payout1 - amount
    profit2 = payout2 - amount
    if min(profit1, profit2) <= 0:
        return None
    return {
        "odd1": float(a), "odd2": float(b), "inverse_sum": float(inv),
        "stake1": float(stake1), "stake2": float(stake2),
        "total_stake": float(amount), "payout1": float(payout1),
        "payout2": float(payout2), "profit1": float(profit1),
        "profit2": float(profit2),
        "minimum_conditional_profit": float(min(profit1, profit2)),
        "no_goal_loss": float(-amount),
    }


class ForkStore:
    def __init__(self, db_path: Path = DATA_FILE) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS virtual_forks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    time_created TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS virtual_fork_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    time_created TEXT NOT NULL,
                    event TEXT NOT NULL,
                    message TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        """Commit/rollback on exit AND close the SQLite file on Windows.

        sqlite3.Connection.__exit__ only finalizes a transaction; it does
        not close the connection. An unclosed handle prevents TemporaryDirectory
        cleanup on Windows (WinError 32) and can keep database files locked.
        """
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def upsert(self, item: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO virtual_forks(id,session_id,match_id,time_created,status,payload) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,payload=excluded.payload",
                (item["id"], item["session_id"], item["match_id"],
                 item["created_at"], item["status"], json.dumps(item, ensure_ascii=False)),
            )

    def add_log(self, session: str, event: str, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO virtual_fork_logs(session_id,time_created,event,message) VALUES(?,?,?,?)",
                (session, utc_time(), event, message),
            )

    def history(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM virtual_forks ORDER BY time_created DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(item[0]) for item in rows]

    def logs(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT time_created,event,message FROM virtual_fork_logs "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{"timestamp": row[0], "event": row[1], "message": row[2]}
                for row in reversed(rows)]


class VirtualForkObserver:
    def __init__(self, store: ForkStore | None = None) -> None:
        self.store = store or ForkStore()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._session = ""
        self._balance = Decimal(0)
        self._initial = Decimal(0)
        self._allocation_pct = Decimal("10")
        self._position: dict[str, Any] | None = None
        self._state: dict[str, Any] = {
            "running": False, "phase": "STOPPED",
            "message": "Наблюдатель остановлен", "window": "05:00–05:30",
            "initial_budget": 0, "balance": 0, "allocated_percent": 10,
            "session_profit": 0, "match": None, "quote": None,
            "position": None, "scanned": 0, "signals": 0,
            "warnings": ["Третий исход «голов больше не будет» не покрыт."],
        }

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        state = deepcopy(self._state)
        state["running"] = self.running
        return state

    def _update(self, **changes: Any) -> None:
        self._state.update(changes, updated_at=utc_time(), running=self.running)

    async def _log(self, event: str, message: str) -> None:
        self.store.add_log(self._session, event, message)
        self._update(message=message)

    async def start(self, budget: Any, percent: Any = 10) -> dict[str, Any]:
        if self.running:
            raise ValueError("Виртуальный наблюдатель уже запущен")
        # Do not run two strategies in the same persistent browser at once.
        # DemoEngine.task is an @property, not a callable.
        engine_task = ENGINE.task
        if engine_task is not None and not engine_task.done():
            raise RuntimeError("Сначала остановите основную стратегию DEMO/LIVE")
        try:
            amount, share = Decimal(str(budget)), Decimal(str(percent))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Укажите числовой бюджет и процент") from error
        if not (Decimal("20") <= amount <= Decimal("1000000000")):
            raise ValueError("Бюджет должен быть от 20 до 1 000 000 000 ₽")
        if not (Decimal("1") <= share <= Decimal("100")):
            raise ValueError("Доля бюджета должна быть от 1% до 100%")
        self._session = uuid4().hex
        self._initial = cash(amount)
        self._balance = self._initial
        self._allocation_pct = share
        self._position = None
        self._stop_event = asyncio.Event()
        self._state.update({
            "phase": "STARTING", "message": "Запуск наблюдателя вилок",
            "session_id": self._session, "initial_budget": float(self._initial),
            "balance": float(self._balance), "allocated_percent": float(share),
            "session_profit": 0, "match": None, "quote": None,
            "position": None, "scanned": 0, "signals": 0,
        })
        self._task = asyncio.create_task(self._run(), name="fc25-virtual-forks")
        self._update(running=True)
        await self._log("START", "Запущен виртуальный мониторинг 05:00–05:30, без реальных ставок")
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._position and self._position.get("status") == "PENDING":
            await self._review("Сессия остановлена до определения результата")
        self._update(running=False, phase="STOPPED", message="Мониторинг остановлен")
        return self.snapshot()

    async def pause(self, seconds: float) -> None:
        if not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass

    async def _run(self) -> None:
        try:
            page = await BROWSER_MANAGER.ensure_page()
            while not self._stop_event.is_set():
                self._update(phase="AUTH_CHECK")
                auth = await authorize(page, self._stop_event)
                if self._stop_event.is_set():
                    break
                if not auth.get("ok") or auth.get("status") == "AUTH_TIMEOUT":
                    await self._log("AUTH", "Требуется ручной вход на сайте — ожидаем")
                    await self.pause(3)
                    continue
                break

            if self._stop_event.is_set():
                return
            # Dedicated tab, same cookies/profile, avoids accidentally using the
            # old strategy's page or its coupon.
            page = await BROWSER_MANAGER.create_aux_page()
            try:
                while not self._stop_event.is_set():
                    try:
                        await self._scan_one(page)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        await self._log("ERROR", f"Ошибка наблюдения: {type(error).__name__}: {error}")
                    await self.pause(2.0)
            finally:
                await BROWSER_MANAGER.close_aux_page(page)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._log("WORKER_ERROR", f"Ошибка запуска: {type(error).__name__}: {error}")
        finally:
            if self._position and self._position.get("status") == "PENDING":
                await self._review("Наблюдение прервано до подтверждения результата")
            self._update(running=False, phase="STOPPED")

    async def _scan_one(self, page: Any) -> None:
        self._update(phase="SEARCHING_MATCH", match=None, quote=None)
        league = LeagueBrowser(page, exclude_teams_enabled=False)
        await league.open()
        matches = await league.scan()
        if not matches:
            await self._log("NO_MATCHES", "Ближайшие матчи не определены — повторяем сканирование")
            return
        match = matches[0]
        match_id = str(match.get("match_id") or match.get("href") or "")
        if not match_id:
            await self._log("NO_MATCH_ID", "У карточки отсутствует ID")
            return
        self._update(phase="OPENING_MATCH", match={
            "id": match_id, "name": f'{match.get("team1")} — {match.get("team2")}',
            "timer": match.get("time"), "score": match.get("score"),
        }, scanned=int(self._state["scanned"]) + 1)
        await self._log("MATCH_SELECTED", f'Выбран ближайший матч: {self._state["match"]["name"]}')
        await league.open_match(match)
        await league.wait_match_content_ready(timeout_ms=12000)

        deadline = asyncio.get_running_loop().time() + 440
        while not self._stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                before = await read_scoreboard(page)
            except Exception:
                await self.pause(0.45)
                continue
            seconds = clock_seconds(before.timer)
            if self._state.get("match") is not None:
                self._state["match"].update(
                    timer=before.timer, score=before.score.text(), period=before.period,
                )
            if finished(before.period):
                return
            if seconds is None:
                self._update(phase="WAITING_TIMER", quote=None)
                await self.pause(0.5)
                continue
            if seconds < OPEN_SECOND:
                self._update(phase="WAITING_WINDOW", quote=None)
                await self.pause(min(0.6, max(0.15, (OPEN_SECOND - seconds) / 3)))
                continue
            if seconds > CLOSE_SECOND:
                await self._log("WINDOW_CLOSED", f"Окно 05:00–05:30 закрыто (таймер {before.timer})")
                return

            self._update(phase="READING_ODDS")
            try:
                odds = await read_next_goal_odds(
                    page, before.team1, before.team2,
                    before.score.team1, before.score.team2,
                    read_only=True,
                )
            except Exception as error:
                self._update(phase="WAITING_MARKET", quote=None)
                if seconds % 5 == 0:
                    await self._log("ODDS_RETRY", f"Ждём коэффициенты/разблокировку: {type(error).__name__}")
                await self.pause(0.35)
                continue
            blocked = bool(odds.locked_sides or odds.recent_locked_sides)
            inv = (1 / odds.team1 + 1 / odds.team2) if odds.team1 > 1 and odds.team2 > 1 else None
            self._update(quote={
                "team1": before.team1, "team2": before.team2,
                "odd1": odds.team1, "odd2": odds.team2,
                "goal_number": odds.next_goal_number,
                "inverse_sum": inv, "conditional_signal": inv is not None and inv < 1,
                "blocked": blocked, "source": odds.source,
                "timer": before.timer, "updated_at": utc_time(),
            })
            if blocked or inv is None or inv >= 1:
                self._update(phase="WATCHING_QUOTES")
                await self.pause(0.35)
                continue

            # Recheck the actual score and elapsed time after reading the quote.
            after = await read_scoreboard(page)
            after_second = clock_seconds(after.timer)
            if (after.team1, after.team2) != (before.team1, before.team2):
                await self._log("STALE_TEAMS", "Состав команд изменился во время чтения")
                return
            if after.score != before.score or after_second is None or not OPEN_SECOND <= after_second <= CLOSE_SECOND:
                await self._log("STALE_QUOTE", "Счёт/время изменились; сигнал пропущен")
                continue
            if odds.next_goal_number != before.score.team1 + before.score.team2 + 1:
                continue
            allocation = cash(self._balance * self._allocation_pct / 100)
            proposal = split_virtual_stakes(allocation, odds.team1, odds.team2)
            if proposal is None:
                self._update(phase="WATCHING_QUOTES")
                await self.pause(0.35)
                continue

            position = {
                **proposal, "id": uuid4().hex, "session_id": self._session,
                "created_at": utc_time(), "status": "PENDING",
                "match_id": match_id,
                "match": f"{before.team1} — {before.team2}",
                "team1": before.team1, "team2": before.team2,
                "score_before": before.score.text(), "score_after": None,
                "goal_number": odds.next_goal_number,
                "timer": after.timer, "quote_source": odds.source,
                "result": None, "profit": None, "payout": None,
                "conditional_only": True,
                "third_outcome_covered": False,
            }
            self._balance -= cash(proposal["total_stake"])
            self._position = position
            self.store.upsert(position)
            self._update(
                phase="WAITING_RESULT", position=position, balance=float(self._balance),
                signals=int(self._state["signals"]) + 1,
            )
            await self._log(
                "VIRTUAL_PAIR",
                f'Виртуальная пара: {before.team1} {proposal["stake1"]:.2f} ₽ @ {odds.team1}; '
                f'{before.team2} {proposal["stake2"]:.2f} ₽ @ {odds.team2}. '
                'Если голов больше не будет — обе ставки проиграют.',
            )
            await self._settle(page, before)
            return
        await self._log("MATCH_TIMEOUT", "Ожидание матча превысило лимит")

    async def _settle(self, page: Any, initial: Any) -> None:
        """Score delta is definitive only when exactly ONE goal was observed."""
        start = asyncio.get_running_loop().time()
        while not self._stop_event.is_set() and asyncio.get_running_loop().time() - start < 120:
            try:
                snap = await read_scoreboard(page)
            except Exception:
                await self.pause(0.4)
                continue
            if (snap.team1, snap.team2) != (initial.team1, initial.team2):
                await self._review("Команды поменялись во время наблюдения")
                return
            delta1 = snap.score.team1 - initial.score.team1
            delta2 = snap.score.team2 - initial.score.team2
            if delta1 < 0 or delta2 < 0 or delta1 + delta2 > 1:
                await self._review("Невозможно достоверно определить первый гол по изменению счёта")
                return
            if delta1 + delta2 == 1:
                await self._resolve("TEAM_1" if delta1 == 1 else "TEAM_2", snap.score.text())
                return
            if finished(snap.period):
                await self._resolve("NO_GOAL", snap.score.text())
                return
            # A bare timer 06:00 does NOT prove the match is over.
            elapsed = clock_seconds(snap.timer)
            if elapsed is not None and elapsed >= MATCH_DURATION + END_GRACE_SECONDS:
                await self._review("Конец матча не подтверждён статусом; исход «нет гола» неизвестен")
                return
            await self.pause(0.4)
        if self._position and self._position["status"] == "PENDING":
            await self._review("Не получен достоверный финальный исход за время ожидания")

    async def _resolve(self, outcome: str, score: str) -> None:
        if not self._position or self._position.get("status") != "PENDING":
            return
        p = self._position
        payout = cash(p["payout1"] if outcome == "TEAM_1" else p["payout2"] if outcome == "TEAM_2" else 0)
        pnl = payout - cash(p["total_stake"])
        self._balance += payout
        p.update(status="SETTLED", result=outcome, score_after=score,
                 payout=float(payout), profit=float(pnl), settled_at=utc_time())
        self.store.upsert(p)
        self._update(
            phase="SETTLED", position=deepcopy(p),
            balance=float(self._balance),
            session_profit=float(self._balance - self._initial),
        )
        await self._log(
            "SETTLED",
            f'{p["match"]}: {outcome} / {score}; результат {pnl:+.2f} ₽',
        )
        self._position = None

    async def _review(self, reason: str) -> None:
        if not self._position or self._position.get("status") != "PENDING":
            return
        p = self._position
        p.update(status="NEEDS_REVIEW", result="UNKNOWN", settled_at=utc_time(),
                 review_reason=reason)
        self.store.upsert(p)
        self._update(phase="NEEDS_REVIEW", position=deepcopy(p))
        await self._log("NEEDS_REVIEW", f'{p["match"]}: {reason}. Списанные виртуальные средства зарезервированы.')
        self._position = None


FORKS = VirtualForkObserver()
