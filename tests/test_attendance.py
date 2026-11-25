"""出勤判定：取消/改期/离线/补录/无签到/核对失约的显式区分。"""

import unittest

from helpers import build_context, ts
from clinic_no_show.attendance import classify_outcome, classify_outcome_at
from clinic_no_show.models import (
    OUTCOME_ARRIVED,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED_NO_SCAN,
    OUTCOME_NO_SHOW,
    OUTCOME_RESCHEDULED,
    OUTCOME_UNKNOWN,
)


class AttendanceTest(unittest.TestCase):
    def setUp(self):
        self.repo, _, self.timeline = build_context()

    def test_reconciled_absence_is_no_show(self):
        outcome = classify_outcome(self.repo, "ap-103")
        self.assertEqual(outcome.outcome, OUTCOME_NO_SHOW)
        self.assertEqual(outcome.evidence_level, "direct")

    def test_late_entry_is_arrival_not_no_show(self):
        outcome = classify_outcome(self.repo, "ap-104a")
        self.assertEqual(outcome.outcome, OUTCOME_ARRIVED)
        self.assertTrue(any("迟到补录" in r for r in outcome.reasons))

    def test_completed_without_scan_is_separate_arrival(self):
        outcome = classify_outcome(self.repo, "ap-104b")
        self.assertEqual(outcome.outcome, OUTCOME_COMPLETED_NO_SCAN)
        self.assertEqual(outcome.evidence_level, "inferred")

    def test_cancel_and_reschedule_are_not_no_show(self):
        self.assertEqual(classify_outcome(self.repo, "ap-106").outcome, OUTCOME_CANCELLED)
        self.assertEqual(classify_outcome(self.repo, "ap-107").outcome, OUTCOME_RESCHEDULED)

    def test_offline_device_is_unknown_with_limitation(self):
        outcome = classify_outcome(self.repo, "ap-108")
        self.assertEqual(outcome.outcome, OUTCOME_UNKNOWN)
        self.assertIsNotNone(outcome.limitation)
        self.assertEqual(outcome.limitation.code, "device_offline_unverified")

    def test_missing_attendance_record_is_unknown(self):
        # ap-201 是未来预约且没有出勤记录，未到就诊时间时返回未知而非失约。
        outcome = classify_outcome_at(
            self.repo, self.timeline, "ap-201", ts("2026-10-11T08:00"))
        self.assertEqual(outcome.outcome, OUTCOME_UNKNOWN)
        self.assertEqual(outcome.limitation.code, "not_yet_due")

    def test_future_fact_not_visible_returns_none(self):
        # ap-201 2026-10-09 才登记；更早的决策时刻看不到该预约。
        self.assertIsNone(
            classify_outcome_at(self.repo, self.timeline, "ap-201",
                                ts("2026-10-08T08:00")))

    def test_backfill_changes_current_but_not_historical_judgment(self):
        # ap-103 的核对缺席 10-08 才补录：补录前决策为未知，补录后变为失约；
        # 已发出的提醒事实（rm-203）仍然存在，不被补录抹掉。
        before = classify_outcome_at(
            self.repo, self.timeline, "ap-103", ts("2026-10-07T12:00"))
        self.assertEqual(before.outcome, OUTCOME_UNKNOWN)
        after = classify_outcome_at(
            self.repo, self.timeline, "ap-103", ts("2026-10-08T12:00"))
        self.assertEqual(after.outcome, OUTCOME_NO_SHOW)
        reminder_facts = [f for f in self.timeline.facts_for("ap-103")
                          if f.kind == "reminder_sent"]
        self.assertEqual(len(reminder_facts), 1)
        self.assertEqual(reminder_facts[0].payload["reminder_id"], "rm-203")


if __name__ == "__main__":
    unittest.main()
