"""可解释的爽约风险分层。

预测只使用决策时刻已可见的事实（由时间线保证），并遵守：
- 取消/改期/记录延迟不是同一种行为，分别计数，只有 ``no_show`` 进入失约率；
- 一次预约拆出的多个服务（service_group_id）按一次就诊归并；
- 缺少关键机会记录（历史样本不足、无偏好、记录缺口）时输出限制，
  分层为 ``indeterminate``，不把未知当高风险。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .attendance import classify_outcome_at
from .identity import IdentityResolution
from .models import (
    ACTION_HOLD,
    ACTION_MANUAL_CONFIRM,
    ACTION_REMINDER,
    LIMIT_RISK_SCORE,
    OUTCOME_NO_SHOW,
    OUTCOME_UNKNOWN,
    RISK_HIGH,
    RISK_INDETERMINATE,
    RISK_LOW,
    RISK_MEDIUM,
    Limitation,
)
from .repository import Repository
from .timeline import Timeline

MIN_HISTORY_SAMPLES = 3  # 少于该数量的已判定历史，风险结论不成立

# 可解释因子：(代码, 中文说明, 权重)
FACTOR_HISTORY_NO_SHOW = ("history_no_show_rate", "历史核对失约率", 45)
FACTOR_REMINDER_GAP = ("reminder_gap", "提醒覆盖/触达缺口", 20)
FACTOR_RECENT_CHANGE = ("recent_reschedule_cancel", "近期改期/取消波动", 15)
FACTOR_NEW_PATIENT = ("new_patient_uncertainty", "初诊且历史不足", 10)
FACTOR_SITE_CLINIC_BASE = ("site_clinic_segment_base", "院区/科室时段基线偏高", 10)

HIGH_THRESHOLD = 60.0
MEDIUM_THRESHOLD = 35.0


@dataclass(frozen=True)
class RiskFactor:
    code: str
    label: str
    contribution: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label,
                "contribution": round(self.contribution, 2), "detail": self.detail}


@dataclass
class PatientHistory:
    identity_group: str | None
    total_units: int = 0
    no_show: int = 0
    arrived: int = 0
    cancelled: int = 0
    rescheduled: int = 0
    unknown: int = 0
    no_show_rate: float | None = None
    limitations: list[Limitation] = field(default_factory=list)


@dataclass
class RiskAssessment:
    appointment_id: str
    identity_group: str | None
    score: float
    risk_level: str
    factors: list[RiskFactor]
    history: PatientHistory | None
    limitations: list[Limitation]
    based_on_visible_facts_as_of: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "appointment_id": self.appointment_id,
            "identity_group": self.identity_group,
            "score": round(self.score, 2),
            "risk_level": self.risk_level,
            "factors": [f.to_dict() for f in self.factors],
            "history": _history_to_dict(self.history),
            "limitations": [l.to_dict() for l in self.limitations],
            "based_on_visible_facts_as_of": self.based_on_visible_facts_as_of,
        }


def _history_to_dict(history: PatientHistory | None) -> dict[str, Any] | None:
    if history is None:
        return None
    return {
        "identity_group": history.identity_group,
        "total_units": history.total_units,
        "no_show": history.no_show,
        "arrived": history.arrived,
        "cancelled": history.cancelled,
        "rescheduled": history.rescheduled,
        "unknown": history.unknown,
        "no_show_rate": history.no_show_rate,
        "limitations": [l.to_dict() for l in history.limitations],
    }


def gather_history(repo: Repository, timeline: Timeline, identities: IdentityResolution,
                   identity_group: str | None, as_of: datetime) -> PatientHistory:
    """汇总身份组在 as_of 时可见、且就诊时间已过的历史（目标预约自身排除）。"""
    history = PatientHistory(identity_group=identity_group)
    if identity_group is None:
        history.limitations.append(Limitation(
            "no_identity_group", "预约无患者键，无法汇总患者级历史", severity="warning"))
        return history

    member_keys = identities.members.get(identity_group, frozenset({identity_group}))
    past = [
        a for a in repo.appointments
        if a.patient_key in member_keys and a.starts_at < as_of
    ]
    # 拆成多个服务的预约按 service_group_id 归并为一个就诊单元。
    units = _collapse_service_groups(past)

    for appt in sorted(units, key=lambda a: a.starts_at):
        outcome = classify_outcome_at(repo, timeline, appt.appointment_id, as_of)
        if outcome is None:  # as_of 时登记事实尚不可见
            continue
        history.total_units += 1
        if outcome.outcome == OUTCOME_NO_SHOW:
            history.no_show += 1
        elif outcome.outcome == "arrived" or outcome.outcome == "completed_no_scan":
            history.arrived += 1
        elif outcome.outcome == "cancelled":
            history.cancelled += 1
        elif outcome.outcome == "rescheduled":
            history.rescheduled += 1
        elif outcome.outcome == OUTCOME_UNKNOWN:
            history.unknown += 1
        if outcome.limitation is not None:
            history.limitations.append(outcome.limitation)

    judged = history.no_show + history.arrived
    if judged == 0:
        history.no_show_rate = None
    else:
        history.no_show_rate = history.no_show / judged

    if history.total_units < MIN_HISTORY_SAMPLES:
        history.limitations.append(Limitation(
            "insufficient_history",
            f"可判定历史仅 {history.total_units} 次（阈值 {MIN_HISTORY_SAMPLES}），"
            "风险结论证据不足，按不可判定处理",
            severity="warning",
        ))
    if history.unknown > 0:
        history.limitations.append(Limitation(
            "unknown_outcomes_excluded_from_rate",
            f"{history.unknown} 次历史结果未知（缺失/离线/补录不全），未计入失约率分母",
            severity="info",
        ))
    return history


def assess_risk(repo: Repository, timeline: Timeline, identities: IdentityResolution,
                appointment_id: str, as_of: datetime,
                segment_baselines: dict[tuple[str, str], float] | None = None) -> RiskAssessment:
    """对单个预约做 as-of 风险评估。"""
    appt = repo.appointment(appointment_id)
    limitations: list[Limitation] = []
    group = identities.group_for(appt.patient_key) if appt.patient_key else None
    history = gather_history(repo, timeline, identities, group, as_of)
    limitations.extend(history.limitations)

    factors: list[RiskFactor] = []
    score = 0.0

    # 因子 1：历史核对失约率（未知结果不进分母）
    if history.no_show_rate is not None and history.total_units >= MIN_HISTORY_SAMPLES:
        contribution = history.no_show_rate * FACTOR_HISTORY_NO_SHOW[2]
        score += contribution
        factors.append(RiskFactor(
            *FACTOR_HISTORY_NO_SHOW[:2], contribution,
            f"失约 {history.no_show}/已判定 {history.no_show + history.arrived}，"
            f"失约率 {history.no_show_rate:.0%}"))

    # 因子 2：提醒触达缺口（只统计 as_of 时已发送的提醒）
    sent = [f for f in timeline.visible_facts_for(appointment_id, as_of) if f.kind == "reminder_sent"]
    delivered = [f for f in sent if f.payload.get("status") == "delivered"]
    if not delivered:
        contribution = float(FACTOR_REMINDER_GAP[2])
        score += contribution
        detail = "尚无已送达提醒" if not sent else "有发送记录但未确认送达"
        factors.append(RiskFactor(*FACTOR_REMINDER_GAP[:2], contribution, detail))
    else:
        factors.append(RiskFactor(
            *FACTOR_REMINDER_GAP[:2], 0.0,
            f"已送达提醒 {len(delivered)} 次（模板 {sent[0].payload['template']}）"))

    # 因子 3：近期改期/取消波动（身份组内、as_of 前 30 天可见）
    recent_changes = _recent_status_changes(repo, identities, group, as_of)
    if recent_changes > 0:
        contribution = min(FACTOR_RECENT_CHANGE[2], recent_changes * 7.5)
        score += contribution
        factors.append(RiskFactor(
            *FACTOR_RECENT_CHANGE[:2], contribution,
            f"近 30 天改期/取消 {recent_changes} 次（与失约分开计数）"))

    # 因子 4：初诊且历史不足
    if appt.type == "new" and history.total_units < MIN_HISTORY_SAMPLES:
        contribution = float(FACTOR_NEW_PATIENT[2])
        score += contribution
        factors.append(RiskFactor(
            *FACTOR_NEW_PATIENT[:2], contribution, "初诊预约且缺少可判定历史"))

    # 因子 5：院区/科室/时段基线（由资料计算或外部传入，缺失则限制）
    baseline = (segment_baselines or {}).get((appt.site, appt.clinic))
    if baseline is not None:
        contribution = baseline * FACTOR_SITE_CLINIC_BASE[2]
        score += contribution
        factors.append(RiskFactor(
            *FACTOR_SITE_CLINIC_BASE[:2], contribution,
            f"{appt.site}/{appt.clinic} 基线失约率 {baseline:.0%}"))
    else:
        limitations.append(Limitation(
            "segment_baseline_unavailable",
            f"院区 {appt.site} 科室 {appt.clinic} 缺少基线，未使用时段基线因子",
            refs=(appointment_id,), severity="info"))

    score = min(LIMIT_RISK_SCORE, score)
    indeterminate = any(
        l.code in {"insufficient_history", "no_identity_group"} for l in limitations
    )
    if indeterminate:
        level = RISK_INDETERMINATE
    elif score >= HIGH_THRESHOLD:
        level = RISK_HIGH
    elif score >= MEDIUM_THRESHOLD:
        level = RISK_MEDIUM
    else:
        level = RISK_LOW

    return RiskAssessment(
        appointment_id=appointment_id,
        identity_group=group,
        score=score,
        risk_level=level,
        factors=factors,
        history=history,
        limitations=limitations,
        based_on_visible_facts_as_of=as_of.isoformat(),
    )


def _collapse_service_groups(appts: list) -> list:
    """同一 service_group_id 只保留一个代表单元（按最早开始时间）。"""
    chosen: dict[str, object] = {}
    singles: list = []
    for appt in appts:
        if appt.service_group_id is None:
            singles.append(appt)
            continue
        current = chosen.get(appt.service_group_id)
        if current is None or appt.starts_at < current.starts_at:
            chosen[appt.service_group_id] = appt
    return singles + list(chosen.values())


def _recent_status_changes(repo: Repository, identities: IdentityResolution,
                           group: str | None, as_of: datetime) -> int:
    if group is None:
        return 0
    member_keys = identities.members.get(group, frozenset({group}))
    cutoff = as_of.replace(microsecond=0)
    from datetime import timedelta

    window_start = cutoff - timedelta(days=30)
    count = 0
    for appt in repo.appointments:
        if appt.patient_key not in member_keys:
            continue
        if appt.status in ("rescheduled", "cancelled") and appt.changed_at is not None:
            if window_start <= appt.changed_at <= as_of:
                count += 1
    return count


def suggested_action(level: str) -> str:
    """分层到默认干预动作的映射（号源释放还要经过保护规则，见 intervention）。"""
    if level == RISK_HIGH:
        return ACTION_MANUAL_CONFIRM
    if level == RISK_MEDIUM:
        return ACTION_REMINDER
    if level == RISK_INDETERMINATE:
        return ACTION_HOLD
    return ACTION_REMINDER
