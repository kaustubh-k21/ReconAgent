"""Run the pipeline and write results.json.

    python main.py            # rules
    python main.py --ml       # trained model
    python main.py --regen    # new synthetic data first
    python main.py --dry-run  # print only
    python main.py --data-dir evaluation_datasets/hard_100
"""

import argparse
import csv
from datetime import datetime
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))

import data_generator
import matcher
import exception_classifier as ec
import ml_classifier
import validate_sources
import ingest_adapter
from ingest_validate import IngestValidationFailed, STATUS_FAILED
from predictions import build_predictions
from exception_copy import enrich_classified
from schema_contract import SchemaError, contract_text

AUTO_MATCH_CAUSES = {
    "clean", "fee_variance", "tds_deduction", "rounding", "batch_settlement",
}
REVIEW_CAUSES = {"timing_lag"}
PENDING_CAUSES = {"pending_bank"}


def load_ground_truth(data_dir="data"):
    with open(f"{data_dir}/ground_truth.csv", newline="") as f:
        return {row["order_id"]: row["true_cause"] for row in csv.DictReader(f)}


def count_ledger_duplicates(data_dir="data"):
    with open(f"{data_dir}/internal_ledger.csv", newline="") as f:
        ids = [row["order_id"] for row in csv.DictReader(f)]
    return dict(Counter(ids))


def score_against_truth(matched_results, classified_exceptions, truth):
    """Self-scoring against seeded ground truth."""
    correct = 0
    total = 0
    rows = []

    auto_ids = {r["order_id"] for r in matched_results["exact"] + matched_results["fuzzy"]}
    review_ids = {r["order_id"] for r in matched_results.get("review", [])}
    pending_ids = {r["order_id"] for r in matched_results.get("pending", [])}

    for oid, true_cause in truth.items():
        total += 1
        if oid in auto_ids:
            predicted = "matched_auto"
            is_ok = true_cause in AUTO_MATCH_CAUSES or true_cause in REVIEW_CAUSES
            # timing_lag may land in review or fuzzy; both are correct.
            if true_cause in REVIEW_CAUSES:
                is_ok = True
        elif oid in review_ids:
            predicted = "matched_review"
            is_ok = true_cause in REVIEW_CAUSES or true_cause in AUTO_MATCH_CAUSES
        elif oid in pending_ids:
            predicted = "pending_bank"
            is_ok = true_cause in PENDING_CAUSES
        else:
            exc = next((c for c in classified_exceptions if c["order_id"] == oid), None)
            predicted = exc["cause"] if exc else "MISSING_FROM_PIPELINE"
            is_ok = predicted == true_cause
            if (
                not is_ok
                and exc
                and exc.get("link_suspicion")
                and exc.get("link_suspicion") == true_cause
            ):
                is_ok = True
                predicted = f"{exc['cause']}+{exc['link_suspicion']}"

        if is_ok:
            correct += 1
        rows.append({
            "order_id": oid,
            "true_cause": true_cause,
            "pipeline_outcome": predicted,
            "correct": is_ok,
        })

    accuracy = correct / total if total else 0.0
    return accuracy, rows


def serialize_match(rec):
    return {
        "order_id": rec["order_id"],
        "amount": rec["ledger"]["amount"],
        "confidence": rec.get("confidence"),
        "note": rec.get("note"),
        "matched_by_rule": rec.get("matched_by_rule"),
        "auto_confirmed": rec.get("auto_confirmed", True),
        "leg_a": rec.get("leg_a"),
        "leg_b": rec.get("leg_b"),
    }


