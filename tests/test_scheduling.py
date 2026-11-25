"""干预与号源调度：保护承诺、可联系时段、候补 FIFO、冻结与重排。"""

import unittest

from helpers import build_context, ts
from clinic_no_show.intervention import (
    can_contact_at,
    is_protected,
    plan_intervention,
)
from clinic_no_show.models import (
    ACTION_HOLD,
    ACTION_MANUAL_CONFIRM,
    ACTION_RELEASE_SLOT,
    RISK_INDETERMINATE,
)
from clinic_no_show.risk import assess_risk
from clinic_no_show.scheduling import (
    ARRANGEMENT_FROZEN,
    ARRANGEMENT_OFFERED,
    ARRANGEMENT_SUPERSEDED,
    freeable_slots,
    mark_notified,
    replan_after_event,
    run_scheduling,
)


class InterventionTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.identities, self.timeline = build_context()

    def test_contact_window_preference_and_default(self):
        # p-1 只在周一上午、周三下午的窗口可联系；周一下午不在窗口内。
        self.assertTrue(can_contact_at(self.repo, "p-1", ts("2026-10-12T09:00"))[0])
        ok, limits = can_contact_at(self.repo, "p-1", ts("2026-10-12T15:00"))
        self.assertFalse(ok)
        # 周二全天不在窗口。
        ok, _ = can_contact_at(self.repo, "p-1", ts("2026-10-13T09:00"))
        self.assertFalse(ok)
        # 无偏好的患者走默认窗口（工作日 09:00–18:00）并产生限制。
        ok, limits = can_contact_at(self.repo, "p-5", ts("2026-10-12T10:00"))
        self.assertTrue(ok)
        self.assertTrue(any(l.code == "contact_preference_missing" for l in limits))
        ok, _ = can_contact_at(self.repo, "p-5", ts("2026-10-12T20:00"))
        self.assertFalse(ok)
        ok, _ = can_contact_at(self.repo, "p-5", ts("2026-10-11T10:00"))  # 周日
        self.assertFalse(ok)

    def test_protected_slots_never_auto_released(self):
        protected = [s for s in self.repo.slots if is_protected(s)]
        self.assertEqual({s.slot_id for s in protected}, {"sl-301", "sl-302"})
        freeable = freeable_slots(self.repo, ts("2026-10-11T12:00"))
        self.assertEqual({s.slot_id for s in freeable}, {"sl-303", "sl-304"})

    def test_high_risk_with_protected_slot_downgrades_to_manual_confirm(self):
        as_of = ts("2026-10-11T12:00")
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-201", as_of)
        plan = plan_intervention(self.repo, assessment, as_of)
        # ap-201 同时段有 sl-301 followup 保护，不得释放。
        self.assertIn("sl-301", plan.protected_slot_ids)
        self.assertNotEqual(plan.action, ACTION_RELEASE_SLOT)

    def test_high_risk_open_slot_within_24h_candidate_release(self):
        # 决策时刻 2026-10-12T20:00，ap-202 在 10-13 08:00（12 小时后），
        # 同院区同时段 sl-303 为 open，可候选释放。
        as_of = ts("2026-10-12T20:00")
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-202", as_of)
        plan = plan_intervention(self.repo, assessment, as_of)
        self.assertEqual(plan.action, ACTION_RELEASE_SLOT)
        self.assertEqual(plan.protected_slot_ids, ())

    def test_indeterminate_holds_and_never_releases(self):
        as_of = ts("2026-10-12T20:00")
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-204", as_of)
        self.assertEqual(assessment.risk_level, RISK_INDETERMINATE)
        plan = plan_intervention(self.repo, assessment, as_of)
        self.assertEqual(plan.action, ACTION_HOLD)

    def test_plan_carries_fact_refs_and_reasons(self):
        as_of = ts("2026-10-11T12:00")
        assessment = assess_risk(self.repo, self.timeline, self.identities,
                                 "ap-202", as_of)
        plan = plan_intervention(self.repo, assessment, as_of)
        self.assertTrue(all(ref.startswith(("appointments.json", "attendance.json"))
                            for ref in plan.fact_refs))
        self.assertTrue(plan.reasons)


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.repo, _, _ = build_context()
        self.as_of = ts("2026-10-11T12:00")

    def test_waitlist_fifo_matches_open_slots_only(self):
        result = run_scheduling(self.repo, self.as_of)
        offered = {a.request_id: a.slot_id for a in result.arrangements}
        self.assertEqual(offered.get("wl-401"), "sl-303")
        self.assertEqual(offered.get("wl-402"), "sl-304")
        # wl-403 想要的是急诊保护号源，不能自动分配。
        self.assertNotIn("wl-403", offered)
        self.assertTrue(any(l.code == "no_freeable_slot"
                            and "wl-403" in l.refs
                            for l in result.limitations))
        self.assertEqual(set(result.protected_slot_ids), {"sl-301", "sl-302"})

    def test_notified_arrangement_freezes_and_is_kept_on_replan(self):
        first = run_scheduling(self.repo, self.as_of)
        target = next(a for a in first.arrangements if a.request_id == "wl-401")
        frozen_ledger = mark_notified(
            first.arrangements, target.arrangement_id,
            ts("2026-10-11T15:00"), "等待患者 24 小时内确认")
        replanned = replan_after_event(
            self.repo, ts("2026-10-11T18:00"), frozen_ledger,
            "ap-202 补录到达，释放候选号源重排")
        kept = next(a for a in replanned.arrangements
                    if a.arrangement_id == target.arrangement_id)
        self.assertEqual(kept.status, ARRANGEMENT_FROZEN)
        self.assertIn("补录到达", kept.follow_up)
        # 冻结号源不会被再次分配。
        self.assertFalse(any(
            a.status == ARRANGEMENT_OFFERED and a.slot_id == "sl-303"
            for a in replanned.arrangements))

    def test_unconfirmed_arrangement_can_be_superseded_with_history_kept(self):
        first = run_scheduling(self.repo, self.as_of)
        target = next(a for a in first.arrangements if a.request_id == "wl-402")
        replanned = replan_after_event(
            self.repo, ts("2026-10-11T18:00"), first.arrangements,
            "患者取消，候补池重排")
        old = next(a for a in replanned.arrangements
                   if a.arrangement_id == target.arrangement_id)
        self.assertEqual(old.status, ARRANGEMENT_SUPERSEDED)
        self.assertIn("患者取消", old.reason)
        # 旧记录仍在台账中（append-only，不抹除已发生的转移）。
        ids = [a.arrangement_id for a in replanned.arrangements]
        self.assertIn(target.arrangement_id, ids)

    def test_replan_reopens_request_and_links_new_to_superseded(self):
        first = run_scheduling(self.repo, self.as_of)
        old = next(a for a in first.arrangements if a.request_id == "wl-402")
        replanned = replan_after_event(
            self.repo, ts("2026-10-11T18:00"), first.arrangements,
            "取消到达，候补重排")
        new = next(a for a in replanned.arrangements
                   if a.request_id == "wl-402" and a.status == ARRANGEMENT_OFFERED)
        self.assertEqual(new.supersedes, old.arrangement_id)
        self.assertEqual(new.slot_id, old.slot_id)  # 同号源重新匹配
        self.assertIn("取消到达", new.reason)
        # 台账同时保留新旧两条记录。
        statuses = {a.arrangement_id: a.status for a in replanned.arrangements}
        self.assertEqual(statuses[old.arrangement_id], ARRANGEMENT_SUPERSEDED)

    def test_manual_confirm_action_constant(self):
        self.assertEqual(ACTION_MANUAL_CONFIRM, "manual_confirm")


if __name__ == "__main__":
    unittest.main()
