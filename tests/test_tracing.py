import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, PermissionDenied, ValidationError
from skills_workspace.storage import Database
from skills_workspace.tracing import TracingService

PRESCRIPTION = {"restoration": "crown", "tooth_fdi": "26",
                "dimensions_mm": {"occlusal_gap": 1.5}, "material": {"code": "zirconia"}}
ROUTE = [{"code": "design", "station_id": "st-design"},
         {"code": "mill", "station_id": "st-mill"},
         {"code": "tryin", "station_id": "st-tryin"}]


class TracingTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = TracingService(self.database,
                                      FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap", organization_id="o1", name="机构")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in (
                ("op1", "designer", "设计", "operator"),
                ("op2", "miller", "切削", "operator"),
                ("op3", "fitter", "试戴", "operator"),
                ("rv1", "quality", "质量", "reviewer"),
                ("au1", "auditor", "审计", "auditor")):
            s.register_actor(request_id=rid, actor_id="a1", new_actor_id=aid,
                             display_name=name, role=role, organization_id="o1")
        s.register_site(request_id="site", actor_id="a1", site_id="s1", organization_id="o1",
                        name="车间", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def create_case(self, request_id="case-1"):
        receipt = self.service.create_case(
            request_id=request_id, actor_id="designer", site_id="s1",
            subject_code=f"subj-{request_id}", prescription=PRESCRIPTION, route=ROUTE)
        ledger = self.service.get_case_ledger(actor_id="quality", case_id=receipt.resource_id)
        return receipt.resource_id, ledger.branches[0]["branch_id"], ledger.products[0]["product_id"]

    def confirm_first(self, case_id, branch_id, *, from_actor="designer", to_actor="miller", seq=1):
        self.service.start_operation(request_id=f"start-{case_id}-{seq}", actor_id=from_actor,
                                     case_id=case_id, branch_id=branch_id, seq=seq)
        hand = self.service.propose_handoff(
            request_id=f"hand-{case_id}-{seq}", actor_id=from_actor, case_id=case_id,
            branch_id=branch_id, seq=seq, to_station_id=f"st-{['design','mill','tryin'][seq % 3]}",
            to_actor_id=to_actor, package={"v": 1})
        self.service.respond_handoff(request_id=f"resp-{case_id}-{seq}", actor_id=to_actor,
                                     handoff_id=hand.resource_id, decision="confirm")
        return hand.resource_id


class GateTest(TracingTestBase):
    def test_operation_cannot_start_before_predecessor_confirmed(self):
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        with self.assertRaises(ConflictError):
            self.service.start_operation(request_id="start-2", actor_id="miller",
                                         case_id=case_id, branch_id=branch_id, seq=2)

    def test_operation_starts_after_confirmation_and_closes_handoff(self):
        case_id, branch_id, _ = self.create_case()
        handoff_id = self.confirm_first(case_id, branch_id)
        self.service.start_operation(request_id="start-2", actor_id="miller",
                                     case_id=case_id, branch_id=branch_id, seq=2)
        ledger = self.service.get_case_ledger(actor_id="quality", case_id=case_id)
        handoff = next(h for h in ledger.handoffs if h["handoff_id"] == handoff_id)
        self.assertEqual("closed", handoff["state"])

    def test_open_discrepancy_blocks_downstream(self):
        case_id, branch_id, _ = self.create_case()
        self.confirm_first(case_id, branch_id)
        self.service.start_operation(request_id="start-2", actor_id="miller",
                                     case_id=case_id, branch_id=branch_id, seq=2)
        hand = self.service.propose_handoff(
            request_id="hand-2", actor_id="miller", case_id=case_id, branch_id=branch_id, seq=2,
            to_station_id="st-tryin", to_actor_id="fitter", package={"v": 1})
        self.service.respond_handoff(request_id="disp-2", actor_id="fitter",
                                     handoff_id=hand.resource_id, decision="dispute", reason="尺寸不符")
        # 差异解决但尚未确认交接，仍不能开始下一道。
        discrepancy = self.service.get_case_ledger(actor_id="quality", case_id=case_id).discrepancies[0]
        self.service.resolve_discrepancy(request_id="resolve", actor_id="quality",
                                         discrepancy_id=discrepancy["discrepancy_id"],
                                         resolution="已整改", approve=True)
        with self.assertRaises(ConflictError):
            self.service.start_operation(request_id="start-3", actor_id="fitter",
                                         case_id=case_id, branch_id=branch_id, seq=3)
        self.service.respond_handoff(request_id="resp-2", actor_id="fitter",
                                     handoff_id=hand.resource_id, decision="confirm")
        self.service.start_operation(request_id="start-3", actor_id="fitter",
                                     case_id=case_id, branch_id=branch_id, seq=3)


class HandoffTest(TracingTestBase):
    def test_duplicate_handoff_rejected_but_request_replays(self):
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        kwargs = dict(actor_id="designer", case_id=case_id, branch_id=branch_id, seq=1,
                      to_station_id="st-mill", to_actor_id="miller", package={"v": 1})
        first = self.service.propose_handoff(request_id="h1", **kwargs)
        with self.assertRaises(ConflictError):
            self.service.propose_handoff(request_id="h2", **kwargs)
        replay = self.service.propose_handoff(request_id="h1", **kwargs)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)

    def test_late_message_with_same_message_id_is_idempotent(self):
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        kwargs = dict(actor_id="designer", case_id=case_id, branch_id=branch_id, seq=1,
                      to_station_id="st-mill", to_actor_id="miller", package={"v": 1},
                      message_id="msg-1")
        first = self.service.propose_handoff(request_id="h1", **kwargs)
        late = self.service.propose_handoff(request_id="h1-late", **kwargs)
        self.assertTrue(late.replayed)
        self.assertEqual(first.resource_id, late.resource_id)

    def test_terminal_late_confirmation_does_not_change_state(self):
        case_id, branch_id, _ = self.create_case()
        handoff_id = self.confirm_first(case_id, branch_id)
        with self.assertRaises(ConflictError):
            self.service.respond_handoff(request_id="late", actor_id="miller",
                                         handoff_id=handoff_id, decision="confirm")
        # 原 request_id 仍稳定回放。
        replay = self.service.respond_handoff(request_id=f"resp-{case_id}-1", actor_id="miller",
                                              handoff_id=handoff_id, decision="confirm")
        self.assertTrue(replay.replayed)

    def test_only_named_receiver_may_respond(self):
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        hand = self.service.propose_handoff(
            request_id="h1", actor_id="designer", case_id=case_id, branch_id=branch_id, seq=1,
            to_station_id="st-mill", to_actor_id="miller", package={"v": 1})
        with self.assertRaises(PermissionDenied):
            self.service.respond_handoff(request_id="wrong", actor_id="fitter",
                                         handoff_id=hand.resource_id, decision="confirm")


