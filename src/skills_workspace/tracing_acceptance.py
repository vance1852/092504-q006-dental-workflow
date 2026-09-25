"""运行病例工序与材料追溯账本的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .storage import Database
from .tracing import TracingService


def _bootstrap(service: TracingService) -> None:
    service.register_organization(request_id="acceptance-org", actor_id="bootstrap",
                                  organization_id="org-001", name="口腔修复训练机构")
    service.register_actor(request_id="acceptance-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    for request_id, actor_id, name, role in (
            ("acceptance-designer", "tech-designer", "设计技师", "operator"),
            ("acceptance-miller", "tech-miller", "切削技师", "operator"),
            ("acceptance-fitter", "tech-fitter", "试戴技师", "operator"),
            ("acceptance-quality", "quality-001", "质量人员", "reviewer")):
        service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=actor_id,
                               display_name=name, role=role, organization_id="org-001")
    service.register_site(request_id="acceptance-site", actor_id="admin-001", site_id="site-001",
                          organization_id="org-001", name="修复加工车间", timezone_name="Asia/Shanghai")


def run() -> dict[str, object]:
    """执行建病例、交接、领料、差异、返修与追溯的完整链。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "tracing_acceptance.sqlite3")
        service = TracingService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        _bootstrap(service)

        case = service.create_case(
            request_id="acceptance-case", actor_id="tech-designer", site_id="site-001",
            subject_code="case-2026-0001",
            prescription={"restoration": "crown", "tooth_fdi": "16",
                          "dimensions_mm": {"occlusal_gap": 1.5, "margin": 0.8},
                          "material": {"code": "zirconia", "shade": "A2"}},
            route=[{"code": "design", "station_id": "station-design"},
                   {"code": "mill", "station_id": "station-mill"},
                   {"code": "tryin", "station_id": "station-tryin"}])
        case_id = case.resource_id
        ledger = service.get_case_ledger(actor_id="quality-001", case_id=case_id)
        branch_id = ledger.branches[0]["branch_id"]
        original_product = ledger.products[0]["product_id"]

        service.register_material_lot(request_id="acceptance-lot", actor_id="admin-001",
                                      site_id="site-001", lot_id="lot-root",
                                      material_code="zirconia-A2", qty="12")
        service.split_material(request_id="acceptance-split", actor_id="admin-001",
                               parent_lot_id="lot-root", qty="4", child_lot_id="lot-work")

        # 设计 -> 切削：正常确认。
        service.start_operation(request_id="acceptance-start-1", actor_id="tech-designer",
                                case_id=case_id, branch_id=branch_id, seq=1)
        service.consume_material(request_id="acceptance-use-1", actor_id="tech-designer",
                                 lot_id="lot-work", case_id=case_id, branch_id=branch_id, seq=1, qty="0.25")
        handoff_1 = service.propose_handoff(
            request_id="acceptance-hand-1", actor_id="tech-designer", case_id=case_id,
            branch_id=branch_id, seq=1, to_station_id="station-mill", to_actor_id="tech-miller",
            package={"prescription_version": 1, "occlusal_gap_mm": 1.5}, message_id="acceptance-msg-1")
        service.respond_handoff(request_id="acceptance-resp-1", actor_id="tech-miller",
                                handoff_id=handoff_1.resource_id, decision="confirm")

        # 切削 -> 试戴：接收方提出差异，解决后确认。
        service.start_operation(request_id="acceptance-start-2", actor_id="tech-miller",
                                case_id=case_id, branch_id=branch_id, seq=2)
        service.consume_material(request_id="acceptance-use-2", actor_id="tech-miller",
                                 lot_id="lot-work", case_id=case_id, branch_id=branch_id, seq=2, qty="1.75")
        handoff_2 = service.propose_handoff(
            request_id="acceptance-hand-2", actor_id="tech-miller", case_id=case_id,
            branch_id=branch_id, seq=2, to_station_id="station-tryin", to_actor_id="tech-fitter",
            package={"prescription_version": 1, "occlusal_gap_mm": 1.2})
        dispute = service.respond_handoff(
            request_id="acceptance-disp-2", actor_id="tech-fitter",
            handoff_id=handoff_2.resource_id, decision="dispute", reason="咬合间隙小于处方要求",
            expected={"occlusal_gap_mm": 1.5}, actual={"occlusal_gap_mm": 1.2})
        service.resolve_discrepancy(request_id="acceptance-resolve-2", actor_id="quality-001",
                                    discrepancy_id=dispute.resource_id, resolution="返工补瓷后复验",
                                    approve=True)
        service.respond_handoff(request_id="acceptance-resp-2", actor_id="tech-fitter",
                                handoff_id=handoff_2.resource_id, decision="confirm")

        service.start_operation(request_id="acceptance-start-3", actor_id="tech-fitter",
                                case_id=case_id, branch_id=branch_id, seq=3)
        handoff_3 = service.propose_handoff(
            request_id="acceptance-hand-3", actor_id="tech-fitter", case_id=case_id,
            branch_id=branch_id, seq=3, to_station_id="station-design", to_actor_id="tech-designer",
            package={"prescription_version": 1, "fit": "accepted"})
        service.respond_handoff(request_id="acceptance-resp-3", actor_id="tech-designer",
                                handoff_id=handoff_3.resource_id, decision="confirm")
        service.finish_product(request_id="acceptance-finish", actor_id="quality-001",
                               product_id=original_product)

        # 试戴返修：原成品保留，建立原因明确的新分支。
        rework = service.rework(request_id="acceptance-rework", actor_id="quality-001",
                                case_id=case_id, reason="边缘密合不良，重新修整边缘",
                                source_product_id=original_product,
                                route=[{"code": "adjust-margin", "station_id": "station-mill"}])
        rework_branch = rework.resource_id

        # 处方修订：返修分支尚未开始的工序重新评估，原分支已完工记录保持不变。
        service.revise_prescription(
            request_id="acceptance-revise", actor_id="quality-001", case_id=case_id,
            prescription={"restoration": "crown", "tooth_fdi": "16",
                          "dimensions_mm": {"occlusal_gap": 1.6, "margin": 0.8},
                          "material": {"code": "zirconia", "shade": "A2"}},
            reason="临床要求加大咬合间隙")

        ledger = service.get_case_ledger(actor_id="quality-001", case_id=case_id)
        trace = service.trace_product(actor_id="quality-001", product_id=original_product)
        rework_ledger = service.get_case_ledger(actor_id="quality-001", case_id=case_id)
        rework_op = next(op for route in rework_ledger.routes if route["branch_id"] == rework_branch
                         for op in route["operations"])
        root_balance = service.material_balance(actor_id="quality-001", lot_id="lot-root")
        work_balance = service.material_balance(actor_id="quality-001", lot_id="lot-work")
        audit_valid, audit_events = service.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "prescription_versions": len(ledger.prescriptions),
            "products": len(ledger.products),
            "original_product_preserved":
                any(p["product_id"] == original_product and p["status"] == "finished"
                    for p in ledger.products),
            "rework_operation_reevaluated":
                rework_op["prescription_version"] == 2 and rework_op["status"] == "pending",
            "original_records_unchanged":
                all(op["status"] == "done" and op["prescription_version"] == 1
                    for route in ledger.routes if route["branch_id"] == branch_id
                    for op in route["operations"]),
            "handoffs_confirmed":
                sum(1 for h in ledger.handoffs if h["state"] in ("confirmed", "closed")),
            "root_balanced": root_balance["balanced"],
            "work_balanced": work_balance["balanced"],
            "root_remaining": root_balance["remaining_qty"],
            "work_remaining": work_balance["remaining_qty"],
            "trace_inputs": len(trace.inputs),
            "trace_handlers": len(trace.product["station_responsibility"]),
            "open_discrepancies": len(trace.open_discrepancies),
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
