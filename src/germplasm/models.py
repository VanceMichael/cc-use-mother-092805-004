"""领域枚举。"""

from __future__ import annotations

import enum


class MaterialType(str, enum.Enum):
    """材料种类。"""

    STRAIN = "strain"          # 菌株
    SEED = "seed"              # 种子
    CONSTRUCT = "construct"    # 基因构件
    DERIVATIVE = "derivative"  # 混合/繁育等衍生材料


class BiosafetyLevel(enum.IntEnum):
    """生物安全等级，取值越大要求越高。"""

    BSL1 = 1
    BSL2 = 2
    BSL3 = 3
    BSL4 = 4


class Unit(str, enum.Enum):
    """计量单位。同一次谱系运算的数量只能在同单位间核对。"""

    VIAL = "vial"      # 支（冻存管）
    GRAM = "gram"      # 克
    MILLILITER = "ml"  # 毫升
    SEED = "seed"      # 粒
    COPY = "copy"      # 份（构件拷贝）


class StorageStatus(str, enum.Enum):
    """材料保存状态。"""

    ACTIVE = "active"        # 正常
    FROZEN = "frozen"        # 被事件冻结，暂停一切取用与处置
    INACTIVATED = "inactivated"  # 已失活
    LOST = "lost"            # 已确认丢失


class IncidentKind(str, enum.Enum):
    """触发冻结的事件类型。"""

    CONTAMINATION = "contamination"  # 污染
    LOSS = "loss"                    # 丢失
    LICENSE_REVOKED = "license_revoked"  # 许可撤销
    STORAGE_FAULT = "storage_fault"  # 场地/保存条件故障


class EvidenceKind(str, enum.Enum):
    """交接证据种类。"""

    SCAN = "scan"            # 扫码记录
    HANDOFF_RECEIPT = "handoff_receipt"  # 交接回执（照片/签收）
    MANIFEST = "manifest"    # 装箱单/批次单


class EvidenceSide(str, enum.Enum):
    """证据由哪一方提交。双方可能各持一份交接照片。"""

    SENDER = "sender"
    RECEIVER = "receiver"
    THIRD_PARTY = "third_party"


class TrialStatus(str, enum.Enum):
    """试验随访状态。"""

    PLANNED = "planned"
    ONGOING = "ongoing"
    FROZEN = "frozen"
    COMPLETED = "completed"
    TERMINATED = "terminated"


class OutputKind(str, enum.Enum):
    """成果类型。"""

    DATASET = "dataset"
    PUBLICATION = "publication"
    PATENT = "patent"
    PRODUCT = "product"
    METHOD = "method"
