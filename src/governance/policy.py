"""取用策略评估：用途、机构、人员资质、生物安全等级、许可有效期与场地容量。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: 生物安全等级达到该值即视为高风险，需要他人复核。
HIGH_BIOSAFETY_THRESHOLD = 3

#: 单次取用超过现存量的该比例即视为大额取用，需要他人复核。
LARGE_QUANTITY_RATIO = 0.5

#: 对不可见材料的统一拒绝语，与“材料不存在”不可区分。
GENERIC_DENIAL = "材料不存在或不在授权范围"


@dataclass
class AccessEvaluation:
    """一次取用评估的结果。

    ``visible=False`` 表示请求方无权感知该材料，此时其余字段
    一律为空，调用方得不到任何可以推断材料存在性的信息。
    """

    visible: bool = True
    approved: bool = False
    max_quantity: float = 0.0
    risk_flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def required_qualifications(biosafety_level: int) -> set[str]:
    return {f"bsl{biosafety_level}"}


def evaluate_access(
    *,
    material: dict[str, Any],
    licenses: list[dict[str, Any]],
    site: dict[str, Any] | None,
    occupancy: int,
    purpose: str,
    quantity: float,
    qualifications: set[str],
    org: str,
    now: float,
) -> AccessEvaluation:
    """评估一次取用申请，给出可执行范围与风险标记。

    许可覆盖不到的机构直接判为不可见；许可状态、用途、资质、
    场地与容量任一不满足都拒绝；全部满足但带风险标记的进入
    待复核（例外）状态，由提交人以外的生物安全审核人员决定。
    """
    evaluation = AccessEvaluation()

    if not licenses or any(org not in json.loads(lic["orgs_json"]) for lic in licenses):
        evaluation.visible = False
        evaluation.reasons = [GENERIC_DENIAL]
        return evaluation

    reasons = evaluation.reasons
    if material["status"] != "active":
        reasons.append("材料已冻结或不可用")
    for lic in licenses:
        if lic["status"] != "active":
            reasons.append("许可已撤销")
            break
        if not lic["valid_from"] <= now <= lic["valid_to"]:
            reasons.append("许可不在有效期")
            break
        if purpose not in json.loads(lic["purposes_json"]):
            reasons.append("用途不在许可范围")
            break

    missing = required_qualifications(int(material["biosafety_level"])) - qualifications
    if missing:
        reasons.append("人员资质不足")

    if site is None:
        reasons.append("场地不存在")
    else:
        if site["status"] != "operational":
            reasons.append("场地不可用")
        if int(site["max_biosafety"]) < int(material["biosafety_level"]):
            reasons.append("场地生物安全等级不足")
        capacity = json.loads(site["capacity_json"])
        if occupancy >= int(capacity.get(material["storage_class"], 0)):
            reasons.append("场地容量不足")

    available = float(material["quantity"])
    if quantity <= 0 or quantity > available:
        reasons.append("申请数量超出现存量")
    evaluation.max_quantity = min(quantity, available)

    if int(material["biosafety_level"]) >= HIGH_BIOSAFETY_THRESHOLD:
        evaluation.risk_flags.append("high_biosafety")
    if available > 0 and quantity > LARGE_QUANTITY_RATIO * available:
        evaluation.risk_flags.append("large_quantity")
    if purpose == "transfer":
        evaluation.risk_flags.append("custody_transfer")

    evaluation.approved = not reasons
    return evaluation
