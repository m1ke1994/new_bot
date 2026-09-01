import re

from playwright.async_api import Locator, Page

from backend.app.demo.models import Score, ScoreboardSnapshot


SCORE_CONTAINER_SELECTOR = ".scoreboard-scores"
SCORE_SELECTOR = ".scoreboard-scores__score"
TEAM_SELECTOR = ".scoreboard-team-name__text"
TIMER_SELECTOR = ".ui-game-timer__label"
TIMER_VALUE_SELECTOR = ".ui-game-timer__time .ui-game-timer__label"


class ScoreReadError(RuntimeError):
    pass


async def _scoreboard_root(page: Page) -> Locator:
    score_container = page.locator(SCORE_CONTAINER_SELECTOR).first
    await score_container.wait_for(state="attached", timeout=15_000)

    # Ближайший общий предок, содержащий этот счёт и обе команды.
    root = score_container.locator(
        "xpath=ancestor::*[count(.//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' scoreboard-team-name__text ')]) >= 2][1]"
    )
    if await root.count() == 0:
        raise ScoreReadError("Общий контейнер scoreboard для команд и счёта не найден.")
    return root.first


def _parse_score(text: str) -> int:
    value = text.strip()
    if not re.fullmatch(r"\d+", value):
        raise ScoreReadError(f"Некорректное числовое значение счёта: {text!r}")
    return int(value)


async def read_scoreboard(page: Page) -> ScoreboardSnapshot:
    try:
        root = await _scoreboard_root(page)
        names = root.locator(TEAM_SELECTOR)
        score_container = root.locator(SCORE_CONTAINER_SELECTOR).first
        scores = score_container.locator(SCORE_SELECTOR)

        if await names.count() < 2 or await scores.count() < 2:
            raise ScoreReadError("В scoreboard нет двух команд или двух значений счёта.")

        team1 = (await names.nth(0).inner_text()).strip()
        team2 = (await names.nth(1).inner_text()).strip()
        score = Score(
            _parse_score(await scores.nth(0).inner_text()),
            _parse_score(await scores.nth(1).inner_text()),
        )

        summary = root.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' live-scoreboard-summary ')][1]"
        )
        timer_root = summary.first if await summary.count() else root

        timer_value = timer_root.locator(TIMER_VALUE_SELECTOR).first
        timer = ""
        if await timer_value.count():
            timer = (await timer_value.inner_text()).strip()

        labels = timer_root.locator(TIMER_SELECTOR)
        label_texts = [text.strip() for text in await labels.all_inner_texts()]
        if not timer:
            timer = next(
                (text for text in label_texts if re.fullmatch(r"\d{1,2}:\d{2}", text)),
                "",
            )
        period = next((text for text in label_texts if text and text != timer), "")

        if not team1 or not team2:
            raise ScoreReadError("Названия команд в scoreboard пусты.")

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
