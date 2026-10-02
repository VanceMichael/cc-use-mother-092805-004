# 种质与试验治理后端设计

`src/governance/` 实现园区种质材料、合成生物试验与成果权益的治理后端。
纯标准库（SQLite 持久化），时钟可注入，便于测试与审计复现。

## 需求到机制的映射

| 需求 | 机制 | 位置 |
| --- | --- | --- |
| 接收时记录来源、保存条件、质量批次、许可条款 | `intake_material` 落 `materials` + `intake` 事件，事件携带许可快照 | `service.py` |
| 拆分、混合、繁育、转移、失活沿同一谱系登记 | 五类 `events`，输入输出数量守恒，`quantity_ledger` 可重算核对 | `service.py` |
| 取用按用途、机构、资质、生物安全等级、许可有效期、场地容量给出可执行范围 | `evaluate_access` 纯函数评估，返回 `max_quantity` 与拒绝原因 | `policy.py` |
| 高风险例外不能由提交人自行批准 | 风险标记（高生物安全等级/大额取用/跨机构转移）进入 `needs_exception`，`approve_exception` 强制复核人≠提交人且具备生物安全审核资质 | `service.py` |
| 未授权企业不能通过搜索推断受限资源存在 | 可见性过滤；`get_material` 与取用拒绝对“不存在”和“受限”返回同一文案，不返回聚合计数、不泄露风险标记 | `service.py`、`policy.py` |
| 污染、丢失、许可撤销、场地故障只冻结受影响对象 | `report_incident` 按类型计算受影响集合（污染含下游谱系，许可撤销含衍生），冻结落 `freezes`，试验置 `suspended` | `service.py` |
| 已完成环节保留当时依据并生成处置责任 | 事件不可变且携带 `basis`（许可快照/取用授权）；每个受影响材料生成 `disposal_tasks` 指派保管机构 | `service.py` |
| 扫描与交接回执可重复或离线补传，数量、身份、内容可核对 | 回执以 `id` 为幂等键，重复提交返回首次结果；交接/扫描回执与事件、台账核对数量与内容摘要，不符即告警 | `service.py` |
| 告警、待复核取用、试验随访不因进程重启消失 | `jobs`/`alerts` 表持久化，`run_due_jobs` 由任意新实例继续执行；随访按周期自动重排 | `service.py` |
| 从成果追溯到原始材料、每次转移与适用权益 | `trace_outcome` 沿谱系回溯到 `intake`，权益按事件数量权重回溯到各原始许可的 `equity_shares` 与 `derivative_owner` | `service.py` |

## 关键不变量

- **数量守恒**：拆分/混合/繁育/转移的输入输出都记事件，`quantity_ledger`
  重算的剩余量必须与投影一致（`consistent=True`），失活数量单独可核。
- **依据不可改写**：事件只增不改。许可撤销只影响之后的评估，已完成
  环节的 `basis` 保留当时的许可状态与授权决定。
- **冻结最小化**：事故只冻结受影响材料与关联试验；解冻需园区运营或
  生物安全审核角色并记录理由。
- **幂等入账**：同一回执（接收、交接、扫描）无论重复提交还是离线
  补传，只入账一次，后续提交返回首次处理结果。

## 运行

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```
