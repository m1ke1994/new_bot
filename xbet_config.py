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
    backend_host: str
    backend_port: int
    backend_cors_origins: tuple[str, ...]
    frontend_api_base_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "UrlConfig":
        source = os.environ if environ is None else environ
        base_url = _validated_url(
            _required(source, "SITE_BASE_URL"),
            "SITE_BASE_URL",
        )
        login_target = _validated_target(
            _required(source, "SITE_LOGIN_URL"),
            "SITE_LOGIN_URL",
        )
        live_target = _validated_target(
            _required(source, "SITE_LIVE_URL"),
            "SITE_LIVE_URL",
        )
        next_goal_target = _validated_target(
            _required(source, "SITE_FIFA_URL"),
            "SITE_FIFA_URL",
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
    def next_goal_league_path(self) -> str:
        return urlsplit(self.next_goal_league_url).path

    def public_dict(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "login_url": self.login_url,
            "live_url": self.live_url,
            "next_goal_league_path": self.next_goal_league_path,
            "next_goal_league_url": self.next_goal_league_url,
            "backend_host": self.backend_host,
            "backend_port": self.backend_port,
            "backend_cors_origins": list(self.backend_cors_origins),
            "frontend_api_base_url": self.frontend_api_base_url,
        }


@dataclass(frozen=True)
class SelectorConfig:
    auth_marker: str
    auth_marker_fallbacks: tuple[str, ...]
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
    market_search_exact: str
    market_search_button: str
    market_search_scoped_inputs: tuple[str, ...]
    market_search_fallback_inputs: tuple[str, ...]
    market_search_ancestor_xpath: str
    market_search_button_fallbacks: tuple[str, ...]
    market_group: str
    market_group_title: str
    market_button: str
    market_locked_class: str
    canvas: str
    canvas_fallback: str
    match_content_market_surfaces: tuple[str, ...]
    scoreboard_root: str
    scoreboard_root_fallbacks: tuple[str, ...]
    scoreboard_team: str
    scoreboard_team_name: str
    scoreboard_team_fallbacks: tuple[str, ...]
    scoreboard_team_1_score: str
    scoreboard_team_1_score_fallbacks: tuple[str, ...]
    scoreboard_team_2_score: str
    scoreboard_team_2_score_fallbacks: tuple[str, ...]
    scoreboard_timer: str
    scoreboard_timer_status: str
    scoreboard_timer_fallbacks: tuple[str, ...]
    subgame_list: str
    subgame_item: str
    subgame_caption: str
    subgame_selected_class: str
    selected_subgames: tuple[str, ...]
    period_signals: tuple[str, ...]
    game_over_panel: str
    game_over_title: str
    betslip: str
    coupon_bet: str
    empty_coupon: str
    coupon_first_team: str
    coupon_second_team: str
    coupon_market: str
    coupon_remove: str
    bet_account: str
    bet_amount_input: str
    bet_amount_inputs: tuple[str, ...]
    bet_submit_button: str
    bet_submit_buttons: tuple[str, ...]
    bet_submit_fallback_buttons: tuple[str, ...]
    bet_balance: str
    locked_bet: str
    locked_bets: tuple[str, ...]
    locked_bet_text: str
    locked_bet_texts: tuple[str, ...]
    remove_bet_buttons: tuple[str, ...]
    success_modal: str
    success_modal_title: str
    success_modal_info: str
    success_modal_continue: str
    success_modal_close_buttons: tuple[str, ...]

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
            auth_marker_fallbacks=many("SELECTOR_AUTH_MARKER_FALLBACKS"),
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
            market_search_exact=one("SELECTOR_MARKET_SEARCH_EXACT"),
            market_search_button=one("SELECTOR_MARKET_SEARCH_BUTTON"),
            market_search_scoped_inputs=many("SELECTOR_MARKET_SEARCH_SCOPED_INPUTS"),
            market_search_fallback_inputs=many("SELECTOR_MARKET_SEARCH_FALLBACK_INPUTS"),
            market_search_ancestor_xpath=one("SELECTOR_MARKET_SEARCH_ANCESTOR_XPATH"),
            market_search_button_fallbacks=many("SELECTOR_MARKET_SEARCH_BUTTON_FALLBACKS"),
            market_group=one("SELECTOR_MARKET_GROUP"),
            market_group_title=one("SELECTOR_MARKET_GROUP_TITLE"),
            market_button=one("SELECTOR_MARKET_BUTTON"),
            market_locked_class=one("CLASS_MARKET_LOCKED"),
            canvas=one("SELECTOR_MARKET_CANVAS"),
            canvas_fallback=one("SELECTOR_MARKET_CANVAS_FALLBACK"),
            match_content_market_surfaces=many("SELECTOR_MATCH_CONTENT_MARKET_SURFACES"),
            scoreboard_root=one("SELECTOR_SCOREBOARD"),
            scoreboard_root_fallbacks=many("SELECTOR_SCOREBOARD_FALLBACKS"),
            scoreboard_team=one("SELECTOR_SCOREBOARD_TEAM"),
            scoreboard_team_name=one("SELECTOR_SCOREBOARD_TEAM_NAME"),
            scoreboard_team_fallbacks=many("SELECTOR_SCOREBOARD_TEAM_FALLBACKS"),
            scoreboard_team_1_score=one("SELECTOR_SCOREBOARD_TEAM_1_SCORE"),
            scoreboard_team_1_score_fallbacks=many("SELECTOR_SCOREBOARD_TEAM_1_SCORE_FALLBACKS"),
            scoreboard_team_2_score=one("SELECTOR_SCOREBOARD_TEAM_2_SCORE"),
            scoreboard_team_2_score_fallbacks=many("SELECTOR_SCOREBOARD_TEAM_2_SCORE_FALLBACKS"),
            scoreboard_timer=one("SELECTOR_TIMER"),
            scoreboard_timer_status=one("SELECTOR_SCOREBOARD_TIMER_STATUS"),
            scoreboard_timer_fallbacks=many("SELECTOR_SCOREBOARD_TIMER_FALLBACKS"),
            subgame_list=one("SELECTOR_SUBGAME_LIST"),
            subgame_item=one("SELECTOR_SUBGAME_ITEM"),
            subgame_caption=one("SELECTOR_SUBGAME_CAPTION"),
            subgame_selected_class=one("CLASS_SELECTED_SUBGAME"),
            selected_subgames=many("SELECTOR_SELECTED_SUBGAME"),
            period_signals=many("SELECTOR_PERIOD_SIGNALS"),
            game_over_panel=one("SELECTOR_GAME_OVER_PANEL"),
            game_over_title=one("SELECTOR_GAME_OVER_TITLE"),
            betslip=one("SELECTOR_BETSLIP"),
            coupon_bet=one("SELECTOR_COUPON_BET"),
            empty_coupon=one("SELECTOR_EMPTY_COUPON"),
            coupon_first_team=one("SELECTOR_COUPON_FIRST_TEAM"),
            coupon_second_team=one("SELECTOR_COUPON_SECOND_TEAM"),
            coupon_market=one("SELECTOR_COUPON_MARKET"),
            coupon_remove=one("SELECTOR_COUPON_REMOVE"),
            bet_account=one("SELECTOR_BET_ACCOUNT"),
            bet_amount_input=one("SELECTOR_BET_AMOUNT_INPUT"),
            bet_amount_inputs=many("SELECTOR_BET_AMOUNT_INPUTS"),
            bet_submit_button=one("SELECTOR_BET_SUBMIT_BUTTON"),
            bet_submit_buttons=many("SELECTOR_BET_SUBMIT_BUTTONS"),
            bet_submit_fallback_buttons=many("SELECTOR_BET_SUBMIT_FALLBACK_BUTTONS"),
            bet_balance=one("SELECTOR_BET_BALANCE"),
            locked_bet=one("SELECTOR_LOCKED_BET"),
            locked_bets=many("SELECTOR_LOCKED_BETS"),
            locked_bet_text=one("SELECTOR_LOCKED_BET_TEXT"),
            locked_bet_texts=many("SELECTOR_LOCKED_BET_TEXTS"),
            remove_bet_buttons=many("SELECTOR_REMOVE_BET_BUTTON"),
            success_modal=one("SELECTOR_SUCCESS_MODAL"),
            success_modal_title=one("SELECTOR_SUCCESS_MODAL_TITLE"),
            success_modal_info=one("SELECTOR_SUCCESS_MODAL_INFO"),
            success_modal_continue=one("SELECTOR_SUCCESS_MODAL_CONTINUE"),
            success_modal_close_buttons=many("SELECTOR_SUCCESS_MODAL_CLOSE_BUTTONS"),
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
    first_half_market: str
    draw_selection: str
    first_half_tab: str
    game_over: str
    bet_confirm: str
    locked_event: str
    bet_success_title: str
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
            first_half_market=required("TEXT_FIRST_HALF_MARKET"),
            draw_selection=required("TEXT_DRAW_SELECTION"),
            first_half_tab=required("TEXT_FIRST_HALF_TAB"),
            game_over=required("TEXT_GAME_OVER"),
            bet_confirm=required("TEXT_BET_CONFIRM"),
            locked_event=required("TEXT_LOCKED_EVENT"),
            bet_success_title=required("TEXT_BET_SUCCESS_TITLE"),
            market_response_keywords=split_config_list(
                required("TEXT_MARKET_RESPONSE_KEYWORDS")
            ),
        )


@dataclass(frozen=True)
class RuntimeConfig:
    league_name: str
    match_monitor_interval: float
    matches_front_file: str
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
