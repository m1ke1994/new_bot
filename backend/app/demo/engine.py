import asyncio
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Any
from uuid import uuid4

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from auth import authorize
from backend.app.browser.canvas_vision import VISION, load_latest_analysis
from backend.app.browser.league import LeagueBrowser, MatchAlreadyStarted
from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from backend.app.browser.market import MarketReadError, market_canvas_debug, read_next_goal_odds
from backend.app.browser.match import MatchBrowser
from backend.app.browser.scoreboard import ScoreReadError

from .config import CONFIG
from .history import REPOSITORY
from .models import DemoStatus, Scorer, ScoreboardSnapshot
from .state import STATE
from .strategy import (
    BET_STEPS,
    detect_scorer,
    odds_for_selected_side,
    select_team_with_higher_odds,
)


RECOVERABLE_MARKET_STATUSES = {
    "CANVAS_NOT_READY",
    "CANVAS_NOT_VISIBLE",
    "CANVAS_CAPTURE_INVALID",
    "OCR_EMPTY",
    "OCR_FAILED",
    "ODDS_NOT_FOUND",
    "ODDS_MAPPING_UNCERTAIN",
    "ODDS_UNSTABLE",
    "ELEMENT_NOT_READY",
    "PAGE_LOADING",
}


class RecoverableDemoError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def local_now() -> str:
    return datetime.now().astimezone().isoformat()


