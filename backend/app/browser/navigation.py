import asyncio
import inspect
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse


NavigationLogger = Callable[[str], Any]


class NavigationLoadError(RuntimeError):
    """Raised when the target page did not become a usable DOM document."""


def _same_target(current_url: str, target_url: str) -> bool:
    """Accept the requested URL (or one of its descendants) after redirects."""
    try:
        current = urlparse(current_url)
        target = urlparse(target_url)
    except Exception:
        return False
    if not current.scheme or current.scheme == "about":
        return False
    if target.netloc and current.netloc.lower() != target.netloc.lower():
        return False
    target_path = target.path.rstrip("/") or "/"
    current_path = current.path.rstrip("/") or "/"
    if target_path == "/":
        return True
    return current_path == target_path or current_path.startswith(f"{target_path}/")


async def _emit(logger: NavigationLogger | None, message: str) -> None:
    if logger is None:
        return
    result = logger(message)
    if inspect.isawaitable(result):
        await result


async def _document_is_usable(page: Any, target_url: str) -> bool:
    try:
        if page.is_closed():
            return False
        current_url = getattr(page, "url", None)
        # Lightweight unit-test/page adapters may only implement goto(). Real
        # Playwright pages always expose url/evaluate and take the strict path.
        if current_url is None or not hasattr(page, "evaluate"):
            return True
        if not _same_target(str(current_url or ""), target_url):
            return False
        state = await page.evaluate(
            "() => ({ readyState: document.readyState, hasBody: Boolean(document.body) })"
        )
    except Exception:
        return False
    return bool(
        isinstance(state, dict)
        and state.get("hasBody")
        and state.get("readyState") in {"interactive", "complete"}
    )


async def _stop_incomplete_navigation(page: Any) -> None:
    try:
        await page.evaluate("() => window.stop()")
    except Exception:
        pass


async def goto_with_retry(
    page: Any,
    url: str,
    *,
    timeout_ms: int = 60_000,
    attempts: int = 2,
    retry_delay_ms: int = 350,
    logger: NavigationLogger | None = None,
) -> Any:
    """Navigate and verify a real DOM, recovering from an interrupted old load."""
    if not url:
        raise ValueError("URL для навигации не задан")
    if attempts < 1:
        raise ValueError("attempts должен быть не меньше 1")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if page.is_closed():
            raise NavigationLoadError("Вкладка Chromium закрыта до начала навигации")
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            if await _document_is_usable(page, url):
                return response
            last_error = NavigationLoadError(
                "Навигация завершилась без готового DOM: "
                f"current_url={getattr(page, 'url', None)!r}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A timeout can be reported after the page has actually rendered.
            # In that case continuing is safer than starting another navigation.
            if await _document_is_usable(page, url):
                return None
            last_error = error

        if attempt < attempts:
            await _emit(
                logger,
                (
                    f"Попытка загрузки {attempt}/{attempts} не завершена "
                    f"({type(last_error).__name__}: {last_error}); повторяем"
                ),
            )
            await _stop_incomplete_navigation(page)
            try:
                await page.wait_for_timeout(retry_delay_ms)
            except Exception:
                await asyncio.sleep(retry_delay_ms / 1000)

    raise NavigationLoadError(
        f"Страница не загрузилась после {attempts} попыток: {type(last_error).__name__}: {last_error}"
    ) from last_error
