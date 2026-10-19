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
from urllib.parse import urlparse
import re
import sqlite3
from typing import Any
from uuid import uuid4

from auth import authorize
from backend.app.browser.canvas_2d_adapter import read_next_goal_odds
from backend.app.browser.league import LeagueBrowser
from backend.app.browser.scoreboard import read_scoreboard, parse_timer_and_period
from backend.app.browser.manager import BROWSER_MANAGER
from backend.app.browser.navigation import NavigationLoadError, is_network_transport_error
from backend.app.demo.config import CONFIG
from backend.app.demo.engine import ENGINE
from backend.app.demo.models import ScoreboardSnapshot

CENT = Decimal("0.01")
OPEN_SECOND = 300
CLOSE_SECOND = 330
MATCH_DURATION = 360
END_GRACE_SECONDS = 35
FINISHED_WORDS = ("заверш", "окончен", "finished", "full time", "game over")
DATA_FILE = CONFIG.data_dir / "virtual_forks.sqlite3"

# The live timer may render OUTSIDE scoreboard-compact-view-tab-panel__body.
# Read only narrow scoreboard status/header selectors, never the full page
# (which may also contain unrelated match countdowns and market values).
LIVE_TIMER_SELECTORS = (
    '[data-test="betting-scoreboard-status-time"]',
    '[data-test="betting-scoreboard-status-time"] .ui-game-timer__label',
    '[data-test="betting-scoreboard-status-time"] .ui-game-timer__time',
    '.scoreboard-layout-head__footer .scoreboard-timer',
    '.scoreboard-layout-head__footer .ui-game-timer',
    '.scoreboard-layout-head__footer .scoreboard-live__status',
    '.scoreboard-compact-view-tab-panel__body .ui-game-timer',
    '.scoreboard-timer .ui-caption',
)
LIVE_CLOCK_PATTERN = re.compile(r"(?<!\d)(\d{1,3}):([0-5]\d)(?!\d)")
PRE_WINDOW_QUOTE_INTERVAL = 5.0
IN_WINDOW_QUOTE_INTERVAL = 0.35



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



