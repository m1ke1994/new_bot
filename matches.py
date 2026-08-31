import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

from auth import authorize, PROFILE_DIR


# ============================================================
# ENV / CONFIG
# ============================================================

load_dotenv()

MATCHES_URL = os.getenv(
    "XBET_MATCHES_URL",
    "https://1xlite-02216.pro/ru/live/fifa/"
    "2860561-fc-25-3x3-conference-league",
).strip()

LEAGUE_NAME = os.getenv(
    "XBET_LEAGUE_NAME",
    "FC 25. 3x3. Лига Конференций",
).strip()

MONITOR_INTERVAL = float(
    os.getenv("MATCH_MONITOR_INTERVAL", "2")
)

BASE_DIR = Path(__file__).resolve().parent

front_file_env = os.getenv(
    "MATCHES_FRONT_FILE",
    "frontend/public/matches.json",
).strip()

FRONT_FILE = Path(front_file_env)
if not FRONT_FILE.is_absolute():
    FRONT_FILE = BASE_DIR / FRONT_FILE


# ============================================================
# SELECTORS
# ============================================================

LEAGUE_TITLE_SELECTOR = ".dashboard-champ-name__caption"
MATCH_SELECTOR = "li.dashboard-champ__game"
TEAM_SELECTOR = ".dashboard-game-team-info__name"
TIME_SELECTOR = ".dashboard-game-info__time"
PERIOD_SELECTOR = ".dashboard-game-info__period"
MARKET_SELECTOR = ".dashboard-markets__market"
MARKET_BUTTON_SELECTOR = "button.ui-market__toggle"
MARKET_VALUE_SELECTOR = ".ui-market__value"
MATCH_LINK_SELECTOR = "a.dashboard-game-block__link"
SCORE_SELECTOR = ".ui-game-scores__item--total .ui-game-scores__num"


# ============================================================
# HELPERS
# ============================================================

def log(message: str):
    print(f"[THIS_MATCH] {message}")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().split())


async def safe_text(locator, default=""):
    try:
        if await locator.count() == 0:
            return default
        text = await locator.first.inner_text()
        return clean_text(text)
    except Exception:
        return default


def time_to_seconds(value: str | None):
    """
    Время на карточке будущего матча:
        01:28 -> 88 сек.
        14:57 -> 897 сек.
        34:56 -> 2096 сек.
    """
    value = clean_text(value)
    if not value:
        return None

    parts = value.split(":")
    if len(parts) != 2:
        return None

    try:
        minutes = int(parts[0])
        seconds = int(parts[1])
    except ValueError:
        return None

    if minutes < 0 or seconds < 0 or seconds >= 60:
        return None

    return minutes * 60 + seconds


# ============================================================
# OPEN / CHECK LEAGUE
# ============================================================

