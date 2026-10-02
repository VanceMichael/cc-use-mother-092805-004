"""领域模型：材料、许可、谱系、取用、事故与回执的常量与数据结构。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class MaterialKind(enum.Enum):
    STRAIN = "strain"  # 菌株
    SEED = "seed"  # 种子
    GENE_CONSTRUCT = "gene_construct"  # 基因构件


class MaterialStatus(enum.Enum):
    ACTIVE = "active"
    FROZEN = "frozen"
    DEACTIVATED = "deactivated"


class LineageOp(enum.Enum):
    INTAKE = "intake"
    SPLIT = "split"
    MIX = "mix"
    PROPAGATE = "propagate"
    TRANSFER = "transfer"
    DEACTIVATE = "deactivate"


class LicenseStatus(enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class RequestStatus(enum.Enum):
    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_EXCEPTION = "needs_exception"
    EXCEPTION_APPROVED = "exception_approved"
    EXCEPTION_DENIED = "exception_denied"


class IncidentType(enum.Enum):
    CONTAMINATION = "contamination"
    LOSS = "loss"
    LICENSE_REVOKED = "license_revoked"
    SITE_FAILURE = "site_failure"


class JobStatus(enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


#: 保存条件类别及其允许的温度区间（摄氏度）。
STORAGE_RANGES: dict[str, tuple[float, float]] = {
    "liquid_nitrogen": (-196.0, -150.0),
    "ultra_low": (-90.0, -60.0),
    "refrigerated": (2.0, 8.0),
    "room": (15.0, 28.0),
}

#: 可以看到全部材料、可以解冻的角色。
OPERATOR_ROLES = frozenset({"park_operator", "biosafety_officer"})

#: 高风险例外的复核人必须具备的角色。
BIOSAFETY_OFFICER = "biosafety_officer"


@dataclass(frozen=True)
class Receipt:
    """扫描或交接回执。

    ``id`` 是幂等键：重复提交或离线补传时返回首次处理结果，
    不重复入账。``occurred_at`` 是回执实际发生时间（可早于
    系统记录时间），``content_hash`` 用于核对材料身份与内容。
    """

    id: str
    type: str  # intake | handover | scan
    actor: str
    org: str
    occurred_at: float
    material_id: str | None = None
    quantity: float | None = None
    unit: str | None = None
    content_hash: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
