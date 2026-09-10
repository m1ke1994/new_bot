import asyncio
import re
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from auth import authorize
from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from xbet_config import get_table_tennis_url

from .models import TableTennisLeague, TableTennisMatch
from .selectors import (
    ACCORDION_TRIGGER_SELECTOR,
    LEAGUE_GAMES_COUNT_SELECTOR,
    LEAGUE_GROUP_SELECTOR,
    LEAGUE_LINK_SELECTOR,
    LEAGUE_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
    MARKET_SELECTOR,
    MARKET_VALUE_SELECTOR,
    MATCH_CARD_SELECTOR,
    MATCH_LINK_SELECTOR,
    PERIOD_SELECTOR,
    PLAYER_SELECTOR,
    SCORE_SELECTOR,
    TIME_SELECTOR,
)
from .state import TABLE_TENNIS_STATE, TableTennisStateStore, utc_now


LEAGUE_PATH_RE = re.compile(r"^/ru/live/table-tennis/(\d+)(?:-[^/?#]+)?/?$")
EVENT_PATH_RE = re.compile(
    r"^/ru/live/table-tennis/\d+(?:-[^/?#]+)?/(\d+)(?:-[^/?#]+)?/?$"
)


class ScanAlreadyRunning(RuntimeError):
    pass


class TableTennisScanError(RuntimeError):
    pass


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def parse_league_id(href: str | None) -> str | None:
    """Accept only a real league URL, never a nested event URL."""
    if not href:
        return None
    match = LEAGUE_PATH_RE.fullmatch(urlparse(href).path)
    return match.group(1) if match else None


def parse_event_id(href: str | None) -> str | None:
    if not href:
        return None
    match = EVENT_PATH_RE.fullmatch(urlparse(href).path)
    return match.group(1) if match else None


def parse_league_label(value: str) -> tuple[str, int | None]:
    text = normalize_text(value)
    match = re.search(r"\s*\((\d+)\)\s*$", text)
    if match is None:
        return text, None
    return text[: match.start()].strip(), int(match.group(1))


def parse_odd(value: str | None) -> float | None:
    text = normalize_text(value).replace(",", ".")
    match = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if match is None:
        return None
    result = float(match.group(0))
    return result if result > 0 else None


def deduplicate_matches(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        event_id = item.get("event_id")
        if event_id:
            key = ("event_id", str(event_id))
        else:
            key = (
                "fallback",
                item.get("league_id") or item.get("league_name"),
                item.get("player_1"),
                item.get("player_2"),
                item.get("url") or item.get("href"),
            )
        unique.setdefault(key, item)
    return list(unique.values())


async def _safe_text(locator: Locator) -> str | None:
    try:
        if await locator.count() == 0:
            return None
        value = normalize_text(await locator.first.inner_text())
        return value or None
    except Exception:
        return None


async def _all_text(locator: Locator) -> list[str]:
    result: list[str] = []
    try:
        for index in range(await locator.count()):
            value = normalize_text(await locator.nth(index).inner_text())
            if value:
                result.append(value)
    except Exception:
        return result
    return result


async def _group_name_for_link(link: Locator) -> str | None:
    try:
        group = link.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' dashboard-champ-group-item ')][1]"
        ).first
        if await group.count() == 0:
            return None
        trigger_text = await _safe_text(group.locator(ACCORDION_TRIGGER_SELECTOR))
        if not trigger_text:
            return None
        first_line = next(
            (normalize_text(line) for line in trigger_text.splitlines() if normalize_text(line)),
            trigger_text,
        )
        return parse_league_label(first_line)[0] or None
    except Exception:
        return None


async def expand_league_groups(page: Page, log) -> None:
    groups = page.locator(LEAGUE_GROUP_SELECTOR)
    for index in range(await groups.count()):
        group = groups.nth(index)
        trigger = group.locator(ACCORDION_TRIGGER_SELECTOR).first
        if await trigger.count() == 0:
            continue
        group_name = parse_league_label(await _safe_text(trigger) or "")[0]
        try:
            expanded = (await trigger.get_attribute("aria-expanded") or "").lower()
            classes = (await trigger.get_attribute("class") or "").lower()
            child_links = group.locator(LEAGUE_LINK_SELECTOR)
            is_expanded = expanded == "true" or any(
                marker in classes for marker in ("expanded", "opened", "active")
            )
            if is_expanded or (expanded == "" and await child_links.count() > 0):
                continue
            await trigger.scroll_into_view_if_needed()
            await trigger.click()
            try:
                await child_links.first.wait_for(state="attached", timeout=3_000)
            except PlaywrightTimeoutError:
                pass
            await log("TABLE_TENNIS_GROUP_EXPANDED", group_name or f"group #{index + 1}")
        except Exception as error:
            await log(
                "TABLE_TENNIS_GROUP_EXPAND_ERROR",
                f"{group_name or f'group #{index + 1}'}: {type(error).__name__}: {error}",
            )


