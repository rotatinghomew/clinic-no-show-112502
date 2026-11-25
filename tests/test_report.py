"""综合报告、人群画像、策略周期对比与 CLI 冒烟。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import DATA, FIXTURE, build_context, ts
from clinic_no_show.report import build_report, build_report_from_repo
from clinic_no_show.segments import hour_bucket, segment_population
from clinic_no_show.strategy import compare_strategies

ROOT = Path(__file__).resolve().parents[1]


class ReportTest(unittest.TestCase):
    def test_report_contains_facts_protections_outcomes_and_limits(self):
        report = build_report(str(FIXTURE), as_of=ts("2026-10-11T12:00"))
        payload = report.to_dict()

        self.assertEqual(payload["as_of"], "2026-10-11T12:00:00")
        # 限制分层输出，不把未知当高风险。
        self.assertTrue(payload["limitations"]["identity"])
        self.assertTrue(payload["limitations"]["timeline"])
        # 保护承诺显式列出。
        protected_ids = {p["slot_id"] for p in payload["protected_commitments"]}
        self.assertEqual(protected_ids, {"sl-301", "sl-302"})
        # 每条预约事实都带实际结果与提醒事实。
        facts = {f["appointment_id"]: f for f in payload["appointment_facts"]}
        self.assertIn("outcome", facts["ap-103"])
        self.assertEqual(facts["ap-103"]["outcome"]["outcome"], "no_show")
        self.assertEqual(len(facts["ap-201"]["reminders_sent"]), 1)
        # 无患者键预约进入事实档案但不进入风险评估。
        assessed = {a["appointment_id"] for a in payload["risk_assessments"]}
        self.assertIn("ap-201", assessed)
        self.assertIn("ap-203", assessed)  # 预约级仍评估，但为 indeterminate
        indeterminate = [a for a in payload["risk_assessments"]
                         if a["risk_level"] == "indeterminate"]
        self.assertTrue(indeterminate)
        # 人群规模统计四层分列。
        self.assertEqual(
            sum(payload["risk_population"].values()),
            len(payload["risk_assessments"]),
        )

    def test_segments_keep_unknown_separate(self):
        repo, identities, timeline = build_context()
        stats = segment_population(repo, timeline, identities,
                                   ts("2026-10-11T12:00"))
        derm_west = next(s for s in stats if s.site == "west" and s.clinic == "derm")
        # ap-108 设备离线 -> unknown，不进失约数也不进失约率分母。
        self.assertGreaterEqual(derm_west.unknown, 1)
        self.assertEqual(derm_west.no_show, 1)  # ap-105 核对失约
        self.assertEqual(hour_bucket(ts("2026-10-12T09:00")), "08-10")

    def test_strategy_comparison_groups_by_cycle_and_template(self):
        repo, _, _ = build_context()
        comparison = compare_strategies(repo).to_dict()
        templates = {c["template"] for c in comparison["cycles"]}
        self.assertIn("v1", templates)
        self.assertIn("v2", templates)
        # v1 周期内 3 次核对失约，失约率应为 1.0；未知不混入。
        v1 = next(c for c in comparison["cycles"] if c["template"] == "v1")
        self.assertEqual(v1["no_show"], 3)
        self.assertEqual(v1["no_show_rate"], 1.0)
        for cycle in comparison["cycles"]:
            self.assertIsInstance(cycle["unknown"], int)

    def test_strategy_excludes_future_appointments(self):
        from clinic_no_show.strategy import compare_strategies as cs
        repo, _, _ = build_context()
        # 截止 10-09：10-12 之后的预约（ap-201 等）尚未到就诊时间，必须排除。
        comparison = cs(repo, observation_as_of=ts("2026-10-09T23:59")).to_dict()
        all_counted = sum(c["appointments"] for c in comparison["cycles"])
        # 10-09 之前开始的预约共 12 个（历史部分），未来 4 个不计。
        self.assertEqual(all_counted, 12)
        codes = {l["code"] for l in comparison["limitations"]}
        self.assertIn("future_appointments_excluded_from_strategy", codes)

    def test_default_as_of_uses_last_visible_fact(self):
        report = build_report(str(FIXTURE))
        self.assertIn("as_of", report.to_dict())

    def test_historical_decision_immutable_after_backfill(self):
        # 同一预约在两个决策时刻的评估都只基于当时可见事实，
        # 后补记录改变当前判断但不能改变历史结论。
        from clinic_no_show.risk import assess_risk
        repo, identities, timeline = build_context()
        early = assess_risk(repo, timeline, identities, "ap-202",
                            ts("2026-09-30T12:00"))
        later = assess_risk(repo, timeline, identities, "ap-202",
                            ts("2026-10-11T12:00"))
        self.assertEqual(early.risk_level, "indeterminate")
        self.assertEqual(later.risk_level, "high")
        self.assertLess(early.score, later.score)
        # 两次评估的 as-of 事实边界不同。
        self.assertNotEqual(early.based_on_visible_facts_as_of,
                            later.based_on_visible_facts_as_of)

    def test_reminder_fact_tied_to_appointment_state_at_send_time(self):
        # rm-203 在 ap-103 还是 scheduled 时发出；之后核对失约不抹掉提醒事实。
        repo, _, timeline = build_context()
        reminder = next(f for f in timeline.facts_for("ap-103")
                        if f.kind == "reminder_sent")
        self.assertEqual(reminder.payload["template"], "v2")
        self.assertEqual(reminder.visible_at.isoformat(), "2026-10-06T18:00:00")
        # pending 提醒（未发送）不构成事实。
        self.assertFalse(any(f.kind == "reminder_sent"
                             for f in timeline.facts_for("ap-203")))

    def test_baseline_dataset_still_loads_and_reports(self):
        # 原始小资料包：无关联/偏好文件，报告必须带数据集级限制而非报错。
        report = build_report(str(DATA))
        payload = report.to_dict()
        codes = {l["code"] for l in payload["limitations"]["dataset"]}
        self.assertIn("missing_identity_links", codes)
        self.assertIn("missing_contact_preferences", codes)


class CliSmokeTest(unittest.TestCase):
    def test_cli_emits_json_report(self):
        proc = subprocess.run(
            [sys.executable, "-m", "clinic_no_show.cli",
             "--data", str(FIXTURE), "--as-of", "2026-10-11T12:00"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": __import__("os").environ["PATH"]},
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertIn("risk_assessments", payload)
        self.assertIn("scheduling", payload)


if __name__ == "__main__":
    unittest.main()
