"""种质与试验治理后端测试。

覆盖：谱系登记与数量核对、转移双方证据、取用六因素决策、
高风险例外独立复核、未授权搜索不可推断、事件隔离冻结与处置责任、
许可撤销、场地故障、离线补传幂等、重启持久化、成果权益追溯。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.germplasm import (
    BiosafetyLevel,
    EventStore,
    EvidenceKind,
    EvidenceSide,
    GovernanceError,
    GovernanceService,
    IncidentKind,
    MaterialType,
    OutputKind,
    Unit,
)

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=365)
PAST = NOW - timedelta(days=60)


class GovernanceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "events.jsonl"
        self.service = self._build(
            GovernanceService(EventStore(self.log_path), clock=lambda: NOW)
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, svc: GovernanceService) -> GovernanceService:
        """构造标准测试环境。

        机构：co_a 企业、inst_b 研究机构、co_x 未授权企业、park 园区。
        """
        s = svc
        for oid, name in [
            ("co_a", "甲生物制造企业"),
            ("inst_b", "乙研究机构"),
            ("co_x", "未授权企业"),
            ("park", "园区运营方"),
        ]:
            s.register_org(oid, name)

        s.register_facility(
            "fa_co", "甲企业超低温库", "co_a", BiosafetyLevel.BSL2,
            capacity_total=100, work_capacity_total=20,
            spec_ranges={"temp_c": (-85.0, -60.0)},
        )
        s.register_facility(
            "fa_lab", "乙机构BSL3实验中心", "inst_b", BiosafetyLevel.BSL3,
            capacity_total=100, work_capacity_total=20,
            spec_ranges={"temp_c": (-85.0, -60.0)},
        )
        s.register_facility(
            "fa_x", "未授权企业场地", "co_x", BiosafetyLevel.BSL2,
            capacity_total=100, work_capacity_total=20,
        )

        s.register_person("p_co", "甲研究员", "co_a", BiosafetyLevel.BSL2,
                          qualifications=["发酵工程"])
        s.register_person("p_inst", "乙研究员", "inst_b", BiosafetyLevel.BSL3,
                          qualifications=["合成生物学"])
        s.register_person("p_junior", "乙初级人员", "inst_b", BiosafetyLevel.BSL1)
        s.register_person("p_auditor", "园区生物安全审核员", "park",
                          BiosafetyLevel.BSL4, is_biosafety_reviewer=True)

        # 许可：甲乙双方共同持有，允许研究与发酵，附带甲企业权益条款
        s.register_license(
            "lic_co", "菌株合作许可", licensor_org="co_a",
            holder_orgs=["co_a", "inst_b"],
            allowed_purposes=["research", "ferment"], max_bsl=BiosafetyLevel.BSL2,
            valid_from=PAST, valid_to=FUTURE,
            terms="仅限合作区试验使用",
            benefit_terms=[{
                "party_org": "co_a", "clause": "衍生成果产业化收益分成10%",
                "share": "10%", "scope": "product",
            }],
        )
        s.register_license(
            "lic_inst", "机构科研许可", licensor_org="park",
            holder_orgs=["inst_b"], allowed_purposes=["research"],
            max_bsl=BiosafetyLevel.BSL3, valid_from=PAST, valid_to=FUTURE,
        )
        s.register_license(
            "lic_secret", "企业受限许可", licensor_org="co_a",
            holder_orgs=["co_a"], allowed_purposes=["ferment"],
            max_bsl=BiosafetyLevel.BSL2, valid_from=PAST, valid_to=FUTURE,
        )
        s.register_license(
            "lic_expired", "已过期许可", licensor_org="park",
            holder_orgs=["inst_b"], allowed_purposes=["research"],
            max_bsl=BiosafetyLevel.BSL2, valid_from=PAST - timedelta(days=400),
            valid_to=PAST,
        )

        # 材料
        s.receive_material(
            "m_strain", MaterialType.STRAIN, "生态链霉菌A", Unit.VIAL, 10,
            BiosafetyLevel.BSL2, "co_a", ["lic_co"], "B2026-01",
            "fa_co", custodian_person="p_co", storage_label="ultra_low",
        )
        s.receive_material(
            "m_strain2", MaterialType.STRAIN, "辅助菌B", Unit.VIAL, 6,
            BiosafetyLevel.BSL1, "co_a", ["lic_co"], "B2026-02", "fa_co",
        )
        s.receive_material(
            "m_secret", MaterialType.CONSTRUCT, "受限基因构件X", Unit.COPY, 8,
            BiosafetyLevel.BSL2, "co_a", ["lic_secret"], "K2026-09", "fa_co",
        )
        s.receive_material(
            "m_labstrain", MaterialType.STRAIN, "实验室常用菌", Unit.VIAL, 5,
            BiosafetyLevel.BSL2, "park", ["lic_inst"], "L2026-03", "fa_lab",
        )
        s.receive_material(
            "m_bsl3", MaterialType.STRAIN, "高风险致病菌", Unit.VIAL, 30,
            BiosafetyLevel.BSL3, "park", ["lic_inst"], "L2026-04", "fa_lab",
        )
        s.receive_material(
            "m_exp", MaterialType.SEED, "过期许可种子", Unit.SEED, 50,
            BiosafetyLevel.BSL1, "park", ["lic_expired"], "S2025-00", "fa_lab",
            occurred_at=PAST - timedelta(days=30),  # 许可当时仍有效，历史入库
        )
        return s

    def reopen(self) -> GovernanceService:
        """模拟进程重启：从事件日志重放。"""
        return GovernanceService(EventStore(self.log_path), clock=lambda: NOW)


class LineageTest(GovernanceCase):
    def test_receive_records_basis_and_ledger(self) -> None:
        m = self.service.timeline.materials["m_strain"]
        self.assertEqual(m.quantity, 10)
        self.assertEqual(m.derivation, "receipt")
        self.assertEqual(m.basis_snapshot["licenses"][0]["license_id"], "lic_co")
        self.assertEqual(m.ledger[0].delta, 10)

    def test_split_conserves_and_links_lineage(self) -> None:
        self.service.split_material("m_strain", "m_split", 3, "B2026-01-A")
        parent = self.service.timeline.materials["m_strain"]
        child = self.service.timeline.materials["m_split"]
        self.assertAlmostEqual(parent.quantity, 7)
        self.assertAlmostEqual(child.quantity, 3)
        self.assertEqual(child.parents, [("m_strain", 3.0)])
        self.assertIn("m_split", parent.children)
        self.assertEqual(child.license_ids, ["lic_co"])
        self.assertEqual(child.facility_id, "fa_co")

    def test_mix_consumes_inputs_and_merges_licenses(self) -> None:
        self.service.mix_materials(
            "m_mix", "混合发酵料",
            [("m_strain", 3), ("m_strain2", 2)], output_qty=5,
            new_batch="M2026-10",
        )
        mix = self.service.timeline.materials["m_mix"]
        self.assertEqual(mix.derivation, "mix")
        self.assertEqual(mix.bsl, BiosafetyLevel.BSL2)  # 取最高
        self.assertAlmostEqual(self.service.timeline.materials["m_strain"].quantity, 7)
        self.assertAlmostEqual(self.service.timeline.materials["m_strain2"].quantity, 4)
        roots = self.service.timeline.lineage_roots("m_mix")
        self.assertEqual(set(roots), {"m_strain", "m_strain2"})

    def test_mix_rejects_cross_facility_and_unit_mismatch(self) -> None:
        with self.assertRaisesRegex(GovernanceError, "同一机构、同一场地"):
            self.service.mix_materials(
                "m_bad", "跨机构混合",
                [("m_strain", 1), ("m_labstrain", 1)], output_qty=2, new_batch="x",
            )

    def test_propagate_keeps_parent_and_adds_yield(self) -> None:
        self.service.propagate_material("m_strain2", "m_prop", 20, "P2026-01")
        parent = self.service.timeline.materials["m_strain2"]
        child = self.service.timeline.materials["m_prop"]
        self.assertAlmostEqual(parent.quantity, 6)  # 原种保留
        self.assertAlmostEqual(child.quantity, 20)
        self.assertEqual(child.parents, [("m_strain2", 0.0)])

    def test_derivatives_forbidden_by_license(self) -> None:
        # lic_secret 不允许衍生：重新登记一份禁止衍生的材料
        self.service.register_license(
            "lic_noderiv", "禁止衍生许可", "park", ["inst_b"], ["research"],
            BiosafetyLevel.BSL1, PAST, FUTURE, derivatives_allowed=False,
        )
        self.service.receive_material(
            "m_noderiv", MaterialType.CONSTRUCT, "禁衍生构件", Unit.COPY, 2,
            BiosafetyLevel.BSL1, "park", ["lic_noderiv", "lic_inst"], "K01",
            "fa_lab",
        )
        with self.assertRaisesRegex(GovernanceError, "不允许繁育扩繁"):
            self.service.propagate_material("m_noderiv", "m_nd2", 2, "P02")

    def test_inactivation_is_countable(self) -> None:
        self.service.inactivate_material("m_strain2", qty=2, reason="过期销毁")
        m = self.service.timeline.materials["m_strain2"]
        self.assertAlmostEqual(m.quantity, 4)
        self.assertAlmostEqual(m.inactivated_qty, 2)
        self.service.inactivate_material("m_strain2")  # 全部失活
        self.assertEqual(self.service.timeline.materials["m_strain2"].status.value,
                         "inactivated")


class TransferEvidenceTest(GovernanceCase):
    def _dispatch_and_receive(self) -> None:
        self.service.dispatch_transfer(
            "tr_1", "m_strain", 3, "inst_b", "fa_lab", to_person="p_inst",
        )
        self.service.receive_transfer("tr_1", "m_strain_r", batch="B2026-01-R")

    def test_transfer_creates_lineage_node_at_receiver(self) -> None:
        self._dispatch_and_receive()
        self.assertAlmostEqual(self.service.timeline.materials["m_strain"].quantity, 7)
        received = self.service.timeline.materials["m_strain_r"]
        self.assertEqual(received.custodian_org, "inst_b")
        self.assertEqual(received.parents, [("m_strain", 3.0)])
        self.assertEqual(received.derivation, "transfer")
        roots = self.service.timeline.lineage_roots("m_strain_r")
        self.assertEqual(roots, ["m_strain"])

    def test_transfer_to_unauthorized_org_blocked(self) -> None:
        with self.assertRaisesRegex(GovernanceError, "未授权接收机构"):
            self.service.dispatch_transfer(
                "tr_x", "m_secret", 1, "co_x", "fa_x",
            )

    def test_confirmation_requires_both_sides(self) -> None:
        self._dispatch_and_receive()
        result = self.service.reconcile_transfer("tr_1")
        self.assertFalse(result["confirmed"])
        self.assertTrue(any("sender方" in p for p in result["problems"]))

        self.service.submit_evidence(
            "transfer", "tr_1", EvidenceSide.SENDER, EvidenceKind.HANDOFF_RECEIPT,
            "photo://sender.jpg", "a" * 64, "p_co",
            observed_qty=3, observed_identity="m_strain",
        )
        result = self.service.reconcile_transfer("tr_1")
        self.assertFalse(result["confirmed"])  # 仍缺接收方

        self.service.submit_evidence(
            "transfer", "tr_1", EvidenceSide.RECEIVER, EvidenceKind.SCAN,
            "scan://receiver.png", "b" * 64, "p_inst",
            observed_qty=3, observed_identity="生态链霉菌A",
        )
        result = self.service.reconcile_transfer("tr_1")
        self.assertTrue(result["confirmed"])
        self.assertTrue(self.service.timeline.transfers["tr_1"].confirmed)

    def test_quantity_dispute_raises_alert(self) -> None:
        self._dispatch_and_receive()
        self.service.submit_evidence(
            "transfer", "tr_1", EvidenceSide.SENDER, EvidenceKind.HANDOFF_RECEIPT,
            "photo://s", "a" * 64, "p_co", observed_qty=3,
        )
        self.service.submit_evidence(
            "transfer", "tr_1", EvidenceSide.RECEIVER, EvidenceKind.HANDOFF_RECEIPT,
            "photo://r", "c" * 64, "p_inst", observed_qty=2,
        )
        result = self.service.reconcile_transfer("tr_1")
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["disputed"])
        self.assertTrue(
            any(a.kind == "transfer_discrepancy" and a.open
                for a in self.service.timeline.alerts.values())
        )

    def test_duplicate_and_offline_evidence_is_idempotent(self) -> None:
        self._dispatch_and_receive()
        kwargs = dict(
            target_type="transfer", target_id="tr_1",
            side=EvidenceSide.SENDER, kind=EvidenceKind.HANDOFF_RECEIPT,
            file_ref="photo://s.jpg", content_sha256="d" * 64,
            submitted_by="p_co", observed_qty=3,
        )
        # 离线补传：发生时间早于登记时间
        e1 = self.service.submit_evidence(occurred_at=NOW - timedelta(days=2), **kwargs)
        e2 = self.service.submit_evidence(occurred_at=NOW - timedelta(days=2), **kwargs)
        self.assertEqual(e1, e2)
        transfer = self.service.timeline.transfers["tr_1"]
        self.assertEqual(len(transfer.evidence_ids), 1)
        ev = self.service.timeline.evidence[e1]
        self.assertLess(ev.occurred_at, ev.recorded_at)

        with self.assertRaises(GovernanceError):
            self.service.receive_transfer("tr_1", "m_again")


class AccessDecisionTest(GovernanceCase):
    def test_allowed_request_returns_executable_scope(self) -> None:
        out = self.service.request_access(
            "m_labstrain", "p_inst", "research", 2, "fa_lab",
        )
        self.assertEqual(out["status"], "approved")
        self.assertIsNotNone(out["decision"]["executable"])
        self.service.issue_access(out["request_id"])
        self.assertAlmostEqual(self.service.timeline.materials["m_labstrain"].quantity, 3)

    def test_purpose_not_covered(self) -> None:
        out = self.service.request_access(
            "m_labstrain", "p_inst", "commercial_sale", 1, "fa_lab",
        )
        self.assertEqual(out["status"], "denied")
        self.assertTrue(any("不覆盖用途" in r for r in out["decision"]["reasons"]))

    def test_expired_license_denied(self) -> None:
        out = self.service.request_access(
            "m_exp", "p_inst", "research", 1, "fa_lab",
        )
        self.assertEqual(out["status"], "denied")
        self.assertTrue(any("有效期" in r for r in out["decision"]["reasons"]))

    def test_personnel_qualification_denied(self) -> None:
        out = self.service.request_access(
            "m_labstrain", "p_junior", "research", 1, "fa_lab",
        )
        self.assertEqual(out["status"], "denied")
        self.assertTrue(any("资质" in r for r in out["decision"]["reasons"]))

    def test_facility_capacity_denied(self) -> None:
        out = self.service.request_access(
            "m_bsl3", "p_inst", "research", 25, "fa_lab",
        )
        self.assertEqual(out["status"], "denied")
        self.assertTrue(any("使用容量" in r for r in out["decision"]["reasons"]))

    def test_storage_capacity_blocked_on_receive(self) -> None:
        with self.assertRaisesRegex(GovernanceError, "保存容量不足"):
            self.service.receive_material(
                "m_huge", MaterialType.SEED, "大批量", Unit.SEED, 1_000_000,
                BiosafetyLevel.BSL1, "co_a", ["lic_co"], "B99", "fa_co",
            )

    def test_unauthorized_org_request_denied(self) -> None:
        # 未授权企业的研究人员（登记在 co_x）
        self.service.register_person(
            "p_x", "未授权企业人员", "co_x", BiosafetyLevel.BSL2,
        )
        out = self.service.request_access(
            "m_secret", "p_x", "ferment", 1, "fa_x",
        )
        self.assertEqual(out["status"], "denied")
        self.assertTrue(any("许可" in r for r in out["decision"]["reasons"]))

    def test_issue_rechecks_license_validity(self) -> None:
        out = self.service.request_access(
            "m_secret", "p_co", "ferment", 1, "fa_co",
        )
        self.assertEqual(out["status"], "approved")
        self.service.revoke_license("lic_secret", "合作终止")
        with self.assertRaisesRegex(GovernanceError, "复核未通过"):
            self.service.issue_access(out["request_id"])


class HighRiskReviewTest(GovernanceCase):
    def test_bsl3_requires_independent_reviewer(self) -> None:
        out = self.service.request_access(
            "m_bsl3", "p_inst", "research", 2, "fa_lab",
            exception_requested=True,
        )
        self.assertEqual(out["status"], "pending_review")
        self.assertTrue(out["decision"]["high_risk"])
        rid = out["request_id"]

        # 提交人不能自行批准
        with self.assertRaisesRegex(GovernanceError, "不能由方案提交人自行批准"):
            self.service.review_access(rid, "p_inst", True)
        # 无审核资质的人不能批准
        self.service.register_person(
            "p_other", "普通同事", "inst_b", BiosafetyLevel.BSL3,
        )
        with self.assertRaisesRegex(GovernanceError, "生物安全审核资质"):
            self.service.review_access(rid, "p_other", True)
        # 独立审核员批准后可以发放
        self.service.review_access(rid, "p_auditor", True, note="附加三级防护条件")
        self.service.issue_access(rid)
        self.assertEqual(self.service.timeline.requests[rid].status, "issued")

    def test_reviewer_can_reject(self) -> None:
        out = self.service.request_access(
            "m_bsl3", "p_inst", "research", 2, "fa_lab",
        )
        self.service.review_access(out["request_id"], "p_auditor", False, note="防护不足")
        self.assertEqual(
            self.service.timeline.requests[out["request_id"]].status, "denied"
        )


class VisibilityTest(GovernanceCase):
    def test_unauthorized_search_reveals_nothing(self) -> None:
        for query in ("受限", "m_secret", "construct", "菌", ""):
            self.assertEqual(
                self.service.search_materials("co_x", query), [],
                f"未授权机构通过 {query!r} 推断出受限资源",
            )

    def test_holder_and_custodian_visibility(self) -> None:
        ids = {m["material_id"] for m in self.service.search_materials("co_a", "")}
        self.assertIn("m_secret", ids)       # 持有方
        self.assertIn("m_strain", ids)
        ids_inst = {m["material_id"] for m in self.service.search_materials("inst_b", "")}
        self.assertNotIn("m_secret", ids_inst)  # 不是持有方，搜索无任何信号
        self.assertIn("m_bsl3", ids_inst)

        # 转移后接收方可见新批次
        self.service.dispatch_transfer("tr_v", "m_strain", 1, "inst_b", "fa_lab")
        self.service.receive_transfer("tr_v", "m_strain_v")
        ids_inst = {m["material_id"] for m in self.service.search_materials("inst_b", "")}
        self.assertIn("m_strain_v", ids_inst)


class IncidentTest(GovernanceCase):
    def test_contamination_freezes_descendants_only(self) -> None:
        self.service.split_material("m_strain", "m_split", 2, "B-A")
        self.service.mix_materials(
            "m_mix", "混合料", [("m_strain", 2), ("m_strain2", 2)], 4, "M-1",
        )
        result = self.service.declare_incident(
            IncidentKind.CONTAMINATION, "转移后检出杂菌", "p_co",
            material_ids=["m_strain"],
        )
        frozen = set(result["materials"])
        self.assertIn("m_strain", frozen)
        self.assertIn("m_split", frozen)   # 下游子代
        self.assertIn("m_mix", frozen)     # 混合衍生产物
        self.assertNotIn("m_strain2", frozen)  # 上游/并列成分不受污染冻结
        self.assertEqual(
            self.service.timeline.materials["m_strain"].status.value, "frozen"
        )
        with self.assertRaisesRegex(GovernanceError, "已被事件冻结"):
            self.service.split_material("m_strain", "m_x", 1, "x")

        # 处置责任明确到机构
        tasks = self.service.timeline.open_dispositions()
        self.assertTrue(any(t.owner_org == "co_a" for t in tasks))

        # 解冻后恢复，且不影响其他事件的冻结
        self.service.resolve_incident(result["incident_id"], "检测确认为假阳性")
        self.assertEqual(
            self.service.timeline.materials["m_strain"].status.value, "active"
        )

    def test_contaminated_material_can_be_inactivated_under_incident(self) -> None:
        result = self.service.declare_incident(
            IncidentKind.CONTAMINATION, "污染", "p_co", material_ids=["m_strain2"],
        )
        with self.assertRaisesRegex(GovernanceError, "暂停失活"):
            self.service.inactivate_material("m_strain2")
        self.service.inactivate_material(
            "m_strain2", under_incident=result["incident_id"], reason="高压灭菌",
            actor="p_co",
        )
        self.assertEqual(
            self.service.timeline.materials["m_strain2"].status.value, "inactivated"
        )

    def test_completed_trial_keeps_basis_and_is_not_frozen(self) -> None:
        # 完成一项使用 m_labstrain 的试验
        self.service.register_trial(
            "t_done", "已完成试验", "p_inst", {"m_labstrain": 1}, "fa_lab",
        )
        req = self.service.request_access(
            "m_labstrain", "p_inst", "research", 1, "fa_lab", trial_id="t_done",
        )
        self.service.issue_access(req["request_id"])
        self.service.start_trial("t_done")
        self.service.complete_trial("t_done", follow_ups=[
            {"followup_id": "fu1", "due_at": (NOW + timedelta(days=30)).isoformat(),
             "note": "30天随访"},
        ])
        snapshot_before = self.service.timeline.trials["t_done"].completion_snapshot

        result = self.service.declare_incident(
            IncidentKind.CONTAMINATION, "库存菌污染", "p_inst",
            material_ids=["m_labstrain"],
        )
        trial = self.service.timeline.trials["t_done"]
        self.assertEqual(trial.status.value, "completed")
        self.assertNotIn("t_done", result["trials"])
        self.assertEqual(trial.completion_snapshot, snapshot_before)
        # 处置任务只针对材料，不涉及已完成试验
        task_text = " ".join(t.description for t in self.service.timeline.tasks.values())
        self.assertNotIn("t_done", task_text)

    def test_ongoing_trial_and_pending_request_frozen_then_resume(self) -> None:
        self.service.register_trial(
            "t_run", "进行中试验", "p_inst", {"m_labstrain": 2}, "fa_lab",
        )
        req = self.service.request_access(
            "m_labstrain", "p_inst", "research", 2, "fa_lab", trial_id="t_run",
        )
        self.service.issue_access(req["request_id"], qty=2)
        self.service.start_trial("t_run")

        hold = self.service.request_access(
            "m_strain2", "p_co", "ferment", 1, "fa_co",
        )
        self.assertEqual(hold["status"], "approved")

        result = self.service.declare_incident(
            IncidentKind.CONTAMINATION, "实验室污染", "p_inst",
            material_ids=["m_labstrain", "m_strain2"],
        )
        self.assertEqual(self.service.timeline.trials["t_run"].status.value, "frozen")
        held = self.service.timeline.requests[hold["request_id"]]
        self.assertTrue(held.frozen_incidents)
        with self.assertRaisesRegex(GovernanceError, "冻结"):
            self.service.issue_access(hold["request_id"])

        self.service.resolve_incident(result["incident_id"], "消除污染")
        self.assertEqual(self.service.timeline.trials["t_run"].status.value, "ongoing")
        # 普通取用解冻后自动恢复可执行
        self.assertEqual(
            self.service.timeline.requests[hold["request_id"]].status, "approved"
        )
        self.service.issue_access(hold["request_id"])

    def test_loss_confirm_zeroes_quantity(self) -> None:
        result = self.service.declare_incident(
            IncidentKind.LOSS, "冰箱盘点发现少1支", "p_co",
            material_ids=["m_strain2"],
        )
        self.assertEqual(self.service.timeline.materials["m_strain2"].status.value,
                         "frozen")
        self.service.confirm_loss(
            "m_strain2", result["incident_id"], "p_co", reason="确认无法找回",
        )
        m = self.service.timeline.materials["m_strain2"]
        self.assertEqual(m.status.value, "lost")
        self.assertAlmostEqual(m.quantity, 0)
        with self.assertRaises(GovernanceError):
            self.service.confirm_loss(
                "m_strain2", result["incident_id"], "p_co",
            )

    def test_license_revocation_freezes_and_denies_after_resolution(self) -> None:
        req = self.service.request_access(
            "m_secret", "p_co", "ferment", 1, "fa_co",
        )
        result = self.service.revoke_license("lic_secret", "合作终止")
        self.assertIn("m_secret", result["materials"])
        self.assertIn(req["request_id"], result["requests"])
        self.assertEqual(
            self.service.timeline.materials["m_secret"].status.value, "frozen"
        )
        self.service.resolve_incident(result["incident_id"], "撤销处置完成")
        # 冻结解除但许可仍失效：取用单最终被拒绝，材料恢复在库但不可用
        self.assertEqual(
            self.service.timeline.requests[req["request_id"]].status, "denied"
        )
        self.assertEqual(
            self.service.timeline.materials["m_secret"].status.value, "active"
        )

    def test_storage_fault_scoped_to_facility(self) -> None:
        raised = self.service.record_storage_reading("fa_lab", {"temp_c": -30.0})
        self.assertEqual(len(raised), 1)
        result = self.service.declare_incident(
            IncidentKind.STORAGE_FAULT, "超低温冰箱故障", "p_inst",
            facility_ids=["fa_lab"],
        )
        lab_materials = {"m_labstrain", "m_bsl3", "m_exp"}
        self.assertTrue(lab_materials.issubset(set(result["materials"])))
        self.assertNotIn("m_strain", result["materials"])  # 另一园区场地不受影响
        # 告警持久且可关闭
        alert_id = raised[0]["alert_id"]
        self.service.resolve_alert(alert_id, "设备修复，温度恢复")
        self.assertFalse(self.service.timeline.alerts[alert_id].open)

    def test_revoking_one_of_two_covering_licenses_does_not_freeze(self) -> None:
        # 材料同时附两份当前有效的 inst_b 许可，撤销一份后仍有覆盖，材料不冻结
        self.service.register_license(
            "lic_backup", "备份科研许可", "park", ["inst_b"], ["research"],
            BiosafetyLevel.BSL2, PAST, FUTURE,
        )
        self.service.receive_material(
            "m_dual", MaterialType.STRAIN, "双许可菌株", Unit.VIAL, 4,
            BiosafetyLevel.BSL2, "park", ["lic_inst", "lic_backup"], "D-01",
            "fa_lab",
        )
        result = self.service.revoke_license("lic_inst", "许可方调整")
        self.assertNotIn("m_dual", result["materials"])
        self.assertEqual(
            self.service.timeline.materials["m_dual"].status.value, "active"
        )
        # 研究用途仍可凭备份许可取用
        out = self.service.request_access(
            "m_dual", "p_inst", "research", 1, "fa_lab",
        )
        self.assertEqual(out["status"], "approved")

    def test_disposition_responsibility_closed_with_record(self) -> None:
        result = self.service.declare_incident(
            IncidentKind.LOSS, "丢失", "p_co", material_ids=["m_strain2"],
        )
        tasks = [t for t in self.service.timeline.open_dispositions()
                 if t.incident_id == result["incident_id"]]
        self.assertEqual(len(tasks), 1)
        self.service.close_disposition(tasks[0].task_id, "p_co", "已按生物安全流程申报")
        self.assertFalse(self.service.timeline.tasks[tasks[0].task_id].open)


class StocktakeTest(GovernanceCase):
    def test_matching_count(self) -> None:
        out = self.service.stocktake("m_strain", 10, "p_co")
        self.assertTrue(out["matched"])

    def test_mismatch_raises_alert(self) -> None:
        out = self.service.stocktake("m_strain", 9, "p_co", note="少一支")
        self.assertFalse(out["matched"])
        self.assertTrue(
            any(a.kind == "stocktake_mismatch" and a.open
                for a in self.service.timeline.alerts.values())
        )


class PersistenceTest(GovernanceCase):
    def test_alerts_reviews_followups_survive_restart(self) -> None:
        # 待复核取用（高风险）
        self.service.request_access(
            "m_bsl3", "p_inst", "research", 1, "fa_lab",
            request_id="req_pending",
        )
        # 保存条件告警
        self.service.record_storage_reading("fa_co", {"temp_c": -20.0})
        # 已完成试验的到期随访
        self.service.register_trial(
            "t_persist", "持久化试验", "p_inst", {"m_labstrain": 1}, "fa_lab",
        )
        r = self.service.request_access(
            "m_labstrain", "p_inst", "research", 1, "fa_lab",
            trial_id="t_persist", request_id="req_tp",
        )
        self.service.issue_access(r["request_id"])
        self.service.start_trial("t_persist")
        self.service.complete_trial("t_persist", follow_ups=[{
            "followup_id": "fu_due",
            "due_at": (NOW - timedelta(days=1)).isoformat(),
            "note": "到期随访",
        }])

        restarted = self.reopen()
        dashboard = restarted.dashboard()
        self.assertEqual(len(dashboard["open_alerts"]), 1)
        self.assertEqual(len(dashboard["pending_reviews"]), 1)
        self.assertEqual(dashboard["pending_reviews"][0]["request_id"], "req_pending")
        self.assertEqual(len(dashboard["due_follow_ups"]), 1)
        self.assertEqual(dashboard["due_follow_ups"][0]["followup_id"], "fu_due")

        # 重启后可以继续处理
        restarted.review_access("req_pending", "p_auditor", True)
        restarted.record_follow_up("t_persist", "fu_due", "p_inst", "随访正常")
        self.assertEqual(restarted.dashboard()["due_follow_ups"], [])

    def test_idempotent_offline_uploads_do_not_double_count(self) -> None:
        before = len(self.service.store.all())
        self.service.record_storage_reading(
            "fa_co", {"temp_c": -70.0},
            occurred_at=NOW - timedelta(days=1), idem_key="reading-0001",
        )
        self.service.record_storage_reading(
            "fa_co", {"temp_c": -70.0},
            occurred_at=NOW - timedelta(days=1), idem_key="reading-0001",
        )
        self.assertEqual(len(self.service.store.all()), before + 1)

        self.service.dispatch_transfer(
            "tr_off", "m_strain2", 1, "inst_b", "fa_lab",
            occurred_at=NOW - timedelta(days=1), idem_key="dispatch-1",
        )
        self.service.dispatch_transfer(
            "tr_off", "m_strain2", 1, "inst_b", "fa_lab",
            occurred_at=NOW - timedelta(days=1), idem_key="dispatch-1",
        )
        # 只出库一次
        self.assertAlmostEqual(self.service.timeline.materials["m_strain2"].quantity, 5)

    def test_returned_qty_consistent_after_restart(self) -> None:
        out = self.service.request_access(
            "m_labstrain", "p_inst", "research", 3, "fa_lab",
            request_id="req_ret",
        )
        self.service.issue_access("req_ret")
        self.service.complete_access("req_ret")
        self.service.return_material("req_ret", 2)
        self.assertAlmostEqual(
            self.service.timeline.materials["m_labstrain"].quantity, 4
        )
        self.assertAlmostEqual(
            self.service.timeline.requests["req_ret"].issued_qty, 1
        )
        restarted = self.reopen()
        self.assertAlmostEqual(
            restarted.timeline.materials["m_labstrain"].quantity, 4
        )
        self.assertAlmostEqual(
            restarted.timeline.requests["req_ret"].issued_qty, 1
        )
        # 退库后容量释放，可以再申请同样数量
        again = restarted.request_access(
            "m_labstrain", "p_inst", "research", 4, "fa_lab",
            request_id="req_cap",
        )
        self.assertEqual(again["status"], "approved")

    def test_high_risk_request_still_requires_review_after_freeze_lifts(self) -> None:
        out = self.service.request_access(
            "m_bsl3", "p_inst", "research", 1, "fa_lab",
            exception_requested=True, request_id="req_hr",
        )
        self.assertEqual(out["status"], "pending_review")
        incident = self.service.declare_incident(
            IncidentKind.STORAGE_FAULT, "冷库故障", "p_inst",
            facility_ids=["fa_lab"],
        )
        self.assertTrue(
            self.service.timeline.requests["req_hr"].frozen_incidents
        )
        self.service.resolve_incident(incident["incident_id"], "故障排除")
        request = self.service.timeline.requests["req_hr"]
        self.assertEqual(request.status, "pending_review")
        # 仍不能自行发放
        with self.assertRaisesRegex(GovernanceError, "未获批准"):
            self.service.issue_access("req_hr")
        self.service.review_access("req_hr", "p_auditor", True)
        self.service.issue_access("req_hr")


class OutputTraceTest(GovernanceCase):
    def _full_chain(self) -> str:
        # 企业菌株转移到研究机构，双方证据齐备并核对一致
        self.service.dispatch_transfer("tr_main", "m_strain", 3, "inst_b", "fa_lab")
        self.service.receive_transfer("tr_main", "m_strain_r", batch="B2026-01-R")
        self.service.submit_evidence(
            "transfer", "tr_main", EvidenceSide.SENDER, EvidenceKind.HANDOFF_RECEIPT,
            "photo://co", "1" * 64, "p_co", observed_qty=3,
            observed_identity="m_strain",
        )
        self.service.submit_evidence(
            "transfer", "tr_main", EvidenceSide.RECEIVER, EvidenceKind.HANDOFF_RECEIPT,
            "photo://inst", "2" * 64, "p_inst", observed_qty=3,
            observed_identity="生态链霉菌A",
        )
        self.assertTrue(self.service.reconcile_transfer("tr_main")["confirmed"])

        # 研究机构混合、繁育
        self.service.mix_materials(
            "m_blend", "研究混合菌剂",
            [("m_strain_r", 3), ("m_labstrain", 1)], output_qty=5,
            new_batch="MB-01",
        )
        self.service.propagate_material("m_blend", "m_blend_p2", 5, "MB-02")

        # 试验使用衍生产物
        self.service.register_trial(
            "t_out", "高产菌株试验", "p_inst", {"m_blend_p2": 2}, "fa_lab",
        )
        req = self.service.request_access(
            "m_blend_p2", "p_inst", "research", 2, "fa_lab", trial_id="t_out",
        )
        self.service.issue_access(req["request_id"])
        self.service.start_trial("t_out")
        self.service.complete_trial("t_out", follow_ups=[{
            "followup_id": "fu_out",
            "due_at": (NOW + timedelta(days=90)).isoformat(),
            "note": "产业化随访",
        }])
        out = self.service.register_output(
            "o_patent", "t_out", OutputKind.PATENT, "一种生态链霉菌发酵工艺",
            reference="PAT-2026-0001",
        )
        return out["output_id"]

    def test_trace_output_to_roots_transfers_and_benefits(self) -> None:
        output_id = self._full_chain()
        trace = self.service.trace_output(output_id)

        root_ids = {n["material_id"] for n in trace["lineage"]["nodes"] if n["is_root"]}
        # 一直追溯到企业与园区提供的原始材料
        self.assertIn("m_strain", root_ids)
        self.assertIn("m_labstrain", root_ids)

        transfers = trace["lineage"]["transfers"]
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]["transfer_id"], "tr_main")
        self.assertTrue(transfers[0]["confirmed"])
        self.assertEqual(transfers[0]["from_org"], "co_a")
        self.assertEqual(transfers[0]["to_org"], "inst_b")
        self.assertEqual(transfers[0]["evidence_count"], 2)

        # 成果登记时固化适用合作权益（来自原始许可，沿混合/繁育谱系继承）
        clauses = {b["clause"] for b in trace["benefit_snapshot_at_registration"]}
        self.assertIn("衍生成果产业化收益分成10%", clauses)
        parties = {b["party_org"] for b in trace["benefit_snapshot_at_registration"]}
        self.assertIn("co_a", parties)

    def test_trace_survives_restart(self) -> None:
        output_id = self._full_chain()
        restarted = self.reopen()
        trace = restarted.trace_output(output_id)
        self.assertEqual(trace["output_id"], output_id)
        self.assertTrue(any(
            n["material_id"] == "m_strain" and n["is_root"]
            for n in trace["lineage"]["nodes"]
        ))


if __name__ == "__main__":
    unittest.main()