async def scan_leagues(page: Page, base_url: str, log) -> list[TableTennisLeague]:
    await expand_league_groups(page, log)
    links = page.locator(LEAGUE_LINK_SELECTOR)
    leagues: dict[str, TableTennisLeague] = {}
    for index in range(await links.count()):
        link = links.nth(index)
        href = await link.get_attribute("href")
        league_id = parse_league_id(href)
        if league_id is None:
            continue
        title = await _safe_text(link.locator(LEAGUE_TITLE_SELECTOR))
        count_text = await _safe_text(link.locator(LEAGUE_GAMES_COUNT_SELECTOR))
        fallback_text = await _safe_text(link) or ""
        fallback_name, fallback_count = parse_league_label(fallback_text)
        name, inline_count = parse_league_label(title or fallback_name)
        _, separate_count = parse_league_label(count_text or "")
        if separate_count is None and count_text:
            digits = re.search(r"\d+", count_text)
            separate_count = int(digits.group(0)) if digits else None
        declared_count = separate_count if separate_count is not None else inline_count
        if declared_count is None:
            declared_count = fallback_count
        if not name:
            name = f"Лига {league_id}"
        leagues.setdefault(
            league_id,
            TableTennisLeague(
                league_id=league_id,
                name=name,
                href=href or "",
                url=urljoin(base_url, href or ""),
                declared_games_count=declared_count,
                group_name=await _group_name_for_link(link),
            ),
        )
    return list(leagues.values())


async def _read_markets(card: Locator) -> tuple[dict[str, float], dict[str, Any]]:
    odds: dict[str, float] = {}
    markets: dict[str, Any] = {}
    buttons = card.locator(MARKET_SELECTOR)
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        name = await _safe_text(button.locator(MARKET_NAME_SELECTOR))
        value = await _safe_text(button.locator(MARKET_VALUE_SELECTOR))
        if not name or value is None:
            continue
        parsed = parse_odd(value)
        markets[name] = parsed if parsed is not None else value
        normalized_name = name.casefold().replace(" ", "")
        if parsed is not None and normalized_name in {"п1", "1", "player1"}:
            odds["p1"] = parsed
        elif parsed is not None and normalized_name in {"п2", "2", "player2"}:
            odds["p2"] = parsed
    return odds, markets


async def parse_match_card(
    card: Locator,
    league: TableTennisLeague,
    base_url: str,
) -> TableTennisMatch | None:
    players = await _all_text(card.locator(PLAYER_SELECTOR))
    if len(players) < 2:
        return None
    link = card.locator(MATCH_LINK_SELECTOR).first
    href = await link.get_attribute("href") if await link.count() else None
    event_id = parse_event_id(href)
    score_values = await _all_text(card.locator(SCORE_SELECTOR))
    score = f"{score_values[0]}:{score_values[1]}" if len(score_values) == 2 else None
    period = await _safe_text(card.locator(PERIOD_SELECTOR))
    time_value = await _safe_text(card.locator(TIME_SELECTOR))
    current_set = None
    set_match = re.search(r"(\d+)\s*[-–—]?\s*(?:й|я|е)?\s*(?:сет|парт)", (period or "").casefold())
    if set_match:
        current_set = int(set_match.group(1))
    odds, markets = await _read_markets(card)
    normalized_period = (period or "").casefold()
    finished = any(
        marker in normalized_period
        for marker in ("заверш", "окончен", "finished", "full time")
    )
    if finished:
        status = "FINISHED"
        started: bool | None = True
    elif period:
        status = "LIVE"
        started = True
    else:
        status = "UNKNOWN"
        started = None
    return TableTennisMatch(
        event_id=event_id,
        league_id=league.league_id,
        league_name=league.name,
        player_1=players[0],
        player_2=players[1],
        score=score,
        sets_score=None,
        current_set=current_set,
        points_player_1=None,
        points_player_2=None,
        status=status,
        started=started,
        time=time_value,
        href=href,
        url=urljoin(base_url, href) if href else None,
        odds=odds,
        markets=markets,
        raw_score_values=score_values,
        period=period,
        collected_at=datetime.now(timezone.utc).isoformat(),
    )


