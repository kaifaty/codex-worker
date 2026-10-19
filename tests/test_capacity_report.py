"""Count capacity retries without duplicating final supervisor totals."""
import json
from pathlib import Path
import tempfile
import unittest

from worker_loader import load_worker


SCRIPT = (Path(__file__).resolve().parents[1]
          / "plugins/codex-worker/skills/codex-worker/scripts/task_report.py")


class CapacityReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="codex-capacity-report-")
        self.addCleanup(directory.cleanup)
        self.journal = Path(directory.name) / "journal.jsonl"
        self.now = 1_800_000_000
        self.reporter = load_worker(name="capacity_report", script=SCRIPT)

    def report(self, stages):
        rows = [dict(stage, v=1, at=self.now - 100 + index, repo="repo",
                     created=self.now - 1000)
                for index, stage in enumerate(stages)]
        self.journal.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        return self.reporter.build_report(self.journal, now=self.now)

    def test_report_counts_stage_retries_and_defaults_legacy_rows_to_zero(self):
        report = self.report([
            {"slug": "legacy", "phase": "work", "turn": 1, "outcome": "completed"},
            {"slug": "legacy", "phase": "turn", "turn": 1, "outcome": "completed"},
            {"slug": "retried", "phase": "work", "turn": 1, "outcome": "completed",
             "capacity_retries": 1},
            {"slug": "retried", "phase": "review", "turn": 1, "outcome": "completed",
             "capacity_retries": 1},
            {"slug": "retried", "phase": "fix", "turn": 1, "outcome": "completed",
             "capacity_retries": 0},
            {"slug": "retried", "phase": "turn", "turn": 1, "outcome": "completed",
             "capacity_retries": 2},
            {"slug": "retried", "phase": "work", "turn": 2, "outcome": "failed",
             "capacity_retries": 1},
            {"slug": "retried", "phase": "turn", "turn": 2, "outcome": "failed",
             "capacity_retries": 1},
            {"slug": "stopped", "phase": "work", "turn": 1, "outcome": "stopped",
             "capacity_retries": 0},
            {"slug": "stopped", "phase": "turn", "turn": 1, "outcome": "stopped",
             "capacity_retries": 0},
        ])
        self.assertEqual(report["summary"]["capacity_retries"], 3)
        self.assertIs(type(report["summary"]["capacity_retries"]), int)
        self.assertEqual({task["slug"]: task["capacity_retries"] for task in report["tasks"]},
                         {"legacy": 0, "retried": 3, "stopped": 0})

    def test_report_text_shows_total_and_task_capacity_retries(self):
        report = self.report([
            {"slug": "retried", "phase": "review", "turn": 1, "outcome": "completed",
             "capacity_retries": 1},
            {"slug": "retried", "phase": "turn", "turn": 1, "outcome": "completed",
             "capacity_retries": 1},
            {"slug": "legacy", "phase": "work", "turn": 1, "outcome": "completed"},
        ])
        text = self.reporter.render_report(report)
        self.assertIn("Capacity retries: 1\n", text)
        header, *lines = text.split("Tasks by cost:\n", 1)[1].splitlines()
        column = header.split("  ").index("capacity retries")
        self.assertEqual({line.split("  ")[0]: line.split("  ")[column] for line in lines},
                         {"legacy": "0", "retried": "1"})

    def test_empty_report_has_zero_capacity_retries(self):
        report = self.report([])
        self.assertEqual(report["summary"]["capacity_retries"], 0)
        self.assertEqual(report["tasks"], [])
        self.assertIn("Capacity retries: 0\n", self.reporter.render_report(report))


if __name__ == "__main__":
    unittest.main()
