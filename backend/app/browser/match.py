from playwright.async_api import Page

from backend.app.demo.models import ScoreboardSnapshot

from .scoreboard import read_scoreboard


class MatchBrowser:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def snapshot(self) -> ScoreboardSnapshot:
        return await read_scoreboard(self.page)
