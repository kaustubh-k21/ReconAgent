"""Train the exception model on matcher leftovers. Different seed than the demo batch."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import data_generator
import matcher
import ml_features

from collections import Counter

from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score
import joblib


def build_training_examples(n_transactions=4000, seed=7, tmp_dir="data_train"):
    os.makedirs(tmp_dir, exist_ok=True)
    data_generator.main(out_dir=tmp_dir, n_transactions=n_transactions, seed=seed)

    match_results = matcher.run_matching(data_dir=tmp_dir)
    exceptions = match_results["exceptions"]

    # Label from ground truth; train only on rows the matcher left unmatched.
    import csv
    with open(f"{tmp_dir}/ground_truth.csv", newline="") as f:
        truth = {row["order_id"]: row["true_cause"] for row in csv.DictReader(f)}
    with open(f"{tmp_dir}/internal_ledger.csv", newline="") as f:
        ids = [row["order_id"] for row in csv.DictReader(f)]
    dupe_counts = dict(Counter(ids))

    X, y = [], []
    for record in exceptions:
        record = dict(record)
        record["ledger_dupe_count"] = dupe_counts.get(record["order_id"], 1)
        feats = ml_features.extract_features(record)
        X.append(ml_features.features_to_vector(feats))
        y.append(truth[record["order_id"]])

    return X, y


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=4000, help="training batch size")
    parser.add_argument("--seed", type=int, default=7, help="training batch seed (kept separate from the demo seed 42)")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "exception_model.joblib"))
    args = parser.parse_args()

    print(f"Generating {args.n} synthetic transactions (seed={args.seed}) for training...")
    X, y = build_training_examples(n_transactions=args.n, seed=args.seed)
    print(f"{len(X)} exception-tier records available for training")
    print("Class distribution:", dict(Counter(y)))

    if len(set(y)) < 2:
        raise SystemExit("Not enough class diversity to train on -- increase --n")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=args.seed, stratify=y if min(Counter(y).values()) >= 2 else None
    )

    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=2,
        random_state=args.seed,
        class_weight="balanced",  # exception causes are naturally imbalanced (duplicates/missing-settlement dominate)
    )
    clf.fit(X_train, y_train)

    y_pred = clf.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    print(f"\nHeld-out test accuracy: {acc:.1%}  (on {len(X_test)} records the model never trained on)")
    print("\nPer-class report:")
    print(classification_report(y_test, y_pred, zero_division=0))

    importances = sorted(
        zip(ml_features.FEATURE_NAMES, clf.feature_importances_),
        key=lambda t: -t[1],
    )
    print("Top features (global importance):")
    for name, imp in importances[:6]:
        print(f"  {name:<30s} {imp:.3f}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    joblib.dump({
        "model": clf,
        "feature_names": ml_features.FEATURE_NAMES,
        "classes": list(clf.classes_),
        "held_out_accuracy": acc,
        "training_n": args.n,
        "training_seed": args.seed,
    }, args.out)
    print(f"\nSaved model -> {args.out}")


if __name__ == "__main__":
    main()