class MaterialTest(TracingTestBase):
    def test_split_and_consume_conserve_quantity(self):
        self.service.register_material_lot(request_id="lot", actor_id="a1", site_id="s1",
                                           lot_id="L1", material_code="zirconia", qty="10")
        self.service.split_material(request_id="split", actor_id="a1", parent_lot_id="L1",
                                    qty="4", child_lot_id="L1-a")
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        self.service.consume_material(request_id="use", actor_id="designer", lot_id="L1-a",
                                      case_id=case_id, branch_id=branch_id, seq=1, qty="1.25")
        root = self.service.material_balance(actor_id="quality", lot_id="L1")
        child = self.service.material_balance(actor_id="quality", lot_id="L1-a")
        self.assertTrue(root["balanced"])
        self.assertTrue(child["balanced"])
        self.assertEqual("6.0000", root["remaining_qty"])
        self.assertEqual("2.7500", child["remaining_qty"])

    def test_over_consumption_rejected_and_conserves(self):
        self.service.register_material_lot(request_id="lot", actor_id="a1", site_id="s1",
                                           lot_id="L1", material_code="zirconia", qty="2")
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        with self.assertRaises(ConflictError):
            self.service.consume_material(request_id="use", actor_id="designer", lot_id="L1",
                                          case_id=case_id, branch_id=branch_id, seq=1, qty="3")
        self.assertEqual("2.0000",
                         self.service.material_balance(actor_id="quality", lot_id="L1")["remaining_qty"])

    def test_concurrent_demand_serialized_never_overshoots(self):
        self.service.register_material_lot(request_id="lot", actor_id="a1", site_id="s1",
                                           lot_id="L1", material_code="zirconia", qty="10")
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        outcomes: list[str] = []

        def consume(tag: str) -> None:
            try:
                self.service.consume_material(
                    request_id=f"use-{tag}", actor_id="designer", lot_id="L1",
                    case_id=case_id, branch_id=branch_id, seq=1, qty="6")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=consume, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])
        balance = self.service.material_balance(actor_id="quality", lot_id="L1")
        self.assertTrue(balance["balanced"])
        self.assertEqual("4.0000", balance["remaining_qty"])

    def test_too_many_decimal_places_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.register_material_lot(request_id="lot", actor_id="a1", site_id="s1",
                                               lot_id="L1", material_code="zirconia", qty="1.00001")


