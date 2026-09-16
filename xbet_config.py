import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


def join_url(base: str, path: str) -> str:
    """Join one configured base URL and path without duplicate slashes."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _required(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"{name} is required. Configure it in .env")
    return value


def _validated_url(value: str, name: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(
            f"{name} must not contain credentials, query parameters, or fragments"
        )
    return normalized


def _validated_path(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path:
        raise RuntimeError(f"{name} must be a URL path, not an absolute URL")
    return f"/{parsed.path.lstrip('/')}".rstrip("/")


def _legacy_table_tennis_path(environ: Mapping[str, str], xbet_url: str) -> str:
    """Keep old TABLE_TENNIS_URL installations readable during migration."""
    legacy_url = str(environ.get("TABLE_TENNIS_URL", "")).strip()
    if not legacy_url:
        return ""
    legacy = urlsplit(_validated_url(legacy_url, "TABLE_TENNIS_URL"))
    base_path = urlsplit(xbet_url).path.rstrip("/")
    if base_path and legacy.path.startswith(f"{base_path}/"):
        return legacy.path[len(base_path) :]
    return legacy.path


@dataclass(frozen=True)
class UrlConfig:
    xbet_url: str
    next_goal_league_path: str
    table_tennis_path: str
    backend_host: str
    backend_port: int
    backend_cors_origins: tuple[str, ...]
    frontend_api_base_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "UrlConfig":
        source = os.environ if environ is None else environ
        xbet_url = _validated_url(_required(source, "XBET_URL"), "XBET_URL")
        next_goal_path = _validated_path(
            _required(source, "XBET_FIFA_3X3_CONFERENCE_LEAGUE_PATH"),
            "XBET_FIFA_3X3_CONFERENCE_LEAGUE_PATH",
        )
        configured_table_path = str(source.get("XBET_TABLE_TENNIS_PATH", "")).strip()
        table_path_value = configured_table_path or _legacy_table_tennis_path(
            source,
            xbet_url,
        )
        if not table_path_value:
            raise RuntimeError(
                "XBET_TABLE_TENNIS_PATH is required. Configure it in .env"
            )
        table_tennis_path = _validated_path(
            table_path_value,
            "XBET_TABLE_TENNIS_PATH",
        )
        backend_host = _required(source, "BACKEND_HOST")
        try:
            backend_port = int(_required(source, "BACKEND_PORT"))
        except ValueError as error:
            raise RuntimeError("BACKEND_PORT must be an integer") from error
        if not 1 <= backend_port <= 65535:
            raise RuntimeError("BACKEND_PORT must be between 1 and 65535")
        cors_origins = tuple(
            _validated_url(item.strip(), "BACKEND_CORS_ORIGINS")
            for item in _required(source, "BACKEND_CORS_ORIGINS").split(",")
            if item.strip()
        )
        if not cors_origins:
            raise RuntimeError("BACKEND_CORS_ORIGINS must contain at least one URL")
        frontend_api_base_url = _validated_url(
            _required(source, "VITE_API_BASE_URL"),
            "VITE_API_BASE_URL",
        )
        return cls(
            xbet_url=xbet_url,
            next_goal_league_path=next_goal_path,
            table_tennis_path=table_tennis_path,
            backend_host=backend_host,
            backend_port=backend_port,
            backend_cors_origins=cors_origins,
            frontend_api_base_url=frontend_api_base_url,
        )

    @property
    def next_goal_league_url(self) -> str:
        return join_url(self.xbet_url, self.next_goal_league_path)

    @property
    def table_tennis_url(self) -> str:
        return join_url(self.xbet_url, self.table_tennis_path)

    @property
    def table_tennis_root_path(self) -> str:
        return urlsplit(self.table_tennis_url).path.rstrip("/")

    def public_dict(self) -> dict[str, object]:
        return {
            "xbet_url": self.xbet_url,
            "next_goal_league_path": self.next_goal_league_path,
            "next_goal_league_url": self.next_goal_league_url,
            "table_tennis_path": self.table_tennis_path,
            "table_tennis_url": self.table_tennis_url,
            "backend_host": self.backend_host,
            "backend_port": self.backend_port,
            "backend_cors_origins": list(self.backend_cors_origins),
            "frontend_api_base_url": self.frontend_api_base_url,
        }


URL_CONFIG = UrlConfig.from_env()


def get_xbet_url(path: str = "") -> str:
    """Compatibility helper; all values still come from URL_CONFIG."""
    return join_url(URL_CONFIG.xbet_url, path) if path else URL_CONFIG.xbet_url


def get_table_tennis_url() -> str:
    return URL_CONFIG.table_tennis_url


def get_table_tennis_odds_poll_interval() -> float:
    """Return a bounded polling interval for the read-only odds monitor."""
    try:
        configured = float(os.getenv("TABLE_TENNIS_ODDS_POLL_INTERVAL", "1.5"))
    except ValueError:
        configured = 1.5
    return max(0.5, configured)


def get_table_tennis_min_arb_percent() -> float:
    """Return the safety margin required before the paper hedge is recorded."""
    try:
        configured = float(os.getenv("TABLE_TENNIS_MIN_ARB_PERCENT", "0.5"))
    except ValueError:
        configured = 0.5
    return max(0.0, configured)
