import unittest
from pathlib import Path

from auth import SITE_URL
from backend.app.demo.config import CONFIG as DEMO_CONFIG
from backend.app.routes.browser import browser_config_check
from matches import MATCHES_URL
from xbet_config import URL_CONFIG, UrlConfig, join_url


def environment(**changes: str) -> dict[str, str]:
    values = {
        "XBET_URL": "https://test-bookmaker.example/ru",
        "XBET_FIFA_3X3_CONFERENCE_LEAGUE_PATH": "/live/fifa/test-league",
        "XBET_TABLE_TENNIS_PATH": "/live/table-tennis",
        "BACKEND_HOST": "127.0.0.1",
        "BACKEND_PORT": "8000",
        "BACKEND_CORS_ORIGINS": "http://127.0.0.1:5173,http://localhost:5173",
        "VITE_API_BASE_URL": "http://127.0.0.1:8000",
    }
    values.update(changes)
    return values


class UrlConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_next_goal_url_is_built_from_env_base_and_path(self):
        config = UrlConfig.from_env(environment())

        self.assertEqual(
            config.next_goal_league_url,
            "https://test-bookmaker.example/ru/live/fifa/test-league",
        )

    def test_join_url_normalizes_both_slashes(self):
        self.assertEqual(
            join_url("https://test-bookmaker.example/ru/", "/live/fifa/test-league"),
            "https://test-bookmaker.example/ru/live/fifa/test-league",
        )

    def test_changing_only_xbet_url_rebases_every_bookmaker_page(self):
        config = UrlConfig.from_env(
            environment(XBET_URL="https://new-mirror.example/ru")
        )

        self.assertEqual(
            config.next_goal_league_url,
            "https://new-mirror.example/ru/live/fifa/test-league",
        )
        self.assertEqual(
            config.table_tennis_url,
            "https://new-mirror.example/ru/live/table-tennis",
        )

    def test_existing_consumers_share_the_central_config(self):
        self.assertEqual(SITE_URL, URL_CONFIG.xbet_url)
        self.assertEqual(MATCHES_URL, URL_CONFIG.next_goal_league_url)
        self.assertEqual(DEMO_CONFIG.league_url, URL_CONFIG.next_goal_league_url)

    def test_required_url_has_no_hidden_production_fallback(self):
        values = environment()
        values.pop("XBET_URL")

        with self.assertRaisesRegex(
            RuntimeError,
            r"Missing required site configuration:\s*XBET_URL",
        ):
            UrlConfig.from_env(values)

    def test_public_urls_reject_embedded_secrets(self):
        with self.assertRaisesRegex(RuntimeError, "must not contain credentials"):
            UrlConfig.from_env(
                environment(XBET_URL="https://user:secret@test-bookmaker.example/ru")
            )

        with self.assertRaisesRegex(RuntimeError, "must not contain credentials"):
            UrlConfig.from_env(
                environment(VITE_API_BASE_URL="https://api.example.test?token=secret")
            )

    async def test_config_check_exposes_urls_but_no_secrets(self):
        payload = await browser_config_check()
        keys = {str(key).casefold() for key in payload}

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["next_goal_league_url"], URL_CONFIG.next_goal_league_url)
        # Public navigation URLs (including login_url) are intentional diagnostics.
        # Credentials themselves must never be exposed by this endpoint.
        self.assertFalse(any("password" in key for key in keys))
        self.assertFalse(any("token" in key for key in keys))
        self.assertNotIn("xbet_login", keys)
        self.assertNotIn("username", keys)

    def test_production_sources_contain_no_bookmaker_domains(self):
        root = Path(__file__).resolve().parents[1]
        production_files = [
            *root.glob("*.py"),
            *root.glob("backend/**/*.py"),
            *root.glob("front/frontend/src/**/*.js"),
            *root.glob("front/frontend/src/**/*.vue"),
            root / "front" / "frontend" / "vite.config.js",
        ]
        forbidden = ("1xbet-start.cc", "1xlite-02216.pro", "1xbet-lowg.top")
        violations = []
        for path in production_files:
            source = path.read_text(encoding="utf-8")
            if any(domain in source for domain in forbidden):
                violations.append(str(path.relative_to(root)))

        self.assertEqual(violations, [])

    def test_frontend_uses_central_vite_api_base(self):
        root = Path(__file__).resolve().parents[1]
        config_source = (root / "front/frontend/src/config.js").read_text(
            encoding="utf-8"
        )
        app_source = (root / "front/frontend/src/App.vue").read_text(
            encoding="utf-8"
        )
        forks_source = (
            root / "front/frontend/src/views/ForksView.vue"
        ).read_text(encoding="utf-8")

        self.assertIn("import.meta.env.VITE_API_BASE_URL", config_source)
        self.assertIn("fetch(apiUrl(path)", app_source)
        self.assertIn("fetch(apiUrl(path)", forks_source)
        self.assertNotIn("127.0.0.1", config_source + app_source + forks_source)


if __name__ == "__main__":
    unittest.main()
