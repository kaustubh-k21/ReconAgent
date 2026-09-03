#!/usr/bin/env python3
"""Score the current pipeline on held-out sets. Does not feed labels into the agent."""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))

import matcher  # noqa: E402
import exception_classifier as ec  # noqa: E402

DATASETS = ["easy_100", "medium_100", "hard_100"]

CAUSE_MAP = {
    "duplicate_credit": "duplicate_bank_credit",
    "duplicate_entry": "duplicate_gateway",
    "missing_settlement": "missing_settlement",
    "aged_missing_bank": "missing_bank_credit",
    "refund_partial": "refund_partial",
    "unexplained": "unexplained_variance",
    "timing_lag": "timing_difference",
    "fee_variance": "amount_mismatch",
    "tds_deduction": "tds_issue",
    "rounding": "unexplained_variance",
}


def norm(s: str) -> str:
    return (s or "").replace("-", "").replace("_", "").lower()


def adapt(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    with (src / "internal_ledger.csv").open(newline="", encoding="utf-8") as f:
        ledger = list(csv.DictReader(f))
    with (dest / "internal_ledger.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "order_date", "amount", "customer"])
        w.writeheader()
        for r in ledger:
            w.writerow({
                "order_id": r["order_id"],
                "order_date": r["order_date"],
                "amount": r["amount"],
                "customer": r.get("customer_name") or r.get("customer") or "",
            })

    with (src / "gateway_settlement.csv").open(newline="", encoding="utf-8") as f:
        gw = list(csv.DictReader(f))
    with (dest / "settlement_report.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["order_id", "settlement_date", "gross_amount", "fee", "tds",
                        "net_amount", "settlement_batch_id"],
        )
        w.writeheader()
        for r in gw:
            w.writerow({
                "order_id": r.get("merchant_order_id") or r.get("order_id") or "",
                "settlement_date": r["settlement_date"],
                "gross_amount": r["gross_amount"],
                "fee": r.get("fee") or "0",
                "tds": r.get("tds") or "0",
                "net_amount": r["net_amount"],
                "settlement_batch_id": r.get("payout_batch") or "",
            })

    with (src / "bank_statement.csv").open(newline="", encoding="utf-8") as f:
        bank = list(csv.DictReader(f))
    with (dest / "bank_statement.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["reference", "credit_date", "credit_amount"])
        w.writeheader()
        for r in bank:
            credit = r.get("credit_amount") or "0"
            debit = float(r.get("debit_amount") or 0)
            # Matcher only sees credit_amount; encode reversals as negative credit.
            if debit > 0:
                credit = str(-debit)
            w.writerow({
                "reference": r.get("reference") or "",
                "credit_date": r.get("value_date") or r.get("credit_date") or "",
                "credit_amount": credit,
            })


def load_gt(src: Path) -> list[dict]:
    with (src / "ground_truth.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def outcome_index(results: dict, classified: list[dict]):
    buckets = {}
    for rec in results["exact"]:
        buckets[rec["order_id"]] = ("MATCH", "EXACT", rec.get("matched_by_rule"), None)
    for rec in results["fuzzy"]:
        buckets[rec["order_id"]] = ("MATCH", "FUZZY", rec.get("matched_by_rule"), None)
    for rec in results.get("review") or []:
        buckets[rec["order_id"]] = ("MATCH", "REVIEW", rec.get("matched_by_rule"), None)
    for rec in results.get("pending") or []:
        buckets[rec["order_id"]] = ("PENDING", "PENDING", rec.get("matched_by_rule"), None)
    exc_by_id = {c["order_id"]: c for c in classified}
    for rec in results["exceptions"]:
        oid = rec["order_id"]
        c = exc_by_id.get(oid, {})
        buckets[oid] = ("EXCEPTION", "EXCEPTION", None, c.get("cause"))
    return buckets


def score_dataset(name: str) -> dict:
    src = ROOT / name
    tmp = Path(tempfile.mkdtemp(prefix=f"recon_eval_{name}_"))
    try:
        adapt(src, tmp)
        results = matcher.run_matching(data_dir=str(tmp))
        ids = [row["order_id"] for row in matcher.load_csv(str(tmp / "internal_ledger.csv"))]
        dupe_counts = dict(Counter(ids))
        classified = ec.classify_exceptions(results["exceptions"], dupe_counts, use_llm=False)
        buckets = outcome_index(results, classified)

        ledger = matcher.load_csv(str(tmp / "internal_ledger.csv"))
        ledger_by_norm = defaultdict(list)
        for row in ledger:
            ledger_by_norm[norm(row["order_id"])].append(row["order_id"])

        gt = load_gt(src)
        rows = []
        decision_ok = 0
        reason_ok = 0
        reason_n = 0
        by_expected_type = defaultdict(lambda: {"n": 0, "decision_ok": 0})
        by_expected_reason = defaultdict(lambda: {"n": 0, "decision_ok": 0, "reason_ok": 0})
        confusion = Counter()
        misses = []

        for g in gt:
            tid = g["transaction_id"]
            expected = g["expected_decision"]
            exp_type = g["expected_match_type"]
            exp_reason = g["expected_reason"] or ""

            candidates = ledger_by_norm.get(norm(tid), [])
            pred_oid = None
            if tid in buckets:
                pred_oid = tid
            elif candidates:
                pred_oid = candidates[0]

            if pred_oid and pred_oid in buckets:
                pred_dec, pred_type, rule, cause = buckets[pred_oid]
            else:
                found = None
                for c in candidates:
                    if c in buckets:
                        found = c
                        break
                if found:
                    pred_dec, pred_type, rule, cause = buckets[found]
                    pred_oid = found
                else:
                    pred_dec, pred_type, rule, cause = "MISSING", "MISSING", None, None

            # PENDING is not a MATCH.
            if pred_dec == "MATCH":
                mapped_dec = "MATCH"
            elif pred_dec in ("EXCEPTION", "PENDING", "MISSING"):
                mapped_dec = "EXCEPTION" if pred_dec != "MISSING" else "MISSING"
                if pred_dec == "PENDING":
                    mapped_dec = "EXCEPTION"  # closest bucket; flagged in pred_type
            else:
                mapped_dec = pred_dec

            ok = mapped_dec == expected
            if pred_dec == "MISSING":
                ok = False
            if ok:
                decision_ok += 1

            mapped_reason = CAUSE_MAP.get(cause or "", cause or "")
            r_ok = False
            if expected == "EXCEPTION":
                reason_n += 1
                r_ok = mapped_reason == exp_reason
                if r_ok:
                    reason_ok += 1

            by_expected_type[exp_type]["n"] += 1
            if ok:
                by_expected_type[exp_type]["decision_ok"] += 1
            if expected == "EXCEPTION":
                by_expected_reason[exp_reason]["n"] += 1
                if ok:
                    by_expected_reason[exp_reason]["decision_ok"] += 1
                if r_ok:
                    by_expected_reason[exp_reason]["reason_ok"] += 1

            confusion[(expected, mapped_dec if pred_dec != "PENDING" else "PENDING")] += 1

            rec = {
                "transaction_id": tid,
                "expected_decision": expected,
                "expected_match_type": exp_type,
                "expected_reason": exp_reason,
                "predicted_decision": pred_dec,
                "predicted_match_type": pred_type,
                "predicted_cause": cause,
                "matched_by_rule": rule,
                "decision_correct": ok,
            }
            rows.append(rec)
            if not ok:
                misses.append(rec)

        n = len(gt)
        pred_match = sum(1 for r in rows if r["predicted_decision"] == "MATCH")
        pred_exc = sum(1 for r in rows if r["predicted_decision"] == "EXCEPTION")
        pred_pending = sum(1 for r in rows if r["predicted_decision"] == "PENDING")
        pred_missing = sum(1 for r in rows if r["predicted_decision"] == "MISSING")

        tp = sum(1 for r in rows if r["expected_decision"] == "MATCH" and r["predicted_decision"] == "MATCH")
        fp = sum(1 for r in rows if r["expected_decision"] == "EXCEPTION" and r["predicted_decision"] == "MATCH")
        fn = sum(1 for r in rows if r["expected_decision"] == "MATCH" and r["predicted_decision"] != "MATCH")

        return {
            "dataset": name,
            "n": n,
            "pipeline_buckets": {
                "exact": len(results["exact"]),
                "fuzzy": len(results["fuzzy"]),
                "review": len(results.get("review") or []),
                "pending": len(results.get("pending") or []),
                "exceptions": len(results["exceptions"]),
            },
            "leg_summary": results.get("leg_summary"),
            "rule_counts": results.get("rule_counts"),
            "predicted_on_base_100": {
                "MATCH": pred_match,
                "EXCEPTION": pred_exc,
                "PENDING": pred_pending,
                "MISSING": pred_missing,
            },
            "decision_accuracy": round(decision_ok / n, 4),
            "decision_correct": decision_ok,
            "match_precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
            "match_recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
            "exception_reason_accuracy": round(reason_ok / reason_n, 4) if reason_n else None,
            "exception_reason_correct": reason_ok,
            "exception_reason_n": reason_n,
            "by_expected_match_type": {
                k: {
                    "n": v["n"],
                    "decision_accuracy": round(v["decision_ok"] / v["n"], 4) if v["n"] else None,
                    "decision_correct": v["decision_ok"],
                }
                for k, v in sorted(by_expected_type.items())
            },
            "by_expected_exception_reason": {
                k: {
                    "n": v["n"],
                    "decision_accuracy": round(v["decision_ok"] / v["n"], 4) if v["n"] else None,
                    "reason_accuracy": round(v["reason_ok"] / v["n"], 4) if v["n"] else None,
                }
                for k, v in sorted(by_expected_reason.items())
            },
            "confusion_expected_vs_predicted": {
                f"{a}->{b}": c for (a, b), c in sorted(confusion.items())
            },
            "false_matches": [
                {"id": m["transaction_id"], "expected_reason": m["expected_reason"],
                 "expected_type": m["expected_match_type"],
                 "predicted": m["predicted_decision"], "type": m["predicted_match_type"]}
                for m in misses if m["expected_decision"] == "EXCEPTION" and m["predicted_decision"] == "MATCH"
            ][:15],
            "missed_matches": [
                {"id": m["transaction_id"], "expected_type": m["expected_match_type"],
                 "predicted": m["predicted_decision"], "cause": m["predicted_cause"]}
                for m in misses if m["expected_decision"] == "MATCH" and m["predicted_decision"] != "MATCH"
            ][:15],
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    report = {name: score_dataset(name) for name in DATASETS}
    out = ROOT / "heldout_pipeline_results.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
