"""治理服务：登记与决策的唯一入口。

所有写操作都校验业务规则后追加事件；规则要点：

- 接收/拆分/混合/繁育/转移/失活沿同一谱系登记，数量守恒并核验收容量；
- 取用决策综合用途、机构、人员资质、生物安全等级、许可有效期、场地容量；
- 高风险例外必须由独立的生物安全审核人员批准，提交人不能批准自己的方案；
- 搜索只返回该机构被授权或正在保管的材料，未授权机构无法推断受限资源；
- 污染、丢失、许可撤销、场地故障只冻结受影响范围，已完成环节不动，
  并生成带责任主体的处置任务。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Iterable

from .errors import GovernanceError
from .events import EventStore, utcnow
from .models import (
    BiosafetyLevel,
    EvidenceKind,
    EvidenceSide,
    IncidentKind,
    MaterialType,
    OutputKind,
    StorageStatus,
    TrialStatus,
    Unit,
)
from .timeline import Timeline

QTY_TOL = 1e-9


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class GovernanceService:
    def __init__(self, store: EventStore, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.clock = clock
        self.timeline = Timeline()
        store.listen(self.timeline.apply)

    # ===== 基础登记 =====================================================

    def register_org(self, org_id: str, name: str) -> None:
        if org_id in self.timeline.orgs:
            raise GovernanceError("机构已登记")
        self.store.append("ORG_REGISTERED", "system", {"org_id": org_id, "name": name})

    def register_facility(
        self,
        facility_id: str,
        name: str,
        owner_org: str,
        bsl_limit: BiosafetyLevel,
        capacity_total: float,
        spec_ranges: dict[str, tuple[float, float]] | None = None,
        work_capacity_total: float | None = None,
    ) -> None:
        if facility_id in self.timeline.facilities:
            raise GovernanceError("场地已登记")
        if owner_org not in self.timeline.orgs:
            raise GovernanceError("所属机构未登记")
        self.store.append("FACILITY_REGISTERED", "system", {
            "facility_id": facility_id,
            "name": name,
            "owner_org": owner_org,
            "bsl_limit": int(bsl_limit),
            "capacity_total": float(capacity_total),
            "work_capacity_total": float(
                capacity_total if work_capacity_total is None else work_capacity_total
            ),
            "spec_ranges": spec_ranges or {},
        })

    def register_person(
        self,
        person_id: str,
        name: str,
        org: str,
        bsl_allowed: BiosafetyLevel,
        qualifications: Iterable[str] = (),
        is_biosafety_reviewer: bool = False,
    ) -> None:
        if person_id in self.timeline.persons:
            raise GovernanceError("人员已登记")
        if org not in self.timeline.orgs:
            raise GovernanceError("所属机构未登记")
        self.store.append("PERSON_REGISTERED", "system", {
            "person_id": person_id,
            "name": name,
            "org": org,
            "bsl_allowed": int(bsl_allowed),
            "qualifications": list(qualifications),
            "is_biosafety_reviewer": is_biosafety_reviewer,
        })

    def register_license(
        self,
        license_id: str,
        title: str,
        licensor_org: str,
        holder_orgs: Iterable[str],
        allowed_purposes: Iterable[str],
        max_bsl: BiosafetyLevel,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        terms: str = "",
        derivatives_allowed: bool = True,
        benefit_terms: list[dict[str, Any]] | None = None,
    ) -> None:
        if license_id in self.timeline.licenses:
            raise GovernanceError("许可已登记")
        holders = list(holder_orgs)
        for org in holders:
            if org not in self.timeline.orgs:
                raise GovernanceError(f"许可持有机构未登记: {org}")
        self.store.append("LICENSE_REGISTERED", "system", {
            "license_id": license_id,
            "title": title,
            "licensor_org": licensor_org,
            "holder_orgs": holders,
            "allowed_purposes": list(allowed_purposes),
            "max_bsl": int(max_bsl),
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
            "terms": terms,
            "derivatives_allowed": derivatives_allowed,
            "benefit_terms": benefit_terms or [],
        })

    # ===== 材料接收与谱系 ===============================================

    def _storage_load(self, facility_id: str) -> float:
        """在场且未失活/未确认丢失的在库数量（冻结材料仍占保存位）。

        已批准待发放的材料尚未出库，仍包含在 occupancy 中，不另计。
        """
        return self.timeline.occupancy(facility_id)

    def _work_load(self, facility_id: str, exclude_request_id: str | None = None) -> float:
        """已发放到试验/在位使用环节、尚未办结的数量，加批准待发放预留。"""
        load = 0.0
        for request in self.timeline.requests.values():
            if request.target_facility_id != facility_id:
                continue
            if request.request_id == exclude_request_id:
                continue
            if request.status == "approved":
                load += max(request.qty - request.issued_qty, 0.0)
            elif request.status == "issued":
                load += request.issued_qty
        return load

    def _check_storage_capacity(self, facility_id: str, delta: float) -> None:
        facility = self.timeline.facilities[facility_id]
        load = self._storage_load(facility_id)
        if load + delta > facility.capacity_total + QTY_TOL:
            raise GovernanceError(
                f"场地保存容量不足：现有占用 {load:g}，新增 {delta:g}，"
                f"总容量 {facility.capacity_total:g}"
            )

    def _check_work_capacity(self, facility_id: str, delta: float) -> None:
        facility = self.timeline.facilities[facility_id]
        load = self._work_load(facility_id)
        if load + delta > facility.work_capacity_total + QTY_TOL:
            raise GovernanceError(
                f"场地使用容量不足：已占用 {load:g}，本次 {delta:g}，"
                f"使用容量上限 {facility.work_capacity_total:g}"
            )

    def _usable(self, material_id: str, action: str = "操作"):
        material = self.timeline.materials.get(material_id)
        if material is None:
            raise GovernanceError(f"材料不存在: {material_id}")
        if material.status == StorageStatus.FROZEN:
            raise GovernanceError(f"材料已被事件冻结，暂停{action}")
        if material.status in (StorageStatus.INACTIVATED, StorageStatus.LOST):
            raise GovernanceError(f"材料已{material.status.value}，不能{action}")
        return material

    def receive_material(
        self,
        material_id: str,
        material_type: MaterialType,
        name: str,
        unit: Unit,
        qty: float,
        bsl: BiosafetyLevel,
        source_org: str,
        license_ids: Iterable[str],
        batch: str,
        facility_id: str,
        custodian_person: str | None = None,
        storage_label: str = "",
        occurred_at: datetime | None = None,
        idem_key: str | None = None,
    ) -> str:
        if material_id in self.timeline.materials:
            raise GovernanceError("材料编号已存在")
        facility = self.timeline.facilities.get(facility_id)
        if facility is None:
            raise GovernanceError("保存场地不存在")
        if qty <= 0:
            raise GovernanceError("接收数量必须为正")
        if bsl > facility.bsl_limit:
            raise GovernanceError("材料生物安全等级超出场地限值")
        license_moment = occurred_at or self.clock()
        licenses = []
        for lid in license_ids:
            license_ = self.timeline.licenses.get(lid)
            if license_ is None:
                raise GovernanceError(f"许可不存在: {lid}")
            if facility.owner_org not in license_.holder_orgs:
                raise GovernanceError(f"许可 {lid} 未授权接收机构 {facility.owner_org}")
            if not license_.valid_at(license_moment):
                raise GovernanceError(f"许可 {lid} 在接收时点不在有效期内或已撤销")
            licenses.append(license_)
        self._check_storage_capacity(facility_id, qty)
        payload = {
            "material_id": material_id,
            "material_type": material_type.value,
            "name": name,
            "unit": unit.value,
            "output_qty": qty,
            "bsl": int(bsl),
            "source_org": source_org,
            "license_ids": [lic.license_id for lic in licenses],
            "batch": batch,
            "storage_label": storage_label,
            "facility_id": facility_id,
            "custodian_org": facility.owner_org,
            "custodian_person": custodian_person,
            "derivation": "receipt",
            "basis_snapshot": {
                "received_at": (occurred_at or self.clock()).isoformat(),
                "licenses": [
                    {"license_id": lic.license_id, "valid_to": lic.valid_to, "terms": lic.terms}
                    for lic in licenses
                ],
            },
        }
        event = self.store.append(
            "MATERIAL_RECEIVED", facility.owner_org, payload,
            occurred_at=occurred_at, idem_key=idem_key,
        )
        return event.event_id

    def _create_child(
        self,
        event_type: str,
        child_id: str,
        name: str,
        material_type: MaterialType,
        unit: Unit,
        output_qty: float,
        bsl: BiosafetyLevel,
        facility_id: str,
        license_ids: list[str],
        batch: str,
        parents: list[tuple[str, float]],
        inputs: list[tuple[str, float]],
        derivation: str,
        storage_label: str = "",
        actor: str = "system",
    ) -> None:
        if child_id in self.timeline.materials:
            raise GovernanceError("材料编号已存在")
        if output_qty <= 0:
            raise GovernanceError("产出数量必须为正")
        custodian = self.timeline.facilities[facility_id].owner_org
        payload = {
            "material_id": child_id,
            "material_type": material_type.value,
            "name": name,
            "unit": unit.value,
            "output_qty": output_qty,
            "bsl": int(bsl),
            "source_org": custodian,
            "license_ids": license_ids,
            "batch": batch,
            "storage_label": storage_label,
            "facility_id": facility_id,
            "custodian_org": custodian,
            "parents": [[pid, qty] for pid, qty in parents],
            "inputs": [[pid, qty] for pid, qty in inputs],
            "derivation": derivation,
        }
        self.store.append(event_type, actor, payload)

    def split_material(
        self, parent_id: str, child_id: str, qty: float, new_batch: str, *,
        name: str | None = None,
    ) -> None:
        """从一批材料中分出一部分成为独立子批次，剩余仍留在原批次。"""
        parent = self._usable(parent_id, "拆分")
        if qty <= 0 or qty > parent.quantity + QTY_TOL:
            raise GovernanceError("拆分数量超出可分库存")
        self._create_child(
            "MATERIAL_SPLIT", child_id, name or parent.name,
            parent.material_type, parent.unit, qty, parent.bsl,
            parent.facility_id, list(parent.license_ids), new_batch,
            parents=[(parent_id, qty)], inputs=[(parent_id, qty)],
            derivation="split", storage_label=parent.storage_label,
            actor=parent.custodian_org or "system",
        )

    def mix_materials(
        self,
        output_id: str,
        name: str,
        inputs: list[tuple[str, float]],
        output_qty: float,
        new_batch: str,
        material_type: MaterialType = MaterialType.DERIVATIVE,
    ) -> None:
        """混合多份材料。所有成分必须在同场地、同单位、许可允许衍生。"""
        if len(inputs) < 2:
            raise GovernanceError("混合至少需要两种成分")
        parents = []
        seen: set[str] = set()
        facility_id = None
        custodian = None
        unit = None
        bsl = BiosafetyLevel.BSL1
        license_ids: list[str] = []
        for mid, qty in inputs:
            material = self._usable(mid, "混合")
            if qty <= 0 or qty > material.quantity + QTY_TOL:
                raise GovernanceError(f"成分 {mid} 数量超出库存")
            if facility_id is None:
                facility_id = material.facility_id
                custodian = material.custodian_org
                unit = material.unit
            elif material.facility_id != facility_id or material.custodian_org != custodian:
                raise GovernanceError("只能混合同一机构、同一场地内的材料")
            if material.unit != unit:
                raise GovernanceError("混合成分计量单位不一致")
            bsl = max(bsl, material.bsl)
            for license_ in self.timeline.effective_licenses(mid):
                if not license_.derivatives_allowed:
                    raise GovernanceError(f"许可 {license_.license_id} 不允许制备衍生材料")
            if mid in seen:
                raise GovernanceError(f"成分重复: {mid}")
            seen.add(mid)
            parents.append((mid, qty))
            for lid in material.license_ids:
                if lid not in license_ids:
                    license_ids.append(lid)
        net = output_qty - sum(qty for _, qty in inputs)
        self._check_storage_capacity(facility_id, net)  # type: ignore[arg-type]
        self._create_child(
            "MATERIAL_MIXED", output_id, name, material_type, unit,  # type: ignore[arg-type]
            output_qty, bsl, facility_id, license_ids, new_batch,  # type: ignore[arg-type]
            parents=parents, inputs=inputs, derivation="mix",
            actor=custodian or "system",
        )

    def propagate_material(
        self,
        parent_id: str,
        child_id: str,
        output_qty: float,
        new_batch: str,
        *,
        name: str | None = None,
    ) -> None:
        """繁育/扩繁：原种保留，新增数量登记为子代。"""
        parent = self._usable(parent_id, "繁育")
        for license_ in self.timeline.effective_licenses(parent_id):
            if not license_.derivatives_allowed:
                raise GovernanceError(f"许可 {license_.license_id} 不允许繁育扩繁")
        self._check_storage_capacity(parent.facility_id, output_qty)
        self._create_child(
            "MATERIAL_PROPAGATED", child_id, name or parent.name,
            parent.material_type, parent.unit, output_qty, parent.bsl,
            parent.facility_id, list(parent.license_ids), new_batch,
            parents=[(parent_id, 0.0)], inputs=[],
            derivation="propagate", storage_label=parent.storage_label,
            actor=parent.custodian_org or "system",
        )

    def inactivate_material(
        self, material_id: str, qty: float | None = None, reason: str = "",
        actor: str | None = None, *, under_incident: str | None = None,
    ) -> None:
        """失活（灭活）剩余材料，全部或部分；失活量逐笔可核。

        常规情况下冻结材料不能失活；事件处置时需指明所属事件，
        且只能处置该事件冻结范围内的材料。
        """
        material = self.timeline.materials.get(material_id)
        if material is None:
            raise GovernanceError(f"材料不存在: {material_id}")
        if material.status in (StorageStatus.INACTIVATED, StorageStatus.LOST):
            raise GovernanceError(f"材料已{material.status.value}，不能失活")
        if material.status == StorageStatus.FROZEN:
            if under_incident is None or under_incident not in material.frozen_incidents:
                raise GovernanceError("材料已被事件冻结，暂停失活")
        qty = material.quantity if qty is None else qty
        if qty <= 0 or qty > material.quantity + QTY_TOL:
            raise GovernanceError("失活数量超出库存")
        self.store.append("MATERIAL_INACTIVATED", actor or material.custodian_org or "system", {
            "material_id": material_id,
            "qty": qty,
            "reason": reason,
            "incident_id": under_incident,
        })

    def confirm_loss(
        self, material_id: str, incident_id: str, actor: str, reason: str = "",
    ) -> None:
        """在丢失事件处置中确认材料无法找回，账面数量清零并标记丢失。"""
        material = self.timeline.materials.get(material_id)
        if material is None:
            raise GovernanceError("材料不存在")
        if material.status == StorageStatus.LOST:
            raise GovernanceError("材料已确认丢失")
        if incident_id not in material.frozen_incidents:
            raise GovernanceError("只能在冻结该材料的丢失事件中确认丢失")
        incident = self.timeline.incidents[incident_id]
        if incident.kind != IncidentKind.LOSS:
            raise GovernanceError("只有丢失事件可以确认丢失")
        self.store.append("MATERIAL_LOST", actor, {
            "material_id": material_id,
            "incident_id": incident_id,
            "reason": reason,
        })

    # ===== 转移与交接证据 ===============================================

    def dispatch_transfer(
        self,
        transfer_id: str,
        material_id: str,
        qty: float,
        to_org: str,
        to_facility_id: str,
        to_person: str | None = None,
        occurred_at: datetime | None = None,
        idem_key: str | None = None,
    ) -> None:
        material = self._usable(material_id, "转移")
        if qty <= 0 or qty > material.quantity + QTY_TOL:
            raise GovernanceError("转移数量超出库存")
        target = self.timeline.facilities.get(to_facility_id)
        if target is None or target.owner_org != to_org:
            raise GovernanceError("接收场地不存在或不属于接收机构")
        if material.bsl > target.bsl_limit:
            raise GovernanceError("材料生物安全等级超出接收场地限值")
        licenses = self.timeline.effective_licenses(material_id)
        if not licenses:
            raise GovernanceError("材料没有适用许可，不能转移")
        for license_ in licenses:
            if license_.revoked or not license_.valid_at(self.clock()):
                raise GovernanceError(f"许可 {license_.license_id} 已失效，不能转移")
            if to_org not in license_.holder_orgs:
                raise GovernanceError(
                    f"许可 {license_.license_id} 未授权接收机构 {to_org}"
                )
        self.store.append("TRANSFER_DISPATCHED", material.custodian_org or "system", {
            "transfer_id": transfer_id,
            "material_id": material_id,
            "qty": qty,
            "from_org": material.custodian_org,
            "from_person": material.custodian_person,
            "from_facility": material.facility_id,
            "to_org": to_org,
            "to_person": to_person,
            "to_facility": to_facility_id,
        }, occurred_at=occurred_at, idem_key=idem_key)

    def receive_transfer(
        self,
        transfer_id: str,
        to_material_id: str,
        batch: str | None = None,
        storage_label: str | None = None,
        occurred_at: datetime | None = None,
        idem_key: str | None = None,
    ) -> None:
        transfer = self.timeline.transfers.get(transfer_id)
        if transfer is None:
            raise GovernanceError("转移单不存在")
        if transfer.received_at is not None:
            raise GovernanceError("转移已接收，不能重复登记")
        if to_material_id in self.timeline.materials:
            raise GovernanceError("接收材料编号已存在")
        self._check_storage_capacity(transfer.to_facility, transfer.qty)
        self.store.append("TRANSFER_RECEIVED", transfer.to_org, {
            "transfer_id": transfer_id,
            "to_material_id": to_material_id,
            "batch": batch,
            "storage_label": storage_label,
        }, occurred_at=occurred_at, idem_key=idem_key)

    def submit_evidence(
        self,
        target_type: str,
        target_id: str,
        side: EvidenceSide,
        kind: EvidenceKind,
        file_ref: str,
        content_sha256: str,
        submitted_by: str,
        *,
        observed_qty: float | None = None,
        observed_identity: str | None = None,
        condition_note: str = "",
        occurred_at: datetime | None = None,
        evidence_id: str | None = None,
    ) -> str:
        """提交扫码/回执/装箱单。

        同一目标、同一提交方、同一内容哈希视为重复提交自动去重；
        交接双方各自的照片（哈希不同）分别保留。
        """
        if target_type not in ("transfer", "receipt"):
            raise GovernanceError("证据目标类型无效")
        evidence_id = evidence_id or _new_id("ev")
        idem_key = EventStore.deterministic_key(
            target_type, target_id, side.value, kind.value, content_sha256
        )
        event = self.store.append("EVIDENCE_SUBMITTED", submitted_by, {
            "evidence_id": evidence_id,
            "target_type": target_type,
            "target_id": target_id,
            "side": side.value,
            "kind": kind.value,
            "file_ref": file_ref,
            "content_sha256": content_sha256,
            "observed_qty": observed_qty,
            "observed_identity": observed_identity,
            "condition_note": condition_note,
            "submitted_by": submitted_by,
            "occurred_at": (occurred_at or self.clock()).isoformat(),
        }, occurred_at=occurred_at, idem_key=idem_key)
        return event.payload["evidence_id"]

    def reconcile_transfer(self, transfer_id: str) -> dict[str, Any]:
        """核对双方交接证据：双侧齐备且数量、身份一致才能确认。"""
        transfer = self.timeline.transfers.get(transfer_id)
        if transfer is None:
            raise GovernanceError("转移单不存在")
        if transfer.received_at is None:
            raise GovernanceError("接收尚未登记，无法核对")
        source = self.timeline.materials[transfer.material_id]
        sides = {}
        for eid in transfer.evidence_ids:
            ev = self.timeline.evidence[eid]
            sides.setdefault(ev.side, []).append(ev)
        problems: list[str] = []
        for side in (EvidenceSide.SENDER, EvidenceSide.RECEIVER):
            if side not in sides:
                problems.append(f"缺少{side.value}方交接证据")
        for side, evidences in sides.items():
            for ev in evidences:
                if ev.observed_qty is not None and abs(ev.observed_qty - transfer.qty) > QTY_TOL:
                    problems.append(
                        f"{side.value}方记录数量 {ev.observed_qty:g} 与转移数量 {transfer.qty:g} 不一致"
                    )
                if ev.observed_identity and ev.observed_identity not in (source.material_id, source.name):
                    problems.append(f"{side.value}方记录身份“{ev.observed_identity}”与材料不符")
        if not sides.get(EvidenceSide.SENDER) or not sides.get(EvidenceSide.RECEIVER):
            return {"confirmed": False, "disputed": transfer.disputed, "problems": problems}
        if problems:
            if not transfer.disputed:
                self.store.append("TRANSFER_DISPUTED", transfer.to_org, {
                    "transfer_id": transfer_id,
                    "discrepancy": "；".join(problems),
                })
                self._raise_alert(
                    "transfer_discrepancy", "high",
                    f"转移 {transfer_id} 核对发现问题：{'；'.join(problems)}",
                    "transfer", transfer_id,
                )
            return {"confirmed": False, "disputed": True, "problems": problems}
        if not transfer.confirmed:
            self.store.append("TRANSFER_CONFIRMED", transfer.to_org,
                              {"transfer_id": transfer_id})
        return {"confirmed": True, "disputed": False, "problems": []}

    def stocktake(
        self, material_id: str, counted_qty: float, counter_id: str, note: str = ""
    ) -> dict[str, Any]:
        """盘点：系统数量与实测数量核对，不一致立即告警。"""
        material = self._usable(material_id, "盘点")
        expected = material.quantity
        matched = abs(expected - counted_qty) <= QTY_TOL
        stocktake_id = _new_id("stk")
        self.store.append("STOCKTAKE_RECORDED", counter_id, {
            "stocktake_id": stocktake_id,
            "material_id": material_id,
            "expected_qty": expected,
            "counted_qty": counted_qty,
            "matched": matched,
            "counter_id": counter_id,
            "note": note,
        })
        if not matched:
            self._raise_alert(
                "stocktake_mismatch", "high",
                f"材料 {material_id} 盘点不符：账面 {expected:g}，实测 {counted_qty:g}",
                "material", material_id,
            )
        return {"stocktake_id": stocktake_id, "expected": expected,
                "counted": counted_qty, "matched": matched}

    # ===== 取用申请与决策 ===============================================

    def _evaluate_access(
        self,
        material_id: str,
        applicant_id: str,
        purpose: str,
        qty: float,
        facility_id: str,
        trial_id: str | None,
        exception_requested: bool,
        current_request_id: str | None = None,
    ) -> dict[str, Any]:
        tl = self.timeline
        reasons: list[str] = []        # 硬性不通过原因
        freeze_reasons: list[str] = []  # 仅因事件暂时冻结
        material = tl.materials.get(material_id)
        person = tl.persons.get(applicant_id)
        facility = tl.facilities.get(facility_id)
        moment = self.clock()

        if material is None:
            reasons.append("材料不存在")
        if person is None or not person.active:
            reasons.append("申请人不存在或已停用")
        if facility is None:
            reasons.append("使用场地不存在")
        if reasons:
            return {"allowed": False, "high_risk": False, "frozen": False,
                    "reasons": reasons, "factors": {}}

        if material.status == StorageStatus.FROZEN:
            freeze_reasons.append("材料已被事件冻结")
        elif material.status != StorageStatus.ACTIVE:
            reasons.append(f"材料状态为 {material.status.value}，不可取用")
        if qty <= 0:
            reasons.append("申请数量必须为正")
        elif qty > material.quantity + QTY_TOL:
            reasons.append(f"申请数量 {qty:g} 超出可用库存 {material.quantity:g}")

        # 许可：适用许可中只要有一份在有效期内、授权该机构且覆盖本次用途即可
        covering = [
            lic for lic in tl.effective_licenses(material_id)
            if person.org in lic.holder_orgs
        ]
        valid_covering: list = []
        license_problems: list[str] = []
        if not covering:
            reasons.append(f"机构 {person.org} 不在任何适用许可的持有方范围内")
        else:
            for lic in covering:
                if lic.revoked:
                    license_problems.append(f"许可 {lic.license_id} 已撤销")
                elif not lic.valid_at(moment):
                    license_problems.append(
                        f"许可 {lic.license_id} 不在有效期内"
                        f"（{lic.valid_from or '…'} 至 {lic.valid_to or '…'}）"
                    )
                elif "*" not in lic.allowed_purposes and purpose not in lic.allowed_purposes:
                    license_problems.append(f"许可 {lic.license_id} 不覆盖用途“{purpose}”")
                else:
                    valid_covering.append(lic)
            if not valid_covering:
                reasons.append("没有覆盖本次取用的有效许可：" + "；".join(license_problems))

        if person.bsl_allowed < material.bsl:
            reasons.append(
                f"人员资质 {person.bsl_allowed.name} 低于材料要求 {material.bsl.name}"
            )
        if facility.bsl_limit < material.bsl:
            reasons.append(
                f"场地等级 {facility.bsl_limit.name} 低于材料要求 {material.bsl.name}"
            )
        if facility.owner_org != person.org:
            reasons.append("使用场地不属于申请人所在机构")
        work_load = self._work_load(facility_id, exclude_request_id=current_request_id)
        if work_load + qty > facility.work_capacity_total + QTY_TOL:
            reasons.append(
                f"场地使用容量不足：已占用 {work_load:g} + 申请 {qty:g}"
                f" > 使用容量 {facility.work_capacity_total:g}"
            )

        trial_info: dict[str, Any] | None = None
        if trial_id is not None:
            trial = tl.trials.get(trial_id)
            if trial is None:
                reasons.append("关联试验不存在")
            else:
                if trial.status not in (TrialStatus.PLANNED, TrialStatus.ONGOING, TrialStatus.FROZEN):
                    reasons.append(f"试验状态为 {trial.status.value}，不能新增取用")
                if trial.facility_id != facility_id:
                    reasons.append("使用场地与试验登记场地不一致")
                planned = trial.bom.get(material_id)
                if planned is None:
                    reasons.append("材料不在试验方案清单内")
                else:
                    committed = trial.issued.get(material_id, 0.0)
                    for other in tl.requests.values():
                        if (
                            other.request_id == current_request_id
                            or other.trial_id != trial_id
                            or other.material_id != material_id
                            or other.status not in (
                                "approved", "pending_review", "issued"
                            )
                        ):
                            continue
                        committed += max(other.qty - other.issued_qty, 0.0)
                    if committed + qty > planned + QTY_TOL:
                        reasons.append(
                            f"超出试验方案用量：已承诺 {committed:g} + 申请 {qty:g}，方案 {planned:g}"
                        )
                    trial_info = {"planned": planned, "committed": committed}
                if trial.status == TrialStatus.FROZEN:
                    freeze_reasons.append("试验已被事件冻结")

        high_risk = exception_requested or material.bsl >= BiosafetyLevel.BSL3
        hard_rules_pass = not reasons
        allowed = hard_rules_pass and not freeze_reasons
        return {
            "allowed": allowed,
            "eligible": hard_rules_pass,  # 硬性条件齐备（可能仅被冻结挂起）
            "high_risk": high_risk,
            "frozen": bool(freeze_reasons),
            "reasons": reasons,
            "freeze_reasons": freeze_reasons,
            "factors": {
                "purpose": purpose,
                "org": person.org,
                "person": applicant_id,
                "person_bsl": person.bsl_allowed.name,
                "material_bsl": material.bsl.name,
                "qty": qty,
                "available_qty": material.quantity,
                "facility": facility_id,
                "facility_load": work_load,
                "facility_capacity": facility.work_capacity_total,
                "decided_at": moment.isoformat(),
                "licenses": [
                    {
                        "license_id": lic.license_id,
                        "title": lic.title,
                        "valid_from": lic.valid_from,
                        "valid_to": lic.valid_to,
                        "purposes": sorted(lic.allowed_purposes),
                        "terms": lic.terms,
                    }
                    for lic in valid_covering
                ],
                "trial": trial_info,
                "exception_requested": exception_requested,
            },
            "executable": {
                "purpose": purpose,
                "qty": qty,
                "facility_id": facility_id,
                "material_id": material_id,
            } if allowed else None,
        }

    def request_access(
        self,
        material_id: str,
        applicant_id: str,
        purpose: str,
        qty: float,
        facility_id: str,
        *,
        trial_id: str | None = None,
        exception_requested: bool = False,
        request_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or _new_id("req")
        if request_id in self.timeline.requests:
            raise GovernanceError("取用单编号已存在")
        person = self.timeline.persons.get(applicant_id)
        if person is None:
            raise GovernanceError("申请人不存在")
        if material_id not in self.timeline.materials:
            raise GovernanceError(f"材料不存在: {material_id}")
        if facility_id not in self.timeline.facilities:
            raise GovernanceError("使用场地不存在")
        self.store.append("ACCESS_REQUESTED", applicant_id, {
            "request_id": request_id,
            "material_id": material_id,
            "applicant_id": applicant_id,
            "org": person.org,
            "purpose": purpose,
            "qty": qty,
            "target_facility_id": facility_id,
            "trial_id": trial_id,
            "exception_requested": exception_requested,
        }, occurred_at=occurred_at)

        decision = self._evaluate_access(
            material_id, applicant_id, purpose, qty, facility_id,
            trial_id, exception_requested, current_request_id=request_id,
        )
        material = self.timeline.materials[material_id]
        if decision["allowed"]:
            # 高风险（BSL3+ 或申请例外）即使条件齐备也必须独立复核
            status = "pending_review" if decision["high_risk"] else "approved"
        elif decision["frozen"] and not decision["reasons"]:
            # 仅因事件冻结挂起，解冻后自动恢复批准/继续复核
            status = "pending_review"
        else:
            status = "denied"
        self.store.append("ACCESS_DECIDED", "system", {
            "request_id": request_id,
            "status": status,
            "high_risk": decision["high_risk"],
            "decision": decision,
        })
        # 申请提出时材料/试验已处于冻结的，立即挂到既有事件下
        request = self.timeline.requests[request_id]
        frozen_by = set()
        if material is not None:
            frozen_by |= material.frozen_incidents
        if trial_id is not None and trial_id in self.timeline.trials:
            frozen_by |= self.timeline.trials[trial_id].frozen_incidents
        if frozen_by and request.status not in ("denied",):
            for incident_id in frozen_by:
                self.store.append("FREEZE_APPLIED", "system", {
                    "incident_id": incident_id,
                    "material_ids": [],
                    "trial_ids": [],
                    "request_ids": [request_id],
                })
        return {"request_id": request_id, "status": status, "decision": decision}

    def review_access(self, request_id: str, reviewer_id: str, approved: bool, note: str = "") -> None:
        """高风险例外的独立复核。提交人自行批准一律拒绝。"""
        request = self.timeline.requests.get(request_id)
        if request is None:
            raise GovernanceError("取用单不存在")
        if request.status != "pending_review":
            raise GovernanceError("取用单不在待复核状态")
        if not request.high_risk:
            raise GovernanceError("非高风险取用不需要例外复核")
        reviewer = self.timeline.persons.get(reviewer_id)
        if reviewer is None or not reviewer.active:
            raise GovernanceError("复核人不存在或已停用")
        if reviewer_id == request.applicant_id:
            raise GovernanceError("高风险例外不能由方案提交人自行批准")
        if not reviewer.is_biosafety_reviewer:
            raise GovernanceError("复核人不具备生物安全审核资质")
        material = self.timeline.materials[request.material_id]
        if reviewer.bsl_allowed < material.bsl:
            raise GovernanceError("复核人资质低于材料生物安全等级")
        if request.frozen_incidents:
            raise GovernanceError("取用单仍被未决事件冻结，不能复核")
        self.store.append("ACCESS_REVIEWED", reviewer_id, {
            "request_id": request_id,
            "reviewer_id": reviewer_id,
            "approved": approved,
            "note": note,
        })

    def issue_access(self, request_id: str, qty: float | None = None) -> None:
        """按批准范围实际发放；发放瞬间重新校验全部依据与冻结状态。"""
        request = self.timeline.requests.get(request_id)
        if request is None:
            raise GovernanceError("取用单不存在")
        if request.status != "approved":
            raise GovernanceError("取用单未获批准，不能发放")
        qty = request.qty if qty is None else qty
        if qty <= 0 or qty > request.qty + QTY_TOL:
            raise GovernanceError("发放数量超出批准范围")
        # 依据可能自批准后变化（许可撤销/到期、库存变动、容量变化、试验用量）：
        # 以发放时点重新评估，原批准记录保留可审计
        fresh = self._evaluate_access(
            request.material_id, request.applicant_id, request.purpose,
            request.qty, request.target_facility_id, request.trial_id,
            request.exception_requested, current_request_id=request_id,
        )
        if not fresh["allowed"]:
            problems = fresh["reasons"] + fresh["freeze_reasons"]
            raise GovernanceError("发放时复核未通过：" + "；".join(problems))
        material = self.timeline.materials[request.material_id]
        if qty > material.quantity + QTY_TOL:
            raise GovernanceError("发放数量超出当前库存")
        self.store.append("ACCESS_ISSUED", request.org, {
            "request_id": request_id,
            "qty": qty,
        })

    def complete_access(self, request_id: str) -> None:
        request = self.timeline.requests.get(request_id)
        if request is None:
            raise GovernanceError("取用单不存在")
        if request.status != "issued":
            raise GovernanceError("只有已发放的取用单可以办结")
        self.store.append("ACCESS_COMPLETED", request.org, {"request_id": request_id})

    def return_material(self, request_id: str, qty: float) -> None:
        """未用完的材料退库，数量回补；重放时由投影同步占用数量。"""
        request = self.timeline.requests.get(request_id)
        if request is None:
            raise GovernanceError("取用单不存在")
        if qty <= 0 or qty > request.issued_qty + QTY_TOL:
            raise GovernanceError("退库数量超出已发放数量")
        self.store.append("MATERIAL_RETURNED", request.org, {
            "material_id": request.material_id,
            "qty": qty,
            "request_id": request_id,
        })

    # ===== 试验与随访 ===================================================

    def register_trial(
        self, trial_id: str, title: str, owner_person_id: str,
        bom: dict[str, float], facility_id: str,
    ) -> None:
        if trial_id in self.timeline.trials:
            raise GovernanceError("试验编号已存在")
        owner = self.timeline.persons.get(owner_person_id)
        if owner is None:
            raise GovernanceError("负责人不存在")
        if facility_id not in self.timeline.facilities:
            raise GovernanceError("试验场地不存在")
        for mid, qty in bom.items():
            material = self.timeline.materials.get(mid)
            if material is None:
                raise GovernanceError(f"清单材料不存在: {mid}")
            if qty <= 0:
                raise GovernanceError("清单用量必须为正")
        self.store.append("TRIAL_REGISTERED", owner_person_id, {
            "trial_id": trial_id,
            "title": title,
            "owner_person_id": owner_person_id,
            "owner_org": owner.org,
            "bom": [[mid, qty] for mid, qty in bom.items()],
            "facility_id": facility_id,
        })

    def start_trial(self, trial_id: str) -> None:
        trial = self.timeline.trials.get(trial_id)
        if trial is None:
            raise GovernanceError("试验不存在")
        if trial.status != TrialStatus.PLANNED:
            raise GovernanceError("只有计划中的试验可以启动")
        missing = []
        for mid, planned in trial.bom.items():
            if trial.issued.get(mid, 0.0) + QTY_TOL < planned:
                missing.append(mid)
        if missing:
            raise GovernanceError(f"方案材料尚未按清单发放齐备: {', '.join(missing)}")
        self.store.append("TRIAL_STARTED", trial.owner_person_id, {"trial_id": trial_id})

    def complete_trial(
        self, trial_id: str, follow_ups: list[dict[str, str]] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        trial = self.timeline.trials.get(trial_id)
        if trial is None:
            raise GovernanceError("试验不存在")
        if trial.status != TrialStatus.ONGOING:
            raise GovernanceError("只有进行中的试验可以完成")
        basis = snapshot or {}
        basis.setdefault("decisions", {})
        for mid in trial.bom:
            basis["decisions"][mid] = sorted(
                {
                    rid
                    for rid, req in self.timeline.requests.items()
                    if req.trial_id == trial_id and req.material_id == mid
                }
            )
        self.store.append("TRIAL_COMPLETED", trial.owner_person_id, {
            "trial_id": trial_id,
            "snapshot": basis,
            "follow_ups": follow_ups or [],
        })

    def record_follow_up(self, trial_id: str, followup_id: str, recorder_id: str, outcome: str) -> None:
        self.store.append("TRIAL_FOLLOWUP_RECORDED", recorder_id, {
            "trial_id": trial_id,
            "followup_id": followup_id,
            "recorder_id": recorder_id,
            "outcome": outcome,
        })

    def terminate_trial(self, trial_id: str) -> None:
        trial = self.timeline.trials.get(trial_id)
        if trial is None or trial.status in (TrialStatus.COMPLETED, TrialStatus.TERMINATED):
            raise GovernanceError("试验不存在或已终结")
        self.store.append("TRIAL_TERMINATED", trial.owner_person_id, {"trial_id": trial_id})

    # ===== 事件、冻结与处置责任 =========================================

    def _raise_alert(
        self, kind: str, severity: str, message: str, target_type: str, target_id: str,
    ) -> None:
        for alert in self.timeline.alerts.values():
            if (
                alert.open and alert.kind == kind
                and alert.target_type == target_type and alert.target_id == target_id
            ):
                return
        self.store.append("ALERT_RAISED", "system", {
            "alert_id": _new_id("alrt"),
            "kind": kind,
            "severity": severity,
            "message": message,
            "target_type": target_type,
            "target_id": target_id,
        })

    def record_storage_reading(
        self, facility_id: str, readings: dict[str, float],
        occurred_at: datetime | None = None, idem_key: str | None = None,
    ) -> list[dict[str, str]]:
        """登记保存条件读数；越限立即产生告警，告警在日志中持久。"""
        facility = self.timeline.facilities.get(facility_id)
        if facility is None:
            raise GovernanceError("场地不存在")
        self.store.append("STORAGE_READING", "system", {
            "facility_id": facility_id,
            "readings": readings,
        }, occurred_at=occurred_at, idem_key=idem_key)
        raised = []
        for metric, value in readings.items():
            spec = facility.spec_ranges.get(metric)
            if spec is None:
                continue
            low, high = spec
            if value < low or value > high:
                alert_id = _new_id("alrt")
                message = (
                    f"场地 {facility.name} 保存条件越限：{metric}={value:g}，"
                    f"允许范围 [{low:g}, {high:g}]"
                )
                exists = any(
                    a.open and a.target_id == facility_id and a.kind == f"storage_{metric}"
                    for a in self.timeline.alerts.values()
                )
                if not exists:
                    self.store.append("ALERT_RAISED", "system", {
                        "alert_id": alert_id,
                        "kind": f"storage_{metric}",
                        "severity": "critical",
                        "message": message,
                        "target_type": "facility",
                        "target_id": facility_id,
                    }, occurred_at=occurred_at)
                    raised.append({"alert_id": alert_id, "message": message})
        return raised

    def resolve_alert(self, alert_id: str, resolution: str, actor: str = "system") -> None:
        if alert_id not in self.timeline.alerts:
            raise GovernanceError("告警不存在")
        self.store.append("ALERT_RESOLVED", actor, {
            "alert_id": alert_id,
            "resolution": resolution,
        })

    def _scope_materials(self, kind: IncidentKind, material_ids: Iterable[str],
                         facility_ids: Iterable[str]) -> list[str]:
        tl = self.timeline
        scope: list[str] = []
        seen: set[str] = set()

        def add(mid: str) -> None:
            if mid not in seen and mid in tl.materials:
                seen.add(mid)
                scope.append(mid)

        for mid in material_ids or []:
            add(mid)
            if kind == IncidentKind.CONTAMINATION:
                # 污染可沿已制备的子代扩散，冻结下游衍生材料
                for desc in tl.descendants(mid):
                    add(desc)
        for fid in facility_ids or []:
            for material in tl.materials_at_facility(fid):
                # 场地故障只波及仍在场内存放的材料，已失活/确认丢失的不再处置
                if material.status in (StorageStatus.ACTIVE, StorageStatus.FROZEN):
                    add(material.material_id)
        return scope

    def declare_incident(
        self,
        kind: IncidentKind,
        reason: str,
        declared_by: str,
        *,
        material_ids: Iterable[str] | None = None,
        facility_ids: Iterable[str] | None = None,
        trial_ids: Iterable[str] | None = None,
        incident_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        """登记污染/丢失/场地故障，冻结受影响范围并生成处置责任。"""
        incident_id = incident_id or _new_id("inc")
        if incident_id in self.timeline.incidents:
            raise GovernanceError("事件编号已存在")
        materials = self._scope_materials(kind, material_ids or [], facility_ids or [])
        material_set = set(materials)

        trials = list(trial_ids or [])
        for trial in self.timeline.trials.values():
            if trial.status in (TrialStatus.COMPLETED, TrialStatus.TERMINATED):
                continue
            if material_set & set(trial.bom.keys()):
                if trial.trial_id not in trials:
                    trials.append(trial.trial_id)

        requests = []
        for req in self.timeline.requests.values():
            if req.material_id in material_set and req.status in ("pending_review", "approved"):
                requests.append(req.request_id)

        self.store.append("INCIDENT_DECLARED", declared_by, {
            "incident_id": incident_id,
            "kind": kind.value,
            "reason": reason,
            "declared_by": declared_by,
            "material_ids": materials,
            "trial_ids": trials,
            "request_ids": requests,
        }, occurred_at=occurred_at)
        self.store.append("FREEZE_APPLIED", "system", {
            "incident_id": incident_id,
            "material_ids": materials,
            "trial_ids": trials,
            "request_ids": requests,
        })

        tasks = self._open_dispositions(kind, incident_id, materials, trials, reason)
        for mid in materials:
            self._raise_alert(
                f"incident_{kind.value}", "critical",
                f"事件 {incident_id}（{kind.value}）影响材料 {mid}：{reason}",
                "material", mid,
            )
        return {"incident_id": incident_id, "materials": materials,
                "trials": trials, "requests": requests, "tasks": tasks}

    def _open_dispositions(
        self, kind: IncidentKind, incident_id: str,
        materials: list[str], trials: list[str], reason: str,
    ) -> list[str]:
        tl = self.timeline
        owners: dict[str, list[str]] = {}

        def assign(org: str, item: str) -> None:
            owners.setdefault(org, []).append(item)

        if kind == IncidentKind.STORAGE_FAULT:
            for mid in materials:
                facility = tl.facilities[tl.materials[mid].facility_id]
                assign(facility.owner_org, f"抢修保存设施并评估材料 {mid}")
        elif kind == IncidentKind.CONTAMINATION:
            for mid in materials:
                m = tl.materials[mid]
                if m.status == StorageStatus.FROZEN:
                    assign(m.custodian_org, f"隔离、检测并处置污染材料 {mid}（{reason}）")
            for tid in trials:
                trial = tl.trials[tid]
                assign(trial.owner_org, f"暂停并评估受影响试验 {tid}（{trial.title}）")
        elif kind == IncidentKind.LOSS:
            for mid in materials:
                m = tl.materials[mid]
                assign(m.custodian_org, f"查找丢失材料 {mid} 并按生物安全要求申报")
        elif kind == IncidentKind.LICENSE_REVOKED:
            for mid in materials:
                m = tl.materials[mid]
                assign(m.custodian_org, f"按撤销许可停止使用并处置材料 {mid}")
            for tid in trials:
                trial = tl.trials[tid]
                assign(trial.owner_org, f"停止试验 {tid} 中受撤销许可约束的环节")

        task_ids = []
        for org, items in owners.items():
            task_id = _new_id("task")
            self.store.append("DISPOSITION_TASK_OPENED", "system", {
                "task_id": task_id,
                "incident_id": incident_id,
                "owner_org": org,
                "owner_person": None,
                "description": "；".join(items),
                "kind": kind.value,
            })
            task_ids.append(task_id)
        return task_ids

    def close_disposition(self, task_id: str, closer_id: str, result: str) -> None:
        if task_id not in self.timeline.tasks:
            raise GovernanceError("处置任务不存在")
        task = self.timeline.tasks[task_id]
        if not task.open:
            raise GovernanceError("处置任务已关闭")
        self.store.append("DISPOSITION_TASK_CLOSED", closer_id, {
            "task_id": task_id,
            "closer_id": closer_id,
            "result": result,
        })

    def resolve_incident(self, incident_id: str, note: str, actor: str = "system") -> None:
        """解除事件冻结；解冻后重新评估挂起取用，处置任务不自动关闭。

        硬性条件仍然不满足（如许可已撤销）的取用单转为拒绝；
        高风险例外继续等待独立复核；其余恢复批准。责任任务保留留痕。
        """
        incident = self.timeline.incidents.get(incident_id)
        if incident is None:
            raise GovernanceError("事件不存在")
        if incident.resolved_at is not None:
            raise GovernanceError("事件已解决")
        self.store.append("FREEZE_LIFTED", actor, {
            "incident_id": incident_id,
            "material_ids": incident.material_ids,
            "trial_ids": incident.trial_ids,
            "request_ids": incident.request_ids,
        })
        self.store.append("INCIDENT_RESOLVED", actor, {
            "incident_id": incident_id,
            "note": note,
        })
        for rid in incident.request_ids:
            request = self.timeline.requests.get(rid)
            if request is None or request.status not in ("pending_review", "approved"):
                continue
            fresh = self._evaluate_access(
                request.material_id, request.applicant_id, request.purpose,
                request.qty, request.target_facility_id, request.trial_id,
                request.exception_requested, current_request_id=rid,
            )
            if fresh["reasons"]:
                # 依据已失效（如许可撤销），即使原本已批准也降级为拒绝
                new_status = "denied"
            elif request.status == "approved":
                new_status = "approved"  # 批准保留
            elif request.high_risk:
                new_status = "pending_review"
            else:
                new_status = "approved"
            if new_status != request.status or not fresh["allowed"]:
                self.store.append("ACCESS_DECIDED", "system", {
                    "request_id": rid,
                    "status": new_status,
                    "high_risk": request.high_risk,
                    "decision": fresh,
                })

    def revoke_license(self, license_id: str, reason: str, actor: str = "system") -> dict[str, Any]:
        """许可撤销：冻结所有仍受其约束的在库材料、取用与试验。"""
        license_ = self.timeline.licenses.get(license_id)
        if license_ is None:
            raise GovernanceError("许可不存在")
        if license_.revoked:
            raise GovernanceError("许可已撤销")
        self.store.append("LICENSE_REVOKED", actor, {
            "license_id": license_id,
            "reason": reason,
        })
        # 只冻结撤销后再无其他有效许可覆盖的材料；仍有其他许可覆盖的，
        # 相关机构在发放时按其机构维度重新判定，材料不整体冻结
        material_ids = []
        for mid, material in self.timeline.materials.items():
            if material.status not in (StorageStatus.ACTIVE, StorageStatus.FROZEN):
                continue
            effective = [
                lic.license_id for lic in self.timeline.effective_licenses(mid)
                if lic.valid_at(self.clock())
            ]
            if not effective:
                material_ids.append(mid)
        return self.declare_incident(
            IncidentKind.LICENSE_REVOKED, f"许可 {license_id} 撤销：{reason}", actor,
            material_ids=material_ids,
        )

    # ===== 搜索与可见性 =================================================

    def search_materials(self, org: str, query: str) -> list[dict[str, Any]]:
        """在机构可见范围内搜索。

        可见集合 = 该机构当前保管的材料 ∪ 适用许可的持有方包含该机构的材料。
        未授权机构对受限资源连“是否存在”都无法推断：既无命中文档，也无空字段提示。
        """
        q = query.strip().lower()
        result = []
        for m in self.timeline.visible_materials(org):
            haystack = " ".join([
                m.material_id, m.name or "", m.batch or "", m.material_type.value
            ]).lower()
            if not q or q in haystack:
                result.append({
                    "material_id": m.material_id,
                    "name": m.name,
                    "type": m.material_type.value,
                    "batch": m.batch,
                    "bsl": m.bsl.name,
                    "status": m.status.value,
                    "quantity": m.quantity,
                    "unit": m.unit.value,
                    "custodian_org": m.custodian_org,
                })
        return result

    # ===== 成果登记与追溯 ===============================================

    def register_output(
        self, output_id: str, trial_id: str, kind: OutputKind, title: str,
        reference: str = "",
    ) -> dict[str, Any]:
        trial = self.timeline.trials.get(trial_id)
        if trial is None:
            raise GovernanceError("试验不存在")
        if output_id in self.timeline.outputs:
            raise GovernanceError("成果编号已存在")
        trace = self.trace_trial(trial_id)
        snapshot = trace["benefit_terms"]
        self.store.append("OUTPUT_REGISTERED", trial.owner_person_id, {
            "output_id": output_id,
            "trial_id": trial_id,
            "kind": kind.value,
            "title": title,
            "reference": reference,
            "benefit_snapshot": snapshot,
        })
        return {"output_id": output_id, "benefit_snapshot": snapshot}

    def trace_trial(self, trial_id: str) -> dict[str, Any]:
        """列出试验所用每份材料的完整谱系、每次转移与适用合作权益。"""
        trial = self.timeline.trials.get(trial_id)
        if trial is None:
            raise GovernanceError("试验不存在")
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        transfers: list[dict[str, Any]] = []
        benefit: dict[tuple[str, str], dict[str, Any]] = {}

        def walk(mid: str, depth: int = 0) -> None:
            material = self.timeline.materials[mid]
            if mid not in nodes:
                nodes[mid] = {
                    "material_id": mid,
                    "name": material.name,
                    "type": material.material_type.value,
                    "batch": material.batch,
                    "derivation": material.derivation,
                    "source_org": material.source_org,
                    "custodian_org": material.custodian_org,
                    "bsl": material.bsl.name,
                    "is_root": not material.parents,
                }
            for pid, qty in material.parents:
                edges.append({"from": pid, "to": mid, "qty": qty,
                              "via": material.derivation})
                walk(pid, depth + 1)
            transfer_id = self.timeline.incoming_transfer.get(mid)
            if transfer_id is not None and not any(
                t["transfer_id"] == transfer_id for t in transfers
            ):
                t = self.timeline.transfers[transfer_id]
                transfers.append({
                    "transfer_id": t.transfer_id,
                    "material_id": t.material_id,
                    "received_as": mid,
                    "qty": t.qty,
                    "from_org": t.from_org,
                    "to_org": t.to_org,
                    "from_facility": t.from_facility,
                    "to_facility": t.to_facility,
                    "dispatched_at": t.dispatched_at,
                    "received_at": t.received_at,
                    "confirmed": t.confirmed,
                    "disputed": t.disputed,
                    "evidence_count": len(t.evidence_ids),
                })

        for mid in trial.bom:
            walk(mid)
            for license_ in self.timeline.effective_licenses(mid):
                for term in license_.benefit_terms:
                    key = (license_.license_id, term.clause)
                    benefit.setdefault(key, {
                        "license_id": license_.license_id,
                        "license_title": license_.title,
                        "party_org": term.party_org,
                        "clause": term.clause,
                        "share": term.share,
                        "scope": term.scope,
                    })

        return {
            "trial_id": trial_id,
            "title": trial.title,
            "nodes": list(nodes.values()),
            "edges": edges,
            "transfers": transfers,
            "benefit_terms": list(benefit.values()),
        }

    def trace_output(self, output_id: str) -> dict[str, Any]:
        output = self.timeline.outputs.get(output_id)
        if output is None:
            raise GovernanceError("成果不存在")
        trace = self.trace_trial(output.trial_id)
        return {
            "output_id": output_id,
            "kind": output.kind.value,
            "title": output.title,
            "reference": output.reference,
            "registered_at": output.registered_at,
            "benefit_snapshot_at_registration": output.benefit_snapshot,
            "lineage": trace,
        }

    # ===== 管理视图（全部可重放恢复）====================================

    def dashboard(self) -> dict[str, Any]:
        moment = self.clock()
        return {
            "open_alerts": [
                {"alert_id": a.alert_id, "kind": a.kind, "severity": a.severity,
                 "message": a.message, "target": f"{a.target_type}:{a.target_id}"}
                for a in self.timeline.open_alerts()
            ],
            "pending_reviews": [
                {"request_id": r.request_id, "material_id": r.material_id,
                 "applicant_id": r.applicant_id, "high_risk": r.high_risk,
                 "frozen": bool(r.frozen_incidents),
                 "reasons": r.decision.get("reasons", [])}
                for r in self.timeline.pending_reviews()
            ],
            "due_follow_ups": [
                {"trial_id": tid, "followup_id": fu.followup_id,
                 "due_at": fu.due_at, "note": fu.note}
                for tid, fu in self.timeline.due_follow_ups(moment)
            ],
            "open_dispositions": [
                {"task_id": t.task_id, "incident_id": t.incident_id,
                 "owner_org": t.owner_org, "description": t.description, "kind": t.kind}
                for t in self.timeline.open_dispositions()
            ],
        }
