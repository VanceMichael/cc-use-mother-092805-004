"""事故局部冻结、处置责任、持久任务与成果追溯。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.governance import (
    FrozenError,
    GovernanceService,
    NotFoundOrRestricted,
    PolicyDenied,
    Receipt,
    Store,
)

T0 = 1_700_000_000.0


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def build_env(service: GovernanceService):
    site = service.register_site(name="低温库", max_biosafety=2, capacity={"ultra_low": 10})
    license_id = service.register_license(
        issuer="园区管委会",
        purposes=["research", "transfer"],
        orgs=["机构甲", "企业乙"],
        equity_shares={"机构甲": 0.6, "企业乙": 0.4},
        derivative_owner="joint",
        valid_from=T0 - 1000,
        valid_to=T0 + 100000,
    )

    def intake(receipt_id, name, quantity=100.0, org="机构甲", site_id=None):
        return service.intake_material(
            receipt=Receipt(id=receipt_id, type="intake", actor="操作员", org=org, occurred_at=T0),
            kind="strain", name=name, quality_batch=f"QB-{name}", biosafety_level=1,
            storage_class="ultra_low", quantity=quantity, unit="ml",
            site_id=site_id or site, custodian_org=org, license_id=license_id,
        )["material_id"]

    return site, license_id, intake


class IncidentTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = GovernanceService(Store.in_memory(), clock=self.clock)
        self.site, self.license_id, self.intake = build_env(self.service)

    def test_contamination_freezes_only_affected_lineage(self):
        contaminated = self.intake("rc-1", "菌株A")
        clean = self.intake("rc-2", "菌株B")
        child = self.service.split(
            actor="操作员", org="机构甲", material_id=contaminated,
            children=[{"quantity": 20.0}],
        )[0]
        incident = self.service.report_incident(
            type="contamination", reporter="安全员", material_ids=[contaminated],
        )
        # 污染株及其子份被冻结，无关材料不受影响
        self.assertEqual(
            self.service.get_material(contaminated, org="机构甲")["status"], "frozen"
        )
        self.assertEqual(self.service.get_material(child, org="机构甲")["status"], "frozen")
        self.assertEqual(self.service.get_material(clean, org="机构甲")["status"], "active")
        with self.assertRaises(FrozenError):
            self.service.split(
                actor="操作员", org="机构甲", material_id=contaminated,
                children=[{"quantity": 1.0}],
            )
        # 处置责任落到保管机构
        tasks = self.service.pending_disposals(org="机构甲")
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t["incident_id"] == incident for t in tasks))
        report = self.service.incident_report(incident)
        self.assertEqual(len(report["freezes"]), 2)

    def test_license_revocation_keeps_completed_steps_basis(self):
        material = self.intake("rc-1", "菌株A")
        # 撤销前完成一次拆分，该环节依据当时有效许可
        child = self.service.split(
            actor="操作员", org="机构甲", material_id=material,
            children=[{"quantity": 30.0}],
        )[0]
        incident = self.service.report_incident(
            type="license_revoked", reporter="园区", license_id=self.license_id,
        )
        self.assertEqual(self.service.get_material(material, org="机构甲")["status"], "frozen")
        self.assertEqual(self.service.get_material(child, org="机构甲")["status"], "frozen")
        # 已完成的拆分事件保留当时依据，不被改写
        trace = self.service.trace(material_id=child, roles={"park_operator"})
        split_event = next(e for e in trace["events"] if e["type"] == "split")
        self.assertEqual(split_event["basis"]["operation"], "custodian_split")
        intake_event = next(e for e in trace["events"] if e["type"] == "intake")
        self.assertEqual(intake_event["basis"]["license_status"], "active")
        # 撤销后新的取用被拒绝
        denied = self.service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material, quantity=1.0, site_id=self.site,
        )
        self.assertEqual(denied["status"], "denied")
        self.assertIn("许可已撤销", denied["reasons"])

    def test_site_failure_freezes_only_that_site(self):
        other_site = self.service.register_site(
            name="备用库", max_biosafety=2, capacity={"ultra_low": 5}
        )
        here = self.intake("rc-1", "菌株A")
        there = self.intake("rc-2", "菌株B", site_id=other_site)
        self.service.report_incident(
            type="site_failure", reporter="场地管理员", site_id=self.site,
        )
        self.assertEqual(self.service.get_material(here, org="机构甲")["status"], "frozen")
        self.assertEqual(self.service.get_material(there, org="机构甲")["status"], "active")

    def test_loss_writes_off_quantity_and_freezes_rest(self):
        material = self.intake("rc-1", "菌株A", quantity=50.0)
        self.service.report_incident(
            type="loss", reporter="保管员", material_ids=[material],
            detail={"lost": {material: 20.0}},
        )
        ledger = self.service.quantity_ledger(material)
        self.assertAlmostEqual(ledger["deactivated"], 20.0)
        self.assertAlmostEqual(ledger["remaining"], 30.0)
        self.assertTrue(ledger["consistent"])
        self.assertEqual(self.service.get_material(material, org="机构甲")["status"], "frozen")

    def test_freeze_release_requires_operator_role(self):
        material = self.intake("rc-1", "菌株A")
        self.service.report_incident(
            type="contamination", reporter="安全员", material_ids=[material],
        )
        freeze = self.service.open_freezes()[0]
        with self.assertRaises(PolicyDenied):
            self.service.release_freeze(
                freeze_id=freeze["id"], actor="研究员", roles={"researcher"},
                rationale="自查无问题",
            )
        self.service.release_freeze(
            freeze_id=freeze["id"], actor="安全官", roles={"biosafety_officer"},
            rationale="复检合格，解除冻结",
        )
        self.assertEqual(self.service.get_material(material, org="机构甲")["status"], "active")

    def test_incident_suspends_related_trials(self):
        material = self.intake("rc-1", "菌株A")
        request = self.service.request_access(
            submitter="研究员", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=material, quantity=5.0, site_id=self.site,
        )
        trial = self.service.register_trial(
            org="企业乙", plan_ref="方案-1", site_id=self.site,
            access_request_id=request["id"],
        )
        self.service.report_incident(
            type="contamination", reporter="安全员", material_ids=[material],
        )
        trial_row = self.service.store.one("SELECT * FROM trials WHERE id=?", (trial,))
        self.assertEqual(trial_row["status"], "suspended")


class DurableJobTest(unittest.TestCase):
    def test_condition_alert_and_recheck_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gov.db"
            clock = Clock()
            service = GovernanceService(Store(path), clock=clock, storage_recheck_delay=600.0)
            site, _, intake = build_env(service)
            intake("rc-1", "菌株A")
            # 超低温要求 -90~-60，读数 -20 越限
            result = service.record_condition(site_id=site, temperature=-20.0)
            self.assertEqual(len(result["violations"]), 1)
            self.assertEqual(
                len([a for a in service.list_alerts() if a["type"] == "storage_condition"]), 1
            )
            # 进程重启：新实例读取同一库，复核任务仍在
            restarted = GovernanceService(Store(path), clock=clock, storage_recheck_delay=600.0)
            clock.now += 600.0
            # 温度仍未恢复
            restarted.record_condition(site_id=site, temperature=-10.0)
            outcomes = restarted.run_due_jobs()
            rechecks = [o for o in outcomes if o["type"] == "storage_recheck"]
            self.assertTrue(rechecks)
            alerts = [a for a in restarted.list_alerts() if a["type"] == "storage_condition"]
            self.assertGreaterEqual(len(alerts), 2)

    def test_review_reminder_and_trial_followup_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gov.db"
            clock = Clock()
            service = GovernanceService(Store(path), clock=clock, review_sla=3600.0)
            site, _, intake = build_env(service)
            # 高生物安全等级材料 → 取用进入待复核
            high_site = service.register_site(
                name="高等级库", max_biosafety=3, capacity={"ultra_low": 5}
            )
            high = service.intake_material(
                receipt=Receipt(id="rc-h", type="intake", actor="操作员", org="机构甲", occurred_at=T0),
                kind="strain", name="高危菌株", quality_batch="QB-H", biosafety_level=3,
                storage_class="ultra_low", quantity=10.0, unit="ml",
                site_id=high_site, custodian_org="机构甲", license_id=_license(service),
            )["material_id"]
            request = service.request_access(
                submitter="研究员", org="企业乙", qualifications={"bsl3"},
                purpose="research", material_id=high, quantity=1.0, site_id=high_site,
            )
            self.assertEqual(request["status"], "needs_exception")
            normal = intake("rc-1", "菌株A")
            approved = service.request_access(
                submitter="研究员", org="企业乙", qualifications={"bsl1"},
                purpose="research", material_id=normal, quantity=5.0, site_id=site,
            )
            trial = service.register_trial(
                org="企业乙", plan_ref="方案-1", site_id=site,
                access_request_id=approved["id"], follow_up_interval=1800.0,
            )
            # 重启后到期任务仍被执行
            clock.now += 3700.0
            restarted = GovernanceService(Store(path), clock=clock, review_sla=3600.0)
            outcomes = restarted.run_due_jobs()
            types = {o["type"] for o in outcomes}
            self.assertIn("review_reminder", types)
            self.assertIn("trial_followup", types)
            alert_types = {a["type"] for a in restarted.list_alerts()}
            self.assertIn("pending_review", alert_types)
            self.assertIn("trial_followup", alert_types)
            # 随访任务按周期重排，再次到期仍触发
            clock.now += 1800.0
            again = restarted.run_due_jobs()
            self.assertIn("trial_followup", {o["type"] for o in again})


def _license(service: GovernanceService) -> str:
    row = service.store.one("SELECT id FROM licenses")
    return row["id"]


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = GovernanceService(Store.in_memory(), clock=self.clock)
        self.site, self.license_id, self.intake = build_env(self.service)

    def test_outcome_traces_to_roots_transfers_and_equity(self):
        material_a = self.intake("rc-1", "菌株A", quantity=100.0)
        material_b = self.intake("rc-2", "菌株B", quantity=100.0)
        # 转移 A 的一部分给企业乙
        site_b = self.service.register_site(name="企业库", max_biosafety=2, capacity={"ultra_low": 5})
        request = self.service.request_access(
            submitter="研究员乙", org="企业乙", qualifications={"bsl1"},
            purpose="transfer", material_id=material_a, quantity=40.0, site_id=site_b,
        )
        request = self.service.approve_exception(
            request_id=request["id"], approver="安全官", approver_roles={"biosafety_officer"},
        )
        moved = self.service.transfer(
            actor="操作员", org="机构甲", material_id=material_a,
            quantity=40.0, to_org="企业乙", to_site_id=site_b,
            access_request_id=request["id"],
        )
        # 企业乙混合 A 的子份与 B 的子份
        split_b = self.service.split(
            actor="操作员", org="机构甲", material_id=material_b,
            children=[{"quantity": 20.0}],
        )[0]
        req_b = self.service.request_access(
            submitter="研究员乙", org="企业乙", qualifications={"bsl1"},
            purpose="transfer", material_id=split_b, quantity=20.0, site_id=site_b,
        )
        req_b = self.service.approve_exception(
            request_id=req_b["id"], approver="安全官", approver_roles={"biosafety_officer"},
        )
        moved_b = self.service.transfer(
            actor="操作员", org="机构甲", material_id=split_b,
            quantity=20.0, to_org="企业乙", to_site_id=site_b,
            access_request_id=req_b["id"],
        )
        mixed = self.service.mix(
            actor="研究员乙", org="企业乙",
            inputs=[(moved["material_id"], 10.0), (moved_b["material_id"], 10.0)],
            name="复合菌剂", quality_batch="M1", storage_class="ultra_low", site_id=site_b,
        )
        # 基于混合材料登记试验与成果
        req_use = self.service.request_access(
            submitter="研究员乙", org="企业乙", qualifications={"bsl1"},
            purpose="research", material_id=mixed, quantity=5.0, site_id=site_b,
        )
        self.assertEqual(req_use["status"], "approved")
        trial = self.service.register_trial(
            org="企业乙", plan_ref="方案-9", site_id=site_b, access_request_id=req_use["id"],
        )
        outcome = self.service.register_outcome(trial_id=trial, description="复合菌剂田间数据")

        report = self.service.trace_outcome(outcome, roles={"park_operator"})
        self.assertEqual(
            set(report["lineage"]["roots"]), {material_a, material_b}
        )
        self.assertEqual(len(report["lineage"]["transfers"]), 2)
        equity = report["equity"]
        self.assertEqual(len(equity), 2)
        for entry in equity:
            self.assertEqual(entry["equity_shares"], {"机构甲": 0.6, "企业乙": 0.4})
            self.assertEqual(entry["derivative_owner"], "joint")
        self.assertAlmostEqual(sum(e["weight"] for e in equity), 1.0)

    def test_trace_requires_operator_role(self):
        material = self.intake("rc-1", "菌株A")
        with self.assertRaises(NotFoundOrRestricted):
            self.service.trace(material_id=material, roles={"researcher"})


if __name__ == "__main__":
    unittest.main()
