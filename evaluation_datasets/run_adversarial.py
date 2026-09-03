#!/usr/bin/env python3
"""Adversarial protocol. Does not modify matcher, classifier, or held-out datasets."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
ADV = ROOT / "adversarial"
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(ROOT))

from ingest_adapter import adapt_eval_dir, canonicalize_id  # noqa: E402
from ingest_validate import IngestValidationFailed  # noqa: E402
from evaluator import evaluate_files, load_gt, load_predictions, lookup  # noqa: E402
from run_heldout import run_agent  # noqa: E402
from predictions import build_predictions  # noqa: E402

WORK = ROOT / "_work" / "adversarial"


def suite_names() -> list[str]:
    index_path = ADV / "index.json"
    if index_path.exists():
        return list(json.loads(index_path.read_text(encoding="utf-8"))["suites"])
    return sorted(p.name for p in ADV.iterdir() if p.is_dir() and (p / "internal_ledger.csv").exists())


def _copy_declared_as_of(src: Path, engine: Path) -> bool:
    meta = src / "recon_meta.json"
    if not meta.exists():
        return False
    shutil.copy2(meta, engine / "recon_meta.json")
    return True


def score_pending(gt_path: Path, pred_path: Path) -> dict:
    """PENDING is not a first-class expected state in evaluator.py — score it here."""
    gt = load_gt(gt_path)
    preds = load_predictions(pred_path)
    by_oid = {p.get("order_id"): p for p in preds}
    by_canon = {p.get("canonical_id") or canonicalize_id(p.get("order_id") or ""): p for p in preds}
    expected = [g for g in gt if g.get("expected_decision") == "PENDING"]
    ok = 0
    misses = []
    for g in expected:
        pred = lookup(g["transaction_id"], by_oid, by_canon)
        dec = (pred or {}).get("decision")
        if dec == "PENDING":
            ok += 1
        else:
            misses.append({"transaction_id": g["transaction_id"], "predicted": dec})
    return {
        "pending_expected": len(expected),
        "pending_correct": ok,
        "pending_accuracy": round(ok / len(expected), 4) if expected else None,
        "pending_misses": misses,
    }


def run_matching_suite(name: str, use_ml: bool) -> dict:
    src = ADV / name
    work = WORK / name
    if work.exists():
        shutil.rmtree(work)
    engine = work / "engine"
    engine.mkdir(parents=True)

    adapter_meta = adapt_eval_dir(str(src), str(engine), normalize_ids=True)
    declared_as_of = _copy_declared_as_of(src, engine)
    results, classified, mode = run_agent(engine, use_ml=use_ml)
    preds = build_predictions(results, classified)
    pred_payload = {
        "schema_version": 1,
        "dataset": name,
        "protocol": "adversarial",
        "classifier_mode": mode,
        "adapter": adapter_meta,
        "declared_as_of": declared_as_of,
        "generated_at": datetime.now().isoformat(),
        "saw_ground_truth": False,
        "saw_dataset_metadata": False,
        "predictions": preds,
    }
    pred_path = work / "predictions.json"
    pred_path.write_text(json.dumps(pred_payload, indent=2) + "\n", encoding="utf-8")

    report = evaluate_files(str(src / "ground_truth.csv"), str(pred_path))
    pending = score_pending(src / "ground_truth.csv", pred_path)
    report["pending_score"] = pending
    # Evaluator treats expected PENDING as a decision error. Credit it here instead.
    # PENDING→MATCH is a false auto-clear the held-out scorer does not count.
    gt_rows = load_gt(src / "ground_truth.csv")
    preds = load_predictions(pred_path)
    by_oid = {p.get("order_id"): p for p in preds}
    by_canon = {p.get("canonical_id") or canonicalize_id(p.get("order_id") or ""): p for p in preds}
    pending_cleared = []
    for g in gt_rows:
        if g.get("expected_decision") != "PENDING":
            continue
        pred = lookup(g["transaction_id"], by_oid, by_canon)
        if (pred or {}).get("decision") == "MATCH":
            pending_cleared.append(g["transaction_id"])
    harm = dict(report.get("harm") or {})
    harm["false_match_count"] = (harm.get("false_match_count") or 0) + len(pending_cleared)
    if pending_cleared:
        extra = list(harm.get("false_matches") or [])
        extra.extend({"transaction_id": tid, "expected_reason": "PENDING",
                      "note": "PENDING auto-cleared as MATCH"} for tid in pending_cleared)
        harm["false_matches"] = extra
    harm["pending_cleared_as_match"] = pending_cleared
    report["harm"] = harm

    gt_n = report["n"]
    pending_n = pending["pending_expected"]
    if pending_n:
        credited = report["correct_matches"] + report["correct_exceptions"] + pending["pending_correct"]
        report["decision_accuracy_with_pending"] = round(credited / gt_n, 4) if gt_n else None
    else:
        report["decision_accuracy_with_pending"] = report["decision_accuracy"]
    (work / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    slim = {k: v for k, v in report.items() if k != "per_row"}
    return {
        "dataset": name,
        "kind": "matching",
        "ingest_status": "OK",
        **slim,
    }


def run_ingest_suite(name: str) -> dict:
    src = ADV / name
    work = WORK / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    expected = json.loads((src / "expected_ingest.json").read_text(encoding="utf-8"))
    try:
        adapt_eval_dir(str(src), str(work / "engine"), normalize_ids=True)
        actual = {"status": "OK", "quarantine": []}
        blocked = False
    except IngestValidationFailed as e:
        actual = e.report.as_dict()
        blocked = True

    (work / "ingest_report.json").write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
    field = expected.get("must_quarantine_field")
    value = expected.get("must_quarantine_value")
    hits = [
        q for q in actual.get("quarantine", [])
        if (not field or q.get("field") == field)
        and (value is None or str(q.get("value")) == str(value))
    ]
    ok = (
        actual.get("status") == expected.get("status")
        and (not expected.get("block_reconcile") or blocked)
        and (not field or len(hits) >= 1)
    )
    return {
        "dataset": name,
        "kind": "ingest",
        "ok": ok,
        "expected": expected,
        "actual_status": actual.get("status"),
        "quarantine": actual.get("quarantine", []),
        "blocked_reconcile": blocked,
        "n": 0,
        "harm": {"false_match_count": 0, "false_matches": []},
        "decision_accuracy": None,
    }


def summarize(reports: dict) -> dict:
    match_reports = [r for r in reports.values() if r.get("kind") == "matching"]
    ingest_reports = [r for r in reports.values() if r.get("kind") == "ingest"]
    n = sum(r.get("n") or 0 for r in match_reports)
    false_match = sum((r.get("harm") or {}).get("false_match_count") or 0 for r in match_reports)
    correct_m = sum(r.get("correct_matches") or 0 for r in match_reports)
    correct_e = sum(r.get("correct_exceptions") or 0 for r in match_reports)
    pending_ok = sum((r.get("pending_score") or {}).get("pending_correct") or 0 for r in match_reports)
    pending_n = sum((r.get("pending_score") or {}).get("pending_expected") or 0 for r in match_reports)
    type_n = sum(r.get("match_type_n") or 0 for r in match_reports)
    type_acc_weighted = 0.0
    for r in match_reports:
        if r.get("match_type_n") and r.get("match_type_accuracy_given_both_match") is not None:
            type_acc_weighted += r["match_type_n"] * r["match_type_accuracy_given_both_match"]
    reason_n = sum(r.get("exception_reason_n") or 0 for r in match_reports)
    reason_weighted = 0.0
    for r in match_reports:
        if r.get("exception_reason_n") and r.get("exception_reason_accuracy_given_both_exception") is not None:
            reason_weighted += r["exception_reason_n"] * r["exception_reason_accuracy_given_both_exception"]
    ingest_ok = sum(1 for r in ingest_reports if r.get("ok"))
    return {
        "matching_rows": n,
        "false_match_count": false_match,
        "correct_matches": correct_m,
        "correct_exceptions": correct_e,
        "decision_accuracy": round((correct_m + correct_e) / n, 4) if n else None,
        "decision_accuracy_with_pending": round((correct_m + correct_e + pending_ok) / n, 4) if n else None,
        "pending_expected": pending_n,
        "pending_correct": pending_ok,
        "match_type_accuracy_given_both_match": round(type_acc_weighted / type_n, 4) if type_n else None,
        "match_type_n": type_n,
        "exception_reason_accuracy_given_both_exception": round(reason_weighted / reason_n, 4) if reason_n else None,
        "exception_reason_n": reason_n,
        "ingest_suites": len(ingest_reports),
        "ingest_suites_ok": ingest_ok,
        "ingest_pass_rate": round(ingest_ok / len(ingest_reports), 4) if ingest_reports else None,
    }


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--ml", action="store_true")
    p.add_argument("--suite", default=None)
    args = p.parse_args()

    if not ADV.exists():
        raise SystemExit("No adversarial suites. Run evaluation_datasets/_generate_adversarial.py")

    names = [args.suite] if args.suite else suite_names()
    reports = {}
    for name in names:
        src = ADV / name
        kind = "ingest" if (src / "expected_ingest.json").exists() else "matching"
        if kind == "ingest":
            reports[name] = run_ingest_suite(name)
        else:
            reports[name] = run_matching_suite(name, use_ml=args.ml)
        printable = {k: v for k, v in reports[name].items()
                     if k not in ("false_exceptions_sample", "false_matches",
                                  "pending_when_match_expected",
                                  "pending_when_exception_expected",
                                  "missing_predictions", "quarantine")}
        print(json.dumps(printable, indent=2))
        print("---")

    totals = summarize(reports)
    out = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "protocol": "adversarial_independent",
        "heldout_untouched": True,
        "classifier_mode": "ml" if args.ml else "rule_based",
        "saw_ground_truth": False,
        "totals": totals,
        "suites": reports,
    }
    dest = ROOT / "adversarial_evaluation.json"
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"totals": totals}, indent=2))
    print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
