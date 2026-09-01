import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import Page

from xbet_config import get_xbet_url


# ============================================================
# ENV
# ============================================================

load_dotenv()

SITE_URL = get_xbet_url()
LOGIN = os.getenv("XBET_LOGIN", "").strip()
PASSWORD = os.getenv("XBET_PASSWORD", "").strip()

BALANCE_SELECTOR = os.getenv(
    "XBET_BALANCE_SELECTOR",
    '[class*="balance"]',
).strip()

CAPTCHA_SELECTOR = os.getenv(
    "XBET_CAPTCHA_SELECTOR",
    ".swal2-popup",
).strip()


# ============================================================
# SETTINGS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"

PAGE_TIMEOUT = 60_000


# ============================================================
# LOG
# ============================================================

def log(message: str):
    print(f"[AUTH] {message}")


# ============================================================
# CHECK ELEMENT
# ============================================================

async def is_visible(
    page: Page,
    selector: str,
    timeout: int = 3000,
) -> bool:

    try:
        locator = page.locator(selector).first

        await locator.wait_for(
            state="visible",
            timeout=timeout,
        )

        return True

    except Exception:
        return False


# ============================================================
# CHECK BALANCE
# ============================================================

async def check_balance(page: Page) -> bool:

    log("Проверяем баланс...")

    if await is_visible(
        page,
        BALANCE_SELECTOR,
        timeout=2500,
    ):
        log("Баланс найден.")
        log("Пользователь авторизован.")

        return True

    log("Баланс НЕ найден.")

    return False


# ============================================================
# OPEN SITE
# ============================================================

async def open_site(page: Page):

    log(f"Открываем сайт: {SITE_URL}")

    await page.goto(
        SITE_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT,
    )

    log("Сайт открыт.")

    # Ждём загрузку интерфейса сайта.
    await page.wait_for_timeout(3000)


# ============================================================
# OPEN LOGIN
# ============================================================

async def open_login(page: Page):

    log("Ищем кнопку «Вход»...")

    selectors = [
        'span.ui-caption:has-text("Вход")',
        'button:has-text("Вход")',
        'a:has-text("Вход")',
        'text="Вход"',
    ]

    for selector in selectors:

        try:
            button = page.locator(selector).first

            await button.wait_for(
                state="visible",
                timeout=1200,
            )

            log(f"Кнопка найдена: {selector}")

            await button.click()

            log("Нажали «Вход».")

            await page.wait_for_timeout(1500)

            return

        except Exception:
            continue

    raise RuntimeError(
        "Не удалось найти кнопку «Вход»."
    )


# ============================================================
# FIND LOGIN FORM
# ============================================================

async def find_login_form(page: Page):

    log("Ищем модальное окно авторизации...")

    selectors = [
        '[role="dialog"]',
        '[class*="auth-form"]',
        '.auth-form',
        '[class*="modal"]',
        '.modal',
    ]

    for selector in selectors:

        try:
            containers = page.locator(selector)

            count = await containers.count()

            for i in range(count):

                container = containers.nth(i)

                try:
                    if not await container.is_visible():
                        continue

                    inputs = container.locator("input")

                    input_count = await inputs.count()

                    visible_count = 0

                    for x in range(input_count):

                        try:
                            if await inputs.nth(x).is_visible():
                                visible_count += 1
                        except Exception:
                            pass

                    if visible_count >= 2:

                        log(
                            f"Форма найдена. "
                            f"Видимых input: {visible_count}"
                        )

                        return container

                except Exception:
                    continue

        except Exception:
            continue

    raise RuntimeError(
        "Модальное окно авторизации "
        "с двумя input не найдено."
    )


# ============================================================
# GET INPUTS
# ============================================================

async def get_inputs(container):

    inputs = container.locator("input")

    count = await inputs.count()

    visible_inputs = []

    for i in range(count):

        locator = inputs.nth(i)

        try:

            if await locator.is_visible():
                visible_inputs.append(locator)

        except Exception:
            continue

    if len(visible_inputs) < 2:

        raise RuntimeError(
            "Не найдено два видимых поля ввода."
        )

    login_input = visible_inputs[0]
    password_input = visible_inputs[1]

    return login_input, password_input


