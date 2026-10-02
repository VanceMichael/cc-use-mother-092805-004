# 种质与试验治理后端设计

治理菌株、种子、基因构件等材料从接收、衍生、转移、取用、试验到成果的完整生命周期。
核心原则：**只追加的事件日志是唯一事实来源**，谱系、数量、冻结、告警、待办、
责任与成果快照全部是日志的投影，进程重启后重放即可恢复。

## 1. 事件时间线

所有写操作经 `GovernanceService` 校验后追加一条事件（`src/germplasm/events.py`），
`Timeline`（`src/germplasm/timeline.py`）在重放时重建全部状态。

| 主题 | 事件 |
| --- | --- |
| 基础登记 | `ORG_REGISTERED` `FACILITY_REGISTERED` `PERSON_REGISTERED` `LICENSE_REGISTERED` |
| 材料谱系 | `MATERIAL_RECEIVED` `MATERIAL_SPLIT` `MATERIAL_MIXED` `MATERIAL_PROPAGATED` |
| 数量变动 | `MATERIAL_CONSUMED` `MATERIAL_INACTIVATED` `MATERIAL_RETURNED` `MATERIAL_LOST` `ACCESS_ISSUED` |
| 转移证据 | `TRANSFER_DISPATCHED` `TRANSFER_RECEIVED` `EVIDENCE_SUBMITTED` `TRANSFER_CONFIRMED` `TRANSFER_DISPUTED` |
| 取用审批 | `ACCESS_REQUESTED` `ACCESS_DECIDED` `ACCESS_REVIEWED` `ACCESS_ISSUED` `ACCESS_COMPLETED` |
| 试验随访 | `TRIAL_REGISTERED` `TRIAL_STARTED` `TRIAL_COMPLETED` `TRIAL_TERMINATED` `TRIAL_FOLLOWUP_RECORDED` |
| 事件处置 | `INCIDENT_DECLARED` `FREEZE_APPLIED` `FREEZE_LIFTED` `INCIDENT_RESOLVED` `LICENSE_REVOKED` `DISPOSITION_TASK_OPENED/CLOSED` |
| 告警盘点 | `STORAGE_READING` `ALERT_RAISED` `ALERT_RESOLVED` `STOCKTAKE_RECORDED` |
| 成果 | `OUTPUT_REGISTERED` |

每条事件带 `seq`、`occurred_at`（事实发生时间）、`recorded_at`（进入日志时间）、
`idem_key`（幂等键）。离线补传保留真实发生时间；扫码、回执、读数、发货等可能
重复提交的事实，以业务要素生成稳定幂等键，重复提交只生效一次，不重复扣数。

日志为每行一个 JSON（JSONL），写入时 `flush + fsync`；重放校验序号连续，
日志损坏立即报错，不静默吞掉。

## 2. 谱系与数量

- 每份材料记录类型、单位、生物安全等级、来源机构、批次、保存条件要求、
  适用许可 ID，以及 `parents`（亲本及投入量）与 `derivation`
  （receipt / split / mix / propagate / transfer）。
- **拆分**：按分出量扣减亲本，新建子批次。
- **混合**：只允许同机构、同场地、同单位的成分；各成分按投入量扣减，
  子代继承全部成分许可，生物安全等级取最高；任一许可禁止衍生则阻断。
- **繁育**：原种保留不扣减，新增产量登记为子代（亲本投入量记 0）。
- **转移**：发货时从发出方库存预扣；接收时在接收方生成 `transfer` 谱系节点，
  与原始批次以亲子边相连，来源可溯。
- 每个材料带逐笔数量台账 `ledger`，任何时刻账面数量 = 接收/产出 − 拆分投入 −
  混合投入 − 转移出库 − 发放 − 失活 − 确认丢失 + 退库，可与盘点核对。
- 场地分**保存容量**（在库数量）与**使用容量**（批准待发放 + 已发放未办结），
  两条线独立核算，避免批准后重复占位。

## 3. 许可与取用决策

研究人员申请取用时，`_evaluate_access` 同时检查六个因素并在 `decision.factors`
中留档，给出 `executable` 可执行范围（用途、数量、场地、材料）：

1. **用途**：适用许可的 `allowed_purposes` 覆盖本次用途（`*` 为全用途）；
   多份许可时任一有效覆盖即可，全部不覆盖才拒绝。
2. **机构**：申请机构必须是某份适用许可的持有方，或是材料当前保管方。
3. **人员资质**：人员允许等级 ≥ 材料等级，且账号有效。
4. **生物安全等级**：使用场地等级 ≥ 材料等级，且场地属于申请机构。
5. **许可有效期**：申请时点未撤销、未超出 `valid_from/valid_to`；
   接收时按接收时点（含补传的历史时点）校验。
