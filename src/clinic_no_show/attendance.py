"""出勤判定：把原始登记转换为可解释的就诊结果。

关键约束（对应业务规则）：
- 取消、改期是独立行为，**不计入失约**；
- 签到设备离线、迟到补录、没有签到记录不能自动等价为失约；
- 只有经核对确认（``reconciled_absence``）或证据充分的缺席才判 ``no_show``；
- 证据不足一律 ``unknown`` 并附限制，不把未知当高风险。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    OUTCOME_ARRIVED,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED_NO_SCAN,
    OUTCOME_NO_SHOW,
    OUTCOME_RESCHEDULED,
    OUTCOME_UNKNOWN,
    QUALITY_MISSING,
    QUALITY_OFFLINE,
    QUALITY_RECONCILED_ABSENCE,
    Limitation,
)
from .repository import Repository
from .timeline import FACT_STATUS_CHANGE, FACT_ATTENDANCE, Timeline


@dataclass(frozen=True)
class Outcome:
    appointment_id: str
    outcome: str
    evidence_level: str  # direct | inferred | none
    reasons: tuple[str, ...] = field(default_factory=tuple)
    limitation: Limitation | None = None

    def to_dict(self) -> dict:
        return {
            "appointment_id": self.appointment_id,
            "outcome": self.outcome,
            "evidence_level": self.evidence_level,
            "reasons": list(self.reasons),
            "limitation": self.limitation.to_dict() if self.limitation else None,
        }


def classify_outcome(repo: Repository, appointment_id: str) -> Outcome:
    """基于当前全部资料的判定（后补记录已包含在内）。"""
    appt = repo.appointment(appointment_id)
    if appt is None:
        return Outcome(appointment_id, OUTCOME_UNKNOWN, "none",
                       ("找不到预约记录",),
                       Limitation("appointment_missing", "预约不存在，无法判定",
                                  (appointment_id,), "blocking"))
    rec = repo.attendance_for(appointment_id)
    return _classify(appointment_id, appt.status, appt.starts_at, rec, as_of=None)


def classify_outcome_at(repo: Repository, timeline: Timeline,
                        appointment_id: str, as_of: datetime) -> Outcome | None:
    """as-of 判定：只使用 as_of 时已可见的事实；预约尚不可见时返回 None。"""
    appt = repo.appointment(appointment_id)
    if appt is None:
        return None
    facts = timeline.visible_facts_for(appointment_id, as_of)
    if not any(f.kind == "appointment_registered" for f in facts):
        return None

    status = "scheduled"
    change_facts = [f for f in facts if f.kind == FACT_STATUS_CHANGE]
    if change_facts:
        status = change_facts[-1].payload["new_status"]

    rec = None
    attend_facts = [f for f in facts if f.kind == FACT_ATTENDANCE]
    if attend_facts:
        rec = repo.attendance_for(appointment_id)
    return _classify(appointment_id, status, appt.starts_at, rec, as_of=as_of)


def _classify(appointment_id: str, status: str, starts_at: datetime,
              rec, as_of: datetime | None) -> Outcome:
    # 1) 取消/改期优先：它们不是失约。若同时存在到诊证据则报冲突，不静默吞掉。
    if status == "cancelled":
        limitation = _conflict_if_evidence(appointment_id, rec)
        return Outcome(appointment_id, OUTCOME_CANCELLED, "direct",
                       ("预约在就诊前取消，取消行为不计入失约",), limitation)
    if status == "rescheduled":
        limitation = _conflict_if_evidence(appointment_id, rec)
        return Outcome(appointment_id, OUTCOME_RESCHEDULED, "direct",
                       ("预约已改期，旧预约的未到不计入失约",), limitation)

    # 2) 到诊证据（迟到补录仍是到诊；无签到但有完成记录单独标注）。
    if rec is not None and rec.checked_in_at is not None:
        reasons = ["存在签到记录"]
        if rec.quality == "late_entry":
            reasons.append("迟到补录：补录影响当前判定，但不改变补录前的历史决策")
        return Outcome(appointment_id, OUTCOME_ARRIVED, "direct", tuple(reasons))
    if rec is not None and rec.checked_in_at is None and rec.completed_at is not None:
        return Outcome(appointment_id, OUTCOME_COMPLETED_NO_SCAN, "inferred",
                       ("无签到记录但存在完成就诊记录，按到诊处理并标注证据差异",))

    # 3) 尚未到就诊时间：不是失约，也不是风险事实。
    if as_of is not None and starts_at > as_of:
        return Outcome(appointment_id, OUTCOME_UNKNOWN, "none",
                       ("就诊尚未开始，未到不能判定为失约",),
                       Limitation("not_yet_due", "就诊时间未到，结果未知",
                                  (appointment_id,), "info"))

    # 4) 无到诊证据：只有经核对的缺席才是 no_show，其余一律 unknown。
    if rec is not None and rec.quality == QUALITY_RECONCILED_ABSENCE:
        return Outcome(appointment_id, OUTCOME_NO_SHOW, "direct",
                       ("无到诊证据且经人工核对确认为失约",))
    if rec is None:
        return Outcome(appointment_id, OUTCOME_UNKNOWN, "none",
                       ("没有任何出勤记录，不能推断为失约",),
                       Limitation("attendance_record_missing",
                                  "缺少出勤记录，未知不等于高风险",
                                  (appointment_id,), "warning"))
    if rec.quality == QUALITY_MISSING:
        return Outcome(appointment_id, OUTCOME_UNKNOWN, "none",
                       ("出勤记录明确缺失（无签到、无完成），按未知处理",),
                       Limitation("attendance_quality_missing",
                                  "记录质量标记为缺失，不能当作失约",
                                  (appointment_id,), "warning"))
    if rec.quality == QUALITY_OFFLINE:
        return Outcome(appointment_id, OUTCOME_UNKNOWN, "none",
                       ("签到设备离线导致无记录，不能自动等价为失约",),
                       Limitation("device_offline_unverified",
                                  "设备离线期间的出勤无法核实",
                                  (appointment_id,), "warning"))
    return Outcome(appointment_id, OUTCOME_UNKNOWN, "none",
                   (f"记录质量 {rec.quality} 证据不足，按未知处理",),
                   Limitation("insufficient_attendance_evidence",
                              "出勤证据不足，保持未知",
                              (appointment_id,), "warning"))


def _conflict_if_evidence(appointment_id: str, rec) -> Limitation | None:
    if rec is not None and (rec.checked_in_at is not None or rec.completed_at is not None):
        return Limitation(
            "status_attendance_conflict",
            "预约状态为取消/改期，但存在到诊或完成证据，需人工核对，状态判定保持不变",
            (appointment_id,), "warning",
        )
    return None
