from __future__ import annotations

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


def install_fast_next_goal_runtime(engine_module: Any) -> None:
    """Patch only DEMO NEXT_GOAL timing; LIVE keeps the original safety checks."""
    engine_class = engine_module.DemoEngine
    if getattr(engine_class, "_fast_next_goal_runtime_installed", False):
        return

    original_wait_for_odds = engine_class._wait_for_odds
    original_read_fresh_score = engine_class._read_fresh_score
    original_sleep_or_stop = engine_class._sleep_or_stop

    async def fast_wait_for_odds(
        self,
        snapshot,
        selected_match: dict[str, Any],
        *,
        blocked_attempt_score=None,
        blocked_selected_side=None,
        demo_blocked_window=None,
    ):
        # LIVE placement keeps every existing confirmation/race guard. The fast
        # path is intentionally limited to virtual DEMO bets.
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
                # One scoreboard read defines which next-goal row is required.
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
                    f"Следующий гол №{next_goal_number}; один кадр, без повторной проверки коэффициента",
                )

            await self._status(
                engine_module.DemoStatus.WAITING_FOR_MARKET,
                f"Ждём рынок следующего гола №{next_goal_number}",
                "WAITING_FOR_MARKET",
            )
            await engine_module.STATE.update(
                market_reader={
                    "source": "CANVAS FAST FRAME",
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
                    "source": "CANVAS_FAST_FRAME",
                    "backend": None,
                    "confidence": None,
                    "status": "WAITING_FOR_MARKET",
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

                # IMPORTANT: odds capture is the virtual-bet ordering boundary.
                # Do not re-read scoreboard or coefficient after this point.
                _cache_fast_snapshot(self, snapshot)

                await engine_module.STATE.update(
                    market_reader={
                        "source": odds.source,
                        "status": "READY",
                        "attempt": attempt,
                        "next_goal_number": odds.next_goal_number,
                    }
                )
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
                            "[NEXT_GOAL][DEMO][BLOCKED] fresh market ready; "
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
                    await engine_module.REPOSITORY.log(
                        "DEMO_BLOCKED_WINDOW_STARTED",
                        (
                            "[NEXT_GOAL][DEMO][BLOCKED] outcome unavailable; "
                            f"match_id={demo_blocked_window.match_id}; "
                            f"selected_team={demo_blocked_window.selected_team}; "
                            f"step={demo_blocked_window.step}; "
                            f"stake={demo_blocked_window.stake:g}; "
                            f"blocked_score_before={demo_blocked_window.blocked_score_before.text()}; "
                            f"market_status={error.status}"
                        ),
                    )

                await engine_module.STATE.update(
                    market_reader={
                        "source": "CANVAS FAST FRAME",
                        "status": error.status,
                        "attempt": attempt,
                        "next_goal_number": next_goal_number,
                    }
                )
                if attempt == 1 or attempt % 10 == 0:
                    await engine_module.REPOSITORY.log(error.status, str(error))
                await original_sleep_or_stop(self, engine_module.CONFIG.ocr_retry_delay)

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

        return await original_read_fresh_score(
            self,
            browser,
            selected_match,
            previous,
        )

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
