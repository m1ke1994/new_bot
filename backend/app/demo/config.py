import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from xbet_config import get_xbet_url, XBET_LEAGUE_PATH


ROOT_DIR = Path(__file__).resolve().parents[3]
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class DemoConfig:
    mode: str = os.getenv("BET_MODE", "DEMO").strip().upper()
    league_url: str = get_xbet_url(XBET_LEAGUE_PATH)
    league_name: str = os.getenv(
        "XBET_LEAGUE_NAME",
        "FC 25. 3x3. Лига Конференций",
    ).strip()
    score_poll_interval: float = float(
        os.getenv("SCORE_POLL_INTERVAL", "0.20")
    )
    league_retry_interval: float = float(
        os.getenv("MATCH_MONITOR_INTERVAL", "2")
    )
    ocr_max_attempts: int = int(os.getenv("OCR_MAX_ATTEMPTS", "5"))
    ocr_retry_delay: float = float(os.getenv("OCR_RETRY_DELAY", "0.7"))
    ocr_min_confidence: float = float(os.getenv("OCR_MIN_CONFIDENCE", "0.55"))
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