class RevisionAndReworkTest(TracingTestBase):
    def _finish_chain(self, case_id, branch_id, product_id):
        self.confirm_first(case_id, branch_id)
        self.service.start_operation(request_id="start-2", actor_id="miller",
                                     case_id=case_id, branch_id=branch_id, seq=2)
        hand = self.service.propose_handoff(
            request_id="hand-2", actor_id="miller", case_id=case_id, branch_id=branch_id, seq=2,
            to_station_id="st-tryin", to_actor_id="fitter", package={"v": 1})
        self.service.respond_handoff(request_id="resp-2", actor_id="fitter",
                                     handoff_id=hand.resource_id, decision="confirm")
        self.service.start_operation(request_id="start-3", actor_id="fitter",
                                     case_id=case_id, branch_id=branch_id, seq=3)
        hand3 = self.service.propose_handoff(
            request_id="hand-3", actor_id="fitter", case_id=case_id, branch_id=branch_id, seq=3,
            to_station_id="st-design", to_actor_id="designer", package={"v": 1})
        self.service.respond_handoff(request_id="resp-3", actor_id="designer",
                                     handoff_id=hand3.resource_id, decision="confirm")
        self.service.finish_product(request_id="finish", actor_id="quality", product_id=product_id)

    def test_revision_reevaluates_pending_but_keeps_records(self):
        case_id, branch_id, _ = self.create_case()
        self.confirm_first(case_id, branch_id)
        self.service.start_operation(request_id="start-2", actor_id="miller",
                                     case_id=case_id, branch_id=branch_id, seq=2)
        revised = {**PRESCRIPTION, "dimensions_mm": {"occlusal_gap": 1.8}}
        receipt = self.service.revise_prescription(
            request_id="revise", actor_id="quality", case_id=case_id,
            prescription=revised, reason="临床调改")
        self.assertFalse(receipt.replayed)
        ledger = self.service.get_case_ledger(actor_id="quality", case_id=case_id)
        ops = {op["seq"]: op for route in ledger.routes for op in route["operations"]}
        self.assertEqual(1, ops[2]["prescription_version"])  # 进行中的加工记录不改写
        self.assertEqual("active", ops[2]["status"])
        self.assertEqual(2, ops[3]["prescription_version"])  # 未开始的工序重估
        self.assertEqual("done", ops[1]["status"])            # 已完成记录不改写
        self.assertEqual(1, ops[1]["prescription_version"])

    def test_revision_supersedes_unconfirmed_handoff(self):
        case_id, branch_id, _ = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        self.service.propose_handoff(
            request_id="h1", actor_id="designer", case_id=case_id, branch_id=branch_id, seq=1,
            to_station_id="st-mill", to_actor_id="miller", package={"v": 1})
        self.service.revise_prescription(request_id="revise", actor_id="quality", case_id=case_id,
                                         prescription={**PRESCRIPTION, "note": "x"}, reason="改单")
        handoff = self.service.get_case_ledger(actor_id="quality", case_id=case_id).handoffs[0]
        self.assertEqual("superseded", handoff["state"])

    def test_rework_creates_new_branch_and_preserves_product(self):
        case_id, branch_id, product_id = self.create_case()
        self._finish_chain(case_id, branch_id, product_id)
        rework = self.service.rework(
            request_id="rework", actor_id="quality", case_id=case_id, reason="边缘不密合",
            source_product_id=product_id,
            route=[{"code": "adjust", "station_id": "st-mill"}])
        ledger = self.service.get_case_ledger(actor_id="quality", case_id=case_id)
        self.assertEqual(2, len(ledger.branches))
        statuses = {p["product_id"]: p["status"] for p in ledger.products}
        self.assertEqual("finished", statuses[product_id])  # 原成品不被覆盖
        new_product = next(p for p in ledger.products if p["branch_id"] == rework.resource_id)
        trace = self.service.trace_product(actor_id="quality", product_id=new_product["product_id"])
        self.assertEqual(product_id, trace.lineage[0]["product_id"])
        self.assertEqual("边缘不密合", trace.lineage[0]["reason"])
        self.assertEqual(rework.resource_id, trace.branch["branch_id"])

    def test_prescription_must_be_desensitized(self):
        with self.assertRaises(ValidationError):
            self.service.create_case(
                request_id="case-pii", actor_id="designer", site_id="s1", subject_code="subj-pii",
                prescription={**PRESCRIPTION, "patient_name": "张三"}, route=ROUTE)
        with self.assertRaises(ValidationError):
            self.service.create_case(
                request_id="case-pii2", actor_id="designer", site_id="s1", subject_code="subj-pii2",
                prescription={"contact": {"Phone": "123"}}, route=ROUTE)