async def scan_league_matches(
    page: Page,
    league: TableTennisLeague,
    base_url: str,
) -> list[TableTennisMatch]:
    await page.goto(league.url, wait_until="domcontentloaded", timeout=60_000)
    cards = page.locator(MATCH_CARD_SELECTOR)
    try:
        await cards.first.wait_for(state="attached", timeout=8_000)
    except PlaywrightTimeoutError:
        return []
    matches: list[TableTennisMatch] = []
    for index in range(await cards.count()):
        try:
            parsed = await parse_match_card(cards.nth(index), league, base_url)
            if parsed is not None:
                matches.append(parsed)
        except Exception:
            continue
    return matches


class TableTennisScanner:
    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        self.browser_manager = browser_manager
        self.state = state
        self._scan_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.logs: list[dict[str, str]] = []

    @property
    def scanning(self) -> bool:
        return (
            self._scan_lock.locked()
            or (self._task is not None and not self._task.done())
        )

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def _log(self, event: str, message: str) -> None:
        record = {"timestamp": utc_now(), "event": event, "message": message}
        self.logs.append(record)
        self.logs = self.logs[-500:]
        output = f"[TABLE_TENNIS] {event}: {message}"
        try:
            print(output)
        except UnicodeEncodeError:
            # Windows consoles may still use cp1251 while Playwright emits
            # characters outside that code page. Logging must never hide the
            # original scanner/browser error.
            print(output.encode("ascii", errors="backslashreplace").decode("ascii"))

    async def start(self) -> dict[str, Any]:
        """Start one scan in the background so the STOP endpoint stays responsive."""
        async with self._control_lock:
            if self._task is not None and not self._task.done():
                raise ScanAlreadyRunning(
                    "Сканирование настольного тенниса уже выполняется."
                )
            if self._task is not None and self._task.done():
                self._task = None
            self._stop_event.clear()
            await self.state.update(
                status="STARTING",
                scanning=True,
                error=None,
                browser=await self.browser_manager.snapshot(),
            )
            self._task = asyncio.create_task(
                self._run_background(), name="table-tennis-scanner"
            )
        await asyncio.sleep(0)
        return await self.state.snapshot()

    async def _run_background(self) -> None:
        try:
            await self.scan()
        except asyncio.CancelledError:
            raise
        except TableTennisScanError:
            # scan() has already published the full diagnostic state.
            pass

    async def stop(self) -> dict[str, Any]:
        """Stop the forks scanner and close the shared persistent Chromium."""
        async with self._control_lock:
            self._stop_event.set()
            task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self._control_lock:
            if self._task is task:
                self._task = None
        await self.browser_manager.stop()
        await self._log(
            "TABLE_TENNIS_STOPPED",
            "[TABLE_TENNIS] scanner stopped; Playwright browser closed",
        )
        return await self.state.update(
            status="STOPPED",
            scanning=False,
            current_league=None,
            browser=await self.browser_manager.snapshot(),
            error=None,
        )

    async def scan(self) -> dict[str, Any]:
        if self._scan_lock.locked():
            raise ScanAlreadyRunning("Сканирование настольного тенниса уже выполняется.")
        async with self._scan_lock:
            started_at = utc_now()
            await self.state.update(
                status="STARTING",
                scanning=True,
                current_league=None,
                last_scan_started_at=started_at,
                error=None,
                league_errors=[],
                browser=await self.browser_manager.snapshot(),
            )
            await self._log("TABLE_TENNIS_SCAN_STARTED", "[TABLE_TENNIS] scanner started")
            try:
                page = await self.browser_manager.ensure_page()
                await self.state.update(browser=await self.browser_manager.snapshot())

                async def on_waiting() -> None:
                    await self.state.update(status="WAITING_MANUAL_LOGIN", auth_status="WAITING_MANUAL_LOGIN")

                async def on_authorized() -> None:
                    await self.state.update(status="AUTHORIZED", authorized=True, auth_status="AUTHORIZED")

                auth_result = await authorize(
                    page,
                    self._stop_event,
                    on_waiting,
                    on_authorized,
                )
                if not auth_result.get("ok"):
                    raise TableTennisScanError("Авторизация была остановлена.")
                if self._stop_event.is_set():
                    raise asyncio.CancelledError
                auth_status = str(auth_result.get("status") or "UNKNOWN")
                authorized = auth_status == "AUTHORIZED"
                await self.state.update(authorized=authorized, auth_status=auth_status)
                await self._log(
                    "TABLE_TENNIS_AUTH",
                    "[TABLE_TENNIS] authorization OK" if authorized else f"authorization status: {auth_status}",
                )

                table_tennis_url = get_table_tennis_url()
                if not table_tennis_url:
                    raise TableTennisScanError("TABLE_TENNIS_URL отсутствует в .env")
                await self.state.update(status="NAVIGATING", current_url=table_tennis_url)
                await self._log(
                    "TABLE_TENNIS_NAVIGATING",
                    f"[TABLE_TENNIS] navigating to {table_tennis_url}",
                )
                await page.goto(table_tennis_url, wait_until="domcontentloaded", timeout=60_000)
                root_candidates = page.locator(f"{LEAGUE_GROUP_SELECTOR}, {LEAGUE_LINK_SELECTOR}")
                await root_candidates.first.wait_for(state="attached", timeout=20_000)

                await self.state.update(status="SCANNING_LEAGUES", current_url=page.url)
                leagues = await scan_leagues(page, table_tennis_url, self._log)
                await self._log(
                    "TABLE_TENNIS_LEAGUES_FOUND",
                    f"[TABLE_TENNIS] leagues found: {len(leagues)}",
                )
                await self.state.update(leagues_found=len(leagues))

                collected: list[dict[str, Any]] = []
                scanned_leagues: list[TableTennisLeague] = list(leagues)
                league_errors: list[dict[str, str]] = []
                await self.state.replace_results(
                    [item.to_dict() for item in scanned_leagues], []
                )
                for league_index, league in enumerate(leagues):
                    if self._stop_event.is_set():
                        raise asyncio.CancelledError
                    await self.state.update(
                        status="SCANNING",
                        current_league=league.name,
                        current_url=league.url,
                    )
                    await self._log(
                        "TABLE_TENNIS_SCANNING_LEAGUE",
                        f"[TABLE_TENNIS] scanning league: {league.name}",
                    )
                    try:
                        league_matches = await scan_league_matches(
                            page, league, table_tennis_url
                        )
                        collected.extend(item.to_dict() for item in league_matches)
                        scanned_leagues[league_index] = replace(
                            league, parsed_games_count=len(league_matches)
                        )
                        await self._log(
                            "TABLE_TENNIS_MATCHES_FOUND",
                            f"[TABLE_TENNIS] {league.name}: matches found: {len(league_matches)}",
                        )
                    except Exception as error:
                        league_errors.append(
                            {
                                "league": league.name,
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
                        await self._log(
                            "TABLE_TENNIS_LEAGUE_ERROR",
                            f"{league.name}: {type(error).__name__}: {error}",
                        )
                    unique_matches = deduplicate_matches(collected)
                    await self.state.replace_results(
                        [item.to_dict() for item in scanned_leagues], unique_matches
                    )
                    await self.state.update(
                        matches_found=len(unique_matches),
                        league_errors=league_errors,
                    )

                unique_matches = deduplicate_matches(collected)
                try:
                    await page.goto(table_tennis_url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as error:
                    await self._log(
                        "TABLE_TENNIS_RETURN_WARNING",
                        f"Не удалось вернуться в раздел: {type(error).__name__}: {error}",
                    )
                finished_at = utc_now()
                await self.state.replace_results(
                    [item.to_dict() for item in scanned_leagues], unique_matches
                )
                await self.state.update(
                    status="READY",
                    scanning=False,
                    leagues_found=len(scanned_leagues),
                    matches_found=len(unique_matches),
                    current_league=None,
                    current_url=page.url,
                    last_scan_finished_at=finished_at,
                    browser=await self.browser_manager.snapshot(),
                    league_errors=league_errors,
                    error=None,
                )
                await self._log(
                    "TABLE_TENNIS_SCAN_FINISHED",
                    f"[TABLE_TENNIS] scan finished; leagues: {len(scanned_leagues)}; unique matches: {len(unique_matches)}",
                )
                return await self.state.snapshot()
            except Exception as error:
                if self._stop_event.is_set():
                    raise asyncio.CancelledError
                await self.state.update(
                    status="ERROR",
                    scanning=False,
                    current_league=None,
                    last_scan_finished_at=utc_now(),
                    browser=await self.browser_manager.snapshot(),
                    error=f"{type(error).__name__}: {error}",
                )
                await self._log(
                    "TABLE_TENNIS_SCAN_ERROR",
                    f"{type(error).__name__}: {error}",
                )
                if isinstance(error, TableTennisScanError):
                    raise
                raise TableTennisScanError(str(error)) from error


TABLE_TENNIS_SCANNER = TableTennisScanner()
