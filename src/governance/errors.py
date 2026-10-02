"""治理后端的领域异常。"""

from __future__ import annotations


class GovernanceError(Exception):
    """治理后端所有领域异常的基类。"""


class NotFoundOrRestricted(GovernanceError):
    """材料不存在，或请求方无权感知其存在。

    两种情况使用同一个异常、同一句提示，调用方无法据此区分
    “确实不存在”与“存在但受限”，避免推断受限资源的存在。
    """


class FrozenError(GovernanceError):
    """材料或试验已被冻结，不能执行新的操作。"""


class QuantityError(GovernanceError):
    """数量不足、超分或台账不一致。"""


class PolicyDenied(GovernanceError):
    """取用策略拒绝。"""


class SelfApprovalError(PolicyDenied):
    """高风险例外不得由方案提交人自行批准。"""


class LicenseError(GovernanceError):
    """许可状态不允许该操作。"""
