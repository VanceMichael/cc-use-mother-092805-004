"""取用策略：用途、机构、资质、生物安全等级、许可有效期、场地容量与例外复核。"""

from __future__ import annotations

import unittest

from src.governance import (
    GovernanceService,
    NotFoundOrRestricted,
    PolicyDenied,
    Receipt,
    SelfApprovalError,
    Store,
)

NOW = 1_700_000_000.0


def make_service(now: float = NOW) -> GovernanceService:
    return GovernanceService(Store.in_memory(), clock=lambda: now)


class MutableClock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def env(service: GovernanceService, *, biosafety_level: int = 1, purposes=("research", "transfer")):
    site = service.register_site(
        name="低温库", max_biosafety=max(2, biosafety_level), capacity={"ultra_low": 2}
    )
    license_id = service.register_license(
        issuer="园区管委会",
        purposes=list(purposes),
        orgs=["机构甲", "企业乙"],
        equity_shares={"机构甲": 1.0},
        derivative_owner="provider",
        valid_from=NOW - 1000,
        valid_to=NOW + 100000,
    )
    result = service.intake_material(
        receipt=Receipt(id="rc-1", type="intake", actor="操作员", org="机构甲", occurred_at=NOW),
        kind="strain", name="菌株X", quality_batch="B1", biosafety_level=biosafety_level,
        storage_class="ultra_low", quantity=100.0, unit="ml",
        site_id=site, custodian_org="机构甲", license_id=license_id,
    )
    return site, license_id, result["material_id"]


class AccessEvaluationTest(unittest.TestCase):
    def test_standard_request_auto_approved_with_scope(self):
        service = make_service()
        site, _, material_id = env(service)
        result = service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material_id, quantity=10.0, site_id=site,
        )
        self.assertEqual(result["status"], "approved")
        self.assertAlmostEqual(result["max_quantity"], 10.0)

    def test_unqualified_personnel_denied(self):
        service = make_service()
        site, _, material_id = env(service, biosafety_level=2)
        result = service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material_id, quantity=10.0, site_id=site,
        )
        self.assertEqual(result["status"], "denied")
        self.assertIn("人员资质不足", result["reasons"])

    def test_expired_license_denied(self):
        clock = MutableClock()
        service = GovernanceService(Store.in_memory(), clock=clock)
        site, _, material_id = env(service)
        clock.now = NOW + 200000  # 许可已过期
        result = service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material_id, quantity=10.0, site_id=site,
        )
        self.assertEqual(result["status"], "denied")
        self.assertIn("许可不在有效期", result["reasons"])

    def test_purpose_outside_license_denied(self):
        service = make_service()
        site, _, material_id = env(service, purposes=("research",))
        result = service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="transfer", material_id=material_id, quantity=10.0, site_id=site,
        )
        self.assertEqual(result["status"], "denied")
        self.assertIn("用途不在许可范围", result["reasons"])

    def test_site_capacity_checked(self):
        service = make_service()
        site, license_id, material_id = env(service)
        # 容量为 2，接收已占 1 份；再接收 1 份后占满
        service.intake_material(
            receipt=Receipt(id="rc-fill", type="intake", actor="操作员", org="机构甲", occurred_at=NOW),
            kind="seed", name="种子", quality_batch="S1", biosafety_level=1,
            storage_class="ultra_low", quantity=1.0, unit="kg",
            site_id=site, custodian_org="机构甲", license_id=license_id,
        )
        full = service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material_id, quantity=1.0, site_id=site,
        )
        self.assertEqual(full["status"], "denied")
        self.assertIn("场地容量不足", full["reasons"])

    def test_high_biosafety_needs_exception_and_no_self_approval(self):
        service = make_service()
        site, _, material_id = env(service, biosafety_level=3)
        result = service.request_access(
            submitter="研究员丙", org="企业乙", qualifications={"bsl3"},
            purpose="research", material_id=material_id, quantity=10.0, site_id=site,
        )
        self.assertEqual(result["status"], "needs_exception")
        self.assertIn("high_biosafety", result["risk_flags"])
        with self.assertRaises(SelfApprovalError):
            service.approve_exception(
                request_id=result["id"], approver="研究员丙",
                approver_roles={"biosafety_officer"},
            )
        with self.assertRaises(PolicyDenied):
            service.approve_exception(
                request_id=result["id"], approver="普通同事", approver_roles={"technician"},
            )
        approved = service.approve_exception(
            request_id=result["id"], approver="安全官", approver_roles={"biosafety_officer"},
        )
        self.assertEqual(approved["status"], "exception_approved")
        self.assertEqual(approved["approver"], "安全官")

    def test_large_quantity_needs_exception(self):
        service = make_service()
        site, _, material_id = env(service)
        result = service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material_id, quantity=60.0, site_id=site,
        )
        self.assertEqual(result["status"], "needs_exception")
        self.assertIn("large_quantity", result["risk_flags"])


class RestrictedVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.site, self.license_id, self.material_id = env(self.service)

    def test_unauthorized_org_cannot_search_or_get(self):
        found = self.service.search_materials(org="外部企业")
        self.assertEqual(found, [])
        with self.assertRaises(NotFoundOrRestricted):
            self.service.get_material(self.material_id, org="外部企业")
        # 授权机构可以检索
        self.assertEqual(len(self.service.search_materials(org="企业乙")), 1)

    def test_denial_does_not_leak_existence(self):
        visible_denial = self.service.request_access(
            submitter="研究员", org="企业乙", qualifications=set(),
            purpose="research", material_id=self.material_id, quantity=1.0, site_id=self.site,
        )
        invisible_denial = self.service.request_access(
            submitter="研究员", org="外部企业", qualifications=set(),
            purpose="research", material_id=self.material_id, quantity=1.0, site_id=self.site,
        )
        ghost_denial = self.service.request_access(
            submitter="研究员", org="外部企业", qualifications=set(),
            purpose="research", material_id="不存在的材料", quantity=1.0, site_id=self.site,
        )
        # 对未授权机构：受限材料与不存在材料的拒绝完全一致
        self.assertEqual(invisible_denial["status"], "denied")
        self.assertEqual(invisible_denial["reasons"], ghost_denial["reasons"])
        self.assertEqual(invisible_denial["risk_flags"], [])
        self.assertEqual(invisible_denial["max_quantity"], 0.0)
        # 而授权机构能看到具体的拒绝原因
        self.assertNotEqual(visible_denial["reasons"], invisible_denial["reasons"])

    def test_mixed_material_visible_only_to_custodian_and_operator(self):
        other = self.service.intake_material(
            receipt=Receipt(id="rc-2", type="intake", actor="操作员", org="机构甲", occurred_at=NOW),
            kind="strain", name="菌株Y", quality_batch="B2", biosafety_level=1,
            storage_class="ultra_low", quantity=50.0, unit="ml",
            site_id=self.site, custodian_org="机构甲", license_id=self.license_id,
        )["material_id"]
        big_site = self.service.register_site(
            name="大容量库", max_biosafety=2, capacity={"ultra_low": 10}
        )
        mixed = self.service.mix(
            actor="操作员", org="机构甲",
            inputs=[(self.material_id, 10.0), (other, 10.0)],
            name="混合菌剂", quality_batch="M1", storage_class="ultra_low",
            site_id=big_site,
        )
        self.assertEqual(self.service.search_materials(org="企业乙", name_contains="混合"), [])
        self.assertEqual(
            len(self.service.search_materials(org="机构甲", name_contains="混合")), 1
        )
        self.assertEqual(
            len(self.service.search_materials(org="园区", roles={"park_operator"}, name_contains="混合")),
            1,
        )


if __name__ == "__main__":
    unittest.main()
