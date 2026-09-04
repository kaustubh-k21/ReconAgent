"""Presentation copy for exception rows — does not change classifier labels."""

from __future__ import annotations

import unittest

from exception_copy import enrich_classified, present_exception


class PresentExceptionCopy(unittest.TestCase):
    def test_wrong_transaction_missing_bank_is_plain_language(self):
        out = present_exception({
            "order_id": "VID_0048",
            "amount": 9999,
            "cause": "wrong_transaction",
            "symptoms": ["one_sided_no_bank_past_sla", "wrong_transaction_candidate"],
            "age_days": 12,
            "candidate_bank_refs": ["VID_0049"],
            "settlement": [{"net_amount": "9799.02"}],
        })
        self.assertEqual(out["display_cause"], "Possible reference mismatch")
        self.assertIn("not auto-linked", out["evidence_teaser"].lower())
        blob = " ".join(out["evidence_facts"]).lower()
        self.assertIn("vid_0048", blob)
        self.assertIn("vid_0049", blob)
        self.assertIn("past the 7-day sla", blob)
        self.assertIn("UTR", out["evidence_action"])
        self.assertNotIn("one sided", out["evidence_teaser"].lower())
        self.assertNotIn("wrong transaction candidate", out["evidence_teaser"].lower())
        self.assertNotIn("settlement", out)
        self.assertNotIn("bank", out)

    def test_structural_cause_with_link_suspicion(self):
        out = present_exception({
            "order_id": "ORD_A",
            "amount": 1000,
            "cause": "aged_missing_bank",
            "link_suspicion": "wrong_transaction",
            "symptoms": ["one_sided_no_bank_past_sla", "wrong_transaction_candidate"],
            "age_days": 14,
            "candidate_bank_refs": ["ORD_B"],
            "settlement": [{"net_amount": "980.00"}],
        })
        self.assertEqual(out["display_cause"], "Missing bank credit (past SLA)")
        self.assertEqual(out["display_link_suspicion"], "Possible reference mismatch")
        self.assertIn("similar", out["evidence_teaser"].lower())
        self.assertIn("ORD_B", " ".join(out["evidence_facts"]))

    def test_refund_cites_amounts_not_the_cause_token(self):
        out = present_exception({
            "order_id": "VID_0032",
            "amount": 4500,
            "cause": "refund_partial",
            "symptoms": ["refund_partial"],
            "settlement": [{"net_amount": "4410.00", "refund_amount": "800.00"}],
            "bank": [{"credit_amount": "3610.00", "debit_amount": "0"}],
        })
        self.assertEqual(out["display_cause"], "Partial refund")
        blob = " ".join(out["evidence_facts"])
        self.assertIn("₹800.00", blob)
        self.assertIn("₹4,410.00", blob)
        self.assertIn("₹3,610.00", blob)
        self.assertNotEqual(out["evidence_teaser"].lower(), "refund partial")

    def test_enrich_pulls_candidates_from_matcher_record(self):
        classified = [{
            "order_id": "A1",
            "amount": 100,
            "cause": "wrong_transaction",
            "symptoms": ["wrong_transaction_candidate"],
            "confidence": 0.78,
        }]
        matcher_exc = [{
            "order_id": "A1",
            "candidate_bank_refs": ["B9"],
            "settlement": [],
            "bank": [],
        }]
        out = enrich_classified(classified, matcher_exc)
        self.assertIn("B9", " ".join(out[0]["evidence_facts"]))
        self.assertEqual(classified[0].get("candidate_bank_refs"), None)


class DualLabelClassifierTests(unittest.TestCase):
    def test_aged_with_lookalike_keeps_structural_cause(self):
        import exception_classifier as ec
        result = ec.rule_based_classify({
            "order_id": "ORD_A",
            "ledger": {"amount": "100.00"},
            "settlement": [{"net_amount": "98.00"}],
            "bank": [],
            "symptoms": ["one_sided_no_bank_past_sla", "wrong_transaction_candidate"],
            "candidate_bank_refs": ["ORD_B"],
            "age_days": 14,
            "leg_b": {"status": "aged_missing", "note": "past SLA"},
        })
        self.assertEqual(result["cause"], "aged_missing_bank")
        self.assertEqual(result["link_suspicion"], "wrong_transaction")

    def test_refund_beats_lookalike_for_cause(self):
        import exception_classifier as ec
        result = ec.rule_based_classify({
            "order_id": "ORD_R",
            "ledger": {"amount": "10000.00"},
            "settlement": [{"net_amount": "9800.00", "refund_amount": "500.00"}],
            "bank": [{"credit_amount": "4189.84"}],
            "symptoms": ["under_amount", "wrong_transaction_candidate", "refund_partial"],
            "candidate_bank_refs": ["ORD_OTHER"],
            "leg_b": {"status": "under_amount", "delta": -5610.16},
        })
        self.assertEqual(result["cause"], "refund_partial")
        self.assertEqual(result["link_suspicion"], "wrong_transaction")


if __name__ == "__main__":
    unittest.main()
