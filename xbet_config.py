"""Central configuration for the external bookmaker site and local runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


def _value(environ: Mapping[str, str], name: str, legacy: str = "") -> str:
    value = str(environ.get(name, "")).strip()
    if not value and legacy:
        value = str(environ.get(legacy, "")).strip()
    return value


def _required(environ: Mapping[str, str], name: str, legacy: str = "") -> str:
    value = _value(environ, name, legacy)
    if not value:
        raise RuntimeError(f"Missing required site configuration:\n{name}")
    return value


def split_config_list(value: str) -> tuple[str, ...]:
    """Split an env fallback list without treating CSS commas as separators."""
    return tuple(item.strip() for item in value.split("||") if item.strip())


def join_selectors(selectors: tuple[str, ...]) -> str:
    """Turn configured CSS fallbacks into one Playwright CSS union."""
    return ", ".join(selectors)


def join_url(base: str, path: str) -> str:
    """Resolve a configured relative site path, preserving absolute URLs."""
    parsed = urlsplit(path)
    if parsed.scheme and parsed.netloc:
        return path.rstrip("/")
    return urljoin(f"{base.rstrip('/')}/", path.lstrip("/")).rstrip("/")


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


def _validated_target(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must not contain query parameters or fragments")
    if parsed.scheme and (parsed.scheme not in {"http", "https"} or not parsed.netloc):
        raise RuntimeError(f"{name} must be an http(s) URL or a relative site path")
    if parsed.username or parsed.password:
        raise RuntimeError(f"{name} must not contain credentials")
    if not parsed.path:
        raise RuntimeError(f"{name} must not be empty")
    return value.rstrip("/")


@dataclass(frozen=True)
class UrlConfig:
    xbet_url: str
    login_target: str
    live_target: str
    next_goal_target: str
    table_tennis_target: str
    backend_host: str
    backend_port: int
    backend_cors_origins: tuple[str, ...]
    frontend_api_base_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "UrlConfig":
        source = os.environ if environ is None else environ
        base_name = "SITE_BASE_URL" if source.get("SITE_BASE_URL") else "XBET_URL"
        base_url = _validated_url(_required(source, base_name), base_name)
        next_goal_name = (
            "SITE_FIFA_URL"
            if source.get("SITE_FIFA_URL")
            else "XBET_FIFA_3X3_CONFERENCE_LEAGUE_PATH"
        )
        table_name = (
            "SITE_TABLE_TENNIS_URL"
            if source.get("SITE_TABLE_TENNIS_URL")
            else "XBET_TABLE_TENNIS_PATH"
        )
        next_goal_target = _validated_target(
            _required(source, next_goal_name), next_goal_name
        )
        table_tennis_target = _value(source, table_name)
        if not table_tennis_target:
            table_tennis_target = _value(source, "TABLE_TENNIS_URL")
        if not table_tennis_target:
            raise RuntimeError(f"Missing required site configuration:\n{table_name}")
        table_tennis_target = _validated_target(table_tennis_target, table_name)
        login_target = _validated_target(
            _value(source, "SITE_LOGIN_URL") or base_url, "SITE_LOGIN_URL"
        )
        live_target = _validated_target(
            _value(source, "SITE_LIVE_URL") or "/live", "SITE_LIVE_URL"
        )

        backend_host = _value(source, "BACKEND_HOST") or "127.0.0.1"
        try:
            backend_port = int(_value(source, "BACKEND_PORT") or "8000")
        except ValueError as error:
            raise RuntimeError("BACKEND_PORT must be an integer") from error
        if not 1 <= backend_port <= 65535:
            raise RuntimeError("BACKEND_PORT must be between 1 and 65535")
        cors_raw = _value(source, "BACKEND_CORS_ORIGINS") or (
            "http://127.0.0.1:5173,http://localhost:5173"
        )
        cors_origins = tuple(
            _validated_url(item.strip(), "BACKEND_CORS_ORIGINS")
            for item in cors_raw.split(",")
            if item.strip()
        )
        frontend_api_base_url = _validated_url(
            _value(source, "VITE_API_BASE_URL") or "http://127.0.0.1:8000",
            "VITE_API_BASE_URL",
        )
        return cls(
            xbet_url=base_url,
            login_target=login_target,
            live_target=live_target,
            next_goal_target=next_goal_target,
            table_tennis_target=table_tennis_target,
            backend_host=backend_host,
            backend_port=backend_port,
            backend_cors_origins=cors_origins,
            frontend_api_base_url=frontend_api_base_url,
        )

    @property
    def base_url(self) -> str:
        return self.xbet_url

    @property
    def login_url(self) -> str:
        return join_url(self.xbet_url, self.login_target)

    @property
    def live_url(self) -> str:
        return join_url(self.xbet_url, self.live_target)

    @property
    def next_goal_league_url(self) -> str:
        return join_url(self.xbet_url, self.next_goal_target)

    @property
    def table_tennis_url(self) -> str:
        return join_url(self.xbet_url, self.table_tennis_target)

    @property
    def next_goal_league_path(self) -> str:
        return urlsplit(self.next_goal_league_url).path

    @property
    def table_tennis_path(self) -> str:
        return urlsplit(self.table_tennis_url).path

    @property
    def table_tennis_root_path(self) -> str:
        return urlsplit(self.table_tennis_url).path.rstrip("/")

    def public_dict(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "login_url": self.login_url,
            "live_url": self.live_url,
            "next_goal_league_path": self.next_goal_league_path,
            "next_goal_league_url": self.next_goal_league_url,
            "table_tennis_path": self.table_tennis_path,
            "table_tennis_url": self.table_tennis_url,
            "backend_host": self.backend_host,
            "backend_port": self.backend_port,
            "backend_cors_origins": list(self.backend_cors_origins),
            "frontend_api_base_url": self.frontend_api_base_url,
        }


@dataclass(frozen=True)
class SelectorConfig:
    auth_marker: str
    page_body: str
    game_card: str
    match_link: str
    match_card_ancestor_xpath: str
    team_name: str
    match_time: str
    match_period: str
    score: str
    match_market: str
    market_name: str
    market_value: str
    additional_markets: tuple[str, ...]
    goals_filter: str
    goals_filter_ancestor_xpath: str
    market_search: str
    market_group: str
    market_group_title: str
    market_button: str
    market_locked_class: str
    canvas: str
    scoreboard_root: str
    scoreboard_team: str
    scoreboard_team_1_score: str
    scoreboard_team_2_score: str
    scoreboard_timer: str
    scoreboard_timer_status: str
    subgame_list: str
    subgame_item: str
    subgame_caption: str
    subgame_selected_class: str
    selected_subgames: tuple[str, ...]
    period_signals: tuple[str, ...]
    game_over_panel: str
    game_over_title: str
    betslip: str
    bet_account: str
    bet_amount_input: str
    bet_submit_button: str
    locked_bet: str
    locked_bet_text: str
    remove_bet_buttons: tuple[str, ...]
    league_item: str
    league_group: str
    league_link_template: str
    league_title: str
    league_games_count: str
    accordion_trigger: str
    market_content_item: str
    market_group_header: str
    market_group_list: str
    market_group_selection: str
    table_market_groups: tuple[str, ...]
    table_market_titles: tuple[str, ...]
    table_market_buttons: tuple[str, ...]

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        required: tuple[str, ...] = ("game_card",),
    ) -> "SelectorConfig":
        source = os.environ if environ is None else environ

        def one(name: str) -> str:
            return _value(source, name)

        def many(name: str) -> tuple[str, ...]:
            return split_config_list(_value(source, name))

        config = cls(
            auth_marker=one("SELECTOR_AUTH_MARKER"),
            page_body=one("SELECTOR_PAGE_BODY"),
            game_card=one("SELECTOR_GAME_CARD"),
            match_link=one("SELECTOR_MATCH_LINK"),
            match_card_ancestor_xpath=one("SELECTOR_MATCH_CARD_ANCESTOR_XPATH"),
            team_name=one("SELECTOR_TEAM_NAME"),
            match_time=one("SELECTOR_MATCH_TIME"),
            match_period=one("SELECTOR_MATCH_PERIOD"),
            score=one("SELECTOR_SCORE"),
            match_market=one("SELECTOR_MATCH_MARKET"),
            market_name=one("SELECTOR_MARKET_NAME"),
            market_value=one("SELECTOR_MARKET_VALUE"),
            additional_markets=many("SELECTOR_ADDITIONAL_MARKETS"),
            goals_filter=one("SELECTOR_GOALS_FILTER"),
            goals_filter_ancestor_xpath=one("SELECTOR_GOALS_FILTER_ANCESTOR_XPATH"),
            market_search=one("SELECTOR_MARKET_SEARCH"),
            market_group=one("SELECTOR_MARKET_GROUP"),
            market_group_title=one("SELECTOR_MARKET_GROUP_TITLE"),
            market_button=one("SELECTOR_MARKET_BUTTON"),
            market_locked_class=one("CLASS_MARKET_LOCKED"),
            canvas=one("SELECTOR_MARKET_CANVAS"),
            scoreboard_root=one("SELECTOR_SCOREBOARD"),
            scoreboard_team=one("SELECTOR_SCOREBOARD_TEAM"),
            scoreboard_team_1_score=one("SELECTOR_SCOREBOARD_TEAM_1_SCORE"),
            scoreboard_team_2_score=one("SELECTOR_SCOREBOARD_TEAM_2_SCORE"),
            scoreboard_timer=one("SELECTOR_TIMER"),
            scoreboard_timer_status=one("SELECTOR_SCOREBOARD_TIMER_STATUS"),
            subgame_list=one("SELECTOR_SUBGAME_LIST"),
            subgame_item=one("SELECTOR_SUBGAME_ITEM"),
            subgame_caption=one("SELECTOR_SUBGAME_CAPTION"),
            subgame_selected_class=one("CLASS_SELECTED_SUBGAME"),
            selected_subgames=many("SELECTOR_SELECTED_SUBGAME"),
            period_signals=many("SELECTOR_PERIOD_SIGNALS"),
            game_over_panel=one("SELECTOR_GAME_OVER_PANEL"),
            game_over_title=one("SELECTOR_GAME_OVER_TITLE"),
            betslip=one("SELECTOR_BETSLIP"),
            bet_account=one("SELECTOR_BET_ACCOUNT"),
            bet_amount_input=one("SELECTOR_BET_AMOUNT_INPUT"),
            bet_submit_button=one("SELECTOR_BET_SUBMIT_BUTTON"),
            locked_bet=one("SELECTOR_LOCKED_BET"),
            locked_bet_text=one("SELECTOR_LOCKED_BET_TEXT"),
            remove_bet_buttons=many("SELECTOR_REMOVE_BET_BUTTON"),
            league_item=one("SELECTOR_LEAGUE_ITEM"),
            league_group=one("SELECTOR_LEAGUE_GROUP"),
            league_link_template=one("SELECTOR_LEAGUE_LINK_TEMPLATE"),
            league_title=one("SELECTOR_LEAGUE_TITLE"),
            league_games_count=one("SELECTOR_LEAGUE_GAMES_COUNT"),
            accordion_trigger=one("SELECTOR_ACCORDION_TRIGGER"),
            market_content_item=one("SELECTOR_MARKET_CONTENT_ITEM"),
            market_group_header=one("SELECTOR_MARKET_GROUP_HEADER"),
            market_group_list=one("SELECTOR_MARKET_GROUP_LIST"),
            market_group_selection=one("SELECTOR_MARKET_GROUP_SELECTION"),
            table_market_groups=many("SELECTOR_TABLE_MARKET_GROUPS"),
            table_market_titles=many("SELECTOR_TABLE_MARKET_TITLES"),
            table_market_buttons=many("SELECTOR_TABLE_MARKET_BUTTONS"),
        )
        config.require(*required)
        return config

    def require(self, *names: str) -> None:
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            env_names = [f"SELECTOR_{name.upper()}" for name in missing]
            raise RuntimeError(
                "Missing required site configuration:\n" + "\n".join(env_names)
            )

    @property
    def loaded_count(self) -> int:
        return sum(bool(getattr(self, item.name)) for item in fields(self))


@dataclass(frozen=True)
class TextConfig:
    auth_marker: str
    goals: str
    next_goal: str
    next_goal_search: str
    total_even: str
    total_even_search: str
    affirmative_selection: str
    first_half_market: str
    draw_selection: str
    first_half_tab: str
    game_over: str
    bet_confirm: str
    locked_event: str
    main_game: str
    first_party: str
    second_party: str
    party_market_template: str
    player_one_aliases: tuple[str, ...]
    player_two_aliases: tuple[str, ...]
    market_response_keywords: tuple[str, ...]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "TextConfig":
        source = os.environ if environ is None else environ

        def required(name: str) -> str:
            return _required(source, name)

        return cls(
            auth_marker=required("TEXT_AUTH_MARKER"),
            goals=required("TEXT_GOALS_FILTER"),
            next_goal=required("TEXT_NEXT_GOAL"),
            next_goal_search=required("TEXT_NEXT_GOAL_SEARCH"),
            total_even=required("TEXT_TOTAL_EVEN"),
            total_even_search=required("TEXT_TOTAL_EVEN_SEARCH"),
            affirmative_selection=required("TEXT_AFFIRMATIVE_SELECTION"),
            first_half_market=required("TEXT_FIRST_HALF_MARKET"),
            draw_selection=required("TEXT_DRAW_SELECTION"),
            first_half_tab=required("TEXT_FIRST_HALF_TAB"),
            game_over=required("TEXT_GAME_OVER"),
            bet_confirm=required("TEXT_BET_CONFIRM"),
            locked_event=required("TEXT_LOCKED_EVENT"),
            main_game=required("TEXT_MAIN_GAME"),
            first_party=required("TEXT_FIRST_PARTY"),
            second_party=required("TEXT_SECOND_PARTY"),
            party_market_template=required("TEXT_PARTY_MARKET_TEMPLATE"),
            player_one_aliases=split_config_list(required("TEXT_PLAYER_ONE_ALIASES")),
            player_two_aliases=split_config_list(required("TEXT_PLAYER_TWO_ALIASES")),
            market_response_keywords=split_config_list(
                required("TEXT_MARKET_RESPONSE_KEYWORDS")
            ),
        )


@dataclass(frozen=True)
class RuntimeConfig:
    league_name: str
    match_monitor_interval: float
    matches_front_file: str
    table_tennis_odds_poll_interval: float
    table_tennis_min_arb_percent: float
    bet_mode: str
    score_poll_interval: float
    ocr_max_attempts: int
    ocr_retry_delay: float
    ocr_min_confidence: float
    tesseract_cmd: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RuntimeConfig":
        source = os.environ if environ is None else environ
        return cls(
            league_name=_value(source, "XBET_LEAGUE_NAME"),
            match_monitor_interval=float(_value(source, "MATCH_MONITOR_INTERVAL") or "2"),
            matches_front_file=_value(source, "MATCHES_FRONT_FILE")
            or "front/frontend/public/matches.json",
            table_tennis_odds_poll_interval=max(
                0.5, float(_value(source, "TABLE_TENNIS_ODDS_POLL_INTERVAL") or "1.5")
            ),
            table_tennis_min_arb_percent=max(
                0.0, float(_value(source, "TABLE_TENNIS_MIN_ARB_PERCENT") or "0.5")
            ),
            bet_mode=(_value(source, "BET_MODE") or "DEMO").upper(),
            score_poll_interval=float(_value(source, "SCORE_POLL_INTERVAL") or "0.20"),
            ocr_max_attempts=int(_value(source, "OCR_MAX_ATTEMPTS") or "5"),
            ocr_retry_delay=float(_value(source, "OCR_RETRY_DELAY") or "0.7"),
            ocr_min_confidence=float(_value(source, "OCR_MIN_CONFIDENCE") or "0.55"),
            tesseract_cmd=_value(source, "TESSERACT_CMD"),
        )


URL_CONFIG = UrlConfig.from_env()
SELECTORS = SelectorConfig.from_env()
TEXTS = TextConfig.from_env()
RUNTIME_CONFIG = RuntimeConfig.from_env()


def build_match_url(href: str) -> str:
    """Keep absolute match links intact and resolve relative ones from the site base."""
    return join_url(URL_CONFIG.base_url, href)


def get_xbet_url(path: str = "") -> str:
    return join_url(URL_CONFIG.base_url, path) if path else URL_CONFIG.base_url


def get_table_tennis_url() -> str:
    return URL_CONFIG.table_tennis_url


def get_table_tennis_odds_poll_interval() -> float:
    return RUNTIME_CONFIG.table_tennis_odds_poll_interval


def get_table_tennis_min_arb_percent() -> float:
    return RUNTIME_CONFIG.table_tennis_min_arb_percent
