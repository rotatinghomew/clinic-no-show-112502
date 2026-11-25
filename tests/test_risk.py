"""风险分层：证据不足不可判定、不把未知当高风险；拆分服务归并一次。"""

import unittest

from helpers import build_context, ts
from clinic_no_show.models import (
    RISK_HIGH,
    RISK_INDETERMINATE,
    RISK_LOW,
    RISK_MEDIUM,
)
from clinic_no_show.risk import (
    MIN_HISTORY_SAMPLES,
    assess_risk,
    gather_history,
)


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.identities, self.timeline = build_context()
        self.as_of = ts("2026-10-11T12:00")

    def test_history_separates_no_show_cancel_reschedule_unknown(self):
        group = self.identities.group_for("p-3")
        history = gather_history(self.repo, self.timeline, self.identities,
                                 group, self.as_of)
        self.assertEqual(history.cancelled, 1)
        self.assertEqual(history.rescheduled, 1)
        self.assertEqual(history.no_show, 0)
        self.assertIsNone(history.no_show_rate)
        self.assertTrue(any(l.code == "insufficient_history" for l in history.limitations))

    def test_service_group_collapsed_to_one_unit(self):
        group = self.identities.group_for("p-2")
        history = gather_history(self.repo, self.timeline, self.identities,
                                 group, self.as_of)
        # ap-104a/ap-104b 是同一次预约的两个服务，只能算 1 个单元。
        self.assertEqual(history.total_units, 2)
        self.assertEqual(history.arrived, 1)
        self.assertEqual(history.no_show, 1)

    def test_unknown_outcome_not_counted_as_no_show(self):
        group = self.identities.group_for("p-4")
        history = gather_history(self.repo, self.timeline, self.identities,
                                 group, self.as_of)
        self.assertEqual(history.unknown, 1)
        self.assertEqual(history.no_show, 0)
        self.assertTrue(any(l.code == "unknown_outcomes_excluded_from_rate"
                            for l in history.limitations))

    def test_high_risk_patient_with_sufficient_evidence(self):
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-202", self.as_of)
        # p-6 有 3 次核对失约，样本达到阈值，失约率 100% -> 高分。
        self.assertEqual(assessment.risk_level, RISK_HIGH)
        self.assertGreaterEqual(assessment.score, 60.0)
        codes = {f.code for f in assessment.factors}
        self.assertIn("history_no_show_rate", codes)
        self.assertIn("reminder_gap", codes)  # rm-102 仍 pending

    def test_insufficient_history_is_indeterminate_not_high(self):
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-204", self.as_of)
        self.assertEqual(assessment.risk_level, RISK_INDETERMINATE)
        self.assertTrue(any(l.code == "insufficient_history"
                            for l in assessment.limitations))

    def test_unattributed_appointment_is_indeterminate(self):
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-203", self.as_of)
        self.assertIsNone(assessment.identity_group)
        self.assertEqual(assessment.risk_level, RISK_INDETERMINATE)

    def test_as_of_ignores_future_facts(self):
        # 在 p-6 第一次失约记录补录之前，历史几乎为空 -> 不可判定。
        early = ts("2026-09-30T12:00")
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-202", early)
        self.assertEqual(assessment.risk_level, RISK_INDETERMINATE)

    def test_minimum_samples_constant(self):
        self.assertGreaterEqual(MIN_HISTORY_SAMPLES, 3)

    def test_risk_levels_are_distinct(self):
        self.assertEqual(len({RISK_HIGH, RISK_MEDIUM, RISK_LOW, RISK_INDETERMINATE}), 4)


if __name__ == "__main__":
    unittest.main()
