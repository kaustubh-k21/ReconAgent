"""Contract checks for the finance-controller workflow UI."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ControllerWorkflowUi(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "controller.html").read_text(encoding="utf-8")
        self.dash = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
        self.policy = json.loads((ROOT / "matching_policy.json").read_text(encoding="utf-8"))

    def test_rail_has_four_control_stages(self):
        for label in (
            "Control setup",
            "Data validation",
            "Reconciliation",
            "Exception review",
        ):
            self.assertIn(label, self.html)
        self.assertNotIn("Human review", self.html)
        self.assertNotIn("Exception triage", self.html)
        self.assertNotIn("reviewCallout", self.html)

    def test_queue_tabs_cover_passed_and_open_rows(self):
        self.assertIn('data-tab="review"', self.html)
        self.assertIn('data-tab="reconciled"', self.html)
        self.assertIn('data-tab="pending"', self.html)
        self.assertIn('data-tab="all"', self.html)
        self.assertIn("Needs review", self.html)
        self.assertIn("Reconciled", self.html)

    def test_mapping_is_nested_not_a_rail_stage(self):
        self.assertIn("Mapping preview", self.html)
        self.assertNotIn('div class="label">Mapping</div>', self.html)
        self.assertIn("nested", self.html)

    def test_evidence_not_ai_reasoning(self):
        self.assertIn("Evidence &amp; explanation", self.html)
        self.assertNotIn(">Reasoning</th>", self.html)
        self.assertNotIn("AI reasoning", self.html.lower())
        self.assertIn("Evidence &amp; explanation", self.dash)
        self.assertNotIn(">Reasoning</th>", self.dash)

    def test_confidence_gated_to_policy_auto_bar(self):
        auto = self.policy["confidence"]["auto"]
        self.assertEqual(auto, 0.9)
        self.assertIn("const AUTO_CONFIRM = 0.9", self.html)
        self.assertIn("const AUTO_CONFIRM = 0.9", self.dash)
        self.assertIn("UNCERTAIN_CAUSES", self.html)
        self.assertIn("showClassificationConfidence", self.html)
        self.assertNotIn("conf-bar", self.html)
        self.assertNotIn("conf-bar", self.dash)

    def test_evidence_uses_plain_language_not_symptom_tokens(self):
        self.assertIn("Possible reference mismatch", self.html)
        self.assertIn("Bank credit missing past SLA", self.html)
        self.assertIn("Needs review", self.html)
        self.assertIn("presentException", self.html)
        self.assertIn("presentException", self.dash)
        self.assertNotIn("symptoms || []).slice(0, 3)", self.html)

    def test_detection_does_not_print_confidence(self):
        self.assertIn("Detected role:", self.html)
        self.assertNotIn("Detected ${d.detected_role} · ${d.confidence}", self.html)


if __name__ == "__main__":
    unittest.main()
