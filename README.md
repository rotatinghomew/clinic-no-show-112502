# 门诊爽约的可解释分层与号源干预

面向门诊团队的合成资料项目：把预约、改期/取消、签到与完成、提醒触达、号源与候补资料
转换为**可解释的风险分层、干预建议与候补号源安排**，并保留每条建议对应的原始事实、
数据限制、号源保护承诺与实际到诊结果。

## 核心原则

- **不把"未到登记"直接当失约**：取消、改期、设备离线、迟到补录、无签到分别建模；
  只有经核对确认的缺席才计 `no_show`，证据不足输出 `unknown` + 限制。
- **未知不是高风险**：历史样本不足或关键机会记录缺失时分层为 `indeterminate`，只观察不释放号源。
- **决策只使用当时可见的事实**：每条事实带可见时间，支持任意 as-of 时点复现决策；
  后补记录改变当前判断，但不抹掉已发提醒与已发生的号源转移。
- **身份只在授权范围内合并**：授权家属可合并，代理人代约只溯源不合并。
- **保护承诺优先**：复诊与急诊预留号源永不自动释放，风险排序不得占用。
- **候补安排可审计**：未确认安排可在取消/补录后重排；已通知安排冻结并保留原因，台账 append-only。

## 目录结构

```
src/clinic_no_show/
  input_contract.py  输入契约与可选资料加载
  repository.py      领域模型结构化，保留 raw 与缺失语义
  identity.py        授权身份合并（并查集）
  timeline.py        事实事件时间/可见时间
  attendance.py      出勤判定（六种结果 + 限制）
  risk.py            可解释因子评分与四级分层
  intervention.py    干预动作、可联系时段、号源保护
  scheduling.py      候补 FIFO 调度、冻结与重排
  segments.py        人群/院区/科室/时段画像
  strategy.py        提醒模板的周期对比
  report.py / cli.py 综合报告与命令行
tests/               40 个单元/场景测试 + 丰富合成夹具 tests/fixtures/scenario_a
docs/                数据字典与架构规则映射
```

## 运行

```bash
# 全部测试
python3 -m unittest discover -s tests -v
# 语法检查
python3 -m compileall src tests
# 综合报告
PYTHONPATH=src python3 -m clinic_no_show.cli --data data
PYTHONPATH=src python3 -m clinic_no_show.cli \
  --data tests/fixtures/scenario_a --as-of 2026-10-12T20:00
```

字段语义见 `docs/data-dictionary.md`，需求与实现的逐条映射见 `docs/architecture.md`。
所有记录均带合成资料标记，仅用于本地可复现运行。
