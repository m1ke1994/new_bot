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

from auth import SITE_URL, authorize
from backend.app.browser.canvas_2d_adapter import read_next_goal_odds
from backend.app.browser.league import LeagueBrowser
from backend.app.browser.scoreboard import read_scoreboard, parse_timer_and_period
from backend.app.browser.manager import BROWSER_MANAGER
from backend.app.browser.navigation import NavigationLoadError, is_network_transport_error
from backend.app.demo.config import CONFIG
from backend.app.demo.engine import ENGINE
from backend.app.demo.models import ScoreboardSnapshot
from backend.app.bookmaker_demo_forks import BookmakerDemoPlacer, require_bookmaker_demo

CENT = Decimal("0.01")
OPEN_SECOND = 300
CLOSE_SECOND = 335
FORK_RESULT_TIMEOUT_SECONDS = 60
TARGET_VIRTUAL_PAIRS = 200
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


def select_live_handoff_match(
    matches: list[dict[str, Any]],
    excluded_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Resume on a different LIVE match if its 05:00 window is still ahead/open."""
    excluded = excluded_ids or set()
    candidates = []
    for match in matches:
        identity = str(match.get("match_id") or match.get("href") or "")
        elapsed = match.get("time_seconds")
        if (
            not identity or identity in excluded or match.get("finished")
            or match.get("is_upcoming")
            or not live_period(match.get("period", ""))
            or not isinstance(elapsed, int)
            or not 0 <= elapsed <= CLOSE_SECOND
        ):
            continue
        candidates.append(match)
    # Prefer a match approaching 05:00. Do not pick an already concluded game.
    return min(
        candidates,
        key=lambda item: (abs(OPEN_SECOND - item["time_seconds"]),
                          -item["time_seconds"]),
        default=None,
    )


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
    """Allocate whole rubles and re-check both CONDITIONAL payoffs after rounding.

    The bookmaker's third 'no more goals' outcome is not covered, so this
    is never advertised as a risk-free arbitrage.
    """
    from decimal import ROUND_HALF_UP
    try:
        a, b = Decimal(str(odd1)), Decimal(str(odd2))
        amount = Decimal(str(total)).quantize(Decimal("1"), rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if a <= 1 or b <= 1 or amount < 20:
        return None
    inverse = Decimal(1) / a + Decimal(1) / b
    if inverse >= 1:
        return None
    ideal = (amount * (Decimal(1) / a) / inverse).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    best = None
    for offset in (-2, -1, 0, 1, 2):
        stake1 = ideal + offset
        stake2 = amount - stake1
        if min(stake1, stake2) < 10:
            continue
        payout1 = (stake1 * a).quantize(CENT, rounding=ROUND_DOWN)
        payout2 = (stake2 * b).quantize(CENT, rounding=ROUND_DOWN)
        if min(payout1, payout2) <= amount:
            continue
        candidate = (min(payout1, payout2), stake1, stake2, payout1, payout2)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    _, stake1, stake2, payout1, payout2 = best
    return {
        "odd1": float(a), "odd2": float(b), "inverse_sum": float(inverse),
        "stake1": float(stake1), "stake2": float(stake2),
        "total_stake": float(amount), "payout1": float(payout1),
        "payout2": float(payout2), "profit1": float(payout1 - amount),
        "profit2": float(payout2 - amount),
        "minimum_conditional_profit": float(min(payout1, payout2) - amount),
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
                CREATE TABLE IF NOT EXISTS fork_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fork_obs_session
                    ON fork_observations(session_id, id);
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

    def add_observation(self, item: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO fork_observations(session_id,match_id,timestamp,payload) "
                "VALUES(?,?,?,?)",
                (item["session_id"], item["match_id"], item["timestamp"],
                 json.dumps(item, ensure_ascii=False)),
            )

    def observations(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._connect() as db:
            items = db.execute(
                "SELECT payload FROM fork_observations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(item[0]) for item in items]

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

    def count_pairs(self) -> int:
        """All stored virtual pairs, irrespective of their result or session."""
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM virtual_forks").fetchone()[0])

    def count_resolved_pairs(self) -> int:
        """A pending or review-only pair does not count as completed."""
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM virtual_forks "
                "WHERE status IN ('SETTLED', 'TIMEOUT_60S')"
            ).fetchone()[0])

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
        self._placement_mode = "VIRTUAL"
        self._bookmaker_placer: BookmakerDemoPlacer | None = None
        self._position: dict[str, Any] | None = None
        self._position_deadline: float | None = None
        self._completed_match_ids: set[str] = set()
        self._allow_live_handoff = False
        self._last_market_event: dict[str, float] = {}
        self._observed_score: Any | None = None
        self._observed_match_id: str | None = None
        self._state: dict[str, Any] = {
            "running": False, "phase": "STOPPED", "placement_mode": "VIRTUAL",
            "message": "Наблюдатель остановлен", "window": "05:00–05:35",
            "initial_budget": 0, "balance": 0, "allocated_percent": 10,
            "session_profit": 0, "match": None, "quote": None,
            "odds_error": None, "odds_status": "WAITING",
            "market_search": "WAITING", "observations": [],
            "position": None, "scanned": 0, "signals": 0,
            "target_pairs": TARGET_VIRTUAL_PAIRS,
            "completed_pairs": 0, "remaining_pairs": TARGET_VIRTUAL_PAIRS,
            "records_total": 0,
            "warnings": ["Третий исход «голов больше не будет» не покрыт."],
        }

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        state = deepcopy(self._state)
        state["running"] = self.running
        import os
        state["demo_account_selector_configured"] = bool(
            os.environ.get("FORK_DEMO_ACCOUNT_SELECTOR", "").strip()
        )
        completed = self.store.count_resolved_pairs()
        state["completed_pairs"] = completed
        state["records_total"] = self.store.count_pairs()
        state["remaining_pairs"] = max(0, TARGET_VIRTUAL_PAIRS - completed)
        state["target_pairs"] = TARGET_VIRTUAL_PAIRS
        if self._position is not None and self._position_deadline is not None:
            remaining = max(0, int(self._position_deadline - asyncio.get_running_loop().time() + 0.999))
            state["result_timeout_remaining"] = remaining
        else:
            state["result_timeout_remaining"] = None
        return state

    def _update(self, **changes: Any) -> None:
        self._state.update(changes, updated_at=utc_time(), running=self.running)

    async def _log(self, event: str, message: str) -> None:
        self.store.add_log(self._session, event, message)
        self._update(message=message)

    async def start(self, budget: Any, percent: Any = 10, *,
                    placement_mode: str = "VIRTUAL",
                    confirm_demo: bool = False) -> dict[str, Any]:
        if self.running:
            raise ValueError("Наблюдатель вилок уже запущен")
        placement_mode = str(placement_mode).upper()
        if placement_mode not in ("VIRTUAL", "BOOKMAKER_DEMO"):
            raise ValueError("Допустимы режимы VIRTUAL и BOOKMAKER_DEMO")
        if placement_mode == "BOOKMAKER_DEMO":
            if not confirm_demo:
                raise ValueError("Подтвердите, что используете учебный счёт букмекера")
            import os
            if not os.environ.get("FORK_DEMO_ACCOUNT_SELECTOR", "").strip():
                raise ValueError(
                    "Для БК DEMO требуется FORK_DEMO_ACCOUNT_SELECTOR: "
                    "селектор видимой метки учебного счёта на сайте"
                )
        completed_before_start = self.store.count_resolved_pairs()
        if placement_mode == "VIRTUAL" and completed_before_start >= TARGET_VIRTUAL_PAIRS:
            raise ValueError(
                f"Цель {TARGET_VIRTUAL_PAIRS} завершённых виртуальных пар "
                "уже достигнута; история сохранена"
            )
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
        self._placement_mode = placement_mode
        self._bookmaker_placer = BookmakerDemoPlacer(self._log) if placement_mode == "BOOKMAKER_DEMO" else None
        self._initial = cash(amount)
        self._balance = self._initial
        self._allocation_pct = share
        self._position = None
        self._position_deadline = None
        self._completed_match_ids = set()
        self._allow_live_handoff = False
        self._last_market_event = {}
        self._observed_score = None
        self._observed_match_id = None
        self._stop_event = asyncio.Event()
        self._state.update({
            "phase": "STARTING", "message": "Запуск наблюдателя вилок",
            "placement_mode": placement_mode,
            "session_id": self._session, "initial_budget": float(self._initial),
            "balance": float(self._balance), "allocated_percent": float(share),
            "session_profit": 0, "match": None, "quote": None,
            "odds_error": None, "odds_status": "WAITING",
            "position": None, "scanned": 0, "signals": 0,
            "completed_pairs": completed_before_start,
            "remaining_pairs": TARGET_VIRTUAL_PAIRS - completed_before_start,
            "target_pairs": TARGET_VIRTUAL_PAIRS,
            "records_total": self.store.count_pairs(),
        })
        self._task = asyncio.create_task(self._run(), name="fc25-virtual-forks")
        self._update(running=True)
        await self._log(
            "START",
            f"Запущен виртуальный мониторинг 05:00–05:35; "
            f"выполнено {completed_before_start}/{TARGET_VIRTUAL_PAIRS} "
            "завершённых виртуальных пар. "
            + ("БК DEMO: клики разрешены только после подтверждения "
               "учебного счёта сайта."
               if placement_mode == "BOOKMAKER_DEMO"
               else "VIRTUAL: купоны БК не открываются."),
        )
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
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed):
                try:
                    if is_closed():
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
                if self._placement_mode == "VIRTUAL" and self.store.count_resolved_pairs() >= TARGET_VIRTUAL_PAIRS:
                    self._update(phase="TARGET_REACHED", match=None, quote=None)
                    await self._log(
                        "TARGET_REACHED",
                        f"Набрано {TARGET_VIRTUAL_PAIRS} завершённых виртуальных пар. "
                        "Мониторинг останавливается, история остаётся в SQLite.",
                    )
                    self._stop_event.set()
                    break
                if self._placement_mode == "BOOKMAKER_DEMO" and self._balance < 20:
                    self._update(phase="DEMO_BALANCE_EXHAUSTED")
                    await self._log(
                        "DEMO_BALANCE_EXHAUSTED",
                        "Демо-лимит менее 20 ₽ — новых купонов не отправляем",
                    )
                    self._stop_event.set()
                    break
                page = None
                try:
                    # A newly launched persistent Chromium starts on about:blank.
                    # prepare_session_page() stabilizes the actual site tab and
                    # opens the bookmaker BEFORE the auth/league observer begins.
                    # A healthy authorized tab is reused without navigating.
                    if authorized_page is None:
                        self._update(
                            phase="OPENING_SITE",
                            target_url=SITE_URL,
                            browser_url="about:blank",
                        )
                        await self._log(
                            "BROWSER_PREPARING",
                            f"Подготавливаем вкладку Chromium и открываем {SITE_URL}",
                        )
                        page = await BROWSER_MANAGER.prepare_session_page()
                    else:
                        page = await BROWSER_MANAGER.ensure_page()
                    if page.is_closed():
                        raise RuntimeError("Вкладка Chromium закрыта до начала наблюдения")
                    generation = BROWSER_MANAGER.generation
                    if page is not authorized_page or generation != authorized_generation:
                        self._update(
                            phase="OPENING_SITE", match=None, quote=None,
                            browser_url=str(getattr(page, "url", "about:blank")),
                            target_url=SITE_URL,
                        )
                        await self._log(
                            "SITE_NAVIGATION_START",
                            f"Открываем сайт {SITE_URL}; текущая вкладка: "
                            f'{getattr(page, "url", "about:blank")}',
                        )
                        # authorize() itself calls auth.open_site(). Keep ONE
                        # navigation owner; do not issue a competing page.goto().
                        auth = await authorize(page, self._stop_event)
                        self._update(browser_url=str(getattr(page, "url", "about:blank")))
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
                        current_url = str(getattr(page, "url", "about:blank"))
                        if current_url.startswith(("about:", "chrome-error:")):
                            await self._log(
                                "SITE_URL_INVALID",
                                f"Маркер авторизации получен, но вкладка не на сайте: "
                                f"{current_url}. Повторяем проверку.",
                            )
                            await self.pause(3)
                            continue
                        authorized_page = page
                        authorized_generation = generation
                        await self._log(
                            "BROWSER_READY",
                            f"Сайт открыт и авторизация подтверждена: {current_url}",
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
                            "SITE_NAVIGATION_FAILED",
                            f"Переход на {SITE_URL} не завершился; "
                            f'вкладка={getattr(page, "url", "<нет>") if page else "<нет>"}; '
                            f"ошибка={type(error).__name__}: {error}",
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
                    previous_phase = str(self._state.get("phase") or "")
                    error_on_auth = previous_phase in ("OPENING_SITE", "AUTH_CHECK")
                    self._update(
                        phase="SITE_ERROR" if error_on_auth else "ERROR",
                        browser_url=str(getattr(page, "url", "<нет>")) if page else "<нет>",
                    )
                    await self._log(
                        "SITE_OPEN_ERROR" if error_on_auth else "SCAN_ERROR",
                        (
                            f"Не удалось открыть сайт {SITE_URL}; "
                            f'вкладка={getattr(page, "url", "<нет>") if page else "<нет>"}; '
                            if error_on_auth else "Ошибка чтения матчей: "
                        ) + f"{type(error).__name__}: {error}",
                    )
                    await self.pause(5.0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._log("WORKER_ERROR", f"Ошибка запуска: {type(error).__name__}: {error}")
        finally:
            if self._position and self._position.get("status") == "PENDING":
                await self._review("Наблюдение прервано до подтверждения результата")
            self._update(
                running=False,
                phase=self._state.get("phase")
                if self._state.get("phase") in (
                    "TARGET_REACHED", "BOOKMAKER_DEMO_HALT",
                    "BOOKMAKER_DEMO_UNRESOLVED", "DEMO_BALANCE_EXHAUSTED"
                ) else "STOPPED",
            )

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

    async def _market_logger(self, event: str, message: str) -> None:
        """Expose the normal Next Goal search/canvas sequence in the live UI."""
        monitored = {
            "MARKET_SEARCH_TARGET", "MARKET_SEARCH_OPENED",
            "MARKET_SEARCH_FILLED", "MARKET_SEARCH_REUSED",
            "NEXT_GOAL_MARKET_FOUND", "TEAM1_ODDS", "TEAM2_ODDS",
            "CANVAS_2D_HOOK_INSTALLED_LATE", "CANVAS_2D_NO_DRAW_CALLS",
            "CANVAS_2D_READ", "ODDS_READY",
        }
        if event not in monitored:
            return
        if event in {"MARKET_SEARCH_FILLED", "MARKET_SEARCH_OPENED"}:
            self._update(market_search="FILLED" if event.endswith("FILLED") else "OPENED")
        if event == "NEXT_GOAL_MARKET_FOUND":
            self._update(market_search="READY")
        now = asyncio.get_running_loop().time()
        if now - self._last_market_event.get(event, -100.0) < (8 if event == "MARKET_SEARCH_REUSED" else 2):
            return
        self._last_market_event[event] = now
        await self._log(event, message)

    async def _record_score(self, match_id: str, snap: ScoreboardSnapshot) -> None:
        """Persist every observed goal exactly once; do not invent missed goals."""
        if self._observed_match_id != match_id:
            self._observed_match_id = match_id
            self._observed_score = snap.score
            self._state["observations"] = []
            item = {
                "session_id": self._session, "match_id": match_id,
                "timestamp": utc_time(), "event": "MATCH_JOINED",
                "match": f"{snap.team1} — {snap.team2}",
                "timer": snap.timer, "period": snap.period,
                "score_before": None, "score_after": snap.score.text(),
                "goal_number": snap.score.team1 + snap.score.team2,
                "scorer": None, "scorer_team": None,
                "note": "Начальное наблюдение. Ранее забитые голы не приписываются.",
            }
            self.store.add_observation(item)
            self._state["observations"].append(item)
            await self._log("SCORE_BASELINE", f'{item["match"]}: счёт при подключении {snap.score.text()}')
            return
        previous = self._observed_score
        if previous == snap.score:
            return
        d1 = snap.score.team1 - previous.team1
        d2 = snap.score.team2 - previous.team2
        if d1 < 0 or d2 < 0:
            # A corrected scoreboard is not proof of a new goal.
            event, scorer, label = "SCORE_CORRECTED", None, None
        elif d1 + d2 == 1:
            event = "GOAL"
            scorer = "TEAM_1" if d1 == 1 else "TEAM_2"
            label = snap.team1 if d1 == 1 else snap.team2
        else:
            event, scorer, label = "AMBIGUOUS_CHANGE", None, None
        item = {
            "session_id": self._session, "match_id": match_id,
            "timestamp": utc_time(), "event": event,
            "match": f"{snap.team1} — {snap.team2}",
            "timer": snap.timer, "period": snap.period,
            "score_before": previous.text(), "score_after": snap.score.text(),
            "goal_number": snap.score.team1 + snap.score.team2,
            "scorer": scorer, "scorer_team": label,
            "note": "" if event == "GOAL" else "Нельзя уверенно определить автора каждого гола по одному изменению счёта",
        }
        self._observed_score = snap.score
        self.store.add_observation(item)
        history = self._state.setdefault("observations", [])
        history.append(item)
        if len(history) > 50:
            del history[:-50]
        if event == "GOAL":
            await self._log(
                "GOAL_OBSERVED",
                f'Гол №{item["goal_number"]}: {label}; '
                f'{item["score_before"]} → {item["score_after"]}; LIVE {snap.timer}',
            )
        else:
            await self._log(
                event,
                f'Неоднозначное изменение счёта: {item["score_before"]} → {item["score_after"]}',
            )

    async def _observe_odds(
        self, page: Any, snapshot: ScoreboardSnapshot, *, entry_window: bool,
    ) -> Any | None:
        """Read and show current odds, without implying a virtual wager."""
        try:
            odds = await read_next_goal_odds(
                page, snapshot.team1, snapshot.team2,
                snapshot.score.team1, snapshot.score.team2,
                self._market_logger,
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
        if not matches and not (self._allow_live_handoff and league.skipped_started):
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
        # Initial cycle is strictly PREMATCH; after a settled virtual pair,
        # a different game may already be LIVE. The regular strategy's scan()
        # keeps LIVE cards in skipped_started, so reuse them without changing
        # its behavior for the old DEMO/LIVE worker.
        handoff = (
            select_live_handoff_match(
                league.skipped_started, self._completed_match_ids,
            )
            if self._allow_live_handoff else None
        )
        match = handoff or select_nearest_unstarted(
            matches, self._completed_match_ids,
        )
        if match is None:
            self._update(phase="NO_UPCOMING_MATCHES")
            now = asyncio.get_running_loop().time()
            if now - getattr(self, "_last_no_eligible_at", -100.0) >= 20:
                self._last_no_eligible_at = now
                count = self.store.count_resolved_pairs()
                await self._log(
                    "NO_ELIGIBLE_MATCH",
                    f"Выполнено {count}/{TARGET_VIRTUAL_PAIRS} пар. "
                    "Нет другого подходящего будущего матча или LIVE до 05:35; "
                    "продолжаем искать, мониторинг не завершён.",
                )
            return
        match_id = str(match.get("match_id") or match.get("href"))
        # Revalidate the exact card before clicking it.
        fresh = await league.revalidate_upcoming(match)
        is_live_handoff = bool(
            handoff is not None
            and select_live_handoff_match([fresh], self._completed_match_ids)
        )
        if not is_live_handoff and not select_nearest_unstarted(
            [fresh], self._completed_match_ids,
        ):
            await self._log(
                "MATCH_NO_LONGER_ELIGIBLE",
                f'{match.get("team1")} — {match.get("team2")} '
                "уже вышел за разрешённое окно; пропускаем",
            )
            self._completed_match_ids.add(match_id)
            return
        self._update(phase="OPENING_MATCH", match={
            "id": match_id, "name": f'{match.get("team1")} — {match.get("team2")}',
            "team1": match.get("team1"), "team2": match.get("team2"),
            "timer": fresh.get("time"), "score": fresh.get("score"),
            "state": "LIVE" if is_live_handoff else "UPCOMING",
            "countdown": None if is_live_handoff else fresh.get("time"),
        }, scanned=int(self._state["scanned"]) + 1)
        await self._log(
            "LIVE_HANDOFF_SELECTED" if is_live_handoff else "UPCOMING_MATCH_SELECTED",
            f'Выбран {"уже LIVE" if is_live_handoff else "ещё не начавшийся"} '
            f'матч: {self._state["match"]["name"]}; '
            f'таймер={fresh.get("time")}; id={match_id}',
        )
        await league.open_match(fresh)
        gate = MatchStartGate()
        if is_live_handoff:
            gate.started = True
            self._update(phase="WAITING_WINDOW")
            await self._log(
                "LIVE_HANDOFF",
                "После расчёта виртуальной пары перешли в уже идущий LIVE матч. "
                "Снимем начальный счёт без догадок о прошлых голах.",
            )
        else:
            self._update(phase="WAITING_MATCH_START")
            await self._log(
                "WAITING_MATCH_START",
                "Матч открыт до начала. Ждём LIVE и затем игровую отметку "
                "05:00; время до начала не считается игровым.",
            )
        countdown = 0 if is_live_handoff else int(fresh["time_seconds"])
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
            await self._record_score(match_id, before)
            context = (before.team1, before.team2,
                       before.score.team1, before.score.team2)
            if context != last_score_context:
                last_score_context = context
                self._update(quote=None, odds_status="WAITING", odds_error=None)
                next_pre_window_quote = 0.0
            if self._state.get("match") is not None:
                self._state["match"].update(
                    timer=before.timer, score=before.score.text(), period=before.period,
                    team1=before.team1, team2=before.team2,
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
                # The current Next Goal search can still be prepared and
                # displayed without a readable elapsed match clock.
                # Allocation remains forbidden without a verified 05:00–05:30.
                self._update(phase="WAITING_TIMER", odds_status="WAITING_TIMER")
                now = asyncio.get_running_loop().time()
                if now >= next_pre_window_quote:
                    next_pre_window_quote = now + PRE_WINDOW_QUOTE_INTERVAL
                    await self._observe_odds(page, before, entry_window=False)
                await self.pause(0.4)
                continue
            if seconds > CLOSE_SECOND:
                self._completed_match_ids.add(match_id)
                await self._log(
                    "WINDOW_CLOSED",
                    f'Время LIVE {before.timer} — позже 05:35; '
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

            # Only the 05:00–05:35 segment may allocate virtual funds.
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
                now = asyncio.get_running_loop().time()
                if now - getattr(self, "_last_no_fork_log_at", -100.0) > 5:
                    self._last_no_fork_log_at = now
                    reason = (
                        "рынок заблокирован" if blocked else
                        "нет двух корректных коэффициентов" if inv is None else
                        f"сумма обратных коэффициентов {inv:.4f} >= 1"
                    )
                    await self._log(
                        "FORK_NOT_FOUND",
                        f"Время LIVE {before.timer}: {reason}. "
                        "Виртуальная пара не создаётся; продолжаем до 05:35.",
                    )
                await self.pause(IN_WINDOW_QUOTE_INTERVAL)
                continue

            # Recheck the actual score and elapsed time after reading the quote.
            after = await self._read_live_scoreboard(page)
            await self._record_score(match_id, after)
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
                "placement_mode": self._placement_mode,
                "third_outcome_covered": False,
                "timeout_seconds": FORK_RESULT_TIMEOUT_SECONDS,
                "settlement_basis": None,
                "team1_result": "PENDING", "team2_result": "PENDING",
                "team1_payout": None, "team2_payout": None,
            }
            if self._placement_mode == "BOOKMAKER_DEMO":
                if self._bookmaker_placer is None:
                    raise RuntimeError("Bookmaker demo executor не инициализирован")
                position.update(
                    status="BOOKMAKER_DEMO_PREPARING",
                    bookmaker_settled=False, debited_sides=[],
                )
                self._position = position
                self.store.upsert(position)
                self._update(phase="BOOKMAKER_DEMO_PLACING",
                             position=deepcopy(position))

                async def persist_demo(item: dict[str, Any]) -> None:
                    for side in (1, 2):
                        if (
                            item.get(f"team{side}_placement") == "ACCEPTED"
                            and side not in item["debited_sides"]
                        ):
                            self._balance -= Decimal(str(
                                item[f"team{side}_accepted_stake"]
                            ))
                            item["debited_sides"].append(side)
                    self.store.upsert(item)
                    self._update(position=deepcopy(item),
                                 balance=float(self._balance))

                def in_window(scoreboard: ScoreboardSnapshot) -> bool:
                    t = clock_seconds(scoreboard.timer)
                    return (
                        t is not None
                        and OPEN_SECOND <= t <= CLOSE_SECOND
                        and not finished(scoreboard.period)
                    )

                try:
                    completed = await self._bookmaker_placer.place(
                        page, position, odds, self._stop_event,
                        persist_demo, self._read_live_scoreboard, in_window,
                    )
                except BaseException as error:
                    position["status"] = "BOOKMAKER_DEMO_SUBMISSION_UNKNOWN"
                    position["placement_error"] = (
                        f"{type(error).__name__}: {error}; "
                        "необходимо проверить учебные купоны БК"
                    )
                    self.store.upsert(position)
                    self._update(
                        phase="BOOKMAKER_DEMO_HALT",
                        position=deepcopy(position),
                    )
                    self._stop_event.set()
                    self._position = None
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    await self._log(
                        "BOOKMAKER_DEMO_HALT", position["placement_error"],
                    )
                    return
                if not completed:
                    self._update(
                        phase="BOOKMAKER_DEMO_HALT",
                        position=deepcopy(position),
                        message=position.get("placement_error",
                                             "Только часть купонов подтверждена"),
                    )
                    await self._log(
                        "BOOKMAKER_DEMO_HALT",
                        "Новых купонов не будет до проверки частичной пары.",
                    )
                    self._stop_event.set()
                    self._position = None
                    return
                position["status"] = "PENDING"
                position["accepted_legs"] = 2
                self.store.upsert(position)
                self._position_deadline = (
                    asyncio.get_running_loop().time() + FORK_RESULT_TIMEOUT_SECONDS
                )
                self._update(
                    phase="WAITING_RESULT",
                    position=deepcopy(position), balance=float(self._balance),
                    signals=int(self._state["signals"]) + 1,
                )
                await self._log(
                    "BOOKMAKER_DEMO_TWO_ACCEPTED",
                    "Оба учебных купона приняты; ждём гол. "
                    "Выигрыш пока не подтверждён расчётом БК.",
                )
                await self._settle(page, after)
                self._completed_match_ids.add(match_id)
                return

            self._balance -= cash(proposal["total_stake"])
            self._position = position
            self._position_deadline = asyncio.get_running_loop().time() + FORK_RESULT_TIMEOUT_SECONDS
            self.store.upsert(position)
            self._update(
                phase="WAITING_RESULT", position=position, balance=float(self._balance),
                result_timeout_seconds=FORK_RESULT_TIMEOUT_SECONDS,
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
        """Observe the next goal for at most 60 real seconds after placement.

        A 60-second timeout is a *simulated strategy* loss, not a bookmaker
        settlement; a bookmaker's next-goal wager can stay open until a later
        goal or the match finishes. If the score cannot be verified at the
        deadline, mark NEEDS_REVIEW instead of inventing a loss.
        """
        deadline = self._position_deadline or (
            asyncio.get_running_loop().time() + FORK_RESULT_TIMEOUT_SECONDS
        )
        while not self._stop_event.is_set():
            try:
                snap = await self._read_live_scoreboard(page)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._is_closed_page_error(error, page):
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    await self._review(
                        f"По истечении 60 секунд счёт нельзя проверить: "
                        f"{type(error).__name__}: {error}"
                    )
                    return
                await self.pause(0.35)
                continue
            if (snap.team1, snap.team2) != (initial.team1, initial.team2):
                await self._review("Команды поменялись во время наблюдения")
                return
            await self._record_score(str(self._state["match"]["id"]), snap)
            if self._state.get("match") is not None:
                self._state["match"].update(
                    timer=snap.timer, period=snap.period,
                    score=snap.score.text(),
                )
            delta1 = snap.score.team1 - initial.score.team1
            delta2 = snap.score.team2 - initial.score.team2
            if delta1 < 0 or delta2 < 0 or delta1 + delta2 > 1:
                await self._review("Нельзя достоверно определить первый гол по изменению счёта")
                return
            if delta1 + delta2 == 1:
                await self._resolve("TEAM_1" if delta1 == 1 else "TEAM_2", snap.score.text())
                return
            if finished(snap.period):
                await self._resolve("NO_GOAL", snap.score.text())
                return
            if asyncio.get_running_loop().time() >= deadline:
                if self._placement_mode == "BOOKMAKER_DEMO":
                    await self._review(
                        "За 60 секунд гол не зафиксирован, но оба учебных "
                        "купона могут оставаться открытыми в БК. "
                        "Расчёт и возврат средств требуют проверки."
                    )
                else:
                    await self._resolve("TIMEOUT_60S", snap.score.text())
                return
            await self.pause(0.35)
        if self._position is not None and self._position["status"] == "PENDING":
            await self._review("Наблюдение остановлено до получения результата")

    async def _resolve(self, outcome: str, score: str) -> None:
        if not self._position or self._position.get("status") != "PENDING":
            return
        p = self._position
        if outcome not in ("TEAM_1", "TEAM_2", "NO_GOAL", "TIMEOUT_60S"):
            raise ValueError(f"Неизвестный исход виртуальной пары: {outcome}")
        if self._placement_mode == "BOOKMAKER_DEMO" and outcome in ("NO_GOAL", "TIMEOUT_60S"):
            await self._review(
                "Итог без нового гола обнаружен, но расчёт купонов учебным "
                "счётом букмекера не проверен."
            )
            return
        bookmaker_demo = p.get("placement_mode") == "BOOKMAKER_DEMO"
        winner1 = outcome == "TEAM_1"
        winner2 = outcome == "TEAM_2"
        payout1 = cash(p["payout1"] if winner1 else 0)
        payout2 = cash(p["payout2"] if winner2 else 0)
        payout = payout1 + payout2
        pnl = payout - cash(p["total_stake"])
        if not bookmaker_demo:
            self._balance += payout
        simulated_timeout = outcome == "TIMEOUT_60S"
        p.update(
            status=(
                "BOOKMAKER_DEMO_GOAL_OBSERVED" if bookmaker_demo
                else "TIMEOUT_60S" if simulated_timeout else "SETTLED"
            ),
            result=outcome, score_after=score,
            team1_result="WIN" if winner1 else "LOSE",
            team2_result="WIN" if winner2 else "LOSE",
            team1_payout=float(payout1), team2_payout=float(payout2),
            payout=None if bookmaker_demo else float(payout),
            profit=None if bookmaker_demo else float(pnl),
            projected_payout=float(payout) if bookmaker_demo else None,
            projected_profit=float(pnl) if bookmaker_demo else None,
            settlement_basis=(
                "OBSERVED_GOAL_AWAITING_BOOKMAKER_RESULT" if bookmaker_demo
                else "SIMULATED_60S_TIMEOUT" if simulated_timeout else "OBSERVED_MATCH"
            ),
            bookmaker_settled=False,
            settled_at=utc_time(),
        )
        self.store.upsert(p)
        self._update(
            phase="SETTLED", position=deepcopy(p),
            balance=float(self._balance),
            session_profit=0.0 if bookmaker_demo else float(self._balance - self._initial),
        )
        await self._log(
            "BOOKMAKER_DEMO_GOAL_OBSERVED" if bookmaker_demo else
            "VIRTUAL_TIMEOUT_60S" if simulated_timeout else "SETTLED",
            f'{p["match"]}: {outcome} / {score}; '
            f'{p["team1"]}: {p["team1_result"]}, '
            f'{p["team2"]}: {p["team2_result"]}; '
            f'расчётный результат {pnl:+.2f} ₽'
            + (" (учебные купоны БК: выигрыш и выплата пока не подтверждены)"
               if bookmaker_demo else
               " (условный тайм-аут; не официальный расчёт БК)" if simulated_timeout else ""),
        )
        self._position = None
        self._position_deadline = None
        self._allow_live_handoff = True

    async def _review(self, reason: str) -> None:
        if not self._position or self._position.get("status") != "PENDING":
            return
        p = self._position
        bookmaker_demo = p.get("placement_mode") == "BOOKMAKER_DEMO"
        p.update(
            status="BOOKMAKER_DEMO_UNRESOLVED" if bookmaker_demo else "NEEDS_REVIEW",
            result="UNKNOWN", settled_at=utc_time(),
            review_reason=reason, bookmaker_settled=False,
        )
        self.store.upsert(p)
        self._update(
            phase="BOOKMAKER_DEMO_UNRESOLVED" if bookmaker_demo else "NEEDS_REVIEW",
            position=deepcopy(p),
        )
        await self._log(
            "BOOKMAKER_DEMO_UNRESOLVED" if bookmaker_demo else "NEEDS_REVIEW",
            f'{p["match"]}: {reason}. '
            + ("Новые купоны заблокированы до проверки учебных ставок."
               if bookmaker_demo else "Виртуальные средства зарезервированы."),
        )
        if bookmaker_demo:
            self._stop_event.set()
        self._position = None
        self._position_deadline = None
        self._allow_live_handoff = True


FORKS = VirtualForkObserver()
