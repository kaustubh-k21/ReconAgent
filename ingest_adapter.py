"""Translate source files into engine schema. Does not read labels or invent batch IDs.

ID normalization (hyphens/case) is ingest, not matching.
"""

from __future__ import annotations

import csv
import os
from typing import Optional

from ingest_validate import (
    BANK_CREDIT,
    BANK_DEBIT,
    GATEWAY_MONEY,
    LEDGER_MONEY,
    IngestValidationFailed,
    canonical_cell,
    canonical_or_zero,
    pick_field,
    raise_if_failed,
    validate_engine_dir,
    validate_eval_rows,
)

ENGINE_FILES = ("internal_ledger.csv", "settlement_report.csv", "bank_statement.csv")
EVAL_GATEWAY = "gateway_settlement.csv"


def looks_like_engine_schema(data_dir: str) -> bool:
    return all(os.path.exists(os.path.join(data_dir, f)) for f in ENGINE_FILES)


def looks_like_eval_schema(data_dir: str) -> bool:
    return (
        os.path.exists(os.path.join(data_dir, "internal_ledger.csv"))
        and os.path.exists(os.path.join(data_dir, EVAL_GATEWAY))
        and os.path.exists(os.path.join(data_dir, "bank_statement.csv"))
    )


def canonicalize_id(value: str) -> str:
    if value is None:
        return ""
    return str(value).replace("-", "").replace(" ", "").strip().lower()