def _print_validation_failed(report):
    print("VALIDATION_FAILED", file=sys.stderr)
    quarantine = report.get("quarantine", []) if isinstance(report, dict) else report.quarantine
    for rec in quarantine:
        if not isinstance(rec, dict):
            rec = rec.as_dict()
        print(
            f"  {rec['file']} row {rec['row']}, {rec['field']}: {rec['error']} value={rec['value']!r}",
            file=sys.stderr,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ml", action="store_true",
                        help="use the self-trained model (run train_model.py first)")
    parser.add_argument("--regen", action="store_true", help="regenerate synthetic source data")
    parser.add_argument("--dry-run", action="store_true",
                        help="run pipeline and print summary without writing results/dashboard")
    parser.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "data"))
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results.json"))
    parser.add_argument("--n", type=int, default=70, help="transaction count when regenerating")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed when regenerating")
    parser.add_argument("--no-normalize-ids", action="store_true",
                        help="when adapting eval schema, do not casefold/strip hyphens on join keys")
    parser.add_argument("--predictions", default=None,
                        help="write canonical predictions.json for the external evaluator")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    adapter_meta = {"adapted": False, "schema": "engine"}

    if args.regen:
        os.makedirs(data_dir, exist_ok=True)
        data_generator.main(out_dir=data_dir, n_transactions=args.n, seed=args.seed)

    try:
        ingest_work = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "evaluation_datasets", "_work", "_ingest")
        engine_dir, adapter_meta = ingest_adapter.resolve_engine_dir(
            data_dir,
            work_dir=ingest_work if ingest_adapter.looks_like_eval_schema(data_dir) else None,
            normalize_ids=not args.no_normalize_ids,
        )
    except IngestValidationFailed as e:
        _print_validation_failed(e.report.as_dict())
        raise SystemExit(1)
    except SchemaError as e:
        print("SCHEMA_ERROR", file=sys.stderr)
        print(str(e), file=sys.stderr)
        raise SystemExit(2)
    except FileNotFoundError as e:
        raise SystemExit(
            f"{e}\nRefusing to silently generate demo data. "
            "Pass --regen if you want a synthetic batch, or --data-dir pointing at complete sources.\n\n"
            + contract_text()
        ) from e

    ok, errs = validate_sources.validate_sources(engine_dir)
    if not ok:
        if STATUS_FAILED in errs:
            print("VALIDATION_FAILED", file=sys.stderr)
            for err in errs:
                if err == STATUS_FAILED:
                    continue
                print(f"  {err}", file=sys.stderr)
        else:
            print("Source validation failed — refusing to match:", file=sys.stderr)
            for err in errs:
                print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)

    results = matcher.run_matching(data_dir=engine_dir)
    dupe_counts = count_ledger_duplicates(engine_dir)

    if args.ml:
        if ml_classifier.load_model() is None:
            raise SystemExit(
                "No trained model found. Run `python3 train_model.py` once first, "
                "then `python3 main.py --ml`."
            )
        classified = ml_classifier.classify_exceptions(results["exceptions"], dupe_counts)
        classifier_mode = "ml"
    else:
        classified = ec.classify_exceptions(results["exceptions"], dupe_counts)
        classifier_mode = "rule_based"

    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    classified = sorted(
        classified,
        key=lambda x: (sev_rank.get(x.get("severity", "low"), 9), -float(x.get("amount") or 0)),
    )
    classified = enrich_classified(classified, results["exceptions"])

    breakdown = ec.cause_breakdown(classified)

    # In-distribution self-score only. Held-out GT uses a different taxonomy.
    truth_path = os.path.join(data_dir, "ground_truth.csv")
    ground_truth_available = False
    accuracy_by_cause = {}
    accuracy = None
    scored_rows = []
    if os.path.exists(truth_path) and not ingest_adapter.looks_like_eval_schema(data_dir):
        with open(truth_path, newline="") as f:
            gt_fields = csv.DictReader(f).fieldnames or []
        if "true_cause" in gt_fields and "order_id" in gt_fields:
            ground_truth_available = True
            truth = load_ground_truth(data_dir)
            accuracy, scored_rows = score_against_truth(results, classified, truth)
            category_stats = {}
            for row in scored_rows:
                cause = row["true_cause"]
                if cause not in category_stats:
                    category_stats[cause] = {"correct": 0, "total": 0}
                category_stats[cause]["total"] += 1
                if row["correct"]:
                    category_stats[cause]["correct"] += 1
            for cause, stats in category_stats.items():
                accuracy_by_cause[cause] = {
                    "correct": stats["correct"],
                    "total": stats["total"],
                    "accuracy": round(stats["correct"] / stats["total"], 4) if stats["total"] > 0 else 0.0,
                }

    n_exact = len(results["exact"])
    n_fuzzy = len(results["fuzzy"])
    n_review = len(results.get("review", []))
    n_pending = len(results.get("pending", []))
    n_exc = len(results["exceptions"])
    total = n_exact + n_fuzzy + n_review + n_pending + n_exc
    auto_match_rate = (n_exact + n_fuzzy) / total if total else 0.0
    auto_plus_review_rate = (n_exact + n_fuzzy + n_review) / total if total else 0.0
    # match_rate is auto-clears only; date-lag is review, not matched.
    match_rate = auto_match_rate

    severity_breakdown = dict(Counter(c.get("severity", "low") for c in classified))

    output = {
        "summary": {
            "total_transactions": total,
            "exact_matches": n_exact,
            "fuzzy_matches": n_fuzzy,
            "review_matches": n_review,
            "pending_bank": n_pending,
            "exceptions": n_exc,
            "match_rate": round(match_rate, 4),
            "auto_match_rate": round(auto_match_rate, 4),
            "auto_plus_review_rate": round(auto_plus_review_rate, 4),
            "classifier_mode": classifier_mode,
            "self_scored_accuracy": round(accuracy, 4) if accuracy is not None else None,
            "self_score_is_in_distribution_only": True,
            "ground_truth_available": ground_truth_available,
            "ingest": adapter_meta,
            "leg_summary": results.get("leg_summary", {}),
            "rule_counts": results.get("rule_counts", {}),
            "severity_breakdown": severity_breakdown,
            "policy": results.get("policy", {}),
            "generated_at": datetime.now().isoformat(),
            "dry_run": bool(args.dry_run),
        },
        "exact_matches": [serialize_match(r) for r in results["exact"]],
        "fuzzy_matches": [serialize_match(r) for r in results["fuzzy"]],
        "review_matches": [serialize_match(r) for r in results.get("review", [])],
        "pending": [serialize_match(r) for r in results.get("pending", [])],
        "exceptions": classified,
        "exception_cause_breakdown": breakdown,
        "ground_truth_scoring": scored_rows,
        "accuracy_by_cause": accuracy_by_cause,
    }

    print(json.dumps(output["summary"], indent=2))

    pred_payload = {
        "schema_version": 1,
        "classifier_mode": classifier_mode,
        "adapter": adapter_meta,
        "generated_at": datetime.now().isoformat(),
        "saw_ground_truth": False,
        "predictions": build_predictions(results, classified),
    }

    if args.dry_run:
        print("\n[dry-run] Skipping write of results.json and dashboard.html")
        return

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nFull results -> {args.out}")

    pred_path = args.predictions or os.path.splitext(args.out)[0] + ".predictions.json"
    with open(pred_path, "w") as f:
        json.dump(pred_payload, f, indent=2)
    print(f"Predictions (for external evaluator) -> {pred_path}")

    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(base_dir, "dashboard_template.html")
        dashboard_path = os.path.join(base_dir, "dashboard.html")
        if os.path.exists(template_path):
            with open(template_path) as tf:
                template_content = tf.read()
            results_json = json.dumps(output, indent=2)
            with open(dashboard_path, "w") as df:
                df.write(template_content.replace("__RESULTS_JSON__", results_json))
            print("Automatically rebuilt dashboard.html from template.")
    except Exception as e:
        print(f"Warning: Could not automatically rebuild dashboard: {e}")


if __name__ == "__main__":
    main()
