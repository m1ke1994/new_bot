import unittest

from backend.app.main import app


class ApiRouteTests(unittest.TestCase):
    def test_core_lifecycle_routes_are_registered(self):
        routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
        self.assertIn(("/api/demo/start", "POST"), routes)
        self.assertIn(("/api/live/start", "POST"), routes)
        self.assertNotIn(("/api/table-tennis/start", "POST"), routes)


if __name__ == "__main__":
    unittest.main()
