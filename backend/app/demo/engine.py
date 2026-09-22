import asyncio
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from auth import authorize
from backend.app.browser.canvas_2d_adapter import (
    read_next_goal_lock_state,
    reset_canvas_2d_for_live_transition,
)
from backend.app.browser.canvas_vision import load_latest_analysis
from backend.app.browser.first_half import (
    FirstHalfNotReady,
    first_half_draw_market_present,
    first_half_end_signal,
    open_first_half,
)
from backend.app.browser.league import (
    LeagueBrowser,
    MatchAlreadyStarted,
    MatchContentLoadTimeout,
)
from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from backend.app.browser.market import (
    MarketReadError,
    market_canvas_debug,
    read_first_half_draw_market,
    read_next_goal_odds,
)
from backend.app.browser.match import MatchBrowser
from backend.app.browser.scoreboard import ScoreReadError
from backend.app.live.executor import BLOCKED_EVENT_SIGNAL, LiveExecutor
from backend.app.live.models import (
    ActiveLiveBet,
    LiveDecision,
    LivePreparationError,
    LiveStatus,
    PendingLiveBet,
    PlacementObservation,
)
from backend.app.match_filters import (
    MIN_INITIAL_SELECTED_ODDS,
    excluded_team_in_match,
    is_initial_odds_allowed,
)

from .budget import DemoBudget
from .config import CONFIG
from .history import REPOSITORY
from .models import CurrentSeries, DemoStatus, Score, Scorer, ScoreboardSnapshot
from .state import STATE
from .strategy import (
    DEFAULT_STRATEGY_CONFIG,
    StrategyConfig,
    StrategyType,
    can_create_initial_bet,
    detect_scorer,
    odds_for_selected_side,
    select_team_with_higher_odds,
)
from .strategies.first_half_draw import (
    MARKET_NAME as FIRST_HALF_DRAW_MARKET_NAME,
    MARKET_SELECTION as FIRST_HALF_DRAW_SELECTION,
    PERIOD_KEY as FIRST_HALF_DRAW_PERIOD,
    STRATEGY_NAME as FIRST_HALF_DRAW_STRATEGY_NAME,
    settle_first_half_draw,
)


class RecoverableDemoError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class BrowserStartError(RuntimeError):
    code = "BROWSER_START_FAILED"


class HistoryClearBlockedError(RuntimeError):
    code = "HISTORY_CLEAR_BLOCKED_ACTIVE_BET"


class DatabaseClearBlockedError(RuntimeError):
    code = "DATABASE_CLEAR_BLOCKED_ACTIVE_BET"


class ModeConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlockedMatchSwitch:
    """An unplaced NEXT_GOAL attempt that must continue on another match."""

    match_id: str
    step: int
    amount: float


@dataclass
class DemoBlockedWindow:
    """Read-only DEMO placement window after this match already had a ready market."""

    initial_score: Score
    blocked_score_before: Score
    selected_side: Scorer
    selected_team: str
    step: int
    stake: float
    match_id: str
    market_was_ready: bool = True
    blocked_window_active: bool = False
    protection_active: bool = True
    started_after_settlement: bool = False


DEMO_BLOCKED_MARKET_STATUSES = frozenset(
    {"MARKET_LOCKED", "MARKET_NOT_FOUND", "ODDS_NOT_FOUND"}
)

DEMO_ACCEPTANCE_CONFIRMATIONS = 3
DEMO_ACCEPTANCE_INTERVAL_SECONDS = 0.12


class MissedSelectedTeamGoal(RuntimeError):
    def __init__(self, snapshot: ScoreboardSnapshot) -> None:
        super().__init__("MISSED_SELECTED_TEAM_GOAL")
        self.snapshot = snapshot


def strategy_market_presentation(strategy_type: StrategyType) -> tuple[str, str | None]:
    if strategy_type == StrategyType.FIRST_HALF_DRAW:
        return FIRST_HALF_DRAW_MARKET_NAME, FIRST_HALF_DRAW_SELECTION
    return strategy_type.display_name, None


def local_now() -> str:
    return datetime.now().astimezone().isoformat()


def next_goal_match_identity(match: dict[str, Any]) -> str:
    return str(match.get("match_id") or match.get("href") or match.get("url") or "")


def selected_team_scored_between(
    attempt_score: Score,
    current_score: Score,
    selected_side: Scorer,
) -> bool:
    if selected_side == Scorer.TEAM_1:
        return current_score.team1 > attempt_score.team1
    if selected_side == Scorer.TEAM_2:
        return current_score.team2 > attempt_score.team2
    return False


def selected_team_scored(
    attempt_score: Score,
    current_score: Score,
    selected_side: Scorer,
) -> bool:
    """Backward-compatible name shared by the LIVE blocked flow."""
    return selected_team_scored_between(
        attempt_score,
        current_score,
        selected_side,
    )