def _read(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def _id(value: str, normalize: bool) -> str:
    raw = (value or "").strip()
    return canonicalize_id(raw) if normalize else raw


def _canon(row: dict, spec) -> str:
    _, raw = pick_field(row, spec)
    return canonical_cell(raw, spec)


def _pick_first(row: dict, *keys: str) -> str:
    for key in keys:
        if key in row and row.get(key) is not None and str(row.get(key)).strip() != "":
            return str(row.get(key)).strip()
    for key in keys:
        if key in row:
            return str(row.get(key) or "").strip()
    return ""


def normalize_ledger_row(row: dict, normalize_ids: bool = True) -> dict:
    oid = _pick_first(row, "merchant_order_id", "order_id", "transaction_id")
    return {
        "order_id": _id(oid, normalize_ids),
        "source_order_id": oid,
        "order_date": _pick_first(row, "order_date", "txn_date"),
        "amount": _canon(row, LEDGER_MONEY[0]),
        "customer": _pick_first(row, "customer", "customer_name"),
        "currency": _pick_first(row, "currency") or "INR",
    }


def adapt_eval_dir(src_dir: str, dest_dir: str, normalize_ids: bool = True) -> dict:
    """Eval CSVs → engine files. Never copies ground_truth.csv."""
    os.makedirs(dest_dir, exist_ok=True)

    ledger_in = _read(os.path.join(src_dir, "internal_ledger.csv"))
    gw_in = _read(os.path.join(src_dir, EVAL_GATEWAY))
    bank_in = _read(os.path.join(src_dir, "bank_statement.csv"))

    report = validate_eval_rows(ledger_in, gw_in, bank_in)
    raise_if_failed(report, os.path.join(dest_dir, "quarantine.json"))

    ledger_out = [normalize_ledger_row(r, normalize_ids=normalize_ids) for r in ledger_in]

    gw_out = []
    gw_by_name = {spec.name: spec for spec in GATEWAY_MONEY}
    for r in gw_in:
        oid = _pick_first(r, "merchant_order_id", "order_id")
        batch = _pick_first(r, "payout_batch", "settlement_batch_id")
        gw_out.append({
            "order_id": _id(oid, normalize_ids),
            "source_order_id": oid,
            "settlement_date": r.get("settlement_date") or "",
            "gross_amount": _canon(r, gw_by_name["gross_amount"]),
            "fee": _canon(r, gw_by_name["fee"]),
            "tds": _canon(r, gw_by_name["tds"]),
            "refund_amount": _canon(r, gw_by_name["refund_amount"]),
            "net_amount": _canon(r, gw_by_name["net_amount"]),
            "settlement_batch_id": _id(batch, normalize_ids) if batch else "",
        })

    bank_out = []
    for r in bank_in:
        # Blank reference is evidence, not a join key.
        ref = _pick_first(r, "reference", "bank_reference")
        _, credit_raw = pick_field(r, BANK_CREDIT)
        debit_present = any(alias in r for alias in BANK_DEBIT.aliases)
        debit_raw = r.get("debit_amount") if debit_present else None
        bank_out.append({
            "reference": _id(ref, normalize_ids) if ref else "",
            "source_reference": ref,
            "credit_date": _pick_first(r, "credit_date", "value_date"),
            "credit_amount": canonical_or_zero(credit_raw, BANK_CREDIT),
            "debit_amount": canonical_or_zero(debit_raw, BANK_DEBIT),
            "utr": r.get("utr") or "",
            "narration": r.get("narration") or "",
        })

    _write(os.path.join(dest_dir, "internal_ledger.csv"), ledger_out,
           ["order_id", "source_order_id", "order_date", "amount", "customer", "currency"])
    _write(os.path.join(dest_dir, "settlement_report.csv"), gw_out,
           ["order_id", "source_order_id", "settlement_date", "gross_amount", "fee", "tds",
            "refund_amount", "net_amount", "settlement_batch_id"])
    _write(os.path.join(dest_dir, "bank_statement.csv"), bank_out,
           ["reference", "source_reference", "credit_date", "credit_amount", "debit_amount",
            "utr", "narration"])

    return {
        "source_dir": os.path.abspath(src_dir),
        "dest_dir": os.path.abspath(dest_dir),
        "normalize_ids": bool(normalize_ids),
        "copied_ground_truth": False,
        "invented_batch_ids": False,
        "ledger_rows": len(ledger_out),
        "settlement_rows": len(gw_out),
        "bank_rows": len(bank_out),
        "money_validation": "OK",
        "column_map": {
            "merchant_order_id": "order_id",
            "customer_name": "customer",
            "gross_amount": "amount",
            "net_amount": "net_amount",
            "refund_amount": "refund_amount",
            "value_date": "credit_date",
            "debit_amount": "debit_amount",
            "payout_batch": "settlement_batch_id",
        },
    }


def _rewrite_money_columns(row: dict, specs) -> dict:
    out = dict(row)
    for spec in specs:
        for alias in spec.aliases:
            if alias in out:
                out[alias] = canonical_or_zero(out.get(alias), spec)
                break
    return out


def canonicalize_engine_dir(src_dir: str, dest_dir: str) -> dict:
    """Validate money, write canonical copies, leave the source files unchanged."""
    os.makedirs(dest_dir, exist_ok=True)

    from ingest_validate import validate_money_rows, merge_reports

    ledger_rows = _read(os.path.join(src_dir, "internal_ledger.csv"))
    settle_rows = _read(os.path.join(src_dir, "settlement_report.csv"))
    bank_rows = _read(os.path.join(src_dir, "bank_statement.csv"))
    money = merge_reports(
        validate_money_rows("internal_ledger.csv", ledger_rows, "ledger"),
        validate_money_rows("settlement_report.csv", settle_rows, "gateway"),
        validate_money_rows("bank_statement.csv", bank_rows, "bank"),
    )
    raise_if_failed(money, os.path.join(dest_dir, "quarantine.json"))

    ledger_out = [normalize_ledger_row(r, normalize_ids=False) for r in ledger_rows]
    _write(os.path.join(dest_dir, "internal_ledger.csv"), ledger_out,
           ["order_id", "source_order_id", "order_date", "amount", "customer", "currency"])

    kind_by_file = {
        "settlement_report.csv": GATEWAY_MONEY,
        "bank_statement.csv": (BANK_CREDIT, BANK_DEBIT),
    }
    counts = {"internal_ledger.csv": len(ledger_out)}
    for filename, specs in kind_by_file.items():
        src = os.path.join(src_dir, filename)
        rows = _read(src)
        with open(src, newline="", encoding="utf-8") as f:
            fieldnames = list(csv.DictReader(f).fieldnames or [])
        rewritten = [_rewrite_money_columns(row, specs) for row in rows]
        _write(os.path.join(dest_dir, filename), rewritten, fieldnames)
        counts[filename] = len(rewritten)

    return {
        "source_dir": os.path.abspath(src_dir),
        "dest_dir": os.path.abspath(dest_dir),
        "adapted": False,
        "schema": "engine",
        "normalize_ids": False,
        "money_validation": "OK",
        "ledger_rows": counts.get("internal_ledger.csv", 0),
        "settlement_rows": counts.get("settlement_report.csv", 0),
        "bank_rows": counts.get("bank_statement.csv", 0),
        "column_map": {
            "merchant_order_id": "order_id",
            "customer_name": "customer",
            "gross_amount": "amount",
        },
    }


def resolve_engine_dir(data_dir: str, work_dir: Optional[str] = None,
                       normalize_ids: bool = True) -> tuple[str, dict]:
    data_dir = os.path.abspath(data_dir)
    if looks_like_engine_schema(data_dir):
        dest = work_dir or os.path.join(data_dir, ".engine_ingest")
        meta = canonicalize_engine_dir(data_dir, dest)
        return os.path.abspath(dest), meta
    if looks_like_eval_schema(data_dir):
        dest = work_dir or os.path.join(data_dir, ".engine_ingest")
        meta = adapt_eval_dir(data_dir, dest, normalize_ids=normalize_ids)
        meta["adapted"] = True
        meta["schema"] = "eval"
        return os.path.abspath(dest), meta
    missing_engine = [f for f in ENGINE_FILES
                      if not os.path.exists(os.path.join(data_dir, f))]
    raise FileNotFoundError(
        "No usable source files in {0}. Missing engine files: {1}. "
        "Expected either settlement_report.csv (engine schema) or "
        "gateway_settlement.csv (eval schema). Pass --regen only to build "
        "synthetic demo data.".format(data_dir, missing_engine)
    )
