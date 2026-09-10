import os


XBET_LEAGUE_PATH = (
    "live/fifa/2860561-fc-25-3x3-conference-league"
)


def get_xbet_url(path: str = "") -> str:
    """Build every project URL from the single XBET_URL setting."""
    base_url = os.getenv("XBET_URL", "").strip().rstrip("/")
    if not base_url or not path:
        return base_url
    return f"{base_url}/{path.lstrip('/')}"


def get_table_tennis_url() -> str:
    """Return the dedicated table-tennis page configured in .env."""
    return os.getenv("TABLE_TENNIS_URL", "").strip().rstrip("/")