def select_nearest_unstarted(
    matches: list[dict[str, Any]],
    excluded_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Only a true UPCOMING countdown qualifies, never an already live card.

    The normal league scanner already sorts upcoming matches, but this
    validates the input again to protect a virtual fork worker against
    reusing an active match accidentally.
    """
    excluded = excluded_ids or set()
    candidates: list[dict[str, Any]] = []
    for match in matches:
        match_id = str(match.get("match_id") or match.get("href") or "")
        timer = match.get("time_seconds")
        if (
            not match_id
            or match_id in excluded
            or match.get("finished")
            or not match.get("is_upcoming")
            or str(match.get("period") or "").strip()
            or not isinstance(timer, int)
            or timer <= 0
            or str(match.get("score") or "0:0").strip() != "0:0"
        ):
            continue
        candidates.append(match)
    return min(candidates, key=lambda item: item["time_seconds"], default=None)


def live_period(period: str) -> bool:
    value = str(period or "").strip().casefold()
    if not value or finished(value):
        return False
    return not any(word in value for word in (
        "не нач", "not started", "upcoming", "до начала", "ожидани",
        "starts in", "prematch",
    ))


class MatchStartGate:
    """Prevent prematch COUNTDOWN from being interpreted as LIVE elapsed time."""

    def __init__(self) -> None:
        self.started = False
        self._previous_seconds: int | None = None
        self._observed_zero = False

    def observe(self, scoreboard: Any) -> bool:
        seconds = clock_seconds(scoreboard.timer)
        if finished(scoreboard.period):
            return self.started
        if live_period(scoreboard.period):
            self.started = True
        elif scoreboard.score.team1 + scoreboard.score.team2 > 0:
            self.started = True
        elif (
            self._observed_zero
            and seconds is not None
            and self._previous_seconds is not None
            and seconds > self._previous_seconds
        ):
            # An empty-period scoreboard may change from prematch 00:00 to
            # elapsed 00:01 before the period label is painted.
            self.started = True
        if seconds == 0:
            self._observed_zero = True
        if seconds is not None:
            self._previous_seconds = seconds
        return self.started


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
        self._completed_match_ids: set[str] = set()
        self._state: dict[str, Any] = {
            "running": False, "phase": "STOPPED",
            "message": "Наблюдатель остановлен", "window": "05:00–05:30",
            "initial_budget": 0, "balance": 0, "allocated_percent": 10,
            "session_profit": 0, "match": None, "quote": None,
            "odds_error": None, "odds_status": "WAITING",
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
        self._completed_match_ids = set()
        self._stop_event = asyncio.Event()
        self._state.update({
            "phase": "STARTING", "message": "Запуск наблюдателя вилок",
            "session_id": self._session, "initial_budget": float(self._initial),
            "balance": float(self._balance), "allocated_percent": float(share),
            "session_profit": 0, "match": None, "quote": None,
            "odds_error": None, "odds_status": "WAITING",
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

    @staticmethod
    def _is_closed_page_error(error: Exception, page: Any | None) -> bool:
        """Detect a disposed tab/context without mistaking a network reset for one."""
        if page is not None:
            try:
                if page.is_closed():
                    return True
            except Exception:
                return True
        message = str(error).casefold()
        return any(part in message for part in (
            "targetclosederror",
            "target page, context or browser has been closed",
            "target page has been closed",
            "вкладка chromium закрыта",
            "browser has been closed",
            "context has been closed",
        ))

    async def _run(self) -> None:
        """Own only the manager's recoverable main page, never a stale auxiliary tab.

        The existing DEMO/LIVE task cannot run simultaneously (start guard).
        After the user closes Chromium, BrowserManager.ensure_page() can recreate
        the page/context and authorize() will be run again on that generation.
        """
        authorized_page = None
        authorized_generation = None
        try:
            while not self._stop_event.is_set():
                page = None
                try:
                    page = await BROWSER_MANAGER.ensure_page()
                    if page.is_closed():
                        raise RuntimeError("Вкладка Chromium закрыта до начала наблюдения")
                    generation = BROWSER_MANAGER.generation
                    if page is not authorized_page or generation != authorized_generation:
                        self._update(phase="AUTH_CHECK", match=None, quote=None)
                        auth = await authorize(page, self._stop_event)
                        if self._stop_event.is_set():
                            break
                        if not auth.get("ok"):
                            if self._state.get("phase") != "WAITING_AUTH":
                                await self._log(
                                    "AUTH_WAITING",
                                    "Ожидается ручная авторизация в восстановленном браузере",
                                )
                            self._update(phase="WAITING_AUTH")
                            await self.pause(3)
                            continue
                        authorized_page = page
                        authorized_generation = generation
                        await self._log(
                            "BROWSER_READY",
                            "Авторизация проверена; используем управляемую вкладку без дублирования",
                        )

                    await self._scan_one(page)
                    # Do not navigate to the same heavy SPA page every 2 seconds.
                    await self.pause(5.0)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if self._stop_event.is_set():
                        break
                    if self._is_closed_page_error(error, page):
                        authorized_page = None
                        authorized_generation = None
                        self._update(phase="RECOVERING_BROWSER", match=None, quote=None)
                        await self._log(
                            "BROWSER_PAGE_LOST",
                            "Вкладка или контекст Chromium закрыты. Ожидаем и "
                            "восстанавливаем через BrowserManager; профиль и cookies не удаляются. "
                            f"Причина: {type(error).__name__}: {error}",
                        )
                        await self.pause(3.0)
                        continue
                    if isinstance(error, NavigationLoadError):
                        authorized_page = None
                        authorized_generation = None
                        self._update(phase="RECOVERING_BROWSER", quote=None)
                        await self._log(
                            "NAVIGATION_RECOVERY",
                            f"Навигация не завершилась: {type(error).__name__}: {error}",
                        )
                        if is_network_transport_error(error):
                            await self.pause(15.0)
                        else:
                            try:
                                await BROWSER_MANAGER.recover_navigation(error)
                            except Exception as recovery_error:
                                await self._log(
                                    "RECOVERY_RETRY",
                                    f"Восстановление не завершено: {type(recovery_error).__name__}",
                                )
                            await self.pause(3.0)
                        continue
                    self._update(phase="ERROR")
                    await self._log(
                        "SCAN_ERROR",
                        f"Ошибка чтения матчей: {type(error).__name__}: {error}",
                    )
                    await self.pause(5.0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._log("WORKER_ERROR", f"Ошибка запуска: {type(error).__name__}: {error}")
        finally:
            if self._position and self._position.get("status") == "PENDING":
                await self._review("Наблюдение прервано до подтверждения результата")
            self._update(running=False, phase="STOPPED")

    async def _read_live_scoreboard(self, page: Any) -> ScoreboardSnapshot:
        """Read scoreboard, then recover a missing LIVE timer from trusted DOM.

        Score and team identity still come exclusively from the original
        scoreboard reader. Never interpolate a guessed clock or use the
        clock of another game as a substitute.
        """
        snapshot = await read_scoreboard(page)
        if clock_seconds(snapshot.timer) is not None:
            return snapshot

        found_text = ""
        found_selector = ""
        for selector in LIVE_TIMER_SELECTORS:
            try:
                locator = page.locator(selector).first
                if not await locator.count() or not await locator.is_visible():
                    continue
                raw = (await locator.inner_text()).strip()
            except Exception:
                continue
            timer_match = LIVE_CLOCK_PATTERN.search(raw)
            if timer_match is None:
                continue
            parsed_timer, parsed_period = parse_timer_and_period(raw)
            if clock_seconds(parsed_timer) is None:
                continue
            found_text, found_selector = parsed_timer, selector
            period = snapshot.period or parsed_period
            if self._state.get("match") is not None:
                self._state["match"]["timer_source"] = selector
            key = (selector, period)
            if key != getattr(self, "_last_timer_source", None):
                self._last_timer_source = key
                await self._log(
                    "MATCH_TIMER_FOUND",
                    f"Игровое время получено из отдельного элемента scoreboard: "
                    f"{parsed_timer}; selector={selector}",
                )
            return ScoreboardSnapshot(
                team1=snapshot.team1, team2=snapshot.team2,
                score=snapshot.score, timer=parsed_timer, period=period,
            )

        now = asyncio.get_running_loop().time()
        if now - getattr(self, "_last_missing_clock_logged_at", -100.0) > 15:
            self._last_missing_clock_logged_at = now
            await self._log(
                "MATCH_TIMER_MISSING",
                "Матч/счёт читаются, но игровой таймер отсутствует в известных "
                "элементах scoreboard. Не подменяем время обратным отсчётом "
                "и не создаём виртуальные ставки.",
            )
        return snapshot

    async def _observe_odds(
        self, page: Any, snapshot: ScoreboardSnapshot, *, entry_window: bool,
    ) -> Any | None:
        """Read and show current odds, without implying a virtual wager."""
        try:
            odds = await read_next_goal_odds(
                page, snapshot.team1, snapshot.team2,
                snapshot.score.team1, snapshot.score.team2,
                read_only=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._update(
                quote=None, odds_error=f"{type(error).__name__}: {error}",
                odds_status="UNAVAILABLE",
            )
            now = asyncio.get_running_loop().time()
            key = (type(error).__name__, str(error)[:100])
            if (
                key != getattr(self, "_last_odds_error_key", None)
                or now - getattr(self, "_last_odds_error_at", -100.0) > 15
            ):
                self._last_odds_error_key = key
                self._last_odds_error_at = now
                await self._log(
                    "ODDS_UNAVAILABLE",
                    f"Коэффициенты следующего гола временно не прочитаны: "
                    f"{type(error).__name__}: {str(error)[:180]}",
                )
            return None

        expected = snapshot.score.team1 + snapshot.score.team2 + 1
        if odds.next_goal_number != expected:
            self._update(quote=None, odds_status="STALE")
            return None
        blocked = bool(odds.locked_sides or odds.recent_locked_sides)
        a, b = odds.team1, odds.team2
        inverse = 1 / a + 1 / b if a > 1 and b > 1 else None
        self._update(
            quote={
                "team1": snapshot.team1, "team2": snapshot.team2,
                "odd1": a, "odd2": b,
                "goal_number": odds.next_goal_number,
                "inverse_sum": inverse,
                "conditional_signal": inverse is not None and inverse < 1,
                "blocked": blocked, "source": odds.source,
                "timer": snapshot.timer, "updated_at": utc_time(),
                "entry_window": entry_window,
                "observation_only": not entry_window,
            },
            odds_error=None, odds_status="AVAILABLE",
        )
        return odds

    async def _scan_one(self, page: Any) -> None:
        self._update(phase="SEARCHING_MATCH", match=None, quote=None)
        league = LeagueBrowser(page, exclude_teams_enabled=False)
        # Reuse a loaded league DOM on repeated scans. Reopening the SPA page
        # on every empty poll can interrupt hydration and hide match cards.
        expected = urlparse(CONFIG.league_url).path.rstrip("/")
        current = urlparse(str(page.url or "")).path.rstrip("/")
        if current != expected:
            await league.open()
        matches = await league.scan()
        stats = dict(league.last_scan_stats)
        self._update(scanner=stats)
        if not matches:
            sample = (league.skipped_unclassified or [])[:2]
            reasons = ", ".join(
                f"{item.get('team1', '?')} — {item.get('team2', '?')}: "
                f"time={item.get('time')!r}, period={item.get('period')!r}, "
                f"reason={item.get('reason', 'unknown')}"
                for item in sample
            )
            if stats.get("unclassified"):
                reason = "Карточки ещё не прочитаны / DOM не готов"
                self._update(phase="WAITING_CARDS")
            elif not stats.get("total"):
                reason = "На странице пока нет карточек целевой лиги"
                self._update(phase="NO_MATCH_CARDS")
            else:
                reason = "Нет ближайших предстоящих матчей"
                self._update(phase="NO_UPCOMING_MATCHES")
            signature = (reason, tuple(sorted(stats.items())), reasons)
            now = asyncio.get_running_loop().time()
            if (
                signature != getattr(self, "_last_empty_scan_signature", None)
                or now - getattr(self, "_last_empty_scan_logged_at", -100.0) >= 20
            ):
                self._last_empty_scan_signature = signature
                self._last_empty_scan_logged_at = now
                await self._log(
                    "MATCH_SCAN_EMPTY",
                    f"{reason}. Всего={stats.get('total', 0)}, "
                    f"будущих={stats.get('upcoming', 0)}, "
                    f"начатых={stats.get('started', 0)}, "
                    f"завершённых={stats.get('finished', 0)}, "
                    f"непрочитанных={stats.get('unclassified', 0)}; "
                    f"URL={page.url}; примеры: {reasons or 'нет'}",
                )
            return
        match = select_nearest_unstarted(matches, self._completed_match_ids)
        if match is None:
            self._update(phase="NO_UPCOMING_MATCHES")
            await self._log(
                "NO_ELIGIBLE_UPCOMING",
                "Среди карточек нет матчей с подтверждённым обратным отсчётом "
                "(прошедшие и уже LIVE исключены)",
            )
            return
        match_id = str(match.get("match_id") or match.get("href"))
        # Double-check the exact selected card immediately before opening.
        # The shared LeagueBrowser deliberately tolerates LIVE transitions,
        # so the forks observer must apply a stricter prematch entry rule.
        fresh = await league.revalidate_upcoming(match)
        if not select_nearest_unstarted([fresh], self._completed_match_ids):
            await self._log(
                "MATCH_STARTED_BEFORE_ENTRY",
                f'{match.get("team1")} — {match.get("team2")} уже начал матч '
                "или обратный отсчёт исчез до входа; пропускаем",
            )
            self._completed_match_ids.add(match_id)
            return
        self._update(phase="OPENING_MATCH", match={
            "id": match_id, "name": f'{match.get("team1")} — {match.get("team2")}',
            "timer": fresh.get("time"), "score": fresh.get("score"),
            "state": "UPCOMING", "countdown": fresh.get("time"),
        }, scanned=int(self._state["scanned"]) + 1)
        await self._log(
            "UPCOMING_MATCH_SELECTED",
            f'Ближайший НЕ начавшийся матч: {self._state["match"]["name"]}; '
            f'до начала {fresh.get("time")}; id={match_id}',
        )
        await league.open_match(fresh)
        # Only wait for the scoreboard now. The market/canvas may not exist
        # prematch, and waiting for it can lose the entire 30-second window.
        gate = MatchStartGate()
        self._update(phase="WAITING_MATCH_START")
        await self._log(
            "WAITING_MATCH_START",
            "Матч открыт до начала. Ждём LIVE и затем игровую отметку "
            "05:00; время до начала не считается игровым.",
        )
        countdown = int(fresh["time_seconds"])
        deadline = asyncio.get_running_loop().time() + max(
            480, min(10800, countdown + MATCH_DURATION + 180),
        )
        next_pre_window_quote = 0.0
        last_score_context = None
        while not self._stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                before = await self._read_live_scoreboard(page)
            except Exception as error:
                # A closed tab must propagate to BrowserManager, not get hidden
                # behind endless scoreboard retries.
                if self._is_closed_page_error(error, page):
                    raise
                await self.pause(0.45)
                continue
            seconds = clock_seconds(before.timer)
            context = (before.team1, before.team2,
                       before.score.team1, before.score.team2)
            if context != last_score_context:
                last_score_context = context
                self._update(quote=None, odds_status="WAITING", odds_error=None)
                next_pre_window_quote = 0.0
            if self._state.get("match") is not None:
                self._state["match"].update(
                    timer=before.timer, score=before.score.text(), period=before.period,
                    state="LIVE" if gate.started else "UPCOMING",
                    next_goal_number=before.score.team1 + before.score.team2 + 1,
                )
            if finished(before.period):
                self._completed_match_ids.add(match_id)
                await self._log(
                    "MATCH_FINISHED",
                    f'Матч завершён: {before.score.text()}; возвращаемся к следующему',
                )
                return
            was_started = gate.started
            now_started = gate.observe(before)
            if not now_started:
                self._update(phase="WAITING_MATCH_START", quote=None)
                await self.pause(0.4)
                continue
            if not was_started:
                self._update(phase="WAITING_WINDOW", quote=None)
                await self._log(
                    "MATCH_LIVE_STARTED",
                    f'Подтверждено начало LIVE: {before.team1} — {before.team2}; '
                    f'игровой таймер {before.timer or "не определён"}',
                )
            if self._state.get("match") is not None:
                self._state["match"]["state"] = "LIVE"
            if seconds is None:
                self._update(phase="WAITING_TIMER", odds_status="WAITING_TIMER")
                await self.pause(0.4)
                continue
            if seconds > CLOSE_SECOND:
                self._completed_match_ids.add(match_id)
                await self._log(
                    "WINDOW_CLOSED",
                    f'Время LIVE {before.timer} — позже 05:30; '
                    "виртуальных ставок больше нет, выбираем следующий будущий матч",
                )
                return

            if seconds < OPEN_SECOND:
                self._update(phase="WAITING_WINDOW")
                # Observe odds periodically before 05:00 so the frontend can
                # show current prices and the correct next-goal number.
                # No virtual pair is allowed here.
                now = asyncio.get_running_loop().time()
                if now >= next_pre_window_quote:
                    next_pre_window_quote = now + PRE_WINDOW_QUOTE_INTERVAL
                    self._update(phase="OBSERVING_ODDS")
                    await self._observe_odds(page, before, entry_window=False)
                    # Avoid an expensive Canvas read on every 400ms tick.
                await self.pause(0.4)
                continue

            # Only the 05:00–05:30 segment may allocate virtual funds.
            self._update(phase="READING_ODDS")
            odds = await self._observe_odds(page, before, entry_window=True)
            if odds is None:
                self._update(phase="WAITING_MARKET")
                await self.pause(IN_WINDOW_QUOTE_INTERVAL)
                continue
            blocked = bool(odds.locked_sides or odds.recent_locked_sides)
            inv = (1 / odds.team1 + 1 / odds.team2) if odds.team1 > 1 and odds.team2 > 1 else None
            if blocked or inv is None or inv >= 1:
                self._update(phase="WATCHING_QUOTES")
                await self.pause(IN_WINDOW_QUOTE_INTERVAL)
                continue

            # Recheck the actual score and elapsed time after reading the quote.
            after = await self._read_live_scoreboard(page)
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
            self._completed_match_ids.add(match_id)
            return
        self._completed_match_ids.add(match_id)
        await self._log("MATCH_TIMEOUT", "Истёк лимит ожидания начала/окна. Переходим к следующему матчу")

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
