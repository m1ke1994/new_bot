import asyncio
import os
import re
from typing import Any

from playwright.async_api import Locator, Page

from backend.app.demo.models import Score, ScoreboardSnapshot
from xbet_config import SELECTORS


SCOREBOARD_ROOT_SELECTOR = SELECTORS.scoreboard_root
TEAM_SELECTOR = SELECTORS.scoreboard_team
TEAM_1_SCORE_SELECTOR = SELECTORS.scoreboard_team_1_score
TEAM_2_SCORE_SELECTOR = SELECTORS.scoreboard_team_2_score
TIMER_SELECTOR = SELECTORS.scoreboard_timer


def _selector_candidates(*values: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        value = (value or "").strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


# The bookmaker currently serves more than one scoreboard layout. The configured
# selector remains first, while confirmed previous/current layouts are retained
# as fallbacks so a cosmetic wrapper change cannot block market reading.
SCOREBOARD_ROOT_SELECTORS = _selector_candidates(
    SCOREBOARD_ROOT_SELECTOR,
    ".scoreboard-layout-head__footer",
    ".scoreboard-compact-view-tab-panel__body",
)
TEAM_SELECTORS = _selector_candidates(
    os.getenv("SELECTOR_SCOREBOARD_TEAM_NAME", ""),
    ".scoreboard-team-name__text",
    TEAM_SELECTOR,
    ".scoreboard-intro__team",
)
TEAM_1_SCORE_SELECTORS = _selector_candidates(
    TEAM_1_SCORE_SELECTOR,
    ".scoreboard-scores__item--team-1",
    ".scoreboard-scores__score:not(.scoreboard-scores__score--team-2)",
)
TEAM_2_SCORE_SELECTORS = _selector_candidates(
    TEAM_2_SCORE_SELECTOR,
    ".scoreboard-scores__item--team-2",
    ".scoreboard-scores__score--team-2",
)
TIMER_SELECTORS = _selector_candidates(
    TIMER_SELECTOR,
    SELECTORS.scoreboard_timer_status,
    ".scoreboard-timer span",
    ".scoreboard-timer .ui-caption",
    ".ui-game-timer__label",
)

_last_debug_scoreboard: tuple[str, str, int, int] | None = None
_body_fallback_reported = False


class ScoreReadError(RuntimeError):
    pass


async def _find_locator(
    scope: Any,
    selectors: tuple[str, ...],
    *,
    minimum_count: int = 1,
    timeout_ms: int = 15_000,
) -> tuple[Locator, str]:
    if not selectors:
        raise ScoreReadError("Не настроены селекторы scoreboard.")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    last_counts: dict[str, int] = {}

    while True:
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                count = await locator.count()
            except Exception:
                count = -1
            last_counts[selector] = count
            if count >= minimum_count:
                return locator, selector

        if loop.time() >= deadline:
            details = ", ".join(
                f"{selector}={count}" for selector, count in last_counts.items()
            )
            raise ScoreReadError(
                f"Scoreboard elements not found within {timeout_ms}ms: {details}"
            )
        await asyncio.sleep(0.10)


async def _scoreboard_root(page: Page) -> Locator:
    global _body_fallback_reported

    try:
        root, _ = await _find_locator(
            page,
            SCOREBOARD_ROOT_SELECTORS,
            timeout_ms=2_500,
        )
        return root.first
    except ScoreReadError:
        # A/B layouts sometimes remove the known wrapper but retain the team and
        # score elements. Scope to body and validate all required fields below.
        body = page.locator("body").first
        await body.wait_for(state="attached", timeout=12_500)
        if not _body_fallback_reported:
            print("[SCOREBOARD] known root missing; using body fallback")
            _body_fallback_reported = True
        return body


def _parse_score(text: str) -> int:
    value = text.strip()
    if not re.fullmatch(r"\d+", value):
        raise ScoreReadError(f"Некорректное числовое значение счёта: {text!r}")
    return int(value)


def parse_timer_and_period(text: str) -> tuple[str, str]:
    value = " ".join(text.strip().split())
    timer_match = re.search(r"\b\d{1,3}:\d{2}\b", value)
    if timer_match is None:
        return "", value.strip(" ,")

    timer = timer_match.group(0)
    period = f"{value[:timer_match.start()]} {value[timer_match.end():]}"
    return timer, " ".join(period.strip(" ,").split())


async def _optional_timer_text(root: Locator) -> str:
    for selector in TIMER_SELECTORS:
        try:
            locator = root.locator(selector).first
            if await locator.count():
                return (await locator.inner_text()).strip()
        except Exception:
            continue
    return ""


async def read_scoreboard(page: Page) -> ScoreboardSnapshot:
    global _last_debug_scoreboard

    try:
        root = await _scoreboard_root(page)
        names, team_selector = await _find_locator(
            root,
            TEAM_SELECTORS,
            minimum_count=2,
            timeout_ms=12_500,
        )
        score1_candidates, score1_selector = await _find_locator(
            root,
            TEAM_1_SCORE_SELECTORS,
            timeout_ms=12_500,
        )
        score2_candidates, score2_selector = await _find_locator(
            root,
            TEAM_2_SCORE_SELECTORS,
            timeout_ms=12_500,
        )

        team1 = (await names.nth(0).inner_text()).strip()
        team2 = (await names.nth(1).inner_text()).strip()
        score1 = _parse_score(await score1_candidates.first.inner_text())
        score2 = _parse_score(await score2_candidates.first.inner_text())
        score = Score(score1, score2)

        status_text = await _optional_timer_text(root)
        timer, period = parse_timer_and_period(status_text)

        if not team1 or not team2:
            raise ScoreReadError("Названия команд в scoreboard пусты.")

        debug_key = (team1, team2, score1, score2)
        if debug_key != _last_debug_scoreboard:
            print(
                "[SCOREBOARD_SELECTORS] "
                f"teams={team_selector}; score1={score1_selector}; "
                f"score2={score2_selector}"
            )
            print(f"[SCOREBOARD] timer={timer or '<пусто>'}")
            print(f"[SCOREBOARD] team1={team1}")
            print(f"[SCOREBOARD] team2={team2}")
            print(f"[SCOREBOARD] score={score1}:{score2}")
            _last_debug_scoreboard = debug_key

        return ScoreboardSnapshot(
            team1=team1,
            team2=team2,
            score=score,
            timer=timer,
            period=period,
        )
    except ScoreReadError:
        raise
    except Exception as error:
        raise ScoreReadError(str(error)) from error
