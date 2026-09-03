"""Tests for strict monetary parsing and ingest-boundary validation."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ingest_adapter import adapt_eval_dir, canonicalize_engine_dir, normalize_ledger_row
from ingest_validate import (
    BANK_CREDIT,
    BANK_DEBIT,
    GATEWAY_MONEY,
    LEDGER_MONEY,
    IngestValidationFailed,
    STATUS_FAILED,
    apply_field_rules,
    validate_money_rows,
)
from money import INVALID, MISSING, VALID, parse_money
from controller_batch import build_mapping, detect_role, format_inr


ROOT = Path(__file__).resolve().parents[1]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _mini_eval(tmpdir: Path, bank_credit="9.80", bank_debit="0.00",
               ledger_amount="10.00", fee="0.20", extra_bank=None) -> Path:
    src = tmpdir / "src"
    src.mkdir()
    _write_csv(src / "internal_ledger.csv",
               ["order_id", "order_date", "amount", "customer_name"],
               [{"order_id": "X", "order_date": "2026-01-01",
                 "amount": ledger_amount, "customer_name": "A"}])
    _write_csv(src / "gateway_settlement.csv",
               ["merchant_order_id", "settlement_date", "gross_amount",
                "fee", "tds", "refund_amount", "net_amount"],
               [{"merchant_order_id": "X", "settlement_date": "2026-01-02",
                 "gross_amount": "10.00", "fee": fee, "tds": "0.00",
                 "refund_amount": "0.00", "net_amount": "9.80"}])
    bank_row = {
        "value_date": "2026-01-02",
        "credit_amount": bank_credit,
        "debit_amount": bank_debit,
        "utr": "U",
        "reference": "X",
        "narration": "n",
    }
    if extra_bank:
        bank_row.update(extra_bank)
    _write_csv(src / "bank_statement.csv",
               ["value_date", "credit_amount", "debit_amount", "utr", "reference", "narration"],
               [bank_row])
    return src


class ParseMoneyTests(unittest.TestCase):
    def test_zero_is_valid_not_missing(self):
        parsed = parse_money("0.00")
        self.assertEqual(str(parsed), "VALID(0.00)")
        self.assertEqual(parsed.status, VALID)
        self.assertEqual(parsed.value, Decimal("0.00"))

    def test_zero_without_decimals(self):
        self.assertEqual(str(parse_money("0")), "VALID(0.00)")

    def test_blank_is_missing(self):
        self.assertEqual(str(parse_money("")), MISSING)
        self.assertEqual(str(parse_money("   ")), MISSING)
        self.assertEqual(str(parse_money(None)), MISSING)

    def test_indian_rupee_thousands(self):
        parsed = parse_money("₹4,500")
        self.assertEqual(str(parsed), "VALID(4500.00)")
        self.assertEqual(parsed.status, VALID)

    def test_rs_prefix(self):
        self.assertEqual(str(parse_money("Rs. 4,500")), "VALID(4500.00)")
        self.assertEqual(str(parse_money("INR 4500.00")), "VALID(4500.00)")

    def test_malformed_currency(self):
        parsed = parse_money("₹4,5OO")
        self.assertEqual(parsed.status, INVALID)
        self.assertEqual(str(parsed), "INVALID(malformed_currency)")
        self.assertEqual(parse_money("INR USD 4500").status, INVALID)
        self.assertEqual(parse_money("₹").status, INVALID)
        self.assertEqual(str(parse_money("₹")), "INVALID(empty_after_currency)")

    def test_malformed_double_decimal(self):
        parsed = parse_money("4.50.00")
        self.assertEqual(parsed.status, INVALID)
        self.assertEqual(str(parsed), "INVALID(invalid_grouping)")

    def test_abc_is_invalid(self):
        parsed = parse_money("abc")
        self.assertEqual(parsed.status, INVALID)
        self.assertEqual(str(parsed), "INVALID(not_numeric)")

    def test_scientific_notation_rejected(self):
        self.assertEqual(str(parse_money("1e3")), "INVALID(scientific_notation)")

    def test_negative_parse_allowed_at_parser(self):
        self.assertEqual(str(parse_money("-12.5")), "VALID(-12.50)")
        self.assertEqual(str(parse_money("(12.50)")), "VALID(-12.50)")

    def test_decimal_and_rounding(self):
        self.assertEqual(str(parse_money("10.1")), "VALID(10.10)")
        self.assertEqual(str(parse_money("4500.129")), "VALID(4500.13)")
        # banker's rounding to paise (ROUND_HALF_EVEN)
        self.assertEqual(str(parse_money("1.225")), "VALID(1.22)")
        self.assertEqual(str(parse_money("1.235")), "VALID(1.24)")

    def test_never_coerces_failure_to_zero(self):
        for raw in ("abc", "₹4,5OO", "", "₹"):
            parsed = parse_money(raw)
            if parsed.status == VALID:
                self.fail(f"{raw!r} should not be VALID")
            self.assertIsNone(parsed.value)


class FieldRuleTests(unittest.TestCase):
    def test_negative_ledger_amount_allowed(self):
        parsed = apply_field_rules(parse_money("-10.00"), LEDGER_MONEY[0])
        self.assertEqual(str(parsed), "VALID(-10.00)")

    def test_negative_credit_allowed(self):
        parsed = apply_field_rules(parse_money("-100.00"), BANK_CREDIT)
        self.assertEqual(str(parsed), "VALID(-100.00)")

    def test_negative_debit_rejected(self):
        parsed = apply_field_rules(parse_money("-5.00"), BANK_DEBIT)
        self.assertEqual(str(parsed), "INVALID(negative_not_allowed)")

    def test_negative_fee_rejected(self):
        fee = next(s for s in GATEWAY_MONEY if s.name == "fee")
        parsed = apply_field_rules(parse_money("-1.00"), fee)
        self.assertEqual(str(parsed), "INVALID(negative_not_allowed)")

    def test_negative_gross_rejected(self):
        gross = next(s for s in GATEWAY_MONEY if s.name == "gross_amount")
        parsed = apply_field_rules(parse_money("-10.00"), gross)
        self.assertEqual(str(parsed), "INVALID(negative_not_allowed)")


class IngestValidationTests(unittest.TestCase):
    def test_zero_credit_is_valid(self):
        rows = [{"credit_amount": "0.00", "debit_amount": "0.00"}]
        report = validate_money_rows("bank_statement.csv", rows, "bank")
        self.assertTrue(report.ok)

    def test_blank_credit_and_debit_fails(self):
        rows = [{"credit_amount": "", "debit_amount": ""}]
        report = validate_money_rows("bank_statement.csv", rows, "bank")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertEqual(report.quarantine[0].field, "credit_amount")
        self.assertEqual(report.quarantine[0].error, MISSING)
        self.assertEqual(report.quarantine[0].row, 2)

    def test_rupee_bank_amount_is_valid(self):
        rows = [{"credit_amount": "₹4,500", "debit_amount": ""}]
        report = validate_money_rows("bank_statement.csv", rows, "bank")
        self.assertTrue(report.ok)

    def test_abc_amount_quarantines_and_fails_batch(self):
        rows = [{"amount": "abc"}]
        report = validate_money_rows("internal_ledger.csv", rows, "ledger")
        self.assertEqual(report.status, STATUS_FAILED)
        rec = report.quarantine[0]
        self.assertEqual(rec.file, "internal_ledger.csv")
        self.assertEqual(rec.row, 2)
        self.assertEqual(rec.field, "amount")
        self.assertEqual(rec.value, "abc")
        self.assertEqual(rec.error, "INVALID(not_numeric)")

    def test_optional_fee_blank_is_not_quarantined(self):
        rows = [{
            "gross_amount": "10.00",
            "net_amount": "10.00",
            "fee": "",
            "tds": "",
            "refund_amount": "",
        }]
        report = validate_money_rows("gateway_settlement.csv", rows, "gateway")
        self.assertTrue(report.ok)

    def test_invalid_optional_fee_fails_batch(self):
        rows = [{
            "gross_amount": "10.00",
            "net_amount": "10.00",
            "fee": "abc",
        }]
        report = validate_money_rows("gateway_settlement.csv", rows, "gateway")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertEqual(report.quarantine[0].field, "fee")
        self.assertEqual(report.quarantine[0].error, "INVALID(not_numeric)")


class AdapterBoundaryTests(unittest.TestCase):
    def test_canonicalizes_rupee_and_zero_without_coercing_blanks_to_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _mini_eval(tmp, bank_credit="₹4,500", bank_debit="", fee="")
            dest = tmp / "engine"
            meta = adapt_eval_dir(str(src), str(dest))
            self.assertEqual(meta["money_validation"], "OK")
            with (dest / "bank_statement.csv").open(newline="", encoding="utf-8") as f:
                bank = list(csv.DictReader(f))
            self.assertEqual(bank[0]["credit_amount"], "4500.00")
            self.assertEqual(bank[0]["debit_amount"], "0.00")
            with (dest / "settlement_report.csv").open(newline="", encoding="utf-8") as f:
                gw = list(csv.DictReader(f))
            self.assertEqual(gw[0]["fee"], "0.00")

    def test_zero_amount_survives_canonicalization(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _mini_eval(tmp, bank_credit="0.00", bank_debit="0.00", ledger_amount="0.00")
            dest = tmp / "engine"
            adapt_eval_dir(str(src), str(dest))
            with (dest / "internal_ledger.csv").open(newline="", encoding="utf-8") as f:
                ledger = list(csv.DictReader(f))
            self.assertEqual(ledger[0]["amount"], "0.00")
            with (dest / "bank_statement.csv").open(newline="", encoding="utf-8") as f:
                bank = list(csv.DictReader(f))
            self.assertEqual(bank[0]["credit_amount"], "0.00")

    def test_invalid_bank_amount_quarantines_and_does_not_write_engine_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _mini_eval(tmp, bank_credit="abc")
            dest = tmp / "engine"
            with self.assertRaises(IngestValidationFailed) as ctx:
                adapt_eval_dir(str(src), str(dest))
            report = ctx.exception.report
            self.assertEqual(report.status, STATUS_FAILED)
            rec = report.quarantine[0].as_dict()
            self.assertEqual(rec["file"], "bank_statement.csv")
            self.assertEqual(rec["row"], 2)
            self.assertEqual(rec["field"], "credit_amount")
            self.assertEqual(rec["value"], "abc")
            self.assertEqual(rec["error"], "INVALID(not_numeric)")
            qpath = dest / "quarantine.json"
            self.assertTrue(qpath.exists())
            payload = json.loads(qpath.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], STATUS_FAILED)
            self.assertFalse((dest / "internal_ledger.csv").exists())
            self.assertFalse((dest / "settlement_report.csv").exists())
            self.assertFalse((dest / "bank_statement.csv").exists())

    def test_malformed_currency_fails_batch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _mini_eval(tmp, bank_credit="₹4,5OO")
            with self.assertRaises(IngestValidationFailed) as ctx:
                adapt_eval_dir(str(src), str(tmp / "engine"))
            rec = ctx.exception.report.quarantine[0]
            self.assertEqual(rec.error, "INVALID(malformed_currency)")
            self.assertEqual(rec.value, "₹4,5OO")

    def test_blank_critical_ledger_amount_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _mini_eval(tmp, ledger_amount="")
            with self.assertRaises(IngestValidationFailed) as ctx:
                adapt_eval_dir(str(src), str(tmp / "engine"))
            rec = ctx.exception.report.quarantine[0]
            self.assertEqual(rec.file, "internal_ledger.csv")
            self.assertEqual(rec.field, "amount")
            self.assertEqual(rec.error, MISSING)

    def test_negative_debit_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _mini_eval(tmp, bank_credit="0.00", bank_debit="-25.00")
            with self.assertRaises(IngestValidationFailed) as ctx:
                adapt_eval_dir(str(src), str(tmp / "engine"))
            rec = ctx.exception.report.quarantine[0]
            self.assertEqual(rec.field, "debit_amount")
            self.assertEqual(rec.error, "INVALID(negative_not_allowed)")

    def test_clean_heldout_easy_still_adapts(self):
        src = ROOT / "evaluation_datasets" / "easy_100"
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "engine"
            meta = adapt_eval_dir(str(src), str(dest))
            self.assertEqual(meta["money_validation"], "OK")
            self.assertGreater(meta["ledger_rows"], 0)
            with (dest / "bank_statement.csv").open(newline="", encoding="utf-8") as f:
                bank = list(csv.DictReader(f))
            self.assertTrue(all(row["credit_amount"] for row in bank))
            Decimal(bank[0]["credit_amount"])

    def test_engine_canonicalize_rewrites_money_only(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "data"
            dest = Path(td) / "engine"
            src.mkdir()
            _write_csv(src / "internal_ledger.csv",
                       ["order_id", "order_date", "amount", "customer"],
                       [{"order_id": "ORD1", "order_date": "2026-01-01",
                         "amount": "10.1", "customer": "c"}])
            _write_csv(src / "settlement_report.csv",
                       ["order_id", "settlement_date", "gross_amount", "fee",
                        "tds", "net_amount"],
                       [{"order_id": "ORD1", "settlement_date": "2026-01-02",
                         "gross_amount": "10.1", "fee": "0.2", "tds": "0.0",
                         "net_amount": "9.9"}])
            _write_csv(src / "bank_statement.csv",
                       ["reference", "credit_date", "credit_amount"],
                       [{"reference": "ORD1", "credit_date": "2026-01-02",
                         "credit_amount": "₹9.90"}])
            meta = canonicalize_engine_dir(str(src), str(dest))
            self.assertEqual(meta["money_validation"], "OK")
            with (dest / "bank_statement.csv").open(newline="", encoding="utf-8") as f:
                bank = list(csv.DictReader(f))
            self.assertEqual(bank[0]["credit_amount"], "9.90")
            self.assertNotIn("debit_amount", bank[0])
            with (dest / "internal_ledger.csv").open(newline="", encoding="utf-8") as f:
                ledger = list(csv.DictReader(f))
            self.assertEqual(ledger[0]["amount"], "10.10")


class ExternalLedgerSchemaTests(unittest.TestCase):
    def test_normalize_external_ledger_row(self):
        row = {
            "merchant_order_id": "PAY_000001",
            "order_date": "2026-07-01",
            "customer_name": "Karan Gupta",
            "gross_amount": "3499.00",
            "currency": "INR",
        }
        out = normalize_ledger_row(row, normalize_ids=False)
        self.assertEqual(out["order_id"], "PAY_000001")
        self.assertEqual(out["order_date"], "2026-07-01")
        self.assertEqual(out["amount"], "3499.00")
        self.assertEqual(out["customer"], "Karan Gupta")
        self.assertEqual(out["currency"], "INR")

    def test_mapping_preview_external_ledger(self):
        headers = [
            "merchant_order_id", "order_date", "customer_name", "gross_amount", "currency",
        ]
        rows = [{
            "merchant_order_id": "PAY_000001",
            "order_date": "2026-07-01",
            "customer_name": "Karan Gupta",
            "gross_amount": "3499.00",
            "currency": "INR",
        }]
        mapping = build_mapping("ledger", headers, rows)
        by_canon = {f["canonical"]: f for f in mapping["fields"]}
        self.assertEqual(by_canon["order_id"]["source"], "merchant_order_id")
        self.assertEqual(by_canon["order_id"]["sample_raw"], "PAY_000001")
        self.assertEqual(by_canon["amount"]["source"], "gross_amount")
        self.assertEqual(by_canon["amount"]["sample_canonical"], "3499.00")
        self.assertEqual(by_canon["amount"]["sample_display"], "₹3,499.00")
        self.assertIsNone(by_canon["amount"]["warning"])
        self.assertEqual(by_canon["customer"]["source"], "customer_name")
        self.assertEqual(by_canon["customer"]["sample_raw"], "Karan Gupta")
        self.assertEqual(by_canon["currency"]["source"], "currency")

    def test_unmapped_ledger_fields_warn_explicitly(self):
        mapping = build_mapping("ledger", ["order_date"], [{"order_date": "2026-07-01"}])
        by_canon = {f["canonical"]: f for f in mapping["fields"]}
        self.assertEqual(by_canon["order_id"]["warning"], "not mapped")
        self.assertEqual(by_canon["amount"]["warning"], "not mapped")
        self.assertIsNone(by_canon["order_date"]["warning"])

    def test_format_inr(self):
        self.assertEqual(format_inr("3499.00"), "₹3,499.00")
        self.assertEqual(format_inr("0"), "₹0.00")

    def test_detect_external_ledger_not_gateway(self):
        headers = [
            "merchant_order_id", "order_date", "customer_name", "gross_amount", "currency",
        ]
        det = detect_role(headers, "merchant_ledger.csv")
        self.assertEqual(det["detected_role"], "ledger")

    def test_adapt_external_ledger_through_eval_dir(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src"
            src.mkdir()
            _write_csv(src / "internal_ledger.csv",
                       ["merchant_order_id", "order_date", "customer_name",
                        "gross_amount", "currency"],
                       [{"merchant_order_id": "PAY_000001", "order_date": "2026-07-01",
                         "customer_name": "Karan Gupta", "gross_amount": "3499.00",
                         "currency": "INR"}])
            _write_csv(src / "gateway_settlement.csv",
                       ["merchant_order_id", "settlement_date", "gross_amount",
                        "fee", "tds", "refund_amount", "net_amount"],
                       [{"merchant_order_id": "PAY_000001", "settlement_date": "2026-07-02",
                         "gross_amount": "3499.00", "fee": "70.00", "tds": "0.00",
                         "refund_amount": "0.00", "net_amount": "3429.00"}])
            _write_csv(src / "bank_statement.csv",
                       ["value_date", "credit_amount", "debit_amount", "utr",
                        "reference", "narration"],
                       [{"value_date": "2026-07-02", "credit_amount": "3429.00",
                         "debit_amount": "0.00", "utr": "U1",
                         "reference": "PAY_000001", "narration": "n"}])
            dest = tmp / "engine"
            adapt_eval_dir(str(src), str(dest), normalize_ids=False)
            with (dest / "internal_ledger.csv").open(newline="", encoding="utf-8") as f:
                ledger = list(csv.DictReader(f))
            self.assertEqual(ledger[0]["order_id"], "PAY_000001")
            self.assertEqual(ledger[0]["amount"], "3499.00")
            self.assertEqual(ledger[0]["customer"], "Karan Gupta")
            self.assertEqual(ledger[0]["currency"], "INR")


if __name__ == "__main__":
    unittest.main()