async def open_matches_page(page: Page):
    print()
    print("=" * 70)
    print("ПЕРЕХОД В LIVE-ЛИГУ")
    print("=" * 70)
    print()

    log(f"Открываем: {MATCHES_URL}")

    await page.goto(
        MATCHES_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    log("DOM страницы загружен.")
    await page.wait_for_timeout(3000)
    log(f"Текущий URL: {page.url}")


async def find_league_container(page: Page):
    log("Ищем нужную лигу...")

    titles = page.locator(LEAGUE_TITLE_SELECTOR)
    count = await titles.count()
    log(f"Найдено заголовков: {count}")

    for index in range(count):
        title = titles.nth(index)

        try:
            if not await title.is_visible():
                continue

            text = clean_text(await title.inner_text())

            if text != LEAGUE_NAME:
                continue

            container = title.locator(
                "xpath=ancestor::li[contains(@class,'dashboard-champ-body')]"
            ).first

            await container.wait_for(
                state="visible",
                timeout=10_000,
            )

            print()
            print("=" * 70)
            print("НУЖНАЯ ЛИГА НАЙДЕНА")
            print("=" * 70)
            print(f"Лига: {text}")
            print(f"URL: {page.url}")
            print("=" * 70)
            print()

            return container

        except Exception as error:
            log(f"Ошибка проверки заголовка: {error}")

    return None


async def check_correct_page(page: Page):
    container = await find_league_container(page)

    if container is None:
        raise RuntimeError(f"Лига '{LEAGUE_NAME}' не найдена.")

    log("Подтверждено: мы находимся там, где нужно.")
    return True


# ============================================================
# MATCH DATA
# ============================================================

async def get_score(game):
    try:
        values = game.locator(SCORE_SELECTOR)
        if await values.count() < 2:
            return None

        score1 = clean_text(await values.nth(0).inner_text())
        score2 = clean_text(await values.nth(1).inner_text())
        return f"{score1}:{score2}"
    except Exception:
        return None


async def get_market_value(game, wanted_market: str):
    markets = game.locator(MARKET_SELECTOR)
    count = await markets.count()

    for index in range(count):
        market = markets.nth(index)
        button = market.locator(MARKET_BUTTON_SELECTOR).first

        try:
            label = await button.get_attribute("aria-label")
            if not label:
                label = await button.get_attribute("title")
            if not label:
                continue

            label = clean_text(label)
            if label != wanted_market:
                continue

            value = await safe_text(
                market.locator(MARKET_VALUE_SELECTOR)
            )

            if not value or value == "-":
                return None

            return value
        except Exception:
            continue

    return None


async def get_match_href(game):
    try:
        return await game.locator(
            MATCH_LINK_SELECTOR
        ).first.get_attribute("href")
    except Exception:
        return None


def get_match_id(href: str | None):
    if not href:
        return None

    result = re.search(r"/(\d+)-[^/]+$", href)
    return result.group(1) if result else None


async def parse_match(game, number: int):
    teams = game.locator(TEAM_SELECTOR)
    if await teams.count() < 2:
        return None

    team1 = clean_text(await teams.nth(0).inner_text())
    team2 = clean_text(await teams.nth(1).inner_text())

    time_value = await safe_text(game.locator(TIME_SELECTOR))
    time_seconds = time_to_seconds(time_value)

    period = await safe_text(game.locator(PERIOD_SELECTOR))
    period_lower = period.lower()

    finished = (
        "завершена" in period_lower
        or "завершен" in period_lower
    )

    # По текущему DOM будущие матчи имеют countdown MM:SS,
    # но не имеют текста периода (1-й тайм / 2-й тайм / завершена).
    is_upcoming = (
        not finished
        and not period
        and time_seconds is not None
    )

    odds_team1 = await get_market_value(game, "П1")
    odds_draw = await get_market_value(game, "Ничья")
    odds_team2 = await get_market_value(game, "П2")

    score = await get_score(game)
    href = await get_match_href(game)
    match_id = get_match_id(href)

    match_url = None
    if href:
        if href.startswith("/"):
            match_url = "https://1xlite-02216.pro" + href
        else:
            match_url = href

    return {
        "number": number,
        "match_id": match_id,
        "team1": team1,
        "team2": team2,
        "time": time_value,
        "time_seconds": time_seconds,
        "period": period,
        "is_upcoming": is_upcoming,
        "score": score,
        "odds_team1": odds_team1,
        "odds_draw": odds_draw,
        "odds_team2": odds_team2,
        "finished": finished,
        "href": href,
        "url": match_url,
    }


# ============================================================
# UPCOMING MATCHES
# ============================================================

async def get_upcoming_matches(page: Page):
    league_container = await find_league_container(page)

    if league_container is None:
        raise RuntimeError("Контейнер нужной лиги не найден.")

    games = league_container.locator(MATCH_SELECTOR)
    count = await games.count()

    upcoming = []

    for index in range(count):
        game = games.nth(index)

        try:
            match = await parse_match(game, index + 1)
        except Exception as error:
            log(f"Ошибка чтения матча #{index + 1}: {error}")
            continue

        if not match:
            continue

        if not match["is_upcoming"]:
            continue

        upcoming.append(match)

    # Чем меньше countdown, тем раньше матч начнётся.
    upcoming.sort(
        key=lambda item: (
            item["time_seconds"]
            if item["time_seconds"] is not None
            else float("inf")
        )
    )

    # После сортировки номера на фронте должны быть 1,2,3...
    for number, match in enumerate(upcoming, start=1):
        match["number"] = number

    return upcoming


# ============================================================
# FRONTEND JSON
# ============================================================

def create_front_data(matches):
    return {
        "ok": True,
        "league": LEAGUE_NAME,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "matches_count": len(matches),
        "next_match": matches[0] if matches else None,
        "matches": matches,
    }


async def write_front(matches):
    FRONT_FILE.parent.mkdir(parents=True, exist_ok=True)

    data = create_front_data(matches)
    temp_file = Path(str(FRONT_FILE) + ".tmp")

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temp_file, FRONT_FILE)
    return data


def create_snapshot(matches):
    return json.dumps(
        matches,
        ensure_ascii=False,
        sort_keys=True,
    )


def print_upcoming_matches(matches):
    print()
    print("=" * 70)
    print(f"ГРЯДУЩИХ МАТЧЕЙ: {len(matches)}")
    print("=" * 70)
    print()

    for match in matches:
        print(f"[МАТЧ {match['number']}] {match['team1']} — {match['team2']}")
        print(f"    До начала: {match['time']}")
        print(f"    КФ {match['team1']}: {match['odds_team1']}")
        print(f"    Ничья: {match['odds_draw']}")
        print(f"    КФ {match['team2']}: {match['odds_team2']}")
        print(f"    ID: {match['match_id']}")
        print(f"    URL: {match['url']}")
        print()

    print(f"[FRONT] JSON: {FRONT_FILE}")
    print("=" * 70)
    print()


# ============================================================
# OPEN NEAREST MATCH
# ============================================================

