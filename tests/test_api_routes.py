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
        self.assertIn(("/api/live/start", "POST"), routes)
        self.assertIn(("/api/table-tennis/scan", "POST"), routes)
        self.assertIn(("/api/table-tennis/start", "POST"), routes)
        self.assertIn(("/api/table-tennis/stop", "POST"), routes)
        self.assertIn(("/api/table-tennis/state", "GET"), routes)
        self.assertIn(("/api/table-tennis/leagues", "GET"), routes)
        self.assertIn(("/api/table-tennis/matches", "GET"), routes)
        self.assertIn(("/api/table-tennis/candidates", "GET"), routes)

if __name__ == "__main__":
    unittest.main()
