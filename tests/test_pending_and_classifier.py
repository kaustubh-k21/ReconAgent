"""Regression: recon_meta survives ingest; within-SLA pending is not demoted."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import matcher
from ingest_adapter import canonicalize_engine_dir


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


class ReconMetaIngestTests(unittest.TestCase):
    def test_canonicalize_copies_recon_meta(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "data"
            dest = Path(td) / "engine"
            src.mkdir()
            _write_csv(
                src / "internal_ledger.csv",
                ["order_id", "order_date", "amount", "customer"],
                [{"order_id": "ORD1", "order_date": "2026-08-04",
                  "amount": "100.00", "customer": "c"}],
            )
            _write_csv(
                src / "settlement_report.csv",
                ["order_id", "settlement_date", "gross_amount", "fee", "tds", "net_amount"],
                [{"order_id": "ORD1", "settlement_date": "2026-08-05",
                  "gross_amount": "100.00", "fee": "2.00", "tds": "0.00",
                  "net_amount": "98.00"}],
            )
            _write_csv(
                src / "bank_statement.csv",
                ["reference", "credit_date", "credit_amount"],
                [],
            )
            (src / "recon_meta.json").write_text(
                json.dumps({"as_of": "2026-08-05", "sla_days": 7}),
                encoding="utf-8",
            )
            meta = canonicalize_engine_dir(str(src), str(dest))
            self.assertTrue(meta["copied_recon_meta"])
            self.assertTrue((dest / "recon_meta.json").exists())

    def test_with_recon_meta_missing_bank_within_sla_is_pending(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "data"
            dest = Path(td) / "engine"
            src.mkdir()
            _write_csv(
                src / "internal_ledger.csv",
                ["order_id", "order_date", "amount", "customer"],
                [{"order_id": "ORD_P", "order_date": "2026-08-04",
                  "amount": "100.00", "customer": "c"}],
            )
            _write_csv(
                src / "settlement_report.csv",
                ["order_id", "settlement_date", "gross_amount", "fee", "tds", "net_amount"],
                [{"order_id": "ORD_P", "settlement_date": "2026-08-05",
                  "gross_amount": "100.00", "fee": "2.00", "tds": "0.00",
                  "net_amount": "98.00"}],
            )
            # Near-ID lookalike must not demote within-SLA pending into an exception.
            _write_csv(
                src / "bank_statement.csv",
                ["reference", "credit_date", "credit_amount"],
                [{"reference": "ORD_Q", "credit_date": "2026-08-05",
                  "credit_amount": "98.00"}],
            )
            (src / "recon_meta.json").write_text(
                json.dumps({"as_of": "2026-08-05", "sla_days": 7}),
                encoding="utf-8",
            )
            canonicalize_engine_dir(str(src), str(dest))
            results = matcher.run_matching(data_dir=str(dest))
            pending_ids = {r["order_id"] for r in results["pending"]}
            exc_ids = {r["order_id"] for r in results["exceptions"]}
            self.assertIn("ORD_P", pending_ids)
            self.assertNotIn("ORD_P", exc_ids)


if __name__ == "__main__":
    unittest.main()
