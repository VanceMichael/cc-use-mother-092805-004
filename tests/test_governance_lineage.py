"""接收登记与谱系操作：拆分、混合、繁育、转移、失活的数量守恒与幂等。"""

from __future__ import annotations

import unittest

from src.governance import (
    FrozenError,
    GovernanceError,
    GovernanceService,
    NotFoundOrRestricted,
    PolicyDenied,
    QuantityError,
    Receipt,
    Store,
)

NOW = 1_700_000_000.0


def make_service() -> GovernanceService:
    return GovernanceService(Store.in_memory(), clock=lambda: NOW)


def base_env(service: GovernanceService):
    site = service.register_site(name="低温库", max_biosafety=2, capacity={"ultra_low": 5})
    license_id = service.register_license(
        issuer="园区管委会",
        purposes=["research", "transfer"],
        orgs=["机构甲", "企业乙"],
        equity_shares={"机构甲": 0.6, "企业乙": 0.4},
        derivative_owner="joint",
        valid_from=NOW - 1000,
        valid_to=NOW + 100000,
    )
    return site, license_id


def intake_strain(service, site, license_id, *, receipt_id="rc-1", quantity=100.0):
    receipt = Receipt(id=receipt_id, type="intake", actor="操作员", org="机构甲", occurred_at=NOW)
    result = service.intake_material(
        receipt=receipt,
        kind="strain",
        name="菌株X",
        quality_batch="B2026-01",
        biosafety_level=1,
        storage_class="ultra_low",
        quantity=quantity,
        unit="ml",
        site_id=site,
        custodian_org="机构甲",
        license_id=license_id,
    )
    return result["material_id"]


