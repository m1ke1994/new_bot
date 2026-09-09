import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .budget import DEMO_START_BUDGET
from .config import CONFIG
from .strategy import DEFAULT_STRATEGY_CONFIG, StrategyConfig


def local_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class DemoRepository:
    """SQLite-backed source of truth for configuration, series, money and history."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or CONFIG.data_dir
        self.database_file = self.data_dir / "demo.sqlite3"
        self._lock = asyncio.Lock()
        self._ready = False

    @contextmanager
    def _connection(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        if self._ready:
            return
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS strategy_config (
                    id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1), initial_budget TEXT NOT NULL, current_budget TEXT NOT NULL, total_pnl TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sequence_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1), sequence_id TEXT NOT NULL, current_step INTEGER NOT NULL, status TEXT NOT NULL,
                    cumulative_pnl TEXT NOT NULL, cumulative_losses TEXT NOT NULL, current_match_id TEXT, selected_team TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bet_history (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, settled_at TEXT);
                CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, timestamp TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, timestamp TEXT NOT NULL);
            """)
            now = local_now()
            db.execute("INSERT OR IGNORE INTO strategy_config VALUES (1, ?, ?, ?)", (json.dumps(DEFAULT_STRATEGY_CONFIG.to_dict()), now, now))
            db.execute("INSERT OR IGNORE INTO budget_state VALUES (1, ?, ?, ?, ?)", (str(DEMO_START_BUDGET), str(DEMO_START_BUDGET), "0.00", now))
            db.execute("INSERT OR IGNORE INTO sequence_state VALUES (1, ?, 1, 'WAITING_FOR_MATCH', '0.00', '0.00', NULL, NULL, ?)", (uuid4().hex, now))
        self._ready = True

    async def initialize(self) -> None:
        async with self._lock:
            self._initialize_sync()

    async def get_config(self) -> dict[str, Any]:
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                row = db.execute("SELECT payload, created_at, updated_at FROM strategy_config WHERE id = 1").fetchone()
        result = StrategyConfig.from_payload(json.loads(row["payload"])).to_dict()
        result.update(created_at=row["created_at"], updated_at=row["updated_at"])
        return result

    async def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = StrategyConfig.from_payload(payload)
        await self.initialize()
        now = local_now()
        async with self._lock:
            with self._connection() as db:
                created = db.execute("SELECT created_at FROM strategy_config WHERE id=1").fetchone()["created_at"]
                db.execute("UPDATE strategy_config SET payload=?, updated_at=? WHERE id=1", (json.dumps(config.to_dict()), now))
        result = config.to_dict()
        result.update(created_at=created, updated_at=now)
        return result

    async def get_budget(self) -> dict[str, Any]:
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                row = db.execute("SELECT * FROM budget_state WHERE id=1").fetchone()
        return {"initial_budget": float(row["initial_budget"]), "current_budget": float(row["current_budget"]), "session_profit": float(row["total_pnl"]), "total_pnl": float(row["total_pnl"]), "updated_at": row["updated_at"]}

    async def save_budget(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        now = local_now()
        async with self._lock:
            with self._connection() as db:
                db.execute("UPDATE budget_state SET initial_budget=?, current_budget=?, total_pnl=?, updated_at=? WHERE id=1", (str(snapshot["initial_budget"]), str(snapshot["current_budget"]), str(snapshot["session_profit"]), now))
        return await self.get_budget()

    async def get_sequence(self) -> dict[str, Any]:
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                row = db.execute("SELECT * FROM sequence_state WHERE id=1").fetchone()
        return dict(row)

    async def save_sequence(self, **changes: Any) -> dict[str, Any]:
        current = await self.get_sequence()
        current.update(changes, updated_at=local_now())
        columns = ("sequence_id", "current_step", "status", "cumulative_pnl", "cumulative_losses", "current_match_id", "selected_team", "updated_at")
        async with self._lock:
            with self._connection() as db:
                db.execute(f"UPDATE sequence_state SET {', '.join(f'{column}=?' for column in columns)} WHERE id=1", tuple(current[column] for column in columns))
        return await self.get_sequence()

    async def reset_sequence(self) -> dict[str, Any]:
        return await self.save_sequence(sequence_id=uuid4().hex, current_step=1, status="WAITING_FOR_MATCH", cumulative_pnl="0.00", cumulative_losses="0.00", current_match_id=None, selected_team=None)

    async def save_bet(self, item: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        now = local_now()
        record = {"timestamp": item.get("resolved_at") or item.get("created_at") or now, "created_at": item.get("created_at") or now, **item}
        bet_id = record.get("id") or uuid4().hex
        record["id"] = bet_id
        async with self._lock:
            with self._connection() as db:
                existing = db.execute("SELECT payload FROM bet_history WHERE id=?", (bet_id,)).fetchone()
                if existing:
                    record = {**json.loads(existing["payload"]), **record}
                db.execute("INSERT INTO bet_history(id,payload,created_at,settled_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, settled_at=excluded.settled_at", (bet_id, json.dumps(record, ensure_ascii=False), record["created_at"], record.get("resolved_at")))
        return record

    async def add_bet(self, item: dict[str, Any]) -> dict[str, Any]:
        return await self.save_bet(item)

    async def discard_unaccepted_bet(self, bet_id: str) -> bool:
        """Remove one confirmed-unaccepted technical LIVE attempt."""
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                row = db.execute(
                    "SELECT payload FROM bet_history WHERE id=?",
                    (bet_id,),
                ).fetchone()
                if row is None:
                    return False
                result = str(json.loads(row["payload"]).get("result") or "")
                if result != "NOT_PLACED":
                    raise ValueError(
                        f"Cannot discard accepted or unresolved bet {bet_id}: {result}"
                    )
                cursor = db.execute("DELETE FROM bet_history WHERE id=?", (bet_id,))
        return cursor.rowcount > 0

    async def history(self, limit: int = 500, mode: str | None = None) -> list[dict[str, Any]]:
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                rows = db.execute("SELECT payload FROM bet_history ORDER BY created_at DESC").fetchall()
        items = [json.loads(row["payload"]) for row in rows]
        if mode is not None:
            expected_mode = mode.strip().upper()
            items = [
                item
                for item in items
                if str(item.get("mode") or "DEMO").upper() == expected_mode
            ]
        return list(reversed(items[:limit]))

    async def active_bet(self, mode: str = "DEMO") -> dict[str, Any] | None:
        expected_mode = mode.strip().upper()
        for item in reversed(await self.history(5000, mode=expected_mode)):
            if item.get("result") == "ACTIVE":
                return item
        return None

    async def verified_active_live_bet(self) -> dict[str, Any] | None:
        for item in reversed(await self.history(5000, mode="LIVE")):
            if item.get("result") == "ACTIVE" and item.get("placement_confirmed_at"):
                return item
        return None

    async def unresolved_live_submission(self) -> dict[str, Any] | None:
        unresolved_statuses = {
            "LIVE_MARKET_SELECTED",
            "LIVE_COUPON_OPENED",
            "LIVE_AMOUNT_FILLED",
            "LIVE_AMOUNT_VERIFIED",
            "READY_FOR_MANUAL_CONFIRMATION",
            "AWAITING_PLACEMENT_RESULT",
            "BET_PLACED",
            "ACTIVE",
        }
        for item in reversed(await self.history(5000, mode="LIVE")):
            if item.get("result") == "SUBMISSION_UNKNOWN":
                return item
            if (
                item.get("result") == "PENDING"
                and item.get("status") in unresolved_statuses
            ):
                return item
        return None

    async def clear_bet_history(self, mode: str = "DEMO") -> int:
        """Delete only bet rows for one executor mode; keep config, budget and logs."""
        expected_mode = mode.strip().upper()
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                rows = db.execute("SELECT id, payload FROM bet_history").fetchall()
                bet_ids = [
                    row["id"]
                    for row in rows
                    if str(json.loads(row["payload"]).get("mode") or "DEMO").upper()
                    == expected_mode
                ]
                if bet_ids:
                    db.executemany("DELETE FROM bet_history WHERE id=?", ((bet_id,) for bet_id in bet_ids))
        return len(bet_ids)

    async def reset_database(self) -> None:
        """Reset all DEMO persistence while keeping the SQLite schema in place."""
        await self.initialize()
        now = local_now()
        async with self._lock:
            with self._connection() as db:
                db.execute("DELETE FROM bet_history")
                db.execute("DELETE FROM logs")
                db.execute("DELETE FROM cycles")
                db.execute("DELETE FROM strategy_config")
                db.execute("DELETE FROM budget_state")
                db.execute("DELETE FROM sequence_state")
                db.execute(
                    "INSERT INTO strategy_config VALUES (1, ?, ?, ?)",
                    (json.dumps(DEFAULT_STRATEGY_CONFIG.to_dict()), now, now),
                )
                db.execute(
                    "INSERT INTO budget_state VALUES (1, ?, ?, ?, ?)",
                    (str(DEMO_START_BUDGET), str(DEMO_START_BUDGET), "0.00", now),
                )
                db.execute(
                    "INSERT INTO sequence_state VALUES (1, ?, 1, 'WAITING_FOR_MATCH', '0.00', '0.00', NULL, NULL, ?)",
                    (uuid4().hex, now),
                )

    async def log(self, event: str, message: str) -> dict[str, Any]:
        await self.initialize()
        record = {"timestamp": local_now(), "time": datetime.now().astimezone().strftime("%H:%M:%S"), "event": event, "message": message}
        async with self._lock:
            with self._connection() as db:
                db.execute("INSERT INTO logs(payload,timestamp) VALUES(?,?)", (json.dumps(record, ensure_ascii=False), record["timestamp"]))
        print(f"[{record['time']}] {event}: {message}")
        return record

    async def logs(self, limit: int = 500) -> list[dict[str, Any]]:
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                rows = db.execute("SELECT payload FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return list(reversed([json.loads(row["payload"]) for row in rows]))

    async def add_cycle(self, item: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        record = {"timestamp": local_now(), **item}
        async with self._lock:
            with self._connection() as db:
                db.execute("INSERT INTO cycles(payload,timestamp) VALUES(?,?)", (json.dumps(record, ensure_ascii=False), record["timestamp"]))
        return record

    async def stats(self) -> dict[str, Any]:
        bets, cycles = await self.history(5000, mode="DEMO"), []
        await self.initialize()
        async with self._lock:
            with self._connection() as db:
                cycles = [json.loads(row["payload"]) for row in db.execute("SELECT payload FROM cycles").fetchall()]
        cycles = [item for item in cycles if str(item.get("mode") or "DEMO").upper() == "DEMO"]
        settled = [item for item in bets if item.get("result") in {"WIN", "LOSE"}]
        odds = [float(item["odds"]) for item in settled if item.get("odds") is not None]
        wins = [item for item in settled if item["result"] == "WIN"]
        return {"matches_processed": len(cycles), "bets": len(bets), "wins": len(wins), "losses": sum(item["result"] == "LOSE" for item in settled), "total_amount": round(sum(float(item.get("amount") or 0) for item in settled), 2), "average_odds": round(sum(odds) / len(odds), 3) if odds else None, "max_step": max((int(item.get("step") or 0) for item in bets), default=0), "average_steps_to_win": round(sum(int(item.get("steps") or 0) for item in cycles if item.get("result") == "WIN") / len(wins), 2) if wins else None}


REPOSITORY = DemoRepository()
