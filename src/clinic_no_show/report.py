"""负责人视角的综合报告：风险建议 + 事实依据 + 适用范围 + 保护承诺 + 实际结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .attendance import classify_outcome
from .identity import resolve_identities
from .intervention import plan_intervention
from .models import Limitation
from .repository import Repository, build_repository
from .risk import assess_risk
from .scheduling import SchedulingResult, list_protected_slots, run_scheduling
from .segments import risk_population_summary, segment_population
from .strategy import compare_strategies
from .timeline import build_timeline


@dataclass
class NoShowReport:
    as_of: str
    dataset_limitations: list[Limitation]
    identity_limitations: list[Limitation]
    timeline_limitations: list[Limitation]
    risk_assessments: list[dict[str, Any]]
    intervention_plans: list[dict[str, Any]]
    appointment_facts: list[dict[str, Any]]
    protected_commitments: list[dict[str, Any]]
    scheduling: dict[str, Any]
    segments: list[dict[str, Any]]
    risk_population: dict[str, int]
    strategy_comparison: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "limitations": {
                "dataset": [l.to_dict() for l in self.dataset_limitations],
                "identity": [l.to_dict() for l in self.identity_limitations],
                "timeline": [l.to_dict() for l in self.timeline_limitations],
            },
            "risk_assessments": self.risk_assessments,
            "intervention_plans": self.intervention_plans,
            "appointment_facts": self.appointment_facts,
            "protected_commitments": self.protected_commitments,
            "scheduling": self.scheduling,
            "segments": self.segments,
            "risk_population": self.risk_population,
            "strategy_comparison": self.strategy_comparison,
        }


def build_report(root: str, as_of: Any = None,
                 scheduling_result: SchedulingResult | None = None) -> NoShowReport:
    repo = build_repository(root)
    return build_report_from_repo(repo, as_of=as_of, scheduling_result=scheduling_result)


def build_report_from_repo(repo: Repository, as_of: Any = None,
                           scheduling_result: SchedulingResult | None = None) -> NoShowReport:
    identities = resolve_identities(repo)
    timeline = build_timeline(repo)
    latest = timeline.latest_visible_time()
    if as_of is None:
        if latest is None:
            raise ValueError("资料中没有任何事实，无法确定决策时刻")
        as_of = latest + timedelta(seconds=1)

    assessments: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []

    for appt in repo.appointments:
        # 风险与干预只针对仍有效（scheduled）的预约；
        # 改期/取消的旧预约只进入事实档案，不占用干预资源。
        if appt.status == "scheduled":
            assessment = assess_risk(repo, timeline, identities, appt.appointment_id, as_of)
            plan = plan_intervention(repo, assessment, as_of)
            assessments.append(assessment.to_dict())
            plans.append(plan.to_dict())

        outcome = classify_outcome(repo, appt.appointment_id)
        reminders = [
            f.to_dict() for f in timeline.facts_for(appt.appointment_id)
            if f.kind == "reminder_sent"
        ]
        facts.append({
            "appointment_id": appt.appointment_id,
            "patient_key": appt.patient_key,
            "identity_group": identities.group_for(appt.patient_key) if appt.patient_key else None,
            "clinic": appt.clinic,
            "site": appt.site,
            "starts_at": appt.starts_at.isoformat(),
            "type": appt.type,
            "status": appt.status,
            "service_group_id": appt.service_group_id,
            "registered_by": appt.registered_by,
            "outcome": outcome.to_dict(),
            "reminders_sent": reminders,
            "visible_fact_count": len(timeline.visible_facts_for(appt.appointment_id, as_of)),
            "total_fact_count": len(timeline.facts_for(appt.appointment_id)),
        })

    protected = [
        {"slot_id": s.slot_id, "site": s.site,
         "starts_at": s.starts_at.isoformat(),
         "reserved_for": s.reserved_for, "status": s.status}
        for s in list_protected_slots(repo)
    ]

    if scheduling_result is None:
        scheduling_result = run_scheduling(repo, as_of)

    seg_stats = segment_population(repo, timeline, identities, as_of)
    comparison = compare_strategies(repo, observation_as_of=as_of)

    return NoShowReport(
        as_of=as_of.isoformat(),
        dataset_limitations=list(repo.limitations),
        identity_limitations=list(identities.limitations),
        timeline_limitations=list(timeline.limitations),
        risk_assessments=assessments,
        intervention_plans=plans,
        appointment_facts=facts,
        protected_commitments=protected,
        scheduling=scheduling_result.to_dict(),
        segments=[s.to_dict() for s in seg_stats],
        risk_population=risk_population_summary(assessments),
        strategy_comparison=comparison.to_dict(),
    )