class DemoEngine:
    def __init__(self, browser_manager: BrowserManager = BROWSER_MANAGER) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._market_lock = asyncio.Lock()
        self.browser_manager = browser_manager
        self.browser_manager.set_logger(REPOSITORY.log)
        self._authorized_generation = -1

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def report_browser_start_error(self, error: Exception) -> None:
        await REPOSITORY.log(
            "BROWSER_START_ERROR",
            f"{type(error).__name__}: {error}",
        )
        await STATE.update(
            browser=await self.browser_manager.snapshot(),
            error="Chromium пока не запущен; используйте /api/browser/start для повтора.",
        )

    async def start(self) -> dict[str, Any]:
        async with self._control_lock:
            if self._task is not None and not self._task.done():
                await REPOSITORY.log("DEMO_ALREADY_RUNNING", "Второй worker не создан")
                await STATE.update(event="DEMO_ALREADY_RUNNING")
                return await STATE.snapshot()

            CONFIG.validate()
            self._stop_event.clear()
            await STATE.reset_for_start(await REPOSITORY.stats())
            await STATE.update(
                status=DemoStatus.STARTING.value,
                browser=await self.browser_manager.snapshot(),
            )
            await REPOSITORY.log("DEMO_START", "Запущен background DEMO worker")
            self._task = asyncio.create_task(self._run_guarded(), name="demo-worker")
            return await STATE.snapshot()

    async def stop(self) -> dict[str, Any]:
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
        await REPOSITORY.log("DEMO_STOP", "DEMO worker остановлен; browser не закрывался")
        await STATE.update(
            running=False,
            status=DemoStatus.STOPPED.value,
            message="Демо остановлено",
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
                message="DEMO worker остановлен из-за fatal error",
                error=f"{type(error).__name__}: {error}",
                event="FATAL_ERROR",
                browser=await self.browser_manager.snapshot(),
            )
        finally:
            if self._stop_event.is_set():
                await STATE.update(running=False)

    async def _run(self) -> None:
        backends = await asyncio.to_thread(VISION.backend_status)
        await REPOSITORY.log("OCR_BACKENDS", str(backends))

        while not self._stop_event.is_set():
            try:
                page = await self.browser_manager.ensure_page()
                await STATE.update(browser=await self.browser_manager.snapshot())
                await self._ensure_authorized(page)
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

    async def _ensure_authorized(self, page: Page) -> None:
        generation = self.browser_manager.generation
        if generation == self._authorized_generation:
            await STATE.update(auth={"status": "AUTHORIZED_ALREADY"})
            return
        await self._status(DemoStatus.AUTH_CHECK, "Проверяем авторизацию", "AUTH_CHECK")
        result = await authorize(page, self._stop_event)
        if self._stop_event.is_set():
            return
        status = result.get("status", "AUTH_NOT_CONFIRMED")
        await STATE.update(auth={"status": status})
        if not result.get("ok"):
            raise RecoverableDemoError(status, "Авторизация пока не подтверждена")
        self._authorized_generation = generation
        await self._status(DemoStatus.AUTHORIZED, "Авторизация подтверждена", status)
        await REPOSITORY.log(status, "Authorized browser session is ready")

    async def _process_next_match(self, page: Page) -> None:
        page = await self.browser_manager.ensure_page()
        league = LeagueBrowser(page)
        await self._status(DemoStatus.OPENING_LEAGUE, "Открываем страницу лиги", "LEAGUE_OPENING")
        await league.open()
        await REPOSITORY.log("LEAGUE_OPENED", CONFIG.league_name)

        selected_match = None
        while selected_match is None and not self._stop_event.is_set():
            page = await self.browser_manager.ensure_page()
            league = LeagueBrowser(page)
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
                f"Всего: {stats['total']}; Started: {stats['started']}; Upcoming: {stats['upcoming']}",
            )
            for item in league.skipped_started:
                await REPOSITORY.log(
                    "MATCH_SKIPPED_STARTED",
                    f"{item['team1']} — {item['team2']} / period={item['period']}",
                )
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
            await self._status(
                DemoStatus.NO_UPCOMING_MATCHES,
                "Предстоящих матчей пока нет",
                "NO_UPCOMING_MATCHES",
            )
            await self._sleep_or_stop(CONFIG.league_retry_interval)

        if selected_match is None or self._stop_event.is_set():
            return

        cycle_id = uuid4().hex
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
        await self._status(DemoStatus.OPENING_MATCH, "Открываем выбранный матч", "OPEN_MATCH")
        try:
            opened = await league.open_match(selected_match)
        except MatchAlreadyStarted as error:
            await REPOSITORY.log("MATCH_ALREADY_STARTED", str(error))
            return
        await REPOSITORY.log("MATCH_OPENED", opened["url"])
        VISION.invalidate_layout()

        snapshot = await self._wait_for_match_start(selected_match)
        if snapshot is None:
            return
        browser = MatchBrowser(await self.browser_manager.ensure_page())
        initial_odds = await self._wait_for_odds(snapshot)
        if initial_odds is None:
            return
        snapshot = await self._read_fresh_score(browser, selected_match, snapshot)
        selection = select_team_with_higher_odds(snapshot.team1, snapshot.team2, initial_odds)
        await STATE.update(
            status=DemoStatus.TEAM_SELECTED.value,
            message=f"Выбрана команда {selection.selected_team}: коэффициент выше",
            event="TEAM_SELECTED",
            selected_team=selection.selected_team,
            other_team=selection.other_team,
            selection_reason="HIGHER_ODDS",
            odds=self._odds_state(initial_odds, selection.selected_odds, selection.other_odds),
        )
        await REPOSITORY.log(
            "TEAM_SELECTED", f"{selection.selected_team} @ {selection.selected_odds} / HIGHER_ODDS"
        )

        won = False
        ambiguous_cycle = False
        for step, amount in enumerate(BET_STEPS, start=1):
            if self._stop_event.is_set():
                return
            if step == 1:
                current_odds = initial_odds
            else:
                current_odds = await self._wait_for_odds(snapshot)
                if current_odds is None:
                    return
            browser = MatchBrowser(await self.browser_manager.ensure_page())
            snapshot = await self._read_fresh_score(browser, selected_match, snapshot)
            selected_odd, opponent_odd = odds_for_selected_side(
                current_odds, selection.selected_side
            )
            score_before = snapshot.score
            created_at = local_now()
            bet_id = uuid4().hex
            await STATE.update(
                status=DemoStatus.BET_SIMULATED.value,
                message=f"DEMO BET #{step}: {amount} RUB @ {selected_odd}",
                event="BET_SIMULATED",
                odds=self._odds_state(current_odds, selected_odd, opponent_odd),
                bet={
                    "id": bet_id,
                    "step": step,
                    "max_steps": len(BET_STEPS),
                    "amount": amount,
                    "selected_team": selection.selected_team,
                    "market": current_odds.market,
                    "odds": selected_odd,
                    "score_before": score_before.text(),
                    "next_goal_number": current_odds.next_goal_number,
                    "status": "WAITING_FOR_NEXT_GOAL",
                    "created_at": created_at,
                },
            )
            await REPOSITORY.log(
                "BET_SIMULATED",
                f"#{step}: {selection.selected_team}, {amount} RUB @ {selected_odd}, score={score_before.text()}",
            )

            goal = await self._wait_for_goal(browser, selected_match, snapshot)
            if goal is None:
                return
            new_snapshot, scorer = goal
            common_record = {
                "id": bet_id,
                "cycle_id": cycle_id,
                "match_id": selected_match.get("match_id"),
                "match": match_name,
                "selected_team": selection.selected_team,
                "step": step,
                "amount": amount,
                "odds": selected_odd,
                "score_before": score_before.text(),
                "score_after": new_snapshot.score.text(),
                "market": current_odds.market,
                "next_goal_number": current_odds.next_goal_number,
                "created_at": created_at,
                "resolved_at": local_now(),
            }

            if scorer == Scorer.AMBIGUOUS_SCORE_CHANGE:
                ambiguous_cycle = True
                record = await REPOSITORY.add_bet(
                    {**common_record, "scorer": "Не определён", "result": "AMBIGUOUS"}
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
                )
                snapshot = new_snapshot
                if step < len(BET_STEPS):
                    await self._status(
                        DemoStatus.NEXT_STEP,
                        f"Неоднозначный score delta; безопасный переход к шагу {step + 1}",
                        "NEXT_STEP",
                    )
                continue

            scorer_name = (
                new_snapshot.team1 if scorer == Scorer.TEAM_1 else new_snapshot.team2
            )
            result = "WIN" if scorer == selection.selected_side else "LOSE"
            record = await REPOSITORY.add_bet(
                {**common_record, "scorer": scorer_name, "result": result}
            )
            await REPOSITORY.log(
                "SCORE_CHANGED", f"{score_before.text()} → {new_snapshot.score.text()}"
            )
            await REPOSITORY.log("GOAL_DETECTED", scorer_name)
            await REPOSITORY.log(result, selection.selected_team)
            await STATE.update(
                status=(DemoStatus.WIN if result == "WIN" else DemoStatus.LOSE).value,
                message=f"Результат виртуальной ставки: {result}",
                event=result,
                last_change={
                    "before": record["score_before"],
                    "after": record["score_after"],
                    "scorer": scorer_name,
                    "result": result,
                },
                stats=await REPOSITORY.stats(),
            )
            snapshot = new_snapshot
            if result == "WIN":
                won = True
                await REPOSITORY.add_cycle(
                    {"cycle_id": cycle_id, "match": match_name, "result": "WIN", "steps": step}
                )
                await REPOSITORY.log("STRATEGY_CYCLE_WON", match_name)
                break
            if step < len(BET_STEPS):
                await self._status(
                    DemoStatus.NEXT_STEP, f"Переход к шагу {step + 1}", "NEXT_STEP"
                )

        if not won and not self._stop_event.is_set():
            await REPOSITORY.add_cycle(
                {
                    "cycle_id": cycle_id,
                    "match": match_name,
                    "result": "SEQUENCE_EXHAUSTED",
                    "steps": len(BET_STEPS),
                    "had_ambiguous": ambiguous_cycle,
                }
            )
            await self._status(
                DemoStatus.SEQUENCE_EXHAUSTED,
                "Все 11 шагов исчерпаны",
                "SEQUENCE_EXHAUSTED",
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log("SEQUENCE_EXHAUSTED", match_name)

        if not self._stop_event.is_set():
            await self._status(
                DemoStatus.RETURNING_TO_LEAGUE,
                "Возвращаемся в лигу и заново читаем DOM",
                "RETURNING_TO_LEAGUE",
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log("RETURNING_TO_LEAGUE", CONFIG.league_url)

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
                await self._publish_snapshot(snapshot, selected_match, state="LIVE")
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

    async def _wait_for_odds(self, snapshot: ScoreboardSnapshot):
        for attempt in range(1, CONFIG.ocr_max_attempts + 1):
            if self._stop_event.is_set():
                return None
            await self._status(
                DemoStatus.WAITING_FOR_ODDS,
                f"OCR attempt {attempt}/{CONFIG.ocr_max_attempts}",
                "OCR_ATTEMPT",
            )
            await STATE.update(
                ocr={
                    "status": "RUNNING",
                    "attempt": attempt,
                    "max_attempts": CONFIG.ocr_max_attempts,
                    "canvas": None,
                    "engine": None,
                    "latency_seconds": None,
                    "candidates": [],
                    "market_bbox": None,
                }
            )
            await REPOSITORY.log(
                "OCR_ATTEMPT", f"{attempt}/{CONFIG.ocr_max_attempts}"
            )
            try:
                page = await self.browser_manager.ensure_page()
                async with self._market_lock:
                    odds = await read_next_goal_odds(
                        page, snapshot.team1, snapshot.team2, REPOSITORY.log
                    )
                analysis = load_latest_analysis() or {}
                await self._publish_ocr(analysis, "READY", attempt)
                await self._status(
                    DemoStatus.ODDS_READY,
                    f"Коэффициенты готовы: {odds.team1} / {odds.team2}",
                    "ODDS_READY",
                )
                return odds
            except MarketReadError as error:
                analysis = error.details.get("analysis") or load_latest_analysis() or {}
                await self._publish_ocr(analysis, error.status, attempt)
                event = error.status if error.status in RECOVERABLE_MARKET_STATUSES else "OCR_FAILED"
                await REPOSITORY.log(event, str(error))
                if attempt < CONFIG.ocr_max_attempts:
                    await REPOSITORY.log("OCR_RETRY", f"Следующая попытка {attempt + 1}")
                    await self._sleep_or_stop(CONFIG.ocr_retry_delay)

        await self._status(
            DemoStatus.RECOVERING,
            "Коэффициенты недоступны после серии OCR попыток; возвращаемся к scan",
            "ODDS_UNAVAILABLE",
        )
        await REPOSITORY.log("ODDS_UNAVAILABLE", "OCR attempts exhausted")
        return None

    async def _publish_ocr(
        self, analysis: dict[str, Any], status: str, attempt: int
    ) -> None:
        await STATE.update(
            ocr={
                "status": status,
                "attempt": attempt,
                "max_attempts": CONFIG.ocr_max_attempts,
                "canvas": analysis.get("canvas"),
                "engine": analysis.get("ocr_backend"),
                "latency_seconds": analysis.get("latency_seconds"),
                "candidates": [item.get("value") for item in analysis.get("numbers", [])],
                "market_bbox": analysis.get("market_bbox"),
            }
        )

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
                await self._publish_snapshot(current, selected_match, state="LIVE")
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
    ) -> tuple[ScoreboardSnapshot, Scorer] | None:
        await self._status(
            DemoStatus.WAITING_FOR_GOAL,
            f"Ожидаем ровно один следующий гол от {previous.score.text()}",
            "WAITING_FOR_NEXT_GOAL",
        )
        await REPOSITORY.log("WAITING_FOR_NEXT_GOAL", previous.score.text())
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
            await self._publish_snapshot(current, selected_match, state="LIVE")
            scorer = detect_scorer(previous.score, current.score)
            if scorer == Scorer.UNKNOWN:
                continue
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
        await STATE.update(match=match)

    @staticmethod
    def _odds_state(odds, selected: float, opponent: float) -> dict[str, Any]:
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
