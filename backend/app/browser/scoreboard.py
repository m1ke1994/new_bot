import re

from playwright.async_api import Locator, Page

from backend.app.demo.models import Score, ScoreboardSnapshot


SCOREBOARD_ROOT_SELECTOR = ".scoreboard-layout-head__footer"
TEAM_SELECTOR = ".scoreboard-intro__team"
TEAM_1_SCORE_SELECTOR = ".scoreboard-scores__item--team-1"
TEAM_2_SCORE_SELECTOR = ".scoreboard-scores__item--team-2"
TIMER_SELECTOR = ".scoreboard-timer span"

_last_debug_scoreboard: tuple[str, str, int, int] | None = None


class ScoreReadError(RuntimeError):
    pass


async def _scoreboard_root(page: Page) -> Locator:
    root = page.locator(SCOREBOARD_ROOT_SELECTOR).first
    await root.wait_for(state="attached", timeout=15_000)
    return root


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


async def read_scoreboard(page: Page) -> ScoreboardSnapshot:
    global _last_debug_scoreboard

    try:
        root = await _scoreboard_root(page)
        names = root.locator(TEAM_SELECTOR)
        score1_locator = root.locator(TEAM_1_SCORE_SELECTOR).first
        score2_locator = root.locator(TEAM_2_SCORE_SELECTOR).first

        if (
            await names.count() < 2
            or await score1_locator.count() == 0
            or await score2_locator.count() == 0
        ):
            raise ScoreReadError("В scoreboard нет двух команд или двух значений счёта.")

        team1 = (await names.nth(0).inner_text()).strip()
        team2 = (await names.nth(1).inner_text()).strip()
        score1 = _parse_score(await score1_locator.inner_text())
        score2 = _parse_score(await score2_locator.inner_text())
        score = Score(score1, score2)

        timer_status = root.locator(TIMER_SELECTOR).first
        status_text = (
            (await timer_status.inner_text()).strip()
            if await timer_status.count()
            else ""
        )
        timer, period = parse_timer_and_period(status_text)

        if not team1 or not team2:
            raise ScoreReadError("Названия команд в scoreboard пусты.")

        debug_key = (team1, team2, score1, score2)
        if debug_key != _last_debug_scoreboard:
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
