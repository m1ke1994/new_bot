import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CONFIG


def local_now() -> str:
    return datetime.now().astimezone().isoformat()


class DemoRepository:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or CONFIG.data_dir
        self.history_file = self.data_dir / "demo_history.jsonl"
        self.logs_file = self.data_dir / "demo_logs.jsonl"
        self.cycles_file = self.data_dir / "demo_cycles.jsonl"
        self._lock = asyncio.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        result = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(raw_line))
            except (json.JSONDecodeError, TypeError):
                continue
        return result

    @staticmethod
    def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = "".join(
            json.dumps(item, ensure_ascii=False) + "\n" for item in items
        )
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)

    async def add_bet(self, item: dict[str, Any]) -> dict[str, Any]:
        return await self.save_bet(item)

    async def save_bet(self, item: dict[str, Any]) -> dict[str, Any]:
        """Create or update one journal row without duplicating a DEMO bet."""
        now = local_now()
        record = {
            "timestamp": item.get("resolved_at") or item.get("created_at") or now,
            "created_at": item.get("created_at") or now,
            **item,
        }
        async with self._lock:
            bets = self._read_jsonl(self.history_file)
            bet_id = record.get("id")
            existing_index = next(
                (
                    index
                    for index, existing in enumerate(bets)
                    if bet_id is not None and existing.get("id") == bet_id
                ),
                None,
            )
            if existing_index is None:
                bets.append(record)
            else:
                record = {**bets[existing_index], **record}
                bets[existing_index] = record
            self._write_jsonl(self.history_file, bets)
        return record

    async def add_cycle(self, item: dict[str, Any]) -> dict[str, Any]:
        record = {"timestamp": local_now(), **item}
        async with self._lock:
            self._append_jsonl(self.cycles_file, record)
        return record

    async def clear_session(self) -> None:
        """Start a clean DEMO session without touching unrelated diagnostics."""
        async with self._lock:
            self._write_jsonl(self.history_file, [])
            self._write_jsonl(self.logs_file, [])
            self._write_jsonl(self.cycles_file, [])

    async def log(self, event: str, message: str) -> dict[str, Any]:
        record = {
            "timestamp": local_now(),
            "time": datetime.now().astimezone().strftime("%H:%M:%S"),
            "event": event,
            "message": message,
        }
        async with self._lock:
            self._append_jsonl(self.logs_file, record)
        print(f"[{record['time']}] {event}: {message}")
        return record

    async def history(self, limit: int = 500) -> list[dict[str, Any]]:
        async with self._lock:
            return self._read_jsonl(self.history_file)[-limit:]

    async def logs(self, limit: int = 500) -> list[dict[str, Any]]:
        async with self._lock:
            return self._read_jsonl(self.logs_file)[-limit:]

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            bets = self._read_jsonl(self.history_file)
            cycles = self._read_jsonl(self.cycles_file)

        odds = [float(item["odds"]) for item in bets if item.get("odds") is not None]
        won_cycles = [item for item in cycles if item.get("result") == "WIN"]
        steps_to_win = [int(item["steps"]) for item in won_cycles if item.get("steps")]

        return {
            "matches_processed": len(cycles),
            "bets": len(bets),
            "wins": sum(item.get("result") == "WIN" for item in bets),
            "losses": sum(item.get("result") == "LOSE" for item in bets),
            "total_amount": sum(float(item.get("amount") or 0) for item in bets),
            "average_odds": round(sum(odds) / len(odds), 3) if odds else None,
            "max_step": max((int(item.get("step") or 0) for item in bets), default=0),
            "average_steps_to_win": (
                round(sum(steps_to_win) / len(steps_to_win), 2)
                if steps_to_win
                else None
            ),
        }


REPOSITORY = DemoRepository()
