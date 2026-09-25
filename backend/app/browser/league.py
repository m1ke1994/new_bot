import asyncio
import re
from typing import Any

from playwright.async_api import Page

from backend.app.browser.scoreboard import ScoreReadError, read_scoreboard
from backend.app.match_filters import excluded_team_in_match
from xbet_config import SELECTORS
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


class MatchContentLoadTimeout(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


MATCH_CARD_READ_ATTEMPTS = 4
MATCH_CARD_RETRY_DELAY_MS = 75
MATCH_CONTENT_READY_TIMEOUT_MS = 30_000
MATCH_CONTENT_READY_POLL_MS = 250

MATCH_CONTENT_MARKET_SELECTORS = tuple(
    selector
    for selector in (
        '.game-panel__markets input.ui-search-default__input[placeholder="Поиск по рынкам"]',
        '.market-grid-game-panel__markets input.ui-search-default__input[placeholder="Поиск по рынкам"]',
        'input.ui-search-default__input[placeholder="Поиск по рынкам"]',
        SELECTORS.market_group,
        ".game-markets-group",
        SELECTORS.canvas,
        "canvas.market-grid-canvas__canvas",
    )
    if selector
)


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

    async def _visible_market_surface(self) -> str | None:
        """Return a concrete rendered market surface, not just the match URL."""
        for selector in MATCH_CONTENT_MARKET_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if not await locator.count() or not await locator.is_visible():
                    continue
                box = await locator.bounding_box()
                if box is not None and (
                    float(box.get("width") or 0.0) < 20.0
                    or float(box.get("height") or 0.0) < 12.0
                ):
                    continue
                return selector
            except Exception:
                continue
        return None

    async def wait_match_content_ready(
        self,
        *,
        timeout_ms: int = MATCH_CONTENT_READY_TIMEOUT_MS,
    ) -> dict[str, Any]:
        """Wait until both scoreboard and a real market surface are rendered."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max(0, timeout_ms) / 1000.0
        attempts = 0
        last_score_error = ""
        last_market_selector: str | None = None

        while True:
            attempts += 1
            scoreboard = None
            try:
                scoreboard = await read_scoreboard(self.page)
                last_score_error = ""
            except ScoreReadError as error:
                last_score_error = str(error)

            last_market_selector = await self._visible_market_surface()
            if scoreboard is not None and last_market_selector is not None:
                return {
                    "ready": True,
                    "attempts": attempts,
                    "elapsed_ms": int((loop.time() - started) * 1000),
                    "score": scoreboard.score.text(),
                    "period": scoreboard.period or "",
                    "market_selector": last_market_selector,
                }

            if loop.time() >= deadline:
                raise MatchContentLoadTimeout(
                    "Страница матча открыта, но scoreboard/рынки не дорисовались "
                    f"за {timeout_ms / 1000:.0f} секунд.",
                    details={
                        "url": self.page.url,
                        "attempts": attempts,
                        "scoreboard_ready": scoreboard is not None,
                        "market_ready": last_market_selector is not None,
                        "market_selector": last_market_selector,
                        "last_score_error": last_score_error,
                    },
                )

            await self.page.wait_for_timeout(MATCH_CONTENT_READY_POLL_MS)

    async def open_match(self, match: dict[str, Any]) -> dict[str, str]:
        match = await self.revalidate_upcoming(match)
        href = match["href"]
        target_url = match.get("url")

        link = self.page.locator(exact_match_link_selector(href)).first
        await link.wait_for(state="visible", timeout=10_000)
        await link.scroll_into_view_if_needed()

        # This is a Vue SPA route. Locator.click() used to wait for navigation
        # internally and could spend the full 30s even after the click had
        # already been dispatched. Dispatch the click only, then own the route
        # wait explicitly below.
        click_error: Exception | None = None
        try:
            await link.click(timeout=5_000, no_wait_after=True)
        except Exception as error:
            click_error = error

        selector = "a.ui-game-card__link"
        route_ready = False
        if match.get("match_id"):
            try:
                await self.page.wait_for_url(
                    re.compile(rf".*/{re.escape(match['match_id'])}-[^/?#]+.*"),
                    timeout=7_500,
                )
                route_ready = True
            except Exception:
                route_ready = False
        else:
            await self.page.wait_for_timeout(500)
            route_ready = click_error is None

        if not route_ready:
            if not target_url:
                if click_error is not None:
                    raise RuntimeError(
                        f"Не удалось открыть матч кликом: {click_error}"
                    ) from click_error
                raise RuntimeError("После клика страница матча не открылась.")
            await self.page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=15_000,
            )

        return {"selector": selector, "url": self.page.url}
