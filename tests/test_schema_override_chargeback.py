"""Schema contract, chargeback/reversal, and operator override tests."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import exception_classifier as ec
import matcher
from schema_contract import SchemaError, validate_source_layout


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


class SchemaContractTests(unittest.TestCase):
    def test_missing_files_lists_contract(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SchemaError) as ctx:
                validate_source_layout(td)
            msg = str(ctx.exception)
            self.assertIn("Expected ENGINE files", msg)
            self.assertIn("internal_ledger.csv", msg)
            self.assertIn("CSV input contract", msg)

    def test_missing_column_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_csv(root / "internal_ledger.csv", ["order_id"], [{"order_id": "A"}])
            _write_csv(
                root / "settlement_report.csv",
                ["order_id", "gross_amount"],  # missing net_amount
                [{"order_id": "A", "gross_amount": "10"}],
            )
            _write_csv(
                root / "bank_statement.csv",
                ["reference", "credit_amount"],
                [{"reference": "A", "credit_amount": "9.8"}],
            )
            with self.assertRaises(SchemaError) as ctx:
                validate_source_layout(str(root))
            self.assertIn("net_amount", str(ctx.exception))


class ChargebackReversalTests(unittest.TestCase):
    def test_chargeback_keyword_beats_generic_shortfall(self):
        result = matcher.check_leg_b_one_to_one(
            {"settlement_date": "2026-07-01", "net_amount": "1000.00", "refund_amount": "0"},
            [{"credit_amount": "200.00", "credit_date": "2026-07-02",
              "debit_amount": "0", "narration": "VISA CHARGEBACK CBK123"}],
            matcher.load_policy(),
            as_of=None,
            closed_batch=True,
        )
        self.assertEqual(result["status"], "chargeback")

    def test_gateway_refund_still_wins_over_chargeback_wording(self):
        result = matcher.check_leg_b_one_to_one(
            {"settlement_date": "2026-07-01", "net_amount": "1000.00", "refund_amount": "800"},
            [{"credit_amount": "200.00", "credit_date": "2026-07-02",
              "debit_amount": "0", "narration": "chargeback mention ignored when refund memo exists"}],
            matcher.load_policy(),
            as_of=None,
            closed_batch=True,
        )
        self.assertIn(result["status"], ("refund_full", "refund_partial"))

    def test_classifier_chargeback_symptom(self):
        out = ec.rule_based_classify({
            "order_id": "X",
            "ledger": {"amount": "1000"},
            "settlement": [{"net_amount": "980"}],
            "bank": [{"credit_amount": "200", "narration": "dispute"}],
            "symptoms": ["chargeback"],
            "leg_b": {"status": "chargeback"},
        })
        self.assertEqual(out["cause"], "chargeback")


class OperatorOverrideTests(unittest.TestCase):
    def test_force_match_moves_exception(self):
        import controller_batch as cb

        with tempfile.TemporaryDirectory() as td:
            cb.BATCH_ROOT = Path(td)
            cb.ensure_batch_root()
            state = cb.create_batch()
            batch_id = state["batch_id"]
            results = {
                "summary": {"exceptions": 1, "exact_matches": 0, "fuzzy_matches": 0,
                            "review_matches": 0, "pending_bank": 0, "total_transactions": 1,
                            "controller": {}},
                "exceptions": [{
                    "order_id": "ORD1", "amount": 100, "cause": "aged_missing_bank",
                    "confidence": 0.8, "severity": "high",
                }],
                "exact_matches": [],
                "fuzzy_matches": [],
                "review_matches": [],
                "pending": [],
                "overrides": [],
            }
            out_path = cb.batch_dir(batch_id) / "results.json"
            out_path.write_text(json.dumps(results), encoding="utf-8")
            state["status"] = "COMPLETED"
            cb.save_batch(state)

            updated = cb.apply_override(
                batch_id, "ORD1", "force_match", "Confirmed UTR with bank", "alice"
            )
            self.assertEqual(len(updated["exceptions"]), 0)
            self.assertEqual(updated["exact_matches"][0]["order_id"], "ORD1")
            self.assertEqual(updated["overrides"][0]["note"], "Confirmed UTR with bank")
            self.assertEqual(updated["overrides"][0]["operator"], "alice")


if __name__ == "__main__":
    unittest.main()
