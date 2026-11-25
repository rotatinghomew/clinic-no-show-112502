# 架构与业务规则映射

## 分层结构

```
原始 JSON（data/、tests/fixtures/）
  └─ input_contract.load_bundle   输入契约：必需文件 + 可选文件，synthetic 校验
       └─ repository              结构化领域模型，保留 raw 与缺失语义，产出数据集级限制
            ├─ identity           授权范围内的身份合并（并查集）
            ├─ timeline           事实事件时间/可见时间，支撑 as-of 决策
            ├─ attendance         出勤判定：失约/到诊/取消/改期/未知显式区分
            ├─ risk               可解释因子评分 + 四级分层（含 indeterminate）
            ├─ intervention       干预动作 × 可联系时段 × 号源保护
            ├─ scheduling         候补 FIFO 调度、冻结、append-only 重排
            ├─ segments           人群/院区/科室/时段画像
            ├─ strategy           提醒模板的周期对比
            └─ report / cli       负责人综合报告与命令行
```

## 需求到实现的对照

| 业务要求 | 实现位置 | 关键行为 |
| --- | --- | --- |
| 取消、改期、记录延迟不能混成一种"未到" | `attendance.py` | 六种结果枚举；取消/改期不计失约，只有 `reconciled_absence` 判 `no_show` |
| 签到设备离线、迟到补录、无签到不自动等价失约 | `attendance.py`、`timeline.py` | 证据不足一律 `unknown` + 限制；补录经 `recorded_at` 延迟可见 |
| 家属代约、代理人、多院区、多服务拆分 | `models.Appointment`、`identity.py`、`risk._collapse_service_groups` | `registered_by` 仅溯源；`service_group_id` 归并为一个就诊单元 |
| 身份只能在授权关联范围内合并 | `identity.py` | 授权家属连通分量合并；agent 不合并；未授权关联忽略并告警 |
| 预测只能用决策时可见的事实 | `timeline.py`、`risk.assess_risk`、`classify_outcome_at` | 所有事实带 `visible_at`，as-of 过滤；可见时间缺失走保守回退 + 限制 |
| 缺少关键机会记录要输出限制，不把未知当高风险 | `risk.py` | 样本 < 3 或无身份组 → `indeterminate`，动作 `hold`；未知结果不进失约率分母 |
| 提醒版本/发送时刻与预约当时状态对应 | `timeline.py`、`strategy.py` | 提醒事实不可变；晚于就诊的提醒不算有效触达；按模板×周期统计 |
| 后补记录改变当前判断但不抹掉已发提醒/号源转移 | `timeline.py`、`scheduling.replan_after_event` | 历史 as-of 结论保持；台账只追加，旧安排标 `superseded` 保留原因 |
| 保护复诊/急诊预留，风险排序不能占用 | `intervention.py`、`scheduling.py` | `protected + followup/urgent` 永不自动释放；冲突时释放降级为人工确认 |
| 候补规则、未确认可重排、已通知要保留原因 | `scheduling.py` | FIFO + 就近窗口；offered 可 supersede，frozen/confirmed 保留并记录 follow_up |
| 负责人看到事实依据、适用范围、保护承诺、实际结果 | `report.py` | 每条建议带 `fact_refs`、限制、保护号源、`appointment_facts.outcome` |
| 比较提醒策略在后续周期的变化 | `strategy.py` | 周一对齐的 7 天周期，输出率值与相邻周期 delta，未知单列 |

## 风险因子（权重可在 `risk.py` 调整）

| 因子代码 | 权重 | 说明 |
| --- | --- | --- |
| `history_no_show_rate` | 45 | 身份组历史**核对失约率**（未知不进分母），需 ≥3 个可判定样本 |
| `reminder_gap` | 20 | 决策时刻尚无已送达提醒 |
| `recent_reschedule_cancel` | 15 | 近 30 天改期/取消次数（与失约分开计数） |
| `new_patient_uncertainty` | 10 | 初诊且历史不足 |
| `site_clinic_segment_base` | 10 | 院区×科室基线（无基线时输出限制而非填 0） |

分层阈值：≥60 高、≥35 中、其余低；触发证据不足限制时覆盖为 `indeterminate`。

## 限制代码一览

`missing_identity_links`、`missing_contact_preferences`、`unattributed_appointment`、
`unauthorized_link_ignored`、`agent_booking_not_merged`、`identity_merge_unavailable`、
`registered_at_inferred`、`status_change_time_missing`、`attendance_visibility_inferred`、
`recorded_at_missing`、`reminder_not_sent`、`reminder_sent_after_appointment`、
`waitlist_created_at_missing`、`device_offline_unverified`、`attendance_quality_missing`、
`attendance_record_missing`、`insufficient_history`、`unknown_outcomes_excluded_from_rate`、
`no_identity_group`、`segment_baseline_unavailable`、`outside_contact_window`、
`contact_preference_missing`、`no_freeable_slot`、`not_yet_due`、`status_attendance_conflict`。

## 运行方式

```bash
# 基线测试
python3 -m unittest discover -s tests -v
# 综合报告（默认 as-of = 最后可见事实之后 1 秒）
PYTHONPATH=src python3 -m clinic_no_show.cli --data data
# 指定决策时刻
PYTHONPATH=src python3 -m clinic_no_show.cli --data tests/fixtures/scenario_a \
    --as-of 2026-10-12T20:00 --output /tmp/report.json
```
