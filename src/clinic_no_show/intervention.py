"""护理协调干预决策：分层风险 × 可解释差异 × 可联系时段 × 号源保护。

规则：
- 已承诺给复诊（followup）或急诊预留（urgent）的号源，任何风险排序都不能直接占用；
- ``indeterminate`` 只做观察（hold），不触发释放，不把未知当高风险；
- 患者可联系时段之外只能安排非即时动作，并标注原因；
- 每条建议都带事实依据与限制，负责人可逐条追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import (
    ACTION_HOLD,
    ACTION_MANUAL_CONFIRM,
    ACTION_RELEASE_SLOT,
    ACTION_REMINDER,
    RISK_HIGH,
    RISK_INDETERMINATE,
    RISK_MEDIUM,
    Limitation,
    Slot,
)
from .repository import Repository
from .risk import RiskAssessment, suggested_action
from .time_utils import parse_hhmm

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WINDOW = ("mon", "fri", "09:00", "18:00")  # 工作日白天
PROTECTED_RESERVATIONS = frozenset({"followup", "urgent"})


@dataclass
class InterventionPlan:
    appointment_id: str
    action: str
    risk_level: str
    within_contact_window: bool
    reasons: tuple[str, ...]
    protected_slot_ids: tuple[str, ...]
    limitations: list[Limitation] = field(default_factory=list)
    fact_refs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appointment_id": self.appointment_id,
            "action": self.action,
            "risk_level": self.risk_level,
            "within_contact_window": self.within_contact_window,
            "reasons": list(self.reasons),
            "protected_slot_ids": list(self.protected_slot_ids),
            "limitations": [l.to_dict() for l in self.limitations],
            "fact_refs": list(self.fact_refs),
        }


def can_contact_at(repo: Repository, patient_key: str | None,
                   moment: datetime) -> tuple[bool, list[Limitation]]:
    """判断某时刻是否落在患者可联系时段；无偏好资料时使用默认窗口并限制。"""
    limitations: list[Limitation] = []
    pref = repo.preference_for(patient_key)
    if pref is None:
        limitations.append(Limitation(
            "contact_preference_missing",
            "缺少患者可联系时段资料，使用默认窗口（工作日 09:00–18:00）",
            refs=(patient_key or "unknown",), severity="warning",
        ))
        if moment.weekday() >= 5:
            return False, limitations
        h, m = moment.hour, moment.minute
        return (9, 0) <= (h, m) < (18, 0), limitations

    day = WEEKDAY_NAMES[moment.weekday()]
    for window_day, start, end in pref.contact_windows:
        if window_day != day:
            continue
        sh, sm = parse_hhmm(start)
        eh, em = parse_hhmm(end)
        if (sh, sm) <= (moment.hour, moment.minute) < (eh, em):
            return True, limitations
    return False, limitations


def protected_slots_for(repo: Repository, site: str, starts_at: datetime) -> list[Slot]:
    """与预约同院区、同时段且承诺给复诊/急诊的保护号源。"""
    return [
        s for s in repo.slots
        if s.site == site and s.starts_at == starts_at
        and s.status == "protected"
        and s.reserved_for in PROTECTED_RESERVATIONS
    ]


def is_protected(slot: Slot) -> bool:
    return slot.status == "protected" and slot.reserved_for in PROTECTED_RESERVATIONS


def plan_intervention(repo: Repository, assessment: RiskAssessment,
                      decision_time: datetime) -> InterventionPlan:
    appt = repo.appointment(assessment.appointment_id)
    limitations: list[Limitation] = list(assessment.limitations)
    protected = protected_slots_for(repo, appt.site, appt.starts_at)
    protected_ids = tuple(s.slot_id for s in protected)
    contact_ok, contact_limits = can_contact_at(repo, appt.patient_key, decision_time)
    limitations.extend(contact_limits)

    fact_refs = (
        f"appointments.json#{appt.appointment_id}",
        f"attendance.json#{appt.appointment_id}",
    )

    reasons: list[str] = []
    action = suggested_action(assessment.risk_level)

    if assessment.risk_level == RISK_INDETERMINATE:
        action = ACTION_HOLD
        reasons.append("关键机会记录不足，风险不可判定：保持观察，不释放号源、不提高干预强度")
    elif assessment.risk_level == RISK_HIGH:
        action = ACTION_MANUAL_CONFIRM
        reasons.append(f"高分 {assessment.score:.1f} 且证据充分，优先人工确认")
    elif assessment.risk_level == RISK_MEDIUM:
        action = ACTION_REMINDER
        reasons.append("中分风险，先发送提醒并记录模板与发送时刻")
    else:
        reasons.append("低分风险，常规提醒即可")

    if not contact_ok and action in (ACTION_MANUAL_CONFIRM, ACTION_REMINDER):
        reasons.append("当前时刻不在患者可联系时段，动作顺延到下一可联系窗口")
        limitations.append(Limitation(
            "outside_contact_window",
            "决策时刻处于可联系时段之外，即时联系动作顺延",
            refs=(appt.appointment_id,), severity="info",
        ))

    # 号源释放需要额外条件：高风险 + 无保护承诺 + 临近就诊。
    if assessment.risk_level == RISK_HIGH and not protected:
        hours_to_appt = (appt.starts_at - decision_time).total_seconds() / 3600
        if 0 < hours_to_appt <= 24:
            action = ACTION_RELEASE_SLOT
            reasons.append("高风险且就诊前 24 小时内未确认，候选释放（仍需候补规则审核）")
    if protected:
        reasons.append(
            "同时段存在复诊/急诊预留保护号源 " + "、".join(protected_ids) + "，风险排序不得占用"
        )
        if action == ACTION_RELEASE_SLOT:
            action = ACTION_MANUAL_CONFIRM
            reasons.append("释放与保护承诺冲突，降级为人工确认")

    return InterventionPlan(
        appointment_id=appt.appointment_id,
        action=action,
        risk_level=assessment.risk_level,
        within_contact_window=contact_ok,
        reasons=tuple(reasons),
        protected_slot_ids=protected_ids,
        limitations=limitations,
        fact_refs=fact_refs,
    )
