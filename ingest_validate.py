"""Validate money before canonicalization. Keep invalid amounts out of reconciliation."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from money import INVALID, MISSING, VALID, MoneyParse, parse_money

STATUS_OK = "OK"
STATUS_FAILED = "VALIDATION_FAILED"


@dataclass(frozen=True)
class MoneyField:
    name: str
    aliases: tuple[str, ...]
    critical: bool
    allow_negative: bool
    optional_missing: bool


@dataclass(frozen=True)
class QuarantineRecord:
    file: str
    row: int
    field: str
    value: str
    error: str

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "row": self.row,
            "field": self.field,
            "value": self.value,
            "error": self.error,
        }


@dataclass
class ValidationReport:
    status: str = STATUS_OK
    quarantine: list[QuarantineRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and not self.quarantine

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "quarantine": [q.as_dict() for q in self.quarantine],
        }


class IngestValidationFailed(Exception):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__(STATUS_FAILED)


LEDGER_MONEY = (
    MoneyField("amount", ("amount", "ledger_amount", "gross_amount"), True, True, False),
)

GATEWAY_MONEY = (
    MoneyField("gross_amount", ("gross_amount", "amount"), True, False, False),
    MoneyField("net_amount", ("net_amount", "settlement_amount"), True, True, False),
    MoneyField("fee", ("fee",), False, False, True),
    MoneyField("tds", ("tds",), False, False, True),
    MoneyField("refund_amount", ("refund_amount",), False, False, True),
)

BANK_CREDIT = MoneyField("credit_amount", ("credit_amount",), True, True, False)
BANK_DEBIT = MoneyField("debit_amount", ("debit_amount",), True, False, True)


def pick_field(row: dict, spec: MoneyField) -> tuple[str, object]:
    for alias in spec.aliases:
        if alias in row:
            return alias, row.get(alias)
    return spec.name, None


def apply_field_rules(parsed: MoneyParse, spec: MoneyField) -> MoneyParse:
    if parsed.status == VALID and parsed.value is not None and parsed.value < 0 and not spec.allow_negative:
        return MoneyParse(INVALID, None, "negative_not_allowed", parsed.raw)
    return parsed


def _quarantine(file: str, row: int, field: str, parsed: MoneyParse) -> QuarantineRecord:
    value = "" if parsed.raw is None else str(parsed.raw)
    error = MISSING if parsed.status == MISSING else str(parsed)
    return QuarantineRecord(file=file, row=row, field=field, value=value, error=error)


def _fail(report: ValidationReport, rec: QuarantineRecord) -> None:
    report.quarantine.append(rec)
    report.status = STATUS_FAILED


def validate_money_rows(filename: str, rows: Iterable[dict], kind: str) -> ValidationReport:
    report = ValidationReport()
    row_list = list(rows)
    if kind == "ledger":
        specs = LEDGER_MONEY
        for i, row in enumerate(row_list, start=2):
            _validate_specs(filename, i, row, specs, report)
        return report
    if kind == "gateway":
        specs = GATEWAY_MONEY
        for i, row in enumerate(row_list, start=2):
            _validate_specs(filename, i, row, specs, report)
        return report
    if kind == "bank":
        for i, row in enumerate(row_list, start=2):
            _validate_bank_row(filename, i, row, report)
        return report
    raise ValueError(f"unknown source kind: {kind}")


def _validate_specs(filename: str, row_num: int, row: dict,
                    specs: tuple[MoneyField, ...], report: ValidationReport) -> None:
    for spec in specs:
        field_name, raw = pick_field(row, spec)
        parsed = apply_field_rules(parse_money(raw), spec)
        if parsed.status == VALID:
            continue
        if parsed.status == MISSING and spec.optional_missing:
            continue
        _fail(report, _quarantine(filename, row_num, field_name, parsed))


def _validate_bank_row(filename: str, row_num: int, row: dict,
                       report: ValidationReport) -> None:
    credit_name, credit_raw = pick_field(row, BANK_CREDIT)
    debit_present = any(alias in row for alias in BANK_DEBIT.aliases)
    debit_name, debit_raw = pick_field(row, BANK_DEBIT) if debit_present else (BANK_DEBIT.name, None)

    credit = apply_field_rules(parse_money(credit_raw if credit_name in row else None), BANK_CREDIT)
    debit = apply_field_rules(
        parse_money(debit_raw if debit_present else None),
        BANK_DEBIT,
    )

    if credit.status == INVALID:
        _fail(report, _quarantine(filename, row_num, credit_name, credit))
    if debit_present and debit.status == INVALID:
        _fail(report, _quarantine(filename, row_num, debit_name, debit))

    credit_ok = credit.status == VALID
    debit_ok = debit_present and debit.status == VALID
    if credit_ok or debit_ok:
        return
    if credit.status == INVALID or (debit_present and debit.status == INVALID):
        return
    # Credit missing and no usable debit — fail on the credit field.
    _fail(report, _quarantine(filename, row_num, credit_name, credit))


def merge_reports(*reports: ValidationReport) -> ValidationReport:
    out = ValidationReport()
    for report in reports:
        out.quarantine.extend(report.quarantine)
        if report.status == STATUS_FAILED:
            out.status = STATUS_FAILED
    return out


def validate_eval_rows(ledger_rows: list[dict], gateway_rows: list[dict],
                       bank_rows: list[dict]) -> ValidationReport:
    return merge_reports(
        validate_money_rows("internal_ledger.csv", ledger_rows, "ledger"),
        validate_money_rows("gateway_settlement.csv", gateway_rows, "gateway"),
        validate_money_rows("bank_statement.csv", bank_rows, "bank"),
    )


def validate_engine_dir(data_dir: str) -> ValidationReport:
    reports = []
    mapping = (
        ("internal_ledger.csv", "ledger"),
        ("settlement_report.csv", "gateway"),
        ("bank_statement.csv", "bank"),
    )
    for filename, kind in mapping:
        path = os.path.join(data_dir, filename)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        reports.append(validate_money_rows(filename, rows, kind))
    return merge_reports(*reports) if reports else ValidationReport()


def canonical_cell(raw, spec: MoneyField) -> str:
    parsed = apply_field_rules(parse_money(raw), spec)
    if parsed.status == VALID:
        return parsed.canonical()
    if parsed.status == MISSING and spec.optional_missing:
        return "0.00"
    raise RuntimeError("unvalidated money reached canonicalization")


def canonical_or_zero(raw, spec: MoneyField) -> str:
    """MISSING becomes 0.00 only after validation allowed it."""
    parsed = apply_field_rules(parse_money(raw), spec)
    if parsed.status == VALID:
        return parsed.canonical()
    if parsed.status == MISSING:
        return "0.00"
    raise RuntimeError("unvalidated money reached canonicalization")


def write_quarantine_report(path: str, report: ValidationReport) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.as_dict(), f, indent=2)
        f.write("\n")


def raise_if_failed(report: ValidationReport, quarantine_path: Optional[str] = None) -> None:
    if report.ok:
        return
    if quarantine_path:
        write_quarantine_report(quarantine_path, report)
    raise IngestValidationFailed(report)
