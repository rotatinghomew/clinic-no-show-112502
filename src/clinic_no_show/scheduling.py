"""候补号源调度与重排。

业务约束：
- 承诺给复诊/急诊的保护号源（protected + followup/urgent）永不自动释放或占用；
- 候补匹配按院区+科室、申请窗口就近、申请创建时间 FIFO；
- 取消或补录到达后，**尚未确认**的候补安排可以重排；
  **已通知患者**的安排冻结（frozen），只能追加原因与后续处理，不可静默改写；
- 安排台账只追加（append-only），每次重排保留旧记录，已发生的号源转移不被抹掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .intervention import is_protected
from .models import (
    SLOT_OPEN,
    SLOT_RELEASED,
    WAITLIST_PENDING,
    Limitation,
    Slot,
    WaitlistRequest,
)
from .repository import Repository
from .time_utils import to_iso

ARRANGEMENT_OFFERED = "offered"
ARRANGEMENT_CONFIRMED = "confirmed"
ARRANGEMENT_FROZEN = "frozen"
ARRANGEMENT_SUPERSEDED = "superseded"
ARRANGEMENT_CANCELLED = "cancelled"

WINDOW_TOLERANCE = timedelta(hours=2)


@dataclass(frozen=True)
class SlotArrangement:
    arrangement_id: str
    request_id: str
    slot_id: str
    clinic: str
    site: str
    status: str
    reason: str
    created_at: datetime
    notified_at: datetime | None
    supersedes: str | None
    follow_up: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "arrangement_id": self.arrangement_id,
            "request_id": self.request_id,
            "slot_id": self.slot_id,
            "clinic": self.clinic,
            "site": self.site,
            "status": self.status,
            "reason": self.reason,
            "created_at": to_iso(self.created_at),
            "notified_at": to_iso(self.notified_at),
            "supersedes": self.supersedes,
            "follow_up": self.follow_up,
        }


@dataclass
class SchedulingResult:
    arrangements: list[SlotArrangement]
    protected_slot_ids: tuple[str, ...]
    limitations: list[Limitation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "arrangements": [a.to_dict() for a in self.arrangements],
            "protected_slot_ids": list(self.protected_slot_ids),
            "limitations": [l.to_dict() for l in self.limitations],
        }


def freeable_slots(repo: Repository, as_of: datetime) -> list[Slot]:
    """当前可释放号源：open/released 且未承诺保护；protected 永不自动释放。"""
    return [
        s for s in repo.slots
        if s.starts_at > as_of
        and s.status in (SLOT_OPEN, SLOT_RELEASED)
        and not is_protected(s)
    ]


def list_protected_slots(repo: Repository) -> list[Slot]:
    return [s for s in repo.slots if is_protected(s)]


def run_scheduling(repo: Repository, as_of: datetime,
                   existing: list[SlotArrangement] | None = None,
                   extra_reason: str = "") -> SchedulingResult:
    """生成/重排候补安排。existing 为既有台账（只读），重排只追加新记录。"""
    limitations: list[Limitation] = []
    arrangements: list[SlotArrangement] = list(existing or [])
    seq = len({a.arrangement_id for a in arrangements})
    protected = list_protected_slots(repo)
    protected_ids = tuple(s.slot_id for s in protected)

    # 已通知/已确认的安排占用对应号源，后续匹配不得重复分配。
    occupied_slots = {
        a.slot_id for a in arrangements
        if a.status in (ARRANGEMENT_OFFERED, ARRANGEMENT_CONFIRMED, ARRANGEMENT_FROZEN)
    }

    # 重排时：仍未确认的旧安排所覆盖的候补请求需要重新参与匹配。
    active_requests = {a.request_id for a in arrangements
                       if a.status in (ARRANGEMENT_OFFERED, ARRANGEMENT_FROZEN,
                                       ARRANGEMENT_CONFIRMED)}
    pending = [
        w for w in repo.waitlist
        if w.status == WAITLIST_PENDING and w.requested_window >= as_of
        and w.request_id not in active_requests
    ]
    pending.sort(key=lambda w: (w.created_at or w.requested_window, w.request_id))

    candidates = [s for s in freeable_slots(repo, as_of) if s.slot_id not in occupied_slots]

    for request in pending:
        slot = _best_slot(request, candidates)
        if slot is None:
            limitations.append(Limitation(
                "no_freeable_slot",
                f"候补 {request.request_id} 暂无可匹配号源（保护号源不参与自动释放）",
                refs=(request.request_id,), severity="info",
            ))
            continue
        seq += 1
        reason = f"按 FIFO 匹配 {slot.site} 院区就近窗口 {slot.starts_at.isoformat()}"
        if extra_reason:
            reason += f"；{extra_reason}"
        arrangements.append(SlotArrangement(
            arrangement_id=f"ar-{seq:04d}",
            request_id=request.request_id,
            slot_id=slot.slot_id,
            clinic=request.clinic,
            site=request.site,
            status=ARRANGEMENT_OFFERED,
            reason=reason,
            created_at=as_of,
            notified_at=None,
            supersedes=None,
            follow_up="等待患者确认；确认前若有取消/补录可重排",
        ))
        candidates.remove(slot)
        occupied_slots.add(slot.slot_id)

    return SchedulingResult(
        arrangements=arrangements,
        protected_slot_ids=protected_ids,
        limitations=limitations,
    )


def mark_notified(arrangements: list[SlotArrangement], arrangement_id: str,
                  notified_at: datetime, follow_up: str) -> list[SlotArrangement]:
    """通知患者后冻结安排：返回新台账，原记录保留、状态转为 frozen。"""
    updated = list(arrangements)
    for index, arr in enumerate(updated):
        if arr.arrangement_id == arrangement_id:
            updated[index] = SlotArrangement(
                arrangement_id=arr.arrangement_id,
                request_id=arr.request_id,
                slot_id=arr.slot_id,
                clinic=arr.clinic,
                site=arr.site,
                status=ARRANGEMENT_FROZEN,
                reason=arr.reason + "；已通知患者，安排冻结",
                created_at=arr.created_at,
                notified_at=notified_at,
                supersedes=arr.supersedes,
                follow_up=follow_up,
            )
            return updated
    raise KeyError(arrangement_id)


def replan_after_event(repo: Repository, as_of: datetime,
                       existing: list[SlotArrangement],
                       event_reason: str) -> SchedulingResult:
    """取消/补录到达后的重排。

    - 未确认且未通知（offered）的安排作废为 superseded，其候补请求重新参与匹配；
    - 已通知（frozen）/已确认安排保留并占用原号源，follow_up 记录原因与后续处理；
    - 保护号源仍不参与；台账只追加，旧记录保留，新安排通过 supersedes 指向旧安排。
    """
    ledger: list[SlotArrangement] = []
    superseded_by_request: dict[str, str] = {}
    for arr in existing:
        if arr.status in (ARRANGEMENT_FROZEN, ARRANGEMENT_CONFIRMED):
            ledger.append(SlotArrangement(
                arrangement_id=arr.arrangement_id,
                request_id=arr.request_id,
                slot_id=arr.slot_id,
                clinic=arr.clinic,
                site=arr.site,
                status=arr.status,
                reason=arr.reason,
                created_at=arr.created_at,
                notified_at=arr.notified_at,
                supersedes=arr.supersedes,
                follow_up=(arr.follow_up or "") + f"；事件后保留：{event_reason}",
            ))
        elif arr.status == ARRANGEMENT_OFFERED:
            superseded_by_request[arr.request_id] = arr.arrangement_id
            ledger.append(SlotArrangement(
                arrangement_id=arr.arrangement_id,
                request_id=arr.request_id,
                slot_id=arr.slot_id,
                clinic=arr.clinic,
                site=arr.site,
                status=ARRANGEMENT_SUPERSEDED,
                reason=arr.reason + f"；重排作废：{event_reason}",
                created_at=arr.created_at,
                notified_at=arr.notified_at,
                supersedes=arr.supersedes,
                follow_up="号源释放回候补池重新匹配",
            ))
        else:
            ledger.append(arr)  # superseded/cancelled 历史记录原样保留

    result = run_scheduling(repo, as_of, existing=ledger,
                            extra_reason=f"事件后重排：{event_reason}")

    # 新安排显式取代同请求的旧安排（如果仍能匹配到号源）。
    for idx, arr in enumerate(result.arrangements):
        old_id = superseded_by_request.get(arr.request_id)
        if arr.status == ARRANGEMENT_OFFERED and old_id is not None:
            result.arrangements[idx] = SlotArrangement(
                arrangement_id=arr.arrangement_id,
                request_id=arr.request_id,
                slot_id=arr.slot_id,
                clinic=arr.clinic,
                site=arr.site,
                status=arr.status,
                reason=arr.reason + f"；取代 {old_id}",
                created_at=arr.created_at,
                notified_at=arr.notified_at,
                supersedes=old_id,
                follow_up=arr.follow_up,
            )
    return result


def _best_slot(request: WaitlistRequest, candidates: list[Slot]) -> Slot | None:
    matching = [
        s for s in candidates
        if s.site == request.site
        and (s.clinic is None or s.clinic == request.clinic)
        and abs(s.starts_at - request.requested_window) <= WINDOW_TOLERANCE
    ]
    if not matching:
        return None
    return min(matching, key=lambda s: (abs(s.starts_at - request.requested_window), s.slot_id))
