from __future__ import annotations

import time
from typing import Any


FAST_PREBET_CACHE_USES = 2
NEW_MATCH_DELAY_SECONDS = 10.0


def _same_snapshot(left: Any, right: Any) -> bool:
    return bool(
        left is not None
        and right is not None
        and getattr(left, "team1", None) == getattr(right, "team1", None)
        and getattr(left, "team2", None) == getattr(right, "team2", None)
        and getattr(left, "score", None) == getattr(right, "score", None)
    )


def _score_changed(left: Any, right: Any) -> bool:
    return bool(
        left is not None
        and right is not None
        and getattr(left, "team1", None) == getattr(right, "team1", None)
        and getattr(left, "team2", None) == getattr(right, "team2", None)
        and getattr(left, "score", None) != getattr(right, "score", None)
    )


def _cache_fast_snapshot(engine: Any, snapshot: Any) -> None:
    engine._fast_demo_odds_snapshot = snapshot
    engine._fast_demo_odds_snapshot_uses = FAST_PREBET_CACHE_USES


def _consume_fast_snapshot(engine: Any, previous: Any) -> Any | None:
    cached = getattr(engine, "_fast_demo_odds_snapshot", None)
    uses = int(getattr(engine, "_fast_demo_odds_snapshot_uses", 0) or 0)
    if uses <= 0 or not _same_snapshot(cached, previous):
        engine._fast_demo_odds_snapshot = None
        engine._fast_demo_odds_snapshot_uses = 0
        return None

    engine._fast_demo_odds_snapshot_uses = uses - 1
    if uses - 1 <= 0:
        engine._fast_demo_odds_snapshot = None
    return cached


def _side_number(side: Any) -> int | None:
    value = getattr(side, "value", side)
    value = str(value or "").upper()
    if value == "TEAM_1":
        return 1
    if value == "TEAM_2":
        return 2
    return None


def _resolve_selected_side(engine_module: Any, engine: Any, odds: Any, demo_blocked_window: Any) -> Any | None:
    if demo_blocked_window is not None:
        return demo_blocked_window.selected_side
    current_series = getattr(engine, "_current_series", None)
    if current_series is not None:
        return current_series.selected_side
    if float(odds.team1) > float(odds.team2):
        return engine_module.Scorer.TEAM_1
    if float(odds.team2) > float(odds.team1):
        return engine_module.Scorer.TEAM_2
    return None


def _selected_and_opponent_odds(side: Any, odds: Any) -> tuple[float | None, float | None]:
    side_number = _side_number(side)
    if side_number == 1:
        return float(odds.team1), float(odds.team2)
    if side_number == 2:
        return float(odds.team2), float(odds.team1)
    return None, None


def _selected_side_is_locked(side: Any, odds: Any) -> bool:
    side_number = _side_number(side)
    locked = {int(value) for value in (getattr(odds, "locked_sides", ()) or ())}
    return side_number is not None and side_number in locked


