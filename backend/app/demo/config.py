from dataclasses import dataclass
from pathlib import Path

from xbet_config import RUNTIME_CONFIG, URL_CONFIG


ROOT_DIR = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DemoConfig:
    mode: str = RUNTIME_CONFIG.bet_mode
    league_url: str = URL_CONFIG.next_goal_league_url
    league_name: str = RUNTIME_CONFIG.league_name
    score_poll_interval: float = RUNTIME_CONFIG.score_poll_interval
    league_retry_interval: float = RUNTIME_CONFIG.match_monitor_interval
    ocr_max_attempts: int = RUNTIME_CONFIG.ocr_max_attempts
    ocr_retry_delay: float = RUNTIME_CONFIG.ocr_retry_delay
    ocr_min_confidence: float = RUNTIME_CONFIG.ocr_min_confidence
    data_dir: Path = ROOT_DIR / "backend" / "data"

    def validate(self) -> None:
        if self.mode != "DEMO":
            raise RuntimeError(
                "Режим REAL не реализован и заблокирован. Установите BET_MODE=DEMO."
            )
        if self.score_poll_interval < 0.1:
            raise RuntimeError("SCORE_POLL_INTERVAL не может быть меньше 0.1 сек.")
        if self.ocr_max_attempts < 1:
            raise RuntimeError("OCR_MAX_ATTEMPTS должен быть положительным.")


CONFIG = DemoConfig()