# ============================================================
# FILL FIELD
# ============================================================

async def fill_field(
    locator,
    value: str,
    field_name: str,
):

    await locator.wait_for(
        state="visible",
        timeout=5000,
    )

    await locator.click()

    await locator.fill("")

    await locator.fill(value)

    try:
        current_value = await locator.input_value()
    except Exception:
        current_value = ""

    if current_value == value:

        log(f"{field_name} заполнен.")

        return

    log(
        f"{field_name}: обычный fill не подтвердился."
    )

    await locator.click()

    await locator.press("Control+A")
    await locator.press("Backspace")

    await locator.type(
        value,
        delay=50,
    )

    current_value = await locator.input_value()

    if current_value != value:

        raise RuntimeError(
            f"Не удалось заполнить {field_name}."
        )

    log(f"{field_name} заполнен.")


# ============================================================
# FILL LOGIN + PASSWORD
# ============================================================

async def fill_credentials(container):

    if not LOGIN:

        raise RuntimeError(
            "XBET_LOGIN отсутствует в .env"
        )

    if not PASSWORD:

        raise RuntimeError(
            "XBET_PASSWORD отсутствует в .env"
        )

    login_input, password_input = (
        await get_inputs(container)
    )

    log(
        "Первый input найден → ID / E-mail."
    )

    await fill_field(
        login_input,
        LOGIN,
        "LOGIN",
    )

    log(
        "Второй input найден → пароль."
    )

    await fill_field(
        password_input,
        PASSWORD,
        "PASSWORD",
    )


# ============================================================
# CLICK "ВОЙТИ"
# ============================================================

async def submit_login(container):

    log("Ищем кнопку «Войти»...")

    selectors = [
        'button:has-text("Войти")',
        'button:has-text("Вход")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]

    for selector in selectors:

        try:

            button = container.locator(
                selector
            ).first

            await button.wait_for(
                state="visible",
                timeout=3000,
            )

            log(
                f"Кнопка найдена: {selector}"
            )

            await button.click()

            log("Нажали «Войти».")

            return

        except Exception:
            continue

    raise RuntimeError(
        "Кнопка «Войти» не найдена."
    )


# ============================================================
# CAPTCHA
# ============================================================

async def check_captcha(page: Page) -> bool:

    log("Проверяем CAPTCHA...")

    if await is_visible(
        page,
        CAPTCHA_SELECTOR,
        timeout=5000,
    ):

        log("CAPTCHA найдена.")

        return True

    extra_selectors = [
        'text=/captcha/i',
        'text=/капч/i',
        'text=/Подтвердите/i',
        'text=/проверку/i',
    ]

    for selector in extra_selectors:

        if await is_visible(
            page,
            selector,
            timeout=1000,
        ):

            log("CAPTCHA / проверка найдена.")

            return True

    log("CAPTCHA не найдена.")

    return False


# ============================================================
# WAIT MANUAL CAPTCHA
# ============================================================

async def wait_manual_captcha(page: Page, stop_event: asyncio.Event | None = None):

    print()
    print("=" * 60)
    print("CAPTCHA НАЙДЕНА")
    print("Пройди CAPTCHA вручную в браузере.")
    print("=" * 60)
    print()

    while True:

        if stop_event is not None and stop_event.is_set():
            return False

        if await check_balance(page):

            print()
            print("=" * 60)
            print("АВТОРИЗАЦИЯ УСПЕШНА")
            print("=" * 60)
            print()

            return True

        if stop_event is None:
            await asyncio.sleep(2)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2)
            except TimeoutError:
                pass


# ============================================================
# AUTH
# ============================================================

