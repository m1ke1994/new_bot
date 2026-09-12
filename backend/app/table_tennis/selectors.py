"""Stable table-tennis DOM selectors. Vue data-v-* attributes are forbidden."""

LEAGUE_ITEM_SELECTOR = ".dashboard-champ-item"
LEAGUE_GROUP_SELECTOR = ".dashboard-champ-group-item"
LEAGUE_LINK_SELECTOR = (
    '.dashboard-champ-item a.dashboard-champ-item-template__link[href*="/ru/live/table-tennis/"], '
    '.dashboard-champ-group-item a.dashboard-champ-item-template__link[href*="/ru/live/table-tennis/"]'
)
LEAGUE_TITLE_SELECTOR = ".dashboard-champ-item-template-title__title"
LEAGUE_GAMES_COUNT_SELECTOR = ".dashboard-champ-games-count"
ACCORDION_TRIGGER_SELECTOR = ".ui-accordion__trigger"

MATCH_CARD_SELECTOR = "article.ui-game-card"
MATCH_LINK_SELECTOR = "a.ui-game-card__link"
PLAYER_SELECTOR = ".ui-game-card-scoreboard__name"
SCORE_SELECTOR = ".ui-game-card-scoreboard__score"
PERIOD_SELECTOR = ".ui-game-card__period"
TIME_SELECTOR = ".ui-game-card__data"
MARKET_SELECTOR = "button.game-card-market"
MARKET_NAME_SELECTOR = ".ui-market__name"
MARKET_VALUE_SELECTOR = ".ui-market__value"

SUB_GAMES_LIST_SELECTOR = ".game-sub-games__list"
SUB_GAME_ITEM_SELECTOR = ".game-sub-games__item"
SELECTED_SUB_GAME_SELECTOR = (
    ".game-sub-games__item:is("
    ".game-sub-games__item--is-selected, "
    ".game-sub-games__item--active, "
    ".active, .selected, [aria-selected=\"true\"]"
    ")"
)
CAPTION_SELECTOR = ".ui-caption"

MARKET_CONTENT_ITEM_SELECTOR = ".game-markets-content__item"
MARKET_GROUP_SELECTOR = ".game-markets-group"
MARKET_GROUP_HEADER_SELECTOR = ".game-markets-group__header"
MARKET_GROUP_TITLE_SELECTOR = ".game-markets-group-header-title"
MARKET_GROUP_LIST_SELECTOR = ".game-markets-group__list"
MARKET_GROUP_SELECTION_SELECTOR = ".game-markets-group__market, .market"
