import threading
import unittest
from datetime import datetime, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.ledger import LedgerService
from skills_workspace.storage import Database


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = LedgerService(self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="训练机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, name, role in [
            ("op1", "t1", "设计技师", "operator"),
            ("op2", "t2", "加工技师", "operator"),
            ("op3", "t3", "试戴技师", "operator"),
            ("rev", "q1", "质量复核", "reviewer"),
            ("aud", "q2", "质量追溯", "auditor"),
        ]:
            self.service.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                        display_name=name, role=role, organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="加工中心", timezone_name="Asia/Shanghai")
        self.case_id = self.service.register_case(
            request_id="case", actor_id="t1", site_id="s1",
            external_key="CASE-001", note="脱敏病例").resource_id
        self.version_id = self.service.register_prescription_version(
            request_id="rx1", actor_id="t1", case_id=self.case_id,
            content={"prosthesis": "单冠", "shade": "A2"},
            dimensions={"近远中径_mm": 10.2, "预备间隙_mm": 1.5}).resource_id

    def tearDown(self):
        self.database.close()

    def _route(self, request_id="route", steps=None):
        receipt = self.service.create_route(
            request_id=request_id, actor_id="t1", case_id=self.case_id,
            steps=steps or [{"station": "设计", "responsible_id": "t1"},
                            {"station": "加工", "responsible_id": "t2"},
                            {"station": "试戴", "responsible_id": "t3"}])
        return receipt.resource_id, receipt.response["step_ids"]

    def _finish_step(self, request_id, actor_id, step_id, output):
        self.service.start_step(request_id=f"{request_id}-start", actor_id=actor_id, step_id=step_id)
        return self.service.complete_step(request_id=f"{request_id}-done", actor_id=actor_id,
                                          step_id=step_id, output=output)

    # ---- 病例与处方版本 ----

    def test_case_registration_replays_and_conflicts(self):
        replay = self.service.register_case(request_id="case", actor_id="t1", site_id="s1",
                                            external_key="CASE-001", note="脱敏病例")
        self.assertTrue(replay.replayed)
        self.assertEqual(self.case_id, replay.resource_id)
        duplicate = self.service.register_case(request_id="case-2", actor_id="t1", site_id="s1",
                                               external_key="CASE-001", note="脱敏病例")
        self.assertEqual(self.case_id, duplicate.resource_id)
        with self.assertRaises(ConflictError):
            self.service.register_case(request_id="case-3", actor_id="t1", site_id="s1",
                                       external_key="CASE-001", note="不同内容")

    def test_prescription_versions_supersede_and_dedup(self):
        second = self.service.register_prescription_version(
            request_id="rx2", actor_id="t1", case_id=self.case_id,
            content={"prosthesis": "单冠", "shade": "A3"},
            dimensions={"近远中径_mm": 10.4, "预备间隙_mm": 1.5})
        self.assertEqual(2, second.response["version_no"])
        duplicate = self.service.register_prescription_version(
            request_id="rx2-again", actor_id="t1", case_id=self.case_id,
            content={"prosthesis": "单冠", "shade": "A3"},
            dimensions={"近远中径_mm": 10.4, "预备间隙_mm": 1.5})
        self.assertEqual(second.resource_id, duplicate.resource_id)
        trace = self.service.trace_case(actor_id="q2", case_id=self.case_id)
        statuses = {item["version_no"]: item["status"] for item in trace["prescription_versions"]}
        self.assertEqual({1: "superseded", 2: "active"}, statuses)

    def test_route_requires_active_prescription(self):
        case_id = self.service.register_case(request_id="case-x", actor_id="t1", site_id="s1",
                                             external_key="CASE-002").resource_id
        with self.assertRaises(ValidationError):
            self.service.create_route(request_id="route-x", actor_id="t1", case_id=case_id,
                                      steps=[{"station": "设计", "responsible_id": "t1"}])

    # ---- 工序与交接门控 ----

    def test_step_requires_confirmed_predecessor(self):
        _, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        with self.assertRaises(ConflictError):
            self.service.start_step(request_id="s2-start", actor_id="t2", step_id=steps[1])
        handover_id = self.service.initiate_handover(
            request_id="h1", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"}).resource_id
        with self.assertRaises(ConflictError):
            self.service.start_step(request_id="s2-start-2", actor_id="t2", step_id=steps[1])
        self.service.confirm_handover(request_id="h1-confirm", actor_id="t2", handover_id=handover_id)
        started = self.service.start_step(request_id="s2-start-3", actor_id="t2", step_id=steps[1])
        self.assertEqual("in_progress", started.response["status"])

    def test_handover_dispute_resolve_confirm_cycle(self):
        _, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        handover_id = self.service.initiate_handover(
            request_id="h1", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"}).resource_id
        discrepancy_id = self.service.raise_discrepancy(
            request_id="d1", actor_id="t2", handover_id=handover_id, detail="尺寸记录缺失").resource_id
        with self.assertRaises(ConflictError):
            self.service.confirm_handover(request_id="h1-confirm", actor_id="t2",
                                          handover_id=handover_id)
        self.service.resolve_discrepancy(request_id="d1-resolve", actor_id="q1",
                                         discrepancy_id=discrepancy_id, resolution="补录后放行")
        confirmed = self.service.confirm_handover(request_id="h1-confirm-2", actor_id="t2",
                                                  handover_id=handover_id)
        self.assertEqual("confirmed", confirmed.response["status"])

    def test_duplicate_handover_is_stable(self):
        _, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        first = self.service.initiate_handover(
            request_id="h1", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"})
        again = self.service.initiate_handover(
            request_id="h1-again", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"})
        self.assertEqual(first.resource_id, again.resource_id)
        self.assertTrue(again.response["already"])
        with self.assertRaises(ConflictError):
            self.service.initiate_handover(
                request_id="h1-diff", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
                version_ref={"设计版本": "v2"})

    def test_late_messages_after_terminal_state_are_stable(self):
        _, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        handover_id = self.service.initiate_handover(
            request_id="h1", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"}).resource_id
        self.service.confirm_handover(request_id="h1-confirm", actor_id="t2", handover_id=handover_id)
        late_confirm = self.service.confirm_handover(request_id="h1-confirm-late", actor_id="t2",
                                                     handover_id=handover_id)
        self.assertTrue(late_confirm.response["already"])
        with self.assertRaises(ConflictError):
            self.service.raise_discrepancy(request_id="d-late", actor_id="t2",
                                           handover_id=handover_id, detail="迟到的差异")
        with self.assertRaises(ConflictError):
            self.service.start_step(request_id="s1-start-late", actor_id="t1", step_id=steps[0])

    def test_complete_step_twice_keeps_original_record(self):
        _, steps = self._route()
        self.service.start_step(request_id="s1-start", actor_id="t1", step_id=steps[0])
        self.service.complete_step(request_id="s1-done", actor_id="t1", step_id=steps[0],
                                   output={"间隙复核": "合格"})
        same = self.service.complete_step(request_id="s1-done-2", actor_id="t1", step_id=steps[0],
                                          output={"间隙复核": "合格"})
        self.assertTrue(same.response["already"])
        with self.assertRaises(ConflictError):
            self.service.complete_step(request_id="s1-done-3", actor_id="t1", step_id=steps[0],
                                       output={"间隙复核": "不合格"})

    def test_handover_permissions(self):
        _, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        with self.assertRaises(PermissionDenied):
            self.service.initiate_handover(request_id="h1", actor_id="t2", from_step_id=steps[0],
                                           to_step_id=steps[1], version_ref={"设计版本": "v1"})
        handover_id = self.service.initiate_handover(
            request_id="h1-ok", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"}).resource_id
        with self.assertRaises(PermissionDenied):
            self.service.confirm_handover(request_id="h1-c", actor_id="t3", handover_id=handover_id)

    # ---- 成品、返修与处方修订 ----

    def _completed_product(self):
        route_id, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        handover_1 = self.service.initiate_handover(
            request_id="h1", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"}).resource_id
        self.service.confirm_handover(request_id="h1-c", actor_id="t2", handover_id=handover_1)
        self._finish_step("s2", "t2", steps[1], {"加工批次": "M-01"})
        handover_2 = self.service.initiate_handover(
            request_id="h2", actor_id="t2", from_step_id=steps[1], to_step_id=steps[2],
            version_ref={"加工批次": "M-01"}).resource_id
        self.service.confirm_handover(request_id="h2-c", actor_id="t3", handover_id=handover_2)
        done = self._finish_step("s3", "t3", steps[2], {"试戴结论": "通过"})
        return route_id, steps, done.response["product_id"]

    def test_route_completion_creates_product(self):
        _, _, product_id = self._completed_product()
        trace = self.service.trace_product(actor_id="q2", product_id=product_id)
        self.assertEqual("completed", trace["route"]["status"])
        self.assertEqual(self.version_id, trace["prescription_version"]["version_id"])
        self.assertEqual(3, len(trace["input_versions"]))
        self.assertEqual("prescription_version", trace["input_versions"][0]["source"])
        self.assertEqual("handover", trace["input_versions"][1]["source"])
        handler_ids = {item["actor_id"] for item in trace["handlers"]}
        self.assertEqual({"t1", "t2", "t3"}, handler_ids)

    def test_rework_creates_branch_without_overwriting_product(self):
        _, _, product_id = self._completed_product()
        with self.assertRaises(ValidationError):
            self.service.create_route(request_id="rw-no-reason", actor_id="t1", case_id=self.case_id,
                                      steps=[{"station": "返修", "responsible_id": "t1"}],
                                      rework_of_product_id=product_id)
        rework = self.service.create_route(
            request_id="rw", actor_id="t1", case_id=self.case_id,
            steps=[{"station": "返修加工", "responsible_id": "t2"}],
            rework_of_product_id=product_id, rework_reason="邻接面偏松")
        trace = self.service.trace_product(actor_id="q2", product_id=product_id)
        self.assertEqual("completed", trace["route"]["status"])
        self.assertEqual(1, len(trace["rework_branches"]))
        self.assertEqual(rework.resource_id, trace["rework_branches"][0]["route_id"])
        self.assertEqual("邻接面偏松", trace["rework_branches"][0]["rework_reason"])

    def test_prescription_revision_reevaluates_only_planned_routes(self):
        planned_id, planned_steps = self._route("route-planned")
        started_id, started_steps = self._route(
            "route-started",
            steps=[{"station": "设计", "responsible_id": "t1"}])
        self.service.start_step(request_id="ss", actor_id="t1", step_id=started_steps[0])
        revision = self.service.register_prescription_version(
            request_id="rx2", actor_id="t1", case_id=self.case_id,
            content={"prosthesis": "单冠", "shade": "A3"},
            dimensions={"近远中径_mm": 10.4, "预备间隙_mm": 1.6})
        self.assertEqual([planned_id], revision.response["reevaluated_routes"])
        trace = self.service.trace_case(actor_id="q2", case_id=self.case_id)
        statuses = {route["route_id"]: route["status"] for route in trace["routes"]}
        self.assertEqual("reevaluated", statuses[planned_id])
        self.assertEqual("in_progress", statuses[started_id])
        with self.assertRaises(ConflictError):
            self.service.start_step(request_id="sp", actor_id="t1", step_id=planned_steps[0])
        continued = self.service.complete_step(request_id="ss-done", actor_id="t1",
                                               step_id=started_steps[0], output={"设计版本": "v1"})
        self.assertEqual("completed", continued.response["status"])

    # ---- 材料批次 ----

    def _batch(self, quantity=100):
        return self.service.register_material_batch(
            request_id=f"batch-{quantity}", actor_id="t2", site_id="s1",
            material_code="ZIRCONIA", lot_number=f"LOT-{quantity}", quantity=quantity,
            unit="g").resource_id

    def test_material_split_conserves_quantity(self):
        batch_id = self._batch(100)
        split = self.service.split_material_batch(request_id="split", actor_id="t2",
                                                  batch_id=batch_id, allocations=[30, 20])
        self.assertEqual("50", split.response["remaining_quantity"])
        trace = self.service.trace_material(actor_id="q2", batch_id=batch_id)
        self.assertTrue(trace["conserved"])
        self.assertEqual(3, len(trace["nodes"]))
        with self.assertRaises(ConflictError):
            self.service.split_material_batch(request_id="split-over", actor_id="t2",
                                              batch_id=batch_id, allocations=[60])

    def test_duplicate_split_returns_same_children(self):
        batch_id = self._batch(100)
        first = self.service.split_material_batch(request_id="split", actor_id="t2",
                                                  batch_id=batch_id, allocations=[30, 20])
        again = self.service.split_material_batch(request_id="split-again", actor_id="t2",
                                                  batch_id=batch_id, allocations=[30, 20])
        self.assertEqual(first.response["children"], again.response["children"])
        self.assertTrue(again.response["already"])

    def test_consume_material_rules(self):
        batch_id = self._batch(50)
        _, steps = self._route()
        with self.assertRaises(ConflictError):
            self.service.consume_material(request_id="c0", actor_id="t2", batch_id=batch_id,
                                          case_id=self.case_id, quantity=5, step_id=steps[0])
        self.service.start_step(request_id="s1-start", actor_id="t1", step_id=steps[0])
        consumed = self.service.consume_material(request_id="c1", actor_id="t2", batch_id=batch_id,
                                                 case_id=self.case_id, quantity=12.5,
                                                 step_id=steps[0])
        self.assertEqual("37.5", consumed.response["remaining_quantity"])
        with self.assertRaises(ConflictError):
            self.service.consume_material(request_id="c2", actor_id="t2", batch_id=batch_id,
                                          case_id=self.case_id, quantity=40, step_id=steps[0])
        with self.assertRaises(PermissionDenied):
            self.service.consume_material(request_id="c3", actor_id="q2", batch_id=batch_id,
                                          case_id=self.case_id, quantity=1)
        trace = self.service.trace_material(actor_id="q2", batch_id=batch_id)
        self.assertTrue(trace["conserved"])
        self.assertEqual(1, len(trace["consumptions"]))

    def test_concurrent_consumption_has_stable_result(self):
        batch_id = self._batch(10)
        outcomes = []

        def claim(request_id):
            try:
                self.service.consume_material(request_id=request_id, actor_id="t2",
                                              batch_id=batch_id, case_id=self.case_id, quantity=7)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        barrier = threading.Barrier(2)

        def run(request_id):
            barrier.wait()
            claim(request_id)

        threads = [threading.Thread(target=run, args=(f"cc-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict", "ok"], sorted(outcomes))
        trace = self.service.trace_material(actor_id="q2", batch_id=batch_id)
        self.assertTrue(trace["conserved"])
        self.assertEqual("3", trace["nodes"][0]["remaining_quantity"])

    # ---- 质量追溯 ----

    def test_trace_product_reports_materials_and_discrepancies(self):
        _, steps = self._route()
        self._finish_step("s1", "t1", steps[0], {"设计版本": "v1"})
        handover_id = self.service.initiate_handover(
            request_id="h1", actor_id="t1", from_step_id=steps[0], to_step_id=steps[1],
            version_ref={"设计版本": "v1"}).resource_id
        discrepancy_id = self.service.raise_discrepancy(
            request_id="d1", actor_id="t2", handover_id=handover_id, detail="尺寸记录缺失").resource_id
        batch_id = self._batch(80)
        self.service.consume_material(request_id="c1", actor_id="t2", batch_id=batch_id,
                                      case_id=self.case_id, quantity=20)
        self.service.resolve_discrepancy(request_id="d1-r", actor_id="q1",
                                         discrepancy_id=discrepancy_id, resolution="补录后放行")
        self.service.confirm_handover(request_id="h1-c", actor_id="t2", handover_id=handover_id)
        self._finish_step("s2", "t2", steps[1], {"加工批次": "M-01"})
        handover_2 = self.service.initiate_handover(
            request_id="h2", actor_id="t2", from_step_id=steps[1], to_step_id=steps[2],
            version_ref={"加工批次": "M-01"}).resource_id
        self.service.confirm_handover(request_id="h2-c", actor_id="t3", handover_id=handover_2)
        done = self._finish_step("s3", "t3", steps[2], {"试戴结论": "通过"})
        trace = self.service.trace_product(actor_id="q2", product_id=done.response["product_id"])
        self.assertEqual([], trace["open_discrepancies"])
        self.assertEqual(1, len(trace["materials"]))
        self.assertEqual("60", trace["materials"][0]["batch_remaining_quantity"])
        listed = self.service.list_discrepancies(actor_id="q1", site_id="s1", status="resolved")
        self.assertEqual(1, len(listed))

    def test_trace_requires_quality_role(self):
        _, _, product_id = self._completed_product()
        with self.assertRaises(PermissionDenied):
            self.service.trace_product(actor_id="t1", product_id=product_id)
        with self.assertRaises(NotFoundError):
            self.service.trace_product(actor_id="q2", product_id="missing")

    def test_request_replay_returns_identical_response(self):
        _, steps = self._route()
        self.service.start_step(request_id="s1-start", actor_id="t1", step_id=steps[0])
        first = self.service.complete_step(request_id="s1-done", actor_id="t1", step_id=steps[0],
                                           output={"间隙复核": "合格"})
        replay = self.service.complete_step(request_id="s1-done", actor_id="t1", step_id=steps[0],
                                            output={"间隙复核": "合格"})
        self.assertTrue(replay.replayed)
        self.assertEqual(first.response, replay.response)

    def test_audit_chain_stays_valid(self):
        self._completed_product()
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
