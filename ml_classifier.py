"""Local exception classifier. Explanations use this record's feature values."""

import os

import ml_features

DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "exception_model.joblib")

try:
    import joblib
except ImportError:  # pragma: no cover
    joblib = None

_FEATURE_LABELS = {
    "ledger_dupe_count": "appears {v:.0f}x in the ledger",
    "has_settlement": "{v} a settlement record",
    "has_bank": "{v} a bank credit record",
    "net_minus_credit": "settlement-vs-bank shortfall of {v:.2f}",
    "net_minus_credit_pct": "shortfall of {v:.0%} of settled amount",
    "fee_rate_diff_from_standard": "fee rate {v:.2%} off standard",
    "settle_lag_days": "{v:.0f} days from order to settlement",
    "bank_lag_days": "{v:.0f} days from settlement to bank credit",
    "implied_fee_rate": "implied fee rate {v:.2%}",
    "has_tds": "{v} TDS applied",
    "n_bank_rows": "has {v:.0f} bank credit entries",
}


def _bundle_cache():
    if not hasattr(_bundle_cache, "_cache"):
        _bundle_cache._cache = {}
    return _bundle_cache._cache


def load_model(path=DEFAULT_MODEL_PATH):
    if joblib is None:
        return None
    cache = _bundle_cache()
    if path not in cache:
        if not os.path.exists(path):
            return None
        cache[path] = joblib.load(path)
    return cache[path]


def _describe_feature(name, value):
    template = _FEATURE_LABELS.get(name)
    if template is None:
        return f"{name}={value:.2f}"
    if "{v}" in template and "has_" in name:
        value = "has" if value >= 0.5 else "missing"
        return template.format(v=value)
    try:
        return template.format(v=value)
    except (ValueError, TypeError):
        return f"{name}={value}"


def ml_classify(record, bundle=None):
    bundle = bundle or load_model()
    if bundle is None:
        raise FileNotFoundError(
            f"No trained model found at {DEFAULT_MODEL_PATH}. Run `python3 train_model.py` first."
        )

    clf = bundle["model"]
    feature_names = bundle["feature_names"]

    feats = ml_features.extract_features(record)
    vector = [feats[name] for name in feature_names]

    probs = clf.predict_proba([vector])[0]
    classes = clf.classes_
    best_idx = probs.argmax()
    cause = classes[best_idx]
    confidence = float(probs[best_idx])

    # Use this record's values, not a canned sentence per class.
    importances = clf.feature_importances_
    ranked = sorted(zip(feature_names, importances), key=lambda t: -t[1])
    top_feature_names = [name for name, _ in ranked[:3]]
    descriptions = [_describe_feature(name, feats[name]) for name in top_feature_names]

    oid = record["order_id"]
    reasoning = (
        f"Model predicted {cause} ({confidence:.0%} confidence) for {oid}, "
        f"based mainly on: " + "; ".join(descriptions) + "."
    )

    return {"cause": cause, "confidence": confidence, "reasoning": reasoning}


def classify_exceptions(exceptions, ledger_dupe_counts, model_path=DEFAULT_MODEL_PATH):
    bundle = load_model(model_path)
    classified = []
    for record in exceptions:
        record = dict(record)
        record["ledger_dupe_count"] = ledger_dupe_counts.get(record["order_id"], 1)
        result = ml_classify(record, bundle=bundle)
        classified.append({
            "order_id": record["order_id"],
            "amount": record["ledger"]["amount"],
            "cause": result["cause"],
            "confidence": result["confidence"],
            "reasoning": result["reasoning"],
            "actually_used": "ml",
            "symptoms": record.get("symptoms") or [],
            "severity": record.get("severity", "low"),
            "age_days": record.get("age_days", 0),
            "leg_a": record.get("leg_a"),
            "leg_b": record.get("leg_b"),
        })
    return classified
