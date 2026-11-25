"""事实时间线：每条事实同时记录事件时间与"对系统可见时间"。

预测只能使用决策时刻（as_of）已经可见的事实。记录延迟（签到设备离线、
迟到补录）通过 ``visible_at`` 建模：后补记录不会改变历史决策，只会影响
当前判断。可见时间缺失时按可解释的回退规则推断并打标记、产出限制，
绝不静默当作一直可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import (
    QUALITY_LATE_ENTRY,
    QUALITY_MISSING,
    QUALITY_OFFLINE,
    Limitation,
)
from .repository import Repository

FACT_APPOINTMENT = "appointment_registered"
FACT_STATUS_CHANGE = "appointment_status_change"
FACT_REMINDER = "reminder_sent"
FACT_ATTENDANCE = "attendance_recorded"
FACT_SLOT = "slot_state"
FACT_WAITLIST = "waitlist_state"


@dataclass(frozen=True)
class Fact:
    fact_id: str
    kind: str
    event_at: datetime
    visible_at: datetime
    appointment_ids: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)
    visibility_inferred: bool = False
    raw_ref: str = ""

    def is_visible(self, as_of: datetime) -> bool:
        return self.visible_at <= as_of

    def to_dict(self) -> dict[str, Any]:
        from .time_utils import to_iso

        return {
            "fact_id": self.fact_id,
            "kind": self.kind,
            "event_at": to_iso(self.event_at),
            "visible_at": to_iso(self.visible_at),
            "appointment_ids": list(self.appointment_ids),
            "payload": self.payload,
            "visibility_inferred": self.visibility_inferred,
            "raw_ref": self.raw_ref,
        }


@dataclass
class Timeline:
    facts: list[Fact]
    limitations: list[Limitation]

    def visible_facts(self, as_of: datetime) -> list[Fact]:
        return [f for f in self.facts if f.is_visible(as_of)]

    def facts_for(self, appointment_id: str) -> list[Fact]:
        return [f for f in self.facts if appointment_id in f.appointment_ids]

    def visible_facts_for(self, appointment_id: str, as_of: datetime) -> list[Fact]:
        return [f for f in self.facts
                if appointment_id in f.appointment_ids and f.is_visible(as_of)]

    def latest_visible_time(self) -> datetime | None:
        return max((f.visible_at for f in self.facts), default=None)


def build_timeline(repo: Repository) -> Timeline:
    facts: list[Fact] = []
    limitations: list[Limitation] = []
    seq = 0

    def next_id(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}-{seq:04d}"

    # ---- 预约登记与状态变更 ----
    for appt in repo.appointments:
        registered_at = _registered_at(appt.raw, appt.starts_at)
        inferred = "registered_at" not in appt.raw
        if inferred:
            limitations.append(Limitation(
                code="registered_at_inferred",
                message="预约缺少登记时间，可见时间按就诊开始时间推断，as-of 决策可能偏晚",
                refs=(appt.appointment_id,),
                severity="info",
            ))
        facts.append(Fact(
            fact_id=next_id("fact"),
            kind=FACT_APPOINTMENT,
            event_at=appt.starts_at,
            visible_at=registered_at,
            appointment_ids=(appt.appointment_id,),
            payload={
                "appointment_id": appt.appointment_id,
                "patient_key": appt.patient_key,
                "clinic": appt.clinic,
                "site": appt.site,
                "starts_at": appt.starts_at.isoformat(),
                "type": appt.type,
                "status": appt.status,
                "service_group_id": appt.service_group_id,
                "registered_by": appt.registered_by,
            },
            visibility_inferred=inferred,
            raw_ref=f"appointments.json#{appt.appointment_id}",
        ))
        if appt.status in ("rescheduled", "cancelled"):
            if appt.changed_at is None:
                limitations.append(Limitation(
                    code="status_change_time_missing",
                    message="取消/改期缺少变更时间，as-of 时点按就诊开始时间保守推断",
                    refs=(appt.appointment_id,),
                    severity="warning",
                ))
            change_at = appt.changed_at or appt.starts_at
            facts.append(Fact(
                fact_id=next_id("fact"),
                kind=FACT_STATUS_CHANGE,
                event_at=change_at,
                visible_at=change_at,
                appointment_ids=(appt.appointment_id,),
                payload={"appointment_id": appt.appointment_id, "new_status": appt.status},
                visibility_inferred=appt.changed_at is None,
                raw_ref=f"appointments.json#{appt.appointment_id}",
            ))

    # ---- 提醒：已发送且有 sent_at 才构成可见事实；pending 不是事实 ----
    for rem in repo.reminders:
        if rem.sent_at is None:
            limitations.append(Limitation(
                code="reminder_not_sent",
                message="提醒尚未发送（pending），不能计入已触达干预",
                refs=(rem.reminder_id, rem.appointment_id),
                severity="info",
            ))
            continue
        facts.append(Fact(
            fact_id=next_id("fact"),
            kind=FACT_REMINDER,
            event_at=rem.sent_at,
            visible_at=rem.sent_at,
            appointment_ids=(rem.appointment_id,),
            payload={
                "reminder_id": rem.reminder_id,
                "template": rem.template,
                "status": rem.status,
                "sent_at": rem.sent_at.isoformat(),
            },
            raw_ref=f"reminders.json#{rem.reminder_id}",
        ))

    # ---- 出勤记录：可见时间决定后补记录何时影响判断 ----
    for rec in repo.attendance:
        if rec.quality == QUALITY_MISSING and rec.checked_in_at is None and rec.completed_at is None:
            # 明确的"缺失"记录：登记了缺失这一事实，但不产生任何到诊证据。
            facts.append(Fact(
                fact_id=next_id("fact"),
                kind=FACT_ATTENDANCE,
                event_at=rec.recorded_at or _fallback_time(repo, rec.appointment_id),
                visible_at=rec.recorded_at or _fallback_time(repo, rec.appointment_id),
                appointment_ids=(rec.appointment_id,),
                payload={"quality": QUALITY_MISSING, "has_evidence": False},
                visibility_inferred=rec.recorded_at is None,
                raw_ref=f"attendance.json#{rec.appointment_id}",
            ))
            if rec.recorded_at is None:
                limitations.append(_visibility_limit(rec.appointment_id, rec.quality))
            continue

        inferred = rec.recorded_at is None
        fallback = rec.recorded_at or _fallback_time(repo, rec.appointment_id)
        if inferred:
            limitations.append(_visibility_limit(rec.appointment_id, rec.quality))
        facts.append(Fact(
            fact_id=next_id("fact"),
            kind=FACT_ATTENDANCE,
            event_at=rec.checked_in_at or rec.completed_at or fallback,
            visible_at=fallback,
            appointment_ids=(rec.appointment_id,),
            payload={
                "checked_in_at": rec.checked_in_at.isoformat() if rec.checked_in_at else None,
                "completed_at": rec.completed_at.isoformat() if rec.completed_at else None,
                "record_source": rec.record_source,
                "quality": rec.quality,
                "has_evidence": True,
            },
            visibility_inferred=inferred,
            raw_ref=f"attendance.json#{rec.appointment_id}",
        ))

    # ---- 号源与候补状态 ----
    for slot in repo.slots:
        visible_at = _slot_visible_at(slot.raw, slot.starts_at)
        facts.append(Fact(
            fact_id=next_id("fact"),
            kind=FACT_SLOT,
            event_at=slot.starts_at,
            visible_at=visible_at,
            appointment_ids=(),
            payload={"slot_id": slot.slot_id, "status": slot.status,
                     "reserved_for": slot.reserved_for, "site": slot.site,
                     "clinic": slot.clinic},
            visibility_inferred="visible_at" not in slot.raw,
            raw_ref=f"slots.json#{slot.slot_id}",
        ))

    for wl in repo.waitlist:
        visible_at = wl.created_at or wl.requested_window
        if wl.created_at is None:
            limitations.append(Limitation(
                code="waitlist_created_at_missing",
                message="候补申请缺少创建时间，按申请窗口推断可见时间",
                refs=(wl.request_id,),
                severity="info",
            ))
        facts.append(Fact(
            fact_id=next_id("fact"),
            kind=FACT_WAITLIST,
            event_at=wl.requested_window,
            visible_at=visible_at,
            appointment_ids=(),
            payload={"request_id": wl.request_id, "clinic": wl.clinic,
                     "site": wl.site, "status": wl.status},
            visibility_inferred=wl.created_at is None,
            raw_ref=f"waitlist.json#{wl.request_id}",
        ))

    facts.sort(key=lambda f: (f.visible_at, f.fact_id))
    return Timeline(facts=facts, limitations=limitations)


def _registered_at(raw: dict[str, Any], starts_at: datetime) -> datetime:
    value = raw.get("registered_at")
    from .time_utils import parse_ts

    return parse_ts(value) or starts_at


def _slot_visible_at(raw: dict[str, Any], starts_at: datetime) -> datetime:
    value = raw.get("visible_at")
    from .time_utils import parse_ts

    return parse_ts(value) or starts_at


def _fallback_time(repo: Repository, appointment_id: str) -> datetime:
    appt = repo.appointment(appointment_id)
    if appt is not None:
        return appt.starts_at
    raise ValueError(f"出勤记录 {appointment_id} 找不到对应预约，无法推断可见时间")


def _visibility_limit(appointment_id: str, quality: str) -> Limitation:
    if quality in (QUALITY_OFFLINE, QUALITY_LATE_ENTRY):
        return Limitation(
            code="attendance_visibility_inferred",
            message=("签到设备离线/迟到补录且缺少记录进入时间，可见时间按就诊开始时间推断；"
                     "该事实不会进入更早时点的决策"),
            refs=(appointment_id,),
            severity="warning",
        )
    return Limitation(
        code="recorded_at_missing",
        message="记录缺少可见时间，as-of 决策按保守回退处理",
        refs=(appointment_id,),
        severity="warning",
    )