class TraceTest(TracingTestBase):
    def test_trace_reports_inputs_handlers_material_and_differences(self):
        self.service.register_material_lot(request_id="lot", actor_id="a1", site_id="s1",
                                           lot_id="L1", material_code="zirconia", qty="5")
        case_id, branch_id, product_id = self.create_case()
        self.service.start_operation(request_id="start-1", actor_id="designer",
                                     case_id=case_id, branch_id=branch_id, seq=1)
        self.service.consume_material(request_id="use", actor_id="designer", lot_id="L1",
                                      case_id=case_id, branch_id=branch_id, seq=1, qty="2")
        hand = self.service.propose_handoff(
            request_id="h1", actor_id="designer", case_id=case_id, branch_id=branch_id, seq=1,
            to_station_id="st-mill", to_actor_id="miller", package={"v": 1})
        self.service.respond_handoff(request_id="r1", actor_id="miller",
                                     handoff_id=hand.resource_id, decision="confirm")
        trace = self.service.trace_product(actor_id="auditor", product_id=product_id)
        kinds = {item["kind"] for item in trace.inputs}
        self.assertIn("handoff", kinds)
        self.assertIn("consumption", kinds)
        technicians = {h["technician_id"] for h in trace.product["station_responsibility"]}
        self.assertIn("designer", technicians)
        self.assertIn("miller", technicians)
        remaining = {m["lot_id"]: m["remaining_qty"] for m in trace.remaining_materials}
        self.assertEqual("3.0000", remaining["L1"])
        self.assertEqual(1, trace.prescription["versions"][0]["version"])

    def test_auditor_cannot_mutate(self):
        case_id, branch_id, _ = self.create_case()
        with self.assertRaises(PermissionDenied):
            self.service.start_operation(request_id="start-1", actor_id="auditor",
                                         case_id=case_id, branch_id=branch_id, seq=1)


if __name__ == "__main__":
    unittest.main()
