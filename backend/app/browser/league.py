import re
from typing import Any

from playwright.async_api import Page

from backend.app.match_filters import excluded_team_in_match
from matches import (
    exact_match_link_selector,
    find_league_container,
    league_match_links,
    match_card_for_link,
    open_matches_page,
    parse_match,
    sort_upcoming_matches,
    write_front,
)


class MatchAlreadyStarted(RuntimeError):
    pass


class LeagueBrowser:
    def __init__(self, page: Page, *, exclude_teams_enabled: bool = True) -> None:
        self.page = page
        self.exclude_teams_enabled = exclude_teams_enabled
        self.last_scan_stats = {
            "total": 0,
            "started": 0,
            "excluded": 0,
            "upcoming": 0,
        }
        self.skipped_started: list[dict[str, Any]] = []
        self.skipped_excluded: list[dict[str, Any]] = []

    async def open(self) -> None:
        await open_matches_page(self.page)
        container = await find_league_container(self.page)
        if container is None:
            raise RuntimeError("Контейнер целевой лиги не найден.")

    async def scan(self) -> list[dict[str, Any]]:
        container = await find_league_container(self.page)
        if container is None:
            raise RuntimeError("Контейнер целевой лиги не найден.")

        links = league_match_links(container)
        total = await links.count()
        print(f"[SELECTOR] Найдено матчей: {total}")
        upcoming: list[dict[str, Any]] = []
        started: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        for index in range(total):
            item = await parse_match(match_card_for_link(links.nth(index)), index + 1)
            if not item or item["finished"]:
                continue
            if item["is_upcoming"]:
                excluded_team = excluded_team_in_match(
                    item["team1"],
                    item["team2"],
                    enabled=self.exclude_teams_enabled,
                )
                if excluded_team is not None:
                    excluded.append({**item, "excluded_team": excluded_team})
                else:
                    upcoming.append(item)
            elif item["period"]:
                started.append(item)

        upcoming = sort_upcoming_matches(upcoming)
        for number, item in enumerate(upcoming, start=1):
            item["number"] = number
        await write_front(upcoming)
        self.skipped_started = started
        self.skipped_excluded = excluded
        self.last_scan_stats = {
            "total": total,
            "started": len(started),
            "excluded": len(excluded),
            "upcoming": len(upcoming),
        }
        return upcoming

    async def revalidate_upcoming(self, match: dict[str, Any]) -> dict[str, Any]:
        href = match.get("href")
        if not href:
            raise RuntimeError("У выбранного матча отсутствует href.")
        link = self.page.locator(exact_match_link_selector(href)).first
        await link.wait_for(state="attached", timeout=10_000)
        block = match_card_for_link(link)
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

        link = self.page.locator(exact_match_link_selector(href)).first
        await link.wait_for(state="visible", timeout=10_000)
        await link.scroll_into_view_if_needed()
        await link.click()
        selector = "a.ui-game-card__link"

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
