"""Reshape tough100 into this project's three-file schema.

Gateway rows are collapsed per order (CAPTURED-only means never settled).
Bank order IDs are parsed from description text. TDS is only a shortfall
in this dataset — the adapter does not invent a tds column.
"""

import argparse
import csv
import os
import re
from collections import defaultdict

ORDER_ID_RE = re.compile(r"ORD-\d+")


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def adapt_ledger(rows):
    out = []
    for r in rows:
        out.append({
            "order_id": r["order_id"],
            "order_date": r["order_date"],
            "amount": r["ledger_amount"],
            "customer": r["customer_name"],
        })
    return out


def adapt_settlement(rows, valid_order_ids):
    by_order = defaultdict(list)
    for r in rows:
        by_order[r["order_id"]].append(r)

    out = []
    skipped_decoys = 0
    for order_id, legs in by_order.items():
        if order_id not in valid_order_ids:
            skipped_decoys += 1
            continue  # decoy order_id with no ledger entry -- not this project's concern to fabricate

        captured = next((r for r in legs if r["gateway_status"] == "CAPTURED"), None)
        settled = [r for r in legs if r["gateway_status"] == "SETTLED"]
        settled_parts = [r for r in legs if r["gateway_status"] == "SETTLED_PART"]

        if not captured and not settled and not settled_parts:
            continue

        gross = to_float(captured["gateway_amount"]) if captured else None
        fee = to_float(captured["gateway_fee"]) if captured else 0.0
        settle_date = captured["gateway_date"] if captured else ""

        if settled:
            net = to_float(settled[0]["gateway_amount"])
            settle_date = settled[0]["gateway_date"]
        elif settled_parts:
            net = sum(to_float(r["gateway_amount"]) for r in settled_parts)
            settle_date = max(r["gateway_date"] for r in settled_parts)
        else:
            # only a CAPTURED leg exists -- genuinely never settled, emit no row
            continue

        if gross is None:
            gross = net  # no CAPTURED leg seen, fall back to settled amount as gross

        out.append({
            "order_id": order_id,
            "settlement_date": settle_date,
            "gross_amount": round(gross, 2),
            "fee": round(fee, 2),
            "tds": 0.0,  # not present as a distinct field in this dataset -- see module docstring
            "net_amount": round(net, 2),
        })

    return out, skipped_decoys


def adapt_bank(rows):
    out = []
    unparsed = 0
    for r in rows:
        m = ORDER_ID_RE.search(r["description"])
        if not m:
            unparsed += 1
            continue
        out.append({
            "reference": m.group(),
            "credit_date": r["posting_date"],
            "credit_amount": r["credit_amount"],
        })
    return out, unparsed


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="folder containing the extracted tough100 CSVs")
    ap.add_argument("--out", default="batches/tough100")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    ledger_raw = load_csv(os.path.join(args.src, "internal_ledger.csv"))
    settlement_raw = load_csv(os.path.join(args.src, "gateway_settlement.csv"))
    bank_raw = load_csv(os.path.join(args.src, "bank_statement.csv"))

    ledger = adapt_ledger(ledger_raw)
    valid_order_ids = {r["order_id"] for r in ledger}

    settlement, skipped_decoys = adapt_settlement(settlement_raw, valid_order_ids)
    bank, unparsed_bank = adapt_bank(bank_raw)

    write_csv(f"{args.out}/internal_ledger.csv", ledger,
              ["order_id", "order_date", "amount", "customer"])
    write_csv(f"{args.out}/settlement_report.csv", settlement,
              ["order_id", "settlement_date", "gross_amount", "fee", "tds", "net_amount"])
    write_csv(f"{args.out}/bank_statement.csv", bank,
              ["reference", "credit_date", "credit_amount"])

    print(f"{len(ledger)} ledger rows, {len(settlement)} settlement rows (collapsed from "
          f"{len(settlement_raw)} raw lifecycle rows, {skipped_decoys} decoy order(s) skipped), "
          f"{len(bank)} bank rows ({unparsed_bank} unparseable descriptions skipped) -> {args.out}/")
    print("No ground_truth.csv written -- this dataset's ground truth uses a different taxonomy "
          "than the project's own synthetic causes. Use compare_tough100.py for an honest, "
          "decision-level comparison instead of forcing a fake cause-label mapping.")


if __name__ == "__main__":
    main()
