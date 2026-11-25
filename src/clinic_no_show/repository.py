"""资料仓储：把原始 JSON 结构化为领域模型，保留 raw 与缺失语义。

可选文件（身份关联、联系偏好）缺失时不报错，而是产出数据集级限制，
让下游显式知道"没有授权关联数据，不能跨患者键合并"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .input_contract import load_bundle
from .models import (
    Appointment,
    AttendanceRecord,
    ContactPreference,
    IdentityLink,
    Limitation,
    Reminder,
    Slot,
    WaitlistRequest,
)
from .time_utils import parse_hhmm, parse_ts

OPTIONAL_FILES = ("identity_links.json", "contact_preferences.json")


@dataclass
class Repository:
    appointments: list[Appointment] = field(default_factory=list)
    attendance: list[AttendanceRecord] = field(default_factory=list)
    reminders: list[Reminder] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    waitlist: list[WaitlistRequest] = field(default_factory=list)
    identity_links: list[IdentityLink] = field(default_factory=list)
    contact_preferences: list[ContactPreference] = field(default_factory=list)
    limitations: list[Limitation] = field(default_factory=list)
    raw_bundle: dict[str, Any] = field(default_factory=dict)

    # ---- 便捷索引 ----
    def appointment(self, appointment_id: str) -> Appointment | None:
        return next((a for a in self.appointments if a.appointment_id == appointment_id), None)

    def attendance_for(self, appointment_id: str) -> AttendanceRecord | None:
        return next((r for r in self.attendance if r.appointment_id == appointment_id), None)

    def reminders_for(self, appointment_id: str) -> list[Reminder]:
        return [r for r in self.reminders if r.appointment_id == appointment_id]

    def preference_for(self, patient_key: str | None) -> ContactPreference | None:
        if patient_key is None:
            return None
        return next((p for p in self.contact_preferences if p.patient_key == patient_key), None)


def build_repository(root: str) -> Repository:
    """从资料包目录构建仓储；执行输入契约并做字段级解析。"""
    bundle = load_bundle(root)
    return repository_from_bundle(bundle)


def repository_from_bundle(bundle: dict[str, Any]) -> Repository:
    repo = Repository(raw_bundle=bundle)

    for row in bundle.get("appointments.json", []):
        repo.appointments.append(Appointment(
            appointment_id=row["appointment_id"],
            patient_key=row.get("patient_key"),
            clinic=row["clinic"],
            site=row["site"],
            starts_at=parse_ts(row["starts_at"]),
            type=row["type"],
            status=row["status"],
            registered_by=row.get("registered_by"),
            service_group_id=row.get("service_group_id"),
            changed_at=parse_ts(row.get("changed_at")),
            raw=row,
        ))

    for row in bundle.get("attendance.json", []):
        repo.attendance.append(AttendanceRecord(
            appointment_id=row["appointment_id"],
            checked_in_at=parse_ts(row.get("checked_in_at")),
            completed_at=parse_ts(row.get("completed_at")),
            record_source=row.get("record_source", ""),
            quality=row.get("quality", ""),
            recorded_at=parse_ts(row.get("recorded_at")),
            raw=row,
        ))

    for row in bundle.get("reminders.json", []):
        repo.reminders.append(Reminder(
            reminder_id=row["reminder_id"],
            appointment_id=row["appointment_id"],
            sent_at=parse_ts(row.get("sent_at")),
            template=row.get("template", ""),
            status=row.get("status", ""),
            raw=row,
        ))

    for row in bundle.get("slots.json", []):
        repo.slots.append(Slot(
            slot_id=row["slot_id"],
            site=row["site"],
            starts_at=parse_ts(row["starts_at"]),
            reserved_for=row.get("reserved_for"),
            status=row.get("status", ""),
            clinic=row.get("clinic"),
            raw=row,
        ))

    for row in bundle.get("waitlist.json", []):
        repo.waitlist.append(WaitlistRequest(
            request_id=row["request_id"],
            clinic=row["clinic"],
            site=row["site"],
            requested_window=parse_ts(row["requested_window"]),
            status=row.get("status", ""),
            patient_key=row.get("patient_key"),
            created_at=parse_ts(row.get("created_at")),
            raw=row,
        ))

    for row in bundle.get("identity_links.json", []):
        repo.identity_links.append(IdentityLink(
            patient_key=row["patient_key"],
            linked_key=row["linked_key"],
            relation=row.get("relation", ""),
            authorized=bool(row.get("authorized", False)),
            raw=row,
        ))

    for row in bundle.get("contact_preferences.json", []):
        windows = tuple(
            (str(w["day"]), str(w["from"]), str(w["to"])) for w in row.get("windows", [])
        )
        # 校验窗口格式，格式错误直接报错，不静默忽略。
        for _, start, end in windows:
            parse_hhmm(start)
            parse_hhmm(end)
        repo.contact_preferences.append(ContactPreference(
            patient_key=row["patient_key"],
            contact_windows=windows,
            channels=tuple(row.get("channels", [])),
            raw=row,
        ))

    _dataset_limitations(repo, bundle)
    return repo


def _dataset_limitations(repo: Repository, bundle: dict[str, Any]) -> None:
    for name in OPTIONAL_FILES:
        if name not in bundle:
            code = "missing_identity_links" if name == "identity_links.json" else "missing_contact_preferences"
            message = (
                "缺少授权关联资料，身份只能按患者键处理，不能跨键合并"
                if name == "identity_links.json"
                else "缺少患者可联系时段资料，干预建议使用默认窗口并标注限制"
            )
            repo.limitations.append(Limitation(code=code, message=message, severity="warning"))

    unattributed = [a.appointment_id for a in repo.appointments if a.patient_key is None]
    if unattributed:
        repo.limitations.append(Limitation(
            code="unattributed_appointment",
            message="存在无患者键的预约，无法纳入患者级历史与身份合并，仅做预约级评估",
            refs=tuple(unattributed),
            severity="warning",
        ))
