import asyncio
from contextlib import suppress
from typing import Any

from backend.app.demo.engine import ENGINE
from backend.app.demo.history import REPOSITORY, local_now
from backend.app.demo.models import DemoStatus
from backend.app.demo.state import STATE, initial_state


LIVE_UNRESOLVED_STATUSES = {
    "LIVE_MARKET_SELECTED",
    "LIVE_COUPON_OPENED",
    "LIVE_AMOUNT_FILLED",
    "LIVE_AMOUNT_VERIFIED",
    "READY_FOR_MANUAL_CONFIRMATION",
    "AWAITING_PLACEMENT_RESULT",
    "BET_PLACED",
    "ACTIVE",
}


def _is_unresolved_record(item: dict[str, Any], mode: str) -> bool:
    item_mode = str(item.get("mode") or "DEMO").upper()
    if item_mode != mode:
        return False

    result = str(item.get("result") or "").upper()
    status = str(item.get("status") or "").upper()

    if result == "ACTIVE":
        return True

    if mode == "LIVE":
        if result == "SUBMISSION_UNKNOWN":
            return True
        if result == "PENDING" and status in LIVE_UNRESOLVED_STATUSES:
            return True

    return False


async def _interrupt_unresolved_records(mode: str) -> list[dict[str, Any]]:
    interrupted: list[dict[str, Any]] = []
    now = local_now()

    for item in await REPOSITORY.history(5000, mode=mode):
        if not _is_unresolved_record(item, mode):
            continue

        previous_result = item.get("result")
        previous_status = item.get("status")
        placement_confirmed = bool(item.get("placement_confirmed_at"))

        updated = await REPOSITORY.save_bet(
            {
                **item,
                "result": "INTERRUPTED",
                "status": "STOPPED_BY_USER",
                "settled": True,
                "resolved_at": now,
                "interrupted_at": now,
                "interrupted_from_result": previous_result,
                "interrupted_from_status": previous_status,
                "interruption_reason": "USER_STOP",
                "external_bet_may_still_be_active": (
                    mode == "LIVE"
                    and (
                        placement_confirmed
                        or str(previous_status or "").upper()
                        in {"BET_PLACED", "ACTIVE", "AWAITING_PLACEMENT_RESULT"}
                    )
                ),
            }
        )
        interrupted.append(updated)

        await REPOSITORY.log(
            f"{mode}_BET_INTERRUPTED",
            (
                f"id={updated.get('id')} step={updated.get('step')} "
                f"from={previous_result}/{previous_status}"
            ),
        )

    return interrupted


async def _force_worker_down(mode: str) -> None:
    """
    First use the normal ENGINE.stop(). If Playwright/worker is stuck,
    bound the wait and force-cancel the asyncio task.
    """
    try:
        await asyncio.wait_for(ENGINE.stop(mode), timeout=3.0)
        return
    except asyncio.TimeoutError:
        await REPOSITORY.log(
            f"{mode}_STOP_TIMEOUT",
            "Normal stop exceeded 3 seconds; forcing worker cancellation",
        )

    ENGINE._stop_event.set()
    task = ENGINE.task

    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)

    async with ENGINE._control_lock:
        if ENGINE._task is task:
            ENGINE._task = None


async def stop_and_reset_session(mode: str) -> dict[str, Any]:
    """
    Explicit STOP for both DEMO and LIVE.

    The SQLite database is NOT deleted.
    Existing WIN/LOSE history is NOT touched.
    Only unresolved local records are changed to INTERRUPTED.
    The strategy sequence is reset to step 1 so the next START can begin normally.
    """
    requested_mode = mode.strip().upper()
    if requested_mode not in {"DEMO", "LIVE"}:
        raise ValueError(f"Unsupported mode: {mode}")

    await REPOSITORY.log(
        f"{requested_mode}_SESSION_STOP_REQUEST",
        f"Explicit session stop requested for {requested_mode}",
    )

    await _force_worker_down(requested_mode)

    interrupted = await _interrupt_unresolved_records(requested_mode)
    sequence = await REPOSITORY.reset_sequence()

    # Runtime-only cleanup.
    ENGINE._current_series = None
    ENGINE._pending_live_bet = None
    ENGINE._active_live_bet = None
    ENGINE.live_executor.reset()
    ENGINE._mode = requested_mode
    ENGINE._authorized_generation = -1
    ENGINE._auth_status = "UNKNOWN"

    previous_state = await STATE.snapshot()

    fresh = initial_state()
    fresh.update(
        running=False,
        mode=requested_mode,
        status=DemoStatus.STOPPED.value,
        message=(
            f"{requested_mode} сессия остановлена. "
            f"Незавершённых записей закрыто: {len(interrupted)}. Можно запускать заново."
        ),
        event=f"{requested_mode}_SESSION_STOPPED",
        browser=await ENGINE.browser_manager.snapshot(),
        auth=previous_state.get("auth") or {"status": "UNKNOWN"},
        budget=await REPOSITORY.get_budget(),
        strategy_config=await REPOSITORY.get_config(),
        sequence=sequence,
        stats=await REPOSITORY.stats(),
    )
    await STATE.update(**fresh)

    await REPOSITORY.log(
        f"{requested_mode}_SESSION_STOPPED",
        f"Session reset complete; interrupted={len(interrupted)}; next_step=1",
    )

    return await STATE.snapshot()
