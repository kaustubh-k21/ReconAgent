"""CSV input contract for judges and CLI users. Clear, actionable errors."""

from __future__ import annotations

import csv
import os
from typing import Optional


ENGINE_FILES = (
    "internal_ledger.csv",
    "settlement_report.csv",
    "bank_statement.csv",
)

EVAL_FILES = (
    "internal_ledger.csv",
    "gateway_settlement.csv",
    "bank_statement.csv",
)

ENGINE_COLUMNS = {
    "internal_ledger.csv": {
        "required": ("order_id",),
        "recommended": ("order_date", "amount", "customer"),
    },
    "settlement_report.csv": {
        "required": ("order_id", "gross_amount", "net_amount"),
        "recommended": ("settlement_date", "fee", "tds", "refund_amount", "settlement_batch_id"),
    },
    "bank_statement.csv": {
        "required": ("reference", "credit_amount"),
        "recommended": ("credit_date", "debit_amount", "utr", "narration"),
    },
}

EVAL_COLUMNS = {
    "internal_ledger.csv": {
        "required": (),
        "id_aliases": ("order_id", "merchant_order_id"),
        "recommended": ("order_date", "amount", "customer_name"),
    },
    "gateway_settlement.csv": {
        "required": ("gross_amount", "net_amount"),
        "id_aliases": ("merchant_order_id", "order_id"),
        "recommended": (
            "settlement_date", "fee", "tds", "refund_amount", "payout_batch",
        ),
    },
    "bank_statement.csv": {
        "required": ("credit_amount",),
        "recommended": ("value_date", "credit_date", "debit_amount", "reference", "utr", "narration"),
    },
}


class SchemaError(ValueError):
    """Human-readable CSV layout / column problems."""

    def __init__(self, messages: list[str]):
        self.messages = list(messages)
        super().__init__("\n".join(self.messages))


def contract_text() -> str:
    return """CSV input contract
==================

Two layouts are accepted (pick one folder; do not mix gateway filenames).

1) Engine schema
   Files:
     internal_ledger.csv
     settlement_report.csv
     bank_statement.csv
   Key columns:
     ledger:     order_id, order_date, amount, customer
     settlement: order_id, settlement_date, gross_amount, fee, tds, refund_amount, net_amount
     bank:       reference, credit_date, credit_amount [, debit_amount, narration]

2) Eval / held-out schema
   Files:
     internal_ledger.csv
     gateway_settlement.csv
     bank_statement.csv
   Key columns:
     ledger:  order_id | merchant_order_id, order_date, amount, customer_name
     gateway: merchant_order_id, settlement_date, gross_amount, fee, tds, refund_amount, net_amount
     bank:    value_date | credit_date, credit_amount, debit_amount, reference, narration

Optional:
  recon_meta.json  →  { "as_of": "YYYY-MM-DD", "sla_days": 7 }
  (Needed for within-SLA pending bank credits. Without it, missing credits age out.)

Run:
  python3 main.py --data-dir PATH_TO_FOLDER
  python3 evaluation_datasets/run_heldout.py
"""


def _header(path: str) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or [])


def _has_any(headers: set[str], names: tuple[str, ...]) -> bool:
    return any(n in headers for n in names)


def _check_file(path: str, label: str, spec: dict) -> list[str]:
    errs: list[str] = []
    if not os.path.exists(path):
        errs.append(f"Missing file: {label}")
        return errs
    try:
        headers = _header(path)
    except Exception as e:
        errs.append(f"Cannot read {label}: {e}")
        return errs
    if not headers:
        errs.append(f"{label} has no header row")
        return errs
    header_set = set(headers)
    id_aliases = spec.get("id_aliases")
    if id_aliases and not _has_any(header_set, id_aliases):
        errs.append(
            f"{label}: missing join key — need one of {list(id_aliases)}; "
            f"found columns: {headers}"
        )
    for col in spec.get("required", ()):
        if col not in header_set:
            errs.append(
                f"{label}: missing required column '{col}'; "
                f"found columns: {headers}"
            )
    return errs


def detect_layout(data_dir: str) -> Optional[str]:
    engine_ok = all(os.path.exists(os.path.join(data_dir, f)) for f in ENGINE_FILES)
    eval_ok = all(os.path.exists(os.path.join(data_dir, f)) for f in EVAL_FILES)
    if engine_ok:
        return "engine"
    if eval_ok:
        return "eval"
    return None


def validate_source_layout(data_dir: str) -> None:
    """Raise SchemaError with actionable messages if the folder is not usable."""
    data_dir = os.path.abspath(data_dir)
    if not os.path.isdir(data_dir):
        raise SchemaError([
            f"Not a directory: {data_dir}",
            "Pass --data-dir pointing at a folder of CSVs.",
            "",
            contract_text(),
        ])

    layout = detect_layout(data_dir)
    present = [f for f in sorted(os.listdir(data_dir)) if f.endswith(".csv")]
    if layout is None:
        msgs = [
            f"No usable CSV layout in {data_dir}.",
            f"CSV files found: {present or '(none)'}.",
            "",
            "Expected ENGINE files: " + ", ".join(ENGINE_FILES),
            "   or EVAL files:     " + ", ".join(EVAL_FILES),
            "",
            contract_text(),
        ]
        if "settlement.csv" in present or "gateway.csv" in present:
            msgs.insert(
                2,
                "Hint: settlement file must be named settlement_report.csv or gateway_settlement.csv.",
            )
        raise SchemaError(msgs)

    errs: list[str] = []
    if layout == "engine":
        for fname, spec in ENGINE_COLUMNS.items():
            errs.extend(_check_file(os.path.join(data_dir, fname), fname, spec))
    else:
        for fname, spec in EVAL_COLUMNS.items():
            errs.extend(_check_file(os.path.join(data_dir, fname), fname, spec))

    if errs:
        errs.append("")
        errs.append(f"Detected layout: {layout}")
        errs.append("Fix the columns above, then re-run.")
        errs.append("")
        errs.append(contract_text())
        raise SchemaError(errs)