6. **场地容量 / 方案用量**：使用容量够；关联试验时不得超出方案清单中
   该材料的计划用量（已发放 + 其他待决申请构成承诺量，评估时排除申请自身）。

决策结论：

- 条件齐备且非高风险 → `approved`；
- **高风险**（BSL≥3 或申请人主动声明例外）→ `pending_review`，必须由具备
  生物安全审核资质的**独立**审核人批准：提交人本人、无资质人员一律不能批准；
  审核人资质还要不低于材料等级；
- 仅因事件冻结挂起 → `pending_review`，解冻后重新评估；
- 其余硬性不通过 → `denied`。

`issue_access` 在发放瞬间用当前状态**重新评估全部因素**：批准后许可被撤销、
库存不足、容量变化、试验超量等都会阻断发放，但原批准记录保留，满足
"已完成环节保留当时依据"。

## 4. 受限资源不可推断

`search_materials(org, query)` 只在该机构的可见集合内检索，可见集合 =
当前保管的材料 ∪ 适用许可持有方包含该机构的材料。未授权机构对受限资源：
搜不到命中文档，也得不到空字段或数量差异等任何信号——连资源是否存在都无法推断。
转移接收后，接收方仅能看到自己持有的新批次节点。

## 5. 事件隔离冻结与处置责任

`declare_incident` 支持四类事件，冻结范围按事件类型精确推导：

- **污染**：指定材料及其全部下游衍生材料（子代、混合产物）；
- **丢失**：指定材料；
- **许可撤销**：所有仍受该许可约束的在库材料；
- **场地故障**：该场地内在库（未失活/未丢失）的全部材料。

关联进行中/计划中试验、待批准取用单自动纳入冻结；**已完成、已终止的试验
不冻结、不改写**，完成时的 `completion_snapshot`（方案、取用依据）原样保留。

冻结按事件集合隔离：一个材料/试验/取用单可同时被多个事件冻结，解除其中一个
不影响其他事件。解冻后：

- 普通取用单重新评估——依据仍有效恢复批准，许可已失效则降级拒绝；
- 高风险取用继续等待独立复核；
- 进行中试验恢复 ongoing。

每个事件生成 `DISPOSITION_TASK_OPENED`，明确**责任机构与处置内容**
（污染隔离检测、丢失查找申报、设施抢修、撤销许可停用等），任务关闭需记录
处理人与结果；事件解除不自动关闭任务，责任全程留痕。冻结材料的失活必须
指明所属事件（`inactivate_material(..., under_incident=...)`），防止越权处置；
确认丢失通过 `confirm_loss` 将账面清零并标记 lost。

## 6. 核对、告警与待办持久化

- 交接证据区分发送方/接收方与扫码/回执/装箱单，带内容哈希与观测数量、身份；
  `reconcile_transfer` 要求双侧齐备且数量、身份一致才确认，不一致标记争议
  并产生 `transfer_discrepancy` 告警。同内容重复提交自动去重，双方各持的
  不同照片分别保留。
- `stocktake` 比对账面与实测，不符产生 `stocktake_mismatch` 告警。
- `record_storage_reading` 按场地登记的保存条件区间判定越限并告警；
  同类未关闭告警不重复堆叠。
- 管理视图 `dashboard()` 汇总开放告警、待复核取用、到期随访、开放处置任务。
  它们都是事件投影，**进程重启后从日志完整恢复**，测试
  `PersistenceTest` 覆盖重启后继续审批与随访。

## 7. 成果权益追溯

试验完成时登记随访；成果（数据集、论文、专利、产品、方法）通过
`register_output` 登记，并在登记时点固化 `benefit_snapshot`。
`trace_output` 从成果回溯：

- 试验所用每份材料 → 混合/繁育/拆分边 → 接收入库的原始材料；
- 路径上的每次转移（双方机构、时间、确认状态、证据数量）；
- 沿原始许可继承、在成果登记时适用的全部合作权益条款
  （权益方、条款、分成、适用范围）。

这样管理人员可以从任何成果一路追到原始材料、每次转移和适用的合作权益，
且登记后即使许可后来变更，成果当时的权益依据仍可核对。

## 8. 模块与验证

- `src/germplasm/models.py`：枚举与投影数据结构
- `src/germplasm/events.py`：事件、JSONL 日志、幂等与重放
- `src/germplasm/timeline.py`：事件投影、谱系与可见性查询
- `src/germplasm/service.py`：治理门面，全部业务规则
- `tests/test_germplasm.py`：40 个用例覆盖上述规则（标准库 unittest，无外部依赖）

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```
