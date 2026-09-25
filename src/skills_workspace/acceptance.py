"""运行基础服务与追溯账本的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .ledger import LedgerService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整登记链与病例工序材料追溯链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = LedgerService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范训练机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 追溯账本：病例 → 处方 → 路线 → 工序 → 交接 → 材料 → 成品 → 返修 → 处方修订。
        for request_id, actor_id, name, role in [
            ("req-tech-design", "tech-design", "设计技师", "operator"),
            ("req-tech-mill", "tech-mill", "加工技师", "operator"),
            ("req-tech-tryin", "tech-tryin", "试戴技师", "operator"),
            ("req-quality", "quality-001", "质量复核", "reviewer"),
            ("req-auditor", "auditor-001", "质量追溯员", "auditor"),
        ]:
            service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=actor_id,
                                   display_name=name, role=role, organization_id="org-001")
        case_id = service.register_case(
            request_id="req-case", actor_id="tech-design", site_id="site-001",
            external_key="CASE-2026-001", note="脱敏病例：后牙单冠").resource_id
        service.register_prescription_version(
            request_id="req-rx-1", actor_id="tech-design", case_id=case_id,
            content={"prosthesis": "单冠", "shade": "A2", "material": "氧化锆"},
            dimensions={"近远中径_mm": 10.2, "颊舌径_mm": 9.8, "预备间隙_mm": 1.5})
        route_receipt = service.create_route(
            request_id="req-route", actor_id="tech-design", case_id=case_id,
            steps=[{"station": "设计", "responsible_id": "tech-design"},
                   {"station": "加工", "responsible_id": "tech-mill"},
                   {"station": "试戴", "responsible_id": "tech-tryin"}])
        step_design, step_mill, step_tryin = route_receipt.response["step_ids"]
        service.start_step(request_id="req-start-1", actor_id="tech-design", step_id=step_design)
        service.complete_step(request_id="req-complete-1", actor_id="tech-design",
                              step_id=step_design, output={"设计版本": "v1", "间隙复核": "合格"})
        handover_1 = service.initiate_handover(
            request_id="req-handover-1", actor_id="tech-design", from_step_id=step_design,
            to_step_id=step_mill,
            version_ref={"设计版本": "v1", "关键尺寸": {"近远中径_mm": 10.2}}).resource_id
        service.confirm_handover(request_id="req-confirm-1", actor_id="tech-mill",
                                 handover_id=handover_1)
        service.start_step(request_id="req-start-2", actor_id="tech-mill", step_id=step_mill)
        batch_id = service.register_material_batch(
            request_id="req-batch", actor_id="tech-mill", site_id="site-001",
            material_code="ZIRCONIA", lot_number="LOT-9001", quantity=100, unit="g").resource_id
        children = service.split_material_batch(
            request_id="req-split", actor_id="tech-mill", batch_id=batch_id,
            allocations=[30, 20]).response["children"]
        service.consume_material(request_id="req-consume", actor_id="tech-mill",
                                 batch_id=children[0], case_id=case_id,
                                 quantity=12.5, step_id=step_mill)
        service.complete_step(request_id="req-complete-2", actor_id="tech-mill",
                              step_id=step_mill, output={"加工批次": "M-01", "外观": "合格"})
        handover_2 = service.initiate_handover(
            request_id="req-handover-2", actor_id="tech-mill", from_step_id=step_mill,
            to_step_id=step_tryin,
            version_ref={"加工批次": "M-01", "关键尺寸": {"近远中径_mm": 10.2}}).resource_id
        discrepancy = service.raise_discrepancy(
            request_id="req-dispute", actor_id="tech-tryin", handover_id=handover_2,
            detail="边缘密合度记录缺失").resource_id
        service.resolve_discrepancy(request_id="req-resolve", actor_id="quality-001",
                                    discrepancy_id=discrepancy, resolution="补录密合度记录后放行")
        service.confirm_handover(request_id="req-confirm-2", actor_id="tech-tryin",
                                 handover_id=handover_2)
        service.start_step(request_id="req-start-3", actor_id="tech-tryin", step_id=step_tryin)
        product_id = service.complete_step(
            request_id="req-complete-3", actor_id="tech-tryin", step_id=step_tryin,
            output={"试戴结论": "通过", "咬合": "正常"}).response["product_id"]
        rework_route = service.create_route(
            request_id="req-rework-route", actor_id="tech-design", case_id=case_id,
            steps=[{"station": "返修设计", "responsible_id": "tech-design"},
                   {"station": "返修加工", "responsible_id": "tech-mill"}],
            rework_of_product_id=product_id, rework_reason="邻接面偏松，需加瓷调整")
        # 处方修订：尚未开始的返修路线被重新评估，既有加工记录保持不变。
        service.register_prescription_version(
            request_id="req-rx-2", actor_id="tech-design", case_id=case_id,
            content={"prosthesis": "单冠", "shade": "A2", "material": "氧化锆", "邻接": "加紧"},
            dimensions={"近远中径_mm": 10.2, "颊舌径_mm": 9.8, "预备间隙_mm": 1.6})
        trace = service.trace_product(actor_id="auditor-001", product_id=product_id)
        material_trace = service.trace_material(actor_id="auditor-001", batch_id=batch_id)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "product_traced": trace["product"]["product_id"] == product_id,
                  "trace_handlers": len(trace["handlers"]),
                  "trace_input_versions": len(trace["input_versions"]),
                  "open_discrepancies": len(trace["open_discrepancies"]),
                  "rework_route_status": trace["rework_branches"][0]["status"]
                  if trace["rework_branches"] else None,
                  "rework_route_id": rework_route.resource_id,
                  "material_conserved": material_trace["conserved"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["product_traced"] and result["material_conserved"]
          and result["open_discrepancies"] == 0
          and result["rework_route_status"] == "reevaluated")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
