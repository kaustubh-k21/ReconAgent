"""Adversarial protocol: generate, ingest gates, and a matching smoke run."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "evaluation_datasets"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(EVAL))

from ingest_adapter import adapt_eval_dir  # noqa: E402
from ingest_validate import IngestValidationFailed  # noqa: E402
from _generate_adversarial import GENERATORS, ADV, main as generate  # noqa: E402
from run_adversarial import run_ingest_suite, run_matching_suite  # noqa: E402


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GenerateDeterministic(unittest.TestCase):
    def test_second_generate_is_identical(self):
        generate()
        first = {
            p.relative_to(ADV).as_posix(): _file_hash(p)
            for p in sorted(ADV.rglob("*"))
            if p.is_file() and p.name != "README.md"
        }
        generate()
        second = {
            p.relative_to(ADV).as_posix(): _file_hash(p)
            for p in sorted(ADV.rglob("*"))
            if p.is_file() and p.name != "README.md"
        }
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(GENERATORS), 10)


class LocaleIngestOk(unittest.TestCase):
    def test_locale_amounts_adapt(self):
        import tempfile
        generate()
        with tempfile.TemporaryDirectory(prefix="adv_locale_") as tmp:
            dest = Path(tmp) / "engine"
            dest.mkdir(parents=True)
            meta = adapt_eval_dir(str(ADV / "locale_money_ok"), str(dest), normalize_ids=True)
            self.assertEqual(meta["money_validation"], "OK")
            ledger = (dest / "internal_ledger.csv").read_text(encoding="utf-8")
            self.assertIn("4500.00", ledger)


class IngestFailures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate()

    def test_malformed_money_fails(self):
        report = run_ingest_suite("malformed_money")
        self.assertTrue(report["ok"])
        self.assertEqual(report["actual_status"], "VALIDATION_FAILED")
        self.assertTrue(report["blocked_reconcile"])
        values = [q.get("value") for q in report["quarantine"]]
        self.assertIn("abc", values)

    def test_missing_amount_fails(self):
        report = run_ingest_suite("missing_amount")
        self.assertTrue(report["ok"])
        self.assertEqual(report["actual_status"], "VALIDATION_FAILED")
        self.assertTrue(report["blocked_reconcile"])
        fields = [q.get("field") for q in report["quarantine"]]
        self.assertIn("amount", fields)

    def test_adapt_raises_on_malformed(self):
        import tempfile
        dest = Path(tempfile.mkdtemp(prefix="adv_bad_")) / "engine"
        dest.mkdir(parents=True)
        with self.assertRaises(IngestValidationFailed):
            adapt_eval_dir(str(ADV / "malformed_money"), str(dest), normalize_ids=True)


class MatchingSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate()

    def test_amount_threshold_no_false_match(self):
        report = run_matching_suite("amount_threshold", use_ml=False)
        self.assertEqual(report["ingest_status"], "OK")
        self.assertEqual((report.get("harm") or {}).get("false_match_count"), 0)
        self.assertGreaterEqual(report["n"], 5)

    def test_near_id_no_false_match(self):
        report = run_matching_suite("near_id_collision", use_ml=False)
        self.assertEqual((report.get("harm") or {}).get("false_match_count"), 0)


if __name__ == "__main__":
    unittest.main()
