import asyncio
from dataclasses import replace
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from xbet_config import get_table_tennis_odds_poll_interval

from .models import TableTennisLeague, TableTennisMatch
from .monitoring import candidate_payload, is_candidate_set
from .scanner import (
    TableTennisScanError,
    TableTennisScanner,
    parse_match_card,
    scan_leagues,
)
from .selectors import LEAGUE_GROUP_SELECTOR, LEAGUE_LINK_SELECTOR, MATCH_CARD_SELECTOR
from .state import TABLE_TENNIS_STATE, TableTennisStateStore, utc_now


def first_monitorable_candidate(
    matches: list[TableTennisMatch],
    excluded_event_ids: set[str] | None = None,
) -> TableTennisMatch | None:
    """Return the first LIVE 1st/2nd-party match in scanner/DOM order."""
    excluded = excluded_event_ids or set()
    for match in matches:
        if not is_candidate_set(match.current_set):
            continue
        if not match.event_id or not match.url:
            continue
        if str(match.event_id) in excluded:
            continue
        return match
    return None


class FirstCandidateTableTennisScanner(TableTennisScanner):
    """Continuous forks runner that stops catalog traversal at the first candidate.

    The ordinary ``scan()`` method is intentionally inherited unchanged, so the
    manual "Обновить матчи" action can still build the full diagnostic catalog.
    Only the background ``start()`` strategy uses first-candidate behaviour.
    """

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager, state)
        self._temporarily_skipped: set[str] = set()

    async def _scan_league_until_candidate(
        self,
        page: Page,
        league: TableTennisLeague,
        base_url: str,
    ) -> tuple[list[TableTennisMatch], TableTennisMatch | None]:
        """Read cards in DOM order and stop immediately on the first candidate."""
        await page.goto(league.url, wait_until="domcontentloaded", timeout=60_000)
        cards = page.locator(MATCH_CARD_SELECTOR)
        try:
            await cards.first.wait_for(state="attached", timeout=8_000)
        except PlaywrightTimeoutError:
            return [], None

        parsed_matches: list[TableTennisMatch] = []
        for index in range(await cards.count()):
            if self._stop_event.is_set():
                raise asyncio.CancelledError
            try:
                match = await parse_match_card(cards.nth(index), league, base_url)
            except Exception:
                continue
            if match is None:
                continue
            parsed_matches.append(match)
            candidate = first_monitorable_candidate(
                [match],
                self._temporarily_skipped,
            )
            if candidate is not None:
                return parsed_matches, candidate
        return parsed_matches, None

    async def _find_first_candidate(
        self,
        page: Page,
        table_tennis_url: str,
    ) -> TableTennisMatch | None:
        await self.state.update(
            status="NAVIGATING",
            scanning=True,
            current_url=table_tennis_url,
            current_league=None,
            active_match_id=None,
            active_match=None,
            last_scan_started_at=utc_now(),
            error=None,
            league_errors=[],
        )
        await self._log(
            "FORKS_SEARCH_STARTED",
            "looking for the first LIVE match in party 1 or 2",
        )

        await page.goto(
            table_tennis_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        roots = page.locator(f"{LEAGUE_GROUP_SELECTOR}, {LEAGUE_LINK_SELECTOR}")
        await roots.first.wait_for(state="attached", timeout=20_000)

        leagues = await scan_leagues(page, table_tennis_url, self._log)
        await self.state.update(
            status="SEARCHING_FIRST_CANDIDATE",
            leagues_found=len(leagues),
        )

        scanned_leagues = list(leagues)
        collected: list[TableTennisMatch] = []
        errors: list[dict[str, str]] = []
        await self.state.replace_results(
            [item.to_dict() for item in scanned_leagues],
            [],
            [],
        )

        for league_index, league in enumerate(leagues):
            if self._stop_event.is_set():
                raise asyncio.CancelledError

            await self.state.update(
                status="SEARCHING_FIRST_CANDIDATE",
                current_league=league.name,
                current_url=league.url,
            )
            await self._log(
                "FORKS_CHECKING_LEAGUE",
                f"checking league: {league.name}",
            )

            try:
                parsed, candidate = await self._scan_league_until_candidate(
                    page,
                    league,
                    table_tennis_url,
                )
                collected.extend(parsed)
                scanned_leagues[league_index] = replace(
                    league,
                    parsed_games_count=len(parsed),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append(
                    {
                        "league": league.name,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                await self._log(
                    "FORKS_LEAGUE_ERROR",
                    f"{league.name}: {type(error).__name__}: {error}",
                )
                continue

            candidate_items = [candidate_payload(candidate)] if candidate else []
            await self.state.replace_results(
                [item.to_dict() for item in scanned_leagues],
                [item.to_dict() for item in collected],
                candidate_items,
            )
            await self.state.update(
                matches_found=len(collected),
                candidates_count=len(candidate_items),
                league_errors=errors,
            )

            if candidate is not None:
                await self.state.update(
                    status="CANDIDATE_FOUND",
                    current_league=None,
                    current_url=page.url,
                    last_scan_finished_at=utc_now(),
                )
                await self._log(
                    "FORKS_FIRST_CANDIDATE_FOUND",
                    f"first candidate: {candidate.player_1} — {candidate.player_2}; "
                    f"party={candidate.current_set}",
                )
                return candidate

        await self.state.update(
            status="WAITING_FOR_CANDIDATES",
            current_league=None,
            matches_found=len(collected),
            candidates_count=0,
            last_scan_finished_at=utc_now(),
            league_errors=errors,
        )
        return None

    async def _run_strategy(self) -> None:
        if self._scan_lock.locked():
            from .scanner import ScanAlreadyRunning

            raise ScanAlreadyRunning("Сканирование настольного тенниса уже выполняется.")

        async with self._scan_lock:
            await self._log("FORKS_RUNNER_STARTED", "first-candidate runner started")
            try:
                page, table_tennis_url = await self._authorize_page()

                while not self._stop_event.is_set():
                    candidate = await self._find_first_candidate(page, table_tennis_url)
                    if candidate is None:
                        self._temporarily_skipped.clear()
                        await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                        continue

                    try:
                        outcome = await self._observe_candidate(page, candidate)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        await self.state.set_active_match(None)
                        await self._log(
                            "FORKS_CANDIDATE_ERROR",
                            f"{candidate.player_1} — {candidate.player_2}: "
                            f"{type(error).__name__}: {error}",
                        )
                        outcome = "CANDIDATE_ERROR"

                    # While MONITORING, _observe_candidate does not return: the
                    # browser remains on this exact match and odds keep polling.
                    # We only reach here after the target party starts or the
                    # candidate becomes unusable, then search from the catalog again.
                    if candidate.event_id and outcome in {
                        "TARGET_PARTY_NOT_AVAILABLE",
                        "NO_LONGER_CANDIDATE",
                        "EVENT_ID_NOT_AVAILABLE",
                        "STALE_MATCH",
                        "SKIPPED",
                        "CANDIDATE_ERROR",
                    }:
                        self._temporarily_skipped.add(str(candidate.event_id))
                    else:
                        self._temporarily_skipped.clear()

                    await self._sleep_or_stop(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._publish_error(error)


TABLE_TENNIS_SCANNER = FirstCandidateTableTennisScanner()
