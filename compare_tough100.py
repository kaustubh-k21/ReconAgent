"""Compare matcher decisions to tough100 ground truth. Cause labels are not auto-scored."""

import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

import matcher
import exception_classifier as ec


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="batches/tough100", help="adapted dataset folder")
    ap.add_argument("--src", required=True, help="original tough100 folder (for ground_truth.csv + transaction_id->order_id mapping)")
    ap.add_argument("--llm", action="store_true", help="use Gemini for exception classification instead of rule-based")
    args = ap.parse_args()

    ledger_raw = load_csv(os.path.join(args.src, "internal_ledger.csv"))
    txn_to_order = {r["transaction_id"]: r["order_id"] for r in ledger_raw}

    ground_truth = {r["transaction_id"]: r for r in load_csv(os.path.join(args.src, "ground_truth.csv"))}

    results = matcher.run_matching(data_dir=args.data_dir)
    matched_order_ids = {r["order_id"] for r in results["exact"] + results["fuzzy"]}
    exception_records = results["exceptions"]

    ledger = load_csv(os.path.join(args.data_dir, "internal_ledger.csv"))
    from collections import Counter
    dupe_counts = dict(Counter(r["order_id"] for r in ledger))

    classified = ec.classify_exceptions(exception_records, dupe_counts, use_llm=args.llm)
    classified_by_order = {c["order_id"]: c for c in classified}

    correct = 0
    total = 0
    confusion = defaultdict(int)
    misses = []

    for txn_id, gt in ground_truth.items():
        order_id = txn_to_order.get(txn_id)
        if order_id is None:
            continue
        total += 1
        my_decision = "MATCH" if order_id in matched_order_ids else "EXCEPTION"
        expected = gt["expected_decision"]
        confusion[(expected, my_decision)] += 1
        if my_decision == expected:
            correct += 1
        else:
            misses.append((order_id, expected, my_decision, gt["expected_reason"]))

    print(f"Decision-level accuracy: {correct}/{total} = {correct/total:.1%}\n")
    print("Confusion (expected -> got):")
    for (exp, got), n in sorted(confusion.items()):
        print(f"  {exp:<10} -> {got:<10} : {n}")

    if misses:
        print(f"\n{len(misses)} decision-level misses:")
        for order_id, expected, got, reason in misses:
            print(f"  {order_id}: expected {expected} ({reason}), got {got}")

    print(f"\n{len(classified)} exceptions classified. Cause vs expected_reason (manual comparison, not auto-scored):\n")
    print(f"{'Order':<12}{'My cause':<20}{'Conf':<7}{'Expected reason (different taxonomy)':<45}")
    print("-" * 90)
    for order_id, c in sorted(classified_by_order.items()):
        gt_row = next((gt for tid, gt in ground_truth.items() if txn_to_order.get(tid) == order_id), None)
        expected_reason = gt_row["expected_reason"] if gt_row else "(no ground truth row)"
        print(f"{order_id:<12}{c['cause']:<20}{c['confidence']:<7.0%}{expected_reason:<45}")


if __name__ == "__main__":
    main()
