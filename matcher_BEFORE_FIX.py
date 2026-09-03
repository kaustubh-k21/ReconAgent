"""
matcher.py

Tiered reconciliation matching across three sources:
  Tier 1 - EXACT:  order appears in all three, net-of-standard-fee math checks out to the cent
  Tier 2 - FUZZY:  order appears in all three but only matches once we allow for one of a known
                   set of tolerances (fee-rate variance, TDS, sub-rupee rounding, a few days'
                   settlement/credit lag)
  Tier 3 - EXCEPTION: everything left over gets handed to the exception classifier, which
                   reasons about *why* it didn't match rather than just flagging "unmatched"

This mirrors how a real finance team actually works a rec: clear the easy 80% fast,
then spend judgment only on the genuinely ambiguous tail.
"""

import csv
from datetime import date, datetime

STANDARD_FEE_RATE = 0.02
TDS_RATE = 0.01
FEE_RATE_CANDIDATES = [0.012, 0.015, 0.02, 0.025, 0.03]
ROUNDING_TOLERANCE = 1.0  # rupees
DATE_LAG_TOLERANCE_DAYS = 7


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_sources(data_dir="data"):
    ledger = load_csv(f"{data_dir}/internal_ledger.csv")
    settlement = load_csv(f"{data_dir}/settlement_report.csv")
    bank = load_csv(f"{data_dir}/bank_statement.csv")

    settlement_by_id = {}
    for row in settlement:
        settlement_by_id.setdefault(row["order_id"], []).append(row)

    bank_by_ref = {}
    for row in bank:
        bank_by_ref.setdefault(row["reference"], []).append(row)

    return ledger, settlement_by_id, bank_by_ref


def try_exact_match(ledger_row, settlement_rows, bank_rows):
    if len(settlement_rows) != 1 or len(bank_rows) != 1:
        return None
    s = settlement_rows[0]
    b = bank_rows[0]

    amount = to_float(ledger_row["amount"])
    expected_fee = round(amount * STANDARD_FEE_RATE, 2)
    expected_net = round(amount - expected_fee, 2)
    net_amount = to_float(s["net_amount"])
    credit_amount = to_float(b["credit_amount"])

    if net_amount is None or credit_amount is None:
        return None

    if abs(net_amount - expected_net) < 0.01 and abs(credit_amount - net_amount) < 0.01:
        ledger_date = parse_date(ledger_row["order_date"])
        bank_date = parse_date(b["credit_date"])
        if (bank_date - ledger_date).days <= 3:
            return {
                "tier": "exact",
                "confidence": 1.0,
                "note": "Net-of-standard-fee math ties out across all three sources within normal settlement timing.",
            }
    return None


def try_fuzzy_match(ledger_row, settlement_rows, bank_rows):
    if len(settlement_rows) != 1 or len(bank_rows) != 1:
        return None
    s = settlement_rows[0]
    b = bank_rows[0]

    amount = to_float(ledger_row["amount"])
    gross = to_float(s["gross_amount"])
    fee = to_float(s["fee"], 0.0)
    tds = to_float(s["tds"], 0.0)
    net_amount = to_float(s["net_amount"])
    credit_amount = to_float(b["credit_amount"])

    if None in (amount, gross, net_amount, credit_amount):
        return None
    if abs(amount - gross) > 0.01:
        return None  # not even the same order amount, don't force a fuzzy match

    reasons = []

    # Does the settlement net tie out under a *different* fee rate than standard?
    implied_fee_rate = round(fee / amount, 4) if amount else None
    fee_ok = abs(net_amount - (amount - fee - tds)) < 0.01
    if fee_ok and implied_fee_rate is not None and abs(implied_fee_rate - STANDARD_FEE_RATE) > 0.001:
        reasons.append(f"fee_variance (effective rate {implied_fee_rate:.2%} vs standard {STANDARD_FEE_RATE:.0%})")

    if tds > 0.01:
        reasons.append("tds_withheld")

    # Bank credit vs settlement net: rounding, lag, or shortfall (partial refund)
    delta = round(credit_amount - net_amount, 2)
    bank_date = parse_date(b["credit_date"])
    settle_date = parse_date(s["settlement_date"])
    lag_days = (bank_date - settle_date).days

    if abs(delta) <= ROUNDING_TOLERANCE and abs(delta) > 0.0:
        reasons.append(f"rounding_diff ({delta:+.2f})")
    elif delta < -ROUNDING_TOLERANCE:
        # bank credited materially less than settlement said -> likely partial refund
        return None  # too big a gap to call "fuzzy" - send to exception classifier
    elif 0 < lag_days <= DATE_LAG_TOLERANCE_DAYS:
        reasons.append(f"settlement_to_bank_lag ({lag_days}d)")

    if not fee_ok and not reasons:
        return None

    if reasons:
        return {
            "tier": "fuzzy",
            "confidence": 0.85 if len(reasons) == 1 else 0.75,
            "note": "Matched after allowing for: " + "; ".join(reasons),
        }
    return None


def run_matching(data_dir="data"):
    ledger, settlement_by_id, bank_by_ref = load_sources(data_dir)

    order_counts = {}
    for row in ledger:
        order_counts[row["order_id"]] = order_counts.get(row["order_id"], 0) + 1

    results = {"exact": [], "fuzzy": [], "exceptions": []}
    seen_duplicate_ids = set()

    for row in ledger:
        oid = row["order_id"]
        s_rows = settlement_by_id.get(oid, [])
        b_rows = bank_by_ref.get(oid, [])

        record = {
            "order_id": oid,
            "ledger": row,
            "settlement": s_rows,
            "bank": b_rows,
        }

        if order_counts[oid] > 1:
            if oid not in seen_duplicate_ids:
                seen_duplicate_ids.add(oid)
                results["exceptions"].append(record)
            continue

        exact = try_exact_match(row, s_rows, b_rows)
        if exact:
            results["exact"].append({**record, **exact})
            continue

        fuzzy = try_fuzzy_match(row, s_rows, b_rows)
        if fuzzy:
            results["fuzzy"].append({**record, **fuzzy})
            continue

        results["exceptions"].append(record)

    return results


if __name__ == "__main__":
    r = run_matching()
    total = len(r["exact"]) + len(r["fuzzy"]) + len(r["exceptions"])
    print(f"Exact: {len(r['exact'])}  Fuzzy: {len(r['fuzzy'])}  Exceptions: {len(r['exceptions'])}  "
          f"Match rate: {(len(r['exact']) + len(r['fuzzy'])) / total:.1%}")
