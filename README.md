# 生态城种质材料试验与权益边界

治理生态城种质材料、合成生物试验、场地、安全和成果权益。

## 参与方与事实

主要参与方包括园区科研运营人员、科研机构、种业与生物制造企业、生物安全审核人员、试验场地管理员。领域资料记录以下已经确认的事实：

- 中新天津生态城合作升级聚焦合成生物制造和现代种业
- 合作区建设生物制造谷和生命科学产业集群
- 双方推动医疗设备和种业等成果转化

## 业务约束

- 材料来源与许可
- 衍生谱系
- 试验审批
- 场地与保存条件
- 成果权益追溯

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/news_context_004.py` 负责读取和校验这些资料。

## 种质与试验治理后端

`src/germplasm/` 在上述领域边界上实现可执行的治理后端（仅用 Python 标准库）：

- **同一谱系登记**：接收、拆分、混合、繁育、转移、失活逐笔入账，数量守恒、台账可盘点
- **取用决策**：依据用途、机构、人员资质、生物安全等级、许可有效期、场地容量给出可执行范围；BSL3+ 与例外申请必须由独立生物安全审核人员批准，提交人不能自批
- **受限资源不可推断**：未授权机构的搜索结果不含任何受限材料存在的信号
- **事件隔离冻结**：污染、丢失、许可撤销、场地故障只冻结受影响材料与试验，已完成环节保留当时依据，并生成带责任主体的处置任务
- **核对与补传**：交接双方证据双侧核对数量与身份，扫码/回执/读数重复或离线补传幂等去重
- **重启不丢待办**：告警、待复核取用、试验随访、处置责任全部从只追加的 JSONL 事件日志重放恢复
- **成果权益追溯**：从任何成果回溯原始材料、每次转移与适用的合作权益条款

设计细节见 `docs/backend-design.md`。快速试用：

```python
from datetime import datetime
from src.germplasm import (
    GovernanceService, EventStore, MaterialType, BiosafetyLevel, Unit,
)

svc = GovernanceService(EventStore("data/events.jsonl"), clock=lambda: datetime.now())
svc.register_org("inst_b", "乙研究机构")
# register_facility / register_person / register_license 后：
svc.receive_material("m1", MaterialType.STRAIN, "生态链霉菌A", Unit.VIAL,
                     10, BiosafetyLevel.BSL2, "co_a", ["lic_co"], "B-01", "fa_lab")
```

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