def install_fast_next_goal_runtime(engine_module: Any) -> None:
    """Patch only DEMO NEXT_GOAL timing; LIVE keeps the original safety checks."""
    engine_class = engine_module.DemoEngine
    if getattr(engine_class, "_fast_next_goal_runtime_installed", False):
        return

    original_wait_for_odds = engine_class._wait_for_odds
    original_read_fresh_score = engine_class._read_fresh_score
    original_sleep_or_stop = engine_class._sleep_or_stop

    # Keep the existing frontend unchanged: it already renders repository logs.
    # Enrich DEMO_BET_CREATED with the measured time from the detected score
    # change to the actual virtual-bet creation, so the value appears directly
    # in the live log stream.
    repository = engine_module.REPOSITORY
    if not getattr(repository, "_reaction_log_wrapped", False):
        original_repository_log = repository.log

        async def reaction_aware_log(event: str, message: str):
            if event in {
                "DEMO_START_REQUEST",
                "DEMO_STOP",
                "DATABASE_CLEARED",
                "SEQUENCE_RESET",
            }:
                repository._reaction_started_perf = None

            if event == "DEMO_BET_CREATED":
                started = getattr(repository, "_reaction_started_perf", None)
                if started is not None:
                    elapsed = max(0.0, time.perf_counter() - float(started))
                    repository._last_reaction_seconds = elapsed
                    repository._reaction_started_perf = None
                    message = (
                        f"{message} | ⏱ реакция после гола: {elapsed:.3f} с"
                    )

            return await original_repository_log(event, message)

        repository.log = reaction_aware_log
        repository._reaction_log_wrapped = True
        repository._reaction_started_perf = None
        repository._last_reaction_seconds = None

    async def fast_wait_for_odds(
        self,
        snapshot,
        selected_match: dict[str, Any],
        *,
        blocked_attempt_score=None,
        blocked_selected_side=None,
        demo_blocked_window=None,
    ):
        if self._mode != "DEMO":
            return await original_wait_for_odds(
                self,
                snapshot,
                selected_match,
                blocked_attempt_score=blocked_attempt_score,
                blocked_selected_side=blocked_selected_side,
                demo_blocked_window=demo_blocked_window,
            )

        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            page = await self.browser_manager.ensure_page()
            browser = engine_module.MatchBrowser(page)

            try:
                # This read happens BEFORE coefficient capture. Once a valid
                # coefficient is captured, no additional scoreboard/odds read is
                # allowed before the virtual bet is created.
                fresh = await browser.snapshot()
                if fresh.team1 != snapshot.team1 or fresh.team2 != snapshot.team2:
                    raise engine_module.RecoverableDemoError(
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
                await self._publish_snapshot(snapshot, selected_match, state="LIVE")
            except engine_module.ScoreReadError as error:
                if attempt == 1 or attempt % 20 == 0:
                    await engine_module.REPOSITORY.log(
                        "SCORE_TEMPORARILY_UNAVAILABLE", str(error)
                    )
                await original_sleep_or_stop(self, engine_module.CONFIG.score_poll_interval)
                continue

            next_goal_number = snapshot.score.team1 + snapshot.score.team2 + 1
            if attempt == 1:
                await engine_module.REPOSITORY.log(
                    "FAST_MARKET_READING",
                    (
                        f"Следующий гол №{next_goal_number}; быстрый Canvas/DOM reader, "
                        "после фиксации коэффициента pre-bet проверки отключены"
                    ),
                )

            await self._status(
                engine_module.DemoStatus.WAITING_FOR_MARKET,
                f"Ждём рынок следующего гола №{next_goal_number}",
                "WAITING_FOR_MARKET",
            )
            # Do not erase previously captured coefficients while a new market
            # frame is being read. This was the reason the frontend looked empty
            # during MARKET_LOCKED/ODDS_NOT_FOUND retries.
            await engine_module.STATE.update(
                market_reader={
                    "source": "HYBRID FAST DEMO",
                    "status": "READING",
                    "attempt": attempt,
                    "next_goal_number": next_goal_number,
                },
            )

            try:
                async with self._market_lock:
                    odds = await engine_module.read_next_goal_odds(
                        page,
                        snapshot.team1,
                        snapshot.team2,
                        snapshot.score.team1,
                        snapshot.score.team2,
                        engine_module.REPOSITORY.log,
                        read_only=True,
                    )

                selected_side = _resolve_selected_side(
                    engine_module,
                    self,
                    odds,
                    demo_blocked_window,
                )
                selected_odd, opponent_odd = _selected_and_opponent_odds(
                    selected_side,
                    odds,
                )
                locked_sides = tuple(
                    int(value) for value in (getattr(odds, "locked_sides", ()) or ())
                )
                selected_locked = bool(
                    self._config.blocked_events_switch_enabled
                    and _selected_side_is_locked(selected_side, odds)
                )

                reaction_started = getattr(
                    engine_module.REPOSITORY,
                    "_reaction_started_perf",
                    None,
                )
                if reaction_started is not None:
                    marker = float(reaction_started)
                    if getattr(self, "_reaction_odds_logged_for", None) != marker:
                        self._reaction_odds_logged_for = marker
                        elapsed_to_odds = max(0.0, time.perf_counter() - marker)
                        await engine_module.REPOSITORY.log(
                            "REACTION_ODDS_READY",
                            (
                                f"Новый КФ прочитан через {elapsed_to_odds:.3f} с "
                                f"после изменения счёта; odds={odds.team1}/{odds.team2}; "
                                f"locked={str(selected_locked).lower()}"
                            ),
                        )

                # Publish coefficients immediately, before any blocked-event
                # decision. A bookmaker lock must never hide recognized odds on
                # the frontend.
                await engine_module.STATE.update(
                    market_reader={
                        "source": odds.source,
                        "status": "MARKET_LOCKED" if selected_locked else "READY",
                        "attempt": attempt,
                        "next_goal_number": odds.next_goal_number,
                    },
                    odds={
                        "selected": selected_odd,
                        "opponent": opponent_odd,
                        "team1": float(odds.team1),
                        "team2": float(odds.team2),
                        "market": odds.market,
                        "source": odds.source,
                        "backend": odds.ocr_backend,
                        "confidence": odds.confidence,
                        "status": "MARKET_LOCKED" if selected_locked else "READY",
                        "locked_sides": list(locked_sides),
                    },
                    market_available=True,
                    market_locked=selected_locked,
                    odds_available=True,
                    odds_value=selected_odd,
                    market_odds=selected_odd,
                )

                if selected_locked:
                    if (
                        demo_blocked_window is not None
                        and demo_blocked_window.market_was_ready
                        and not demo_blocked_window.blocked_window_active
                    ):
                        demo_blocked_window.blocked_window_active = True
                        demo_blocked_window.blocked_score_before = snapshot.score
                        await engine_module.REPOSITORY.log(
                            "DEMO_BLOCKED_WINDOW_STARTED",
                            (
                                "[NEXT_GOAL][DEMO][BLOCKED] selected outcome visually locked; "
                                f"match_id={demo_blocked_window.match_id}; "
                                f"selected_team={demo_blocked_window.selected_team}; "
                                f"step={demo_blocked_window.step}; "
                                f"stake={demo_blocked_window.stake:g}; "
                                f"score={snapshot.score.text()}; "
                                f"locked_sides={list(locked_sides)}"
                            ),
                        )

                    if attempt == 1 or attempt % 10 == 0:
                        await engine_module.REPOSITORY.log(
                            "MARKET_LOCKED",
                            (
                                f"Выбранный исход заблокирован; коэффициенты сохранены "
                                f"на фронте: {odds.team1} / {odds.team2}; "
                                f"locked_sides={list(locked_sides)}"
                            ),
                        )
                    await self._status(
                        engine_module.DemoStatus.MARKET_LOCKED,
                        "Выбранный исход заблокирован; ждём открытие рынка",
                        "MARKET_LOCKED",
                    )
                    await original_sleep_or_stop(
                        self,
                        engine_module.CONFIG.ocr_retry_delay,
                    )
                    continue

                if locked_sides and self._config.blocked_events_switch_enabled:
                    await engine_module.REPOSITORY.log(
                        "NON_SELECTED_LOCK_IGNORED",
                        (
                            f"Заблокированы стороны {list(locked_sides)}, но выбранная "
                            "сторона доступна; виртуальная ставка продолжается"
                        ),
                    )

                # IMPORTANT: coefficient capture is the virtual-bet ordering
                # boundary. From here to simulated placement we reuse the same
                # scoreboard snapshot and do not re-read the coefficient.
                _cache_fast_snapshot(self, snapshot)

                await self._status(
                    engine_module.DemoStatus.ODDS_READY,
                    f"Коэффициенты зафиксированы: {odds.team1} / {odds.team2}",
                    "ODDS_READY",
                )

                if (
                    demo_blocked_window is not None
                    and demo_blocked_window.blocked_window_active
                ):
                    demo_blocked_window.blocked_window_active = False
                    await engine_module.REPOSITORY.log(
                        "DEMO_BLOCKED_MARKET_RECOVERED",
                        (
                            "[NEXT_GOAL][DEMO][BLOCKED] selected outcome available; "
                            f"score={snapshot.score.text()} step={demo_blocked_window.step} "
                            f"stake={demo_blocked_window.stake:g}"
                        ),
                    )

                await engine_module.REPOSITORY.log(
                    "DEMO_ODDS_CAPTURE_IS_BET_BOUNDARY",
                    (
                        f"score={snapshot.score.text()} next_goal={odds.next_goal_number} "
                        f"odds={odds.team1}/{odds.team2}"
                    ),
                )
                return snapshot, odds

            except engine_module.MarketReadError as error:
                if (
                    demo_blocked_window is not None
                    and demo_blocked_window.market_was_ready
                    and error.status in engine_module.DEMO_BLOCKED_MARKET_STATUSES
                    and not demo_blocked_window.blocked_window_active
                ):
                    demo_blocked_window.blocked_window_active = True
                    demo_blocked_window.blocked_score_before = snapshot.score
                    await engine_module.REPOSITORY.log(
                        "DEMO_BLOCKED_WINDOW_STARTED",
                        (
                            "[NEXT_GOAL][DEMO][BLOCKED] market unavailable; "
                            f"match_id={demo_blocked_window.match_id}; "
                            f"selected_team={demo_blocked_window.selected_team}; "
                            f"step={demo_blocked_window.step}; "
                            f"stake={demo_blocked_window.stake:g}; "
                            f"blocked_score_before={snapshot.score.text()}; "
                            f"market_status={error.status}"
                        ),
                    )

                await engine_module.STATE.update(
                    market_reader={
                        "source": "HYBRID FAST DEMO",
                        "status": error.status,
                        "attempt": attempt,
                        "next_goal_number": next_goal_number,
                    },
                    market_locked=error.status == "MARKET_LOCKED",
                )
                if attempt == 1 or attempt % 10 == 0:
                    await engine_module.REPOSITORY.log(error.status, str(error))
                await original_sleep_or_stop(
                    self,
                    engine_module.CONFIG.ocr_retry_delay,
                )

        return None

    async def fast_read_fresh_score(
        self,
        browser,
        selected_match: dict[str, Any],
        previous,
    ):
        if self._mode == "DEMO":
            cached = _consume_fast_snapshot(self, previous)
            if cached is not None:
                await self._publish_snapshot(
                    cached,
                    selected_match,
                    state="LIVE" if cached.period else "UPCOMING",
                )
                await engine_module.REPOSITORY.log(
                    "DEMO_PREBET_RECHECK_SKIPPED",
                    (
                        "Коэффициент уже зафиксирован; повторная проверка scoreboard "
                        f"перед виртуальной ставкой пропущена ({cached.score.text()})"
                    ),
                )
                return cached

        current = await original_read_fresh_score(
            self,
            browser,
            selected_match,
            previous,
        )

        if (
            self._mode == "DEMO"
            and self._config.strategy_type == engine_module.StrategyType.NEXT_GOAL
            and _score_changed(previous, current)
        ):
            started = time.perf_counter()
            engine_module.REPOSITORY._reaction_started_perf = started
            self._reaction_odds_logged_for = None
            await engine_module.REPOSITORY.log(
                "REACTION_TIMER_STARTED",
                (
                    f"Изменение счёта обнаружено: {previous.score.text()} → "
                    f"{current.score.text()}; замер до следующей виртуальной ставки запущен"
                ),
            )

        return current

    async def fast_sleep_or_stop(self, seconds: float) -> None:
        if (
            self._mode == "DEMO"
            and self._config.strategy_type == engine_module.StrategyType.NEXT_GOAL
            and abs(float(seconds) - NEW_MATCH_DELAY_SECONDS) < 0.001
        ):
            await engine_module.REPOSITORY.log(
                "DEMO_NEW_MATCH_DELAY_SKIPPED",
                "10-секундная задержка перед первой виртуальной ставкой отключена",
            )
            return
        await original_sleep_or_stop(self, seconds)

    engine_class._wait_for_odds = fast_wait_for_odds
    engine_class._read_fresh_score = fast_read_fresh_score
    engine_class._sleep_or_stop = fast_sleep_or_stop
    engine_class._fast_next_goal_runtime_installed = True
