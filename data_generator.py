"""Synthetic ledger, settlement, and bank files with a known cause per row."""

import csv
import os
import random
from datetime import date, timedelta

STANDARD_FEE_RATE = 0.02
TDS_RATE = 0.01
SLA_DAYS = 7

CAUSES = [
    "clean",
    "timing_lag",
    "fee_variance",
    "tds_deduction",
    "rounding",
    "duplicate_entry",
    "duplicate_credit",
    "missing_settlement",
    "pending_bank",
    "aged_missing_bank",
    "refund_partial",
    "batch_settlement",
]

# Mostly clean/tolerance; enough of each other cause for demos.
CAUSE_WEIGHTS = [40, 7, 6, 6, 6, 5, 5, 5, 5, 5, 5, 5]

BASE_DATE = date(2026, 7, 1)


def rupees(x):
    return round(x, 2)


def gen_dataset(n_transactions, as_of_date=None):
    """as_of_date is 'today' for pending vs aged-missing bank."""
    if as_of_date is None:
        as_of_date = BASE_DATE + timedelta(days=35)

    ledger_rows = []
    settlement_rows = []
    bank_rows = []
    ground_truth = []

    # Pre-pick causes; batch_settlement rows are deferred into groups
    causes = random.choices(CAUSES, weights=CAUSE_WEIGHTS, k=n_transactions)
    batch_queue = []  # (index, order_meta) for batch_settlement
    non_batch = []

    for i, cause in enumerate(causes):
        if cause == "batch_settlement":
            batch_queue.append(i)
        else:
            non_batch.append((i, cause))

    def emit_order(i, cause, batch_id=None, emit_individual_bank=True):
        order_id = f"ORD{1000 + i}"
        order_date = BASE_DATE + timedelta(days=random.randint(0, 27))
        amount = rupees(random.uniform(500, 45000))
        customer = f"cust_{random.randint(1000, 9999)}"

        fee_rate = STANDARD_FEE_RATE
        tds_applied = False
        settle_date_offset = random.randint(1, 2)
        bank_date_offset = 0
        settlement_net = None
        bank_credit = None
        refund_amount = 0.0
        emit_settlement = True
        emit_bank = emit_individual_bank
        duplicate = False
        duplicate_bank = False

        if cause == "clean":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            bank_credit = settlement_net

        elif cause == "timing_lag":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            bank_credit = settlement_net
            bank_date_offset = random.randint(3, 6)

        elif cause == "fee_variance":
            fee_rate = random.choice([0.012, 0.015, 0.025, 0.03])
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            bank_credit = settlement_net

        elif cause == "tds_deduction":
            fee = rupees(amount * STANDARD_FEE_RATE)
            tds = rupees(fee * TDS_RATE)
            settlement_net = rupees(amount - fee - tds)
            bank_credit = settlement_net
            tds_applied = True

        elif cause == "rounding":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            bank_credit = rupees(settlement_net + random.choice([-0.5, -0.3, 0.4, 0.6]))

        elif cause == "duplicate_entry":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            bank_credit = settlement_net
            duplicate = True

        elif cause == "duplicate_credit":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            bank_credit = settlement_net
            duplicate_bank = True

        elif cause == "missing_settlement":
            emit_settlement = False
            emit_bank = False

        elif cause == "pending_bank":
            # Settlement recent enough that bank not arriving yet is still within SLA
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            emit_bank = False
            # Place settlement so (as_of - settle_date) <= SLA
            days_ago = random.randint(0, SLA_DAYS - 1)
            order_date = as_of_date - timedelta(days=days_ago + settle_date_offset)

        elif cause == "aged_missing_bank":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            emit_bank = False
            # Settlement older than SLA
            days_ago = random.randint(SLA_DAYS + 1, SLA_DAYS + 10)
            order_date = as_of_date - timedelta(days=days_ago + settle_date_offset)

        elif cause == "refund_partial":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            refund_amount = rupees(settlement_net * random.uniform(0.2, 0.6))
            bank_credit = rupees(settlement_net - refund_amount)

        elif cause == "batch_settlement":
            fee = rupees(amount * fee_rate)
            settlement_net = rupees(amount - fee)
            # Individual bank row omitted; batch bank row emitted later
            emit_bank = False
            bank_credit = settlement_net

        ledger_rows.append({
            "order_id": order_id,
            "order_date": order_date.isoformat(),
            "amount": amount,
            "customer": customer,
        })
        if duplicate:
            ledger_rows.append({
                "order_id": order_id,
                "order_date": order_date.isoformat(),
                "amount": amount,
                "customer": customer,
            })

        settle_date = None
        if emit_settlement:
            settle_date = order_date + timedelta(days=settle_date_offset)
            settlement_rows.append({
                "order_id": order_id,
                "settlement_date": settle_date.isoformat(),
                "gross_amount": amount,
                "fee": rupees(amount * fee_rate),
                "tds": rupees(amount * fee_rate * TDS_RATE) if tds_applied else 0.0,
                "net_amount": settlement_net,
                "settlement_batch_id": batch_id or "",
            })

        if emit_bank and bank_credit is not None:
            bank_date = order_date + timedelta(days=settle_date_offset + bank_date_offset)
            bank_rows.append({
                "reference": order_id,
                "credit_date": bank_date.isoformat(),
                "credit_amount": bank_credit,
            })
            if duplicate_bank:
                bank_rows.append({
                    "reference": order_id,
                    "credit_date": bank_date.isoformat(),
                    "credit_amount": bank_credit,
                })

        ground_truth.append({
            "order_id": order_id,
            "true_cause": cause,
            "ledger_amount": amount,
            "settlement_net": settlement_net if settlement_net is not None else "",
            "bank_credit": bank_credit if (emit_bank and bank_credit is not None) else (
                settlement_net if cause == "batch_settlement" else ""
            ),
            "refund_amount": refund_amount,
            "settlement_batch_id": batch_id or "",
        })

        return {
            "order_id": order_id,
            "settlement_net": settlement_net,
            "settle_date": settle_date,
            "amount": amount,
        }

    for i, cause in non_batch:
        emit_order(i, cause)

    # Group batch_settlement indices into batches of 3–6
    random.shuffle(batch_queue)
    batch_num = 0
    idx = 0
    while idx < len(batch_queue):
        size = min(random.randint(3, 6), len(batch_queue) - idx)
        # Need at least 2 for a meaningful N:1; if leftover 1, emit as clean 1:1
        if size == 1:
            emit_order(batch_queue[idx], "clean")
            idx += 1
            continue

        batch_id = f"BATCH{20260700 + batch_num}"
        batch_num += 1
        members = []
        for j in range(size):
            meta = emit_order(batch_queue[idx + j], "batch_settlement", batch_id=batch_id,
                              emit_individual_bank=False)
            members.append(meta)
        idx += size

        total_net = rupees(sum(m["settlement_net"] for m in members))
        # Use the latest settle date in the batch as credit date
        settle_dates = [m["settle_date"] for m in members if m["settle_date"]]
        credit_date = max(settle_dates) if settle_dates else as_of_date
        bank_rows.append({
            "reference": batch_id,
            "credit_date": credit_date.isoformat(),
            "credit_amount": total_net,
        })

    return ledger_rows, settlement_rows, bank_rows, ground_truth, as_of_date


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(out_dir="data", n_transactions=70, seed=42):
    import json
    os.makedirs(out_dir, exist_ok=True)
    if seed is not None:
        random.seed(seed)
    ledger, settlement, bank, truth, as_of = gen_dataset(n_transactions)

    write_csv(f"{out_dir}/internal_ledger.csv", ledger,
              ["order_id", "order_date", "amount", "customer"])
    write_csv(f"{out_dir}/settlement_report.csv", settlement,
              ["order_id", "settlement_date", "gross_amount", "fee", "tds",
               "net_amount", "settlement_batch_id"])
    write_csv(f"{out_dir}/bank_statement.csv", bank,
              ["reference", "credit_date", "credit_amount"])
    write_csv(f"{out_dir}/ground_truth.csv", truth,
              ["order_id", "true_cause", "ledger_amount", "settlement_net",
               "bank_credit", "refund_amount", "settlement_batch_id"])
    with open(f"{out_dir}/recon_meta.json", "w") as f:
        json.dump({"as_of": as_of.isoformat(), "sla_days": SLA_DAYS, "seed": seed,
                   "n_transactions": n_transactions}, f, indent=2)

    print(f"Generated {len(ledger)} ledger rows, {len(settlement)} settlement rows, "
          f"{len(bank)} bank rows -> {out_dir}/ (as_of={as_of.isoformat()})")


if __name__ == "__main__":
    main()
