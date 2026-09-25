"""病例工序与材料追溯账本。

在 :class:`DomainService` 的登记、权限、幂等与审计能力之上实现：

* 脱敏处方的版本化保存与修订（修订只重估尚未开始的工序，不改写加工记录）；
* 交方发起、接收方确认或提出差异的交接状态机，后序工序只能基于已确认的前序版本开始；
* 材料批次拆分/消耗的数量守恒与并发领料的条件扣减；
* 返修以新分支、新产品承接，原成品保持不变；
* 面向质量人员的成品追溯与未决差异、剩余材料查询。

所有写操作都在进程写锁与 ``BEGIN IMMEDIATE`` 事务内完成读-判-写，
重复交接、并发领料与终态迟到消息因此具有稳定结果。
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService
from .tracing_models import CaseLedger, ProductTrace

QUANTITY_SCALE = 4

# 脱敏处方中禁止出现的身份类字段（大小写不敏感，支持嵌套路径检查）。
PII_KEYS = frozenset({
    "patient_name", "patient_id", "client_name", "customer_name",
    "id_card", "identity_card", "id_number", "passport", "social_security",
    "phone", "mobile", "telephone", "email", "address", "birth_date", "birthday",
})

HANDOFF_OPEN_STATES = ("proposed", "disputed")
HANDOFF_ACCEPTED_STATES = ("confirmed", "closed")


class TracingService(DomainService):
    """实现病例工序与材料追溯的全部用例。"""

    def __init__(self, database, clock=None) -> None:
        super().__init__(database, clock)
        # 同一进程内串行化写事务；跨进程由 BEGIN IMMEDIATE + busy_timeout 串行化。
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------ 基础工具

    @contextmanager
    def _unit(self) -> Iterator[Any]:
        with self._write_lock:
            with self.database.transaction(immediate=True) as connection:
                yield connection

    @contextmanager
    def _read_unit(self) -> Iterator[Any]:
        # 多线程共享单一连接时，读事务也需与写事务互斥，避免在同一连接上嵌套 BEGIN。
        with self._write_lock:
            with self.database.transaction() as connection:
                yield connection

    def _qty(self, value: Any, field: str, *, positive: bool = True) -> Decimal:
        try:
            qty = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(f"{field} 必须是十进制数字") from exc
        if not qty.is_finite():
            raise ValidationError(f"{field} 必须是有限数字")
        if qty < 0 or (positive and qty == 0):
            raise ValidationError(f"{field} 必须大于 0" if positive else f"{field} 不能为负")
        if qty.as_tuple().exponent < -QUANTITY_SCALE:
            raise ValidationError(f"{field} 小数位不能超过 {QUANTITY_SCALE} 位")
        return qty

    def _sanitize_prescription(self, payload: Any, path: str = "$") -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_path = f"{path}.{key}"
                if str(key).strip().lower() in PII_KEYS:
                    raise ValidationError(f"处方必须脱敏，不能包含身份字段 {key_path}")
                self._sanitize_prescription(value, key_path)
        elif isinstance(payload, list):
            for index, value in enumerate(payload):
                self._sanitize_prescription(value, f"{path}[{index}]")

    def _case(self, connection, case_id: str) -> Any:
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("病例不存在")
        return row

    def _op(self, connection, branch_id: str, seq: int) -> Any:
        row = connection.execute(
            "SELECT * FROM route_operations WHERE branch_id=? AND seq=?", (branch_id, seq)
        ).fetchone()
        if row is None:
            raise NotFoundError("工序不存在")
        return row

    def _current_product(self, connection, branch_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM products WHERE branch_id=? AND status!='scrapped' "
            "ORDER BY created_at DESC, product_id DESC LIMIT 1",
            (branch_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("分支上没有在制成品")
        return row

    def _current_version(self, connection, case_id: str) -> int:
        row = connection.execute(
            "SELECT MAX(version) AS v FROM prescriptions WHERE case_id=?", (case_id,)
        ).fetchone()
        return int(row["v"] or 0)

    def _site_scope(self, connection, actor, site_id: str) -> None:
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    # ------------------------------------------------------------------ 病例与处方

    def create_case(self, *, request_id: str, actor_id: str, site_id: str, subject_code: str,
                    prescription: dict[str, Any], route: list[dict[str, Any]],
                    sku: str = "restoration") -> Any:
        if not isinstance(prescription, dict) or not prescription:
            raise ValidationError("prescription 必须是非空脱敏对象")
        self._sanitize_prescription(prescription)
        if not isinstance(route, list) or not route:
            raise ValidationError("route 必须是非空工序数组")
        normalized: list[tuple[str, str]] = []
        for index, item in enumerate(route):
            if not isinstance(item, dict) or "code" not in item or "station_id" not in item:
                raise ValidationError(f"route[{index}] 必须包含 code 与 station_id")
            normalized.append((self._text(item["code"], f"route[{index}].code", 60),
                               self._identifier(item["station_id"], f"route[{index}].station_id")))
        payload = {"actor_id": actor_id, "site_id": site_id, "subject_code": subject_code,
                   "prescription": prescription, "route": [{"code": c, "station_id": s} for c, s in normalized],
                   "sku": sku}

        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_scope(connection, actor, site_id)
            subject_code = self._identifier(subject_code, "subject_code")
            sku = self._text(sku, "sku", 80)
            case_id = uuid.uuid4().hex
            prescription_id = uuid.uuid4().hex
            branch_id = uuid.uuid4().hex
            product_id = uuid.uuid4().hex
            now = self._now()
            payload_hash = digest(prescription)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO cases(case_id,site_id,subject_code,status,created_by,created_at) "
                        "VALUES(?,?,?,'open',?,?)",
                        (case_id, site_id, subject_code, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("同一场所下病例脱敏码已经存在") from exc
                connection.execute(
                    "INSERT INTO prescriptions(prescription_id,case_id,version,payload_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,1,?,?,?,?)",
                    (prescription_id, case_id, canonical_json(prescription), payload_hash, actor_id, now),
                )
                connection.execute(
                    "INSERT INTO case_branches(branch_id,case_id,parent_branch_id,root_branch_id,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (branch_id, case_id, None, branch_id, "初始生产", actor_id, now),
                )
                for seq, (code, station_id) in enumerate(normalized, start=1):
                    connection.execute(
                        "INSERT INTO route_operations(operation_id,branch_id,seq,code,station_id,"
                        "prescription_version,status,note) VALUES(?,?,?,?,?,1,'pending','')",
                        (uuid.uuid4().hex, branch_id, seq, code, station_id),
                    )
                connection.execute(
                    "INSERT INTO products(product_id,case_id,branch_id,parent_product_id,sku,status,"
                    "created_by,created_at) VALUES(?,?,?,NULL,?,'in_progress',?,?)",
                    (product_id, case_id, branch_id, sku, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="case.created",
                             resource_type="case", resource_id=case_id,
                             detail={"site_id": site_id, "subject_code": subject_code,
                                     "prescription_version": 1, "prescription_hash": payload_hash,
                                     "branch_id": branch_id, "product_id": product_id,
                                     "operations": len(normalized)}, occurred_at=now)
                return "case", case_id, {"case_id": case_id, "branch_id": branch_id,
                                         "product_id": product_id, "prescription_version": 1}

            return self._idempotent(connection, request_id=request_id, action="create_case",
                                    payload=payload, create=create)

    def revise_prescription(self, *, request_id: str, actor_id: str, case_id: str,
                            prescription: dict[str, Any], reason: str) -> Any:
        if not isinstance(prescription, dict) or not prescription:
            raise ValidationError("prescription 必须是非空脱敏对象")
        self._sanitize_prescription(prescription)
        payload = {"actor_id": actor_id, "case_id": case_id, "prescription": prescription, "reason": reason}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._case(connection, case_id)
            if actor.organization_id != self._org_of(connection, case["site_id"]) and actor.role != "admin":
                raise PermissionDenied("不能修订其他组织的病例处方")
            reason = self._text(reason, "reason", 300)
            latest = self._current_version(connection, case_id)
            new_version = latest + 1
            now = self._now()
            payload_hash = digest(prescription)
            prescription_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO prescriptions(prescription_id,case_id,version,payload_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (prescription_id, case_id, new_version, canonical_json(prescription),
                     payload_hash, actor_id, now),
                )
                # 尚未开始（pending）的工序重新评估并绑定新版本；
                # active/done 等已开始记录按要求保持不变，不能被改写。
                rebound = connection.execute(
                    "UPDATE route_operations SET prescription_version=? "
                    "WHERE branch_id IN (SELECT branch_id FROM case_branches WHERE case_id=?) "
                    "AND status='pending' AND prescription_version!=?",
                    (new_version, case_id, new_version),
                ).rowcount
                # 尚未被接收确认的交接随旧版本作废，交方需按新版本重新发起。
                superseded = connection.execute(
                    "UPDATE handoffs SET state='superseded', closed_at=? "
                    "WHERE case_id=? AND state IN ('proposed','disputed') AND prescription_version<?",
                    (now, case_id, new_version),
                ).rowcount
                append_event(connection, actor_id=actor_id, action="prescription.revised",
                             resource_type="case", resource_id=case_id,
                             detail={"version": new_version, "prescription_hash": payload_hash,
                                     "reason": reason, "operations_rebound": rebound,
                                     "handoffs_superseded": superseded}, occurred_at=now)
                return "prescription", prescription_id, {"case_id": case_id,
                                                         "prescription_version": new_version,
                                                         "operations_rebound": rebound,
                                                         "handoffs_superseded": superseded}

            return self._idempotent(connection, request_id=request_id, action="revise_prescription",
                                    payload=payload, create=create)

    def _org_of(self, connection, site_id: str) -> str:
        row = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
        return row["organization_id"] if row else ""

    # ------------------------------------------------------------------ 工序启动闸门

    def start_operation(self, *, request_id: str, actor_id: str, case_id: str,
                        branch_id: str, seq: int) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "branch_id": branch_id, "seq": seq}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._case(connection, case_id)
            op = self._op(connection, branch_id, int(seq))
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT * FROM route_operations WHERE operation_id=?", (op["operation_id"],)
                ).fetchone()
                if current["status"] != "pending":
                    raise ConflictError("工序已开始或不可再开始")
                predecessor = None
                if seq > 1:
                    predecessor = self._op(connection, branch_id, int(seq) - 1)
                    handoff = connection.execute(
                        "SELECT * FROM handoffs WHERE operation_id=? "
                        "ORDER BY CASE state WHEN 'closed' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, "
                        "created_at DESC",
                        (predecessor["operation_id"],),
                    ).fetchone()
                    if handoff is None or handoff["state"] not in HANDOFF_ACCEPTED_STATES:
                        raise ConflictError("前序交接尚未确认，不能开始本工序")
                    open_diff = connection.execute(
                        "SELECT COUNT(*) AS c FROM discrepancies WHERE handoff_id=? AND status='open'",
                        (handoff["handoff_id"],),
                    ).fetchone()["c"]
                    if open_diff:
                        raise ConflictError("前序交接存在未决差异，不能开始本工序")
                    # 已确认的前序版本即本工序的输入基线；若处方在此后修订，本工序已绑定新版本，
                    # 前序既有的加工/确认记录按要求保持不变，需要重做时另开返修分支。
                    if handoff["prescription_version"] > current["prescription_version"]:
                        raise ConflictError("前序基于更新的处方版本，当前工序需重新评估")
                connection.execute(
                    "UPDATE route_operations SET status='active' WHERE operation_id=? AND status='pending'",
                    (op["operation_id"],),
                )
                connection.execute(
                    "INSERT INTO station_assignments(assignment_id,operation_id,station_id,"
                    "technician_id,role,assigned_at) VALUES(?,?,?,?, 'owner', ?)",
                    (uuid.uuid4().hex, op["operation_id"], op["station_id"], actor_id, now),
                )
                if predecessor is not None:
                    connection.execute(
                        "UPDATE handoffs SET state='closed', closed_at=? WHERE operation_id=? AND state='confirmed'",
                        (now, predecessor["operation_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="operation.started",
                             resource_type="operation", resource_id=op["operation_id"],
                             detail={"case_id": case_id, "branch_id": branch_id, "seq": seq,
                                     "station_id": op["station_id"],
                                     "prescription_version": current["prescription_version"]}, occurred_at=now)
                return "operation", op["operation_id"], {"operation_id": op["operation_id"], "status": "active"}

            return self._idempotent(connection, request_id=request_id, action="start_operation",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 交接

    def propose_handoff(self, *, request_id: str, actor_id: str, case_id: str, branch_id: str,
                        seq: int, to_station_id: str, to_actor_id: str,
                        package: dict[str, Any], message_id: str | None = None) -> Any:
        if not isinstance(package, dict) or not package:
            raise ValidationError("package 必须是非空对象（脱敏处方版本与关键尺寸）")
        message_id = self._optional_identifier(message_id, "message_id")
        to_station_id = self._identifier(to_station_id, "to_station_id")
        payload = {"actor_id": actor_id, "case_id": case_id, "branch_id": branch_id, "seq": seq,
                   "to_station_id": to_station_id, "to_actor_id": to_actor_id,
                   "package": package, "message_id": message_id}
        with self._unit() as connection:
            # 终态/任意状态的迟到或重复消息：同一 message_id 稳定返回原交接。
            if message_id:
                prior = connection.execute(
                    "SELECT handoff_id FROM handoffs WHERE message_id=?", (message_id,)
                ).fetchone()
                if prior:
                    return self._replay_ref(connection, "handoff", prior["handoff_id"], request_id,
                                            "propose_handoff", payload)
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._case(connection, case_id)
            op = self._op(connection, branch_id, int(seq))
            receiver = self._actor(connection, to_actor_id)
            if receiver.organization_id != actor.organization_id and actor.role != "admin":
                raise PermissionDenied("接收方必须属于同一组织")
            handoff_id = uuid.uuid4().hex
            now = self._now()
            package_hash = digest(package)

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT status FROM route_operations WHERE operation_id=?", (op["operation_id"],)
                ).fetchone()
                if current["status"] not in ("active", "done"):
                    raise ConflictError("工序尚未开始，不能发起交接")
                existing = connection.execute(
                    "SELECT state FROM handoffs WHERE operation_id=? AND state IN ('proposed','disputed')",
                    (op["operation_id"],),
                ).fetchone()
                if existing:
                    raise ConflictError("该工序已有进行中的交接，不能重复发起")
                accepted = connection.execute(
                    "SELECT 1 FROM handoffs WHERE operation_id=? AND state IN ('confirmed','closed')",
                    (op["operation_id"],),
                ).fetchone()
                if accepted:
                    raise ConflictError("该工序交接已被确认，不能重复发起")
                connection.execute(
                    "INSERT INTO handoffs(handoff_id,case_id,branch_id,operation_id,seq,"
                    "from_station_id,from_actor_id,to_station_id,to_actor_id,prescription_version,"
                    "payload_hash,state,completed_operation,marked_started,message_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'proposed', 0, 0, ?,?)",
                    (handoff_id, case_id, branch_id, op["operation_id"], seq,
                     op["station_id"], actor_id, to_station_id, to_actor_id,
                     op["prescription_version"], package_hash, message_id, now),
                )
                append_event(connection, actor_id=actor_id, action="handoff.proposed",
                             resource_type="handoff", resource_id=handoff_id,
                             detail={"case_id": case_id, "branch_id": branch_id, "seq": seq,
                                     "to_actor_id": to_actor_id, "to_station_id": to_station_id,
                                     "prescription_version": op["prescription_version"],
                                     "package_hash": package_hash, "message_id": message_id},
                             occurred_at=now)
                return "handoff", handoff_id, {"handoff_id": handoff_id, "state": "proposed"}

            return self._idempotent(connection, request_id=request_id, action="propose_handoff",
                                    payload=payload, create=create)

    def respond_handoff(self, *, request_id: str, actor_id: str, handoff_id: str,
                        decision: str, message_id: str | None = None,
                        reason: str = "", expected: dict[str, Any] | None = None,
                        actual: dict[str, Any] | None = None) -> Any:
        if decision not in ("confirm", "dispute"):
            raise ValidationError("decision 只能是 confirm 或 dispute")
        message_id = self._optional_identifier(message_id, "message_id")
        payload = {"actor_id": actor_id, "handoff_id": handoff_id, "decision": decision,
                   "message_id": message_id, "reason": reason, "expected": expected, "actual": actual}
        with self._unit() as connection:
            if message_id:
                prior = connection.execute(
                    "SELECT handoff_id FROM handoffs WHERE message_id=?", (message_id,)
                ).fetchone()
                if prior and prior["handoff_id"] != handoff_id:
                    return self._replay_ref(connection, "handoff", prior["handoff_id"], request_id,
                                            "respond_handoff", payload)
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            handoff = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
            if handoff is None:
                raise NotFoundError("交接不存在")
            if actor.actor_id != handoff["to_actor_id"] and actor.role != "admin":
                raise PermissionDenied("只有指定接收方可以确认或提出差异")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)
                ).fetchone()
                # 终态迟到消息：不改变任何状态，返回稳定冲突。
                if current["state"] in ("confirmed", "closed", "superseded"):
                    raise ConflictError(f"交接已处于终态 {current['state']}，迟到消息不再生效")
                if decision == "dispute":
                    text = self._text(reason, "reason", 300)
                    discrepancy_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO discrepancies(discrepancy_id,handoff_id,case_id,reason,"
                        "expected_json,actual_json,raised_by,raised_at,status) VALUES(?,?,?,?,?,?,?,?,'open')",
                        (discrepancy_id, handoff_id, current["case_id"], text,
                         canonical_json(expected or {}), canonical_json(actual or {}), actor_id, now),
                    )
                    connection.execute(
                        "UPDATE handoffs SET state='disputed', decided_at=? WHERE handoff_id=?",
                        (now, handoff_id),
                    )
                    append_event(connection, actor_id=actor_id, action="handoff.disputed",
                                 resource_type="handoff", resource_id=handoff_id,
                                 detail={"discrepancy_id": discrepancy_id, "reason": text}, occurred_at=now)
                    return "discrepancy", discrepancy_id, {"handoff_id": handoff_id,
                                                           "state": "disputed",
                                                           "discrepancy_id": discrepancy_id}
                open_diff = connection.execute(
                    "SELECT COUNT(*) AS c FROM discrepancies WHERE handoff_id=? AND status='open'",
                    (handoff_id,),
                ).fetchone()["c"]
                if open_diff:
                    raise ConflictError("存在未解决差异，不能确认交接")
                connection.execute(
                    "UPDATE handoffs SET state='confirmed', decided_at=?, completed_operation=1 WHERE handoff_id=?",
                    (now, handoff_id),
                )
                connection.execute(
                    "UPDATE route_operations SET status='done' WHERE operation_id=?",
                    (current["operation_id"],),
                )
                connection.execute(
                    "INSERT INTO station_assignments(assignment_id,operation_id,station_id,"
                    "technician_id,role,assigned_at) VALUES(?,?,?,?, 'handled', ?)",
                    (uuid.uuid4().hex, current["operation_id"], current["to_station_id"], actor_id, now),
                )
                product = self._current_product(connection, current["branch_id"])
                connection.execute(
                    "INSERT OR IGNORE INTO product_inputs(product_id,kind,ref_id,recorded_at) "
                    "VALUES(?, 'handoff', ?, ?)",
                    (product["product_id"], handoff_id, now),
                )
                append_event(connection, actor_id=actor_id, action="handoff.confirmed",
                             resource_type="handoff", resource_id=handoff_id,
                             detail={"case_id": current["case_id"], "seq": current["seq"],
                                     "prescription_version": current["prescription_version"],
                                     "operation_id": current["operation_id"]}, occurred_at=now)
                return "handoff", handoff_id, {"handoff_id": handoff_id, "state": "confirmed"}

            return self._idempotent(connection, request_id=request_id, action="respond_handoff",
                                    payload=payload, create=create)

    def resolve_discrepancy(self, *, request_id: str, actor_id: str, discrepancy_id: str,
                            resolution: str, approve: bool = True) -> Any:
        payload = {"actor_id": actor_id, "discrepancy_id": discrepancy_id,
                   "resolution": resolution, "approve": bool(approve)}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            row = connection.execute(
                "SELECT * FROM discrepancies WHERE discrepancy_id=?", (discrepancy_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("差异不存在")
            resolution = self._text(resolution, "resolution", 300)
            new_status = "resolved" if approve else "rejected"
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT * FROM discrepancies WHERE discrepancy_id=?", (discrepancy_id,)
                ).fetchone()
                if current["status"] != "open":
                    raise ConflictError("差异已经处理，不能重复处理")
                connection.execute(
                    "UPDATE discrepancies SET status=?, resolution=?, resolved_by=?, resolved_at=? "
                    "WHERE discrepancy_id=? AND status='open'",
                    (new_status, resolution, actor_id, now, discrepancy_id),
                )
                append_event(connection, actor_id=actor_id, action="discrepancy.resolved",
                             resource_type="discrepancy", resource_id=discrepancy_id,
                             detail={"handoff_id": row["handoff_id"], "status": new_status,
                                     "resolution": resolution}, occurred_at=now)
                return "discrepancy", discrepancy_id, {"discrepancy_id": discrepancy_id,
                                                       "status": new_status}

            return self._idempotent(connection, request_id=request_id, action="resolve_discrepancy",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 材料

    def register_material_lot(self, *, request_id: str, actor_id: str, site_id: str, lot_id: str,
                              material_code: str, qty: Any) -> Any:
        qty_value = self._qty(qty, "qty", positive=False)
        lot_id = self._identifier(lot_id, "lot_id")
        payload = {"actor_id": actor_id, "site_id": site_id, "lot_id": lot_id,
                   "material_code": material_code, "qty": str(qty_value)}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_scope(connection, actor, site_id)
            material_code = self._text(material_code, "material_code", 80)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO material_lots(lot_id,site_id,material_code,initial_qty,"
                        "remaining_qty,scale,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (lot_id, site_id, material_code, str(qty_value), str(qty_value),
                         QUANTITY_SCALE, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("材料批次号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="material_lot.registered",
                             resource_type="material_lot", resource_id=lot_id,
                             detail={"site_id": site_id, "material_code": material_code,
                                     "initial_qty": str(qty_value)}, occurred_at=now)
                return "material_lot", lot_id, {"lot_id": lot_id, "initial_qty": str(qty_value),
                                                "remaining_qty": str(qty_value)}

            return self._idempotent(connection, request_id=request_id, action="register_material_lot",
                                    payload=payload, create=create)

    def split_material(self, *, request_id: str, actor_id: str, parent_lot_id: str,
                       qty: Any, child_lot_id: str, material_code: str | None = None) -> Any:
        qty_value = self._qty(qty, "qty")
        parent_lot_id = self._identifier(parent_lot_id, "parent_lot_id")
        child_lot_id = self._identifier(child_lot_id, "child_lot_id")
        payload = {"actor_id": actor_id, "parent_lot_id": parent_lot_id, "qty": str(qty_value),
                   "child_lot_id": child_lot_id, "material_code": material_code}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            parent = connection.execute(
                "SELECT * FROM material_lots WHERE lot_id=?", (parent_lot_id,)
            ).fetchone()
            if parent is None:
                raise NotFoundError("父材料批次不存在")
            code = material_code or parent["material_code"]
            code = self._text(code, "material_code", 80)
            split_id = uuid.uuid4().hex
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                current_parent = connection.execute(
                    "SELECT * FROM material_lots WHERE lot_id=?", (parent_lot_id,)
                ).fetchone()
                if current_parent is None:
                    raise NotFoundError("父材料批次不存在")
                if connection.execute(
                        "SELECT 1 FROM material_lots WHERE lot_id=?", (child_lot_id,)).fetchone():
                    raise ConflictError("子批次号已经存在")
                if Decimal(current_parent["remaining_qty"]) < qty_value:
                    raise ConflictError("父批次剩余数量不足，不能拆分")
                cursor = connection.execute(
                    "UPDATE material_lots SET remaining_qty=? WHERE lot_id=? AND remaining_qty=?",
                    (str(Decimal(current_parent["remaining_qty"]) - qty_value), parent_lot_id,
                     current_parent["remaining_qty"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("材料批次并发变化，请重试")
                connection.execute(
                    "INSERT INTO material_lots(lot_id,site_id,material_code,initial_qty,"
                    "remaining_qty,scale,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (child_lot_id, current_parent["site_id"], code, str(qty_value), str(qty_value),
                     QUANTITY_SCALE, actor_id, now),
                )
                connection.execute(
                    "INSERT INTO material_splits(split_id,parent_lot_id,child_lot_id,qty,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (split_id, parent_lot_id, child_lot_id, str(qty_value), actor_id, now),
                )
                parent_remaining = str(Decimal(current_parent["remaining_qty"]) - qty_value)
                append_event(connection, actor_id=actor_id, action="material.split",
                             resource_type="material_lot", resource_id=child_lot_id,
                             detail={"parent_lot_id": parent_lot_id, "qty": str(qty_value),
                                     "parent_remaining": parent_remaining},
                             occurred_at=now)
                return "material_split", split_id, {"split_id": split_id, "child_lot_id": child_lot_id,
                                                    "qty": str(qty_value)}

            return self._idempotent(connection, request_id=request_id, action="split_material",
                                    payload=payload, create=create)

    def consume_material(self, *, request_id: str, actor_id: str, lot_id: str, case_id: str,
                         branch_id: str, seq: int, qty: Any) -> Any:
        qty_value = self._qty(qty, "qty")
        lot_id = self._identifier(lot_id, "lot_id")
        payload = {"actor_id": actor_id, "lot_id": lot_id, "case_id": case_id,
                   "branch_id": branch_id, "seq": seq, "qty": str(qty_value)}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            lot = connection.execute("SELECT * FROM material_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if lot is None:
                raise NotFoundError("材料批次不存在")
            self._case(connection, case_id)
            op = self._op(connection, branch_id, int(seq))
            consumption_id = uuid.uuid4().hex
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                current_op = connection.execute(
                    "SELECT status FROM route_operations WHERE operation_id=?", (op["operation_id"],)
                ).fetchone()
                if current_op["status"] not in ("active", "done"):
                    raise ConflictError("工序尚未开始，不能领料")
                current_lot = connection.execute(
                    "SELECT remaining_qty FROM material_lots WHERE lot_id=?", (lot_id,)
                ).fetchone()
                remaining = Decimal(current_lot["remaining_qty"])
                if remaining < qty_value:
                    raise ConflictError("批次剩余数量不足，不能领料")
                # 条件扣减保证并发领种下数量守恒：剩余不足或版本变化时影响 0 行。
                cursor = connection.execute(
                    "UPDATE material_lots SET remaining_qty=? WHERE lot_id=? AND remaining_qty=?",
                    (str(remaining - qty_value), lot_id, current_lot["remaining_qty"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("材料批次并发变化，领料被拒绝")
                connection.execute(
                    "INSERT INTO material_consumptions(consumption_id,lot_id,case_id,branch_id,"
                    "operation_id,qty,status,consumed_by,consumed_at) VALUES(?,?,?,?,?,?, 'consumed', ?,?)",
                    (consumption_id, lot_id, case_id, branch_id, op["operation_id"],
                     str(qty_value), actor_id, now),
                )
                product = self._current_product(connection, branch_id)
                connection.execute(
                    "INSERT OR IGNORE INTO product_inputs(product_id,kind,ref_id,recorded_at) "
                    "VALUES(?, 'consumption', ?, ?)",
                    (product["product_id"], consumption_id, now),
                )
                append_event(connection, actor_id=actor_id, action="material.consumed",
                             resource_type="material_lot", resource_id=lot_id,
                             detail={"case_id": case_id, "branch_id": branch_id, "seq": seq,
                                     "qty": str(qty_value), "consumption_id": consumption_id,
                                     "remaining": str(remaining - qty_value)},
                             occurred_at=now)
                return "material_consumption", consumption_id, {"consumption_id": consumption_id,
                                                                "lot_id": lot_id,
                                                                "qty": str(qty_value)}

            return self._idempotent(connection, request_id=request_id, action="consume_material",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 成品与返修

    def finish_product(self, *, request_id: str, actor_id: str, product_id: str) -> Any:
        payload = {"actor_id": actor_id, "product_id": product_id}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            product = connection.execute(
                "SELECT * FROM products WHERE product_id=?", (product_id,)
            ).fetchone()
            if product is None:
                raise NotFoundError("成品不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT * FROM products WHERE product_id=?", (product_id,)
                ).fetchone()
                if current["status"] != "in_progress":
                    raise ConflictError("成品不在制或已完工，不能重复完工")
                unfinished = connection.execute(
                    "SELECT COUNT(*) AS c FROM route_operations WHERE branch_id=? AND status!='done'",
                    (current["branch_id"],),
                ).fetchone()["c"]
                if unfinished:
                    raise ConflictError("分支上仍有工序未完成，不能完工")
                connection.execute(
                    "UPDATE products SET status='finished' WHERE product_id=? AND status='in_progress'",
                    (product_id,)
                )
                append_event(connection, actor_id=actor_id, action="product.finished",
                             resource_type="product", resource_id=product_id,
                             detail={"case_id": current["case_id"], "branch_id": current["branch_id"]},
                             occurred_at=self._now())
                return "product", product_id, {"product_id": product_id, "status": "finished"}

            return self._idempotent(connection, request_id=request_id, action="finish_product",
                                    payload=payload, create=create)

    def rework(self, *, request_id: str, actor_id: str, case_id: str, reason: str,
               source_product_id: str, route: list[dict[str, Any]], sku: str | None = None,
               scrap_source: bool = False) -> Any:
        if not isinstance(route, list) or not route:
            raise ValidationError("route 必须是非空工序数组")
        normalized: list[tuple[str, str]] = []
        for index, item in enumerate(route):
            if not isinstance(item, dict) or "code" not in item or "station_id" not in item:
                raise ValidationError(f"route[{index}] 必须包含 code 与 station_id")
            normalized.append((self._text(item["code"], f"route[{index}].code", 60),
                               self._identifier(item["station_id"], f"route[{index}].station_id")))
        reason = self._text(reason, "reason", 300)
        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason,
                   "source_product_id": source_product_id,
                   "route": [{"code": c, "station_id": s} for c, s in normalized], "sku": sku,
                   "scrap_source": bool(scrap_source)}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._case(connection, case_id)
            source = connection.execute(
                "SELECT * FROM products WHERE product_id=? AND case_id=?",
                (source_product_id, case_id),
            ).fetchone()
            if source is None:
                raise NotFoundError("来源成品不存在")
            version = self._current_version(connection, case_id)
            parent_branch = connection.execute(
                "SELECT * FROM case_branches WHERE branch_id=?", (source["branch_id"],)
            ).fetchone()
            branch_id = uuid.uuid4().hex
            product_id = uuid.uuid4().hex
            new_sku = self._text(sku or source["sku"], "sku", 80)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                # 原成品默认保留；只有明确要求时才标记报废，绝不覆盖。
                if scrap_source:
                    connection.execute(
                        "UPDATE products SET status='scrapped' WHERE product_id=? AND status!='delivered'",
                        (source_product_id,),
                    )
                connection.execute(
                    "INSERT INTO case_branches(branch_id,case_id,parent_branch_id,root_branch_id,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (branch_id, case_id, parent_branch["branch_id"], parent_branch["root_branch_id"],
                     reason, actor_id, now),
                )
                for seq, (code, station_id) in enumerate(normalized, start=1):
                    connection.execute(
                        "INSERT INTO route_operations(operation_id,branch_id,seq,code,station_id,"
                        "prescription_version,status,note) VALUES(?,?,?,?,?,?,'pending','')",
                        (uuid.uuid4().hex, branch_id, seq, code, station_id, version),
                    )
                connection.execute(
                    "INSERT INTO products(product_id,case_id,branch_id,parent_product_id,sku,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?, 'in_progress', ?,?)",
                    (product_id, case_id, branch_id, source_product_id, new_sku, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="branch.rework_created",
                             resource_type="case_branch", resource_id=branch_id,
                             detail={"case_id": case_id, "parent_branch_id": parent_branch["branch_id"],
                                     "source_product_id": source_product_id, "new_product_id": product_id,
                                     "reason": reason, "prescription_version": version,
                                     "source_scrapped": bool(scrap_source)}, occurred_at=now)
                return "case_branch", branch_id, {"branch_id": branch_id, "product_id": product_id,
                                                  "parent_product_id": source_product_id,
                                                  "prescription_version": version}

            return self._idempotent(connection, request_id=request_id, action="rework",
                                    payload=payload, create=create)

    def close_case(self, *, request_id: str, actor_id: str, case_id: str) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id}
        with self._unit() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._case(connection, case_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT status FROM cases WHERE case_id=?", (case_id,)
                ).fetchone()
                if current["status"] != "open":
                    raise ConflictError("病例不是开放状态或已关闭")
                open_diff = connection.execute(
                    "SELECT COUNT(*) AS c FROM discrepancies WHERE case_id=? AND status='open'", (case_id,)
                ).fetchone()["c"]
                if open_diff:
                    raise ConflictError("病例仍有未决差异，不能关闭")
                finished = connection.execute(
                    "SELECT COUNT(*) AS c FROM products WHERE case_id=? AND status='finished'", (case_id,)
                ).fetchone()["c"]
                if not finished:
                    raise ConflictError("病例尚无完工成品，不能关闭")
                connection.execute(
                    "UPDATE cases SET status='completed' WHERE case_id=? AND status='open'", (case_id,)
                )
                append_event(connection, actor_id=actor_id, action="case.completed",
                             resource_type="case", resource_id=case_id,
                             detail={"finished_products": finished}, occurred_at=self._now())
                return "case", case_id, {"case_id": case_id, "status": "completed"}

            return self._idempotent(connection, request_id=request_id, action="close_case",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 幂等回放辅助

    def _replay_ref(self, connection, resource_type: str, resource_id: str, request_id: str,
                    action: str, payload: dict[str, Any]) -> Any:
        receipt = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if receipt and (receipt["action"] != action or receipt["payload_hash"] != digest(payload)):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, resource_type, resource_id, True)

    def _optional_identifier(self, value: Any, field: str) -> str | None:
        if value is None or value == "":
            return None
        return self._identifier(value, field)

    def _lot_family(self, connection, seed_lot_ids: list[str]) -> list[str]:
        """沿拆分父链向上扩展，返回消耗批次及其全部祖先批次。"""

        family: list[str] = []
        seen: set[str] = set()
        frontier = list(seed_lot_ids)
        while frontier:
            lot_id = frontier.pop()
            if lot_id in seen:
                continue
            seen.add(lot_id)
            family.append(lot_id)
            parent = connection.execute(
                "SELECT parent_lot_id FROM material_splits WHERE child_lot_id=?", (lot_id,)
            ).fetchone()
            if parent:
                frontier.append(parent["parent_lot_id"])
        return family

    def _balance(self, connection, lot_id: str) -> dict[str, str]:
        quantum = Decimal(1).scaleb(-QUANTITY_SCALE)
        split_out = Decimal("0")
        for r in connection.execute(
                "SELECT qty FROM material_splits WHERE parent_lot_id=?", (lot_id,)):
            split_out += Decimal(r["qty"])
        consumed = Decimal("0")
        for r in connection.execute(
                "SELECT qty FROM material_consumptions WHERE lot_id=? AND status='consumed'", (lot_id,)):
            consumed += Decimal(r["qty"])
        return {"split_out_qty": str(split_out.quantize(quantum)),
                "consumed_qty": str(consumed.quantize(quantum))}

    @staticmethod
    def _lot_view(row) -> dict[str, Any]:
        item = dict(row)
        quantum = Decimal(1).scaleb(-QUANTITY_SCALE)
        item["initial_qty"] = str(Decimal(item["initial_qty"]).quantize(quantum))
        item["remaining_qty"] = str(Decimal(item["remaining_qty"]).quantize(quantum))
        return item

    # ------------------------------------------------------------------ 质量追溯

    def _reader(self, connection, actor_id: str):
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "reviewer", "auditor")
        return actor

    def get_case_ledger(self, *, actor_id: str, case_id: str) -> CaseLedger:
        with self._read_unit() as connection:
            self._reader(connection, actor_id)
            case = dict(self._case(connection, case_id))
            def rows(table: str, order: str) -> list[dict[str, Any]]:
                return [dict(r) for r in connection.execute(
                    f"SELECT * FROM {table} WHERE case_id=? ORDER BY {order}", (case_id,))]
            prescriptions = []
            for r in connection.execute(
                    "SELECT * FROM prescriptions WHERE case_id=? ORDER BY version", (case_id,)):
                item = dict(r)
                item["payload"] = json.loads(item.pop("payload_json"))
                prescriptions.append(item)
            branches = rows("case_branches", "created_at, branch_id")
            products = rows("products", "created_at, product_id")
            handoffs = rows("handoffs", "created_at, handoff_id")
            discrepancies = rows("discrepancies", "raised_at, discrepancy_id")
            branch_ids = [b["branch_id"] for b in branches]
            operations = []
            assignments = []
            if branch_ids:
                marks = ",".join("?" for _ in branch_ids)
                operations = [dict(r) for r in connection.execute(
                    f"SELECT * FROM route_operations WHERE branch_id IN ({marks}) ORDER BY branch_id, seq",
                    branch_ids)]
                op_ids = [o["operation_id"] for o in operations]
                if op_ids:
                    op_marks = ",".join("?" for _ in op_ids)
                    assignments = [dict(r) for r in connection.execute(
                        f"SELECT * FROM station_assignments WHERE operation_id IN ({op_marks}) "
                        "ORDER BY assigned_at", op_ids)]
            lot_rows = connection.execute(
                "SELECT DISTINCT lot_id FROM material_consumptions WHERE case_id=?", (case_id,)
            ).fetchall()
            seed_lots = [r["lot_id"] for r in lot_rows]
            family = self._lot_family(connection, seed_lots)
            # 同场所尚未使用的批次也一并呈现，便于质量核对库存。
            for r in connection.execute("SELECT lot_id FROM material_lots WHERE site_id=?", (case["site_id"],)):
                if r["lot_id"] not in family:
                    family.append(r["lot_id"])
            lots = []
            for lot_id in family:
                lot = connection.execute("SELECT * FROM material_lots WHERE lot_id=?", (lot_id,)).fetchone()
                if lot is not None:
                    item = self._lot_view(lot)
                    item.update(self._balance(connection, lot_id))
                    lots.append(item)
            splits, consumptions = [], []
            if family:
                lot_marks = ",".join("?" for _ in family)
                splits = [dict(r) for r in connection.execute(
                    f"SELECT * FROM material_splits WHERE parent_lot_id IN ({lot_marks}) "
                    f"OR child_lot_id IN ({lot_marks}) ORDER BY created_at", family + family)]
                consumptions = [dict(r) for r in connection.execute(
                    "SELECT * FROM material_consumptions WHERE case_id=? ORDER BY consumed_at", (case_id,))]
            case["operations"] = operations
            case["station_assignments"] = assignments
            return CaseLedger(case, prescriptions, self._routes_view(operations), branches, products,
                              handoffs, discrepancies, lots, splits, consumptions)

    @staticmethod
    def _routes_view(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for op in operations:
            grouped.setdefault(op["branch_id"], []).append(op)
        return [{"branch_id": branch_id, "operations": sorted(ops, key=lambda o: o["seq"])}
                for branch_id, ops in sorted(grouped.items())]

    def material_balance(self, *, actor_id: str, lot_id: str) -> dict[str, Any]:
        with self._read_unit() as connection:
            self._reader(connection, actor_id)
            lot = connection.execute("SELECT * FROM material_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if lot is None:
                raise NotFoundError("材料批次不存在")
            balance = self._balance(connection, lot_id)
            quantum = Decimal(1).scaleb(-QUANTITY_SCALE)
            initial = Decimal(lot["initial_qty"])
            remaining = Decimal(lot["remaining_qty"])
            accounted = remaining + Decimal(balance["split_out_qty"]) + Decimal(balance["consumed_qty"])
            return {"lot_id": lot_id, "material_code": lot["material_code"],
                    "initial_qty": str(initial.quantize(quantum)),
                    "remaining_qty": str(remaining.quantize(quantum)),
                    "split_out_qty": balance["split_out_qty"], "consumed_qty": balance["consumed_qty"],
                    "balanced": initial == accounted}

    def trace_product(self, *, actor_id: str, product_id: str) -> ProductTrace:
        with self._read_unit() as connection:
            self._reader(connection, actor_id)
            product_row = connection.execute(
                "SELECT * FROM products WHERE product_id=?", (product_id,)
            ).fetchone()
            if product_row is None:
                raise NotFoundError("成品不存在")
            product = dict(product_row)
            case_id = product["case_id"]
            operations = [dict(r) for r in connection.execute(
                "SELECT * FROM route_operations WHERE branch_id=? ORDER BY seq", (product["branch_id"],))]
            versions = sorted({o["prescription_version"] for o in operations})
            prescriptions = []
            for r in connection.execute(
                    "SELECT * FROM prescriptions WHERE case_id=? ORDER BY version", (case_id,)):
                item = dict(r)
                item["payload"] = json.loads(item.pop("payload_json"))
                if item["version"] in versions or item["version"] == self._current_version(connection, case_id):
                    prescriptions.append(item)
            input_rows = connection.execute(
                "SELECT * FROM product_inputs WHERE product_id=? ORDER BY recorded_at", (product_id,)
            ).fetchall()
            inputs = []
            final_handoff = None
            for r in input_rows:
                if r["kind"] == "handoff":
                    h = connection.execute("SELECT * FROM handoffs WHERE handoff_id=?", (r["ref_id"],)).fetchone()
                    if h:
                        hdict = dict(h)
                        inputs.append({"kind": "handoff", **hdict})
                        if hdict["seq"] == max([o["seq"] for o in operations], default=0):
                            final_handoff = hdict
                else:
                    c = connection.execute(
                        "SELECT * FROM material_consumptions WHERE consumption_id=?", (r["ref_id"],)).fetchone()
                    if c:
                        inputs.append({"kind": "consumption", **dict(c)})
            branch = dict(connection.execute(
                "SELECT * FROM case_branches WHERE branch_id=?", (product["branch_id"],)).fetchone())
            lineage = []
            current = product
            while current["parent_product_id"]:
                parent = connection.execute(
                    "SELECT * FROM products WHERE product_id=?", (current["parent_product_id"],)
                ).fetchone()
                if parent is None:
                    break
                # 返修原因记录在子产品所在分支上（即从父产品派生当前产品的原因）。
                child_branch = connection.execute(
                    "SELECT reason, branch_id FROM case_branches WHERE branch_id=?",
                    (current["branch_id"],)).fetchone()
                lineage.append({"product_id": parent["product_id"], "sku": parent["sku"],
                                "status": parent["status"], "branch_id": parent["branch_id"],
                                "reason": child_branch["reason"] if child_branch else ""})
                current = parent
            case_lot_rows = connection.execute(
                "SELECT DISTINCT lot_id FROM material_consumptions WHERE case_id=?", (case_id,)).fetchall()
            remaining_materials = []
            for lot_id in self._lot_family(connection, [r["lot_id"] for r in case_lot_rows]):
                lot = connection.execute("SELECT * FROM material_lots WHERE lot_id=?", (lot_id,)).fetchone()
                if lot is None:
                    continue
                remaining_materials.append({**self._lot_view(lot), **self._balance(connection, lot_id)})
            open_discrepancies = [dict(r) for r in connection.execute(
                "SELECT * FROM discrepancies WHERE case_id=? AND status='open' ORDER BY raised_at", (case_id,))]
            handlers = [dict(r) for r in connection.execute(
                "SELECT * FROM station_assignments WHERE operation_id IN "
                "(SELECT operation_id FROM route_operations WHERE branch_id=?) ORDER BY assigned_at",
                (product["branch_id"],))]
            product["operations"] = operations
            product["station_responsibility"] = handlers
            return ProductTrace(product, {"versions": prescriptions}, inputs, final_handoff, branch,
                                lineage, remaining_materials, open_discrepancies)
