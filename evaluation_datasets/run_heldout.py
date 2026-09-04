#!/usr/bin/env python3
"""Held-out protocol: eval CSVs → adapter → agent → evaluator. Agent never opens labels."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
sys.path.insert(0, str(PROJ))

import matcher  # noqa: E402
import exception_classifier as ec  # noqa: E402
import ml_classifier  # noqa: E402
from ingest_adapter import adapt_eval_dir  # noqa: E402
from predictions import build_predictions  # noqa: E402
from evaluator import evaluate_files  # noqa: E402

DATASETS = ["easy_100", "medium_100", "hard_100"]


def count_dupes(ledger_path: Path) -> dict:
    from collections import Counter
    import csv
    with ledger_path.open(newline="", encoding="utf-8") as f:
        ids = [r["order_id"] for r in csv.DictReader(f)]
    return dict(Counter(ids))


def run_agent(engine_dir: Path, use_ml: bool = False) -> tuple[dict, list, str]:
    results = matcher.run_matching(data_dir=str(engine_dir))
    dupes = count_dupes(engine_dir / "internal_ledger.csv")
    if use_ml:
        if ml_classifier.load_model() is None:
            raise SystemExit("No trained model. Run train_model.py or omit --ml.")
        classified = ml_classifier.classify_exceptions(results["exceptions"], dupes)
        mode = "ml"
    else:
        classified = ec.classify_exceptions(results["exceptions"], dupes)
        mode = "rule_based"
    return results, classified, mode


def run_one(name: str, normalize_ids: bool, use_ml: bool, work_root: Path) -> dict:
    src = ROOT / name
    work = work_root / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    adapter_meta = adapt_eval_dir(str(src), str(work / "engine"), normalize_ids=normalize_ids)
    results, classified, mode = run_agent(work / "engine", use_ml=use_ml)
    preds = build_predictions(results, classified)

    pred_payload = {
        "schema_version": 1,
        "dataset": name,
        "classifier_mode": mode,
        "adapter": adapter_meta,
        "generated_at": datetime.now().isoformat(),
        "saw_ground_truth": False,
        "saw_dataset_metadata": False,
        "predictions": preds,
    }
    pred_path = work / "predictions.json"
    pred_path.write_text(json.dumps(pred_payload, indent=2) + "\n", encoding="utf-8")

    report = evaluate_files(str(src / "ground_truth.csv"), str(pred_path))
    slim = {k: v for k, v in report.items() if k != "per_row"}
    (work / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return {"dataset": name, "predictions": str(pred_path), **slim}


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--ml", action="store_true")
    p.add_argument("--no-normalize-ids", action="store_true",
                   help="disable declared ingest ID normalization (stricter join)")
    p.add_argument("--dataset", choices=DATASETS, default=None)
    args = p.parse_args()

    names = [args.dataset] if args.dataset else DATASETS
    work_root = ROOT / "_work"
    reports = {}
    for name in names:
        reports[name] = run_one(
            name,
            normalize_ids=not args.no_normalize_ids,
            use_ml=args.ml,
            work_root=work_root,
        )
        print(json.dumps({k: v for k, v in reports[name].items()
                          if k not in ("false_exceptions_sample", "false_matches",
                                       "pending_when_match_expected",
                                       "pending_when_exception_expected",
                                       "missing_predictions")}, indent=2))
        print("---")

    out = ROOT / "heldout_evaluation.json"
    out.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
