# 资料说明与字段字典

所有资料均为合成记录（`source_info.json.source_kind = "synthetic"`），仅用于本地可复现运行。
文件之间通过稳定标识（`appointment_id`、`slot_id`、`request_id`、`patient_key`）与时间字段关联。
缺失、部分质量和待处理状态都是资料含义的一部分，读取层不会静默填充。

## 必需文件

### appointments.json（预约/改期/取消）

| 字段 | 含义 |
| --- | --- |
| `appointment_id` | 预约稳定标识 |
| `patient_key` | 患者键；`null` 表示无法归属（如匿名/代理人代约未回填），只能做预约级评估 |
| `clinic` / `site` | 科室 / 院区 |
| `starts_at` | 就诊开始时间（ISO8601 本地时间） |
| `type` | `new` 初诊、`followup` 复诊、`lab` 化验等服务类型 |
| `status` | `scheduled` 有效、`rescheduled` 已改期（旧预约不算失约）、`cancelled` 已取消、`completed`/`no_show` 终态 |
| `registered_at` | 可选。登记进入系统的时间，即该事实的可见时间；缺失时按 `starts_at` 推断并输出限制 |
| `changed_at` | 可选。改期/取消的发生与可见时间；缺失时按 `starts_at` 保守推断并输出限制 |
| `registered_by` | 可选。代理人/家属代约的登记方，仅用于溯源，不参与身份合并 |
| `service_group_id` | 可选。一次预约拆成多个服务时共用同一归组，历史统计按一个就诊单元处理 |

### attendance.json（签到与完成就诊）

| 字段 | 含义 |
| --- | --- |
| `appointment_id` | 关联预约 |
| `checked_in_at` | 签到时间；`null` 不自动等价于失约 |
| `completed_at` | 完成就诊时间；无签到但有完成记录判为 `completed_no_scan`（推断级证据） |
| `record_source` | `device` 设备 / `manual` 人工 |
| `quality` | `ok` 正常、`late_entry` 迟到补录、`offline_device` 设备离线、`missing` 明确缺失、`reconciled_absence` 经核对确认的失约 |
| `recorded_at` | 可选。记录进入系统的时间（事实可见时间）；缺失时按就诊开始时间推断并输出限制 |

判定规则（见 `attendance.py`）：取消/改期优先且不算失约；`reconciled_absence` 才是 `no_show`；
离线、缺失、无记录一律 `unknown` 并附限制，不把未知当高风险。

### reminders.json（提醒触达）

| 字段 | 含义 |
| --- | --- |
| `reminder_id` | 提醒标识 |
| `appointment_id` | 关联预约 |
| `sent_at` | 发送时刻；`null` 且 `status=pending` 时不构成已触达事实 |
| `template` | 提醒版本（`v1`/`v2`/`v3`…），用于策略周期对比 |
| `status` | `delivered` 已送达、`pending` 待发送、`failed` 发送失败 |

晚于就诊开始时间的提醒不计为有效触达并输出限制。

### slots.json（号源）

| 字段 | 含义 |
| --- | --- |
| `slot_id` | 号源标识 |
| `site` / `clinic` | 院区 / 科室 |
| `starts_at` | 号源时段 |
| `reserved_for` | `followup` 复诊承诺、`urgent` 急诊预留（均为保护用途）；`null` 为普通号源 |
| `status` | `protected` 保护、`open` 可分配、`released` 已释放 |
| `visible_at` | 可选。号源状态的可见时间，缺失时按 `starts_at` 推断 |

`protected` 且 `reserved_for ∈ {followup, urgent}` 的号源永不自动释放或被风险排序占用。

### waitlist.json（候补申请）

| 字段 | 含义 |
| --- | --- |
| `request_id` | 候补申请标识 |
| `clinic` / `site` | 期望科室 / 院区 |
| `requested_window` | 期望时段 |
| `status` | `pending` 待处理、`offered` 已给号、`confirmed` 已确认、`cancelled` 已取消 |
| `patient_key` | 可选。申请人患者键 |
| `created_at` | 可选。申请创建时间，作为 FIFO 排序依据；缺失时按申请窗口推断 |

## 可选文件（缺失会在报告中输出数据集级限制）

### identity_links.json（授权身份关联）

| 字段 | 含义 |
| --- | --- |
| `patient_key` / `linked_key` | 关联的两个患者键 |
| `relation` | `family_authorized` 授权家属（可合并）、`agent` 代理人（仅登记溯源，不合并） |
| `authorized` | `true` 才允许合并；未授权关联被忽略并输出限制 |

### contact_preferences.json（患者可联系时段）

| 字段 | 含义 |
| --- | --- |
| `patient_key` | 患者键 |
| `windows` | `{day: mon..sun, from: HH:MM, to: HH:MM}` 列表 |
| `channels` | 允许的联系渠道，如 `sms`、`call` |

缺失时使用默认窗口（工作日 09:00–18:00）并对每条建议标注 `contact_preference_missing` 限制。

## 事实与可见时间

分析层（`timeline.py`）为每条记录同时保留事件时间与可见时间：
设备离线、迟到补录的记录只在 `recorded_at` 之后才对决策可见；
as-of 决策只能使用 `visible_at <= as_of` 的事实。后补记录改变当前判断，
但不会抹掉已经发出的提醒和已经发生的号源转移（安排台账 append-only）。
