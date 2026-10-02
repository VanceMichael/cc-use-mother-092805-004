"""生态城种质与试验治理后端门面。

一条主线：接收登记 → 谱系操作（拆分/混合/繁育/转移/失活）→ 取用审批
→ 事故冻结与处置 → 成果追溯。所有状态变化都落成不可变事件并携带当时
依据（许可快照、取用授权），已完成环节事后不受许可撤销影响。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Iterable

from .errors import (
    FrozenError,
    GovernanceError,
    LicenseError,
    NotFoundOrRestricted,
    PolicyDenied,
    QuantityError,
    SelfApprovalError,
)
from .models import (
    BIOSAFETY_OFFICER,
    OPERATOR_ROLES,
    STORAGE_RANGES,
    IncidentType,
    MaterialStatus,
    Receipt,
    RequestStatus,
)
from .policy import GENERIC_DENIAL, AccessEvaluation, evaluate_access
from .store import Store

_EPS = 1e-9


def _new_id() -> str:
    return uuid.uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(text: str) -> Any:
    return json.loads(text)


def _enum_value(value: Any) -> str:
    return getattr(value, "value", value)


class GovernanceService:
    """治理后端门面。``clock`` 可注入以便测试；任务与告警全部持久化。"""

    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], float] = time.time,
        review_sla: float = 24 * 3600.0,
        storage_recheck_delay: float = 3600.0,
    ):
        self.store = store
        self._clock = clock
        self.review_sla = review_sla
        self.storage_recheck_delay = storage_recheck_delay

    # ------------------------------------------------------------------
    # 基础登记：场地与许可
    # ------------------------------------------------------------------

    def register_site(
        self,
        *,
        name: str,
        max_biosafety: int,
        capacity: dict[str, int],
        site_id: str | None = None,
    ) -> str:
        sid = site_id or _new_id()
        self.store.execute(
            "INSERT INTO sites(id,name,max_biosafety,capacity_json,status) VALUES(?,?,?,?,'operational')",
            (sid, name, int(max_biosafety), _json(capacity)),
        )
        return sid

    def register_license(
        self,
        *,
        issuer: str,
        purposes: Iterable[str],
        orgs: Iterable[str],
        equity_shares: dict[str, float],
        derivative_owner: str,
        valid_from: float,
        valid_to: float,
        transferable: bool = False,
        license_id: str | None = None,
    ) -> str:
        lid = license_id or _new_id()
        self.store.execute(
            "INSERT INTO licenses(id,issuer,purposes_json,orgs_json,equity_json,"
            "derivative_owner,transferable,valid_from,valid_to,status)"
            " VALUES(?,?,?,?,?,?,?,?,?,'active')",
            (
                lid,
                issuer,
                _json(sorted(purposes)),
                _json(sorted(orgs)),
                _json(equity_shares),
                derivative_owner,
                1 if transferable else 0,
                float(valid_from),
                float(valid_to),
            ),
        )
        return lid

    # ------------------------------------------------------------------
    # 材料接收
    # ------------------------------------------------------------------

    @staticmethod
    def content_hash_for(
        kind: str, name: str, quality_batch: str, composition: list[dict[str, Any]]
    ) -> str:
        """材料身份与内容摘要：类别、名称、质量批次与组成共同决定。"""
        descriptor = {
            "kind": kind,
            "name": name,
            "quality_batch": quality_batch,
            "composition": composition,
        }
        return hashlib.sha256(_json(descriptor).encode("utf-8")).hexdigest()

    def expected_content_hash(self, material_id: str) -> str:
        row = self._material(material_id)
        return self.content_hash_for(
            row["kind"], row["name"], row["quality_batch"], _loads(row["composition_json"])
        )

    def intake_material(
        self,
        *,
        receipt: Receipt,
        kind: str,
        name: str,
        quality_batch: str,
        biosafety_level: int,
        storage_class: str,
        quantity: float,
        unit: str,
        site_id: str,
        custodian_org: str,
        license_id: str,
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        """接收菌株、种子、基因构件等材料。

        以来源回执为幂等键：同一回执重复提交或离线补传不会重复入账。
        """
        existing = self._existing_receipt(receipt.id)
        if existing is not None:
            return {**existing, "duplicate": True}

        kind = _enum_value(kind)
        if storage_class not in STORAGE_RANGES:
            raise ValueError(f"未知保存条件类别: {storage_class}")
        if quantity <= 0:
            raise QuantityError("接收数量必须为正")
        site = self._site(site_id)
        if site["status"] != "operational":
            raise GovernanceError("场地不可用，无法接收")
        if int(site["max_biosafety"]) < int(biosafety_level):
            raise PolicyDenied("场地生物安全等级不足以保存该材料")
        self._assert_capacity(site, storage_class)
        license_row = self._license(license_id)
        self._assert_license_active(license_row, occurred_at or self._clock())

        composition: list[dict[str, Any]] = []
        content_hash = self.content_hash_for(kind, name, quality_batch, composition)
        if receipt.content_hash is not None and receipt.content_hash != content_hash:
            raise ValueError("回执内容摘要与登记信息不一致")

        material_id = _new_id()
        now = self._clock()
        event_id = _new_id()
        basis = {
            "license_id": license_id,
            "license_status": license_row["status"],
            "license_valid_to": license_row["valid_to"],
            "receipt_id": receipt.id,
        }
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO events(id,type,occurred_at,recorded_at,actor,org,site_id,"
                "basis_json,inputs_json,outputs_json,note,content_hash)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    "intake",
                    occurred_at if occurred_at is not None else now,
                    now,
                    receipt.actor,
                    custodian_org,
                    site_id,
                    _json(basis),
                    "[]",
                    _json([{"material_id": material_id, "quantity": quantity}]),
                    "材料接收",
                    content_hash,
                ),
            )
            conn.execute(
                "INSERT INTO materials(id,kind,name,quality_batch,biosafety_level,"
                "storage_class,quantity,unit,site_id,custodian_org,license_id,"
                "composition_json,status,created_event)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                (
                    material_id,
                    kind,
                    name,
                    quality_batch,
                    int(biosafety_level),
                    storage_class,
                    float(quantity),
                    unit,
                    site_id,
                    custodian_org,
                    license_id,
                    _json(composition),
                    event_id,
                ),
            )
            self._record_receipt(
                conn,
                receipt,
                material_id=material_id,
                result={"status": "accepted", "material_id": material_id},
            )
        return {"material_id": material_id, "content_hash": content_hash, "duplicate": False}

    # ------------------------------------------------------------------
    # 谱系操作：拆分、混合、繁育、转移、失活
    # ------------------------------------------------------------------

    def split(
        self,
        *,
        actor: str,
        org: str,
        material_id: str,
        children: list[dict[str, Any]],
        occurred_at: float | None = None,
    ) -> list[str]:
        """拆分为若干子份，子份总量不得超过父份现存量。"""
        parent = self._material(material_id)
        self._assert_custodian(parent, org)
        self._assert_usable(parent)
        if not children:
            raise QuantityError("拆分至少产生一个子份")
        total = sum(float(c["quantity"]) for c in children)
        if total <= 0 or total > float(parent["quantity"]) + _EPS:
            raise QuantityError("子份总量超出现存量")

        now = self._clock()
        child_ids = [_new_id() for _ in children]
        outputs = []
        with self.store.transaction() as conn:
            for child_id, spec in zip(child_ids, children):
                qty = float(spec["quantity"])
                composition = _loads(parent["composition_json"])
                outputs.append({"material_id": child_id, "quantity": qty})
                self._insert_lot(
                    conn,
                    material_id=child_id,
                    kind=parent["kind"],
                    name=spec.get("name", parent["name"]),
                    quality_batch=spec.get("quality_batch", parent["quality_batch"]),
                    biosafety_level=parent["biosafety_level"],
                    storage_class=parent["storage_class"],
                    quantity=qty,
                    unit=parent["unit"],
                    site_id=parent["site_id"],
                    custodian_org=parent["custodian_org"],
                    license_id=parent["license_id"],
                    composition=composition,
                    created_event="",  # 事件插入后回填
                )
            event_id = self._append_event(
                conn,
                type="split",
                occurred_at=occurred_at or now,
                actor=actor,
                org=org,
                site_id=parent["site_id"],
                basis={"operation": "custodian_split"},
                inputs=[{"material_id": material_id, "quantity": total}],
                outputs=outputs,
                note="拆分",
                content_hash=None,
            )
            for child_id in child_ids:
                conn.execute(
                    "UPDATE materials SET created_event=? WHERE id=?", (event_id, child_id)
                )
            self._decrease_quantity(conn, material_id, total)
        return child_ids

    def mix(
        self,
        *,
        actor: str,
        org: str,
        inputs: list[tuple[str, float]],
        name: str,
        quality_batch: str,
        storage_class: str,
        site_id: str | None = None,
        occurred_at: float | None = None,
    ) -> str:
        """混合多份材料为一份衍生材料，组成沿谱系登记。"""
        if len(inputs) < 2:
            raise QuantityError("混合至少需要两份输入")
        rows = []
        for mid, qty in inputs:
            row = self._material(mid)
            self._assert_custodian(row, org)
            self._assert_usable(row)
            if qty <= 0 or qty > float(row["quantity"]) + _EPS:
                raise QuantityError(f"材料 {mid} 可用数量不足")
            rows.append((row, float(qty)))

        target_site = site_id or rows[0][0]["site_id"]
        site = self._site(target_site)
        self._assert_capacity(site, storage_class)
        biosafety_level = max(int(r["biosafety_level"]) for r, _ in rows)
        if int(site["max_biosafety"]) < biosafety_level:
            raise PolicyDenied("场地生物安全等级不足以保存该材料")

        composition = [
            {"material_id": row["id"], "quantity": qty} for row, qty in rows
        ]
        total = sum(qty for _, qty in rows)
        child_id = _new_id()
        now = self._clock()
        with self.store.transaction() as conn:
            event_id = self._append_event(
                conn,
                type="mix",
                occurred_at=occurred_at or now,
                actor=actor,
                org=org,
                site_id=target_site,
                basis={"operation": "custodian_mix"},
                inputs=[{"material_id": r["id"], "quantity": q} for r, q in rows],
                outputs=[{"material_id": child_id, "quantity": total}],
                note="混合",
                content_hash=self.content_hash_for(
                    rows[0][0]["kind"], name, quality_batch, composition
                ),
            )
            self._insert_lot(
                conn,
                material_id=child_id,
                kind=rows[0][0]["kind"],
                name=name,
                quality_batch=quality_batch,
                biosafety_level=biosafety_level,
                storage_class=storage_class,
                quantity=total,
                unit=rows[0][0]["unit"],
                site_id=target_site,
                custodian_org=org,
                license_id=None,  # 混合材料权益由谱系回溯计算
                composition=composition,
                created_event=event_id,
            )
            for row, qty in rows:
                self._decrease_quantity(conn, row["id"], qty)
        return child_id

    def propagate(
        self,
        *,
        actor: str,
        org: str,
        material_id: str,
        consumed: float,
        output_quantity: float,
        name: str | None = None,
        quality_batch: str | None = None,
        occurred_at: float | None = None,
    ) -> str:
        """繁育/扩繁：消耗少量母本，产出可大于投入。"""
        parent = self._material(material_id)
        self._assert_custodian(parent, org)
        self._assert_usable(parent)
        if consumed <= 0 or consumed > float(parent["quantity"]) + _EPS:
            raise QuantityError("消耗数量无效")
        if output_quantity <= 0:
            raise QuantityError("繁育产出必须为正")
        self._assert_capacity(self._site(parent["site_id"]), parent["storage_class"])

        child_id = _new_id()
        now = self._clock()
        composition = _loads(parent["composition_json"])
        with self.store.transaction() as conn:
            event_id = self._append_event(
                conn,
                type="propagate",
                occurred_at=occurred_at or now,
                actor=actor,
                org=org,
                site_id=parent["site_id"],
                basis={"operation": "custodian_propagate"},
                inputs=[{"material_id": material_id, "quantity": consumed}],
                outputs=[{"material_id": child_id, "quantity": output_quantity}],
                note="繁育",
                content_hash=self.content_hash_for(
                    parent["kind"],
                    name or parent["name"],
                    quality_batch or parent["quality_batch"],
                    composition,
                ),
            )
            self._insert_lot(
                conn,
                material_id=child_id,
                kind=parent["kind"],
                name=name or parent["name"],
                quality_batch=quality_batch or parent["quality_batch"],
                biosafety_level=parent["biosafety_level"],
                storage_class=parent["storage_class"],
                quantity=output_quantity,
                unit=parent["unit"],
                site_id=parent["site_id"],
                custodian_org=parent["custodian_org"],
                license_id=parent["license_id"],
                composition=composition,
                created_event=event_id,
            )
            self._decrease_quantity(conn, material_id, consumed)
        return child_id

    def transfer(
        self,
        *,
        actor: str,
        org: str,
        material_id: str,
        quantity: float,
        to_org: str,
        to_site_id: str,
        access_request_id: str,
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        """跨机构转移：必须持有覆盖本次转移的已批准取用授权。"""
        row = self._material(material_id)
        self._assert_custodian(row, org)
        self._assert_usable(row)
        if quantity <= 0 or quantity > float(row["quantity"]) + _EPS:
            raise QuantityError("转移数量超出现存量")

        req = self._access_request(access_request_id)
        if req["material_id"] != material_id or req["org"] != to_org:
            raise PolicyDenied("取用授权与本次转移不匹配")
        if req["status"] not in (
            RequestStatus.APPROVED.value,
            RequestStatus.EXCEPTION_APPROVED.value,
        ):
            raise PolicyDenied("取用未获批准")
        if req["purpose"] != "transfer":
            raise PolicyDenied("取用授权用途不是转移")
        if quantity > float(req["max_quantity"]) + _EPS:
            raise QuantityError("转移数量超出授权范围")

        to_site = self._site(to_site_id)
        if to_site["status"] != "operational":
            raise GovernanceError("目标场地不可用")
        if int(to_site["max_biosafety"]) < int(row["biosafety_level"]):
            raise PolicyDenied("目标场地生物安全等级不足")
        self._assert_capacity(to_site, row["storage_class"])

        child_id = _new_id()
        now = self._clock()
        composition = _loads(row["composition_json"])
        content_hash = self.content_hash_for(
            row["kind"], row["name"], row["quality_batch"], composition
        )
        basis = {
            "access_request_id": access_request_id,
            "decision": req["status"],
            "approver": req["approver"],
            "license_id": row["license_id"],
            "decided_at": req["decided_at"] or req["created_at"],
        }
        with self.store.transaction() as conn:
            event_id = self._append_event(
                conn,
                type="transfer",
                occurred_at=occurred_at or now,
                actor=actor,
                org=org,
                site_id=to_site_id,
                basis=basis,
                inputs=[{"material_id": material_id, "quantity": quantity}],
                outputs=[{"material_id": child_id, "quantity": quantity}],
                note=f"转移至 {to_org}",
                content_hash=content_hash,
            )
            self._insert_lot(
                conn,
                material_id=child_id,
                kind=row["kind"],
                name=row["name"],
                quality_batch=row["quality_batch"],
                biosafety_level=row["biosafety_level"],
                storage_class=row["storage_class"],
                quantity=quantity,
                unit=row["unit"],
                site_id=to_site_id,
                custodian_org=to_org,
                license_id=row["license_id"],
                composition=composition,
                created_event=event_id,
            )
            self._decrease_quantity(conn, material_id, quantity)
        return {
            "material_id": child_id,
            "transfer_event_id": event_id,
            "expected_content_hash": content_hash,
        }

    def deactivate(
        self,
        *,
        actor: str,
        org: str,
        material_id: str,
        quantity: float,
        reason: str,
        occurred_at: float | None = None,
    ) -> None:
        """失活：登记数量与原因。冻结中的材料也允许失活（处置动作）。"""
        row = self._material(material_id)
        self._assert_custodian(row, org)
        if row["status"] == MaterialStatus.DEACTIVATED.value:
            raise GovernanceError("材料已整体失活")
        if quantity <= 0 or quantity > float(row["quantity"]) + _EPS:
            raise QuantityError("失活数量超出现存量")
        now = self._clock()
        with self.store.transaction() as conn:
            self._append_event(
                conn,
                type="deactivate",
                occurred_at=occurred_at or now,
                actor=actor,
                org=org,
                site_id=row["site_id"],
                basis={"operation": "deactivate", "reason": reason},
                inputs=[{"material_id": material_id, "quantity": quantity}],
                outputs=[],
                note=f"失活：{reason}",
                content_hash=None,
            )
            self._decrease_quantity(conn, material_id, quantity)
            remaining = float(row["quantity"]) - quantity
            if remaining <= _EPS:
                conn.execute(
                    "UPDATE materials SET status='deactivated' WHERE id=?", (material_id,)
                )

    # ------------------------------------------------------------------
    # 取用申请与例外复核
    # ------------------------------------------------------------------

    def request_access(
        self,
        *,
        submitter: str,
        org: str,
        qualifications: Iterable[str],
        purpose: str,
        material_id: str,
        quantity: float,
        site_id: str,
    ) -> dict[str, Any]:
        """评估取用申请，给出可执行范围。

        标准检查全部通过且无风险标记的自动批准；带风险标记的进入
        待复核；许可未覆盖的机构得到与“材料不存在”相同的拒绝。
        """
        now = self._clock()
        row = self.store.one("SELECT * FROM materials WHERE id=?", (material_id,))
        if row is None:
            evaluation = AccessEvaluation(visible=False, reasons=[GENERIC_DENIAL])
        else:
            site = self.store.one("SELECT * FROM sites WHERE id=?", (site_id,))
            occupancy = (
                self._occupancy(site_id, row["storage_class"]) if site else 0
            )
            evaluation = evaluate_access(
                material=row,
                licenses=self._effective_licenses(row),
                site=site,
                occupancy=occupancy,
                purpose=purpose,
                quantity=quantity,
                qualifications=set(qualifications),
                org=org,
                now=now,
            )

        if not evaluation.visible or not evaluation.approved:
            status = RequestStatus.DENIED.value
        elif evaluation.risk_flags:
            status = RequestStatus.NEEDS_EXCEPTION.value
        else:
            status = RequestStatus.APPROVED.value

        request_id = _new_id()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO access_requests(id,submitter,org,qualifications_json,"
                "purpose,material_id,quantity,site_id,status,risk_flags_json,"
                "reasons_json,max_quantity,approver,created_at,decided_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    submitter,
                    org,
                    _json(sorted(qualifications)),
                    purpose,
                    material_id,
                    float(quantity),
                    site_id,
                    status,
                    _json(evaluation.risk_flags if evaluation.visible else []),
                    _json(evaluation.reasons),
                    evaluation.max_quantity if evaluation.visible else 0.0,
                    None,
                    now,
                    now if status == RequestStatus.APPROVED.value else None,
                ),
            )
            if status == RequestStatus.NEEDS_EXCEPTION.value:
                self._schedule_job(
                    conn,
                    "review_reminder",
                    now + self.review_sla,
                    {"request_id": request_id},
                )
        return self.get_access_request(request_id, org=org, roles=set())

    def get_access_request(
        self, request_id: str, *, org: str, roles: set[str]
    ) -> dict[str, Any]:
        req = self._access_request(request_id)
        if org != req["org"] and not (OPERATOR_ROLES & roles):
            raise NotFoundOrRestricted("取用申请不存在或不在授权范围")
        return self._request_view(req)

    def approve_exception(
        self, *, request_id: str, approver: str, approver_roles: Iterable[str]
    ) -> dict[str, Any]:
        """高风险例外复核：不得由方案提交人自行批准。"""
        req = self._access_request(request_id)
        if req["status"] != RequestStatus.NEEDS_EXCEPTION.value:
            raise PolicyDenied("该申请不在待复核状态")
        if approver == req["submitter"]:
            raise SelfApprovalError("高风险例外不得由方案提交人自行批准")
        if BIOSAFETY_OFFICER not in set(approver_roles):
            raise PolicyDenied("复核人须具备生物安全审核资质")
        now = self._clock()
        self.store.execute(
            "UPDATE access_requests SET status=?, approver=?, decided_at=? WHERE id=?",
            (RequestStatus.EXCEPTION_APPROVED.value, approver, now, request_id),
        )
        return self._request_view(self._access_request(request_id))

    def deny_exception(
        self, *, request_id: str, approver: str, approver_roles: Iterable[str]
    ) -> dict[str, Any]:
        req = self._access_request(request_id)
        if req["status"] != RequestStatus.NEEDS_EXCEPTION.value:
            raise PolicyDenied("该申请不在待复核状态")
        if approver == req["submitter"]:
            raise SelfApprovalError("高风险例外不得由方案提交人自行处置")
        if BIOSAFETY_OFFICER not in set(approver_roles):
            raise PolicyDenied("复核人须具备生物安全审核资质")
        now = self._clock()
        self.store.execute(
            "UPDATE access_requests SET status=?, approver=?, decided_at=? WHERE id=?",
            (RequestStatus.EXCEPTION_DENIED.value, approver, now, request_id),
        )
        return self._request_view(self._access_request(request_id))

    # ------------------------------------------------------------------
    # 可见性与检索：未授权机构无法推断受限资源存在
    # ------------------------------------------------------------------

    def search_materials(
        self,
        *,
        org: str,
        roles: Iterable[str] = (),
        kind: str | None = None,
        name_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        """按可见性过滤的检索。受限材料被整体排除，不返回任何聚合计数。"""
        rows = self.store.query("SELECT * FROM materials ORDER BY rowid")
        result = []
        for row in rows:
            if not self._visible_to(row, org, set(roles)):
                continue
            if kind and row["kind"] != kind:
                continue
            if name_contains and name_contains not in row["name"]:
                continue
            result.append(self._material_view(row))
        return result

    def get_material(self, material_id: str, *, org: str, roles: Iterable[str] = ()) -> dict[str, Any]:
        row = self._material(material_id)
        if not self._visible_to(row, org, set(roles)):
            raise NotFoundOrRestricted(GENERIC_DENIAL)
        view = self._material_view(row)
        view["open_freezes"] = len(self._open_freezes("material", material_id))
        return view

    # ------------------------------------------------------------------
    # 事故处置：局部冻结、处置责任、已完成环节保留依据
    # ------------------------------------------------------------------

    def report_incident(
        self,
        *,
        type: str,
        reporter: str,
        material_ids: Iterable[str] = (),
        site_id: str | None = None,
        license_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        """登记污染、丢失、许可撤销或场地故障，只冻结受影响对象。"""
        incident_type = _enum_value(type)
        now = self._clock()
        incident_id = _new_id()
        detail = dict(detail or {})
        affected: set[str] = set()
        actions: dict[str, str] = {}

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO incidents(id,type,reporter,detail_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    incident_id,
                    incident_type,
                    reporter,
                    _json(
                        {
                            "material_ids": sorted(material_ids),
                            "site_id": site_id,
                            "license_id": license_id,
                            **detail,
                        }
                    ),
                    now,
                ),
            )

            if incident_type == IncidentType.CONTAMINATION.value:
                affected = self._descendants(set(material_ids))
                for mid in affected:
                    actions[mid] = "污染核查与处置"
            elif incident_type == IncidentType.LOSS.value:
                lost = detail.get("lost", {})
                for mid in material_ids:
                    row = self._material(mid)
                    lost_qty = float(lost.get(mid, row["quantity"]))
                    if lost_qty > float(row["quantity"]) + _EPS:
                        raise QuantityError("丢失数量超出现存量")
                    self._append_event(
                        conn,
                        type="deactivate",
                        occurred_at=now,
                        actor=reporter,
                        org=row["custodian_org"],
                        site_id=row["site_id"],
                        basis={"operation": "incident_loss", "incident_id": incident_id},
                        inputs=[{"material_id": mid, "quantity": lost_qty}],
                        outputs=[],
                        note="丢失核销",
                        content_hash=None,
                    )
                    self._decrease_quantity(conn, mid, lost_qty)
                    affected.add(mid)
                    actions[mid] = "丢失调查与核销"
            elif incident_type == IncidentType.LICENSE_REVOKED.value:
                if license_id is None:
                    raise LicenseError("许可撤销事故必须指定许可")
                lic = self._license(license_id)
                conn.execute(
                    "UPDATE licenses SET status='revoked' WHERE id=?", (license_id,)
                )
                seeded = {
                    r["id"]
                    for r in self.store.query(
                        "SELECT id FROM materials WHERE license_id=?", (license_id,)
                    )
                }
                affected = self._descendants(seeded)
                for mid in affected:
                    actions[mid] = "许可撤销处置"
            elif incident_type == IncidentType.SITE_FAILURE.value:
                if site_id is None:
                    raise GovernanceError("场地故障事故必须指定场地")
                self._site(site_id)
                conn.execute(
                    "UPDATE sites SET status='failed' WHERE id=?", (site_id,)
                )
                affected = {
                    r["id"]
                    for r in self.store.query(
                        "SELECT id FROM materials WHERE site_id=? AND status='active'",
                        (site_id,),
                    )
                }
                for mid in affected:
                    actions[mid] = "场地故障材料转移"
            else:
                raise ValueError(f"未知事故类型: {incident_type}")

            for mid in sorted(affected):
                row = self.store.one("SELECT * FROM materials WHERE id=?", (mid,))
                if row is None or row["status"] == MaterialStatus.DEACTIVATED.value:
                    continue
                self._freeze(conn, "material", mid, actions[mid], incident_id, now)
                conn.execute(
                    "INSERT INTO disposal_tasks(id,incident_id,material_id,"
                    "assignee_org,action,status,created_at) VALUES(?,?,?,?,?,'open',?)",
                    (_new_id(), incident_id, mid, row["custodian_org"], actions[mid], now),
                )
            for trial in self._trials_using(conn, affected):
                self._freeze(conn, "trial", trial["id"], "关联材料受影响", incident_id, now)
        return incident_id

    def release_freeze(
        self,
        *,
        freeze_id: str,
        actor: str,
        roles: Iterable[str],
        rationale: str,
    ) -> None:
        """解除冻结：仅园区运营或生物安全审核人员可操作，并记录理由。"""
        if not (OPERATOR_ROLES & set(roles)):
            raise PolicyDenied("解冻需要园区运营或生物安全审核资质")
        freeze = self.store.one("SELECT * FROM freezes WHERE id=?", (freeze_id,))
        if freeze is None or freeze["released_at"] is not None:
            raise NotFoundOrRestricted("冻结记录不存在或已解除")
        now = self._clock()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE freezes SET released_at=?, released_by=?, release_rationale=?"
                " WHERE id=?",
                (now, actor, rationale, freeze_id),
            )
            target_type, target_id = freeze["target_type"], freeze["target_id"]
            if not self._open_freezes(target_type, target_id, conn=conn):
                table = "materials" if target_type == "material" else "trials"
                conn.execute(
                    f"UPDATE {table} SET status='active' WHERE id=? AND status IN ('frozen','suspended')",
                    (target_id,),
                )

    def complete_disposal(self, *, task_id: str, actor: str, note: str = "") -> None:
        task = self.store.one("SELECT * FROM disposal_tasks WHERE id=?", (task_id,))
        if task is None:
            raise NotFoundOrRestricted("处置任务不存在")
        self.store.execute(
            "UPDATE disposal_tasks SET status='done', completed_at=?, completed_by=?,"
            " note=? WHERE id=?",
            (self._clock(), actor, note, task_id),
        )

    def incident_report(self, incident_id: str) -> dict[str, Any]:
        incident = self.store.one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if incident is None:
            raise NotFoundOrRestricted("事故记录不存在")
        return {
            "incident": {**incident, "detail": _loads(incident["detail_json"])},
            "freezes": self.store.query(
                "SELECT * FROM freezes WHERE incident_id=?", (incident_id,)
            ),
            "disposal_tasks": self.store.query(
                "SELECT * FROM disposal_tasks WHERE incident_id=?", (incident_id,)
            ),
        }

    # ------------------------------------------------------------------
    # 回执：幂等入账与数量/身份/内容核对
    # ------------------------------------------------------------------

    def submit_receipt(self, receipt: Receipt) -> dict[str, Any]:
        """接收扫描或交接回执。

        重复提交与离线补传返回首次处理结果，不重复入账、不重复告警。
        """
        existing = self._existing_receipt(receipt.id)
        if existing is not None:
            return {**existing, "duplicate": True}

        if receipt.type == "handover":
            result = self._reconcile_handover(receipt)
        elif receipt.type == "scan":
            result = self._reconcile_scan(receipt)
        else:
            result = {"status": "recorded"}
        with self.store.transaction() as conn:
            self._record_receipt(conn, receipt, material_id=receipt.material_id, result=result)
        return {**result, "duplicate": False}

    # ------------------------------------------------------------------
    # 保存条件、持久任务与试验随访
    # ------------------------------------------------------------------

    def record_condition(
        self,
        *,
        site_id: str,
        temperature: float,
        humidity: float | None = None,
        at: float | None = None,
    ) -> dict[str, Any]:
        """登记场地环境读数，越限立即告警并安排复核任务。"""
        self._site(site_id)
        now = self._clock()
        recorded_at = at if at is not None else now
        violations = self._condition_violations(site_id, temperature)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO condition_readings(id,site_id,recorded_at,temperature,humidity)"
                " VALUES(?,?,?,?,?)",
                (_new_id(), site_id, recorded_at, float(temperature), humidity),
            )
            for row in violations:
                self._alert(
                    conn,
                    "storage_condition",
                    f"场地 {site_id} 温度 {temperature} 超出材料 {row['id']} 保存条件",
                    {"site_id": site_id, "material_id": row["id"]},
                    now,
                )
            if violations:
                self._schedule_job(
                    conn,
                    "storage_recheck",
                    now + self.storage_recheck_delay,
                    {"site_id": site_id},
                )
        return {"violations": [row["id"] for row in violations]}

    def schedule_job(self, *, type: str, due_at: float, payload: dict[str, Any]) -> str:
        with self.store.transaction() as conn:
            return self._schedule_job(conn, type, due_at, payload)

    def run_due_jobs(self, now: float | None = None) -> list[dict[str, Any]]:
        """执行到期任务。任务持久化在库中，进程重启后由新实例继续执行。"""
        now = self._clock() if now is None else now
        rows = self.store.query(
            "SELECT * FROM jobs WHERE status='pending' AND due_at<=? ORDER BY due_at",
            (now,),
        )
        results = []
        for row in rows:
            try:
                outcome = self._run_job(row["type"], _loads(row["payload_json"]), now)
            except Exception as exc:  # 任务失败保留现场，允许重试
                attempts = int(row["attempts"]) + 1
                status = "failed" if attempts >= 3 else "pending"
                self.store.execute(
                    "UPDATE jobs SET status=?, attempts=?, last_error=? WHERE id=?",
                    (status, attempts, str(exc), row["id"]),
                )
                results.append(
                    {"job_id": row["id"], "type": row["type"], "status": status, "error": str(exc)}
                )
            else:
                self.store.execute(
                    "UPDATE jobs SET status='done', attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
                results.append(
                    {"job_id": row["id"], "type": row["type"], "status": "done", "outcome": outcome}
                )
        return results

    def register_trial(
        self,
        *,
        org: str,
        plan_ref: str,
        site_id: str,
        access_request_id: str,
        follow_up_interval: float | None = None,
    ) -> str:
        """登记试验：必须基于已批准的取用授权；随访任务持久化。"""
        req = self._access_request(access_request_id)
        if req["org"] != org:
            raise PolicyDenied("试验机构与取用授权不一致")
        if req["status"] not in (
            RequestStatus.APPROVED.value,
            RequestStatus.EXCEPTION_APPROVED.value,
        ):
            raise PolicyDenied("取用未获批准，不能登记试验")
        trial_id = _new_id()
        now = self._clock()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO trials(id,org,plan_ref,site_id,access_request_id,"
                "follow_up_interval,status,created_at) VALUES(?,?,?,?,?,?,'active',?)",
                (trial_id, org, plan_ref, site_id, access_request_id, follow_up_interval, now),
            )
            if follow_up_interval:
                self._schedule_job(
                    conn, "trial_followup", now + follow_up_interval, {"trial_id": trial_id}
                )
        return trial_id

    def complete_trial(self, *, trial_id: str, org: str) -> None:
        trial = self.store.one("SELECT * FROM trials WHERE id=?", (trial_id,))
        if trial is None or trial["org"] != org:
            raise NotFoundOrRestricted("试验不存在或不在授权范围")
        self.store.execute(
            "UPDATE trials SET status='completed' WHERE id=?", (trial_id,)
        )

    def register_outcome(self, *, trial_id: str, description: str) -> str:
        trial = self.store.one("SELECT * FROM trials WHERE id=?", (trial_id,))
        if trial is None:
            raise NotFoundOrRestricted("试验不存在")
        outcome_id = _new_id()
        self.store.execute(
            "INSERT INTO outcomes(id,trial_id,description,created_at) VALUES(?,?,?,?)",
            (outcome_id, trial_id, description, self._clock()),
        )
        return outcome_id

    # ------------------------------------------------------------------
    # 追溯：从成果回到原始材料、每次转移与适用权益
    # ------------------------------------------------------------------

    def trace(self, *, material_id: str, roles: Iterable[str]) -> dict[str, Any]:
        """回溯材料的完整谱系。仅管理人员可用。"""
        if not (OPERATOR_ROLES & set(roles)):
            raise NotFoundOrRestricted(GENERIC_DENIAL)
        row = self._material(material_id)
        events = self._lineage_events(material_id)
        roots = self._root_material_ids(material_id)
        licenses = []
        for lic in self._effective_licenses(row):
            licenses.append(
                {
                    "id": lic["id"],
                    "issuer": lic["issuer"],
                    "status": lic["status"],
                    "equity_shares": _loads(lic["equity_json"]),
                    "derivative_owner": lic["derivative_owner"],
                }
            )
        return {
            "material": self._material_view(row),
            "events": [self._event_view(e) for e in events],
            "roots": sorted(roots),
            "transfers": [self._event_view(e) for e in events if e["type"] == "transfer"],
            "licenses": licenses,
        }

    def trace_outcome(self, outcome_id: str, *, roles: Iterable[str]) -> dict[str, Any]:
        """从成果追溯到原始材料、每次转移以及适用的合作权益。"""
        outcome = self.store.one("SELECT * FROM outcomes WHERE id=?", (outcome_id,))
        if outcome is None:
            raise NotFoundOrRestricted("成果不存在")
        trial = self.store.one("SELECT * FROM trials WHERE id=?", (outcome["trial_id"],))
        req = self._access_request(trial["access_request_id"])
        return {
            "outcome": dict(outcome),
            "trial": dict(trial),
            "lineage": self.trace(material_id=req["material_id"], roles=roles),
            "equity": self.equity_report(material_id=req["material_id"], roles=roles),
        }

    def equity_report(
        self, *, material_id: str, roles: Iterable[str]
    ) -> list[dict[str, Any]]:
        """按谱系数量权重回溯到各原始材料，汇总其许可载明的合作权益。"""
        if not (OPERATOR_ROLES & set(roles)):
            raise NotFoundOrRestricted(GENERIC_DENIAL)
        self._material(material_id)
        created_by = self._created_by_index()
        contributions: dict[str, float] = {}

        def walk(mid: str, weight: float) -> None:
            event = created_by.get(mid)
            if event is None or event["type"] == "intake":
                contributions[mid] = contributions.get(mid, 0.0) + weight
                return
            inputs = _loads(event["inputs_json"])
            outputs = _loads(event["outputs_json"])
            out_total = sum(o["quantity"] for o in outputs)
            out_qty = next(o["quantity"] for o in outputs if o["material_id"] == mid)
            in_total = sum(i["quantity"] for i in inputs)
            if out_total <= 0 or in_total <= 0:
                return
            for i in inputs:
                walk(i["material_id"], weight * (out_qty / out_total) * (i["quantity"] / in_total))

        walk(material_id, 1.0)
        report = []
        for root_id, weight in sorted(contributions.items()):
            root = self.store.one("SELECT * FROM materials WHERE id=?", (root_id,))
            lic = (
                self.store.one("SELECT * FROM licenses WHERE id=?", (root["license_id"],))
                if root and root["license_id"]
                else None
            )
            report.append(
                {
                    "root_material_id": root_id,
                    "root_name": root["name"] if root else None,
                    "weight": weight,
                    "license_id": lic["id"] if lic else None,
                    "equity_shares": _loads(lic["equity_json"]) if lic else {},
                    "derivative_owner": lic["derivative_owner"] if lic else None,
                }
            )
        return report

    def quantity_ledger(self, material_id: str) -> dict[str, Any]:
        """数量台账：按事件重算并与当前记录核对，验证剩余与失活数量。"""
        self._material(material_id)
        received = consumed = deactivated = 0.0
        for event in self.store.query("SELECT * FROM events"):
            for out in _loads(event["outputs_json"]):
                if out["material_id"] == material_id:
                    received += out["quantity"]
            for inp in _loads(event["inputs_json"]):
                if inp["material_id"] == material_id:
                    consumed += inp["quantity"]
                    if event["type"] == "deactivate":
                        deactivated += inp["quantity"]
        row = self._material(material_id)
        remaining = received - consumed
        return {
            "material_id": material_id,
            "received": received,
            "consumed": consumed,
            "deactivated": deactivated,
            "remaining": remaining,
            "recorded": float(row["quantity"]),
            "consistent": abs(remaining - float(row["quantity"])) <= _EPS,
        }

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def list_alerts(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM alerts ORDER BY created_at, id")
        return [{**r, "ref": _loads(r["ref_json"])} for r in rows]

    def open_freezes(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM freezes WHERE released_at IS NULL")

    def pending_disposals(self, *, org: str | None = None) -> list[dict[str, Any]]:
        if org is None:
            return self.store.query("SELECT * FROM disposal_tasks WHERE status='open'")
        return self.store.query(
            "SELECT * FROM disposal_tasks WHERE status='open' AND assignee_org=?", (org,)
        )

    def list_jobs(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            return self.store.query("SELECT * FROM jobs ORDER BY due_at")
        return self.store.query("SELECT * FROM jobs WHERE status=? ORDER BY due_at", (status,))

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _now(self) -> float:
        return float(self._clock())

    def _site(self, site_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM sites WHERE id=?", (site_id,))
        if row is None:
            raise NotFoundOrRestricted("场地不存在")
        return row

    def _license(self, license_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM licenses WHERE id=?", (license_id,))
        if row is None:
            raise LicenseError("许可不存在")
        return row

    def _material(self, material_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM materials WHERE id=?", (material_id,))
        if row is None:
            raise NotFoundOrRestricted(GENERIC_DENIAL)
        return row

    def _access_request(self, request_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM access_requests WHERE id=?", (request_id,))
        if row is None:
            raise NotFoundOrRestricted("取用申请不存在")
        return row

    @staticmethod
    def _assert_license_active(license_row: dict[str, Any], at: float) -> None:
        if license_row["status"] != "active":
            raise LicenseError("许可已撤销")
        if not license_row["valid_from"] <= at <= license_row["valid_to"]:
            raise LicenseError("许可不在有效期")

    def _assert_capacity(self, site: dict[str, Any], storage_class: str) -> None:
        capacity = _loads(site["capacity_json"])
        if self._occupancy(site["id"], storage_class) >= int(capacity.get(storage_class, 0)):
            raise GovernanceError("场地容量不足")

    def _occupancy(self, site_id: str, storage_class: str) -> int:
        row = self.store.one(
            "SELECT COUNT(*) AS n FROM materials WHERE site_id=? AND storage_class=?"
            " AND status='active'",
            (site_id, storage_class),
        )
        return int(row["n"])

    @staticmethod
    def _assert_custodian(row: dict[str, Any], org: str) -> None:
        if row["custodian_org"] != org:
            raise PolicyDenied("只有保管机构可以执行该操作")

    def _assert_usable(self, row: dict[str, Any]) -> None:
        if row["status"] != MaterialStatus.ACTIVE.value:
            raise FrozenError("材料不在可用状态")
        if self._open_freezes("material", row["id"]):
            raise FrozenError("材料已冻结")

    def _visible_to(self, row: dict[str, Any], org: str, roles: set[str]) -> bool:
        if OPERATOR_ROLES & roles:
            return True
        if row["custodian_org"] == org:
            return True
        if row["license_id"]:
            lic = self.store.one("SELECT orgs_json FROM licenses WHERE id=?", (row["license_id"],))
            return lic is not None and org in _loads(lic["orgs_json"])
        return False

    def _effective_licenses(self, material_row: dict[str, Any]) -> list[dict[str, Any]]:
        """材料适用的许可：直接许可，或混合材料各原始来源的许可。"""
        if material_row["license_id"]:
            lic = self.store.one(
                "SELECT * FROM licenses WHERE id=?", (material_row["license_id"],)
            )
            return [lic] if lic else []
        licenses: list[dict[str, Any]] = []
        for root_id in self._root_material_ids(material_row["id"]):
            root = self.store.one("SELECT license_id FROM materials WHERE id=?", (root_id,))
            if root and root["license_id"]:
                lic = self.store.one(
                    "SELECT * FROM licenses WHERE id=?", (root["license_id"],)
                )
                if lic and lic["id"] not in {l["id"] for l in licenses}:
                    licenses.append(lic)
        return licenses

    def _created_by_index(self) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for event in self.store.query("SELECT * FROM events ORDER BY seq"):
            for out in _loads(event["outputs_json"]):
                index[out["material_id"]] = event
        return index

    def _root_material_ids(self, material_id: str) -> list[str]:
        created_by = self._created_by_index()
        roots: list[str] = []
        stack = [material_id]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            event = created_by.get(current)
            if event is None or event["type"] == "intake":
                roots.append(current)
                continue
            for inp in _loads(event["inputs_json"]):
                stack.append(inp["material_id"])
        return roots

    def _descendants(self, seeds: set[str]) -> set[str]:
        children_of: dict[str, set[str]] = {}
        for event in self.store.query("SELECT * FROM events"):
            outputs = _loads(event["outputs_json"])
            for inp in _loads(event["inputs_json"]):
                for out in outputs:
                    children_of.setdefault(inp["material_id"], set()).add(out["material_id"])
        seen = set(seeds)
        stack = list(seeds)
        while stack:
            current = stack.pop()
            for child in children_of.get(current, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    def _ancestors(self, material_id: str) -> set[str]:
        """材料及其全部上游来源（含中间环节）。"""
        created_by = self._created_by_index()
        seen: set[str] = set()
        stack = [material_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            event = created_by.get(current)
            if event is not None and event["type"] != "intake":
                for inp in _loads(event["inputs_json"]):
                    stack.append(inp["material_id"])
        return seen

    def _lineage_events(self, material_id: str) -> list[dict[str, Any]]:
        family = self._ancestors(material_id) | self._descendants({material_id})
        events = []
        for event in self.store.query("SELECT * FROM events ORDER BY seq"):
            ids = {e["material_id"] for e in _loads(event["inputs_json"])} | {
                e["material_id"] for e in _loads(event["outputs_json"])
            }
            if ids & family:
                events.append(event)
        return events

    def _trials_using(self, conn: Any, material_ids: set[str]) -> list[dict[str, Any]]:
        if not material_ids:
            return []
        trials = []
        for trial in conn.execute("SELECT * FROM trials WHERE status='active'").fetchall():
            req = self.store.one(
                "SELECT material_id FROM access_requests WHERE id=?",
                (trial["access_request_id"],),
            )
            if req and req["material_id"] in material_ids:
                trials.append(dict(trial))
        return trials

    def _open_freezes(
        self, target_type: str, target_id: str, conn: Any = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM freezes WHERE target_type=? AND target_id=? AND released_at IS NULL"
        )
        params = (target_type, target_id)
        if conn is not None:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
        return self.store.query(query, params)

    def _freeze(
        self, conn: Any, target_type: str, target_id: str, reason: str, incident_id: str, now: float
    ) -> None:
        conn.execute(
            "INSERT INTO freezes(id,target_type,target_id,reason,incident_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (_new_id(), target_type, target_id, reason, incident_id, now),
        )
        if target_type == "material":
            conn.execute(
                "UPDATE materials SET status='frozen' WHERE id=? AND status='active'",
                (target_id,),
            )
        elif target_type == "trial":
            conn.execute(
                "UPDATE trials SET status='suspended' WHERE id=? AND status='active'",
                (target_id,),
            )

    def _append_event(
        self,
        conn: Any,
        *,
        type: str,
        occurred_at: float,
        actor: str,
        org: str,
        site_id: str | None,
        basis: dict[str, Any],
        inputs: list[dict[str, Any]],
        outputs: list[dict[str, Any]],
        note: str,
        content_hash: str | None,
    ) -> str:
        event_id = _new_id()
        conn.execute(
            "INSERT INTO events(id,type,occurred_at,recorded_at,actor,org,site_id,"
            "basis_json,inputs_json,outputs_json,note,content_hash)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                type,
                float(occurred_at),
                self._now(),
                actor,
                org,
                site_id,
                _json(basis),
                _json(inputs),
                _json(outputs),
                note,
                content_hash,
            ),
        )
        return event_id

    def _insert_lot(self, conn: Any, *, created_event: str, composition: list, **fields: Any) -> None:
        conn.execute(
            "INSERT INTO materials(id,kind,name,quality_batch,biosafety_level,"
            "storage_class,quantity,unit,site_id,custodian_org,license_id,"
            "composition_json,status,created_event)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (
                fields["material_id"],
                fields["kind"],
                fields["name"],
                fields["quality_batch"],
                int(fields["biosafety_level"]),
                fields["storage_class"],
                float(fields["quantity"]),
                fields["unit"],
                fields["site_id"],
                fields["custodian_org"],
                fields["license_id"],
                _json(composition),
                created_event,
            ),
        )

    @staticmethod
    def _decrease_quantity(conn: Any, material_id: str, quantity: float) -> None:
        conn.execute(
            "UPDATE materials SET quantity=quantity-? WHERE id=?", (float(quantity), material_id)
        )

    def _existing_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT result_json FROM receipts WHERE id=?", (receipt_id,))
        return _loads(row["result_json"]) if row else None

    def _record_receipt(
        self, conn: Any, receipt: Receipt, *, material_id: str | None, result: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO receipts(id,type,actor,org,material_id,quantity,unit,"
            "content_hash,occurred_at,recorded_at,payload_json,result_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt.id,
                receipt.type,
                receipt.actor,
                receipt.org,
                material_id,
                receipt.quantity,
                receipt.unit,
                receipt.content_hash,
                float(receipt.occurred_at),
                self._now(),
                _json(receipt.payload),
                _json(result),
            ),
        )

    def _reconcile_handover(self, receipt: Receipt) -> dict[str, Any]:
        event_id = receipt.payload.get("transfer_event_id")
        event = (
            self.store.one("SELECT * FROM events WHERE id=? AND type='transfer'", (event_id,))
            if event_id
            else None
        )
        if event is None:
            self._safe_alert(
                "receipt_discrepancy",
                f"交接回执 {receipt.id} 找不到对应转移事件",
                {"receipt_id": receipt.id},
            )
            return {"status": "unmatched"}
        expected = _loads(event["outputs_json"])[0]
        quantity_match = receipt.quantity is not None and abs(
            receipt.quantity - expected["quantity"]
        ) <= _EPS
        content_match = receipt.content_hash == event["content_hash"]
        if quantity_match and content_match:
            return {"status": "confirmed", "transfer_event_id": event_id}
        result = {
            "status": "discrepancy",
            "transfer_event_id": event_id,
            "expected_quantity": expected["quantity"],
            "reported_quantity": receipt.quantity,
            "content_match": content_match,
        }
        self._safe_alert(
            "receipt_discrepancy",
            f"交接回执 {receipt.id} 与转移事件 {event_id} 不一致",
            {"receipt_id": receipt.id, "transfer_event_id": event_id},
        )
        return result

    def _reconcile_scan(self, receipt: Receipt) -> dict[str, Any]:
        if receipt.material_id is None:
            return {"status": "unmatched"}
        row = self.store.one("SELECT * FROM materials WHERE id=?", (receipt.material_id,))
        if row is None:
            self._safe_alert(
                "receipt_discrepancy",
                f"扫描回执 {receipt.id} 指向未知材料",
                {"receipt_id": receipt.id},
            )
            return {"status": "unmatched"}
        ledger = self.quantity_ledger(receipt.material_id)
        quantity_match = receipt.quantity is not None and abs(
            receipt.quantity - ledger["remaining"]
        ) <= _EPS
        content_match = receipt.content_hash is None or (
            receipt.content_hash == self.expected_content_hash(receipt.material_id)
        )
        if quantity_match and content_match:
            return {"status": "confirmed", "remaining": ledger["remaining"]}
        self._safe_alert(
            "receipt_discrepancy",
            f"扫描回执 {receipt.id} 与台账不一致",
            {"receipt_id": receipt.id, "material_id": receipt.material_id},
        )
        return {
            "status": "discrepancy",
            "expected_quantity": ledger["remaining"],
            "reported_quantity": receipt.quantity,
            "content_match": content_match,
        }

    def _safe_alert(self, type: str, message: str, ref: dict[str, Any]) -> None:
        with self.store.transaction() as conn:
            self._alert(conn, type, message, ref, self._now())

    def _alert(
        self, conn: Any, type: str, message: str, ref: dict[str, Any], now: float
    ) -> None:
        conn.execute(
            "INSERT INTO alerts(id,type,message,ref_json,created_at) VALUES(?,?,?,?,?)",
            (_new_id(), type, message, _json(ref), now),
        )

    def _schedule_job(
        self, conn: Any, type: str, due_at: float, payload: dict[str, Any]
    ) -> str:
        job_id = _new_id()
        conn.execute(
            "INSERT INTO jobs(id,type,due_at,payload_json,status,created_at)"
            " VALUES(?,?,?,?,'pending',?)",
            (job_id, type, float(due_at), _json(payload), self._now()),
        )
        return job_id

    def _run_job(self, type: str, payload: dict[str, Any], now: float) -> dict[str, Any]:
        if type == "review_reminder":
            req = self.store.one(
                "SELECT * FROM access_requests WHERE id=?", (payload["request_id"],)
            )
            if req and req["status"] == RequestStatus.NEEDS_EXCEPTION.value:
                self._safe_alert(
                    "pending_review",
                    f"取用申请 {req['id']} 超过复核时限仍未处理",
                    {"request_id": req["id"]},
                )
                return {"alerted": True}
            return {"alerted": False}
        if type == "trial_followup":
            trial = self.store.one("SELECT * FROM trials WHERE id=?", (payload["trial_id"],))
            if trial and trial["status"] == "active":
                with self.store.transaction() as conn:
                    self._alert(
                        conn,
                        "trial_followup",
                        f"试验 {trial['id']} 随访到期",
                        {"trial_id": trial["id"]},
                        now,
                    )
                    if trial["follow_up_interval"]:
                        self._schedule_job(
                            conn,
                            "trial_followup",
                            now + float(trial["follow_up_interval"]),
                            {"trial_id": trial["id"]},
                        )
                return {"alerted": True}
            return {"alerted": False}
        if type == "storage_recheck":
            reading = self.store.one(
                "SELECT * FROM condition_readings WHERE site_id=? ORDER BY recorded_at DESC",
                (payload["site_id"],),
            )
            violations = (
                self._condition_violations(payload["site_id"], reading["temperature"])
                if reading
                else []
            )
            now2 = self._now()
            with self.store.transaction() as conn:
                for row in violations:
                    self._alert(
                        conn,
                        "storage_condition",
                        f"复核：场地 {payload['site_id']} 仍未恢复材料 {row['id']} 保存条件",
                        {"site_id": payload["site_id"], "material_id": row["id"]},
                        now2,
                    )
            return {"violations": [row["id"] for row in violations]}
        raise ValueError(f"未知任务类型: {type}")

    def _condition_violations(self, site_id: str, temperature: float) -> list[dict[str, Any]]:
        violations = []
        for row in self.store.query(
            "SELECT * FROM materials WHERE site_id=? AND status='active'", (site_id,)
        ):
            low, high = STORAGE_RANGES[row["storage_class"]]
            if not low <= temperature <= high:
                violations.append(row)
        return violations

    def _material_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "quality_batch": row["quality_batch"],
            "biosafety_level": row["biosafety_level"],
            "storage_class": row["storage_class"],
            "quantity": row["quantity"],
            "unit": row["unit"],
            "site_id": row["site_id"],
            "custodian_org": row["custodian_org"],
            "license_id": row["license_id"],
            "status": row["status"],
        }

    def _request_view(self, req: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": req["id"],
            "submitter": req["submitter"],
            "org": req["org"],
            "purpose": req["purpose"],
            "material_id": req["material_id"],
            "quantity": req["quantity"],
            "site_id": req["site_id"],
            "status": req["status"],
            "risk_flags": _loads(req["risk_flags_json"]),
            "reasons": _loads(req["reasons_json"]),
            "max_quantity": req["max_quantity"],
            "approver": req["approver"],
        }

    @staticmethod
    def _event_view(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event["id"],
            "type": event["type"],
            "occurred_at": event["occurred_at"],
            "actor": event["actor"],
            "org": event["org"],
            "site_id": event["site_id"],
            "basis": _loads(event["basis_json"]),
            "inputs": _loads(event["inputs_json"]),
            "outputs": _loads(event["outputs_json"]),
            "note": event["note"],
        }
