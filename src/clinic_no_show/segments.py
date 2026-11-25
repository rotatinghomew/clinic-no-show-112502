"""人群与时段画像：回答"哪些人群、哪些时段存在可干预风险"。

所有口径都区分失约、取消、改期与未知，避免把后三者简单累加成"未到次数"。
样本不足的分层给出限制，不输出排名假象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .attendance import classify_outcome_at
from .identity import IdentityResolution
from .models import OUTCOME_NO_SHOW, OUTCOME_UNKNOWN, Limitation
from .repository import Repository
from .timeline import Timeline


@dataclass
class SegmentStat:
    site: str
    clinic: str
    hour_bucket: str
    appointment_count: int
    no_show: int
    arrived: int
    cancelled: int
    rescheduled: int
    unknown: int
    no_show_rate: float | None
    limitations: list[Limitation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "clinic": self.clinic,
            "hour_bucket": self.hour_bucket,
            "appointment_count": self.appointment_count,
            "no_show": self.no_show,
            "arrived": self.arrived,
            "cancelled": self.cancelled,
            "rescheduled": self.rescheduled,
            "unknown": self.unknown,
            "no_show_rate": self.no_show_rate,
            "limitations": [l.to_dict() for l in self.limitations],
        }


def hour_bucket(starts_at: datetime) -> str:
    """两小时一桶，如 09:00 -> '09-11'。"""
    start_hour = starts_at.hour - (starts_at.hour % 2)
    return f"{start_hour:02d}-{start_hour + 2:02d}"


def segment_population(repo: Repository, timeline: Timeline,
                       identities: IdentityResolution, as_of: datetime) -> list[SegmentStat]:
    buckets: dict[tuple[str, str, str], SegmentStat] = {}

    for appt in repo.appointments:
        key = (appt.site, appt.clinic, hour_bucket(appt.starts_at))
        stat = buckets.setdefault(key, SegmentStat(
            site=appt.site, clinic=appt.clinic, hour_bucket=key[2],
            appointment_count=0, no_show=0, arrived=0, cancelled=0,
            rescheduled=0, unknown=0, no_show_rate=None,
        ))
        outcome = classify_outcome_at(repo, timeline, appt.appointment_id, as_of)
        if outcome is None:
            stat.limitations.append(Limitation(
                "fact_not_visible_as_of",
                f"{appt.appointment_id} 在 as-of 时不可见，未纳入画像",
                refs=(appt.appointment_id,), severity="info",
            ))
            continue
        stat.appointment_count += 1
        if outcome.outcome == OUTCOME_NO_SHOW:
            stat.no_show += 1
        elif outcome.outcome in ("arrived", "completed_no_scan"):
            stat.arrived += 1
        elif outcome.outcome == "cancelled":
            stat.cancelled += 1
        elif outcome.outcome == "rescheduled":
            stat.rescheduled += 1
        elif outcome.outcome == OUTCOME_UNKNOWN:
            stat.unknown += 1
        if outcome.limitation is not None:
            stat.limitations.append(outcome.limitation)

    for stat in buckets.values():
        judged = stat.no_show + stat.arrived
        if judged:
            stat.no_show_rate = stat.no_show / judged
        else:
            stat.limitations.append(Limitation(
                "segment_no_judged_outcomes",
                f"{stat.site}/{stat.clinic}/{stat.hour_bucket} 没有可判定结果，不参与排名",
                severity="warning",
            ))
        if stat.unknown:
            stat.limitations.append(Limitation(
                "segment_unknown_outcomes",
                f"{stat.unknown} 条结果未知，未计入失约率分母",
                severity="info",
            ))

    return sorted(
        buckets.values(),
        key=lambda s: (-(s.no_show_rate or -1.0), -s.appointment_count, s.site, s.clinic),
    )


def risk_population_summary(assessments: list[dict[str, Any]]) -> dict[str, int]:
    """按风险层统计人群规模（indeterminate 单独成层，不并入高风险）。"""
    summary = {"high": 0, "medium": 0, "low": 0, "indeterminate": 0}
    for item in assessments:
        summary[item["risk_level"]] = summary.get(item["risk_level"], 0) + 1
    return summary
