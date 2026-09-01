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

    def set_logger(self, logger: Logger) -> None:
        self._logger = logger

    async def _log(self, event: str, message: str) -> None:
        if self._logger is not None:
            await self._logger(event, message)

    def _mark_context_closed(self) -> None:
        self._context_closed = True
        self.context = None
        self.page = None

    def _context_is_alive(self) -> bool:
        if self.context is None or self._context_closed:
            return False
        try:
            self.context.pages
        except Exception:
            return False
        return True

    async def start(self) -> Page:
        async with self.lock:
            if self._context_is_alive():
                page = await self._ensure_page_locked()
                await self._log("BROWSER_ALREADY_RUNNING", "Persistent Chromium уже запущен")
                return page
            return await self._launch_locked(recovered=False)

    async def ensure_browser(self) -> BrowserContext:
        async with self.lock:
            if not self._context_is_alive():
                await self._launch_locked(recovered=self.playwright is not None)
            assert self.context is not None
            return self.context

    async def ensure_page(self) -> Page:
        async with self.lock:
            if not self._context_is_alive():
                return await self._launch_locked(recovered=True)
            return await self._ensure_page_locked()

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

        self.context = context
        self.generation += 1
        self._context_closed = False
        context.on("close", lambda *_: self._mark_context_closed())
        self.page = context.pages[0] if context.pages else await context.new_page()
        await self._log(
            "BROWSER_CONTEXT_RECOVERED" if recovered else "BROWSER_STARTED",
            "Visible persistent Chromium context is ready",
        )
        await self._log("BROWSER_PAGE_READY", self.page.url or "about:blank")
        return self.page

    async def _ensure_page_locked(self) -> Page:
        if self.page is not None and not self.page.is_closed():
            return self.page
        assert self.context is not None
        open_pages = [page for page in self.context.pages if not page.is_closed()]
        self.page = open_pages[0] if open_pages else await self.context.new_page()
        await self._log("BROWSER_PAGE_RECOVERED", self.page.url or "about:blank")
        return self.page

    async def stop(self) -> dict[str, str]:
        async with self.lock:
            context, playwright = self.context, self.playwright
            self.context = None
            self.page = None
            self.playwright = None
            self._context_closed = True
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
        page_open = bool(self.page is not None and not self.page.is_closed())
        return {
            "status": "OPEN" if context_open else "CLOSED",
            "context": "OPEN" if context_open else "CLOSED",
            "page": "OPEN" if page_open else "CLOSED",
        }


BROWSER_MANAGER = BrowserManager()