async def open_nearest_match(page: Page, matches):
    if not matches:
        return None

    nearest = matches[0]

    print()
    print("=" * 70)
    print("БЛИЖАЙШИЙ МАТЧ")
    print("=" * 70)
    print(f"Матч: {nearest['team1']} — {nearest['team2']}")
    print(f"До начала: {nearest['time']}")
    print(f"П1: {nearest['odds_team1']}")
    print(f"X: {nearest['odds_draw']}")
    print(f"П2: {nearest['odds_team2']}")
    print(f"href: {nearest['href']}")
    print("=" * 70)
    print()

    href = nearest.get("href")
    if not href:
        raise RuntimeError("У ближайшего матча не найдена ссылка href.")

    # Ищем именно ссылку выбранного матча, а не первую ссылку на странице.
    href_css_value = json.dumps(href)
    link = page.locator(
        f"a.dashboard-game-block__link[href={href_css_value}]"
    ).first

    await link.wait_for(
        state="visible",
        timeout=10_000,
    )

    log("Ссылка ближайшего матча найдена в DOM.")
    log("Кликаем по ближайшему матчу...")

    await link.click()

    # Для SPA достаточно дождаться смены URL / отрисовки.
    try:
        if nearest.get("match_id"):
            await page.wait_for_url(
                re.compile(
                    rf".*/{re.escape(nearest['match_id'])}-[^/?#]+.*"
                ),
                timeout=15_000,
            )
        else:
            await page.wait_for_timeout(2500)
    except Exception:
        await page.wait_for_timeout(2500)

    print()
    print("=" * 70)
    print("ПЕРЕШЛИ В БЛИЖАЙШИЙ МАТЧ")
    print("=" * 70)
    print(f"{nearest['team1']} — {nearest['team2']}")
    print(f"URL браузера: {page.url}")
    print("=" * 70)
    print()

    return nearest


# ============================================================
# MONITOR UPCOMING UNTIL WE CAN SELECT ONE
# ============================================================

async def monitor_upcoming_matches(page: Page):
    print()
    print("=" * 70)
    print("МОНИТОРИНГ ГРЯДУЩИХ МАТЧЕЙ ЗАПУЩЕН")
    print("=" * 70)
    print(f"Лига: {LEAGUE_NAME}")
    print(f"Интервал: {MONITOR_INTERVAL} сек.")
    print(f"FRONT: {FRONT_FILE}")
    print("=" * 70)
    print()

    previous_snapshot = None

    while True:
        if page.is_closed():
            raise RuntimeError("Страница браузера закрыта.")

        try:
            upcoming_matches = await get_upcoming_matches(page)

            # Сначала ВСЕ будущие матчи записываем фронту.
            await write_front(upcoming_matches)

            current_snapshot = create_snapshot(upcoming_matches)

            if current_snapshot != previous_snapshot:
                print_upcoming_matches(upcoming_matches)
                previous_snapshot = current_snapshot
            else:
                log(
                    "Грядущие матчи проверены. "
                    f"Изменений нет. Матчей: {len(upcoming_matches)}"
                )

            # После записи на фронт выбираем матч с минимальным countdown.
            if upcoming_matches:
                return await open_nearest_match(
                    page,
                    upcoming_matches,
                )

            log("Грядущих матчей пока нет. Ждём...")

        except asyncio.CancelledError:
            raise
        except Exception as error:
            log(f"Ошибка мониторинга: {error}")

        await asyncio.sleep(MONITOR_INTERVAL)


# ============================================================
# THIS MATCH
# ============================================================

async def this_match(page: Page):
    # 1. Открываем страницу лиги.
    await open_matches_page(page)

    # 2. Проверяем по заголовку, что мы точно в нужной лиге.
    await check_correct_page(page)

    # 3. Получаем будущие матчи, передаём их фронту,
    #    сортируем по countdown и кликаем ближайший.
    selected_match = await monitor_upcoming_matches(page)

    return selected_match


# ============================================================
# MAIN
# ============================================================

async def main():
    print()
    print("=" * 70)
    print("THIS_MATCH")
    print("=" * 70)
    print()

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport=None,
        )

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            print()
            print("=" * 70)
            print("ШАГ 1 — АВТОРИЗАЦИЯ")
            print("=" * 70)
            print()

            auth_result = await authorize(page)
            log(f"AUTH RESULT: {auth_result}")

            if not auth_result.get("ok"):
                raise RuntimeError(
                    "Авторизация не выполнена. Работа остановлена."
                )

            print()
            print("=" * 70)
            print("АВТОРИЗАЦИЯ ПОДТВЕРЖДЕНА")
            print("=" * 70)
            print()

            print()
            print("=" * 70)
            print("ШАГ 2 — ПОИСК БЛИЖАЙШЕГО МАТЧА")
            print("=" * 70)
            print()

            selected_match = await this_match(page)

            print()
            log(f"SELECTED MATCH: {selected_match}")

            # На следующем этапе здесь продолжим работу уже
            # внутри открытого матча.
            print()
            print("Браузер оставлен открытым для следующего этапа.")
            print("Нажми ENTER для завершения.")
            await asyncio.to_thread(input)

        except KeyboardInterrupt:
            print()
            print("[SYSTEM] Остановка пользователем.")

        except Exception as error:
            print()
            print("=" * 70)
            print("ОШИБКА THIS_MATCH")
            print("=" * 70)
            print(error)
            print("=" * 70)
            print()
            print("Браузер оставлен открытым для диагностики.")
            await asyncio.to_thread(input)

        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
