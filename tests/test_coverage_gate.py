"""Regression checks for the separate Python coverage floors."""

from __future__ import annotations

import configparser
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_coverage import main, percentage


class CoverageGateTest(unittest.TestCase):
    def run_gate(self, covered_lines: int, covered_branches: int) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "coverage.json"
            report.write_text(
                json.dumps({"totals": {
                    "covered_lines": covered_lines,
                    "num_statements": 1000,
                    "covered_branches": covered_branches,
                    "num_branches": 1000,
                }}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with patch("sys.argv", ["check_coverage.py", str(report), "--lines", "45", "--branches", "30"]):
                with contextlib.redirect_stdout(output):
                    result = main()
            return result, output.getvalue()

    def test_exact_floors_pass(self) -> None:
        result, output = self.run_gate(450, 300)
        self.assertEqual(result, 0)
        self.assertIn("lines=45.00% (required 45.00%)", output)
        self.assertIn("branches=30.00% (required 30.00%)", output)

    def test_line_regression_still_fails(self) -> None:
        result, output = self.run_gate(449, 600)
        self.assertEqual(result, 1)
        self.assertIn("line coverage 44.90% is below 45.00%", output)

    def test_branch_regression_still_fails(self) -> None:
        result, output = self.run_gate(800, 299)
        self.assertEqual(result, 1)
        self.assertIn("branch coverage 29.90% is below 30.00%", output)

    def test_reports_both_failures(self) -> None:
        result, output = self.run_gate(449, 299)
        self.assertEqual(result, 1)
        self.assertIn("line coverage", output)
        self.assertIn("branch coverage", output)

    def test_no_branch_opportunities_are_fully_covered(self) -> None:
        self.assertEqual(percentage(0, 0), 100.0)

    def test_aggregate_metric_is_report_only(self) -> None:
        config = configparser.ConfigParser()
        config.read(Path(__file__).resolve().parents[1] / ".coveragerc", encoding="utf-8")
        self.assertTrue(config.getboolean("run", "branch"))
        self.assertEqual(config.getfloat("report", "fail_under"), 0)


if __name__ == "__main__":
    unittest.main()
