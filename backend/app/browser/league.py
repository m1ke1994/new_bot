import json
import re
from typing import Any

from playwright.async_api import Page

from matches import (
    MATCH_SELECTOR,
    find_league_container,
    open_matches_page,
    parse_match,
    sort_upcoming_matches,
    write_front,
)


class MatchAlreadyStarted(RuntimeError):
    pass


class LeagueBrowser:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.last_scan_stats = {"total": 0, "started": 0, "upcoming": 0}
        self.skipped_started: list[dict[str, Any]] = []

    async def open(self) -> None:
        await open_matches_page(self.page)
        container = await find_league_container(self.page)
        if container is None:
            raise RuntimeError("Контейнер целевой лиги не найден.")

    async def scan(self) -> list[dict[str, Any]]:
        container = await find_league_container(self.page)
        if container is None:
            raise RuntimeError("Контейнер целевой лиги не найден.")

        games = container.locator(MATCH_SELECTOR)
        upcoming: list[dict[str, Any]] = []
        started: list[dict[str, Any]] = []

        for index in range(await games.count()):
            item = await parse_match(games.nth(index), index + 1)
            if not item or item["finished"]:
                continue
            if item["is_upcoming"]:
                upcoming.append(item)
            elif item["period"]:
                started.append(item)

        upcoming = sort_upcoming_matches(upcoming)
        for number, item in enumerate(upcoming, start=1):
            item["number"] = number
        await write_front(upcoming)
        self.skipped_started = started
        self.last_scan_stats = {
            "total": await games.count(),
            "started": len(started),
            "upcoming": len(upcoming),
        }
        return upcoming

    async def revalidate_upcoming(self, match: dict[str, Any]) -> dict[str, Any]:
        href = match.get("href")
        if not href:
            raise RuntimeError("У выбранного матча отсутствует href.")
        href_value = json.dumps(href)
        link = self.page.locator(
            f"a.dashboard-game-block__link[href={href_value}]"
        ).first
        await link.wait_for(state="attached", timeout=10_000)
        block = link.locator(
            "xpath=ancestor::li[contains(concat(' ', normalize-space(@class), ' '), "
            "' dashboard-champ__game ')][1]"
        )
        current = await parse_match(block, int(match.get("number") or 1))
        if current is None or not current.get("is_upcoming"):
            period = (current or {}).get("period") or ""
            raise MatchAlreadyStarted(
                f"Матч уже начался перед открытием; period={period or '<empty>'}"
            )
        return current

    async def open_match(self, match: dict[str, Any]) -> dict[str, str]:
        match = await self.revalidate_upcoming(match)
        href = match["href"]

        href_value = json.dumps(href)
        link = self.page.locator(
            f"a.dashboard-game-block__link[href={href_value}]"
        ).first
        await link.wait_for(state="visible", timeout=10_000)
        await link.scroll_into_view_if_needed()
        await link.click()
        selector = "a.dashboard-game-block__link"

        try:
            if match.get("match_id"):
                await self.page.wait_for_url(
                    re.compile(rf".*/{re.escape(match['match_id'])}-[^/?#]+.*"),
                    timeout=10_000,
                )
            else:
                await self.page.wait_for_timeout(1500)
        except Exception:
            target_url = match.get("url")
            if not target_url:
                raise RuntimeError("После клика страница матча не открылась.")
            await self.page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)

        return {"selector": selector, "url": self.page.url}
