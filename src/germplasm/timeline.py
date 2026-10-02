"""事件时间线投影。

事件日志是事实来源，本模块在重放时重建全部可执行状态：
机构、人员、场地、许可、材料谱系与数量、转移与证据、取用单与审批、
试验、事件冻结与处置责任、告警、盘点、成果。

冻结按事件隔离：材料/试验/取用单分别记录被哪些未决事件冻结，
一个事件解除时不影响其他事件造成的冻结；已完成环节的状态与
当时快照永不被追溯修改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .events import Event
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


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class BenefitTerm:
    """合作权益条款，随原始许可沿谱系适用于衍生成果。"""

    party_org: str
    clause: str
    share: str | None = None
    scope: str = "output"


@dataclass
class License:
    license_id: str
    title: str
    licensor_org: str
    holder_orgs: set[str]
    allowed_purposes: set[str]
    valid_from: str | None
    valid_to: str | None
    max_bsl: BiosafetyLevel
    terms: str
    derivatives_allowed: bool = True
    benefit_terms: list[BenefitTerm] = field(default_factory=list)
    revoked: bool = False
    revoked_at: str | None = None
    revoke_reason: str | None = None

    def valid_at(self, moment: datetime) -> bool:
        if self.revoked:
            return False
        if self.valid_from and moment < parse_dt(self.valid_from):
            return False
        if self.valid_to and moment > parse_dt(self.valid_to):
            return False
        return True


@dataclass
class Facility:
    facility_id: str
    name: str
    owner_org: str
    bsl_limit: BiosafetyLevel
    capacity_total: float       # 保存容量
    work_capacity_total: float  # 试验取用/在位使用容量
    spec_ranges: dict[str, tuple[float, float]]  # 例如 {"temp_c": (-85.0, -60.0)}


@dataclass
class Person:
    person_id: str
    name: str
    org: str
    qualifications: set[str]
    bsl_allowed: BiosafetyLevel
    is_biosafety_reviewer: bool = False
    active: bool = True


@dataclass
class LedgerEntry:
    seq: int
    delta: float
    reason: str


@dataclass
class Material:
    material_id: str
    material_type: MaterialType
    name: str
    unit: Unit
    bsl: BiosafetyLevel
    source_org: str
    license_ids: list[str]          # 沿谱系继承到的原始许可
    batch: str
    storage_label: str              # 保存条件要求，例如 ultra_low
    parents: list[tuple[str, float]] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    derivation: str = "receipt"     # receipt/split/mix/propagate
    quantity: float = 0.0
    facility_id: str | None = None
    custodian_org: str | None = None
    custodian_person: str | None = None
    status: StorageStatus = StorageStatus.ACTIVE
    frozen_incidents: set[str] = field(default_factory=set)
    inactivated_qty: float = 0.0
    received_event_seq: int = 0
    ledger: list[LedgerEntry] = field(default_factory=list)
    receipt_evidence: list[str] = field(default_factory=list)
    basis_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def frozen(self) -> bool:
        return bool(self.frozen_incidents)


@dataclass
class Evidence:
    evidence_id: str
    target_type: str   # receipt / transfer
    target_id: str
    side: EvidenceSide
    kind: EvidenceKind
    file_ref: str
    content_sha256: str
    observed_qty: float | None
    observed_identity: str | None
    condition_note: str
    submitted_by: str
    occurred_at: str
    recorded_at: str


@dataclass
class TransferRecord:
    transfer_id: str
    material_id: str
    qty: float
    from_org: str
    from_person: str | None
    from_facility: str | None
    to_org: str
    to_person: str | None
    to_facility: str | None
    dispatched_at: str | None = None
    received_at: str | None = None
    confirmed: bool = False
    disputed: bool = False
    discrepancy: str | None = None
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ReviewRecord:
    reviewer_id: str
    approved: bool
    note: str
    at: str


@dataclass
class AccessRequest:
    request_id: str
    material_id: str
    applicant_id: str
    org: str
    purpose: str
    qty: float
    target_facility_id: str
    trial_id: str | None
    exception_requested: bool
    created_at: str
    status: str = "pending_review"  # approved/denied/pending_review/issued/completed/cancelled
    high_risk: bool = False
    issued_qty: float = 0.0
    decision: dict[str, Any] = field(default_factory=dict)
    reviews: list[ReviewRecord] = field(default_factory=list)
    frozen_incidents: set[str] = field(default_factory=set)


@dataclass
class FollowUp:
    followup_id: str
    due_at: str
    note: str
    recorded_at: str | None = None
    recorder_id: str | None = None
    outcome: str | None = None


@dataclass
class Trial:
    trial_id: str
    title: str
    owner_person_id: str
    owner_org: str
    bom: dict[str, float]           # material_id -> 计划用量
    facility_id: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    status: TrialStatus = TrialStatus.PLANNED
    issued: dict[str, float] = field(default_factory=dict)
    frozen_incidents: set[str] = field(default_factory=set)
    follow_ups: list[FollowUp] = field(default_factory=list)
    completion_snapshot: dict[str, Any] | None = None


@dataclass
class DispositionTask:
    task_id: str
    incident_id: str
    owner_org: str
    owner_person: str | None
    description: str
    kind: str
    created_at: str
    closed_at: str | None = None
    closer_id: str | None = None
    result: str | None = None

    @property
    def open(self) -> bool:
        return self.closed_at is None


@dataclass
class Incident:
    incident_id: str
    kind: IncidentKind
    reason: str
    declared_by: str
    declared_at: str
    material_ids: list[str]
    trial_ids: list[str]
    request_ids: list[str]
    tasks: list[str] = field(default_factory=list)
    resolved_at: str | None = None
    resolution_note: str | None = None


@dataclass
class Alert:
    alert_id: str
    kind: str
    severity: str
    message: str
    target_type: str
    target_id: str
    raised_at: str
    resolved_at: str | None = None
    resolution: str | None = None

    @property
    def open(self) -> bool:
        return self.resolved_at is None


@dataclass
class StocktakeRecord:
    stocktake_id: str
    material_id: str
    expected_qty: float
    counted_qty: float
    matched: bool
    counter_id: str
    at: str
    note: str | None


@dataclass
class OutputRecord:
    output_id: str
    trial_id: str
    kind: OutputKind
    title: str
    reference: str
    registered_at: str
    benefit_snapshot: list[dict[str, Any]] = field(default_factory=list)


class Timeline:
    """事件投影，同时提供谱系、容量、可见性等查询。"""

    def __init__(self) -> None:
        self.orgs: dict[str, str] = {}
        self.facilities: dict[str, Facility] = {}
        self.persons: dict[str, Person] = {}
        self.licenses: dict[str, License] = {}
        self.materials: dict[str, Material] = {}
        self.evidence: dict[str, Evidence] = {}
        self.transfers: dict[str, TransferRecord] = {}
        self.requests: dict[str, AccessRequest] = {}
        self.trials: dict[str, Trial] = {}
        self.incidents: dict[str, Incident] = {}
        self.tasks: dict[str, DispositionTask] = {}
        self.alerts: dict[str, Alert] = {}
        self.stocktakes: dict[str, StocktakeRecord] = {}
        self.outputs: dict[str, OutputRecord] = {}
        self._in_transit: dict[str, float] = {}
        self.incoming_transfer: dict[str, str] = {}  # 接收生成的新材料 -> 转移单

    # ---- 重放 ----------------------------------------------------------

    def apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            raise ValueError(f"未知事件类型: {event.type}")
        handler(event)

    def _get(self, store: dict[str, Any], key: str, label: str) -> Any:
        if key not in store:
            raise ValueError(f"{label}不存在: {key}")
        return store[key]

    def _on_ORG_REGISTERED(self, e: Event) -> None:
        self.orgs[e.payload["org_id"]] = e.payload["name"]

    def _on_FACILITY_REGISTERED(self, e: Event) -> None:
        p = e.payload
        self.facilities[p["facility_id"]] = Facility(
            facility_id=p["facility_id"],
            name=p["name"],
            owner_org=p["owner_org"],
            bsl_limit=BiosafetyLevel(p["bsl_limit"]),
            capacity_total=float(p["capacity_total"]),
            work_capacity_total=float(p.get("work_capacity_total", p["capacity_total"])),
            spec_ranges={k: tuple(v) for k, v in p.get("spec_ranges", {}).items()},
        )

    def _on_PERSON_REGISTERED(self, e: Event) -> None:
        p = e.payload
        self.persons[p["person_id"]] = Person(
            person_id=p["person_id"],
            name=p["name"],
            org=p["org"],
            qualifications=set(p.get("qualifications", [])),
            bsl_allowed=BiosafetyLevel(p["bsl_allowed"]),
            is_biosafety_reviewer=p.get("is_biosafety_reviewer", False),
            active=p.get("active", True),
        )

    def _on_LICENSE_REGISTERED(self, e: Event) -> None:
        p = e.payload
        self.licenses[p["license_id"]] = License(
            license_id=p["license_id"],
            title=p["title"],
            licensor_org=p["licensor_org"],
            holder_orgs=set(p["holder_orgs"]),
            allowed_purposes=set(p["allowed_purposes"]),
            valid_from=p.get("valid_from"),
            valid_to=p.get("valid_to"),
            max_bsl=BiosafetyLevel(p["max_bsl"]),
            terms=p.get("terms", ""),
            derivatives_allowed=p.get("derivatives_allowed", True),
            benefit_terms=[BenefitTerm(**bt) for bt in p.get("benefit_terms", [])],
        )

    def _material_from_payload(self, e: Event, p: dict[str, Any]) -> Material:
        parents = [(pid, float(qty)) for pid, qty in p.get("parents", [])]
        inputs = [(pid, float(qty)) for pid, qty in p.get("inputs", [])]
        for pid, _ in parents:
            self.materials[pid].children.append(p["material_id"])
        material = Material(
            material_id=p["material_id"],
            material_type=MaterialType(p["material_type"]),
            name=p["name"],
            unit=Unit(p["unit"]),
            bsl=BiosafetyLevel(p["bsl"]),
            source_org=p["source_org"],
            license_ids=list(p["license_ids"]),
            batch=p["batch"],
            storage_label=p.get("storage_label", ""),
            parents=parents,
            derivation=p.get("derivation", "receipt"),
            quantity=float(p["output_qty"]),
            facility_id=p.get("facility_id"),
            custodian_org=p.get("custodian_org"),
            custodian_person=p.get("custodian_person"),
            received_event_seq=e.seq,
            basis_snapshot=p.get("basis_snapshot", {}),
            ledger=[LedgerEntry(e.seq, float(p["output_qty"]), p.get("derivation", "receipt"))],
            receipt_evidence=list(p.get("evidence_ids", [])),
        )
        self.materials[material.material_id] = material
        # 拆分与混合按实际投入扣减亲本库存；繁育的亲本保留，inputs 为空
        for pid, qty in inputs:
            parent = self.materials[pid]
            parent.quantity -= qty
            parent.ledger.append(
                LedgerEntry(e.seq, -qty, f"{material.derivation}_input:{material.material_id}")
            )
        return material

    def _on_MATERIAL_RECEIVED(self, e: Event) -> None:
        self._material_from_payload(e, e.payload)

    def _on_MATERIAL_SPLIT(self, e: Event) -> None:
        self._material_from_payload(e, e.payload)

    def _on_MATERIAL_MIXED(self, e: Event) -> None:
        self._material_from_payload(e, e.payload)

    def _on_MATERIAL_PROPAGATED(self, e: Event) -> None:
        self._material_from_payload(e, e.payload)

    def _on_MATERIAL_CONSUMED(self, e: Event) -> None:
        p = e.payload
        material = self._get(self.materials, p["material_id"], "材料")
        material.quantity -= float(p["qty"])
        material.ledger.append(LedgerEntry(e.seq, -float(p["qty"]), p["reason"]))

    def _on_MATERIAL_INACTIVATED(self, e: Event) -> None:
        p = e.payload
        material = self._get(self.materials, p["material_id"], "材料")
        material.quantity -= float(p["qty"])
        material.inactivated_qty += float(p["qty"])
        material.ledger.append(LedgerEntry(e.seq, -float(p["qty"]), f"inactivated:{p.get('reason', '')}"))
        if material.quantity <= 1e-9:
            material.quantity = 0.0
            material.status = StorageStatus.INACTIVATED

    def _on_MATERIAL_RETURNED(self, e: Event) -> None:
        p = e.payload
        material = self._get(self.materials, p["material_id"], "材料")
        material.quantity += float(p["qty"])
        material.ledger.append(LedgerEntry(e.seq, float(p["qty"]), "returned"))
        request_id = p.get("request_id")
        if request_id and request_id in self.requests:
            request = self.requests[request_id]
            request.issued_qty -= float(p["qty"])
            if request.trial_id:
                trial = self.trials[request.trial_id]
                issued = trial.issued.get(p["material_id"], 0.0) - float(p["qty"])
                if issued > 0:
                    trial.issued[p["material_id"]] = issued
                else:
                    trial.issued.pop(p["material_id"], None)

    def _on_MATERIAL_LOST(self, e: Event) -> None:
        p = e.payload
        material = self._get(self.materials, p["material_id"], "材料")
        if material.quantity > 0:
            material.ledger.append(LedgerEntry(e.seq, -material.quantity, "confirmed_loss"))
            material.quantity = 0.0
        material.status = StorageStatus.LOST

    def _on_EVIDENCE_SUBMITTED(self, e: Event) -> None:
        p = e.payload
        evidence = Evidence(
            evidence_id=p["evidence_id"],
            target_type=p["target_type"],
            target_id=p["target_id"],
            side=EvidenceSide(p["side"]),
            kind=EvidenceKind(p["kind"]),
            file_ref=p["file_ref"],
            content_sha256=p["content_sha256"],
            observed_qty=p.get("observed_qty"),
            observed_identity=p.get("observed_identity"),
            condition_note=p.get("condition_note", ""),
            submitted_by=p["submitted_by"],
            occurred_at=p["occurred_at"],
            recorded_at=e.recorded_at,
        )
        self.evidence[p["evidence_id"]] = evidence
        if p["target_type"] == "transfer":
            transfer = self._get(self.transfers, p["target_id"], "转移")
            transfer.evidence_ids.append(p["evidence_id"])
        elif p["target_type"] == "receipt":
            material = self._get(self.materials, p["target_id"], "材料")
            material.receipt_evidence.append(p["evidence_id"])

    def _on_TRANSFER_DISPATCHED(self, e: Event) -> None:
        p = e.payload
        transfer = TransferRecord(
            transfer_id=p["transfer_id"],
            material_id=p["material_id"],
            qty=float(p["qty"]),
            from_org=p["from_org"],
            from_person=p.get("from_person"),
            from_facility=p.get("from_facility"),
            to_org=p["to_org"],
            to_person=p.get("to_person"),
            to_facility=p.get("to_facility"),
            dispatched_at=p.get("occurred_at", e.occurred_at),
        )
        self.transfers[p["transfer_id"]] = transfer
        material = self._get(self.materials, p["material_id"], "材料")
        material.quantity -= float(p["qty"])
        material.ledger.append(LedgerEntry(e.seq, -float(p["qty"]), f"transfer_dispatched:{p['transfer_id']}"))
        self._in_transit[p["transfer_id"]] = float(p["qty"])

    def _on_TRANSFER_RECEIVED(self, e: Event) -> None:
        p = e.payload
        transfer = self._get(self.transfers, p["transfer_id"], "转移")
        source = self._get(self.materials, transfer.material_id, "材料")
        new = Material(
            material_id=p["to_material_id"],
            material_type=source.material_type,
            name=source.name,
            unit=source.unit,
            bsl=source.bsl,
            source_org=transfer.to_org,
            license_ids=list(dict.fromkeys(source.license_ids + p.get("extra_license_ids", []))),
            batch=p.get("batch", source.batch),
            storage_label=p.get("storage_label", source.storage_label),
            parents=[(source.material_id, transfer.qty)],
            derivation="transfer",
            quantity=transfer.qty,
            facility_id=transfer.to_facility,
            custodian_org=transfer.to_org,
            custodian_person=transfer.to_person,
            received_event_seq=e.seq,
            ledger=[LedgerEntry(e.seq, transfer.qty, f"transfer_received:{transfer.transfer_id}")],
        )
        self.materials[new.material_id] = new
        source.children.append(new.material_id)
        self._in_transit.pop(transfer.transfer_id, None)
        self.incoming_transfer[new.material_id] = transfer.transfer_id
        transfer.received_at = p.get("occurred_at", e.occurred_at)

    def _on_TRANSFER_CONFIRMED(self, e: Event) -> None:
        p = e.payload
        transfer = self._get(self.transfers, p["transfer_id"], "转移")
        transfer.confirmed = True
        transfer.disputed = False
        transfer.discrepancy = None

    def _on_TRANSFER_DISPUTED(self, e: Event) -> None:
        p = e.payload
        transfer = self._get(self.transfers, p["transfer_id"], "转移")
        transfer.disputed = True
        transfer.discrepancy = p["discrepancy"]

    def _on_ACCESS_REQUESTED(self, e: Event) -> None:
        p = e.payload
        self.requests[p["request_id"]] = AccessRequest(
            request_id=p["request_id"],
            material_id=p["material_id"],
            applicant_id=p["applicant_id"],
            org=p["org"],
            purpose=p["purpose"],
            qty=float(p["qty"]),
            target_facility_id=p["target_facility_id"],
            trial_id=p.get("trial_id"),
            exception_requested=p.get("exception_requested", False),
            created_at=p.get("occurred_at", e.occurred_at),
            status="pending_review",
        )

    def _on_ACCESS_DECIDED(self, e: Event) -> None:
        p = e.payload
        request = self._get(self.requests, p["request_id"], "取用单")
        request.status = p["status"]  # approved / denied / pending_review
        request.high_risk = p.get("high_risk", False)
        request.decision = p["decision"]

    def _on_ACCESS_REVIEWED(self, e: Event) -> None:
        p = e.payload
        request = self._get(self.requests, p["request_id"], "取用单")
        request.reviews.append(ReviewRecord(
            reviewer_id=p["reviewer_id"],
            approved=bool(p["approved"]),
            note=p.get("note", ""),
            at=e.occurred_at,
        ))
        request.status = "approved" if p["approved"] else "denied"

    def _on_ACCESS_ISSUED(self, e: Event) -> None:
        p = e.payload
        request = self._get(self.requests, p["request_id"], "取用单")
        request.status = "issued"
        request.issued_qty += float(p["qty"])
        material = self._get(self.materials, request.material_id, "材料")
        material.quantity -= float(p["qty"])
        material.ledger.append(LedgerEntry(e.seq, -float(p["qty"]), f"access:{request.request_id}"))
        if request.trial_id:
            trial = self._get(self.trials, request.trial_id, "试验")
            trial.issued[request.material_id] = trial.issued.get(request.material_id, 0.0) + float(p["qty"])

    def _on_ACCESS_COMPLETED(self, e: Event) -> None:
        request = self._get(self.requests, e.payload["request_id"], "取用单")
        request.status = "completed"

    def _on_TRIAL_REGISTERED(self, e: Event) -> None:
        p = e.payload
        self.trials[p["trial_id"]] = Trial(
            trial_id=p["trial_id"],
            title=p["title"],
            owner_person_id=p["owner_person_id"],
            owner_org=p["owner_org"],
            bom={mid: float(qty) for mid, qty in p["bom"]},
            facility_id=p["facility_id"],
            created_at=p.get("occurred_at", e.occurred_at),
        )

    def _on_TRIAL_STARTED(self, e: Event) -> None:
        trial = self._get(self.trials, e.payload["trial_id"], "试验")
        trial.status = TrialStatus.ONGOING
        trial.started_at = e.occurred_at

    def _on_TRIAL_COMPLETED(self, e: Event) -> None:
        p = e.payload
        trial = self._get(self.trials, p["trial_id"], "试验")
        trial.status = TrialStatus.COMPLETED
        trial.completed_at = e.occurred_at
        trial.completion_snapshot = p.get("snapshot", {})
        for fu in p.get("follow_ups", []):
            trial.follow_ups.append(FollowUp(
                followup_id=fu["followup_id"], due_at=fu["due_at"], note=fu.get("note", "")
            ))

    def _on_TRIAL_TERMINATED(self, e: Event) -> None:
        trial = self._get(self.trials, e.payload["trial_id"], "试验")
        trial.status = TrialStatus.TERMINATED

    def _on_TRIAL_FOLLOWUP_RECORDED(self, e: Event) -> None:
        p = e.payload
        trial = self._get(self.trials, p["trial_id"], "试验")
        for fu in trial.follow_ups:
            if fu.followup_id == p["followup_id"]:
                fu.recorded_at = e.occurred_at
                fu.recorder_id = p["recorder_id"]
                fu.outcome = p.get("outcome")
                return
        raise ValueError("随访任务不存在")

    def _on_INCIDENT_DECLARED(self, e: Event) -> None:
        p = e.payload
        self.incidents[p["incident_id"]] = Incident(
            incident_id=p["incident_id"],
            kind=IncidentKind(p["kind"]),
            reason=p["reason"],
            declared_by=p["declared_by"],
            declared_at=e.occurred_at,
            material_ids=list(p.get("material_ids", [])),
            trial_ids=list(p.get("trial_ids", [])),
            request_ids=list(p.get("request_ids", [])),
        )

    def _on_FREEZE_APPLIED(self, e: Event) -> None:
        p = e.payload
        incident_id = p["incident_id"]
        for mid in p.get("material_ids", []):
            material = self.materials.get(mid)
            if material is not None and material.status in (StorageStatus.ACTIVE, StorageStatus.FROZEN):
                material.frozen_incidents.add(incident_id)
                if material.status == StorageStatus.ACTIVE:
                    material.status = StorageStatus.FROZEN
        for tid in p.get("trial_ids", []):
            trial = self.trials.get(tid)
            if trial is not None and trial.status in (TrialStatus.PLANNED, TrialStatus.ONGOING):
                trial.frozen_incidents.add(incident_id)
                trial.status = TrialStatus.FROZEN
        for rid in p.get("request_ids", []):
            request = self.requests.get(rid)
            if request is not None and request.status not in ("denied", "completed", "cancelled", "issued"):
                request.frozen_incidents.add(incident_id)

    def _on_FREEZE_LIFTED(self, e: Event) -> None:
        p = e.payload
        incident_id = p["incident_id"]
        for mid in p.get("material_ids", []):
            material = self.materials.get(mid)
            if material is None:
                continue
            material.frozen_incidents.discard(incident_id)
            if not material.frozen_incidents and material.status == StorageStatus.FROZEN:
                material.status = StorageStatus.ACTIVE
        for tid in p.get("trial_ids", []):
            trial = self.trials.get(tid)
            if trial is None:
                continue
            trial.frozen_incidents.discard(incident_id)
            if not trial.frozen_incidents and trial.status == TrialStatus.FROZEN:
                trial.status = TrialStatus.ONGOING if trial.started_at else TrialStatus.PLANNED
        for rid in p.get("request_ids", []):
            request = self.requests.get(rid)
            if request is not None:
                request.frozen_incidents.discard(incident_id)
                # 取用单状态由服务层在解冻后重新评估并追加 ACCESS_DECIDED，
                # 保证重放确定性，不在投影层自行恢复。

    def _on_INCIDENT_RESOLVED(self, e: Event) -> None:
        incident = self._get(self.incidents, e.payload["incident_id"], "事件")
        incident.resolved_at = e.occurred_at
        incident.resolution_note = e.payload.get("note")

    def _on_DISPOSITION_TASK_OPENED(self, e: Event) -> None:
        p = e.payload
        task = DispositionTask(
            task_id=p["task_id"],
            incident_id=p["incident_id"],
            owner_org=p["owner_org"],
            owner_person=p.get("owner_person"),
            description=p["description"],
            kind=p["kind"],
            created_at=e.occurred_at,
        )
        self.tasks[p["task_id"]] = task
        self.incidents[p["incident_id"]].tasks.append(p["task_id"])

    def _on_DISPOSITION_TASK_CLOSED(self, e: Event) -> None:
        p = e.payload
        task = self._get(self.tasks, p["task_id"], "处置任务")
        task.closed_at = e.occurred_at
        task.closer_id = p["closer_id"]
        task.result = p.get("result")

    def _on_LICENSE_REVOKED(self, e: Event) -> None:
        license_ = self._get(self.licenses, e.payload["license_id"], "许可")
        license_.revoked = True
        license_.revoked_at = e.occurred_at
        license_.revoke_reason = e.payload.get("reason")

    def _on_STORAGE_READING(self, e: Event) -> None:
        # 读数本身只存档；越限由服务层评估后追加 ALERT_RAISED。
        return

    def _on_ALERT_RAISED(self, e: Event) -> None:
        p = e.payload
        self.alerts[p["alert_id"]] = Alert(
            alert_id=p["alert_id"],
            kind=p["kind"],
            severity=p["severity"],
            message=p["message"],
            target_type=p["target_type"],
            target_id=p["target_id"],
            raised_at=e.occurred_at,
        )

    def _on_ALERT_RESOLVED(self, e: Event) -> None:
        alert = self._get(self.alerts, e.payload["alert_id"], "告警")
        alert.resolved_at = e.occurred_at
        alert.resolution = e.payload.get("resolution")

    def _on_STOCKTAKE_RECORDED(self, e: Event) -> None:
        p = e.payload
        self.stocktakes[p["stocktake_id"]] = StocktakeRecord(
            stocktake_id=p["stocktake_id"],
            material_id=p["material_id"],
            expected_qty=float(p["expected_qty"]),
            counted_qty=float(p["counted_qty"]),
            matched=bool(p["matched"]),
            counter_id=p["counter_id"],
            at=e.occurred_at,
            note=p.get("note"),
        )

    def _on_OUTPUT_REGISTERED(self, e: Event) -> None:
        p = e.payload
        self.outputs[p["output_id"]] = OutputRecord(
            output_id=p["output_id"],
            trial_id=p["trial_id"],
            kind=OutputKind(p["kind"]),
            title=p["title"],
            reference=p.get("reference", ""),
            registered_at=e.occurred_at,
            benefit_snapshot=list(p.get("benefit_snapshot", [])),
        )

    # ---- 查询 ----------------------------------------------------------

    def lineage_roots(self, material_id: str) -> list[str]:
        """沿第一亲本链之外完整回溯，返回所有接收入库的原始材料。"""
        roots: list[str] = []
        seen: set[str] = set()

        def walk(mid: str) -> None:
            if mid in seen:
                return
            seen.add(mid)
            material = self.materials[mid]
            if not material.parents:
                roots.append(mid)
            else:
                for pid, _ in material.parents:
                    walk(pid)

        walk(material_id)
        return roots

    def descendants(self, material_id: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        def walk(mid: str) -> None:
            for cid in self.materials[mid].children:
                if cid not in seen:
                    seen.add(cid)
                    result.append(cid)
                    walk(cid)

        walk(material_id)
        return result

    def effective_licenses(self, material_id: str) -> list[License]:
        material = self.materials[material_id]
        by_id = {lid: self.licenses[lid] for lid in material.license_ids if lid in self.licenses}
        for root_id in self.lineage_roots(material_id):
            for lid in self.materials[root_id].license_ids:
                if lid in self.licenses:
                    by_id.setdefault(lid, self.licenses[lid])
        return list(by_id.values())

    def materials_at_facility(self, facility_id: str) -> list[Material]:
        return [m for m in self.materials.values() if m.facility_id == facility_id]

    def occupancy(self, facility_id: str) -> float:
        """在场且未失活/未确认丢失的材料均占用保存位（冻结材料仍在场内）。"""
        return sum(
            m.quantity
            for m in self.materials_at_facility(facility_id)
            if m.status in (StorageStatus.ACTIVE, StorageStatus.FROZEN)
        )

    def visible_materials(self, org: str) -> list[Material]:
        """机构只能看到其作为许可持有方或当前保管方的材料。

        受限资源对未授权机构不可见：搜索在该集合内进行，
        未授权机构无法通过名称、编号或命中数量推断资源存在。
        """
        result = []
        for material in self.materials.values():
            if material.custodian_org == org:
                result.append(material)
                continue
            if any(org in license.holder_orgs for license in self.effective_licenses(material.material_id)):
                result.append(material)
        return result

    def transfers_of(self, material_id: str) -> list[TransferRecord]:
        return [t for t in self.transfers.values() if t.material_id == material_id]

    def open_alerts(self) -> list[Alert]:
        return [a for a in self.alerts.values() if a.open]

    def pending_reviews(self) -> list[AccessRequest]:
        return [r for r in self.requests.values() if r.status == "pending_review"]

    def due_follow_ups(self, moment: datetime) -> list[tuple[str, FollowUp]]:
        due: list[tuple[str, FollowUp]] = []
        for trial in self.trials.values():
            for fu in trial.follow_ups:
                if fu.recorded_at is None and parse_dt(fu.due_at) <= moment:
                    due.append((trial.trial_id, fu))
        return due

    def open_dispositions(self) -> list[DispositionTask]:
        return [t for t in self.tasks.values() if t.open]
