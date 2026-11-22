# sre-postmortem-action-dependency-scheduler

规划复盘行动项依赖、负责人和复发防护。

复盘行动项之间存在依赖：有些是自动验证项，有些需要人工审批。负责人按截止时间排程时，
可能出现验证项先于修复项执行的情况。本调度器接收行动项、依赖、负责人容量、截止时间、
验证证据、复发防护级别和冻结窗口，生成可执行的排程结果。

## 功能

- 依赖拓扑排序：依赖未完成前，后续项不会被排入（按截止时间优先出队）。
- 依赖环检测：环上的行动项标记为 `dependency_cycle` 风险并进入等待条件，不参与排程。
- 验证闭环：没有验证证据的人工审批项不能关闭（`evidence_missing` 风险 +
  `awaiting_approval` 等待条件）；自动验证项完成后即可关闭。
- 负责人容量：同一负责人串行执行；容量不足时，可拆分项（`splittable=True`）先排入
  部分工作量、其余延期，不可拆分项整体延期（`owner_overload` 风险）。
- 冻结窗口：窗口内不安排任何工作，排程自动跳过；截止时间落入窗口时标记
  `freeze_conflict` 风险。
- 输出：执行顺序、关键路径（按工作量加权的最长依赖链）、风险项、等待条件。

## 使用

兼容 Python 3.8.2+。

```python
from datetime import date, timedelta
from postmortem_scheduler import (
    ActionItem, FreezeWindow, Owner, Scheduler, VERIFY_AUTO, VERIFY_MANUAL,
)

start = date(2026, 11, 23)
items = [
    ActionItem("AI-1", "修复连接池泄漏", "owner-alpha", 3,
               start + timedelta(days=10),
               verification=VERIFY_MANUAL,
               verification_evidence=["pr://example/101"]),
    ActionItem("AI-2", "自动回归验证", "owner-beta", 1,
               start + timedelta(days=14),
               kind="verify", depends_on=["AI-1"], verification=VERIFY_AUTO),
]
owners = [Owner("owner-alpha", 4), Owner("owner-beta", 5)]
freezes = [FreezeWindow(start + timedelta(days=4),
                        start + timedelta(days=5), "发布冻结")]

result = Scheduler(items, owners, freezes, start).plan()
result.order          # 执行顺序（含每项起止日期与状态）
result.critical_path  # 关键路径
result.risks          # 风险项
result.waiting        # 等待条件
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖依赖环、证据缺失、负责人容量冲突和冻结/复核窗口等场景。

## 数据说明

示例与测试中的负责人均为虚构角色标识（如 `owner-alpha`），不包含任何真实人员数据。
