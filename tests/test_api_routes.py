import unittest

from backend.app.main import app


class ApiRouteTests(unittest.TestCase):
    def test_demo_lifecycle_and_history_routes_are_registered(self):
        routes = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }

        self.assertIn(("/api/demo/start", "POST"), routes)
        self.assertIn(("/api/demo/stop", "POST"), routes)
        self.assertIn(("/api/demo/state", "GET"), routes)
        self.assertIn(("/api/demo/history", "GET"), routes)
        self.assertIn(("/api/demo/history", "DELETE"), routes)
        self.assertIn(("/api/demo/strategy-config", "GET"), routes)
        self.assertIn(("/api/demo/strategy-config", "PUT"), routes)
        self.assertIn(("/api/live/start", "POST"), routes)
        self.assertIn(("/api/live/stop", "POST"), routes)
        self.assertIn(("/api/live/state", "GET"), routes)
        self.assertIn(("/api/live/history", "GET"), routes)
        self.assertIn(("/api/demo/database", "DELETE"), routes)
        self.assertIn(("/api/table-tennis/scan", "POST"), routes)
        self.assertIn(("/api/table-tennis/start", "POST"), routes)
        self.assertIn(("/api/table-tennis/stop", "POST"), routes)
        self.assertIn(("/api/table-tennis/state", "GET"), routes)
        self.assertIn(("/api/table-tennis/leagues", "GET"), routes)
        self.assertIn(("/api/table-tennis/matches", "GET"), routes)


if __name__ == "__main__":
    unittest.main()