async def authorize(page: Page, stop_event: asyncio.Event | None = None):

    # --------------------------------------------------------
    # 1. Открываем сайт
    # --------------------------------------------------------

    await open_site(page)

    # --------------------------------------------------------
    # 2. Проверяем баланс ДО входа
    # --------------------------------------------------------

    for attempt in range(1, 4):
        if await check_balance(page):
            return {
                "ok": True,
                "status": "AUTHORIZED_ALREADY",
            }
        if attempt < 3:
            log(f"AUTH_CHECK_RETRY {attempt}/3")
            await page.wait_for_timeout(700)

    # --------------------------------------------------------
    # 3. Баланса нет → ищем Вход
    # --------------------------------------------------------

    log(
        "Авторизация отсутствует. "
        "Переходим к форме входа."
    )

    login_error = None
    for attempt in range(1, 4):
        if await check_balance(page):
            return {"ok": True, "status": "AUTHORIZED_ALREADY"}
        try:
            await open_login(page)
            login_error = None
            break
        except RuntimeError as error:
            login_error = error
            log(f"AUTH_CHECK_RETRY {attempt}/3: {error}")
            await page.wait_for_timeout(700)
    if login_error is not None:
        return {"ok": False, "status": "LOGIN_BUTTON_NOT_READY"}

    # --------------------------------------------------------
    # 4. Ищем модальное окно
    # --------------------------------------------------------

    container = await find_login_form(page)

    # --------------------------------------------------------
    # 5. Заполняем первый и второй input
    # --------------------------------------------------------

    await fill_credentials(container)

    # --------------------------------------------------------
    # 6. Нажимаем Войти
    # --------------------------------------------------------

    await submit_login(container)

    # --------------------------------------------------------
    # 7. Ждём ответ сайта
    # --------------------------------------------------------

    log("Ждём результат авторизации...")

    await page.wait_for_timeout(3000)

    # --------------------------------------------------------
    # 8. Снова проверяем баланс
    # --------------------------------------------------------

    if await check_balance(page):

        print()
        print("=" * 60)
        print("АВТОРИЗАЦИЯ УСПЕШНА")
        print("=" * 60)
        print()

        return {
            "ok": True,
            "status": "AUTHORIZED",
        }

    # --------------------------------------------------------
    # 9. Баланса нет
    # --------------------------------------------------------

    log(
        "После отправки формы "
        "баланс не появился."
    )

    # --------------------------------------------------------
    # 10. Ищем CAPTCHA
    # --------------------------------------------------------

    if await check_captcha(page):

        completed = await wait_manual_captcha(page, stop_event)

        if not completed:
            return {"ok": False, "status": "AUTH_STOPPED"}

        return {
            "ok": True,
            "status": "AUTHORIZED_AFTER_CAPTCHA",
        }

    # --------------------------------------------------------
    # 11. Нет ни баланса, ни CAPTCHA
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("АВТОРИЗАЦИЯ НЕ ПОДТВЕРЖДЕНА")
    print("Баланс не найден.")
    print("CAPTCHA не найдена.")
    print("=" * 60)
    print()

    return {
        "ok": False,
        "status": "AUTH_NOT_CONFIRMED",
    }


# ============================================================
# MAIN
# ============================================================

async def main():

    if not SITE_URL:

        raise RuntimeError(
            "XBET_URL отсутствует в .env"
        )

    print()
    print("=" * 60)
    print("PLAYWRIGHT AUTH")
    print("=" * 60)
    print()

    from backend.app.browser.manager import BROWSER_MANAGER

    page = await BROWSER_MANAGER.start()
    try:

            result = await authorize(page)

            print()
            print("=" * 60)
            print("RESULT")
            print("=" * 60)
            print(result)
            print("=" * 60)
            print()

            print(
                "Браузер оставлен открытым."
            )

            print(
                "Нажми ENTER в консоли для завершения."
            )

            await asyncio.to_thread(input)

    except Exception as error:

            print()
            print("=" * 60)
            print("ОШИБКА")
            print("=" * 60)
            print(error)
            print("=" * 60)
            print()

            print(
                "Браузер оставлен открытым "
                "для диагностики."
            )

            await asyncio.to_thread(input)

    finally:
        # Standalone script termination is an explicit application shutdown.
        await BROWSER_MANAGER.stop()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