class IntakeTest(unittest.TestCase):
    def test_intake_records_source_condition_batch_and_license(self):
        service = make_service()
        site, license_id = base_env(service)
        material_id = intake_strain(service, site, license_id)
        view = service.get_material(material_id, org="机构甲")
        self.assertEqual(view["quality_batch"], "B2026-01")
        self.assertEqual(view["storage_class"], "ultra_low")
        self.assertEqual(view["license_id"], license_id)
        trace = service.trace(material_id=material_id, roles={"park_operator"})
        self.assertEqual(trace["events"][0]["type"], "intake")
        self.assertEqual(trace["events"][0]["basis"]["license_id"], license_id)

    def test_intake_receipt_is_idempotent(self):
        service = make_service()
        site, license_id = base_env(service)
        first = intake_strain(service, site, license_id)
        receipt = Receipt(id="rc-1", type="intake", actor="操作员", org="机构甲", occurred_at=NOW)
        again = service.intake_material(
            receipt=receipt,
            kind="strain",
            name="菌株X",
            quality_batch="B2026-01",
            biosafety_level=1,
            storage_class="ultra_low",
            quantity=100.0,
            unit="ml",
            site_id=site,
            custodian_org="机构甲",
            license_id=license_id,
        )
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["material_id"], first)
        self.assertEqual(len(service.search_materials(org="机构甲")), 1)

    def test_intake_rejects_mismatched_content_hash(self):
        service = make_service()
        site, license_id = base_env(service)
        receipt = Receipt(
            id="rc-bad", type="intake", actor="操作员", org="机构甲",
            occurred_at=NOW, content_hash="0" * 64,
        )
        with self.assertRaises(ValueError):
            service.intake_material(
                receipt=receipt, kind="strain", name="菌株X", quality_batch="B1",
                biosafety_level=1, storage_class="ultra_low", quantity=10, unit="ml",
                site_id=site, custodian_org="机构甲", license_id=license_id,
            )


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.site, self.license_id = base_env(self.service)
        self.material_id = intake_strain(self.service, self.site, self.license_id)

    def test_split_conserves_quantity(self):
        children = self.service.split(
            actor="操作员", org="机构甲", material_id=self.material_id,
            children=[{"quantity": 40.0}, {"quantity": 25.0}],
        )
        self.assertEqual(len(children), 2)
        ledger = self.service.quantity_ledger(self.material_id)
        self.assertTrue(ledger["consistent"])
        self.assertAlmostEqual(ledger["remaining"], 35.0)
        with self.assertRaises(QuantityError):
            self.service.split(
                actor="操作员", org="机构甲", material_id=self.material_id,
                children=[{"quantity": 36.0}],
            )

    def test_mix_records_composition_and_derives_no_direct_license(self):
        other = intake_strain(self.service, self.site, self.license_id, receipt_id="rc-2")
        mixed = self.service.mix(
            actor="操作员", org="机构甲",
            inputs=[(self.material_id, 20.0), (other, 30.0)],
            name="混合菌剂", quality_batch="M1", storage_class="ultra_low",
        )
        view = self.service.get_material(mixed, org="机构甲")
        self.assertIsNone(view["license_id"])
        self.assertAlmostEqual(view["quantity"], 50.0)
        roots = self.service.trace(material_id=mixed, roles={"park_operator"})["roots"]
        self.assertEqual(set(roots), {self.material_id, other})

    def test_propagate_output_may_exceed_input(self):
        child = self.service.propagate(
            actor="操作员", org="机构甲", material_id=self.material_id,
            consumed=5.0, output_quantity=80.0,
        )
        self.assertAlmostEqual(
            self.service.get_material(child, org="机构甲")["quantity"], 80.0
        )
        self.assertTrue(self.service.quantity_ledger(self.material_id)["consistent"])

    def test_deactivate_tracks_remaining_quantity(self):
        self.service.deactivate(
            actor="操作员", org="机构甲", material_id=self.material_id,
            quantity=60.0, reason="批次复检不合格",
        )
        ledger = self.service.quantity_ledger(self.material_id)
        self.assertAlmostEqual(ledger["deactivated"], 60.0)
        self.assertAlmostEqual(ledger["remaining"], 40.0)
        self.service.deactivate(
            actor="操作员", org="机构甲", material_id=self.material_id,
            quantity=40.0, reason="剩余全部失活",
        )
        view = self.service.get_material(self.material_id, org="机构甲")
        self.assertEqual(view["status"], "deactivated")

    def test_non_custodian_cannot_operate(self):
        with self.assertRaises(PolicyDenied):
            self.service.split(
                actor="外人", org="企业乙", material_id=self.material_id,
                children=[{"quantity": 1.0}],
            )


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.site, self.license_id = base_env(self.service)
        self.site_b = self.service.register_site(
            name="企业库", max_biosafety=2, capacity={"ultra_low": 5}
        )
        self.material_id = intake_strain(self.service, self.site, self.license_id)

    def _approved_transfer_request(self, quantity=30.0):
        result = self.service.request_access(
            submitter="研究员乙", org="企业乙", qualifications={"bsl1"},
            purpose="transfer", material_id=self.material_id,
            quantity=quantity, site_id=self.site_b,
        )
        # 转移属于风险标记，需要他人复核
        if result["status"] == "needs_exception":
            result = self.service.approve_exception(
                request_id=result["id"], approver="安全官",
                approver_roles={"biosafety_officer"},
            )
        return result

    def test_transfer_requires_approved_access(self):
        with self.assertRaises(GovernanceError):
            self.service.transfer(
                actor="操作员", org="机构甲", material_id=self.material_id,
                quantity=10.0, to_org="企业乙", to_site_id=self.site_b,
                access_request_id="不存在的授权",
            )

    def test_transfer_moves_custody_and_reconciles_handover(self):
        request = self._approved_transfer_request()
        moved = self.service.transfer(
            actor="操作员", org="机构甲", material_id=self.material_id,
            quantity=30.0, to_org="企业乙", to_site_id=self.site_b,
            access_request_id=request["id"],
        )
        new_lot = self.service.get_material(moved["material_id"], org="企业乙")
        self.assertEqual(new_lot["custodian_org"], "企业乙")
        self.assertEqual(new_lot["site_id"], self.site_b)

        receipt = Receipt(
            id="ho-1", type="handover", actor="收货员", org="企业乙",
            material_id=moved["material_id"], quantity=30.0, unit="ml",
            content_hash=moved["expected_content_hash"], occurred_at=NOW,
            payload={"transfer_event_id": moved["transfer_event_id"]},
        )
        result = self.service.submit_receipt(receipt)
        self.assertEqual(result["status"], "confirmed")
        # 重复提交（离线补传）返回首次结果，不重复告警
        again = self.service.submit_receipt(receipt)
        self.assertTrue(again["duplicate"])
        self.assertEqual(
            len([a for a in self.service.list_alerts() if a["type"] == "receipt_discrepancy"]), 0
        )

    def test_handover_quantity_mismatch_raises_alert(self):
        request = self._approved_transfer_request()
        moved = self.service.transfer(
            actor="操作员", org="机构甲", material_id=self.material_id,
            quantity=30.0, to_org="企业乙", to_site_id=self.site_b,
            access_request_id=request["id"],
        )
        receipt = Receipt(
            id="ho-2", type="handover", actor="收货员", org="企业乙",
            material_id=moved["material_id"], quantity=28.0, unit="ml",
            content_hash=moved["expected_content_hash"], occurred_at=NOW,
            payload={"transfer_event_id": moved["transfer_event_id"]},
        )
        result = self.service.submit_receipt(receipt)
        self.assertEqual(result["status"], "discrepancy")
        self.assertAlmostEqual(result["expected_quantity"], 30.0)
        alerts = [a for a in self.service.list_alerts() if a["type"] == "receipt_discrepancy"]
        self.assertEqual(len(alerts), 1)

    def test_transfer_beyond_authorized_quantity_rejected(self):
        request = self._approved_transfer_request(quantity=30.0)
        with self.assertRaises(QuantityError):
            self.service.transfer(
                actor="操作员", org="机构甲", material_id=self.material_id,
                quantity=31.0, to_org="企业乙", to_site_id=self.site_b,
                access_request_id=request["id"],
            )


if __name__ == "__main__":
    unittest.main()
