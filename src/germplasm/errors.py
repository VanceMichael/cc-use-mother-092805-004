"""领域错误。"""

from __future__ import annotations


class GovernanceError(Exception):
    """业务规则被违反。

    报文面向园区运营与审核人员，描述具体阻断原因，
    例如许可不覆盖该用途、数量核对不一致、高风险审批人资质不足。
    """
