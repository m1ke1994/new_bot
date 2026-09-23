import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from backend.app.browser.navigation import goto_with_retry
from xbet_config import SELECTORS, TEXTS, URL_CONFIG


SITE_URL = URL_CONFIG.login_url
# Configured selector stays first, while the confirmed current/legacy header
# layouts are kept as fallbacks so a bookmaker wrapper rename does not cost a
# 40-second auth timeout.
BALANCE_SELECTOR = SELECTORS.auth_marker
BALANCE_CURRENCY = TEXTS.auth_marker
AUTH_SELECTOR_CANDIDATES = tuple(
    dict.fromkeys(
        selector
        for selector in (
            BALANCE_SELECTOR,
            ".double-row-header-balance-info__currency",
            ".double-row-header-balance__info",
            ".balance__currency",
        )
        if selector
    )
)

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"

PAGE_TIMEOUT = 60_000
# Auth is checked frequently because this is only a tiny DOM lookup. The old
# 2-second poll could unnecessarily stall an already-authorized session.
MANUAL_LOGIN_POLL_INTERVAL = 0.25
MANUAL_LOGIN_TIMEOUT = 40.0
# There is no reason to sleep for 25 seconds after RUB has already been found.
AUTHORIZED_CONTINUE_DELAY = 0.0

WaitingCallback = Callable[[], Awaitable[None]]


def log(message: str) -> None:
    print(f"[AUTH] {message}")


def _contains_currency(text: str | None, currency: str = BALANCE_CURRENCY) -> bool:
    if not text or not currency:
        return False
    # inner_text() usually returns "RUB\n9.13", while text_content() can return
    # "RUB9.13". A word-boundary match safely handles both representations.
    return bool(re.search(rf"(?<![A-Za-zА-Яа-я0-9]){re.escape(currency)}(?![A-Za-zА-Яа-я0-9])", text.strip()))


async def check_balance(page: Page) -> bool:
    """Confirm authorization by the balance header containing exact RUB text."""
    for selector in AUTH_SELECTOR_CANDIDATES:
        try:
            candidates = page.locator(selector)
            count = await candidates.count()
        except Exception:
            continue

        for index in range(count):
            candidate = candidates.nth(index)
            try:
                text = await candidate.inner_text()
            except Exception:
                try:
                    text = await candidate.text_content()
                except Exception:
                    text = None
            if _contains_currency(text):
                return True

            # Current layout keeps RUB in a nested ui-caption span inside the
            # balance currency container. This avoids depending on balance value.
            try:
                if await candidate.get_by_text(BALANCE_CURRENCY, exact=True).count():
                    return True
            except Exception:
                pass
    return False


async def open_site(page: Page, url: str = SITE_URL) -> None:
    """Open only the configured site; login remains entirely manual."""
    if not url:
        raise RuntimeError("XBET_URL отсутствует в .env")

    log(f"Открываем сайт: {url}")
    await goto_with_retry(
        page,
        url,
        timeout_ms=PAGE_TIMEOUT,
        attempts=2,
        logger=log,
    )
    # domcontentloaded is enough. The auth poll below observes the header as
    # soon as Vue renders it, instead of paying an unconditional 3-second wait.
    log("DOM сайта загружен")


async def _wait_poll_interval(
    stop_event: asyncio.Event | None,
    timeout: float,
) -> bool:
    """Wait for the next check and return False when a stop was requested."""
    if timeout <= 0:
        return stop_event is None or not stop_event.is_set()
    if stop_event is None:
        await asyncio.sleep(timeout)
        return True

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        return True
    return False


async def wait_for_manual_login(
    page: Page,
    stop_event: asyncio.Event | None = None,
    on_authorized: WaitingCallback | None = None,
) -> dict[str, Any]:
    """Observe RUB for at most 40 seconds without touching the login form."""
    started_at = asyncio.get_running_loop().time()

    while asyncio.get_running_loop().time() - started_at < MANUAL_LOGIN_TIMEOUT:
        if stop_event is not None and stop_event.is_set():
            return {"ok": False, "status": "AUTH_STOPPED"}

        if page.is_closed():
            raise RuntimeError("Страница Chromium была закрыта во время ручного входа")

        if await check_balance(page):
            log("Обнаружен баланс RUB")
            log("Авторизация подтверждена")
            if on_authorized is not None:
                await on_authorized()
            log("Продолжаем работу программы без дополнительной задержки")
            return {"ok": True, "status": "AUTHORIZED"}

        elapsed = asyncio.get_running_loop().time() - started_at
        remaining = MANUAL_LOGIN_TIMEOUT - elapsed
        if remaining <= 0:
            break
        if not await _wait_poll_interval(
            stop_event,
            min(MANUAL_LOGIN_POLL_INTERVAL, remaining),
        ):
            break

    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "status": "AUTH_STOPPED"}

    log("Таймаут ожидания ручной авторизации: 40 секунд")
    log("RUB не обнаружен")
    log("Продолжаем выполнение программы")
    return {"ok": True, "status": "AUTH_TIMEOUT"}


async def authorize(
    page: Page,
    stop_event: asyncio.Event | None = None,
    on_waiting: WaitingCallback | None = None,
    on_authorized: WaitingCallback | None = None,
) -> dict[str, Any]:
    """Open XBET_URL and apply the bounded manual-authorization flow."""
    await open_site(page)
    log("Проверяем авторизацию")

    if await check_balance(page):
        log("Найден баланс RUB")
        log("Пользователь авторизован")
        if on_authorized is not None:
            await on_authorized()
        if AUTHORIZED_CONTINUE_DELAY > 0:
            if not await _wait_poll_interval(stop_event, AUTHORIZED_CONTINUE_DELAY):
                return {"ok": False, "status": "AUTH_STOPPED"}
        log("Продолжаем работу программы без дополнительной задержки")
        return {"ok": True, "status": "AUTHORIZED"}

    log("RUB не найден")
    log("Ожидаем ручную авторизацию, максимум 40 секунд")
    if on_waiting is not None:
        await on_waiting()

    return await wait_for_manual_login(page, stop_event, on_authorized)


async def main() -> None:
    from backend.app.browser.manager import BROWSER_MANAGER

    page = await BROWSER_MANAGER.start()
    try:
        result = await authorize(page)
        print(result)
        if result.get("status") == "AUTHORIZED":
            print("Авторизация выполнена. Нажмите ENTER для завершения.")
            await asyncio.to_thread(input)
        elif result.get("status") == "AUTH_TIMEOUT":
            print("Ожидание авторизации завершено. Нажмите ENTER для завершения.")
            await asyncio.to_thread(input)
    finally:
        await BROWSER_MANAGER.stop()


if __name__ == "__main__":
    asyncio.run(main())
