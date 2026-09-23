import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from auth import PROFILE_DIR


Logger = Callable[[str, str], Awaitable[Any]]


class BrowserManager:
    """Owns the only Playwright lifecycle used by the application."""

    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()
        self._context_closed = True
        self._logger: Logger | None = None
        self.generation = 0
        self._navigation_recovery_stage = 0

    def set_logger(self, logger: Logger) -> None:
        self._logger = logger

    async def _log(self, event: str, message: str) -> None:
        if self._logger is not None:
            await self._logger(event, message)

    def _mark_context_closed(self) -> None:
        self._context_closed = True
        self.context = None
        self.page = None
        self._navigation_recovery_stage = 0

    def _context_is_alive(self) -> bool:
        if self.context is None or self._context_closed:
            return False
        try:
            browser = self.context.browser
            if browser is not None and not browser.is_connected():
                return False
            self.context.pages
        except Exception:
            return False
        return True

    async def _discard_stale_context_locked(self) -> None:
        context = self.context
        self._mark_context_closed()
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    async def _recover_page_locked(self, error: Exception | None = None) -> Page:
        if error is not None:
            await self._log(
                "BROWSER_STALE_RUNTIME",
                f"Discarding stale browser context: {type(error).__name__}: {error}",
            )
        await self._discard_stale_context_locked()
        return await self._launch_locked(recovered=True)

    async def start(self) -> Page:
        async with self.lock:
            if self._context_is_alive():
                try:
                    page = await self._ensure_page_locked()
                except Exception as error:
                    return await self._recover_page_locked(error)
                await self._log("BROWSER_ALREADY_RUNNING", "Persistent Chromium уже запущен")
                return page
            if self.context is not None:
                return await self._recover_page_locked()
            return await self._launch_locked(recovered=False)

    async def ensure_browser(self) -> BrowserContext:
        async with self.lock:
            if not self._context_is_alive():
                if self.context is not None:
                    await self._recover_page_locked()
                else:
                    await self._launch_locked(recovered=self.playwright is not None)
            assert self.context is not None
            return self.context

    async def ensure_page(self) -> Page:
        async with self.lock:
            if not self._context_is_alive():
                if self.context is not None:
                    return await self._recover_page_locked()
                return await self._launch_locked(recovered=True)
            try:
                return await self._ensure_page_locked()
            except Exception as error:
                return await self._recover_page_locked(error)

    async def recover_navigation(self, error: Exception) -> Page:
        """Replace a detached page first, then restart the context if it repeats."""
        async with self.lock:
            if not self._context_is_alive():
                self._navigation_recovery_stage = 0
                return await self._recover_page_locked(error)

            if self._navigation_recovery_stage >= 1:
                self._navigation_recovery_stage = 0
                await self._log(
                    "NAVIGATION_CONTEXT_RESTART",
                    (
                        "Новая вкладка также не загрузила сайт; "
                        f"перезапускаем persistent Chromium: {type(error).__name__}: {error}"
                    ),
                )
                return await self._recover_page_locked(error)

            assert self.context is not None
            old_page = self.page
            old_url = self._page_url(old_page) if old_page is not None else "<none>"
            try:
                new_page = await self.context.new_page()
                await new_page.bring_to_front()
            except Exception as replacement_error:
                self._navigation_recovery_stage = 0
                await self._log(
                    "NAVIGATION_PAGE_REPLACE_FAILED",
                    f"{type(replacement_error).__name__}: {replacement_error}",
                )
                return await self._recover_page_locked(replacement_error)

            self.page = new_page
            self.generation += 1
            self._navigation_recovery_stage = 1
            if old_page is not None and old_page is not new_page:
                try:
                    await old_page.close()
                except Exception:
                    pass
            await self._log(
                "NAVIGATION_PAGE_REPLACED",
                (
                    f"Сбойная вкладка заменена: old_url={old_url or 'about:blank'}; "
                    f"new_url={self._page_url(new_page) or 'about:blank'}; "
                    f"reason={type(error).__name__}: {error}"
                ),
            )
            return new_page

    async def navigation_succeeded(self) -> None:
        """Reset escalation after the target site produced a usable document."""
        async with self.lock:
            self._navigation_recovery_stage = 0

    @staticmethod
    def _page_url(page: Page) -> str:
        try:
            return str(page.url or "")
        except Exception:
            return ""

    @classmethod
    def _select_reusable_page(
        cls,
        pages: list[Page],
        preferred: Page | None = None,
    ) -> Page | None:
        """Prefer the current real site tab, then the newest non-blank tab."""
        open_pages: list[Page] = []
        for page in pages:
            try:
                if not page.is_closed():
                    open_pages.append(page)
            except Exception:
                continue
        if preferred in open_pages and cls._page_url(preferred) not in {"", "about:blank"}:
            return preferred
        for page in reversed(open_pages):
            if cls._page_url(page) not in {"", "about:blank"}:
                return page
        if preferred in open_pages:
            return preferred
        return open_pages[-1] if open_pages else None

    async def prepare_session_page(self) -> Page:
        """Select and stabilize the persistent tab before a new worker starts."""
        async with self.lock:
            if not self._context_is_alive():
                if self.context is not None:
                    page = await self._recover_page_locked()
                else:
                    page = await self._launch_locked(recovered=True)
            else:
                try:
                    page = await self._ensure_page_locked()
                except Exception as error:
                    page = await self._recover_page_locked(error)

            try:
                await page.bring_to_front()
            except Exception as error:
                if page.is_closed():
                    page = await self._recover_page_locked(error)
                    await page.bring_to_front()

            ready_state = "unknown"
            try:
                # stop() clears a navigation left in progress when a previous
                # worker was cancelled, while preserving cookies/profile state.
                ready_state = await page.evaluate(
                    """
                    () => {
                      const state = document.readyState;
                      if (state === 'loading') window.stop();
                      return state;
                    }
                    """
                )
            except Exception as error:
                # Execution context destruction is normal while Chromium is in
                # the middle of a redirect. goto_with_retry() will settle it.
                await self._log(
                    "BROWSER_SESSION_PAGE_BUSY",
                    f"{type(error).__name__}: {error}",
                )
            await self._log(
                "BROWSER_SESSION_PAGE_READY",
                f"url={self._page_url(page) or 'about:blank'}; ready_state={ready_state}",
            )
            return page

    async def _launch_locked(self, *, recovered: bool) -> Page:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        try:
            context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport=None,
            )
        except Exception:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
            raise

        # Do not instrument Canvas/Path2D during the bookmaker application's
        # initial boot. Hook v4 is intentionally installed lazily by the market
        # reader only after the match page has rendered. Replacing/intercepting
        # Path2D during Vue/application startup can delay or break skeleton
        # hydration on some bookmaker builds.
        self.context = context
        self.generation += 1
        self._navigation_recovery_stage = 0
        self._context_closed = False
        context.on("close", lambda *_: self._mark_context_closed())
        self.page = self._select_reusable_page(list(context.pages))
        if self.page is None:
            self.page = await context.new_page()

        await self._log(
            "CANVAS_2D_HOOK_DEFERRED",
            (
                "Canvas 2D hook v4 deferred until market reading; "
                "bookmaker page boot remains untouched."
            ),
        )
        await self._log(
            "BROWSER_CONTEXT_RECOVERED" if recovered else "BROWSER_STARTED",
            "Visible persistent Chromium context is ready",
        )
        await self._log("BROWSER_PAGE_READY", self.page.url or "about:blank")
        return self.page

    async def _ensure_page_locked(self) -> Page:
        assert self.context is not None
        selected = self._select_reusable_page(list(self.context.pages), self.page)
        if selected is None:
            selected = await self.context.new_page()
        if selected is not self.page:
            previous_url = self._page_url(self.page) if self.page is not None else "<none>"
            self.page = selected
            await self._log(
                "BROWSER_PAGE_RECOVERED",
                f"{previous_url} -> {self._page_url(self.page) or 'about:blank'}",
            )
        else:
            self.page = selected
        return self.page

    async def stop(self) -> dict[str, str]:
        async with self.lock:
            context, playwright = self.context, self.playwright
            self.context = None
            self.page = None
            self.playwright = None
            self._context_closed = True
            self._navigation_recovery_stage = 0
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass
            await self._log("BROWSER_CLOSED", "Persistent Chromium закрыт явно")
        return await self.snapshot()

    async def snapshot(self) -> dict[str, str]:
        context_open = self._context_is_alive()
        try:
            page_open = bool(context_open and self.page is not None and not self.page.is_closed())
        except Exception:
            page_open = False
        return {
            "status": "OPEN" if context_open else "CLOSED",
            "context": "OPEN" if context_open else "CLOSED",
            "page": "OPEN" if page_open else "CLOSED",
        }


BROWSER_MANAGER = BrowserManager()
