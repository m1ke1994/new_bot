import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import Page

from xbet_config import get_xbet_url


load_dotenv()

SITE_URL = get_xbet_url()
BALANCE_SELECTOR = ".balance__currency"
BALANCE_CURRENCY = "RUB"

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"

PAGE_TIMEOUT = 60_000
MANUAL_LOGIN_POLL_INTERVAL = 2.0
MANUAL_LOGIN_TIMEOUT = 40.0
AUTHORIZED_CONTINUE_DELAY = 25.0

WaitingCallback = Callable[[], Awaitable[None]]


def log(message: str) -> None:
    print(f"[AUTH] {message}")


async def check_balance(page: Page) -> bool:
    """Confirm authorization only by .balance__currency with exact RUB text."""
    try:
        currencies = page.locator(BALANCE_SELECTOR)
        for index in range(await currencies.count()):
            text = await currencies.nth(index).text_content()
            if text and text.strip() == BALANCE_CURRENCY:
                return True
    except Exception:
        pass
    return False


async def open_site(page: Page, url: str = SITE_URL) -> None:
    """Open only the configured site; login remains entirely manual."""
    if not url:
        raise RuntimeError("XBET_URL отсутствует в .env")

    log(f"Открываем сайт: {url}")
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT,
    )
    await page.wait_for_timeout(3000)
    log("Сайт открыт")


async def _wait_poll_interval(
    stop_event: asyncio.Event | None,
    timeout: float,
) -> bool:
    """Wait for the next check and return False when a stop was requested."""
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
            log("Обнаружен .balance__currency = RUB")
            log("Авторизация подтверждена")
            if on_authorized is not None:
                await on_authorized()
            log("Продолжаем работу программы")
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
        log("Найден .balance__currency = RUB")
        log("Пользователь авторизован")
        if on_authorized is not None:
            await on_authorized()
        log("Ожидание перед продолжением: 25 секунд")
        if not await _wait_poll_interval(stop_event, AUTHORIZED_CONTINUE_DELAY):
            return {"ok": False, "status": "AUTH_STOPPED"}
        log("Продолжаем работу программы")
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
