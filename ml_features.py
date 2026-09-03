"""Numeric features for the exception model. Same signals the matcher already has."""

from datetime import datetime

STANDARD_FEE_RATE = 0.02

FEATURE_NAMES = [
    "ledger_dupe_count",
    "has_settlement",
    "has_bank",
    "n_settlement_rows",
    "n_bank_rows",
    "ledger_amount",
    "amount_vs_gross_diff",
    "implied_fee_rate",
    "fee_rate_diff_from_standard",
    "has_tds",
    "tds_amount",
    "net_amount",
    "credit_amount",
    "net_minus_credit",
    "net_minus_credit_pct",
    "settle_lag_days",
    "bank_lag_days",
    "symptom_ledger_dupe",
    "symptom_no_settlement",
    "symptom_no_bank_past_sla",
    "symptom_under_amount",
    "symptom_cardinality_break",
    "age_days",
]


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def extract_features(record):
    """record shape matches matcher.py / main.py exception rows."""
    ledger = record["ledger"]
    settlement_rows = record.get("settlement") or []
    bank_rows = record.get("bank") or []
    dupe_count = record.get("ledger_dupe_count", 1)

    ledger_amount = to_float(ledger.get("amount"))
    ledger_date = parse_date(ledger.get("order_date"))

    has_settlement = 1 if settlement_rows else 0
    has_bank = 1 if bank_rows else 0

    gross_amount = amount_vs_gross_diff = implied_fee_rate = fee_rate_diff = 0.0
    tds_amount = net_amount = 0.0
    has_tds = 0
    settle_lag_days = 0.0
    settle_date = None

    if settlement_rows:
        s = settlement_rows[0]
        gross_amount = to_float(s.get("gross_amount"))
        amount_vs_gross_diff = ledger_amount - gross_amount
        fee = to_float(s.get("fee"))
        implied_fee_rate = (fee / gross_amount) if gross_amount else 0.0
        fee_rate_diff = abs(implied_fee_rate - STANDARD_FEE_RATE)
        tds_amount = to_float(s.get("tds"))
        has_tds = 1 if tds_amount > 0.01 else 0
        net_amount = to_float(s.get("net_amount"))
        settle_date = parse_date(s.get("settlement_date"))
        if settle_date and ledger_date:
            settle_lag_days = (settle_date - ledger_date).days

    credit_amount = net_minus_credit = net_minus_credit_pct = bank_lag_days = 0.0
    if bank_rows:
        b = bank_rows[0]
        credit_amount = to_float(b.get("credit_amount"))
        net_minus_credit = net_amount - credit_amount
        net_minus_credit_pct = (net_minus_credit / net_amount) if net_amount else 0.0
        bank_date = parse_date(b.get("credit_date"))
        if bank_date and settle_date:
            bank_lag_days = (bank_date - settle_date).days
        elif bank_date and ledger_date:
            bank_lag_days = (bank_date - ledger_date).days

    symptoms = set(record.get("symptoms") or [])
    age_days = float(record.get("age_days") or 0)

    return {
        "ledger_dupe_count": float(dupe_count),
        "has_settlement": float(has_settlement),
        "has_bank": float(has_bank),
        "n_settlement_rows": float(len(settlement_rows)),
        "n_bank_rows": float(len(bank_rows)),
        "ledger_amount": ledger_amount,
        "amount_vs_gross_diff": amount_vs_gross_diff,
        "implied_fee_rate": implied_fee_rate,
        "fee_rate_diff_from_standard": fee_rate_diff,
        "has_tds": float(has_tds),
        "tds_amount": tds_amount,
        "net_amount": net_amount,
        "credit_amount": credit_amount,
        "net_minus_credit": net_minus_credit,
        "net_minus_credit_pct": net_minus_credit_pct,
        "settle_lag_days": settle_lag_days,
        "bank_lag_days": bank_lag_days,
        "symptom_ledger_dupe": 1.0 if "ledger_dupe" in symptoms else 0.0,
        "symptom_no_settlement": 1.0 if "one_sided_no_settlement" in symptoms else 0.0,
        "symptom_no_bank_past_sla": 1.0 if "one_sided_no_bank_past_sla" in symptoms else 0.0,
        "symptom_under_amount": 1.0 if "under_amount" in symptoms else 0.0,
        "symptom_cardinality_break": 1.0 if "cardinality_break" in symptoms else 0.0,
        "age_days": age_days,
    }


def features_to_vector(feature_dict):
    return [feature_dict[name] for name in FEATURE_NAMES]
