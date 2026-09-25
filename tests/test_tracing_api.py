import unittest

from skills_workspace.api import route
from skills_workspace.storage import Database
from skills_workspace.tracing import TracingService


class TracingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = TracingService(self.database)
        self.call(201, "POST", "/organizations",
                  {"request_id": "org", "organization_id": "o1", "name": "机构"}, "bootstrap")
        self.call(201, "POST", "/actors",
                  {"request_id": "adm", "new_actor_id": "a1", "display_name": "管理员",
                   "role": "admin", "organization_id": "o1"}, "bootstrap")
        self.call(201, "POST", "/actors",
                  {"request_id": "op1", "new_actor_id": "designer", "display_name": "设计",
                   "role": "operator", "organization_id": "o1"}, "a1")
        self.call(201, "POST", "/actors",
                  {"request_id": "op2", "new_actor_id": "miller", "display_name": "切削",
                   "role": "operator", "organization_id": "o1"}, "a1")
        self.call(201, "POST", "/actors",
                  {"request_id": "rv1", "new_actor_id": "quality", "display_name": "质量",
                   "role": "reviewer", "organization_id": "o1"}, "a1")
        self.call(201, "POST", "/sites",
                  {"request_id": "site", "site_id": "s1", "organization_id": "o1",
                   "name": "车间", "timezone_name": "Asia/Shanghai"}, "a1")

    def tearDown(self):
        self.database.close()

    def call(self, expected_status, method, path, body=None, actor="a1"):
        status, payload = route(self.service, method, path, body, {"X-Actor-Id": actor})
        self.assertEqual(expected_status, status, payload)
        return payload

    def _case(self):
        payload = self.call(201, "POST", "/cases", {
            "request_id": "case-1", "site_id": "s1", "subject_code": "subj-1",
            "prescription": {"tooth_fdi": "16", "dimensions_mm": {"gap": 1.5}},
            "route": [{"code": "design", "station_id": "st-design"},
                      {"code": "mill", "station_id": "st-mill"}]}, "designer")
        return payload["resource_id"]

    def test_full_handoff_chain_over_http(self):
        case_id = self._case()
        ledger = self.call(200, "GET", f"/cases/{case_id}", actor="quality")
        branch_id = ledger["branches"][0]["branch_id"]

        self.call(201, "POST", "/operations/start",
                  {"request_id": "start-1", "case_id": case_id, "branch_id": branch_id, "seq": 1},
                  "designer")
        hand = self.call(201, "POST", "/handoffs", {
            "request_id": "h1", "case_id": case_id, "branch_id": branch_id, "seq": 1,
            "to_station_id": "st-mill", "to_actor_id": "miller", "package": {"gap": 1.5}}, "designer")
        handoff_id = hand["resource_id"]

        # 未确认前，下一道不能开始。
        status, payload = route(self.service, "POST", "/operations/start",
                                {"request_id": "start-2", "case_id": case_id,
                                 "branch_id": branch_id, "seq": 2}, {"X-Actor-Id": "miller"})
        self.assertEqual(409, status)

        self.call(201, "POST", f"/handoffs/{handoff_id}/respond",
                  {"request_id": "r1", "decision": "confirm"}, "miller")
        self.call(201, "POST", "/operations/start",
                  {"request_id": "start-2", "case_id": case_id, "branch_id": branch_id, "seq": 2},
                  "miller")

    def test_desensitization_rejected_over_http(self):
        status, payload = route(self.service, "POST", "/cases", {
            "request_id": "case-pii", "site_id": "s1", "subject_code": "subj-pii",
            "prescription": {"patient_name": "张三"},
            "route": [{"code": "design", "station_id": "st-design"}]},
            {"X-Actor-Id": "designer"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_material_balance_endpoint(self):
        self.call(201, "POST", "/material-lots",
                  {"request_id": "lot-1", "site_id": "s1", "lot_id": "L1",
                   "material_code": "zirconia", "qty": "8"}, "a1")
        payload = self.call(200, "GET", "/material-lots/L1/balance", actor="quality")
        self.assertTrue(payload["balanced"])
        self.assertEqual("8.0000", payload["remaining_qty"])


if __name__ == "__main__":
    unittest.main()
