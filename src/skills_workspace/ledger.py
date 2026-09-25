"""病例工序与材料追溯账本。

在基础服务之上提供口腔修复工艺训练的追溯能力：脱敏处方版本、关键尺寸、
工序路线、材料批次与工位责任全程入账；交接由交出方发起、接收方确认或提出
差异，任何工序只能基于已确认的前序版本开始；材料拆分与消耗保持数量守恒；
返修建立原因明确的新分支而不覆盖原成品；处方修订仅重新评估尚未开始的
路线，不改写既有加工记录；重复交接、并发领料与终态迟到消息都返回稳定结果。
"""

from __future__ import annotations

import json
import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService

MAX_STEPS = 20
MAX_ALLOCATIONS = 50
QUANTITY_SCALE = 6
TRACE_ROLES = ("admin", "reviewer", "auditor")


class LedgerService(DomainService):
    """协调病例、处方版本、工序路线、交接与材料批次的追溯规则。"""

    # ---- 基础校验与查询助手 ----

    def _object(self, value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValidationError(f"{field} 必须是非空对象")
        try:
            canonical_json(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{field} 必须是可序列化的 JSON 对象") from None
        return value

    def _dimensions(self, value: Any) -> dict[str, Any]:
        value = self._object(value, "dimensions")
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValidationError("关键尺寸的字段名不能为空")
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                raise ValidationError("关键尺寸的取值必须是字符串或数值")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValidationError("关键尺寸的数值必须有限")
            if isinstance(item, str) and len(item) > 200:
                raise ValidationError("关键尺寸的文本取值过长")
        return value

    def _quantity(self, value: Any, field: str = "quantity") -> Decimal:
        try:
            quantity = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError(f"{field} 必须是数值") from None
        if not quantity.is_finite() or quantity <= 0:
            raise ValidationError(f"{field} 必须是正数")
        if -quantity.as_tuple().exponent > QUANTITY_SCALE:
            raise ValidationError(f"{field} 最多支持 {QUANTITY_SCALE} 位小数")
        return quantity

    @staticmethod
    def _quantity_text(quantity: Decimal) -> str:
        return format(quantity.normalize(), "f")

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _case_row(self, connection, case_id: str):
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("病例不存在")
        return row

    def _batch_row(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("材料批次不存在")
        return row

    def _handover_row(self, connection, handover_id: str):
        row = connection.execute("SELECT * FROM handovers WHERE handover_id=?", (handover_id,)).fetchone()
        if row is None:
            raise NotFoundError("交接单不存在")
        return row

    def _check_site_scope(self, actor: Actor, site_row) -> None:
        if actor.role != "admin" and actor.organization_id != site_row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的资源")

    def _step_context(self, connection, step_id: str):
        step = connection.execute("SELECT * FROM route_steps WHERE step_id=?", (step_id,)).fetchone()
        if step is None:
            raise NotFoundError("工序不存在")
        route = connection.execute("SELECT * FROM routes WHERE route_id=?", (step["route_id"],)).fetchone()
        case = self._case_row(connection, route["case_id"])
        site = self._site_row(connection, case["site_id"])
        return step, route, case, site

    # ---- 病例接收 ----

    def register_case(self, *, request_id: str, actor_id: str, site_id: str,
                      external_key: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id,
                   "external_key": external_key, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            external_key = self._identifier(external_key, "external_key")
            note = self._text(note, "note", 500) if note else ""

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM cases WHERE site_id=? AND external_key=?",
                    (site_id, external_key),
                ).fetchone()
                if existing:
                    if existing["note"] != note:
                        raise ConflictError("同一病例编号已登记不同内容")
                    return "case", existing["case_id"], {"case_id": existing["case_id"],
                                                         "status": existing["status"]}
                case_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cases(case_id,site_id,external_key,note,status,created_by,created_at) "
                    "VALUES(?,?,?,?,'open',?,?)",
                    (case_id, site_id, external_key, note, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="case.registered",
                             resource_type="case", resource_id=case_id,
                             detail={"site_id": site_id, "external_key": external_key},
                             occurred_at=self._now())
                return "case", case_id, {"case_id": case_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_case", payload=payload, create=create)

    # ---- 脱敏处方版本 ----

    def register_prescription_version(self, *, request_id: str, actor_id: str, case_id: str,
                                      content: dict[str, Any], dimensions: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "case_id": case_id,
                   "content": content, "dimensions": dimensions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._case_row(connection, case_id)
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            if case["status"] != "open":
                raise ConflictError("病例已关闭，不能登记处方版本")
            content = self._object(content, "content")
            dimensions = self._dimensions(dimensions)
            version_hash = digest({"content": content, "dimensions": dimensions})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM prescription_versions WHERE case_id=? AND payload_hash=?",
                    (case_id, version_hash),
                ).fetchone()
                if existing:
                    return "prescription_version", existing["version_id"], {
                        "version_id": existing["version_id"], "version_no": existing["version_no"],
                        "status": existing["status"], "reevaluated_routes": []}
                row = connection.execute(
                    "SELECT COALESCE(MAX(version_no),0) AS max_no FROM prescription_versions WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                version_no = row["max_no"] + 1
                version_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "UPDATE prescription_versions SET status='superseded' WHERE case_id=? AND status='active'",
                    (case_id,),
                )
                connection.execute(
                    "INSERT INTO prescription_versions(version_id,case_id,version_no,payload_json,payload_hash,"
                    "dimensions_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                    (version_id, case_id, version_no, canonical_json(content), version_hash,
                     canonical_json(dimensions), actor_id, now),
                )
                reevaluated: list[str] = []
                if version_no > 1:
                    planned = connection.execute(
                        "SELECT * FROM routes WHERE case_id=? AND status='planned'", (case_id,)
                    ).fetchall()
                    for route in planned:
                        connection.execute("UPDATE routes SET status='reevaluated' WHERE route_id=?",
                                           (route["route_id"],))
                        append_event(connection, actor_id=actor_id, action="route.reevaluated",
                                     resource_type="route", resource_id=route["route_id"],
                                     detail={"case_id": case_id, "prescription_version_id": version_id,
                                             "previous_version_id": route["prescription_version_id"]},
                                     occurred_at=now)
                        reevaluated.append(route["route_id"])
                append_event(connection, actor_id=actor_id, action="prescription.version_registered",
                             resource_type="prescription_version", resource_id=version_id,
                             detail={"case_id": case_id, "version_no": version_no,
                                     "payload_hash": version_hash, "reevaluated_routes": reevaluated},
                             occurred_at=now)
                return "prescription_version", version_id, {
                    "version_id": version_id, "version_no": version_no,
                    "status": "active", "reevaluated_routes": reevaluated}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_prescription_version", payload=payload, create=create)

    # ---- 工序路线 ----

    def _steps(self, connection, steps: Any, site_row) -> list[dict[str, str]]:
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValidationError(f"工序路线必须包含 1 到 {MAX_STEPS} 个工序")
        normalized = []
        for item in steps:
            if not isinstance(item, dict):
                raise ValidationError("工序必须是对象")
            station = self._text(str(item.get("station", "")), "station", 80)
            responsible_id = self._identifier(str(item.get("responsible_id", "")), "responsible_id")
            responsible = self._actor(connection, responsible_id)
            if responsible.role not in ("operator", "admin"):
                raise ValidationError("工位责任人必须是技师或管理员")
            if responsible.organization_id != site_row["organization_id"]:
                raise ValidationError("工位责任人必须属于场所所在组织")
            normalized.append({"station": station, "responsible_id": responsible_id})
        return normalized

    def create_route(self, *, request_id: str, actor_id: str, case_id: str,
                     steps: list[dict[str, Any]], rework_of_product_id: str | None = None,
                     rework_reason: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "case_id": case_id, "steps": steps,
                   "rework_of_product_id": rework_of_product_id, "rework_reason": rework_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case = self._case_row(connection, case_id)
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            if case["status"] != "open":
                raise ConflictError("病例已关闭，不能建立工序路线")
            version = connection.execute(
                "SELECT * FROM prescription_versions WHERE case_id=? AND status='active'", (case_id,)
            ).fetchone()
            if version is None:
                raise ValidationError("病例缺少有效处方版本，不能建立工序路线")
            normalized = self._steps(connection, steps, site)
            branch_of = None
            reason = None
            if rework_of_product_id is not None:
                product = connection.execute("SELECT * FROM products WHERE product_id=?",
                                             (rework_of_product_id,)).fetchone()
                if product is None:
                    raise NotFoundError("返修来源成品不存在")
                if product["case_id"] != case_id:
                    raise ValidationError("返修必须基于同一病例的成品")
                branch_of = rework_of_product_id
                reason = self._text(str(rework_reason or ""), "rework_reason", 500)
            elif rework_reason:
                raise ValidationError("非返修路线不能填写返修原因")
            definition_hash = digest({"case_id": case_id,
                                      "prescription_version_id": version["version_id"],
                                      "steps": normalized, "branch_of": branch_of,
                                      "rework_reason": reason})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM routes WHERE case_id=? AND definition_hash=?",
                    (case_id, definition_hash),
                ).fetchone()
                if existing:
                    step_ids = [row["step_id"] for row in connection.execute(
                        "SELECT step_id FROM route_steps WHERE route_id=? ORDER BY sequence",
                        (existing["route_id"],))]
                    return "route", existing["route_id"], {"route_id": existing["route_id"],
                                                           "status": existing["status"],
                                                           "step_ids": step_ids}
                route_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO routes(route_id,case_id,prescription_version_id,definition_hash,"
                    "branch_of_product_id,rework_reason,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,'planned',?,?)",
                    (route_id, case_id, version["version_id"], definition_hash,
                     branch_of, reason, actor_id, now),
                )
                step_ids = []
                for index, step in enumerate(normalized, start=1):
                    step_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO route_steps(step_id,route_id,sequence,station,responsible_id,status) "
                        "VALUES(?,?,?,?,?,'pending')",
                        (step_id, route_id, index, step["station"], step["responsible_id"]),
                    )
                    step_ids.append(step_id)
                append_event(connection, actor_id=actor_id, action="route.created",
                             resource_type="route", resource_id=route_id,
                             detail={"case_id": case_id,
                                     "prescription_version_id": version["version_id"],
                                     "steps": len(step_ids), "branch_of_product_id": branch_of,
                                     "rework_reason": reason},
                             occurred_at=now)
                return "route", route_id, {"route_id": route_id, "status": "planned",
                                           "step_ids": step_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_route", payload=payload, create=create)

    # ---- 工序执行 ----

    def start_step(self, *, request_id: str, actor_id: str, step_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "step_id": step_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            step, route, case, site = self._step_context(connection, step_id)
            self._check_site_scope(actor, site)
            if actor.role != "admin" and actor.actor_id != step["responsible_id"]:
                raise PermissionDenied("只有工位责任人或管理员可以开始工序")

            def create() -> tuple[str, str, dict[str, Any]]:
                if step["status"] == "in_progress":
                    return "route_step", step_id, {"step_id": step_id, "status": "in_progress",
                                                   "started_at": step["started_at"], "already": True}
                if step["status"] == "completed":
                    raise ConflictError("工序已完成，迟到的开始请求不予受理")
                if route["status"] not in ("planned", "in_progress"):
                    raise ConflictError("路线当前状态不允许开始工序")
                blockers = connection.execute(
                    "SELECT COUNT(*) AS count FROM route_steps "
                    "WHERE route_id=? AND sequence<? AND status!='completed'",
                    (route["route_id"], step["sequence"]),
                ).fetchone()["count"]
                if blockers:
                    raise ConflictError("前序工序尚未完成")
                if step["sequence"] > 1:
                    previous = connection.execute(
                        "SELECT step_id FROM route_steps WHERE route_id=? AND sequence=?",
                        (route["route_id"], step["sequence"] - 1),
                    ).fetchone()
                    handover = connection.execute(
                        "SELECT 1 FROM handovers WHERE route_id=? AND from_step_id=? AND to_step_id=? "
                        "AND status='confirmed'",
                        (route["route_id"], previous["step_id"], step_id),
                    ).fetchone()
                    if handover is None:
                        raise ConflictError("前序版本尚未确认，不能开始工序")
                now = self._now()
                connection.execute("UPDATE route_steps SET status='in_progress', started_at=? WHERE step_id=?",
                                   (now, step_id))
                if route["status"] == "planned":
                    connection.execute("UPDATE routes SET status='in_progress' WHERE route_id=?",
                                       (route["route_id"],))
                append_event(connection, actor_id=actor_id, action="step.started",
                             resource_type="route_step", resource_id=step_id,
                             detail={"route_id": route["route_id"], "sequence": step["sequence"],
                                     "station": step["station"]},
                             occurred_at=now)
                return "route_step", step_id, {"step_id": step_id, "status": "in_progress",
                                               "started_at": now}

            return self._idempotent(connection, request_id=request_id,
                                    action="start_step", payload=payload, create=create)

    def complete_step(self, *, request_id: str, actor_id: str, step_id: str,
                      output: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "step_id": step_id, "output": output}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            step, route, case, site = self._step_context(connection, step_id)
            self._check_site_scope(actor, site)
            if actor.role != "admin" and actor.actor_id != step["responsible_id"]:
                raise PermissionDenied("只有工位责任人或管理员可以完成工序")
            output = self._object(output, "output")
            output_hash = digest(output)

            def create() -> tuple[str, str, dict[str, Any]]:
                if step["status"] == "completed":
                    if step["output_hash"] == output_hash:
                        return "route_step", step_id, {"step_id": step_id, "status": "completed",
                                                       "output_hash": output_hash, "already": True}
                    raise ConflictError("工序已完成，不能改写既有加工记录")
                if step["status"] != "in_progress":
                    raise ConflictError("工序尚未开始，不能登记完成")
                now = self._now()
                connection.execute(
                    "UPDATE route_steps SET status='completed', completed_at=?, output_json=?, "
                    "output_hash=? WHERE step_id=?",
                    (now, canonical_json(output), output_hash, step_id),
                )
                append_event(connection, actor_id=actor_id, action="step.completed",
                             resource_type="route_step", resource_id=step_id,
                             detail={"route_id": route["route_id"], "sequence": step["sequence"],
                                     "output_hash": output_hash},
                             occurred_at=now)
                response: dict[str, Any] = {"step_id": step_id, "status": "completed",
                                            "output_hash": output_hash}
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM route_steps WHERE route_id=? AND status!='completed'",
                    (route["route_id"],),
                ).fetchone()["count"]
                if remaining == 0:
                    connection.execute("UPDATE routes SET status='completed', completed_at=? WHERE route_id=?",
                                       (now, route["route_id"]))
                    product_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO products(product_id,case_id,route_id,prescription_version_id,completed_at) "
                        "VALUES(?,?,?,?,?)",
                        (product_id, case["case_id"], route["route_id"],
                         route["prescription_version_id"], now),
                    )
                    append_event(connection, actor_id=actor_id, action="route.completed",
                                 resource_type="route", resource_id=route["route_id"],
                                 detail={"case_id": case["case_id"]}, occurred_at=now)
                    append_event(connection, actor_id=actor_id, action="product.completed",
                                 resource_type="product", resource_id=product_id,
                                 detail={"case_id": case["case_id"], "route_id": route["route_id"],
                                         "prescription_version_id": route["prescription_version_id"]},
                                 occurred_at=now)
                    response["route_status"] = "completed"
                    response["product_id"] = product_id
                return "route_step", step_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_step", payload=payload, create=create)

    # ---- 交接 ----

    @staticmethod
    def _handover_response(row) -> dict[str, Any]:
        return {"handover_id": row["handover_id"], "status": row["status"],
                "attempt_no": row["attempt_no"]}

    def initiate_handover(self, *, request_id: str, actor_id: str, from_step_id: str,
                          to_step_id: str, version_ref: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "from_step_id": from_step_id,
                   "to_step_id": to_step_id, "version_ref": version_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            from_step, route, case, site = self._step_context(connection, from_step_id)
            self._check_site_scope(actor, site)
            to_step = connection.execute("SELECT * FROM route_steps WHERE step_id=?",
                                         (to_step_id,)).fetchone()
            if to_step is None or to_step["route_id"] != route["route_id"]:
                raise NotFoundError("接收工序不存在或不属于同一路线")
            if to_step["sequence"] != from_step["sequence"] + 1:
                raise ValidationError("交接必须面向相邻的下一工序")
            if actor.role != "admin" and actor.actor_id != from_step["responsible_id"]:
                raise PermissionDenied("只有交出工序的责任人或管理员可以发起交接")
            version_ref = self._object(version_ref, "version_ref")
            version_hash = digest(version_ref)

            def create() -> tuple[str, str, dict[str, Any]]:
                if from_step["status"] != "completed":
                    raise ConflictError("交出工序尚未完成，不能发起交接")
                existing = connection.execute(
                    "SELECT * FROM handovers WHERE route_id=? AND from_step_id=? AND to_step_id=? "
                    "ORDER BY attempt_no DESC",
                    (route["route_id"], from_step_id, to_step_id),
                ).fetchall()
                for row in existing:
                    if row["status"] in ("pending", "disputed"):
                        if row["version_ref_hash"] == version_hash:
                            response = {**self._handover_response(row), "already": True}
                            return "handover", row["handover_id"], response
                        raise ConflictError("存在未完成的交接，请先确认或处理差异")
                    if row["status"] == "confirmed":
                        if row["version_ref_hash"] == version_hash:
                            response = {**self._handover_response(row), "already": True}
                            return "handover", row["handover_id"], response
                        raise ConflictError("交接已确认，不能重复发起不同内容的交接")
                handover_id = uuid.uuid4().hex
                attempt_no = (existing[0]["attempt_no"] + 1) if existing else 1
                now = self._now()
                connection.execute(
                    "INSERT INTO handovers(handover_id,route_id,case_id,from_step_id,to_step_id,attempt_no,"
                    "from_actor_id,to_actor_id,version_ref_json,version_ref_hash,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)",
                    (handover_id, route["route_id"], case["case_id"], from_step_id, to_step_id,
                     attempt_no, actor_id, to_step["responsible_id"],
                     canonical_json(version_ref), version_hash, now),
                )
                append_event(connection, actor_id=actor_id, action="handover.initiated",
                             resource_type="handover", resource_id=handover_id,
                             detail={"route_id": route["route_id"], "from_step_id": from_step_id,
                                     "to_step_id": to_step_id, "attempt_no": attempt_no,
                                     "to_actor_id": to_step["responsible_id"],
                                     "version_ref_hash": version_hash},
                             occurred_at=now)
                return "handover", handover_id, {"handover_id": handover_id,
                                                 "status": "pending", "attempt_no": attempt_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="initiate_handover", payload=payload, create=create)

    def confirm_handover(self, *, request_id: str, actor_id: str, handover_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "handover_id": handover_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            handover = self._handover_row(connection, handover_id)
            case = self._case_row(connection, handover["case_id"])
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            if actor.role != "admin" and actor.actor_id != handover["to_actor_id"]:
                raise PermissionDenied("只有接收方或管理员可以确认交接")

            def create() -> tuple[str, str, dict[str, Any]]:
                if handover["status"] == "confirmed":
                    return "handover", handover_id, {"handover_id": handover_id,
                                                     "status": "confirmed",
                                                     "confirmed_at": handover["confirmed_at"],
                                                     "already": True}
                open_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM handover_discrepancies "
                    "WHERE handover_id=? AND status='open'",
                    (handover_id,),
                ).fetchone()["count"]
                if open_count:
                    raise ConflictError("存在未决差异，不能确认交接")
                now = self._now()
                connection.execute(
                    "UPDATE handovers SET status='confirmed', confirmed_at=?, confirmed_by=? "
                    "WHERE handover_id=?",
                    (now, actor_id, handover_id),
                )
                append_event(connection, actor_id=actor_id, action="handover.confirmed",
                             resource_type="handover", resource_id=handover_id,
                             detail={"route_id": handover["route_id"],
                                     "from_step_id": handover["from_step_id"],
                                     "to_step_id": handover["to_step_id"]},
                             occurred_at=now)
                return "handover", handover_id, {"handover_id": handover_id,
                                                 "status": "confirmed", "confirmed_at": now}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_handover", payload=payload, create=create)

    def raise_discrepancy(self, *, request_id: str, actor_id: str, handover_id: str,
                          detail: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "handover_id": handover_id, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            handover = self._handover_row(connection, handover_id)
            case = self._case_row(connection, handover["case_id"])
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            if actor.role != "admin" and actor.actor_id != handover["to_actor_id"]:
                raise PermissionDenied("只有接收方或管理员可以提出差异")
            detail = self._text(detail, "detail", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if handover["status"] == "confirmed":
                    raise ConflictError("交接已确认，迟到的差异不予受理")
                open_one = connection.execute(
                    "SELECT * FROM handover_discrepancies WHERE handover_id=? AND status='open'",
                    (handover_id,),
                ).fetchone()
                if open_one:
                    return "handover_discrepancy", open_one["discrepancy_id"], {
                        "discrepancy_id": open_one["discrepancy_id"], "status": "open",
                        "already": True}
                discrepancy_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO handover_discrepancies(discrepancy_id,handover_id,detail,status,"
                    "raised_by,created_at) VALUES(?,?,?,'open',?,?)",
                    (discrepancy_id, handover_id, detail, actor_id, now),
                )
                connection.execute("UPDATE handovers SET status='disputed' WHERE handover_id=?",
                                   (handover_id,))
                append_event(connection, actor_id=actor_id, action="discrepancy.raised",
                             resource_type="handover_discrepancy", resource_id=discrepancy_id,
                             detail={"handover_id": handover_id, "detail": detail},
                             occurred_at=now)
                append_event(connection, actor_id=actor_id, action="handover.disputed",
                             resource_type="handover", resource_id=handover_id,
                             detail={"discrepancy_id": discrepancy_id}, occurred_at=now)
                return "handover_discrepancy", discrepancy_id, {
                    "discrepancy_id": discrepancy_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="raise_discrepancy", payload=payload, create=create)

    def resolve_discrepancy(self, *, request_id: str, actor_id: str, discrepancy_id: str,
                            resolution: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "discrepancy_id": discrepancy_id,
                   "resolution": resolution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM handover_discrepancies WHERE discrepancy_id=?",
                (discrepancy_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("差异记录不存在")
            handover = self._handover_row(connection, row["handover_id"])
            case = self._case_row(connection, handover["case_id"])
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            if actor.role not in ("admin", "reviewer") and actor.actor_id != row["raised_by"]:
                raise PermissionDenied("只有差异提出方、质量复核或管理员可以结案差异")
            resolution = self._text(resolution, "resolution", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "resolved":
                    return "handover_discrepancy", discrepancy_id, {
                        "discrepancy_id": discrepancy_id, "status": "resolved", "already": True}
                now = self._now()
                connection.execute(
                    "UPDATE handover_discrepancies SET status='resolved', resolution=?, "
                    "resolved_by=?, resolved_at=? WHERE discrepancy_id=?",
                    (resolution, actor_id, now, discrepancy_id),
                )
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM handover_discrepancies "
                    "WHERE handover_id=? AND status='open'",
                    (handover["handover_id"],),
                ).fetchone()["count"]
                if remaining == 0 and handover["status"] == "disputed":
                    connection.execute("UPDATE handovers SET status='pending' WHERE handover_id=?",
                                       (handover["handover_id"],))
                append_event(connection, actor_id=actor_id, action="discrepancy.resolved",
                             resource_type="handover_discrepancy", resource_id=discrepancy_id,
                             detail={"handover_id": handover["handover_id"],
                                     "resolution": resolution},
                             occurred_at=now)
                return "handover_discrepancy", discrepancy_id, {
                    "discrepancy_id": discrepancy_id, "status": "resolved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_discrepancy", payload=payload, create=create)

    # ---- 材料批次 ----

    @staticmethod
    def _batch_response(row) -> dict[str, Any]:
        return {"batch_id": row["batch_id"], "material_code": row["material_code"],
                "lot_number": row["lot_number"], "unit": row["unit"],
                "initial_quantity": row["initial_quantity"],
                "remaining_quantity": row["remaining_quantity"], "status": row["status"]}

    def register_material_batch(self, *, request_id: str, actor_id: str, site_id: str,
                                material_code: str, lot_number: str, quantity: Any,
                                unit: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "material_code": material_code,
                   "lot_number": lot_number, "quantity": quantity, "unit": unit}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            material_code = self._identifier(material_code, "material_code")
            lot_number = self._identifier(lot_number, "lot_number")
            unit = self._text(unit, "unit", 20)
            amount_text = self._quantity_text(self._quantity(quantity))

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM material_batches WHERE site_id=? AND material_code=? AND lot_number=?",
                    (site_id, material_code, lot_number),
                ).fetchone()
                if existing:
                    if existing["initial_quantity"] != amount_text or existing["unit"] != unit:
                        raise ConflictError("同一批次已登记不同内容")
                    return "material_batch", existing["batch_id"], self._batch_response(existing)
                batch_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO material_batches(batch_id,site_id,material_code,lot_number,unit,"
                    "initial_quantity,remaining_quantity,parent_batch_id,split_hash,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,NULL,NULL,'available',?,?)",
                    (batch_id, site_id, material_code, lot_number, unit,
                     amount_text, amount_text, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="material.batch_registered",
                             resource_type="material_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "material_code": material_code,
                                     "lot_number": lot_number, "quantity": amount_text,
                                     "unit": unit},
                             occurred_at=self._now())
                return "material_batch", batch_id, {"batch_id": batch_id,
                                                    "material_code": material_code,
                                                    "lot_number": lot_number, "unit": unit,
                                                    "initial_quantity": amount_text,
                                                    "remaining_quantity": amount_text,
                                                    "status": "available"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_material_batch", payload=payload, create=create)

    def split_material_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                             allocations: list[Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "allocations": allocations}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch_row(connection, batch_id)
            site = self._site_row(connection, batch["site_id"])
            self._check_site_scope(actor, site)
            if not isinstance(allocations, list) or not 1 <= len(allocations) <= MAX_ALLOCATIONS:
                raise ValidationError(f"拆分方案必须包含 1 到 {MAX_ALLOCATIONS} 份")
            amounts = [self._quantity(item, "allocations") for item in allocations]
            total = sum(amounts, Decimal(0))
            split_hash = digest([self._quantity_text(amount) for amount in amounts])

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM material_batches WHERE parent_batch_id=? AND split_hash=? "
                    "ORDER BY lot_number",
                    (batch_id, split_hash),
                ).fetchall()
                if existing:
                    if len(existing) != len(amounts):
                        raise ConflictError("拆分记录与请求不一致")
                    return "material_batch", batch_id, {
                        "batch_id": batch_id,
                        "children": [row["batch_id"] for row in existing], "already": True}
                remaining = Decimal(batch["remaining_quantity"])
                if total > remaining:
                    raise ConflictError("拆分数量超过批次剩余")
                child_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM material_batches WHERE parent_batch_id=?",
                    (batch_id,),
                ).fetchone()["count"]
                now = self._now()
                children = []
                for index, amount in enumerate(amounts, start=1):
                    child_id = uuid.uuid4().hex
                    lot_number = f"{batch['lot_number']}-S{child_count + index}"
                    connection.execute(
                        "INSERT INTO material_batches(batch_id,site_id,material_code,lot_number,unit,"
                        "initial_quantity,remaining_quantity,parent_batch_id,split_hash,status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'available',?,?)",
                        (child_id, batch["site_id"], batch["material_code"], lot_number,
                         batch["unit"], self._quantity_text(amount), self._quantity_text(amount),
                         batch_id, split_hash, actor_id, now),
                    )
                    children.append(child_id)
                new_remaining = remaining - total
                connection.execute(
                    "UPDATE material_batches SET remaining_quantity=?, status=? WHERE batch_id=?",
                    (self._quantity_text(new_remaining),
                     "depleted" if new_remaining == 0 else "available", batch_id),
                )
                append_event(connection, actor_id=actor_id, action="material.split",
                             resource_type="material_batch", resource_id=batch_id,
                             detail={"children": children,
                                     "allocations": [self._quantity_text(a) for a in amounts],
                                     "remaining_quantity": self._quantity_text(new_remaining)},
                             occurred_at=now)
                return "material_batch", batch_id, {
                    "batch_id": batch_id, "children": children,
                    "remaining_quantity": self._quantity_text(new_remaining)}

            return self._idempotent(connection, request_id=request_id,
                                    action="split_material_batch", payload=payload, create=create)

    def consume_material(self, *, request_id: str, actor_id: str, batch_id: str, case_id: str,
                         quantity: Any, step_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "case_id": case_id,
                   "quantity": quantity, "step_id": step_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch_row(connection, batch_id)
            site = self._site_row(connection, batch["site_id"])
            self._check_site_scope(actor, site)
            case = self._case_row(connection, case_id)
            if case["site_id"] != batch["site_id"]:
                raise ValidationError("材料与病例不属于同一场所")
            amount = self._quantity(quantity)
            route_id = None
            if step_id is not None:
                step, route, step_case, _ = self._step_context(connection, step_id)
                if step_case["case_id"] != case_id:
                    raise ValidationError("工序不属于该病例")
                if step["status"] != "in_progress":
                    raise ConflictError("工序未在制，不能领料")
                route_id = route["route_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                remaining = Decimal(batch["remaining_quantity"])
                if amount > remaining:
                    raise ConflictError("批次剩余数量不足")
                consumption_id = uuid.uuid4().hex
                now = self._now()
                new_remaining = remaining - amount
                connection.execute(
                    "UPDATE material_batches SET remaining_quantity=?, status=? WHERE batch_id=?",
                    (self._quantity_text(new_remaining),
                     "depleted" if new_remaining == 0 else "available", batch_id),
                )
                connection.execute(
                    "INSERT INTO material_consumptions(consumption_id,batch_id,case_id,route_id,"
                    "step_id,quantity,unit,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (consumption_id, batch_id, case_id, route_id, step_id,
                     self._quantity_text(amount), batch["unit"], actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="material.consumed",
                             resource_type="material_batch", resource_id=batch_id,
                             detail={"consumption_id": consumption_id, "case_id": case_id,
                                     "step_id": step_id,
                                     "quantity": self._quantity_text(amount),
                                     "remaining_quantity": self._quantity_text(new_remaining)},
                             occurred_at=now)
                return "material_consumption", consumption_id, {
                    "consumption_id": consumption_id,
                    "remaining_quantity": self._quantity_text(new_remaining)}

            return self._idempotent(connection, request_id=request_id,
                                    action="consume_material", payload=payload, create=create)

    # ---- 质量追溯 ----

    def _trace_actor(self, connection, actor_id: str) -> Actor:
        return self._actor(connection, actor_id)

    def _open_discrepancies(self, connection, case_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT d.*, h.route_id FROM handover_discrepancies d "
            "JOIN handovers h ON h.handover_id=d.handover_id "
            "WHERE h.case_id=? AND d.status='open' ORDER BY d.created_at, d.discrepancy_id",
            (case_id,),
        ).fetchall()
        return [{"discrepancy_id": row["discrepancy_id"], "handover_id": row["handover_id"],
                 "route_id": row["route_id"], "detail": row["detail"],
                 "raised_by": row["raised_by"], "created_at": row["created_at"]}
                for row in rows]

    def _case_materials(self, connection, case_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT mc.*, mb.material_code, mb.lot_number, mb.remaining_quantity AS batch_remaining "
            "FROM material_consumptions mc JOIN material_batches mb ON mb.batch_id=mc.batch_id "
            "WHERE mc.case_id=? ORDER BY mc.created_at, mc.consumption_id",
            (case_id,),
        ).fetchall()
        return [{"consumption_id": row["consumption_id"], "batch_id": row["batch_id"],
                 "material_code": row["material_code"], "lot_number": row["lot_number"],
                 "quantity": row["quantity"], "unit": row["unit"],
                 "route_id": row["route_id"], "step_id": row["step_id"],
                 "actor_id": row["actor_id"], "created_at": row["created_at"],
                 "batch_remaining_quantity": row["batch_remaining"]}
                for row in rows]

    def _handlers(self, connection, actor_ids: list[str]) -> list[dict[str, Any]]:
        handlers = []
        seen = set()
        for actor_id in actor_ids:
            if actor_id in seen:
                continue
            seen.add(actor_id)
            row = connection.execute("SELECT * FROM actors WHERE actor_id=?",
                                     (actor_id,)).fetchone()
            if row:
                handlers.append({"actor_id": row["actor_id"],
                                 "display_name": row["display_name"], "role": row["role"]})
        return handlers

    def trace_product(self, *, actor_id: str, product_id: str) -> dict[str, Any]:
        """追溯任一成品的输入版本、经手人、剩余材料及未决差异。"""

        with self.database.read() as connection:
            actor = self._trace_actor(connection, actor_id)
            self._require(actor, *TRACE_ROLES)
            product = connection.execute("SELECT * FROM products WHERE product_id=?",
                                         (product_id,)).fetchone()
            if product is None:
                raise NotFoundError("成品不存在")
            case = self._case_row(connection, product["case_id"])
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            route = connection.execute("SELECT * FROM routes WHERE route_id=?",
                                       (product["route_id"],)).fetchone()
            version = connection.execute("SELECT * FROM prescription_versions WHERE version_id=?",
                                         (product["prescription_version_id"],)).fetchone()
            steps = connection.execute(
                "SELECT * FROM route_steps WHERE route_id=? ORDER BY sequence",
                (route["route_id"],),
            ).fetchall()
            handovers = connection.execute(
                "SELECT * FROM handovers WHERE route_id=? ORDER BY created_at, attempt_no",
                (route["route_id"],),
            ).fetchall()
            confirmed_by_step = {row["to_step_id"]: row for row in handovers
                                 if row["status"] == "confirmed"}
            input_versions = [{
                "step_id": steps[0]["step_id"], "sequence": steps[0]["sequence"],
                "source": "prescription_version", "version_id": version["version_id"],
                "version_no": version["version_no"], "payload_hash": version["payload_hash"],
            }]
            for step in steps[1:]:
                handover = confirmed_by_step.get(step["step_id"])
                input_versions.append({
                    "step_id": step["step_id"], "sequence": step["sequence"],
                    "source": "handover",
                    "handover_id": handover["handover_id"] if handover else None,
                    "version_ref": json.loads(handover["version_ref_json"]) if handover else None,
                    "confirmed_by": handover["confirmed_by"] if handover else None,
                    "confirmed_at": handover["confirmed_at"] if handover else None,
                })
            handler_ids: list[str] = []
            for step in steps:
                handler_ids.append(step["responsible_id"])
            for row in handovers:
                handler_ids.extend([row["from_actor_id"], row["to_actor_id"]])
            materials = self._case_materials(connection, case["case_id"])
            handler_ids.extend(row["actor_id"] for row in materials)
            branches = connection.execute(
                "SELECT * FROM routes WHERE branch_of_product_id=? ORDER BY created_at",
                (product_id,),
            ).fetchall()
            return {
                "product": {"product_id": product["product_id"], "case_id": product["case_id"],
                            "route_id": product["route_id"],
                            "prescription_version_id": product["prescription_version_id"],
                            "completed_at": product["completed_at"]},
                "case": {"case_id": case["case_id"], "external_key": case["external_key"],
                         "site_id": case["site_id"], "status": case["status"]},
                "prescription_version": {
                    "version_id": version["version_id"], "version_no": version["version_no"],
                    "status": version["status"], "payload_hash": version["payload_hash"],
                    "dimensions": json.loads(version["dimensions_json"])},
                "route": {"route_id": route["route_id"], "status": route["status"],
                          "branch_of_product_id": route["branch_of_product_id"],
                          "rework_reason": route["rework_reason"],
                          "steps": [{"step_id": step["step_id"], "sequence": step["sequence"],
                                     "station": step["station"],
                                     "responsible_id": step["responsible_id"],
                                     "status": step["status"], "started_at": step["started_at"],
                                     "completed_at": step["completed_at"],
                                     "output_hash": step["output_hash"]} for step in steps]},
                "input_versions": input_versions,
                "handlers": self._handlers(connection, handler_ids),
                "materials": materials,
                "open_discrepancies": self._open_discrepancies(connection, case["case_id"]),
                "rework_branches": [{"route_id": row["route_id"], "status": row["status"],
                                     "rework_reason": row["rework_reason"],
                                     "created_at": row["created_at"]} for row in branches],
            }

    def trace_case(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        """汇总病例的处方版本、路线、成品、材料与未决差异。"""

        with self.database.read() as connection:
            actor = self._trace_actor(connection, actor_id)
            self._require(actor, *TRACE_ROLES)
            case = self._case_row(connection, case_id)
            site = self._site_row(connection, case["site_id"])
            self._check_site_scope(actor, site)
            versions = connection.execute(
                "SELECT * FROM prescription_versions WHERE case_id=? ORDER BY version_no",
                (case_id,),
            ).fetchall()
            routes = connection.execute(
                "SELECT * FROM routes WHERE case_id=? ORDER BY created_at, route_id",
                (case_id,),
            ).fetchall()
            route_items = []
            for route in routes:
                steps = connection.execute(
                    "SELECT * FROM route_steps WHERE route_id=? ORDER BY sequence",
                    (route["route_id"],),
                ).fetchall()
                route_items.append({
                    "route_id": route["route_id"], "status": route["status"],
                    "prescription_version_id": route["prescription_version_id"],
                    "branch_of_product_id": route["branch_of_product_id"],
                    "rework_reason": route["rework_reason"],
                    "steps": [{"step_id": step["step_id"], "sequence": step["sequence"],
                               "station": step["station"],
                               "responsible_id": step["responsible_id"],
                               "status": step["status"]} for step in steps]})
            products = connection.execute(
                "SELECT * FROM products WHERE case_id=? ORDER BY completed_at", (case_id,)
            ).fetchall()
            return {
                "case": {"case_id": case["case_id"], "external_key": case["external_key"],
                         "site_id": case["site_id"], "status": case["status"],
                         "note": case["note"]},
                "prescription_versions": [
                    {"version_id": row["version_id"], "version_no": row["version_no"],
                     "status": row["status"], "payload_hash": row["payload_hash"],
                     "dimensions": json.loads(row["dimensions_json"])} for row in versions],
                "routes": route_items,
                "products": [{"product_id": row["product_id"], "route_id": row["route_id"],
                              "prescription_version_id": row["prescription_version_id"],
                              "completed_at": row["completed_at"]} for row in products],
                "materials": self._case_materials(connection, case_id),
                "open_discrepancies": self._open_discrepancies(connection, case_id),
            }

    def trace_material(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        """追溯批次拆分树与消耗记录，并校验数量守恒。"""

        with self.database.read() as connection:
            actor = self._trace_actor(connection, actor_id)
            self._require(actor, *TRACE_ROLES)
            root = self._batch_row(connection, batch_id)
            site = self._site_row(connection, root["site_id"])
            self._check_site_scope(actor, site)
            ancestors = []
            current = root
            while current["parent_batch_id"]:
                current = self._batch_row(connection, current["parent_batch_id"])
                ancestors.append(current["batch_id"])
            nodes = []
            consumptions = []
            queue = [batch_id]
            while queue:
                current_id = queue.pop(0)
                row = self._batch_row(connection, current_id)
                children = connection.execute(
                    "SELECT * FROM material_batches WHERE parent_batch_id=? "
                    "ORDER BY lot_number",
                    (current_id,),
                ).fetchall()
                used = connection.execute(
                    "SELECT * FROM material_consumptions WHERE batch_id=? "
                    "ORDER BY created_at, consumption_id",
                    (current_id,),
                ).fetchall()
                consumed_total = sum((Decimal(item["quantity"]) for item in used), Decimal(0))
                children_total = sum((Decimal(child["initial_quantity"]) for child in children),
                                     Decimal(0))
                conserved = (Decimal(row["remaining_quantity"]) + consumed_total
                             + children_total) == Decimal(row["initial_quantity"])
                nodes.append({
                    "batch_id": row["batch_id"], "material_code": row["material_code"],
                    "lot_number": row["lot_number"], "unit": row["unit"],
                    "initial_quantity": row["initial_quantity"],
                    "remaining_quantity": row["remaining_quantity"],
                    "status": row["status"], "parent_batch_id": row["parent_batch_id"],
                    "children": [child["batch_id"] for child in children],
                    "consumed_quantity": self._quantity_text(consumed_total)
                    if consumed_total else "0",
                    "conserved": conserved})
                for item in used:
                    consumptions.append({"consumption_id": item["consumption_id"],
                                         "batch_id": item["batch_id"],
                                         "case_id": item["case_id"],
                                         "step_id": item["step_id"],
                                         "quantity": item["quantity"], "unit": item["unit"],
                                         "actor_id": item["actor_id"],
                                         "created_at": item["created_at"]})
                queue.extend(child["batch_id"] for child in children)
            return {"batch_id": batch_id, "ancestors": ancestors, "nodes": nodes,
                    "conserved": all(node["conserved"] for node in nodes),
                    "consumptions": consumptions}

    def list_discrepancies(self, *, actor_id: str, site_id: str,
                           status: str = "open") -> list[dict[str, Any]]:
        """按场所列出交接差异，供质量人员跟进。"""

        if status not in ("open", "resolved"):
            raise ValidationError("status 只能是 open 或 resolved")
        with self.database.read() as connection:
            actor = self._trace_actor(connection, actor_id)
            self._require(actor, *TRACE_ROLES)
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            rows = connection.execute(
                "SELECT d.*, h.case_id, h.route_id FROM handover_discrepancies d "
                "JOIN handovers h ON h.handover_id=d.handover_id "
                "JOIN cases c ON c.case_id=h.case_id "
                "WHERE c.site_id=? AND d.status=? ORDER BY d.created_at, d.discrepancy_id",
                (site_id, status),
            ).fetchall()
            return [{"discrepancy_id": row["discrepancy_id"],
                     "handover_id": row["handover_id"], "case_id": row["case_id"],
                     "route_id": row["route_id"], "detail": row["detail"],
                     "status": row["status"], "raised_by": row["raised_by"],
                     "created_at": row["created_at"], "resolution": row["resolution"],
                     "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"]}
                    for row in rows]
