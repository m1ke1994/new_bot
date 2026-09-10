import os


XBET_LEAGUE_PATH = (
    "live/fifa/2860561-fc-25-3x3-conference-league"
)
TABLE_TENNIS_PATH = "ru/live/table-tennis"


def get_xbet_url(path: str = "") -> str:
    """Build every project URL from the single XBET_URL setting."""
    base_url = os.getenv("XBET_URL", "").strip().rstrip("/")
    if not base_url or not path:
        return base_url
    return f"{base_url}/{path.lstrip('/')}"
