import asyncio
import inspect
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse


NavigationLogger = Callable[[str], Any]

NETWORK_ERROR_MARKERS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
)


class NavigationLoadError(RuntimeError):
    """Raised when the target page did not become a usable DOM document."""


def _page_diagnostics(page: Any) -> str:
    try:
        closed = page.is_closed()
    except Exception:
        closed = "unknown"
    try:
        current_url = str(page.url or "about:blank")
    except Exception:
        current_url = "<unavailable>"
    return f"current_url={current_url!r}; page_closed={closed}"


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
            """() => ({
                readyState: document.readyState,
                hasBody: Boolean(document.body),
                href: String(location.href || ''),
                title: String(document.title || ''),
                bodyText: String(document.body?.innerText || '').slice(0, 4000),
            })"""
        )
    except Exception:
        return False

    if not isinstance(state, dict):
        return False

    href = str(state.get("href") or "")
    diagnostic_text = " ".join(
        (
            href,
            str(state.get("title") or ""),
            str(state.get("bodyText") or ""),
        )
    ).upper()
    if href.lower().startswith("chrome-error://"):
        return False
    if any(marker in diagnostic_text for marker in NETWORK_ERROR_MARKERS):
        return False

    return bool(
        state.get("hasBody")
        and state.get("readyState") in {"interactive", "complete"}
    )


def _error_text_chain(error: BaseException | None) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return " ".join(parts).lower()


def is_network_transport_error(error: BaseException | None) -> bool:
    """True for socket/DNS/proxy failures where aggressive retries make things worse."""
    message = _error_text_chain(error)
    return any(marker.lower() in message for marker in NETWORK_ERROR_MARKERS)


def _is_transient_navigation_abort(error: Exception) -> bool:
    message = _error_text_chain(error)
    return (
        "err_aborted" in message
        or "frame was detached" in message
        or is_network_transport_error(error)
    )


async def _wait_for_redirected_document(
    page: Any,
    target_url: str,
    *,
    timeout_ms: int = 2_000,
    poll_ms: int = 100,
) -> bool:
    """Allow a bookmaker redirect to attach its replacement main frame."""
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        if await _document_is_usable(page, target_url):
            return True
        try:
            if page.is_closed():
                return False
            await page.wait_for_timeout(poll_ms)
        except Exception:
            await asyncio.sleep(poll_ms / 1000)
    return await _document_is_usable(page, target_url)


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
            if (
                _is_transient_navigation_abort(error)
                and await _wait_for_redirected_document(page, url)
            ):
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
            # Do not call window.stop() here. 1xBet is a heavy SPA and aborting
            # an in-flight document can leave its persistent tab half-hydrated.
            # A new page.goto() safely supersedes the previous navigation.
            network_failure = is_network_transport_error(last_error)
            delay_ms = (
                max(8_000, retry_delay_ms * attempt)
                if network_failure
                else retry_delay_ms * attempt
            )
            if network_failure:
                await _emit(
                    logger,
                    (
                        "Сетевой сбой: не штурмуем зеркало повторными запросами; "
                        f"пауза {delay_ms / 1000:.0f} сек перед одной следующей попыткой"
                    ),
                )
            try:
                await page.wait_for_timeout(delay_ms)
            except Exception:
                await asyncio.sleep(delay_ms / 1000)

    raise NavigationLoadError(
        f"Страница не загрузилась после {attempts} попыток: "
        f"{type(last_error).__name__}: {last_error}; {_page_diagnostics(page)}"
    ) from last_error
