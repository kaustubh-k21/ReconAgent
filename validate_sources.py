"""Fail bad CSVs before matching so they don't become fake exceptions."""

import csv
import os
from datetime import datetime

from ingest_validate import STATUS_FAILED, validate_engine_dir


REQUIRED = {
    "internal_ledger.csv": ["order_id", "order_date", "amount"],
    "settlement_report.csv": ["order_id", "settlement_date", "gross_amount", "net_amount"],
    "bank_statement.csv": ["reference", "credit_date", "credit_amount"],
}

NULL_RATE_LIMIT = 0.05  # 5% null/blank on join keys


def _parse_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def validate_sources(data_dir="data"):
    """Return (ok: bool, errors: list[str])."""
    errors = []

    for filename, required_cols in REQUIRED.items():
        path = os.path.join(data_dir, filename)
        if not os.path.exists(path):
            errors.append(f"missing file: {filename}")
            continue

        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                errors.append(f"{filename}: empty or unreadable header")
                continue

            missing_cols = [c for c in required_cols if c not in reader.fieldnames]
            if missing_cols:
                errors.append(f"{filename}: missing columns {missing_cols}")
                continue

            rows = list(reader)
            if not rows:
                errors.append(f"{filename}: no data rows")
                continue

            # Bank reference may be blank (UTR-only narrations). Ledger/settlement IDs may not.
            if filename != "bank_statement.csv":
                key = "order_id"
                blank = sum(1 for r in rows if not (r.get(key) or "").strip())
                rate = blank / len(rows)
                if rate > NULL_RATE_LIMIT:
                    errors.append(
                        f"{filename}: {key} null/blank rate {rate:.0%} exceeds {NULL_RATE_LIMIT:.0%} "
                        f"({blank}/{len(rows)} rows)"
                    )

            date_col = next((c for c in ("order_date", "settlement_date", "credit_date")
                             if c in reader.fieldnames), None)
            if date_col:
                bad = [i for i, r in enumerate(rows, start=2)
                       if (r.get(date_col) or "").strip() and not _parse_date(r[date_col].strip())]
                if bad:
                    sample = bad[:5]
                    errors.append(
                        f"{filename}: {len(bad)} rows with unparseable {date_col} "
                        f"(e.g. line(s) {sample}; expected YYYY-MM-DD)"
                    )

    money = validate_engine_dir(data_dir)
    if not money.ok:
        errors.append(STATUS_FAILED)
        for rec in money.quarantine:
            errors.append(
                f"{rec.file} row {rec.row}, {rec.field}: {rec.error} value={rec.value!r}"
            )

    return (len(errors) == 0, errors)


if __name__ == "__main__":
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    ok, errs = validate_sources(data_dir)
    if ok:
        print(f"OK: sources in {data_dir} passed validation")
    else:
        if errs and errs[0] == STATUS_FAILED:
            print("VALIDATION_FAILED:")
        else:
            print("VALIDATION FAILED:")
        for e in errs:
            if e == STATUS_FAILED:
                continue
            print(f"  - {e}")
        sys.exit(1)
