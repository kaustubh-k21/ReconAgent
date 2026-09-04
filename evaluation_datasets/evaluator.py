#!/usr/bin/env python3
"""Score predictions.json against hidden ground_truth.csv. Does not run the matcher."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))

from ingest_adapter import canonicalize_id  # noqa: E402

MATCH_TYPES = {"EXACT", "FUZZY", "TOLERANCE", "MANY_TO_ONE", "ONE_TO_MANY"}


def load_gt(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_predictions(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    return payload.get("predictions") or []


def _index_preds(preds: list[dict]) -> tuple[dict, dict]:
    by_oid = {}
    by_canon = {}
    for p in preds:
        oid = p.get("order_id") or ""
        cid = p.get("canonical_id") or canonicalize_id(oid)
        if oid:
            by_oid[oid] = p
        if cid:
            by_canon[cid] = p
    return by_oid, by_canon


def lookup(tid: str, by_oid: dict, by_canon: dict):
    if tid in by_oid:
        return by_oid[tid]
    c = canonicalize_id(tid)
    if c in by_canon:
        return by_canon[c]
    if c in by_oid:
        return by_oid[c]
    return None


def evaluate(gt_rows: list[dict], preds: list[dict]) -> dict:
    by_oid, by_canon = _index_preds(preds)

    correct_match = 0
    correct_exception = 0
    false_match = []
    false_exception = []
    pending_on_match = []
    pending_on_exception = []
    review_on_match = []
    review_on_exception = []
    missing = []
    rows = []

    type_n = type_ok = 0
    reason_n = reason_ok = 0
    type_by = defaultdict(lambda: {"n": 0, "ok": 0})
    reason_by = defaultdict(lambda: {"n": 0, "decision_ok": 0, "reason_ok": 0})
    confusion = Counter()

    for g in gt_rows:
        tid = g["transaction_id"]
        expected = g["expected_decision"]
        exp_type = g.get("expected_match_type") or ""
        exp_reason = g.get("expected_reason") or ""
        pred = lookup(tid, by_oid, by_canon)

        if pred is None:
            pred_dec = "MISSING"
            pred_type = None
            pred_reason = None
            pred_suspicion = None
            missing.append(tid)
        else:
            pred_dec = pred.get("decision") or "MISSING"
            pred_type = pred.get("match_type")
            pred_reason = pred.get("exception_reason")
            pred_suspicion = pred.get("link_suspicion")

        confusion[(expected, pred_dec)] += 1

        decision_ok = False
        if expected == "MATCH" and pred_dec == "MATCH":
            decision_ok = True
            correct_match += 1
        elif expected == "EXCEPTION" and pred_dec == "EXCEPTION":
            decision_ok = True
            correct_exception += 1
        elif expected == "EXCEPTION" and pred_dec == "MATCH":
            false_match.append({
                "transaction_id": tid,
                "expected_reason": exp_reason,
                "expected_match_type": exp_type,
                "predicted_match_type": pred_type,
                "predicted_reason": pred_reason,
                "predicted_link_suspicion": pred_suspicion,
            })
        elif expected == "MATCH" and pred_dec == "EXCEPTION":
            false_exception.append({
                "transaction_id": tid,
                "expected_match_type": exp_type,
                "predicted_reason": pred_reason,
            })
        elif expected == "MATCH" and pred_dec == "PENDING":
            pending_on_match.append(tid)
        elif expected == "EXCEPTION" and pred_dec == "PENDING":
            pending_on_exception.append({
                "transaction_id": tid,
                "expected_reason": exp_reason,
            })
        elif expected == "MATCH" and pred_dec == "REVIEW":
            review_on_match.append(tid)
        elif expected == "EXCEPTION" and pred_dec == "REVIEW":
            review_on_exception.append({
                "transaction_id": tid,
                "expected_reason": exp_reason,
            })

        if expected == "MATCH" and pred_dec == "MATCH" and exp_type in MATCH_TYPES:
            type_n += 1
            type_by[exp_type]["n"] += 1
            ok = pred_type == exp_type
            # FUZZY and TOLERANCE are related; they still must match exactly here.
            if ok:
                type_ok += 1
                type_by[exp_type]["ok"] += 1

        if expected == "EXCEPTION":
            reason_by[exp_reason]["n"] += 1
            if pred_dec == "EXCEPTION":
                reason_by[exp_reason]["decision_ok"] += 1
                reason_n += 1
                # Structural cause or lookalike suspicion may satisfy the label.
                # wrong_transaction ↔ ambiguous_match are the same "do not auto-link" family.
                lookalike = {"wrong_transaction", "ambiguous_match"}
                reason_hit = (
                    pred_reason == exp_reason
                    or (pred_suspicion is not None and pred_suspicion == exp_reason)
                    or (
                        exp_reason in lookalike
                        and (
                            (pred_reason in lookalike)
                            or (pred_suspicion in lookalike)
                        )
                    )
                )
                if reason_hit:
                    reason_ok += 1
                    reason_by[exp_reason]["reason_ok"] += 1

        rows.append({
            "transaction_id": tid,
            "expected_decision": expected,
            "predicted_decision": pred_dec,
            "expected_match_type": exp_type,
            "predicted_match_type": pred_type,
            "expected_reason": exp_reason,
            "predicted_reason": pred_reason,
            "predicted_link_suspicion": pred_suspicion,
            "decision_correct": decision_ok,
        })

    n = len(gt_rows)
    tp = correct_match
    fp = len(false_match)
    fn = len(false_exception) + len(pending_on_match) + len(review_on_match) + sum(
        1 for r in rows if r["expected_decision"] == "MATCH" and r["predicted_decision"] == "MISSING"
    )

    return {
        "n": n,
        "harm": {
            "false_match_count": fp,
            "false_matches": false_match,
            "note": "False MATCH is the dangerous error: a break was auto-cleared.",
        },
        "false_exception_count": len(false_exception),
        "false_exceptions_sample": false_exception[:20],
        "correct_matches": correct_match,
        "correct_exceptions": correct_exception,
        "pending_when_match_expected": pending_on_match,
        "pending_when_exception_expected": pending_on_exception,
        "review_when_match_expected": review_on_match,
        "review_when_exception_expected": review_on_exception,
        "missing_predictions": missing,
        "decision_accuracy": round((correct_match + correct_exception) / n, 4) if n else None,
        "match_precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
        "match_recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "match_type_accuracy_given_both_match": round(type_ok / type_n, 4) if type_n else None,
        "match_type_n": type_n,
        "match_type_by_expected": {
            k: {"n": v["n"], "accuracy": round(v["ok"] / v["n"], 4) if v["n"] else None}
            for k, v in sorted(type_by.items())
        },
        "exception_reason_accuracy_given_both_exception": round(reason_ok / reason_n, 4) if reason_n else None,
        "exception_reason_n": reason_n,
        "exception_reason_by_expected": {
            k: {
                "n": v["n"],
                "caught_as_exception": v["decision_ok"],
                "reason_accuracy": round(v["reason_ok"] / v["n"], 4) if v["n"] else None,
            }
            for k, v in sorted(reason_by.items())
        },
        "confusion_expected_vs_predicted": {
            f"{a}->{b}": c for (a, b), c in sorted(confusion.items())
        },
        "per_row": rows,
    }


def evaluate_files(gt_path: str, pred_path: str) -> dict:
    gt = load_gt(Path(gt_path))
    preds_path = Path(pred_path)
    payload = json.loads(preds_path.read_text(encoding="utf-8"))
    preds = payload if isinstance(payload, list) else payload.get("predictions") or []
    report = evaluate(gt, preds)
    report["ground_truth"] = str(Path(gt_path).resolve())
    report["predictions"] = str(preds_path.resolve())
    if isinstance(payload, dict):
        report["agent_meta"] = {
            k: payload.get(k) for k in ("dataset", "adapter", "classifier_mode", "schema_version")
            if k in payload
        }
    return report


def main():
    import argparse
    p = argparse.ArgumentParser(description="Score predictions.json against hidden ground_truth.csv")
    p.add_argument("--ground-truth", required=True)
    p.add_argument("--predictions", required=True)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    report = evaluate_files(args.ground_truth, args.predictions)
    slim = {k: v for k, v in report.items() if k != "per_row"}
    print(json.dumps(slim, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
