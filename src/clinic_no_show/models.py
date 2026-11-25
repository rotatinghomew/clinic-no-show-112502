"""领域模型：保留原始事实的结构化记录与统一的限制（数据缺口）对象。

设计原则：
- 所有原始字段可经 ``raw`` 追溯，分析层不覆盖原始事实。
- 未知状态显式建模（``quality == missing``、``checked_in_at is None`` 等），
  不把缺失静默当作高风险或失约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---- 预约 / 出勤 / 提醒 / 号源 / 候补 的状态常量（未知值原样保留） ----

APPT_SCHEDULED = "scheduled"
APPT_RESCHEDULED = "rescheduled"
APPT_CANCELLED = "cancelled"
APPT_COMPLETED = "completed"
ACTIVE_APPT_STATUSES = frozenset({APPT_SCHEDULED, APPT_RESCHEDULED})

# 签到设备离线、迟到补录、缺失、已核对的真实失约
QUALITY_OFFLINE = "offline_device"
QUALITY_LATE_ENTRY = "late_entry"
QUALITY_MISSING = "missing"
QUALITY_RECONCILED_ABSENCE = "reconciled_absence"
SOURCE_DEVICE = "device"
SOURCE_MANUAL = "manual"

REMINDER_DELIVERED = "delivered"
REMINDER_PENDING = "pending"
REMINDER_FAILED = "failed"

SLOT_PROTECTED = "protected"
SLOT_OPEN = "open"
SLOT_RELEASED = "released"
SLOT_FROZEN = "frozen"  # 已通知患者的安排，不可静默重排

WAITLIST_PENDING = "pending"
WAITLIST_OFFERED = "offered"
WAITLIST_CONFIRMED = "confirmed"
WAITLIST_CANCELLED = "cancelled"

# ---- 出勤判定（不与"未到登记"直接等价） ----

OUTCOME_ARRIVED = "arrived"                 # 有签到并完成（含迟到补录）
OUTCOME_COMPLETED_NO_SCAN = "completed_no_scan"  # 无签到但有完成记录
OUTCOME_NO_SHOW = "no_show"                 # 经核对确认的失约
OUTCOME_UNKNOWN = "unknown"                 # 记录缺失/设备离线/证据不足
OUTCOME_CANCELLED = "cancelled"             # 就诊前取消
OUTCOME_RESCHEDULED = "rescheduled"         # 改期，旧预约不再算失约

# ---- 风险分层与干预动作 ----

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"
RISK_INDETERMINATE = "indeterminate"  # 关键机会记录缺失：不把未知当高风险
LIMIT_RISK_SCORE = 100.0  # 风险分上限

ACTION_REMINDER = "reminder"
ACTION_MANUAL_CONFIRM = "manual_confirm"
ACTION_RELEASE_SLOT = "release_slot"
ACTION_HOLD = "hold"

# 授权关系
LINK_AGENT = "agent"        # 代理人：只用于登记代办，不合并身份
LINK_FAMILY_AUTHORIZED = "family_authorized"  # 授权家属：可合并身份


@dataclass(frozen=True)
class Limitation:
    """数据缺口/规则限制。缺少关键机会记录时必须输出，而不是猜测。"""

    code: str
    message: str
    refs: tuple[str, ...] = field(default_factory=tuple)
    severity: str = "info"  # info | warning | blocking

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "refs": list(self.refs), "severity": self.severity}


@dataclass(frozen=True)
class Appointment:
    appointment_id: str
    patient_key: str | None
    clinic: str
    site: str
    starts_at: datetime
    type: str
    status: str
    registered_by: str | None = None        # 代理人/家属代约登记方
    service_group_id: str | None = None     # 一次预约拆成多个服务时的归组
    changed_at: datetime | None = None      # 状态变更（取消/改期）发生时间
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttendanceRecord:
    appointment_id: str
    checked_in_at: datetime | None
    completed_at: datetime | None
    record_source: str
    quality: str
    recorded_at: datetime | None = None  # 记录进入系统的可见时间；缺失时见时间线限制
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    appointment_id: str
    sent_at: datetime | None
    template: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Slot:
    slot_id: str
    site: str
    starts_at: datetime
    reserved_for: str | None
    status: str
    clinic: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WaitlistRequest:
    request_id: str
    clinic: str
    site: str
    requested_window: datetime
    status: str
    patient_key: str | None = None
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityLink:
    patient_key: str
    linked_key: str
    relation: str
    authorized: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContactPreference:
    patient_key: str
    contact_windows: tuple[tuple[str, str, str], ...]  # (day, HH:MM, HH:MM) day=mon..sun
    channels: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict)
