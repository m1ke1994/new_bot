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


MATCH_CARD_READ_ATTEMPTS = 4
MATCH_CARD_RETRY_DELAY_MS = 75


class LeagueBrowser:
    def __init__(self, page: Page, *, exclude_teams_enabled: bool = True) -> None:
        self.page = page
        self.exclude_teams_enabled = exclude_teams_enabled
        self.last_scan_stats = {
            "total": 0,
            "started": 0,
            "finished": 0,
            "excluded": 0,
            "unclassified": 0,
            "upcoming": 0,
        }
        self.skipped_started: list[dict[str, Any]] = []
        self.skipped_excluded: list[dict[str, Any]] = []
        self.skipped_unclassified: list[dict[str, Any]] = []

    async def open(self) -> None:
        await open_matches_page(self.page)
        container = await find_league_container(self.page)
        if container is None:
            raise RuntimeError("Контейнер целевой лиги не найден.")

    async def _read_match_stable(
        self,
        link: Any,
        number: int,
    ) -> dict[str, Any] | None:
        """Retry a card while the bookmaker is between countdown render frames."""
        item: dict[str, Any] | None = None
        for attempt in range(MATCH_CARD_READ_ATTEMPTS):
            item = await parse_match(match_card_for_link(link), number)
            if item is not None and (
                item.get("finished")
                or item.get("is_upcoming")
                or bool(item.get("period"))
            ):
                return item
            if attempt + 1 < MATCH_CARD_READ_ATTEMPTS:
                await self.page.wait_for_timeout(MATCH_CARD_RETRY_DELAY_MS)
        return item

    async def scan(self) -> list[dict[str, Any]]:
        container = await find_league_container(self.page)
        if container is None:
            raise RuntimeError("Контейнер целевой лиги не найден.")

        links = league_match_links(container)
        total = await links.count()
        print(f"[SELECTOR] Найдено матчей: {total}")
        upcoming: list[dict[str, Any]] = []
        started: list[dict[str, Any]] = []
        finished: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        unclassified: list[dict[str, Any]] = []

        for index in range(total):
            item = await self._read_match_stable(links.nth(index), index + 1)
            if item is None:
                unclassified.append(
                    {
                        "number": index + 1,
                        "team1": "<не прочитано>",
                        "team2": "<не прочитано>",
                        "time": "",
                        "period": "",
                        "reason": "parse_match_returned_none",
                    }
                )
                continue
            if item["finished"]:
                finished.append(item)
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
            else:
                unclassified.append(
                    {
                        **item,
                        "reason": "countdown_or_period_not_ready",
                    }
                )

        upcoming = sort_upcoming_matches(upcoming)
        for number, item in enumerate(upcoming, start=1):
            item["number"] = number
        await write_front(upcoming)
        self.skipped_started = started
        self.skipped_excluded = excluded
        self.skipped_unclassified = unclassified
        self.last_scan_stats = {
            "total": total,
            "started": len(started),
            "finished": len(finished),
            "excluded": len(excluded),
            "unclassified": len(unclassified),
            "upcoming": len(upcoming),
        }

        # Never choose a later match while an earlier card is temporarily
        # unreadable. The next engine scan will retry the league DOM.
        if unclassified:
            return []
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
