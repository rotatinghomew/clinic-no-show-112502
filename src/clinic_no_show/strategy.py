"""提醒策略的后续周期对比。

- 周期以固定 7 天锚定（周一对齐），可复现；
- 提醒版本（template）与发送时刻通过时间线与预约当时状态对应，
  后补记录改变当前结果判断，但不改变当时发出的提醒事实；
- 未知结果单独计数，不并入失约，避免夸大策略差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .attendance import classify_outcome
from .models import OUTCOME_NO_SHOW, OUTCOME_UNKNOWN, Limitation
from .repository import Repository
from .time_utils import floor_to_cycle, to_iso

CYCLE_DAYS = 7


@dataclass
class CycleStrategyMetrics:
    cycle_start: datetime
    cycle_label: str
    template: str
    appointments: int = 0
    reminders_delivered: int = 0
    reminders_pending_or_failed: int = 0
    arrived: int = 0
    no_show: int = 0
    cancelled: int = 0
    rescheduled: int = 0
    unknown: int = 0
    no_show_rate: float | None = None
    arrival_rate: float | None = None
    delivery_coverage: float | None = None
    limitations: list[Limitation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_start": to_iso(self.cycle_start),
            "cycle_label": self.cycle_label,
            "template": self.template,
            "appointments": self.appointments,
            "reminders_delivered": self.reminders_delivered,
            "reminders_pending_or_failed": self.reminders_pending_or_failed,
            "arrived": self.arrived,
            "no_show": self.no_show,
            "cancelled": self.cancelled,
            "rescheduled": self.rescheduled,
            "unknown": self.unknown,
            "no_show_rate": self.no_show_rate,
            "arrival_rate": self.arrival_rate,
            "delivery_coverage": self.delivery_coverage,
            "limitations": [l.to_dict() for l in self.limitations],
        }


@dataclass
class StrategyComparison:
    cycles: list[CycleStrategyMetrics]
    deltas: list[dict[str, Any]]
    limitations: list[Limitation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_days": CYCLE_DAYS,
            "cycles": [c.to_dict() for c in self.cycles],
            "deltas": self.deltas,
            "limitations": [l.to_dict() for l in self.limitations],
        }


def compare_strategies(repo: Repository,
                       observation_as_of: datetime | None = None) -> StrategyComparison:
    """按提醒模板×周期统计到诊/失约。

    只统计就诊时间已到（``starts_at <= observation_as_of``）的预约；
    未来预约既不是失约也不是未知，不进入策略评估，避免把未发生的事计入对比。
    """
    metrics: dict[tuple[datetime, str], CycleStrategyMetrics] = {}
    limitations: list[Limitation] = []
    excluded_future: list[str] = []

    for appt in repo.appointments:
        if observation_as_of is not None and appt.starts_at > observation_as_of:
            excluded_future.append(appt.appointment_id)
            continue
        cycle_start = floor_to_cycle(appt.starts_at, CYCLE_DAYS)
        reminders = repo.reminders_for(appt.appointment_id)
        template = reminders[0].template if reminders else "none"
        key = (cycle_start, template)
        label = cycle_start.date().isoformat()
        m = metrics.setdefault(key, CycleStrategyMetrics(
            cycle_start=cycle_start, cycle_label=label, template=template))
        m.appointments += 1

        for rem in reminders:
            if rem.status == "delivered" and rem.sent_at is not None:
                # 提醒必须早于就诊开始，才是"预约当时状态下"的有效触达。
                if rem.sent_at <= appt.starts_at:
                    m.reminders_delivered += 1
                else:
                    m.reminders_pending_or_failed += 1
                    limitations.append(Limitation(
                        "reminder_sent_after_appointment",
                        f"提醒 {rem.reminder_id} 发送晚于就诊开始，不计为有效触达",
                        refs=(rem.reminder_id,), severity="warning",
                    ))
            else:
                m.reminders_pending_or_failed += 1

        outcome = classify_outcome(repo, appt.appointment_id)
        if outcome.outcome in ("arrived", "completed_no_scan"):
            m.arrived += 1
        elif outcome.outcome == OUTCOME_NO_SHOW:
            m.no_show += 1
        elif outcome.outcome == "cancelled":
            m.cancelled += 1
        elif outcome.outcome == "rescheduled":
            m.rescheduled += 1
        elif outcome.outcome == OUTCOME_UNKNOWN:
            m.unknown += 1
        if outcome.limitation is not None:
            m.limitations.append(outcome.limitation)

    cycles = sorted(metrics.values(), key=lambda c: (c.cycle_start, c.template))
    if excluded_future:
        limitations.append(Limitation(
            "future_appointments_excluded_from_strategy",
            f"{len(excluded_future)} 个就诊时间未到的预约未参与策略对比",
            refs=tuple(excluded_future), severity="info",
        ))
    for m in cycles:
        judged = m.no_show + m.arrived
        if judged:
            m.no_show_rate = m.no_show / judged
            m.arrival_rate = m.arrived / judged
        else:
            m.limitations.append(Limitation(
                "cycle_no_judged_outcomes",
                f"周期 {m.cycle_label} 模板 {m.template} 无可判定结果，率值留空",
                severity="warning",
            ))
        if m.appointments:
            m.delivery_coverage = m.reminders_delivered / m.appointments

    deltas = _build_deltas(cycles)
    return StrategyComparison(cycles=cycles, deltas=deltas, limitations=limitations)


def _build_deltas(cycles: list[CycleStrategyMetrics]) -> list[dict[str, Any]]:
    by_template: dict[str, list[CycleStrategyMetrics]] = {}
    for m in cycles:
        by_template.setdefault(m.template, []).append(m)

    deltas: list[dict[str, Any]] = []
    for template, series in by_template.items():
        series.sort(key=lambda c: c.cycle_start)
        for prev, curr in zip(series, series[1:]):
            deltas.append({
                "template": template,
                "from_cycle": prev.cycle_label,
                "to_cycle": curr.cycle_label,
                "no_show_rate_delta": _rate_delta(curr.no_show_rate, prev.no_show_rate),
                "arrival_rate_delta": _rate_delta(curr.arrival_rate, prev.arrival_rate),
                "delivery_coverage_delta": _rate_delta(
                    curr.delivery_coverage, prev.delivery_coverage),
            })
    return deltas


def _rate_delta(curr: float | None, prev: float | None) -> float | None:
    if curr is None or prev is None:
        return None
    return round(curr - prev, 4)
