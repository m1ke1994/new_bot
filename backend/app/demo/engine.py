import asyncio
import traceback
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from auth import authorize
from backend.app.browser.canvas_vision import load_latest_analysis
from backend.app.browser.league import LeagueBrowser, MatchAlreadyStarted
from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from backend.app.browser.market import MarketReadError, market_canvas_debug, read_next_goal_odds
from backend.app.browser.match import MatchBrowser
from backend.app.browser.scoreboard import ScoreReadError

from .budget import DemoBudget
from .config import CONFIG
from .history import REPOSITORY
from .models import DemoStatus, Scorer, ScoreboardSnapshot
from .state import STATE
from .strategy import (
    DEFAULT_STRATEGY_CONFIG,
    ScoreProgression,
    StrategyConfig,
    can_create_initial_bet,
    detect_scorer,
    odds_for_selected_side,
    select_team_with_higher_odds,
    validate_score_progression,
)


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
        self._auth_status = "UNKNOWN"
        self._budget = DemoBudget()
        self._config = DEFAULT_STRATEGY_CONFIG

    async def restore(self) -> None:
        """Hydrate the in-memory dashboard from durable storage after a restart."""
        await REPOSITORY.initialize()
        config_data = await REPOSITORY.get_config()
        self._config = StrategyConfig.from_payload(config_data)
        budget = await REPOSITORY.get_budget()
        self._budget.restore(budget["initial_budget"], budget["current_budget"])
        sequence = await REPOSITORY.get_sequence()
        active = await REPOSITORY.active_bet()
        if active is not None:
            # An interrupted ACTIVE bet is retained as history and never duplicated.
            sequence = await REPOSITORY.save_sequence(status="RECOVERY_REQUIRED")
        await STATE.restore(budget=budget, strategy_config=config_data, sequence=sequence, stats=await REPOSITORY.stats())
        if active is not None:
            await STATE.update(status=DemoStatus.RECOVERING.value, message="ACTIVE ставка ожидает ручной reconciliation после рестарта", bet={**active, "max_steps": self._config.max_steps})

    async def save_strategy_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise ValueError("Stop the strategy before changing its stake row")
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

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def start(self) -> dict[str, Any]:
        async with self._control_lock:
            if self._task is not None and not self._task.done():
                await REPOSITORY.log("DEMO_ALREADY_RUNNING", "Второй worker не создан")
                await STATE.update(event="DEMO_ALREADY_RUNNING")
                return await STATE.snapshot()

            CONFIG.validate()
            await self.restore()
            sequence = await REPOSITORY.get_sequence()
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
            await REPOSITORY.save_sequence(status="WAITING_FOR_MATCH")
            await STATE.update(
                status=DemoStatus.STARTING.value,
                browser=await self.browser_manager.snapshot(),
                budget=self._budget.snapshot(),
                strategy_config=(await REPOSITORY.get_config()),
                sequence=(await REPOSITORY.get_sequence()),
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
        page = await self.browser_manager.ensure_page()
        league = LeagueBrowser(page)
        await self._status(DemoStatus.OPENING_LEAGUE, "Открываем страницу лиги", "LEAGUE_OPENING")
        await league.open()
        await REPOSITORY.log("LEAGUE_OPENED", CONFIG.league_name)
        await STATE.update(
            selected_team=None,
            selected_side=None,
            initial_selected_odds=None,
            other_team=None,
            selection_reason=None,
            bet={
                "step": 0,
                "max_steps": self._config.max_steps,
                "amount": None,
                "market": "Следующий гол",
                "odds": None,
                "score_before": None,
                "next_goal_number": None,
                "status": "WAITING",
            },
            budget=self._budget.snapshot(),
        )

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

        sequence = await REPOSITORY.get_sequence()
        cycle_id = sequence["sequence_id"]
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

        snapshot = await self._wait_for_initial_zero_score(selected_match)
        if snapshot is None:
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
            status="ACTIVE",
            current_match_id=selected_match.get("match_id"),
            selected_team=selection.selected_team,
        )

        won = False
        ambiguous_cycle = False
        start_step = int(sequence["current_step"])
        previous_settlement_score = None
        if sequence["status"] == "SEQUENCE_EXHAUSTED":
            await self._status(DemoStatus.SEQUENCE_EXHAUSTED, "Серия исчерпана; выполните явный сброс", "SEQUENCE_EXHAUSTED")
            return
        for step in range(start_step, self._config.max_steps + 1):
            amount = float(self._config.stakes[step - 1])
            if self._stop_event.is_set():
                return
            if step == 1:
                current_odds = initial_odds
            else:
                odds_result = await self._wait_for_odds(snapshot, selected_match)
                if odds_result is None:
                    return
                snapshot, current_odds = odds_result
                if previous_settlement_score is not None and validate_score_progression(
                    previous_settlement_score, snapshot.score, expected_goals=0
                ) != ScoreProgression.UNCHANGED:
                    await self._skip_match_for_missed_event(selected_match, step, snapshot)
                    return
            browser = MatchBrowser(await self.browser_manager.ensure_page())
            snapshot = await self._read_fresh_score(browser, selected_match, snapshot)
            if step == 1 and not can_create_initial_bet(snapshot.score):
                await REPOSITORY.log(
                    "INITIAL_BET_SKIPPED",
                    f"Счёт изменился до фиксации ставки: {snapshot.score.text()}",
                )
                return
            while (
                current_odds.next_goal_number
                != snapshot.score.team1 + snapshot.score.team2 + 1
            ):
                await REPOSITORY.log(
                    "STALE_MARKET_IGNORED",
                    "Счёт изменился до создания ставки; читаем коэффициент нового гола.",
                )
                odds_result = await self._wait_for_odds(snapshot, selected_match)
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
            await REPOSITORY.save_bet(active_record)
            await STATE.update(
                status=(
                    DemoStatus.WAITING_FOR_MATCH_START.value
                    if waiting_for_match_start
                    else DemoStatus.BET_SIMULATED.value
                ),
                message=f"DEMO BET #{step}: {amount} RUB @ {selected_odd}",
                event="DEMO_BET_CREATED",
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
                    "budget_before": float(self._budget.current_budget),
                },
                budget=self._budget.snapshot(),
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log(
                "DEMO_BET_CREATED",
                f"#{step}: {selection.selected_team}, {amount} RUB @ {selected_odd}, score={score_before.text()}",
            )

            if waiting_for_match_start:
                started_snapshot = await self._wait_for_match_start(selected_match)
                if started_snapshot is None:
                    return
                await REPOSITORY.log("ACTIVE_BET_RESUMED", f"existing bet_id={bet_id}")
                current_state = await STATE.snapshot()
                await STATE.update(
                    event="ACTIVE_BET_RESUMED",
                    bet={
                        **current_state.get("bet", {}),
                        "status": "WAITING_FOR_GOAL",
                    },
                )

            goal = await self._wait_for_goal(browser, selected_match, snapshot)
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
                await self._skip_match_for_missed_event(selected_match, step, new_snapshot)
                return

            scorer_name = (
                new_snapshot.team1 if scorer == Scorer.TEAM_1 else new_snapshot.team2
            )
            result = "WIN" if scorer == selection.selected_side else "LOSE"
            budget_change = self._budget.settle(bet_id, result, amount, selected_odd)
            if budget_change is None:
                await REPOSITORY.log(
                    "DEMO_BUDGET_DUPLICATE_IGNORED", f"bet_id={bet_id}"
                )
                snapshot = new_snapshot
                continue
            record = await REPOSITORY.save_bet(
                {
                    **common_record,
                    "scorer": scorer_name,
                    "result": result,
                    **budget_change,
                }
            )
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
            await REPOSITORY.log(
                "DEMO_BUDGET",
                f"result={result} stake={amount} before={budget_change['budget_before']} "
                f"change={budget_change['budget_change']:+.2f} after={budget_change['budget_after']}",
            )
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
                budget=self._budget.snapshot(),
                sequence=await REPOSITORY.get_sequence(),
                bet={**record, "max_steps": self._config.max_steps},
            )
            snapshot = new_snapshot
            if result == "WIN":
                won = True
                await REPOSITORY.add_cycle(
                    {"cycle_id": cycle_id, "match": match_name, "result": "WIN", "steps": step}
                )
                await REPOSITORY.log("STRATEGY_CYCLE_WON", match_name)
                await REPOSITORY.reset_sequence()
                await STATE.update(stats=await REPOSITORY.stats(), sequence=await REPOSITORY.get_sequence())
                break
            if step < self._config.max_steps:
                next_step = step + 1
                await REPOSITORY.save_sequence(
                    current_step=next_step,
                    status="ACTIVE",
                    cumulative_pnl=str((await REPOSITORY.get_sequence())["cumulative_pnl"]),
                )
                # Read the scoreboard again before exposing/creating the next bet.
                fresh_after_settlement = await self._read_fresh_score(browser, selected_match, new_snapshot)
                if validate_score_progression(new_snapshot.score, fresh_after_settlement.score, expected_goals=0) != ScoreProgression.UNCHANGED:
                    await self._skip_match_for_missed_event(selected_match, next_step, fresh_after_settlement)
                    return
                previous_settlement_score = new_snapshot.score
                await self._status(
                    DemoStatus.NEXT_STEP, f"Переход к шагу {next_step}", "NEXT_STEP"
                )
                await self._publish_pending_bet(
                    selection,
                    match_name,
                    next_step,
                    new_snapshot,
                )

        if not won and not self._stop_event.is_set():
            await REPOSITORY.add_cycle(
                {
                    "cycle_id": cycle_id,
                    "match": match_name,
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

        if not self._stop_event.is_set():
            await self._status(
                DemoStatus.RETURNING_TO_LEAGUE,
                "Возвращаемся в лигу и заново читаем DOM",
                "RETURNING_TO_LEAGUE",
                stats=await REPOSITORY.stats(),
            )
            await REPOSITORY.log("RETURNING_TO_LEAGUE", CONFIG.league_url)

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

    async def _wait_for_odds(
        self,
        snapshot: ScoreboardSnapshot,
        selected_match: dict[str, Any],
    ):
        """Wait for current DOM odds without losing the active match or score."""
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
                f"Ждём DOM-рынок следующего гола №{next_goal_number}",
                "WAITING_FOR_MARKET",
            )
            await STATE.update(
                market_reader={
                    "source": "DOM / Playwright",
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
                    "source": "DOM_PLAYWRIGHT",
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
                    )

                verified = await browser.snapshot()
                if verified.team1 != snapshot.team1 or verified.team2 != snapshot.team2:
                    raise RecoverableDemoError(
                        "SCOREBOARD_TEAMS_CHANGED",
                        "Порядок или названия команд в scoreboard изменились.",
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

                await STATE.update(
                    market_reader={
                        "source": "DOM / Playwright",
                        "status": "READY",
                        "attempt": attempt,
                        "next_goal_number": odds.next_goal_number,
                    }
                )
                await self._status(
                    DemoStatus.ODDS_READY,
                    f"DOM-коэффициенты готовы: {odds.team1} / {odds.team2}",
                    "ODDS_READY",
                )
                return snapshot, odds
            except MarketReadError as error:
                await STATE.update(
                    market_reader={
                        "source": "DOM / Playwright",
                        "status": error.status,
                        "attempt": attempt,
                        "next_goal_number": next_goal_number,
                    }
                )
                if attempt == 1 or attempt % 10 == 0:
                    await REPOSITORY.log(error.status, str(error))
                await self._sleep_or_stop(CONFIG.ocr_retry_delay)
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