class DemoEngine:
    def __init__(self, browser_manager: BrowserManager = BROWSER_MANAGER) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._market_lock = asyncio.Lock()
        self.browser_manager = browser_manager
        self.browser_manager.set_logger(REPOSITORY.log)
        self._authorized_generation = -1
        self._auth_status = "UNKNOWN"
        self._budget = DemoBudget()
        self._config = DEFAULT_STRATEGY_CONFIG
        self._mode = "DEMO"
        self.live_executor = LiveExecutor(REPOSITORY.log)
        self._pending_live_bet: PendingLiveBet | None = None
        self._active_live_bet: ActiveLiveBet | None = None
        self._live_attempt_counter = 0
        self._current_series: CurrentSeries | None = None
        self._active_demo_protection: DemoBlockedWindow | None = None
        self._active_bet_lock_context: dict[str, Any] | None = None

    async def restore(self) -> None:
        """Hydrate the in-memory dashboard from durable storage after a restart."""
        await REPOSITORY.initialize()
        config_data = await REPOSITORY.get_config()
        self._config = StrategyConfig.from_payload(config_data)
        budget = await REPOSITORY.get_budget()
        self._budget.restore(budget["initial_budget"], budget["current_budget"])
        sequence = await REPOSITORY.get_sequence()
        active_demo = await REPOSITORY.active_bet("DEMO")
        active_live = await REPOSITORY.verified_active_live_bet()
        unresolved_live = await REPOSITORY.unresolved_live_submission()
        active = active_demo or active_live or unresolved_live
        if active is not None:
            # An interrupted ACTIVE bet is retained as history and never duplicated.
            sequence = await REPOSITORY.save_sequence(status="RECOVERY_REQUIRED")
        elif sequence["status"] == "RECOVERY_REQUIRED":
            # LIVE used the same singleton sequence in an older build. Once LIVE
            # is removed, its orphaned recovery marker must not block DEMO.
            sequence = await REPOSITORY.reset_sequence()
            await REPOSITORY.log(
                "STALE_RECOVERY_CLEARED",
                "RECOVERY_REQUIRED had no ACTIVE DEMO bet; DEMO runtime was reset",
            )
        await STATE.restore(budget=budget, strategy_config=config_data, sequence=sequence, stats=await REPOSITORY.stats())
        if active is not None:
            await STATE.update(
                mode=str(active.get("mode") or "DEMO").upper(),
                status=DemoStatus.RECOVERING.value,
                message="Ставка или LIVE-отправка ожидает reconciliation после рестарта",
                bet={**active, "max_steps": self._config.max_steps},
            )

    async def save_strategy_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise ValueError("Stop the strategy before changing its settings")
        saved = await REPOSITORY.save_config(payload)
        self._config = StrategyConfig.from_payload(saved)
        sequence = await REPOSITORY.get_sequence()
        if int(sequence["current_step"]) > self._config.max_steps:
            sequence = await REPOSITORY.reset_sequence()
        await STATE.restore(budget=await REPOSITORY.get_budget(), strategy_config=saved, sequence=sequence, stats=await REPOSITORY.stats())
        return saved

    async def reset_sequence(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise ValueError("Stop the strategy before resetting the sequence")
        sequence = await REPOSITORY.reset_sequence()
        await self.restore()
        await STATE.update(status=DemoStatus.STOPPED.value, event="SEQUENCE_RESET", message="Новая серия готова")
        return await STATE.snapshot()

    async def clear_history(self) -> dict[str, Any]:
        async with self._control_lock:
            worker_running = self._task is not None and not self._task.done()
            active = await REPOSITORY.active_bet("DEMO")
            if worker_running or active is not None:
                await REPOSITORY.log(
                    HistoryClearBlockedError.code,
                    "DEMO history clear rejected while worker or ACTIVE bet exists",
                )
                raise HistoryClearBlockedError(
                    "Остановите worker и завершите ACTIVE DEMO-ставку перед очисткой истории."
                )
            deleted = await REPOSITORY.clear_bet_history("DEMO")
            stats = await REPOSITORY.stats()
            await STATE.update(stats=stats)
            await REPOSITORY.log("HISTORY_CLEARED", f"Удалено DEMO-ставок: {deleted}")
            return {
                "ok": True,
                "deleted": deleted,
                "items": [],
                "state": await STATE.snapshot(),
            }

    async def clear_database(self) -> dict[str, Any]:
        async with self._control_lock:
            worker_running = self._task is not None and not self._task.done()
            active = await REPOSITORY.active_bet("DEMO") or await REPOSITORY.active_bet("LIVE")
            if worker_running or active is not None:
                await REPOSITORY.log(
                    DatabaseClearBlockedError.code,
                    "Database clear rejected while worker or ACTIVE DEMO bet exists",
                )
                raise DatabaseClearBlockedError(
                    "Остановите worker и завершите ACTIVE DEMO-ставку перед очисткой базы."
                )
            await REPOSITORY.reset_database()
            config_data = await REPOSITORY.get_config()
            self._config = StrategyConfig.from_payload(config_data)
            budget = await REPOSITORY.get_budget()
            self._budget.restore(budget["initial_budget"], budget["current_budget"])
            sequence = await REPOSITORY.get_sequence()
            stats = await REPOSITORY.stats()
            await STATE.reset_after_database_clear(
                browser=await self.browser_manager.snapshot(),
                budget=budget,
                strategy_config=config_data,
                sequence=sequence,
                stats=stats,
            )
            await REPOSITORY.log("DATABASE_CLEARED", "DEMO SQLite reset to initial state")
            return {
                "ok": True,
                "items": [],
                "state": await STATE.snapshot(),
            }

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def mode(self) -> str:
        return self._mode

    async def start(self, mode: str = "DEMO") -> dict[str, Any]:
        requested_mode = mode.strip().upper()
        if requested_mode not in {"DEMO", "LIVE"}:
            raise ValueError(f"Неизвестный режим: {mode}")
        async with self._control_lock:
            await REPOSITORY.log(f"{requested_mode}_START_REQUEST", f"Получен запрос запуска {requested_mode}")
            if self._task is not None and not self._task.done():
                if self._mode != requested_mode:
                    raise ModeConflictError(f"Уже запущен режим {self._mode}.")
                await REPOSITORY.log(f"{requested_mode}_ALREADY_RUNNING", "Второй worker не создан")
                await STATE.update(event=f"{requested_mode}_ALREADY_RUNNING")
                return await STATE.snapshot()
            if self._task is not None and self._task.done():
                self._task = None

            CONFIG.validate()
            await self.restore()
            sequence = await REPOSITORY.get_sequence()
            if requested_mode == "LIVE":
                unresolved_live = await REPOSITORY.verified_active_live_bet()
                if unresolved_live is None:
                    unresolved_live = await REPOSITORY.unresolved_live_submission()
                if unresolved_live is not None:
                    raise ModeConflictError(
                        "Найдена незавершённая LIVE-ставка или отправка; повторный запуск заблокирован."
                    )
            if sequence["status"] in {"SEQUENCE_EXHAUSTED", "RECOVERY_REQUIRED"}:
                await STATE.update(
                    status=DemoStatus.SEQUENCE_EXHAUSTED.value if sequence["status"] == "SEQUENCE_EXHAUSTED" else DemoStatus.RECOVERING.value,
                    event=sequence["status"],
                    message="Серия требует явного сброса или reconciliation ACTIVE ставки",
                )
                return await STATE.snapshot()
            self._stop_event.clear()
            self._authorized_generation = -1
            self._auth_status = "UNKNOWN"
            self._mode = requested_mode
            self._current_series = None
            if requested_mode == "LIVE":
                self.live_executor.reset()
                self._pending_live_bet = None
                self._active_live_bet = None
            await REPOSITORY.log(
                "MATCH_FILTERS_CONFIG",
                f"exclude_teams_enabled={str(self._config.exclude_teams_enabled).lower()} "
                f"min_initial_odds_enabled={str(self._config.min_initial_odds_enabled).lower()} "
                f"blocked_events_switch_enabled={str(self._config.blocked_events_switch_enabled).lower()} "
                f"max_three_steps_enabled={str(self._config.max_three_steps_enabled).lower()} "
                f"min_initial_odds={MIN_INITIAL_SELECTED_ODDS}",
            )
            await REPOSITORY.log(
                "STRATEGY_STARTED",
                f"{self._config.strategy_type.value} / {self._config.strategy_type.display_name}",
            )
            await REPOSITORY.save_sequence(status="WAITING_FOR_MATCH")
            await STATE.reset_for_start(await REPOSITORY.stats())
            market_name, market_selection = strategy_market_presentation(
                self._config.strategy_type
            )
            await STATE.update(
                strategy_type=self._config.strategy_type.value,
                strategy_name=self._config.strategy_type.display_name,
                market_name=market_name,
                market_selection=market_selection,
                strategy_config=(await REPOSITORY.get_config()),
                sequence=(await REPOSITORY.get_sequence()),
            )
            try:
                await REPOSITORY.log("BROWSER_STARTING", "Проверяем Playwright/browser/context/page")
                await self.browser_manager.ensure_page()
            except Exception as error:
                trace = traceback.format_exc()
                await REPOSITORY.log(
                    BrowserStartError.code,
                    f"{type(error).__name__}: {error}\n{trace}",
                )
                await REPOSITORY.log(
                    f"{requested_mode}_START_FAILED",
                    f"{type(error).__name__}: {error}",
                )
                await STATE.update(
                    running=False,
                    status=DemoStatus.ERROR.value,
                    message=f"Не удалось запустить браузер для {requested_mode}",
                    error=f"{type(error).__name__}: {error}",
                    event=BrowserStartError.code,
                    browser=await self.browser_manager.snapshot(),
                )
                raise BrowserStartError(str(error)) from error
            await REPOSITORY.log("BROWSER_OPENED", "Playwright browser/context/page готовы")
            market_name, market_selection = strategy_market_presentation(
                self._config.strategy_type
            )
            await STATE.update(
                status=DemoStatus.STARTING.value,
                mode=requested_mode,
                message=f"Запуск {requested_mode} worker",
                event=f"{requested_mode}_START",
                browser=await self.browser_manager.snapshot(),
                budget=self._budget.snapshot(),
                strategy_config=(await REPOSITORY.get_config()),
                sequence=(await REPOSITORY.get_sequence()),
                strategy_type=self._config.strategy_type.value,
                strategy_name=self._config.strategy_type.display_name,
                market_name=market_name,
                market_selection=market_selection,
            )
            await REPOSITORY.log(f"{requested_mode}_START", f"Запущен background {requested_mode} worker")
            self._task = asyncio.create_task(self._run_guarded(), name=f"{requested_mode.lower()}-worker")
            await REPOSITORY.log("WORKER_STARTED", f"Создан единственный asyncio {requested_mode} worker")
            await REPOSITORY.log(f"{requested_mode}_RUNNING", f"{requested_mode} worker и Playwright запущены")
            await STATE.update(event=f"{requested_mode}_RUNNING", message=f"{requested_mode} worker запущен")
            return await STATE.snapshot()

    async def stop(self, mode: str | None = None) -> dict[str, Any]:
        if mode is not None and self._task is not None and not self._task.done() and self._mode != mode.upper():
            raise ModeConflictError(f"Сейчас запущен режим {self._mode}.")
        current_mode = self._mode
        await REPOSITORY.log(f"{current_mode}_STOP_REQUEST", f"Получен запрос остановки {current_mode}")
        async with self._control_lock:
            self._stop_event.set()
            task = self._task

        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.75)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        async with self._control_lock:
            if self._task is task:
                self._task = None
        await REPOSITORY.log("WORKER_STOPPED", f"asyncio {current_mode} worker остановлен")
        await REPOSITORY.log(f"{current_mode}_STOP", f"{current_mode} worker остановлен; browser не закрывался")
        await STATE.update(
            running=False,
            status=DemoStatus.STOPPED.value,
            message=f"{current_mode} остановлен",
            event="STOPPED",
            browser=await self.browser_manager.snapshot(),
        )
        return await STATE.snapshot()

    async def canvas_debug(self) -> dict[str, Any]:
        try:
            page = await self.browser_manager.ensure_page()
        except Exception:
            cached = load_latest_analysis()
            if cached is not None:
                return {**cached, "cached": True}
            return {
                "ok": False,
                "status": "NO_ACTIVE_MARKET_CANVAS",
                "error": "Browser и сохранённая диагностика недоступны.",
            }
        async with self._market_lock:
            return await market_canvas_debug(page, REPOSITORY.log)

    async def _run_guarded(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # Only truly fatal programming/config errors arrive here.
            trace = traceback.format_exc()
            await REPOSITORY.log(
                "FATAL_ERROR",
                f"{type(error).__name__}: {error}\n{trace}",
            )
            await STATE.update(
                running=False,
                status=DemoStatus.ERROR.value,
                message=f"{self._mode} worker остановлен из-за fatal error",
                error=f"{type(error).__name__}: {error}",
                event="FATAL_ERROR",
                browser=await self.browser_manager.snapshot(),
            )
        finally:
            if self._stop_event.is_set():
                await STATE.update(running=False)

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                page = await self.browser_manager.ensure_page()
                await STATE.update(browser=await self.browser_manager.snapshot())
                if not await self._ensure_authorized(page):
                    continue
                await self._process_next_match(page)
            except asyncio.CancelledError:
                raise
            except RecoverableDemoError as error:
                await self._recover(error.status, str(error))
            except (PlaywrightTimeoutError, ScoreReadError) as error:
                await self._recover("ELEMENT_NOT_READY", str(error))
            except Exception as error:
                trace = traceback.format_exc()
                await REPOSITORY.log(
                    "WORKER_EXCEPTION",
                    f"{type(error).__name__}: {error}\n{trace}",
                )
                await self._recover("RECOVERING", f"{type(error).__name__}: {error}")

    async def _ensure_authorized(self, page: Page) -> bool:
        generation = self.browser_manager.generation
        if generation == self._authorized_generation:
            await STATE.update(auth={"status": self._auth_status})
            return True

        async def publish_waiting_status() -> None:
            await STATE.update(auth={"status": "WAITING_MANUAL_LOGIN"})
            await self._status(
                DemoStatus.WAITING_MANUAL_LOGIN,
                "Браузер открыт. Войдите на сайте вручную.",
                "WAITING_MANUAL_LOGIN",
            )
            await REPOSITORY.log(
                "WAITING_MANUAL_LOGIN",
                "Browser is open; waiting for the user to sign in manually",
            )

        async def publish_authorized_status() -> None:
            await STATE.update(auth={"status": "AUTHORIZED"})
            await self._status(
                DemoStatus.AUTHORIZED,
                "Авторизация выполнена",
                "AUTHORIZED",
            )
            await REPOSITORY.log(
                "AUTHORIZED",
                "Authorized browser session is ready",
            )

        await self._status(DemoStatus.AUTH_CHECK, "Проверяем авторизацию", "AUTH_CHECK")
        result = await authorize(
            page,
            self._stop_event,
            publish_waiting_status,
            publish_authorized_status,
        )
        if self._stop_event.is_set():
            return False
        status = result.get("status", "WAITING_MANUAL_LOGIN")
        await STATE.update(auth={"status": status})
        if not result.get("ok"):
            return False
        self._authorized_generation = generation
        self._auth_status = status
        if status == "AUTH_TIMEOUT":
            await self._status(
                DemoStatus.AUTH_TIMEOUT,
                "Авторизация не подтверждена за 40 секунд. Продолжаем работу.",
                "AUTH_TIMEOUT",
            )
            await REPOSITORY.log(
                "AUTH_TIMEOUT",
                "RUB was not detected; continuing the existing flow",
            )
        return True

    async def _process_next_match(self, page: Page) -> None:
        if self._config.strategy_type == StrategyType.FIRST_HALF_DRAW:
            await self._process_first_half_draw_match(page)
            return
        await self._process_next_goal_match(page)

    async def _process_next_goal_match(self, page: Page) -> None:
        page = await self.browser_manager.ensure_page()
        league = LeagueBrowser(
            page,
            exclude_teams_enabled=self._config.exclude_teams_enabled,
        )
        await self._status(DemoStatus.OPENING_LEAGUE, "Открываем страницу лиги", "LEAGUE_OPENING")
        await league.open()
        await REPOSITORY.log("LEAGUE_OPENED", CONFIG.league_name)
        sequence = await REPOSITORY.get_sequence()
        blocked_match_ids = (
            set(sequence.get("blocked_match_ids") or [])
            if (
                self._config.blocked_events_switch_enabled
                or self._config.max_three_steps_enabled
            )
            else set()
        )
        logged_blocked_skips: set[str] = set()
        await STATE.update(
            selected_team=None,
            selected_side=None,
            initial_selected_odds=None,
            other_team=None,
            selection_reason=None,
            bet={
                "step": int(sequence["current_step"]) if blocked_match_ids else 0,
                "max_steps": self._config.max_steps,
                "amount": (
                    float(self._config.stakes[int(sequence["current_step"]) - 1])
                    if blocked_match_ids
                    else None
                ),
                "market": "Следующий гол",
                "odds": None,
                "score_before": None,
                "next_goal_number": None,
                "status": "WAITING_NEXT_MATCH" if blocked_match_ids else "WAITING",
            },
            budget=self._budget.snapshot(),
        )

        selected_match = None
        while selected_match is None and not self._stop_event.is_set():
            page = await self.browser_manager.ensure_page()
            league = LeagueBrowser(
                page,
                exclude_teams_enabled=self._config.exclude_teams_enabled,
            )
            await self._status(
                DemoStatus.SCANNING_MATCHES,
                "Считываем актуальный DOM списка матчей",
                "MATCHES_REFRESHING",
            )
            matches = await league.scan()
            stats = league.last_scan_stats
            await REPOSITORY.log("MATCHES_FOUND", str(stats["total"]))
            await REPOSITORY.log(
                "MATCHES_FILTERED",
                f"Всего: {stats['total']}; Started: {stats['started']}; "
                f"Finished: {stats.get('finished', 0)}; "
                f"Excluded: {stats.get('excluded', 0)}; "
                f"Unclassified: {stats.get('unclassified', 0)}; "
                f"Upcoming: {stats['upcoming']}",
            )
            for item in league.skipped_started:
                await REPOSITORY.log(
                    "MATCH_SKIPPED_STARTED",
                    f"{item['team1']} — {item['team2']} / period={item['period']}",
                )
            for item in getattr(league, "skipped_excluded", []):
                await REPOSITORY.log(
                    "MATCH_SKIPPED_EXCLUDED_TEAM",
                    f"{item['team1']} — {item['team2']}: match excluded by team filter "
                    f"({item['excluded_team']})",
                )
            for item in getattr(league, "skipped_unclassified", []):
                await REPOSITORY.log(
                    "MATCH_UNCLASSIFIED",
                    (
                        f"{item.get('team1', '<не прочитано>')} — "
                        f"{item.get('team2', '<не прочитано>')} / "
                        f"time={item.get('time')!r}; period={item.get('period')!r}; "
                        f"reason={item.get('reason', 'unknown')}"
                    ),
                )
            if stats.get("unclassified", 0):
                await REPOSITORY.log(
                    "MATCH_SCAN_INCOMPLETE",
                    (
                        "Есть карточки матча в промежуточном DOM-состоянии; "
                        "не выбираем более поздний матч, повторяем сканирование"
                    ),
                )
                await STATE.update(scanner={**stats, "selected": None})
                await self._sleep_or_stop(CONFIG.league_retry_interval)
                continue
            allowed_matches = []
            for item in matches:
                excluded_team = excluded_team_in_match(
                    item["team1"],
                    item["team2"],
                    enabled=self._config.exclude_teams_enabled,
                )
                if excluded_team is not None:
                    await REPOSITORY.log(
                        "MATCH_SKIPPED_EXCLUDED_TEAM",
                        f"{item['team1']} — {item['team2']}: match excluded by team filter "
                        f"({excluded_team})",
                    )
                    continue
                allowed_matches.append(item)
            matches = allowed_matches
            if blocked_match_ids:
                unblocked_matches = []
                for item in matches:
                    match_id = next_goal_match_identity(item)
                    if match_id in blocked_match_ids:
                        if match_id not in logged_blocked_skips:
                            await REPOSITORY.log(
                                "NEXT_GOAL_BLOCKED_MATCH_SKIPPED",
                                f"[NEXT_GOAL] Current sequence excludes match_id={match_id}",
                            )
                            logged_blocked_skips.add(match_id)
                        continue
                    unblocked_matches.append(item)
                matches = unblocked_matches
            for item in matches:
                await REPOSITORY.log(
                    "MATCH_CANDIDATE",
                    f"{item['team1']} — {item['team2']} / time={item['time']}",
                )
            await STATE.update(
                scanner={**stats, "selected": matches[0] if matches else None}
            )
            if matches:
                selected_match = matches[0]
                break
            if blocked_match_ids:
                await self._status(
                    DemoStatus.WAITING_NEXT_MATCH,
                    "Текущий матч исключён из серии — ждём следующий подходящий матч",
                    "BLOCKED_WAITING_NEXT_MATCH",
                    sequence=sequence,
                )
            else:
                await self._status(
                    DemoStatus.NO_UPCOMING_MATCHES,
                    "Предстоящих матчей пока нет",
                    "NO_UPCOMING_MATCHES",
                )
            await self._sleep_or_stop(CONFIG.league_retry_interval)

        if selected_match is None or self._stop_event.is_set():
            return

        sequence = await REPOSITORY.get_sequence()
        cycle_id = sequence["sequence_id"]
        max_three_switch_used = bool(
            int(sequence.get("max_three_switched") or 0)
        )
        match_name = f"{selected_match['team1']} — {selected_match['team2']}"
        match_state = {
            "id": selected_match.get("match_id"),
            "team1": selected_match["team1"],
            "team2": selected_match["team2"],
            "score1": None,
            "score2": None,
            "timer": selected_match.get("time") or "",
            "period": selected_match.get("period") or "",
            "url": selected_match.get("url"),
            "state": "UPCOMING",
        }
        await self._status(
            DemoStatus.MATCH_SELECTED,
            f"Выбран ближайший матч: {match_name}",
            "MATCH_SELECTED",
            match=match_state,
        )
        await REPOSITORY.log(
            "MATCH_SELECTED", f"{match_name} / {selected_match.get('time', '')}"
        )
        if blocked_match_ids:
            step = int(sequence["current_step"])
            amount = float(self._config.stakes[step - 1])
            await REPOSITORY.log(
                "NEXT_GOAL_BLOCKED_CONTINUING",
                f"[NEXT_GOAL] Continuing step={step} stake={amount:g}",
            )
            await REPOSITORY.log(
                "NEXT_GOAL_BLOCKED_NEXT_MATCH_SELECTED",
                f"[NEXT_GOAL] Next match selected: {match_name}",
            )
            if (
                self._config.max_three_steps_enabled
                and max_three_switch_used
            ):
                await REPOSITORY.log(
                    "MAX_3_STEPS_CONTINUING_TO_END",
                    (
                        f"Второй матч после лимита max 3: продолжаем догон "
                        f"с шага {step} до WIN или конца ряда; "
                        f"stake={amount:g}; match={match_name}; "
                        "повторного переключения по max 3 не будет"
                    ),
                )
        await self._status(DemoStatus.OPENING_MATCH, "Открываем выбранный матч", "OPEN_MATCH")
        try:
            opened = await league.open_match(selected_match)
        except MatchAlreadyStarted as error:
            await REPOSITORY.log("MATCH_ALREADY_STARTED", str(error))
            return
        await REPOSITORY.log("MATCH_OPENED", opened["url"])

        readiness_elapsed_seconds = 0.0
        readiness_waiter = getattr(league, "wait_match_content_ready", None)
        if callable(readiness_waiter):
            await REPOSITORY.log(
                "MATCH_CONTENT_WAIT",
                "URL матча открыт; ждём scoreboard и фактически отрисованную зону рынков (до 30 секунд)",
            )
            try:
                ready = await readiness_waiter()
            except MatchContentLoadTimeout as error:
                details = error.details
                await REPOSITORY.log(
                    "MATCH_CONTENT_LOAD_TIMEOUT",
                    (
                        f"{error}; url={details.get('url')}; "
                        f"scoreboard_ready={details.get('scoreboard_ready')}; "
                        f"market_ready={details.get('market_ready')}; "
                        f"market_selector={details.get('market_selector')}; "
                        f"attempts={details.get('attempts')}; "
                        f"last_score_error={details.get('last_score_error')}"
                    ),
                )
                await self._status(
                    DemoStatus.MATCH_SKIPPED,
                    "Матч не дорисовался за 30 секунд; возвращаемся к выбору матча",
                    "MATCH_CONTENT_LOAD_TIMEOUT",
                )
                return

            readiness_elapsed_seconds = max(
                0.0,
                float(ready.get("elapsed_ms") or 0) / 1000.0,
            )
            await REPOSITORY.log(
                "MATCH_CONTENT_READY",
                (
                    f"score={ready.get('score')}; period={ready.get('period') or 'UPCOMING'}; "
                    f"market_selector={ready.get('market_selector')}; "
                    f"elapsed_ms={ready.get('elapsed_ms')}; attempts={ready.get('attempts')}"
                ),
            )

        # Сохраняем исходное правило: первая ставка не раньше чем через 10 секунд
        # после открытия нового матча. Время, уже потраченное readiness-gate,
        # засчитываем в эти 10 секунд, чтобы не добавлять лишнюю задержку.
        remaining_new_match_delay = max(0.0, 10.0 - readiness_elapsed_seconds)
        await REPOSITORY.log(
            "NEW_MATCH_BET_DELAY",
            (
                f"{match_name}: до первой ставки осталось "
                f"{remaining_new_match_delay:.2f} сек из исходных 10 сек"
            ),
        )
        await STATE.update(
            message=(
                f"Новый матч открыт. До первой ставки осталось "
                f"{remaining_new_match_delay:.2f} сек: {match_name}"
            ),
            event="NEW_MATCH_BET_DELAY",
        )

        await self._sleep_or_stop(remaining_new_match_delay)

        if self._stop_event.is_set():
            return

        snapshot = await self._wait_for_initial_zero_score(selected_match)
        if snapshot is None:
            return
        excluded_team = excluded_team_in_match(
            snapshot.team1,
            snapshot.team2,
            enabled=self._config.exclude_teams_enabled,
        )
        if excluded_team is not None:
            message = (
                f"{snapshot.team1} — {snapshot.team2}: match excluded by team filter "
                f"({excluded_team})"
            )
            await REPOSITORY.log("MATCH_SKIPPED_EXCLUDED_TEAM", message)
            await self._status(
                DemoStatus.MATCH_SKIPPED,
                message,
                "MATCH_SKIPPED_EXCLUDED_TEAM",
            )
            return
        odds_result = await self._wait_for_odds(snapshot, selected_match)
        if odds_result is None:
            return
        snapshot, initial_odds = odds_result
        if not can_create_initial_bet(snapshot.score):
            await REPOSITORY.log(
                "INITIAL_BET_SKIPPED",
                f"Счёт изменился до создания первой ставки: {snapshot.score.text()}",
            )
            return
        selection = select_team_with_higher_odds(snapshot.team1, snapshot.team2, initial_odds)
        if not is_initial_odds_allowed(
            selection.selected_odds,
            enabled=self._config.min_initial_odds_enabled,
        ):
            message = (
                f"{snapshot.team1} — {snapshot.team2}: "
                f"selected_team={selection.selected_team} "
                f"selected_odds={selection.selected_odds} "
                f"minimum={MIN_INITIAL_SELECTED_ODDS}"
            )
            await REPOSITORY.log("MATCH_SKIPPED_LOW_INITIAL_ODDS", message)
            await self._status(
                DemoStatus.MATCH_SKIPPED,
                message,
                "MATCH_SKIPPED_LOW_INITIAL_ODDS",
                odds=self._odds_state(
                    initial_odds, selection.selected_odds, selection.other_odds
                ),
            )
            return
        await STATE.update(
            status=DemoStatus.TEAM_SELECTED.value,
            message=f"Выбрана команда {selection.selected_team}: коэффициент выше",
            event="TEAM_SELECTED",
            selected_team=selection.selected_team,
            selected_side=selection.selected_side.value,
            initial_selected_odds=selection.selected_odds,
            other_team=selection.other_team,
            selection_reason="HIGHER_ODDS",
            odds=self._odds_state(initial_odds, selection.selected_odds, selection.other_odds),
        )
        await REPOSITORY.log(
            "TEAM_SELECTED", f"{selection.selected_team} @ {selection.selected_odds} / HIGHER_ODDS"
        )
        await REPOSITORY.save_sequence(
            status="PENDING" if self._mode == "LIVE" else "ACTIVE",
            current_match_id=selected_match.get("match_id"),
            selected_team=selection.selected_team,
        )

        won = False
        ambiguous_cycle = False
        losses_in_current_match = 0
        start_step = int(sequence["current_step"])
        pending_demo_protection: DemoBlockedWindow | None = None
        self._current_series = CurrentSeries(
            cycle_id=cycle_id,
            match_id=selected_match.get("match_id"),
            match_url=selected_match.get("url"),
            team_1=snapshot.team1,
            team_2=snapshot.team2,
            selected_team=selection.selected_team,
            selected_side=selection.selected_side,
            current_step=start_step,
        )
        if sequence["status"] == "SEQUENCE_EXHAUSTED":
            await self._status(DemoStatus.SEQUENCE_EXHAUSTED, "Серия исчерпана; выполните явный сброс", "SEQUENCE_EXHAUSTED")
            return
        for step in range(start_step, self._config.max_steps + 1):
            self._current_series.assert_identity(
                selected_match.get("match_id"), selection.selected_team
            )
            self._current_series.current_step = step
            amount = float(self._config.stakes[step - 1])
            if self._stop_event.is_set():
                return
            if pending_demo_protection is not None:
                if pending_demo_protection.step != step:
                    raise RuntimeError("DEMO_NEXT_STEP_PROTECTION_STEP_MISMATCH")
                demo_blocked_window = pending_demo_protection
                pending_demo_protection = None
            else:
                demo_blocked_window = (
                    DemoBlockedWindow(
                        initial_score=snapshot.score,
                        blocked_score_before=snapshot.score,
                        selected_side=selection.selected_side,
                        selected_team=selection.selected_team,
                        step=step,
                        stake=amount,
                        match_id=next_goal_match_identity(selected_match),
                    )
                    if self._mode == "DEMO"
                    and self._config.blocked_events_switch_enabled
                    else None
                )
            self._active_demo_protection = demo_blocked_window
            if self._mode == "LIVE":
                self._pending_live_bet = PendingLiveBet(
                    decision_id=f"{selected_match.get('match_id')}_{selection.selected_side.value}_step{step}",
                    match_id=str(selected_match.get("match_id") or cycle_id),
                    team=selection.selected_team,
                    side=selection.selected_side,
                    strategy_step=step,
                    amount=amount,
                    target_goal_number=snapshot.score.team1 + snapshot.score.team2 + 1,
                )
            if step == 1:
                current_odds = initial_odds
            else:
                try:
                    odds_result = await (
                        self._wait_for_odds(
                            snapshot,
                            selected_match,
                            demo_blocked_window=demo_blocked_window,
                        )
                        if demo_blocked_window is not None
                        else self._wait_for_odds(snapshot, selected_match)
                    )
                except MissedSelectedTeamGoal as missed:
                    if demo_blocked_window is None:
                        raise
                    await self._finish_demo_missed_selected_team_goal(
                        window=demo_blocked_window,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        snapshot=missed.snapshot,
                    )
                    self._current_series = None
                    return
                if odds_result is None:
                    return
                snapshot, current_odds = odds_result
            page = await self.browser_manager.ensure_page()
            browser = MatchBrowser(page)
            try:
                snapshot = await self._read_fresh_score(
                    browser,
                    selected_match,
                    snapshot,
                )
                if demo_blocked_window is not None:
                    await self._observe_demo_blocked_score(
                        demo_blocked_window,
                        snapshot,
                    )
            except MissedSelectedTeamGoal as missed:
                if demo_blocked_window is None:
                    raise
                await self._finish_demo_missed_selected_team_goal(
                    window=demo_blocked_window,
                    selected_match=selected_match,
                    cycle_id=cycle_id,
                    snapshot=missed.snapshot,
                )
                self._current_series = None
                return
            if step == 1 and not can_create_initial_bet(snapshot.score):
                await REPOSITORY.log(
                    "SCORE_CHANGED_BEFORE_BET",
                    f"Счёт изменился до ставки: {snapshot.score.text()}; сохраняем матч, команду и шаг 1",
                )
            while (
                current_odds.next_goal_number
                != snapshot.score.team1 + snapshot.score.team2 + 1
            ):
                await REPOSITORY.log(
                    "STALE_MARKET_IGNORED",
                    "Счёт изменился до создания ставки; читаем коэффициент нового гола.",
                )
                try:
                    odds_result = await (
                        self._wait_for_odds(
                            snapshot,
                            selected_match,
                            demo_blocked_window=demo_blocked_window,
                        )
                        if demo_blocked_window is not None
                        else self._wait_for_odds(snapshot, selected_match)
                    )
                except MissedSelectedTeamGoal as missed:
                    if demo_blocked_window is None:
                        raise
                    await self._finish_demo_missed_selected_team_goal(
                        window=demo_blocked_window,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        snapshot=missed.snapshot,
                    )
                    self._current_series = None
                    return
                if odds_result is None:
                    return
                snapshot, current_odds = odds_result
            while demo_blocked_window is not None:
                try:
                    pre_active = await self._read_fresh_score(
                        browser,
                        selected_match,
                        snapshot,
                    )
                    await self._observe_demo_blocked_score(
                        demo_blocked_window,
                        pre_active,
                    )
                except MissedSelectedTeamGoal as missed:
                    await self._finish_demo_missed_selected_team_goal(
                        window=demo_blocked_window,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        snapshot=missed.snapshot,
                    )
                    self._current_series = None
                    return
                snapshot = pre_active
                expected_goal = snapshot.score.team1 + snapshot.score.team2 + 1
                if current_odds.next_goal_number == expected_goal:
                    acceptance_ready = True
                    if snapshot.period:
                        if demo_blocked_window.started_after_settlement:
                            try:
                                (
                                    acceptance_ready,
                                    snapshot,
                                ) = await self._confirm_demo_bet_acceptance(
                                    page=page,
                                    browser=browser,
                                    selected_match=selected_match,
                                    current_odds=current_odds,
                                    window=demo_blocked_window,
                                    snapshot=snapshot,
                                )
                            except MissedSelectedTeamGoal as missed:
                                await self._finish_demo_missed_selected_team_goal(
                                    window=demo_blocked_window,
                                    selected_match=selected_match,
                                    cycle_id=cycle_id,
                                    snapshot=missed.snapshot,
                                )
                                self._current_series = None
                                return
                        else:
                            acceptance_ready = not await self._demo_prebet_canvas_is_blocked(
                                page,
                                current_odds,
                                demo_blocked_window,
                            )

                    if not acceptance_ready:
                        try:
                            odds_result = await self._wait_for_odds(
                                snapshot,
                                selected_match,
                                demo_blocked_window=demo_blocked_window,
                            )
                        except MissedSelectedTeamGoal as missed:
                            await self._finish_demo_missed_selected_team_goal(
                                window=demo_blocked_window,
                                selected_match=selected_match,
                                cycle_id=cycle_id,
                                snapshot=missed.snapshot,
                            )
                            self._current_series = None
                            return
                        if odds_result is None:
                            return
                        snapshot, current_odds = odds_result
                        continue
                    break
                await REPOSITORY.log(
                    "DEMO_PRE_ACTIVE_SCORE_RECHECK",
                    f"[NEXT_GOAL][DEMO] score changed before ACTIVE; reading fresh market №{expected_goal}",
                )
                try:
                    odds_result = await self._wait_for_odds(
                        snapshot,
                        selected_match,
                        demo_blocked_window=demo_blocked_window,
                    )
                except MissedSelectedTeamGoal as missed:
                    await self._finish_demo_missed_selected_team_goal(
                        window=demo_blocked_window,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        snapshot=missed.snapshot,
                    )
                    self._current_series = None
                    return
                if odds_result is None:
                    return
                snapshot, current_odds = odds_result
            selected_odd, opponent_odd = odds_for_selected_side(
                current_odds, selection.selected_side
            )
            score_before = snapshot.score
            created_at = local_now()
            bet_id = f"{cycle_id}:{step}:{score_before.text()}"
            selected_side_label = (
                "Команда 1" if selection.selected_side == Scorer.TEAM_1 else "Команда 2"
            )
            waiting_for_match_start = step == 1 and not snapshot.period
            active_status = (
                "WAITING_FOR_MATCH_START" if waiting_for_match_start else "ACTIVE"
            )
            active_record = {
                "id": bet_id,
                "mode": self._mode,
                "strategy_type": StrategyType.NEXT_GOAL.value,
                "strategy_name": StrategyType.NEXT_GOAL.display_name,
                "cycle_id": cycle_id,
                "match_id": selected_match.get("match_id"),
                "match": match_name,
                "selected_team": selection.selected_team,
                "selected_side": selection.selected_side.value,
                "side_label": selected_side_label,
                "step": step,
                "amount": amount,
                "odds": selected_odd,
                "score_before": score_before.text(),
                "score_after": None,
                "scorer": None,
                "result": "ACTIVE",
                "status": active_status,
                "settled": False,
                "market": current_odds.market,
                "next_goal_number": current_odds.next_goal_number,
                "created_at": created_at,
                "resolved_at": None,
                "budget_before": float(self._budget.current_budget),
                "budget_change": None,
                "budget_after": None,
            }
            if self._mode == "LIVE":
                placement = await self._prepare_live_until_placed(
                    browser=browser,
                    selected_match=selected_match,
                    selection=selection,
                    match_name=match_name,
                    cycle_id=cycle_id,
                    step=step,
                    amount=amount,
                    snapshot=snapshot,
                    current_odds=current_odds,
                    record=active_record,
                )
                if placement is None:
                    return
                if isinstance(placement, BlockedMatchSwitch):
                    self._current_series = None
                    return
                active_record, snapshot, current_odds, selected_odd, opponent_odd = placement
                score_before = snapshot.score
                created_at = active_record["created_at"]
                bet_id = active_record["id"]
                waiting_for_match_start = step == 1 and not snapshot.period
                active_status = "WAITING_FOR_MATCH_START" if waiting_for_match_start else "ACTIVE"
            await REPOSITORY.save_bet(active_record)
            await STATE.update(
                status=(
                    DemoStatus.WAITING_FOR_MATCH_START.value
                    if waiting_for_match_start
                    else (LiveStatus.ACTIVE.value if self._mode == "LIVE" else DemoStatus.BET_SIMULATED.value)
                ),
                message=f"{self._mode} BET #{step}: {amount} RUB @ {selected_odd}",
                event=f"{self._mode}_BET_CREATED",
                odds=self._odds_state(current_odds, selected_odd, opponent_odd),
                bet={
                    "id": bet_id,
                    "step": step,
                    "max_steps": self._config.max_steps,
                    "amount": amount,
                    "selected_team": selection.selected_team,
                    "selected_side": selection.selected_side.value,
                    "side_label": selected_side_label,
                    "match": match_name,
                    "market": current_odds.market,
                    "odds": selected_odd,
                    "score_before": score_before.text(),
                    "next_goal_number": current_odds.next_goal_number,
                    "status": (
                        "WAITING_FOR_MATCH_START"
                        if waiting_for_match_start
                        else "WAITING_FOR_GOAL"
                    ),
                    "created_at": created_at,
                    "budget_before": (
                        float(self._budget.current_budget)
                        if self._mode == "DEMO"
                        else None
                    ),
                },
                budget=self._budget.snapshot(),
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log(
                f"{self._mode}_BET_CREATED",
                f"#{step}: {selection.selected_team}, {amount} RUB @ {selected_odd}, score={score_before.text()}",
            )
            if demo_blocked_window is not None:
                demo_blocked_window.protection_active = False
                self._active_demo_protection = None
                if demo_blocked_window.started_after_settlement:
                    await REPOSITORY.log(
                        "NEXT_STEP_PROTECTION_COMPLETED",
                        f"step={step} stake={amount:g} score={score_before.text()}; "
                        f"boundary={self._mode}_BET_CREATED",
                    )

            monitor_odds = current_odds
            goal_seen_at_live_start: tuple[ScoreboardSnapshot, Scorer] | None = None
            if waiting_for_match_start:
                canvas_live_transition = (
                    str(getattr(current_odds, "source", "") or "") == "CANVAS_2D"
                )
                if canvas_live_transition:
                    try:
                        transition_page = await self.browser_manager.ensure_page()
                        await reset_canvas_2d_for_live_transition(
                            transition_page,
                            REPOSITORY.log,
                        )
                        await REPOSITORY.log(
                            "CANVAS_2D_WAITING_NATIVE_LIVE_BOOT",
                            (
                                f"bet_id={bet_id}; goal={current_odds.next_goal_number}; "
                                "Canvas hook cleared before UPCOMING -> LIVE"
                            ),
                        )
                    except Exception as error:
                        await REPOSITORY.log(
                            "CANVAS_2D_LIVE_TRANSITION_RESET_FAILED",
                            (
                                f"{type(error).__name__}: {error}; "
                                "continuing match-start wait without aborting active bet"
                            ),
                        )

                started_snapshot = await self._wait_for_match_start(selected_match)
                if started_snapshot is None:
                    return
                snapshot = started_snapshot

                # The first accepted bet belongs to goal №1 from score 0:0.
                # The bookmaker can expose the LIVE scoreboard only after that
                # goal has already happened. In that case the bet is already
                # resolved and MUST NOT be remapped to goal №2 or kept waiting
                # for the stale goal №1.
                live_start_scorer = detect_scorer(score_before, snapshot.score)
                if live_start_scorer != Scorer.UNKNOWN:
                    goal_seen_at_live_start = (snapshot, live_start_scorer)
                    await REPOSITORY.log(
                        "ACTIVE_BET_RESOLVED_DURING_LIVE_START",
                        (
                            f"bet_id={bet_id}; step={step}; accepted_goal="
                            f"{current_odds.next_goal_number}; "
                            f"score={score_before.text()}->{snapshot.score.text()}; "
                            f"scorer={live_start_scorer.value}; "
                            "score advanced before active-bet monitor started"
                        ),
                    )
                    await STATE.update(
                        event="ACTIVE_BET_RESOLVED_DURING_LIVE_START",
                        message=(
                            "Счёт изменился до запуска LIVE-монитора; "
                            "рассчитываем уже принятую ставку по фактическому голу"
                        ),
                    )

                if canvas_live_transition and goal_seen_at_live_start is None:
                    await REPOSITORY.log(
                        "CANVAS_2D_LIVE_BOOT_GRACE",
                        (
                            f"score={snapshot.score.text()}; waiting 750 ms for native "
                            "LIVE market hydration before reinstalling Canvas hook"
                        ),
                    )
                    await self._sleep_or_stop(0.75)
                    try:
                        transition_page = await self.browser_manager.ensure_page()
                        refreshed_monitor_odds = await read_next_goal_odds(
                            transition_page,
                            snapshot.team1,
                            snapshot.team2,
                            snapshot.score.team1,
                            snapshot.score.team2,
                            REPOSITORY.log,
                            read_only=True,
                        )
                        if (
                            refreshed_monitor_odds.next_goal_number
                            == current_odds.next_goal_number
                        ):
                            monitor_odds = refreshed_monitor_odds
                            await REPOSITORY.log(
                                "CANVAS_2D_LIVE_MONITOR_REMAPPED",
                                (
                                    f"bet_id={bet_id}; goal={current_odds.next_goal_number}; "
                                    f"score={snapshot.score.text()}; "
                                    "Canvas hook reinstalled after LIVE hydration; "
                                    "active-bet lock mapping refreshed"
                                ),
                            )
                        else:
                            await REPOSITORY.log(
                                "CANVAS_2D_LIVE_MONITOR_REMAP_STALE",
                                (
                                    f"bet_id={bet_id}; accepted_goal="
                                    f"{current_odds.next_goal_number}; live_goal="
                                    f"{refreshed_monitor_odds.next_goal_number}; "
                                    f"score={snapshot.score.text()}"
                                ),
                            )
                    except Exception as error:
                        await REPOSITORY.log(
                            "CANVAS_2D_LIVE_MONITOR_REMAP_FAILED",
                            (
                                f"{type(error).__name__}: {error}; "
                                "active bet remains valid; lock diagnostics may be unavailable "
                                "until the next market read"
                            ),
                        )

                if goal_seen_at_live_start is None:
                    await REPOSITORY.log(
                        "ACTIVE_BET_RESUMED",
                        f"existing bet_id={bet_id}",
                    )
                    current_state = await STATE.snapshot()
                    await STATE.update(
                        event="ACTIVE_BET_RESUMED",
                        bet={
                            **current_state.get("bet", {}),
                            "status": "WAITING_FOR_GOAL",
                        },
                    )

            if self._mode == "LIVE" and (
                self._active_live_bet is None
                or self._active_live_bet.attempt_id != bet_id
                or self._active_live_bet.strategy_step != step
            ):
                await REPOSITORY.log(
                    "LIVE_SETTLEMENT_BLOCKED",
                    "Изменение счёта нельзя оценивать без подтверждённой ACTIVE LIVE-ставки",
                )
                return
            if goal_seen_at_live_start is not None:
                goal = goal_seen_at_live_start
            else:
                self._active_bet_lock_context = {
                    "active_odds": monitor_odds,
                    "selected_side": selection.selected_side,
                    "step": step,
                    "bet_id": bet_id,
                }
                try:
                    goal = await self._wait_for_goal(
                        browser,
                        selected_match,
                        snapshot,
                    )
                finally:
                    self._active_bet_lock_context = None
                if goal is None:
                    return
            new_snapshot, scorer = goal
            common_record = {
                **active_record,
                "score_after": new_snapshot.score.text(),
                "resolved_at": local_now(),
                "status": "SETTLED",
                "settled": True,
            }

            if scorer == Scorer.AMBIGUOUS_SCORE_CHANGE:
                ambiguous_cycle = True
                record = await REPOSITORY.save_bet(
                    {
                        **common_record,
                        "scorer": "Не определён",
                        "result": "AMBIGUOUS",
                        "budget_change": 0,
                        "budget_after": float(self._budget.current_budget),
                    }
                )
                await REPOSITORY.log(
                    "AMBIGUOUS_SCORE_CHANGE",
                    f"{score_before.text()} → {new_snapshot.score.text()}",
                )
                await STATE.update(
                    event="AMBIGUOUS_SCORE_CHANGE",
                    last_change={
                        "before": record["score_before"],
                        "after": record["score_after"],
                        "scorer": "Не определён",
                        "result": "AMBIGUOUS",
                    },
                    stats=await REPOSITORY.stats(),
                    budget=self._budget.snapshot(),
                    bet={**record, "max_steps": self._config.max_steps},
                )
                snapshot = new_snapshot
                losses_in_current_match = 0
                if self._mode == "LIVE":
                    self._active_live_bet = None
                if step < self._config.max_steps:
                    next_step = step + 1
                    self._current_series.assert_identity(
                        selected_match.get("match_id"), selection.selected_team
                    )
                    self._current_series.current_step = next_step
                    await REPOSITORY.save_sequence(
                        current_step=next_step,
                        status="ACTIVE",
                    )
                    await self._status(
                        DemoStatus.NEXT_STEP,
                        f"Неоднозначный score delta; остаёмся в том же матче, шаг {next_step}",
                        "NEXT_STEP",
                    )
                    await self._publish_pending_bet(
                        selection,
                        match_name,
                        next_step,
                        new_snapshot,
                    )
                continue

            scorer_name = (
                new_snapshot.team1 if scorer == Scorer.TEAM_1 else new_snapshot.team2
            )
            result = "WIN" if scorer == selection.selected_side else "LOSE"
            if self._mode == "DEMO":
                budget_change = self._budget.settle(bet_id, result, amount, selected_odd)
                if budget_change is None:
                    await REPOSITORY.log(
                        "DEMO_BUDGET_DUPLICATE_IGNORED", f"bet_id={bet_id}"
                    )
                    snapshot = new_snapshot
                    continue
            else:
                budget_change = {
                    "budget_before": None,
                    "gross_return": None,
                    "pnl": 0.0,
                    "budget_change": None,
                    "budget_after": None,
                }
            record = await REPOSITORY.save_bet(
                {
                    **common_record,
                    "scorer": scorer_name,
                    "result": result,
                    **budget_change,
                }
            )
            if self._mode == "DEMO":
                await REPOSITORY.save_budget(self._budget.snapshot())
                sequence_after_result = await REPOSITORY.get_sequence()
                sequence_pnl = Decimal(str(sequence_after_result["cumulative_pnl"])) + Decimal(str(budget_change["pnl"]))
                sequence_losses = Decimal(str(sequence_after_result["cumulative_losses"]))
                if result == "LOSE":
                    sequence_losses += Decimal(str(amount))
                await REPOSITORY.save_sequence(
                    cumulative_pnl=str(sequence_pnl.quantize(Decimal("0.01"))),
                    cumulative_losses=str(sequence_losses.quantize(Decimal("0.01"))),
                )
            await REPOSITORY.log(
                "SCORE_CHANGED", f"{score_before.text()} → {new_snapshot.score.text()}"
            )
            await REPOSITORY.log("GOAL_DETECTED", scorer_name)
            await REPOSITORY.log(result, selection.selected_team)
            if self._mode == "DEMO":
                await REPOSITORY.log(
                    "DEMO_BUDGET",
                    f"result={result} stake={amount} before={budget_change['budget_before']} "
                    f"change={budget_change['budget_change']:+.2f} after={budget_change['budget_after']}",
                )
            await STATE.update(
                status=(DemoStatus.WIN if result == "WIN" else DemoStatus.LOSE).value,
                message=f"Результат {'виртуальной' if self._mode == 'DEMO' else 'LIVE'} ставки: {result}",
                event=result,
                last_change={
                    "before": record["score_before"],
                    "after": record["score_after"],
                    "scorer": scorer_name,
                    "result": result,
                },
                stats=await REPOSITORY.stats(),
                budget=self._budget.snapshot(),
                sequence=await REPOSITORY.get_sequence(),
                bet={**record, "max_steps": self._config.max_steps},
            )
            snapshot = new_snapshot
            if result == "LOSE":
                losses_in_current_match += 1
                await REPOSITORY.log(
                    "MATCH_CONSECUTIVE_LOSSES",
                    (
                        f"match={match_name}; losses_in_match={losses_in_current_match}; "
                        f"step={step}; max_three_steps_enabled="
                        f"{str(self._config.max_three_steps_enabled).lower()}"
                    ),
                )
            if self._mode == "LIVE":
                self._active_live_bet = None
            if result == "WIN":
                won = True
                self._current_series.assert_identity(
                    selected_match.get("match_id"), selection.selected_team
                )
                self._current_series.status = "FINISHED"
                await REPOSITORY.add_cycle(
                    {"cycle_id": cycle_id, "match": match_name, "mode": self._mode, "strategy_type": StrategyType.NEXT_GOAL.value, "result": "WIN", "steps": step}
                )
                await REPOSITORY.log("STRATEGY_CYCLE_WON", match_name)
                await REPOSITORY.reset_sequence()
                await STATE.update(stats=await REPOSITORY.stats(), sequence=await REPOSITORY.get_sequence())
                self._current_series = None
                break

            if (
                result == "LOSE"
                and self._config.max_three_steps_enabled
                and not max_three_switch_used
                and losses_in_current_match >= 3
                and step < self._config.max_steps
            ):
                await self._switch_match_after_three_losses(
                    selected_match=selected_match,
                    match_name=match_name,
                    cycle_id=cycle_id,
                    step=step,
                    losses_in_current_match=losses_in_current_match,
                )
                return

            if step < self._config.max_steps:
                next_step = step + 1
                self._current_series.assert_identity(
                    selected_match.get("match_id"), selection.selected_team
                )
                self._current_series.current_step = next_step
                await REPOSITORY.save_sequence(
                    current_step=next_step,
                    status="ACTIVE",
                    cumulative_pnl=str((await REPOSITORY.get_sequence())["cumulative_pnl"]),
                )
                if (
                    self._mode == "DEMO"
                    and self._config.blocked_events_switch_enabled
                ):
                    pending_demo_protection = DemoBlockedWindow(
                        initial_score=new_snapshot.score,
                        blocked_score_before=new_snapshot.score,
                        selected_side=selection.selected_side,
                        selected_team=selection.selected_team,
                        step=next_step,
                        stake=float(self._config.stakes[next_step - 1]),
                        match_id=next_goal_match_identity(selected_match),
                        started_after_settlement=True,
                    )
                    await REPOSITORY.log(
                        "NEXT_STEP_PROTECTION_STARTED",
                        f"step={next_step} stake={pending_demo_protection.stake:g} "
                        f"baseline={new_snapshot.score.text()} "
                        f"selected_team={selection.selected_team}",
                    )
                    self._active_demo_protection = pending_demo_protection
                try:
                    fresh_after_settlement = await self._read_fresh_score(
                        browser,
                        selected_match,
                        new_snapshot,
                    )
                    if pending_demo_protection is not None:
                        await self._observe_demo_blocked_score(
                            pending_demo_protection,
                            fresh_after_settlement,
                        )
                except MissedSelectedTeamGoal as missed:
                    if pending_demo_protection is None:
                        raise
                    await self._finish_demo_missed_selected_team_goal(
                        window=pending_demo_protection,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        snapshot=missed.snapshot,
                    )
                    self._current_series = None
                    return
                snapshot = fresh_after_settlement
                await self._status(
                    DemoStatus.NEXT_STEP, f"Переход к шагу {next_step}", "NEXT_STEP"
                )
                await self._publish_pending_bet(
                    selection,
                    match_name,
                    next_step,
                    snapshot,
                )

        if not won and not self._stop_event.is_set():
            if self._current_series is not None:
                self._current_series.assert_identity(
                    selected_match.get("match_id"), selection.selected_team
                )
                self._current_series.status = "SEQUENCE_EXHAUSTED"
            await REPOSITORY.add_cycle(
                {
                    "cycle_id": cycle_id,
                    "match": match_name,
                    "mode": self._mode,
                    "strategy_type": StrategyType.NEXT_GOAL.value,
                    "result": "SEQUENCE_EXHAUSTED",
                    "steps": self._config.max_steps,
                    "had_ambiguous": ambiguous_cycle,
                }
            )
            await self._status(
                DemoStatus.SEQUENCE_EXHAUSTED,
                "Все 7 шагов исчерпаны",
                "SEQUENCE_EXHAUSTED",
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log("SEQUENCE_EXHAUSTED", match_name)
            await REPOSITORY.save_sequence(current_step=self._config.max_steps, status="SEQUENCE_EXHAUSTED", current_match_id=selected_match.get("match_id"))
            self._current_series = None

        if not self._stop_event.is_set():
            await self._status(
                DemoStatus.RETURNING_TO_LEAGUE,
                "Возвращаемся в лигу и заново читаем DOM",
                "RETURNING_TO_LEAGUE",
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log("RETURNING_TO_LEAGUE", CONFIG.league_url)

    async def _process_first_half_draw_match(self, page: Page) -> None:
        """Run one complete «Ничья в 1-м тайме» bet on one match."""
        prefix = "[FIRST_HALF_DRAW]"
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_SEARCHING_MATCH", f"{prefix} Searching next match"
        )
        page = await self.browser_manager.ensure_page()
        league = LeagueBrowser(
            page,
            exclude_teams_enabled=self._config.exclude_teams_enabled,
        )
        await self._status(
            DemoStatus.OPENING_LEAGUE,
            "Открываем страницу Conference League 3x3",
            "LEAGUE_OPENING",
        )
        await league.open()
        await STATE.update(
            strategy_type=StrategyType.FIRST_HALF_DRAW.value,
            strategy_name=FIRST_HALF_DRAW_STRATEGY_NAME,
            market_name=FIRST_HALF_DRAW_MARKET_NAME,
            market_selection=FIRST_HALF_DRAW_SELECTION,
            selected_team=FIRST_HALF_DRAW_SELECTION,
            selected_side=None,
            selection_reason=FIRST_HALF_DRAW_MARKET_NAME,
            market_odds=None,
            market_available=False,
            market_locked=False,
            odds_available=False,
            odds_value=None,
            time_waiting_for_market=0.0,
            last_result=(await STATE.snapshot()).get("last_result"),
            bet={
                "step": 0,
                "max_steps": self._config.max_steps,
                "amount": None,
                "market": FIRST_HALF_DRAW_MARKET_NAME,
                "selection": FIRST_HALF_DRAW_SELECTION,
                "odds": None,
                "score_before": None,
                "period": FIRST_HALF_DRAW_PERIOD,
                "status": "SEARCHING_MATCH",
            },
            budget=self._budget.snapshot(),
        )

        selected_match: dict[str, Any] | None = None
        selected_identity = ""
        while selected_match is None and not self._stop_event.is_set():
            league = LeagueBrowser(
                await self.browser_manager.ensure_page(),
                exclude_teams_enabled=self._config.exclude_teams_enabled,
            )
            await self._status(
                DemoStatus.SCANNING_MATCHES,
                "Ищем следующий ближайший матч для ничьей в 1-м тайме",
                "FIRST_HALF_DRAW_SEARCHING_MATCH",
            )
            matches = await league.scan()
            processed = await REPOSITORY.processed_match_ids(
                StrategyType.FIRST_HALF_DRAW.value,
                mode=self._mode,
                period=FIRST_HALF_DRAW_PERIOD,
            )
            eligible: list[tuple[dict[str, Any], str]] = []
            for item in matches:
                identity = str(
                    item.get("match_id") or item.get("href") or item.get("url") or ""
                )
                if not identity:
                    await REPOSITORY.log(
                        "FIRST_HALF_DRAW_MATCH_SKIPPED_NO_ID",
                        f"{prefix} {item['team1']} - {item['team2']}",
                    )
                    continue
                if identity in processed:
                    await REPOSITORY.log(
                        "FIRST_HALF_DRAW_MATCH_ALREADY_PROCESSED",
                        f"{prefix} match_id={identity}",
                    )
                    continue
                eligible.append((item, identity))
            await STATE.update(
                scanner={
                    **league.last_scan_stats,
                    "processed": len(processed),
                    "selected": eligible[0][0] if eligible else None,
                }
            )
            if eligible:
                selected_match, selected_identity = eligible[0]
                break
            await self._status(
                DemoStatus.NO_UPCOMING_MATCHES,
                "Подходящих необработанных матчей пока нет",
                "NO_UPCOMING_MATCHES",
            )
            await self._sleep_or_stop(CONFIG.league_retry_interval)

        if selected_match is None or self._stop_event.is_set():
            return

        match_name = f"{selected_match['team1']} — {selected_match['team2']}"
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_MATCH_SELECTED", f"{prefix} Match selected: {match_name}"
        )
        await self._status(
            DemoStatus.MATCH_SELECTED,
            f"Выбран ближайший матч: {match_name}",
            "FIRST_HALF_DRAW_MATCH_SELECTED",
            match={
                "id": selected_identity,
                "team1": selected_match["team1"],
                "team2": selected_match["team2"],
                "score1": None,
                "score2": None,
                "timer": selected_match.get("time") or "",
                "period": selected_match.get("period") or "",
                "url": selected_match.get("url"),
                "state": "UPCOMING",
            },
        )
        await self._status(
            DemoStatus.OPENING_MATCH,
            f"Открываем матч: {match_name}",
            "FIRST_HALF_DRAW_OPENING_MATCH",
        )
        try:
            opened = await league.open_match(selected_match)
        except MatchAlreadyStarted as error:
            await REPOSITORY.log("MATCH_ALREADY_STARTED", str(error))
            return
        await REPOSITORY.log("MATCH_OPENED", opened["url"])

        if not await self._open_first_half_subgame(selected_match):
            return
        snapshot = await self._wait_for_initial_zero_score(selected_match)
        if snapshot is None:
            return
        excluded_team = excluded_team_in_match(
            snapshot.team1,
            snapshot.team2,
            enabled=self._config.exclude_teams_enabled,
        )
        if excluded_team is not None:
            await REPOSITORY.log(
                "FIRST_HALF_DRAW_MATCH_SKIPPED_EXCLUDED_TEAM",
                f"{snapshot.team1} — {snapshot.team2} ({excluded_team})",
            )
            return

        market_result = await self._wait_for_first_half_draw_market(
            snapshot, selected_match
        )
        if market_result is None:
            return
        snapshot, market, wait_seconds = market_result
        sequence = await REPOSITORY.get_sequence()
        step = int(sequence["current_step"])
        if step > self._config.max_steps:
            await REPOSITORY.save_sequence(status="SEQUENCE_EXHAUSTED")
            return
        amount = float(self._config.stakes[step - 1])
        cycle_id = str(sequence["sequence_id"])
        idempotency_key = (
            f"{self._mode}:{cycle_id}:{StrategyType.FIRST_HALF_DRAW.value}:"
            f"{selected_identity}:{FIRST_HALF_DRAW_PERIOD}:step{step}:attempt1"
        )
        active_record = {
            "id": idempotency_key,
            "idempotency_key": idempotency_key,
            "attempt": 1,
            "mode": self._mode,
            "strategy_type": StrategyType.FIRST_HALF_DRAW.value,
            "strategy_name": FIRST_HALF_DRAW_STRATEGY_NAME,
            "cycle_id": cycle_id,
            "match_id": selected_identity,
            "match": match_name,
            "period": FIRST_HALF_DRAW_PERIOD,
            "selected_team": FIRST_HALF_DRAW_SELECTION,
            "selected_side": None,
            "side_label": FIRST_HALF_DRAW_SELECTION,
            "step": step,
            "amount": amount,
            "odds": market.odds,
            "score_before": snapshot.score.text(),
            "score_after": None,
            "result": "ACTIVE",
            "status": DemoStatus.BET_ACTIVE.value,
            "strategy_state": DemoStatus.BET_ACTIVE.value,
            "settled": False,
            "market": FIRST_HALF_DRAW_MARKET_NAME,
            "market_selection": FIRST_HALF_DRAW_SELECTION,
            "created_at": local_now(),
            "resolved_at": None,
            "budget_before": (
                float(self._budget.current_budget) if self._mode == "DEMO" else None
            ),
            "budget_change": None,
            "budget_after": None,
            "time_waiting_for_market": round(wait_seconds, 3),
        }
        await self._status(
            DemoStatus.PLACING_BET,
            f"{prefix} Step {step}; stake {amount:g} RUB; draw @ {market.odds}",
            "FIRST_HALF_DRAW_PLACING_BET",
        )
        await REPOSITORY.log("FIRST_HALF_DRAW_STEP", f"{prefix} Step: {step}")
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_STAKE", f"{prefix} Stake: {amount:g} RUB"
        )

        if self._mode == "LIVE":
            placed = await self._place_first_half_draw_live(
                selected_match=selected_match,
                snapshot=snapshot,
                market=market,
                record=active_record,
            )
            if placed is None:
                return
            active_record, snapshot = placed
        else:
            await REPOSITORY.save_sequence(
                status="ACTIVE",
                current_match_id=selected_identity,
                selected_team=FIRST_HALF_DRAW_SELECTION,
            )
            await REPOSITORY.save_bet(active_record)

        await STATE.update(
            status=DemoStatus.BET_ACTIVE.value,
            event="FIRST_HALF_DRAW_BET_PLACED",
            message=f"{prefix} Bet placed",
            market_odds=market.odds,
            odds_value=market.odds,
            current_stake=amount,
            current_step=step,
            bet={**active_record, "max_steps": self._config.max_steps},
            sequence=await REPOSITORY.get_sequence(),
            stats=await REPOSITORY.stats(),
        )
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_BET_PLACED", f"{prefix} Bet placed"
        )

        finished = await self._wait_for_first_half_finish(
            MatchBrowser(await self.browser_manager.ensure_page()),
            selected_match,
            snapshot,
        )
        if finished is None:
            return
        final_snapshot, finish_evidence = finished
        await self._status(
            DemoStatus.SETTLING,
            f"{prefix} Settling first-half result",
            "FIRST_HALF_DRAW_SETTLING",
        )
        result = settle_first_half_draw(final_snapshot.score)
        budget_change: dict[str, Any] = {}
        if self._mode == "DEMO":
            applied = self._budget.settle(
                idempotency_key, result, amount, market.odds
            )
            if applied is None:
                await REPOSITORY.log(
                    "DEMO_BUDGET_DUPLICATE_IGNORED", f"bet_id={idempotency_key}"
                )
                return
            budget_change = applied
            await REPOSITORY.save_budget(self._budget.snapshot())

        settled_record = await REPOSITORY.save_bet(
            {
                **active_record,
                "score_after": final_snapshot.score.text(),
                "first_half_end_evidence": finish_evidence,
                "result": result,
                "status": "SETTLED",
                "strategy_state": DemoStatus.SETTLING.value,
                "settled": True,
                "resolved_at": local_now(),
                **budget_change,
            }
        )
        if self._mode == "LIVE":
            self._active_live_bet = None

        current_sequence = await REPOSITORY.get_sequence()
        if self._mode == "DEMO":
            cumulative_pnl = Decimal(
                str(current_sequence["cumulative_pnl"])
            ) + Decimal(str(budget_change["pnl"]))
            cumulative_losses = Decimal(str(current_sequence["cumulative_losses"]))
            if result == "LOSE":
                cumulative_losses += Decimal(str(amount))
            await REPOSITORY.save_sequence(
                cumulative_pnl=str(cumulative_pnl.quantize(Decimal("0.01"))),
                cumulative_losses=str(cumulative_losses.quantize(Decimal("0.01"))),
            )

        await REPOSITORY.log(
            "FIRST_HALF_DRAW_FIRST_HALF_FINISHED",
            f"{prefix} First half finished: {finish_evidence}",
        )
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_FINAL_SCORE",
            f"{prefix} Final first-half score: {final_snapshot.score.text()}",
        )
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_RESULT", f"{prefix} Result: {result}"
        )
        await REPOSITORY.add_cycle(
            {
                "cycle_id": cycle_id,
                "match": match_name,
                "match_id": selected_identity,
                "mode": self._mode,
                "strategy_type": StrategyType.FIRST_HALF_DRAW.value,
                "result": result,
                "steps": step,
            }
        )

        if result == "WIN":
            sequence_after = await REPOSITORY.reset_sequence()
            await REPOSITORY.log(
                "FIRST_HALF_DRAW_STEP_RESET", f"{prefix} Reset step -> 1"
            )
        elif step < self._config.max_steps:
            sequence_after = await REPOSITORY.save_sequence(
                current_step=step + 1,
                status="WAITING_NEXT_MATCH",
                current_match_id=None,
                selected_team=None,
            )
            await REPOSITORY.log(
                "FIRST_HALF_DRAW_STEP_ADVANCED",
                f"{prefix} Step {step} -> Step {step + 1}",
            )
            await REPOSITORY.log(
                "FIRST_HALF_DRAW_NEXT_STAKE",
                f"{prefix} Next stake: {float(self._config.stakes[step]):g} RUB",
            )
        else:
            sequence_after = await REPOSITORY.save_sequence(
                current_step=step,
                status="SEQUENCE_EXHAUSTED",
                current_match_id=selected_identity,
            )

        await STATE.update(
            status=(DemoStatus.WIN if result == "WIN" else DemoStatus.LOSE).value,
            event=result,
            message=f"{prefix} Result: {result}",
            last_result=result,
            last_change={
                "before": settled_record["score_before"],
                "after": settled_record["score_after"],
                "scorer": "Итог 1-го тайма",
                "result": result,
            },
            bet={**settled_record, "max_steps": self._config.max_steps},
            budget=self._budget.snapshot(),
            sequence=sequence_after,
            stats=await REPOSITORY.stats(),
        )
        if sequence_after["status"] != "SEQUENCE_EXHAUSTED":
            await self._status(
                DemoStatus.SWITCHING_MATCH,
                f"{prefix} Searching next match",
                "FIRST_HALF_DRAW_SWITCHING_MATCH",
                last_result=result,
            )

    async def _open_first_half_subgame(
        self, selected_match: dict[str, Any]
    ) -> bool:
        await self._status(
            DemoStatus.OPENING_FIRST_HALF,
            "Открываем sub-game «1-й тайм»",
            "FIRST_HALF_DRAW_OPENING_FIRST_HALF",
        )
        attempts = 0
        while not self._stop_event.is_set():
            attempts += 1
            page = await self.browser_manager.ensure_page()
            try:
                await open_first_half(page, REPOSITORY.log)
                return True
            except FirstHalfNotReady as error:
                if attempts == 1 or attempts % 10 == 0:
                    await REPOSITORY.log("FIRST_HALF_NOT_READY", str(error))
                try:
                    snapshot = await MatchBrowser(page).snapshot()
                    finished, evidence = await first_half_end_signal(page, snapshot)
                    if finished:
                        await REPOSITORY.log(
                            "FIRST_HALF_DRAW_MATCH_SKIPPED_FINISHED",
                            f"Первый тайм уже завершён: {evidence}",
                        )
                        return False
                except ScoreReadError:
                    pass
                await self._sleep_or_stop(CONFIG.ocr_retry_delay)
        return False

    async def _wait_for_first_half_draw_market(
        self,
        snapshot: ScoreboardSnapshot,
        selected_match: dict[str, Any],
    ):
        started = time.monotonic()
        attempt = 0
        last_status: str | None = None
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_MARKET_SEARCH",
            "[FIRST_HALF_DRAW] Market search: 1X2. 1-й тайм -> Ничья",
        )
        while not self._stop_event.is_set():
            attempt += 1
            page = await self.browser_manager.ensure_page()
            browser = MatchBrowser(page)
            try:
                fresh = await browser.snapshot()
                if fresh.team1 != snapshot.team1 or fresh.team2 != snapshot.team2:
                    raise RecoverableDemoError(
                        "SCOREBOARD_TEAMS_CHANGED",
                        "Порядок или названия команд в scoreboard изменились.",
                    )
                snapshot = fresh
                finished, evidence = await first_half_end_signal(page, snapshot)
                await self._publish_snapshot(
                    snapshot,
                    selected_match,
                    state="FIRST_HALF_FINISHED" if finished else "FIRST_HALF",
                )
                if finished:
                    await REPOSITORY.log(
                        "FIRST_HALF_DRAW_MARKET_EXPIRED",
                        f"Первый тайм завершён до ставки: {evidence}",
                    )
                    return None
                await self._status(
                    DemoStatus.SEARCHING_MARKET,
                    "Ищем рынок 1X2. 1-й тайм и выбор Ничья",
                    "FIRST_HALF_DRAW_SEARCHING_MARKET",
                )
                async with self._market_lock:
                    market = await read_first_half_draw_market(page, REPOSITORY.log)
                waited = time.monotonic() - started
                await STATE.update(
                    market_reader={
                        "source": "DOM / Playwright",
                        "status": "READY",
                        "attempt": attempt,
                        "market": FIRST_HALF_DRAW_MARKET_NAME,
                        "selection": FIRST_HALF_DRAW_SELECTION,
                    },
                    market_available=True,
                    market_locked=False,
                    odds_available=True,
                    odds_value=market.odds,
                    market_odds=market.odds,
                    initial_selected_odds=market.odds,
                    time_waiting_for_market=round(waited, 3),
                    odds={
                        "selected": market.odds,
                        "opponent": None,
                        "team1": None,
                        "team2": None,
                        "market": market.market,
                        "selection": market.selection,
                        "source": market.source,
                        "backend": None,
                        "confidence": None,
                        "status": "ODDS_CONFIRMED",
                    },
                )
                await REPOSITORY.log(
                    "FIRST_HALF_DRAW_MARKET_READY",
                    f"[FIRST_HALF_DRAW] Draw odds: {market.odds}",
                )
                return snapshot, market, waited
            except MarketReadError as error:
                waited = time.monotonic() - started
                locked = error.status == "MARKET_LOCKED"
                await STATE.update(
                    status=(
                        DemoStatus.MARKET_LOCKED
                        if locked
                        else DemoStatus.WAITING_FOR_MARKET
                    ).value,
                    event=error.status,
                    market_available=bool(error.details.get("market_available")),
                    market_locked=locked,
                    odds_available=False,
                    odds_value=None,
                    time_waiting_for_market=round(waited, 3),
                    market_reader={
                        "source": "DOM / Playwright",
                        "status": error.status,
                        "attempt": attempt,
                        "market": FIRST_HALF_DRAW_MARKET_NAME,
                        "selection": FIRST_HALF_DRAW_SELECTION,
                    },
                )
                if error.status != last_status or attempt % 10 == 0:
                    await REPOSITORY.log(error.status, str(error))
                last_status = error.status
                await self._sleep_or_stop(CONFIG.ocr_retry_delay)
        return None

    async def _place_first_half_draw_live(
        self,
        *,
        selected_match: dict[str, Any],
        snapshot: ScoreboardSnapshot,
        market: Any,
        record: dict[str, Any],
    ) -> tuple[dict[str, Any], ScoreboardSnapshot] | None:
        """Use the shared LIVE coupon executor for the first-half draw market."""
        attempt_id = str(record["id"])
        record.update(result="PENDING", status=LiveStatus.IDLE.value)
        decision = LiveDecision(
            attempt_id=attempt_id,
            match_id=str(record["match_id"]),
            team=FIRST_HALF_DRAW_SELECTION,
            side=Scorer.UNKNOWN,
            strategy_step=int(record["step"]),
            amount=float(record["amount"]),
            goal_number=0,
            coefficient=float(market.odds),
            coefficient_locator=market.locator,
        )
        await REPOSITORY.save_sequence(
            status="PLACING_BET",
            current_match_id=record["match_id"],
            selected_team=FIRST_HALF_DRAW_SELECTION,
        )
        await REPOSITORY.save_bet(record)

        async def publish_live(status: LiveStatus, message: str) -> None:
            record["status"] = status.value
            await REPOSITORY.save_bet(record)
            await STATE.update(
                mode="LIVE",
                status=status.value,
                message=message,
                event=status.value,
                bet={**record, "max_steps": self._config.max_steps},
            )

        try:
            page = await self.browser_manager.ensure_page()
            browser = MatchBrowser(page)
            await self.live_executor.prepare(page, decision, publish_live)
            observation, latest = await self._wait_for_live_confirmation_or_score(
                page,
                browser,
                selected_match,
                snapshot,
                decision,
                publish_live,
            )
            if observation is None:
                clicked = await self.live_executor.manual_click_seen(attempt_id)
                record.update(
                    result="SUBMISSION_UNKNOWN" if clicked else "NOT_PLACED",
                    status=self.live_executor.state(attempt_id).value,
                    settled=not clicked,
                    resolved_at=local_now(),
                )
                await REPOSITORY.save_bet(record)
                if clicked:
                    self._stop_event.set()
                return None
            if not observation.placed:
                await self._invalidate_live_attempt(
                    record, decision, observation.signal, publish_live
                )
                if observation.signal == BLOCKED_EVENT_SIGNAL:
                    await self.live_executor.remove_blocked_coupon(page, attempt_id)
                return None

            placement_snapshot = await self._read_fresh_score(
                browser, selected_match, latest or snapshot
            )
            record.update(
                result="ACTIVE",
                status=LiveStatus.ACTIVE.value,
                score_before=placement_snapshot.score.text(),
                placement_signal=observation.signal,
                placement_confirmed_at=local_now(),
            )
            await REPOSITORY.save_sequence(status="ACTIVE")
            await REPOSITORY.save_bet(record)
            return record, placement_snapshot
        except LivePreparationError as error:
            record.update(
                result="NOT_PLACED",
                status=error.status,
                settled=True,
                resolved_at=local_now(),
                error=str(error),
            )
            await REPOSITORY.save_bet(record)
            await REPOSITORY.log(error.status, str(error))
            if self.live_executor.market_was_selected(attempt_id):
                self._stop_event.set()
                await STATE.update(
                    running=False,
                    status=LiveStatus.ERROR.value,
                    error=str(error),
                    message="LIVE остановлен после открытия coupon: повтор запрещён.",
                )
            return None

    async def _wait_for_first_half_finish(
        self,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        previous: ScoreboardSnapshot,
    ) -> tuple[ScoreboardSnapshot, str] | None:
        await self._status(
            DemoStatus.WAITING_FIRST_HALF_END,
            "[FIRST_HALF_DRAW] Waiting for first half end",
            "WAITING_FIRST_HALF_END",
        )
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_WAITING_FIRST_HALF_END",
            "[FIRST_HALF_DRAW] Waiting for first half end",
        )
        await REPOSITORY.log(
            "FIRST_HALF_DRAW_SCORE",
            f"[FIRST_HALF_DRAW] Current score: {previous.score.text()}",
        )
        last_score = previous.score
        saw_running_timer = bool(previous.timer)
        market_missing_reads = 0
        read_errors = 0
        while not self._stop_event.is_set():
            await self._sleep_or_stop(CONFIG.score_poll_interval)
            try:
                current = await browser.snapshot()
            except ScoreReadError as error:
                read_errors += 1
                if read_errors == 1 or read_errors % 20 == 0:
                    await REPOSITORY.log("SCORE_TEMPORARILY_UNAVAILABLE", str(error))
                continue
            read_errors = 0
            if current.team1 != previous.team1 or current.team2 != previous.team2:
                raise RecoverableDemoError(
                    "SCOREBOARD_TEAMS_CHANGED",
                    "Порядок или названия команд в scoreboard изменились.",
                )
            page = await self.browser_manager.ensure_page()
            finished, evidence = await first_half_end_signal(page, current)
            saw_running_timer = saw_running_timer or bool(current.timer)
            if await first_half_draw_market_present(page):
                market_missing_reads = 0
            else:
                market_missing_reads += 1
            market_and_timer_confirmed_end = (
                market_missing_reads >= 2 and saw_running_timer and not current.timer
            )
            if market_and_timer_confirmed_end:
                finished = True
                evidence = "LIVE-рынок 1-го тайма исчез; scoreboard timer завершён"
            await self._publish_snapshot(
                current,
                selected_match,
                state="FIRST_HALF_FINISHED" if finished else "FIRST_HALF",
            )
            if current.score != last_score:
                await REPOSITORY.log(
                    "FIRST_HALF_DRAW_SCORE",
                    f"[FIRST_HALF_DRAW] Current score: {current.score.text()}",
                )
                last_score = current.score
            if finished:
                return current, evidence
        return None

    async def _wait_for_initial_zero_score(
        self, selected_match: dict[str, Any]
    ) -> ScoreboardSnapshot | None:
        await self._status(
            DemoStatus.WAITING_FOR_MATCH_START,
            "Ожидаем валидный начальный счёт 0:0",
            "WAITING_FOR_INITIAL_SCORE",
        )
        retries = 0
        while not self._stop_event.is_set():
            page = await self.browser_manager.ensure_page()
            try:
                snapshot = await MatchBrowser(page).snapshot()
                match_state = "LIVE" if snapshot.period else "UPCOMING"
                await self._publish_snapshot(snapshot, selected_match, state=match_state)
                if can_create_initial_bet(snapshot.score):
                    await REPOSITORY.log(
                        "PREMATCH_SCORE_READY",
                        f"{snapshot.score.text()} / {match_state}",
                    )
                    return snapshot
                await REPOSITORY.log(
                    "INITIAL_SCORE_NOT_ZERO",
                    f"Первая ставка запрещена при счёте {snapshot.score.text()}",
                )
                return None
            except ScoreReadError as error:
                retries += 1
                if retries == 1 or retries % 20 == 0:
                    await REPOSITORY.log("SCORE_TEMPORARILY_UNAVAILABLE", str(error))
                await self._sleep_or_stop(CONFIG.score_poll_interval)
        return None

    async def _wait_for_match_start(
        self, selected_match: dict[str, Any]
    ) -> ScoreboardSnapshot | None:
        await self._status(
            DemoStatus.WAITING_FOR_MATCH_START,
            "Ожидаем появления LIVE scoreboard",
            "WAITING_FOR_MATCH_START",
        )
        retries = 0
        while not self._stop_event.is_set():
            page = await self.browser_manager.ensure_page()
            try:
                snapshot = await MatchBrowser(page).snapshot()
                if not snapshot.period:
                    await self._publish_snapshot(snapshot, selected_match, state="UPCOMING")
                    await self._sleep_or_stop(CONFIG.score_poll_interval)
                    continue
                await self._publish_snapshot(
                    snapshot,
                    selected_match,
                    state="LIVE" if snapshot.period else "UPCOMING",
                )
                await self._status(
                    DemoStatus.MATCH_STARTED,
                    f"Матч начался: {snapshot.score.text()}",
                    "MATCH_STARTED",
                )
                await REPOSITORY.log("MATCH_STARTED", snapshot.score.text())
                return snapshot
            except ScoreReadError as error:
                retries += 1
                if retries == 1 or retries % 20 == 0:
                    await REPOSITORY.log("SCORE_TEMPORARILY_UNAVAILABLE", str(error))
                await self._sleep_or_stop(CONFIG.score_poll_interval)
        return None

    async def _observe_demo_blocked_score(
        self,
        window: DemoBlockedWindow,
        current: ScoreboardSnapshot,
    ) -> None:
        """Track score deltas while no DEMO bet exists; never infer goal order."""
        if not window.protection_active:
            return
        before = window.blocked_score_before
        after = current.score
        if after == before:
            return
        await REPOSITORY.log(
            "NEXT_STEP_SCORE_CHANGED_NO_ACTIVE_BET",
            f"step={window.step} {before.text()} -> {after.text()}",
        )
        await REPOSITORY.log(
            "DEMO_BLOCKED_SCORE_CHANGED",
            f"[NEXT_GOAL][DEMO][BLOCKED] score changed {before.text()} -> {after.text()}",
        )
        if selected_team_scored_between(before, after, window.selected_side):
            await REPOSITORY.log(
                "NEXT_STEP_SELECTED_TEAM_GOAL_MISSED",
                f"step={window.step} selected_team={window.selected_team} "
                f"{before.text()} -> {after.text()}",
            )
            await REPOSITORY.log(
                "DEMO_MISSED_SELECTED_TEAM_GOAL",
                "[NEXT_GOAL][DEMO][BLOCKED] SELECTED TEAM SCORED WITHOUT ACTIVE BET",
            )
            raise MissedSelectedTeamGoal(current)
        opponent_increased = (
            after.team2 > before.team2
            if window.selected_side == Scorer.TEAM_1
            else after.team1 > before.team1
        )
        window.blocked_score_before = after
        await REPOSITORY.log(
            "NEXT_STEP_OPPONENT_GOAL_BASELINE_UPDATED",
            f"step={window.step} baseline={after.text()}",
        )
        await REPOSITORY.log(
            (
                "DEMO_BLOCKED_OPPONENT_SCORED"
                if opponent_increased
                else "DEMO_BLOCKED_BASELINE_CORRECTED"
            ),
            f"[NEXT_GOAL][DEMO][BLOCKED] selected team did not score; baseline updated to {after.text()}",
        )

    async def _confirm_demo_bet_acceptance(
        self,
        *,
        page: Page,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        current_odds: Any,
        window: DemoBlockedWindow,
        snapshot: ScoreboardSnapshot,
    ) -> tuple[bool, ScoreboardSnapshot]:
        """Require a short stable-open window before DEMO_BET_CREATED.

        This gate is used for a next step after settlement. One unlocked Canvas
        sample is not enough: the same score/goal must stay open across several
        fast checks. Any lock resets acceptance. Any score change is handled as
        an unaccepted step and is routed through the existing missed-goal logic.
        """
        baseline_score = snapshot.score
        expected_goal = baseline_score.team1 + baseline_score.team2 + 1
        await REPOSITORY.log(
            "DEMO_STEP_ARMING",
            (
                f"step={window.step}; stake={window.stake:g}; "
                f"score={baseline_score.text()}; goal={expected_goal}; "
                f"confirmations_required={DEMO_ACCEPTANCE_CONFIRMATIONS}; "
                f"interval_ms={int(DEMO_ACCEPTANCE_INTERVAL_SECONDS * 1000)}"
            ),
        )
        await STATE.update(
            event="DEMO_STEP_ARMING",
            market_locked=False,
            message=(
                f"Шаг {window.step}: подтверждаем свободный рынок перед "
                "виртуальным принятием ставки"
            ),
        )

        for confirmation in range(1, DEMO_ACCEPTANCE_CONFIRMATIONS + 1):
            fresh = await self._read_fresh_score(
                browser,
                selected_match,
                snapshot,
            )
            if fresh.score != baseline_score:
                await REPOSITORY.log(
                    "DEMO_SCORE_CHANGED_BEFORE_ACCEPTANCE",
                    (
                        f"step={window.step}; score={baseline_score.text()}"
                        f"->{fresh.score.text()}; accepted=false"
                    ),
                )
                await self._observe_demo_blocked_score(window, fresh)
                await REPOSITORY.log(
                    "DEMO_OPEN_CONFIRMATION_RESET",
                    (
                        f"step={window.step}; reason=score_changed; "
                        f"confirmed={confirmation - 1}/"
                        f"{DEMO_ACCEPTANCE_CONFIRMATIONS}"
                    ),
                )
                return False, fresh

            fresh_expected_goal = fresh.score.team1 + fresh.score.team2 + 1
            if (
                current_odds.next_goal_number != expected_goal
                or fresh_expected_goal != expected_goal
            ):
                await REPOSITORY.log(
                    "DEMO_OPEN_CONFIRMATION_RESET",
                    (
                        f"step={window.step}; reason=stale_goal; "
                        f"market_goal={current_odds.next_goal_number}; "
                        f"expected_goal={fresh_expected_goal}; accepted=false"
                    ),
                )
                return False, fresh

            blocked = (
                bool(fresh.period)
                and await self._demo_prebet_canvas_is_blocked(
                    page,
                    current_odds,
                    window,
                )
            )
            if blocked:
                await REPOSITORY.log(
                    "DEMO_OPEN_CONFIRMATION_RESET",
                    (
                        f"step={window.step}; reason=canvas_lock; "
                        f"confirmed={confirmation - 1}/"
                        f"{DEMO_ACCEPTANCE_CONFIRMATIONS}; accepted=false"
                    ),
                )
                return False, fresh

            await REPOSITORY.log(
                "DEMO_OPEN_CONFIRMATION",
                (
                    f"step={window.step}; confirmation={confirmation}/"
                    f"{DEMO_ACCEPTANCE_CONFIRMATIONS}; "
                    f"score={fresh.score.text()}; goal={expected_goal}; "
                    "locked=false"
                ),
            )
            snapshot = fresh
            if confirmation < DEMO_ACCEPTANCE_CONFIRMATIONS:
                await self._sleep_or_stop(DEMO_ACCEPTANCE_INTERVAL_SECONDS)

        # One final scoreboard read after the last lock observation closes the
        # most important race: goal after the last unlocked sample but before
        # the virtual bet boundary.
        final_snapshot = await self._read_fresh_score(
            browser,
            selected_match,
            snapshot,
        )
        if final_snapshot.score != baseline_score:
            await REPOSITORY.log(
                "DEMO_SCORE_CHANGED_BEFORE_ACCEPTANCE",
                (
                    f"step={window.step}; score={baseline_score.text()}"
                    f"->{final_snapshot.score.text()}; accepted=false; "
                    "phase=final_score_guard"
                ),
            )
            await self._observe_demo_blocked_score(window, final_snapshot)
            await REPOSITORY.log(
                "DEMO_OPEN_CONFIRMATION_RESET",
                (
                    f"step={window.step}; reason=final_score_changed; "
                    f"confirmed={DEMO_ACCEPTANCE_CONFIRMATIONS}/"
                    f"{DEMO_ACCEPTANCE_CONFIRMATIONS}; accepted=false"
                ),
            )
            return False, final_snapshot

        final_expected_goal = (
            final_snapshot.score.team1 + final_snapshot.score.team2 + 1
        )
        if final_expected_goal != expected_goal:
            await REPOSITORY.log(
                "DEMO_OPEN_CONFIRMATION_RESET",
                (
                    f"step={window.step}; reason=final_goal_mismatch; "
                    f"market_goal={current_odds.next_goal_number}; "
                    f"expected_goal={final_expected_goal}; accepted=false"
                ),
            )
            return False, final_snapshot

        await REPOSITORY.log(
            "DEMO_BET_ACCEPTANCE_CONFIRMED",
            (
                f"step={window.step}; stake={window.stake:g}; "
                f"score={final_snapshot.score.text()}; goal={expected_goal}; "
                f"confirmations={DEMO_ACCEPTANCE_CONFIRMATIONS}; accepted=true"
            ),
        )
        await STATE.update(
            event="DEMO_BET_ACCEPTANCE_CONFIRMED",
            market_locked=False,
            message=(
                f"Шаг {window.step}: рынок стабильно свободен, "
                "виртуальная ставка может быть создана"
            ),
        )
        return True, final_snapshot

    async def _demo_prebet_canvas_is_blocked(
        self,
        page: Page,
        current_odds: Any,
        window: DemoBlockedWindow,
    ) -> bool:
        """Final lock gate before DEMO_BET_CREATED for a live Canvas market."""
        if str(getattr(current_odds, "source", "") or "") != "CANVAS_2D":
            return False
        next_goal_number = getattr(current_odds, "next_goal_number", None)
        if next_goal_number is None:
            await REPOSITORY.log(
                "DEMO_PREBET_CANVAS_LOCK_STATE_UNAVAILABLE",
                "[NEXT_GOAL][DEMO][PREBET] Canvas odds have no next_goal_number; fail closed",
            )
            return True

        state = await read_next_goal_lock_state(
            page,
            int(next_goal_number),
            logger=REPOSITORY.log,
        )
        if not state.get("available"):
            await REPOSITORY.log(
                "DEMO_PREBET_CANVAS_LOCK_STATE_UNAVAILABLE",
                (
                    "[NEXT_GOAL][DEMO][PREBET] cached Canvas button mapping unavailable; "
                    f"goal={next_goal_number}; reason={state.get('reason')}"
                ),
            )
            return True

        selected_side = 1 if window.selected_side == Scorer.TEAM_1 else 2
        locked = {int(side) for side in (state.get("locked_sides") or ())}
        recent = {
            int(side)
            for side in (state.get("recent_locked_sides") or ())
        }
        await REPOSITORY.log(
            "DEMO_PREBET_CANVAS_LOCK_CHECK",
            (
                "[NEXT_GOAL][DEMO][PREBET] bookmaker lock check; "
                f"step={window.step}; selected_side={selected_side}; "
                f"goal={next_goal_number}; "
                f"locked_sides={sorted(locked)}; "
                f"recent_locked_sides={sorted(recent)}; "
                f"checked_canvas_ids={list(state.get('checked_canvas_ids') or ())}"
            ),
        )
        if selected_side not in locked and selected_side not in recent:
            if window.blocked_window_active:
                window.blocked_window_active = False
                await REPOSITORY.log(
                    "DEMO_PREBET_CANVAS_UNLOCKED",
                    (
                        "[NEXT_GOAL][DEMO][PREBET] bookmaker lock released; "
                        f"step={window.step}; selected_side={selected_side}; "
                        f"goal={next_goal_number}; "
                        f"released_sides={list(state.get('released_sides') or ())}; "
                        "bet creation may continue"
                    ),
                )
                await STATE.update(
                    market_locked=False,
                    event="DEMO_PREBET_CANVAS_UNLOCKED",
                    message=(
                        f"Замок БК снят для шага {window.step}; "
                        "следующая ставка снова разрешена"
                    ),
                )
            return False

        window.blocked_window_active = True
        marker = (
            (state.get("markers") or {}).get(str(selected_side))
            or (state.get("recent_markers") or {}).get(str(selected_side))
            or {}
        )
        await REPOSITORY.log(
            "DEMO_PREBET_CANVAS_LOCKED",
            (
                "[NEXT_GOAL][DEMO][PREBET] virtual bet blocked before creation; "
                f"step={window.step}; stake={window.stake:g}; "
                f"selected_team={window.selected_team}; "
                f"goal={next_goal_number}; "
                f"locked_sides={sorted(locked)}; "
                f"recent_locked_sides={sorted(recent)}; "
                f"reason={marker.get('reason')}; "
                f"canvas_id={marker.get('detected_canvas_id')}; "
                f"marker_seq={marker.get('seq')}; "
                f"latched={bool(marker.get('latched'))}; "
                f"persistent_until_clear={bool(marker.get('persistent_until_clear'))}; "
                f"checked_canvas_ids={list(state.get('checked_canvas_ids') or ())}"
            ),
        )
        await STATE.update(
            market_locked=True,
            event="DEMO_PREBET_CANVAS_LOCKED",
            message=(
                f"Блокировка БК активна: шаг {window.step}, "
                f"сторона {selected_side}, причина={marker.get('reason')}"
            ),
        )
        return True

    async def _switch_match_after_three_losses(
        self,
        *,
        selected_match: dict[str, Any],
        match_name: str,
        cycle_id: str,
        step: int,
        losses_in_current_match: int,
    ) -> int:
        """Use the one allowed max-three switch for this betting sequence."""
        if step >= self._config.max_steps:
            raise ValueError("Cannot switch match after the final configured step")
        current_sequence = await REPOSITORY.get_sequence()
        if bool(int(current_sequence.get("max_three_switched") or 0)):
            raise RuntimeError("MAX_3_STEPS_SWITCH_ALREADY_USED")
        next_step = step + 1
        match_id = next_goal_match_identity(selected_match)
        await REPOSITORY.add_blocked_match(cycle_id, match_id)
        sequence_after_switch = await REPOSITORY.save_sequence(
            current_step=next_step,
            status="WAITING_FOR_MATCH",
            current_match_id=None,
            selected_team=None,
            max_three_switched=1,
        )
        await REPOSITORY.log(
            "MAX_3_STEPS_LIMIT_REACHED",
            (
                f"match={match_name}; losses_in_match={losses_in_current_match}; "
                f"last_lost_step={step}; next_step={next_step}; "
                f"next_stake={float(self._config.stakes[next_step - 1]):g}"
            ),
        )
        await REPOSITORY.log(
            "MAX_3_STEPS_SWITCHING_MATCH",
            (
                "Закрываем первый матч после 3 проигрышей подряд; "
                f"на следующем матче продолжаем с шага {next_step} "
                "и идём догоном до WIN или конца ряда без повторного max 3"
            ),
        )
        await STATE.update(
            status=DemoStatus.WAITING_NEXT_MATCH.value,
            event="MAX_3_STEPS_SWITCHING_MATCH",
            message=(
                "3 проигрыша подряд в первом матче. Переходим на второй "
                f"матч с шага {next_step}; дальше догон до WIN или конца ряда"
            ),
            sequence=sequence_after_switch,
            bet={
                "step": next_step,
                "max_steps": self._config.max_steps,
                "amount": float(self._config.stakes[next_step - 1]),
                "market": "Следующий гол",
                "odds": None,
                "score_before": None,
                "next_goal_number": None,
                "status": "WAITING_NEXT_MATCH",
            },
        )
        self._active_demo_protection = None
        self._pending_live_bet = None
        self._current_series = None
        return next_step

    async def _finish_demo_missed_selected_team_goal(
        self,
        *,
        window: DemoBlockedWindow,
        selected_match: dict[str, Any],
        cycle_id: str,
        snapshot: ScoreboardSnapshot,
    ) -> BlockedMatchSwitch:
        attempt_record = {
            "id": f"{cycle_id}:demo-blocked:{window.step}:{uuid4().hex[:8]}",
            "attempt_id": f"{cycle_id}:demo-blocked:{window.step}",
            "mode": "DEMO",
            "strategy_type": StrategyType.NEXT_GOAL.value,
            "strategy_name": StrategyType.NEXT_GOAL.display_name,
            "cycle_id": cycle_id,
            "match_id": window.match_id,
            "match": f"{snapshot.team1} — {snapshot.team2}",
            "selected_team": window.selected_team,
            "selected_side": window.selected_side.value,
            "step": window.step,
            "amount": window.stake,
            "odds": None,
            "score_before": window.blocked_score_before.text(),
            "attempt_score_before": window.blocked_score_before.text(),
            "blocked_score_initial": window.initial_score.text(),
            "market": f"Следующий гол №{snapshot.score.team1 + snapshot.score.team2 + 1}",
            "next_goal_number": snapshot.score.team1 + snapshot.score.team2 + 1,
            "result": "PENDING",
            "status": "DEMO_BLOCKED_WINDOW",
            "settled": False,
            "created_at": local_now(),
            "resolved_at": None,
            "budget_before": float(self._budget.current_budget),
            "budget_change": None,
            "budget_after": float(self._budget.current_budget),
        }
        return await self._finish_missed_selected_team_goal(
            attempt_record=attempt_record,
            selected_match=selected_match,
            cycle_id=cycle_id,
            step=window.step,
            amount=window.stake,
            score_after_removal=snapshot,
            placement_signal="DEMO_MARKET_BLOCKED",
            explanation=(
                "DEMO ставка не создана: выбранная команда забила, пока outcome "
                "следующего гола был недоступен"
            ),
        )

    async def _guard_blocked_recovery_score(
        self,
        *,
        attempt_score: Score | None,
        current: ScoreboardSnapshot,
        selected_side: Scorer | None,
    ) -> None:
        if (
            attempt_score is None
            or selected_side is None
            or not selected_team_scored(attempt_score, current.score, selected_side)
        ):
            return
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_SCORE_CHANGED",
            f"[NEXT_GOAL][BLOCKED] score changed {attempt_score.text()} -> {current.score.text()}",
        )
        await REPOSITORY.log(
            "NEXT_GOAL_MISSED_SELECTED_TEAM_GOAL",
            "[NEXT_GOAL][BLOCKED] SELECTED TEAM SCORED WITHOUT ACTIVE BET; MISSED_SELECTED_TEAM_GOAL",
        )
        raise MissedSelectedTeamGoal(current)

    async def _wait_for_odds(
        self,
        snapshot: ScoreboardSnapshot,
        selected_match: dict[str, Any],
        *,
        blocked_attempt_score: Score | None = None,
        blocked_selected_side: Scorer | None = None,
        demo_blocked_window: DemoBlockedWindow | None = None,
    ):
        """Wait for current next-goal odds via Canvas 2D draw calls with DOM fallback."""
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            page = await self.browser_manager.ensure_page()
            browser = MatchBrowser(page)
            try:
                fresh = await browser.snapshot()
                if fresh.team1 != snapshot.team1 or fresh.team2 != snapshot.team2:
                    raise RecoverableDemoError(
                        "SCOREBOARD_TEAMS_CHANGED",
                        "Порядок или названия команд в scoreboard изменились.",
                    )
                snapshot = fresh
                await self._guard_blocked_recovery_score(
                    attempt_score=blocked_attempt_score,
                    current=snapshot,
                    selected_side=blocked_selected_side,
                )
                if demo_blocked_window is not None:
                    await self._observe_demo_blocked_score(
                        demo_blocked_window,
                        snapshot,
                    )
                if self._mode == "LIVE" and self._pending_live_bet is not None:
                    self._pending_live_bet = self._pending_live_bet.with_score(snapshot.score)
                await self._publish_snapshot(snapshot, selected_match, state="LIVE")
            except ScoreReadError as error:
                if attempt == 1 or attempt % 20 == 0:
                    await REPOSITORY.log("SCORE_TEMPORARILY_UNAVAILABLE", str(error))
                await self._sleep_or_stop(CONFIG.score_poll_interval)
                continue

            next_goal_number = snapshot.score.team1 + snapshot.score.team2 + 1
            if attempt == 1:
                await REPOSITORY.log(
                    "MARKET_READING", f"Следующий гол №{next_goal_number}"
                )
            await self._status(
                DemoStatus.WAITING_FOR_MARKET,
                f"Ждём рынок следующего гола №{next_goal_number} (Canvas 2D / DOM)",
                "WAITING_FOR_MARKET",
            )
            await STATE.update(
                market_reader={
                    "source": "CANVAS 2D / fillText + DOM fallback",
                    "status": "READING",
                    "attempt": attempt,
                    "next_goal_number": next_goal_number,
                },
                odds={
                    "selected": None,
                    "opponent": None,
                    "team1": None,
                    "team2": None,
                    "market": f"Следующий гол №{next_goal_number}",
                    "source": None,
                    "backend": None,
                    "confidence": None,
                    "status": "WAITING_FOR_MARKET",
                },
                ocr={
                    "status": "NOT_USED_FOR_NEXT_GOAL",
                    "attempt": 0,
                    "max_attempts": 0,
                    "canvas": None,
                    "engine": None,
                    "latency_seconds": None,
                    "candidates": [],
                    "market_bbox": None,
                },
            )
            try:
                async with self._market_lock:
                    odds = await read_next_goal_odds(
                        page,
                        snapshot.team1,
                        snapshot.team2,
                        snapshot.score.team1,
                        snapshot.score.team2,
                        REPOSITORY.log,
                        read_only=self._mode == "DEMO",
                    )

                verified = await browser.snapshot()
                if verified.team1 != snapshot.team1 or verified.team2 != snapshot.team2:
                    raise RecoverableDemoError(
                        "SCOREBOARD_TEAMS_CHANGED",
                        "Порядок или названия команд в scoreboard изменились.",
                    )
                await self._guard_blocked_recovery_score(
                    attempt_score=blocked_attempt_score,
                    current=verified,
                    selected_side=blocked_selected_side,
                )
                if demo_blocked_window is not None:
                    await self._observe_demo_blocked_score(
                        demo_blocked_window,
                        verified,
                    )
                await self._publish_snapshot(
                    verified,
                    selected_match,
                    state="LIVE" if verified.period else "UPCOMING",
                )
                if verified.score != snapshot.score:
                    await REPOSITORY.log(
                        "SCORE_CHANGED_WHILE_READING_MARKET",
                        f"{snapshot.score.text()} → {verified.score.text()}; читаем новый N",
                    )
                    snapshot = verified
                    continue

                if odds.source == "CANVAS_2D":
                    # Canvas 2D / fillText is captured from the draw-call stream.
                    reader_source = "Canvas 2D / draw calls"
                elif odds.source == "CANVAS_VISION":
                    reader_source = f"Canvas Vision / {odds.ocr_backend or 'OCR'}"
                else:
                    reader_source = "DOM / Playwright"
                odds_state = self._odds_state(odds, None, None)
                selected_lock_active = False
                if demo_blocked_window is not None:
                    selected_side_number = (
                        1
                        if demo_blocked_window.selected_side == Scorer.TEAM_1
                        else 2
                    )
                    odds_locked = {
                        int(side)
                        for side in (
                            tuple(getattr(odds, "locked_sides", ()) or ())
                            + tuple(getattr(odds, "recent_locked_sides", ()) or ())
                        )
                    }
                    selected_lock_active = selected_side_number in odds_locked
                await STATE.update(
                    market_reader={
                        "source": reader_source,
                        "status": "READY",
                        "attempt": attempt,
                        "next_goal_number": odds.next_goal_number,
                    },
                    odds=odds_state,
                    market_available=True,
                    market_locked=selected_lock_active,
                    odds_available=True,
                )
                await self._status(
                    DemoStatus.ODDS_READY,
                    f"Коэффициенты готовы: {odds.team1} / {odds.team2}",
                    "ODDS_READY",
                )
                if (
                    demo_blocked_window is not None
                    and demo_blocked_window.blocked_window_active
                ):
                    await REPOSITORY.log(
                        "DEMO_BLOCKED_ODDS_VISIBLE",
                        (
                            "[NEXT_GOAL][DEMO][BLOCKED] odds are readable but "
                            "blocked_window remains active until final Canvas lock gate "
                            f"confirms unlock; step={demo_blocked_window.step}; "
                            f"selected_lock_active={selected_lock_active}"
                        ),
                    )
                return snapshot, odds
            except MarketReadError as error:
                if (
                    demo_blocked_window is not None
                    and demo_blocked_window.market_was_ready
                    and error.status in DEMO_BLOCKED_MARKET_STATUSES
                    and not demo_blocked_window.blocked_window_active
                ):
                    demo_blocked_window.blocked_window_active = True
                    await REPOSITORY.log(
                        "DEMO_BLOCKED_WINDOW_STARTED",
                        f"[NEXT_GOAL][DEMO][BLOCKED] outcome temporarily unavailable; "
                        f"match_id={demo_blocked_window.match_id}; "
                        f"selected_team={demo_blocked_window.selected_team}; "
                        f"step={demo_blocked_window.step} stake={demo_blocked_window.stake:g}; "
                        f"blocked_score_before={demo_blocked_window.blocked_score_before.text()}; "
                        f"market_status={error.status}",
                    )
                error_source = str(error.details.get("source") or "DOM_PLAYWRIGHT")
                if "CANVAS_2D" in error_source:
                    reader_source = "Canvas 2D / draw calls"
                elif "CANVAS" in error_source:
                    reader_source = "Canvas Vision / OCR"
                else:
                    reader_source = "DOM / Playwright"
                state_changes: dict[str, Any] = {
                    "market_reader": {
                        "source": reader_source,
                        "status": error.status,
                        "attempt": attempt,
                        "next_goal_number": next_goal_number,
                    },
                    "event": error.status,
                    "error": str(error),
                }
                canvas_details = error.details.get("canvas_details") or error.details
                recognized_team1 = canvas_details.get("recognized_team1")
                recognized_team2 = canvas_details.get("recognized_team2")
                if recognized_team1 is not None and recognized_team2 is not None:
                    state_changes["odds"] = {
                        "selected": None,
                        "opponent": None,
                        "team1": recognized_team1,
                        "team2": recognized_team2,
                        "market": (
                            f"Следующий гол №"
                            f"{canvas_details.get('recognized_goal_number')}"
                        ),
                        "source": error_source,
                        "backend": canvas_details.get("ocr_backend"),
                        "confidence": None,
                        "status": error.status,
                    }
                    state_changes["market_available"] = False
                    state_changes["odds_available"] = False
                await STATE.update(
                    **state_changes,
                )
                if (
                    attempt == 1
                    or attempt % 10 == 0
                    or "CANVAS" in error_source
                ):
                    await REPOSITORY.log(
                        error.status,
                        f"{error} | details={error.details}",
                    )
                await self._sleep_or_stop(CONFIG.ocr_retry_delay)
        return None

    def _blocked_score_monitoring_enabled(
        self,
        *,
        observation: PlacementObservation | None,
        selected_match: dict[str, Any],
    ) -> bool:
        """Enable score-aware recovery only for a definitive unaccepted coupon."""
        return bool(
            self._config.strategy_type == StrategyType.NEXT_GOAL
            and self._config.blocked_events_switch_enabled
            and observation is not None
            and not observation.placed
            and observation.signal == BLOCKED_EVENT_SIGNAL
            and next_goal_match_identity(selected_match)
        )

    async def _finish_missed_selected_team_goal(
        self,
        *,
        attempt_record: dict[str, Any],
        selected_match: dict[str, Any],
        cycle_id: str,
        step: int,
        amount: float,
        score_after_removal: ScoreboardSnapshot,
        placement_signal: str = BLOCKED_EVENT_SIGNAL,
        explanation: str = "Ставка не принята: выбранная команда забила во время блокировки",
    ) -> BlockedMatchSwitch:
        """Record a selected-team goal missed before activation and switch matches."""
        match_id = next_goal_match_identity(selected_match)
        blocked_record = await REPOSITORY.save_bet(
            {
                **attempt_record,
                "cycle_id": cycle_id,
                "match_id": match_id,
                "result": "MISSED_SELECTED_TEAM_GOAL",
                "status": "MISSED_SELECTED_TEAM_GOAL",
                "settled": True,
                "score_after": score_after_removal.score.text(),
                "resolved_at": local_now(),
                "placement_signal": placement_signal,
                "placement_result": "BLOCKED",
                "budget_change": 0,
                "pnl": 0,
                "explanation": explanation,
            }
        )
        await REPOSITORY.add_blocked_match(cycle_id, match_id)
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_MATCH_BLACKLISTED",
            f"[NEXT_GOAL][BLOCKED] match added to blocked_match_ids: {match_id}",
        )
        sequence = await REPOSITORY.save_sequence(
            current_step=step,
            status="WAITING_NEXT_MATCH",
            current_match_id=None,
            selected_team=None,
        )
        self._pending_live_bet = None
        self._active_live_bet = None
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_EVENT_DETECTED",
            "[NEXT_GOAL][BLOCKED] MISSED_SELECTED_TEAM_GOAL",
        )
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_STEP_PRESERVED",
            f"[NEXT_GOAL][BLOCKED] preserving step={step} stake={amount:g}",
        )
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_MATCH_SKIPPED",
            f"[NEXT_GOAL][BLOCKED] switching match; match_id={match_id}",
        )
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_RETURNING_TO_LEAGUE",
            "[NEXT_GOAL] Returning to league; searching next match",
        )
        await STATE.update(
            status=DemoStatus.WAITING_NEXT_MATCH.value,
            event="MISSED_SELECTED_TEAM_GOAL",
            message=f"Пропущен гол выбранной команды — продолжаем шаг {step} в другом матче",
            bet={**blocked_record, "max_steps": self._config.max_steps},
            sequence=sequence,
            budget=self._budget.snapshot(),
            stats=await REPOSITORY.stats(),
        )
        return BlockedMatchSwitch(match_id=match_id, step=step, amount=amount)

    async def _finish_live_blocked_coupon_switch(
        self,
        *,
        attempt_record: dict[str, Any],
        selected_match: dict[str, Any],
        cycle_id: str,
        step: int,
        amount: float,
        snapshot: ScoreboardSnapshot,
    ) -> BlockedMatchSwitch:
        """A confirmed locked LIVE coupon always moves the same step to next match."""
        match_id = next_goal_match_identity(selected_match)
        blocked_record = await REPOSITORY.save_bet(
            {
                **attempt_record,
                "cycle_id": cycle_id,
                "match_id": match_id,
                "result": "BLOCKED_NOT_PLACED",
                "status": "BLOCKED_NOT_PLACED",
                "settled": True,
                "score_after": snapshot.score.text(),
                "resolved_at": local_now(),
                "placement_signal": BLOCKED_EVENT_SIGNAL,
                "placement_result": "BLOCKED",
                "budget_change": 0,
                "pnl": 0,
                "explanation": (
                    "LIVE coupon показал точный текст «Заблокированное событие»; "
                    "ставка не принята, тот же шаг переносится на следующий матч"
                ),
            }
        )
        await REPOSITORY.add_blocked_match(cycle_id, match_id)
        sequence = await REPOSITORY.save_sequence(
            current_step=step,
            status="WAITING_NEXT_MATCH",
            current_match_id=None,
            selected_team=None,
        )
        self._pending_live_bet = None
        self._active_live_bet = None
        await REPOSITORY.log(
            "LIVE_BLOCKED_MATCH_SWITCH",
            (
                f"match_id={match_id}; step={step}; stake={amount:g}; "
                "confirmed coupon lock -> next match"
            ),
        )
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_STEP_PRESERVED",
            f"[NEXT_GOAL][BLOCKED] preserving step={step} stake={amount:g}",
        )
        await REPOSITORY.log(
            "NEXT_GOAL_BLOCKED_RETURNING_TO_LEAGUE",
            "[NEXT_GOAL] Returning to league; searching next match",
        )
        await STATE.update(
            status=DemoStatus.WAITING_NEXT_MATCH.value,
            event="LIVE_BLOCKED_MATCH_SWITCH",
            message=(
                f"Coupon заблокирован — продолжаем шаг {step} "
                "в ближайшем следующем матче"
            ),
            bet={**blocked_record, "max_steps": self._config.max_steps},
            sequence=sequence,
            budget=self._budget.snapshot(),
            stats=await REPOSITORY.stats(),
        )
        return BlockedMatchSwitch(match_id=match_id, step=step, amount=amount)

    async def _prepare_live_until_placed(
        self,
        *,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        selection: Any,
        match_name: str,
        cycle_id: str,
        step: int,
        amount: float,
        snapshot: ScoreboardSnapshot,
        current_odds: Any,
        record: dict[str, Any],
    ):
        """Prepare one strategy decision until the user places it or stops LIVE."""
        retrying_blocked_attempt = False
        blocked_recovery_score: Score | None = None
        blocked_recovery_record: dict[str, Any] | None = None
        while not self._stop_event.is_set():
            assert self._pending_live_bet is not None
            self._pending_live_bet = self._pending_live_bet.with_score(snapshot.score)
            target_goal = self._pending_live_bet.target_goal_number
            if current_odds.next_goal_number != target_goal:
                try:
                    odds_result = await self._wait_for_odds(
                        snapshot,
                        selected_match,
                        blocked_attempt_score=blocked_recovery_score,
                        blocked_selected_side=(
                            selection.selected_side if blocked_recovery_score else None
                        ),
                    )
                except MissedSelectedTeamGoal as missed:
                    if blocked_recovery_record is None:
                        raise
                    await REPOSITORY.log(
                        "NEXT_GOAL_BLOCKED_SAME_MATCH_RETRY_DISABLED",
                        "[NEXT_GOAL][BLOCKED] SAME-MATCH RETRY DISABLED",
                    )
                    return await self._finish_missed_selected_team_goal(
                        attempt_record=blocked_recovery_record,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        step=step,
                        amount=amount,
                        score_after_removal=missed.snapshot,
                    )
                if odds_result is None:
                    return None
                snapshot, current_odds = odds_result
                continue

            if retrying_blocked_attempt:
                verified = await self._read_fresh_score(
                    browser,
                    selected_match,
                    snapshot,
                )
                try:
                    await self._guard_blocked_recovery_score(
                        attempt_score=blocked_recovery_score,
                        current=verified,
                        selected_side=(
                            selection.selected_side if blocked_recovery_score else None
                        ),
                    )
                except MissedSelectedTeamGoal as missed:
                    if blocked_recovery_record is None:
                        raise
                    await REPOSITORY.log(
                        "NEXT_GOAL_BLOCKED_SAME_MATCH_RETRY_DISABLED",
                        "[NEXT_GOAL][BLOCKED] SAME-MATCH RETRY DISABLED",
                    )
                    return await self._finish_missed_selected_team_goal(
                        attempt_record=blocked_recovery_record,
                        selected_match=selected_match,
                        cycle_id=cycle_id,
                        step=step,
                        amount=amount,
                        score_after_removal=missed.snapshot,
                    )
                if verified.score != snapshot.score:
                    await REPOSITORY.log(
                        "LIVE_SCORE_CHANGED_BEFORE_RETRY",
                        f"{snapshot.score.text()} → {verified.score.text()}; "
                        f"strategy_step={step} unchanged",
                    )
                    snapshot = verified
                    self._pending_live_bet = self._pending_live_bet.with_score(
                        snapshot.score
                    )
                    try:
                        odds_result = await self._wait_for_odds(
                            snapshot,
                            selected_match,
                            blocked_attempt_score=blocked_recovery_score,
                            blocked_selected_side=selection.selected_side,
                        )
                    except MissedSelectedTeamGoal as missed:
                        if blocked_recovery_record is None:
                            raise
                        await REPOSITORY.log(
                            "NEXT_GOAL_BLOCKED_SAME_MATCH_RETRY_DISABLED",
                            "[NEXT_GOAL][BLOCKED] SAME-MATCH RETRY DISABLED",
                        )
                        return await self._finish_missed_selected_team_goal(
                            attempt_record=blocked_recovery_record,
                            selected_match=selected_match,
                            cycle_id=cycle_id,
                            step=step,
                            amount=amount,
                            score_after_removal=missed.snapshot,
                        )
                    if odds_result is None:
                        return None
                    snapshot, current_odds = odds_result
                    continue
                retrying_blocked_attempt = False
                blocked_recovery_score = None
                blocked_recovery_record = None
                await REPOSITORY.log(
                    "LIVE_BLOCKED_RETRY_MARKET_READY",
                    f"step={step} stake={amount} next_goal={target_goal}",
                )

            selected_odd, opponent_odd = odds_for_selected_side(
                current_odds, selection.selected_side
            )
            coefficient_locator = current_odds.locator_for_side(selection.selected_side)
            placement_snapshot = snapshot
            self._live_attempt_counter += 1
            attempt_id = (
                f"{self._pending_live_bet.decision_id}_goal{target_goal}_"
                f"attempt{self._live_attempt_counter}_{uuid4().hex[:8]}"
            )
            attempt_record = {
                **record,
                "id": attempt_id,
                "attempt_id": attempt_id,
                "decision_id": self._pending_live_bet.decision_id,
                "mode": "LIVE",
                "cycle_id": cycle_id,
                "match_id": selected_match.get("match_id"),
                "match": match_name,
                "selected_team": selection.selected_team,
                "selected_side": selection.selected_side.value,
                "step": step,
                "amount": amount,
                "odds": selected_odd,
                "score_before": placement_snapshot.score.text(),
                "attempt_score_before": placement_snapshot.score.text(),
                "market": current_odds.market,
                "next_goal_number": target_goal,
                "result": "PENDING",
                "status": LiveStatus.IDLE.value,
                "settled": False,
                "created_at": local_now(),
                "resolved_at": None,
                "budget_before": None,
                "budget_change": None,
                "budget_after": None,
            }
            decision = LiveDecision(
                attempt_id=attempt_id,
                match_id=str(selected_match.get("match_id") or cycle_id),
                team=selection.selected_team,
                side=selection.selected_side,
                strategy_step=step,
                amount=amount,
                goal_number=target_goal,
                coefficient=selected_odd,
                coefficient_locator=coefficient_locator,
            )
            await REPOSITORY.save_bet(attempt_record)

            async def publish_live(status: LiveStatus, message: str) -> None:
                attempt_record["status"] = status.value
                await REPOSITORY.save_bet(attempt_record)
                await STATE.update(
                    mode="LIVE",
                    status=status.value,
                    message=message,
                    event=status.value,
                    odds=self._odds_state(current_odds, selected_odd, opponent_odd),
                    bet={**attempt_record, "max_steps": self._config.max_steps},
                )

            try:
                page = await self.browser_manager.ensure_page()
                await self.live_executor.prepare(page, decision, publish_live)
                fresh = await self._read_fresh_score(browser, selected_match, placement_snapshot)
                clicked = await self.live_executor.manual_click_seen(attempt_id)
                if fresh.score != placement_snapshot.score and not clicked:
                    await self._invalidate_live_attempt(
                        attempt_record,
                        decision,
                        f"Счёт изменился {placement_snapshot.score.text()} → {fresh.score.text()} до подтверждения.",
                        publish_live,
                    )
                    snapshot = fresh
                    odds_result = await self._wait_for_odds(snapshot, selected_match)
                    if odds_result is None:
                        return None
                    snapshot, current_odds = odds_result
                    continue

                observation, latest = await self._wait_for_live_confirmation_or_score(
                    page,
                    browser,
                    selected_match,
                    placement_snapshot,
                    decision,
                    publish_live,
                )
                if observation is None:
                    attempt_record.update(
                        result=(
                            "SUBMISSION_UNKNOWN"
                            if await self.live_executor.manual_click_seen(attempt_id)
                            else "NOT_PLACED"
                        ),
                        status=self.live_executor.state(attempt_id).value,
                        resolved_at=local_now(),
                    )
                    await REPOSITORY.save_bet(attempt_record)
                    return None
                if not observation.placed:
                    if observation.signal == BLOCKED_EVENT_SIGNAL:
                        await REPOSITORY.log(
                            "NEXT_GOAL_BLOCKED_CONFIRMED",
                            (
                                f"[NEXT_GOAL][BLOCKED] match={match_name}; "
                                f"step={step}; stake={amount:g}; "
                                "switching immediately to next match"
                            ),
                        )
                        latest_snapshot = latest or placement_snapshot
                        await self._invalidate_live_attempt(
                            attempt_record,
                            decision,
                            observation.signal,
                            publish_live,
                        )
                        try:
                            await self.live_executor.remove_blocked_coupon(
                                page,
                                attempt_id,
                            )
                        except LivePreparationError as error:
                            await REPOSITORY.log(
                                error.status,
                                (
                                    f"{error}; continuing match switch because "
                                    "the exact blocked coupon was already confirmed"
                                ),
                            )
                        return await self._finish_live_blocked_coupon_switch(
                            attempt_record=attempt_record,
                            selected_match=selected_match,
                            cycle_id=cycle_id,
                            step=step,
                            amount=amount,
                            snapshot=latest_snapshot,
                        )

                    # Any non-blocked submission whose balance could not prove
                    # acceptance is never retried blindly: a second click could
                    # duplicate a real bet.
                    attempt_record.update(
                        result="SUBMISSION_UNKNOWN",
                        status="SUBMISSION_UNKNOWN",
                        settled=False,
                        resolved_at=local_now(),
                        placement_signal=observation.signal,
                    )
                    await REPOSITORY.save_bet(attempt_record)
                    await REPOSITORY.log(
                        "LIVE_SUBMISSION_UNKNOWN_NO_RETRY",
                        (
                            f"attempt={attempt_id}; signal={observation.signal}; "
                            "no second click"
                        ),
                    )
                    self._stop_event.set()
                    return None

            except LivePreparationError as error:
                attempt_record.update(
                    result="NOT_PLACED",
                    status=error.status,
                    settled=True,
                    resolved_at=local_now(),
                    error=str(error),
                )
                await REPOSITORY.save_bet(attempt_record)
                await REPOSITORY.log(error.status, str(error))
                if self.live_executor.market_was_selected(attempt_id):
                    await STATE.update(
                        mode="LIVE",
                        running=False,
                        status=LiveStatus.ERROR.value,
                        event=error.status,
                        message=(
                            "Подготовка LIVE остановлена после открытия coupon, "
                            "чтобы не создать повторную ставку."
                        ),
                        error=str(error),
                        bet={**attempt_record, "max_steps": self._config.max_steps},
                    )
                    self._stop_event.set()
                    return None
                fresh = await self._read_fresh_score(browser, selected_match, snapshot)
                snapshot = fresh
                await self._sleep_or_stop(CONFIG.ocr_retry_delay)
                odds_result = await self._wait_for_odds(snapshot, selected_match)
                if odds_result is None:
                    return None
                snapshot, current_odds = odds_result
                continue

            # ACCEPTED is the ordering boundary.  Keep the last score observed
            # before confirmation as the bet baseline: a goal that appears just
            # after acceptance must be settled normally instead of being hidden
            # by a post-confirmation baseline refresh.
            placement_snapshot = latest or placement_snapshot
            attempt_record.update(
                result="ACTIVE",
                status=LiveStatus.ACTIVE.value,
                score_before=placement_snapshot.score.text(),
                placement_signal=observation.signal,
                placement_confirmed_at=local_now(),
            )
            await REPOSITORY.save_bet(attempt_record)
            self._active_live_bet = ActiveLiveBet(
                attempt_id=attempt_id,
                match_id=decision.match_id,
                team=decision.team,
                side=decision.side,
                strategy_step=step,
                amount=amount,
                coefficient=selected_odd,
                goal_number=target_goal,
                score_before=placement_snapshot.score,
            )
            self._pending_live_bet = None
            return attempt_record, placement_snapshot, current_odds, selected_odd, opponent_odd
        return None

    async def _invalidate_live_attempt(
        self,
        record: dict[str, Any],
        decision: LiveDecision,
        reason: str,
        publish: Any,
    ) -> None:
        await self.live_executor.invalidate(decision.attempt_id, reason, publish)
        record.update(
            result="NOT_PLACED",
            status=LiveStatus.STALE_COUPON.value,
            settled=True,
            resolved_at=local_now(),
            placement_signal=reason,
        )
        await REPOSITORY.save_bet(record)

    async def _wait_for_live_confirmation_or_score(
        self,
        page: Any,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        snapshot: ScoreboardSnapshot,
        decision: LiveDecision,
        publish: Any,
    ) -> tuple[PlacementObservation | None, ScoreboardSnapshot | None]:
        confirmation_task = asyncio.create_task(
            self.live_executor.wait_for_manual_confirmation(
                page, decision, self._stop_event, publish
            )
        )
        latest = snapshot
        score_task = asyncio.create_task(
            self._wait_for_pending_score_change(browser, selected_match, latest)
        )
        try:
            while not self._stop_event.is_set():
                done, _ = await asyncio.wait(
                    {confirmation_task, score_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if confirmation_task in done:
                    return confirmation_task.result(), latest
                if score_task in done:
                    changed = score_task.result()
                    if changed is None:
                        return None, latest
                    latest = changed
                    if not await self.live_executor.manual_click_seen(decision.attempt_id):
                        confirmation_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await confirmation_task
                        return PlacementObservation(
                            False,
                            "Счёт изменился до ручного подтверждения",
                            retryable=True,
                        ), latest
                    score_task = asyncio.create_task(
                        self._wait_for_pending_score_change(browser, selected_match, latest)
                    )
            return None, latest
        finally:
            for task in (confirmation_task, score_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _wait_for_pending_score_change(
        self,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        previous: ScoreboardSnapshot,
    ) -> ScoreboardSnapshot | None:
        while not self._stop_event.is_set():
            await self._sleep_or_stop(CONFIG.score_poll_interval)
            try:
                current = await browser.snapshot()
            except ScoreReadError:
                continue
            if current.team1 != previous.team1 or current.team2 != previous.team2:
                raise RecoverableDemoError(
                    "SCOREBOARD_TEAMS_CHANGED",
                    "Порядок или названия команд в scoreboard изменились.",
                )
            await self._publish_snapshot(current, selected_match, state="LIVE")
            if current.score != previous.score:
                if self._pending_live_bet is not None:
                    self._pending_live_bet = self._pending_live_bet.with_score(current.score)
                await REPOSITORY.log(
                    "LIVE_SCORE_ONLY",
                    f"{previous.score.text()} → {current.score.text()}; ACTIVE-ставки ещё нет",
                )
                return current
        return None

    async def _publish_pending_bet(
        self,
        selection,
        match_name: str,
        step: int,
        snapshot: ScoreboardSnapshot,
    ) -> None:
        side_label = (
            "Команда 1" if selection.selected_side == Scorer.TEAM_1 else "Команда 2"
        )
        await STATE.update(
            bet={
                "step": step,
                "max_steps": self._config.max_steps,
                "amount": float(self._config.stakes[step - 1]),
                "selected_team": selection.selected_team,
                "selected_side": selection.selected_side.value,
                "side_label": side_label,
                "match": match_name,
                "market": f"Следующий гол №{snapshot.score.team1 + snapshot.score.team2 + 1}",
                "odds": None,
                "score_before": snapshot.score.text(),
                "next_goal_number": snapshot.score.team1 + snapshot.score.team2 + 1,
                "status": "WAITING_FOR_MARKET",
                "budget_before": float(self._budget.current_budget),
            }
        )

    async def _skip_match_for_missed_event(
        self,
        selected_match: dict[str, Any],
        next_step: int,
        snapshot: ScoreboardSnapshot,
    ) -> None:
        """Close only the current match; preserve the pending series step."""
        await REPOSITORY.save_sequence(
            current_step=next_step,
            status="WAITING_NEXT_MATCH",
            current_match_id=None,
            selected_team=None,
        )
        await self._status(
            DemoStatus.WAITING_NEXT_MATCH,
            "Матч закрыт: scoreboard изменился до новой ставки; серия продолжится на следующем матче",
            "MATCH_SKIPPED_MISSED_EVENT",
            sequence=await REPOSITORY.get_sequence(),
            bet={
                "step": next_step,
                "max_steps": self._config.max_steps,
                "amount": float(self._config.stakes[next_step - 1]),
                "score_before": snapshot.score.text(),
                "status": "WAITING_NEXT_MATCH",
            },
        )
        await REPOSITORY.log("MATCH_SKIPPED_MISSED_EVENT", snapshot.score.text())

    async def _read_fresh_score(
        self,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        previous: ScoreboardSnapshot,
    ) -> ScoreboardSnapshot:
        for attempt in range(20):
            if self._stop_event.is_set():
                raise asyncio.CancelledError
            try:
                current = await browser.snapshot()
                if current.team1 != previous.team1 or current.team2 != previous.team2:
                    raise RecoverableDemoError(
                        "SCOREBOARD_TEAMS_CHANGED",
                        "Порядок или названия команд в scoreboard изменились.",
                    )
                await self._publish_snapshot(
                    current,
                    selected_match,
                    state="LIVE" if current.period else "UPCOMING",
                )
                return current
            except ScoreReadError:
                if attempt == 0:
                    await REPOSITORY.log(
                        "SCORE_TEMPORARILY_UNAVAILABLE", "Повтор чтения scoreboard"
                    )
                await self._sleep_or_stop(CONFIG.score_poll_interval)
        raise RecoverableDemoError("SCORE_TEMPORARILY_UNAVAILABLE", "Scoreboard не готов")

    async def _wait_for_goal(
        self,
        browser: MatchBrowser,
        selected_match: dict[str, Any],
        previous: ScoreboardSnapshot,
        *,
        active_odds: Any | None = None,
        selected_side: Scorer | None = None,
        step: int | None = None,
        bet_id: str | None = None,
    ) -> tuple[ScoreboardSnapshot, Scorer] | None:
        await self._status(
            DemoStatus.WAITING_FOR_GOAL,
            f"Ожидаем ровно один следующий гол от {previous.score.text()}",
            "WAITING_FOR_NEXT_GOAL",
        )
        await REPOSITORY.log("WAITING_FOR_NEXT_GOAL", previous.score.text())

        lock_context = self._active_bet_lock_context or {}
        if active_odds is None:
            active_odds = lock_context.get("active_odds")
        if selected_side is None:
            selected_side = lock_context.get("selected_side")
        if step is None:
            step = lock_context.get("step")
        if bet_id is None:
            bet_id = lock_context.get("bet_id")

        lock_monitor_enabled = bool(
            self._config.blocked_events_switch_enabled
            and active_odds is not None
            and str(getattr(active_odds, "source", "") or "") == "CANVAS_2D"
            and getattr(active_odds, "next_goal_number", None) is not None
        )
        target_goal = (
            int(getattr(active_odds, "next_goal_number"))
            if lock_monitor_enabled
            else None
        )
        selected_side_number = (
            1
            if selected_side == Scorer.TEAM_1
            else (2 if selected_side == Scorer.TEAM_2 else None)
        )
        active_lock = False
        lock_started_at: float | None = None
        lock_seen_since_bet = False
        last_lock_release_at: float | None = None
        last_lock_duration_ms: int | None = None
        last_locked_sides: tuple[int, ...] = ()
        lock_read_errors = 0

        if lock_monitor_enabled:
            await REPOSITORY.log(
                "ACTIVE_BET_LOCK_MONITOR_STARTED",
                (
                    f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                    f"selected_side={selected_side_number}; score={previous.score.text()}; "
                    "Canvas lock monitoring active while bet is waiting for goal"
                ),
            )

        read_errors = 0
        while not self._stop_event.is_set():
            await self._sleep_or_stop(CONFIG.score_poll_interval)

            if lock_monitor_enabled and target_goal is not None:
                try:
                    lock_state = await read_next_goal_lock_state(
                        browser.page,
                        target_goal,
                        logger=REPOSITORY.log,
                    )
                    if lock_state.get("available"):
                        locked_sides = tuple(
                            sorted(
                                {
                                    int(side)
                                    for side in (
                                        tuple(lock_state.get("locked_sides") or ())
                                        + tuple(
                                            lock_state.get("recent_locked_sides") or ()
                                        )
                                    )
                                }
                            )
                        )
                        now = time.monotonic()
                        lock_now = bool(locked_sides)
                        selected_locked = bool(
                            selected_side_number is not None
                            and selected_side_number in locked_sides
                        )

                        if lock_now and not active_lock:
                            active_lock = True
                            lock_seen_since_bet = True
                            lock_started_at = now
                            last_locked_sides = locked_sides
                            marker_parts: list[str] = []
                            for side in locked_sides:
                                marker = (
                                    (lock_state.get("markers") or {}).get(str(side))
                                    or (
                                        lock_state.get("recent_markers") or {}
                                    ).get(str(side))
                                    or {}
                                )
                                marker_parts.append(
                                    f"side={side}:reason={marker.get('reason')}:"
                                    f"canvas={marker.get('detected_canvas_id') or marker.get('canvas_id')}:"
                                    f"seq={marker.get('seq')}"
                                )
                            await REPOSITORY.log(
                                "ACTIVE_BET_LOCK_ACTIVE",
                                (
                                    f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                                    f"score={previous.score.text()}; "
                                    f"locked_sides={list(locked_sides)}; "
                                    f"selected_side={selected_side_number}; "
                                    f"selected_side_locked={selected_locked}; "
                                    f"checked_canvas_ids={list(lock_state.get('checked_canvas_ids') or ())}; "
                                    f"markers=[{' | '.join(marker_parts)}]"
                                ),
                            )
                            await STATE.update(
                                market_locked=selected_locked,
                                event="ACTIVE_BET_LOCK_ACTIVE",
                                message=(
                                    f"БК заблокировал рынок при активной ставке: "
                                    f"шаг {step}, стороны {list(locked_sides)}"
                                ),
                            )
                        elif not lock_now and active_lock:
                            active_lock = False
                            duration_ms = int(
                                max(0.0, now - (lock_started_at or now)) * 1000
                            )
                            last_lock_duration_ms = duration_ms
                            last_lock_release_at = now
                            await REPOSITORY.log(
                                "ACTIVE_BET_LOCK_RELEASED",
                                (
                                    f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                                    f"score={previous.score.text()}; "
                                    f"previous_locked_sides={list(last_locked_sides)}; "
                                    f"duration_ms={duration_ms}; "
                                    f"released_sides={list(lock_state.get('released_sides') or ())}"
                                ),
                            )
                            await STATE.update(
                                market_locked=False,
                                event="ACTIVE_BET_LOCK_RELEASED",
                                message=(
                                    f"Блокировка БК при активной ставке снята; "
                                    f"длительность {duration_ms} мс"
                                ),
                            )
                            lock_started_at = None
                            last_locked_sides = ()
                        elif lock_now:
                            last_locked_sides = locked_sides

                        lock_read_errors = 0
                    else:
                        lock_read_errors += 1
                        if lock_read_errors == 1 or lock_read_errors % 20 == 0:
                            await REPOSITORY.log(
                                "ACTIVE_BET_LOCK_STATE_UNAVAILABLE",
                                (
                                    f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                                    f"reason={lock_state.get('reason')}"
                                ),
                            )
                except Exception as error:
                    lock_read_errors += 1
                    if lock_read_errors == 1 or lock_read_errors % 20 == 0:
                        await REPOSITORY.log(
                            "ACTIVE_BET_LOCK_MONITOR_ERROR",
                            (
                                f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                                f"{type(error).__name__}: {error}"
                            ),
                        )

            try:
                current = await browser.snapshot()
            except ScoreReadError as error:
                read_errors += 1
                if read_errors == 1 or read_errors % 20 == 0:
                    await REPOSITORY.log("SCORE_TEMPORARILY_UNAVAILABLE", str(error))
                continue
            read_errors = 0
            if current.team1 != previous.team1 or current.team2 != previous.team2:
                raise RecoverableDemoError(
                    "SCOREBOARD_TEAMS_CHANGED",
                    "Порядок или названия команд в scoreboard изменились.",
                )
            await self._publish_snapshot(current, selected_match, state="LIVE")
            scorer = detect_scorer(previous.score, current.score)
            if scorer == Scorer.UNKNOWN:
                continue

            if lock_monitor_enabled:
                now = time.monotonic()
                release_ago_ms = (
                    int(max(0.0, now - last_lock_release_at) * 1000)
                    if last_lock_release_at is not None
                    else None
                )
                await REPOSITORY.log(
                    "ACTIVE_BET_GOAL_LOCK_CONTEXT",
                    (
                        f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                        f"score={previous.score.text()}->{current.score.text()}; "
                        f"lock_active_at_detection={active_lock}; "
                        f"lock_seen_since_bet={lock_seen_since_bet}; "
                        f"locked_sides={list(last_locked_sides)}; "
                        f"last_lock_duration_ms={last_lock_duration_ms}; "
                        f"last_lock_release_ms_ago={release_ago_ms}"
                    ),
                )
                if active_lock:
                    await REPOSITORY.log(
                        "ACTIVE_BET_GOAL_DURING_LOCK",
                        (
                            f"bet_id={bet_id}; step={step}; goal={target_goal}; "
                            f"score={previous.score.text()}->{current.score.text()}; "
                            f"locked_sides={list(last_locked_sides)}; "
                            "score changed while Canvas lock was still active"
                        ),
                    )

            await STATE.update(
                status=DemoStatus.GOAL_DETECTED.value,
                message=f"Счёт изменился: {previous.score.text()} → {current.score.text()}",
                event="GOAL_DETECTED",
            )
            return current, scorer
        return None

    async def _publish_snapshot(
        self,
        snapshot: ScoreboardSnapshot,
        selected_match: dict[str, Any],
        *,
        state: str,
    ) -> None:
        match = snapshot.to_dict()
        match.update(
            id=selected_match.get("match_id"),
            url=selected_match.get("url"),
            state=state,
        )
        changes: dict[str, Any] = {"match": match}
        if self._config.strategy_type == StrategyType.FIRST_HALF_DRAW:
            changes.update(score=snapshot.score.text())
        await STATE.update(**changes)

    @staticmethod
    def _odds_state(
        odds,
        selected: float | None,
        opponent: float | None,
    ) -> dict[str, Any]:
        return {
            "selected": selected,
            "opponent": opponent,
            "team1": odds.team1,
            "team2": odds.team2,
            "market": odds.market,
            "source": odds.source,
            "backend": odds.ocr_backend,
            "confidence": odds.confidence,
            "status": "ODDS_CONFIRMED",
        }

    async def _recover(self, event: str, message: str) -> None:
        await REPOSITORY.log(event, message)
        await self._status(
            DemoStatus.RECOVERING,
            message,
            event,
            browser=await self.browser_manager.snapshot(),
        )
        await self._sleep_or_stop(CONFIG.league_retry_interval)

    async def _status(
        self,
        status: DemoStatus,
        message: str,
        event: str,
        **changes: Any,
    ) -> None:
        await STATE.update(
            status=status.value,
            message=message,
            event=event,
            error=None,
            **changes,
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass


ENGINE = DemoEngine()
