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
