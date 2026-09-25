import unittest

from skills_workspace.api import route
from skills_workspace.ledger import LedgerService
from skills_workspace.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = LedgerService(self.database)

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "训练机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "op", "new_actor_id": "t1", "display_name": "技师",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors",
              {"request_id": "aud", "new_actor_id": "q1", "display_name": "质量",
               "role": "auditor", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "加工中心", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "a1"})

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_case_write_and_replay_over_http(self):
        self._bootstrap()
        body = {"request_id": "case-1", "site_id": "s1", "external_key": "CASE-001"}
        status, payload = route(self.service, "POST", "/cases", body, {"X-Actor-Id": "t1"})
        self.assertEqual(201, status)
        self.assertEqual("case", payload["resource_type"])
        self.assertEqual("open", payload["response"]["status"])
        status, replay = route(self.service, "POST", "/cases", body, {"X-Actor-Id": "t1"})
        self.assertEqual(200, status)
        self.assertEqual(payload["resource_id"], replay["resource_id"])
        self.assertEqual(payload["response"], replay["response"])

    def test_trace_endpoints_require_quality_actor(self):
        self._bootstrap()
        status, payload = route(self.service, "GET", "/products/trace", None,
                                {"X-Actor-Id": "q1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])
        status, payload = route(self.service, "GET", "/products/trace?product_id=missing",
                                None, {"X-Actor-Id": "q1"})
        self.assertEqual(404, status)
        status, payload = route(self.service, "GET", "/products/trace?product_id=missing",
                                None, {"X-Actor-Id": "t1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_material_batch_conflict_over_http(self):
        self._bootstrap()
        body = {"request_id": "b1", "site_id": "s1", "material_code": "ZIRCONIA",
                "lot_number": "LOT-1", "quantity": 100, "unit": "g"}
        status, _ = route(self.service, "POST", "/material-batches", body, {"X-Actor-Id": "t1"})
        self.assertEqual(201, status)
        conflict = dict(body, request_id="b2", quantity=200)
        status, payload = route(self.service, "POST", "/material-batches", conflict,
                                {"X-Actor-Id": "t1"})
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])


if __name__ == "__main__":
    unittest.main()
