"""Prediction records the evaluator can join without reading ground truth."""

from __future__ import annotations

from ingest_adapter import canonicalize_id

# Internal classifier labels → eval taxonomy.
REASON_TO_EVAL = {
    "duplicate_credit": "duplicate_bank_credit",
    "duplicate_gateway": "duplicate_gateway",
    "missing_settlement": "missing_settlement",
    "aged_missing_bank": "missing_bank_credit",
    "refund_partial": "refund_partial",
    "refund_full": "refund_full",
    "chargeback": "refund_full",
    "reversal": "refund_full",
    "amount_mismatch": "amount_mismatch",
    "wrong_transaction": "wrong_transaction",
    "ambiguous_match": "ambiguous_match",
    "unexplained_variance": "unexplained_variance",
    "unexplained": "unexplained_variance",
    "timing_lag": "timing_difference",
    "fee_variance": "unexplained_variance",
    "tds_deduction": "tds_issue",
    "rounding": "unexplained_variance",
    "operator_override": "unexplained_variance",
}


def _amount(rec) -> float:
    ledger = rec.get("ledger") or {}
    try:
        return float(ledger.get("amount") or rec.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _match_type_from_rule(rule: str | None, bucket: str, rec: dict | None = None) -> str | None:
    note = (rec or {}).get("note") or ""
    id_fuzzy = "id_format" in note
    if bucket == "exact":
        return "FUZZY" if id_fuzzy else "EXACT"
    if bucket == "review":
        if rule == "rounding_review":
            return "TOLERANCE"
        return "FUZZY"
    if bucket == "fuzzy":
        if rule == "batch_n_to_1":
            return "MANY_TO_ONE"
        if rule == "one_to_many":
            return "ONE_TO_MANY"
        if rule == "date_lag":
            return "FUZZY"
        if rule == "tolerance_fee_tds_round":
            return "FUZZY" if id_fuzzy else "TOLERANCE"
        if id_fuzzy:
            return "FUZZY"
        return "FUZZY"
    return None


def _one(order_id, decision, match_type, rec, cause=None, link_suspicion=None):
    rule = rec.get("matched_by_rule")
    reason = None
    suspicion_eval = None
    if decision == "EXCEPTION":
        reason = REASON_TO_EVAL.get(cause or "", cause)
        if link_suspicion:
            suspicion_eval = REASON_TO_EVAL.get(link_suspicion, link_suspicion)
    return {
        "order_id": order_id,
        "canonical_id": canonicalize_id(order_id),
        "decision": decision,  # MATCH | REVIEW | EXCEPTION | PENDING
        "match_type": match_type,
        "exception_reason": reason,
        "link_suspicion": suspicion_eval,
        "confidence": rec.get("confidence"),
        "amount": _amount(rec),
        "matched_by_rule": rule,
        "note": rec.get("reasoning") or rec.get("note"),
        "symptoms": rec.get("symptoms") or [],
    }


def build_predictions(match_results: dict, classified_exceptions: list) -> list[dict]:
    """One prediction per ledger order the matcher processed."""
    by_exc = {c["order_id"]: c for c in classified_exceptions}
    out = []
    seen = set()

    def add(bucket, rec, decision, match_type, cause=None, link_suspicion=None):
        oid = rec["order_id"]
        if oid in seen:
            return
        seen.add(oid)
        merged = dict(rec)
        if oid in by_exc:
            classified = by_exc[oid]
            cause = cause or classified.get("cause")
            if link_suspicion is None:
                link_suspicion = classified.get("link_suspicion")
            merged["confidence"] = classified.get("confidence", rec.get("confidence"))
            merged["reasoning"] = classified.get("reasoning")
            merged["symptoms"] = classified.get("symptoms") or rec.get("symptoms") or []
        out.append(
            _one(oid, decision, match_type, merged, cause=cause, link_suspicion=link_suspicion)
        )

    for rec in match_results.get("exact") or []:
        add("exact", rec, "MATCH", _match_type_from_rule(rec.get("matched_by_rule"), "exact", rec))
    for rec in match_results.get("fuzzy") or []:
        add("fuzzy", rec, "MATCH", _match_type_from_rule(rec.get("matched_by_rule"), "fuzzy", rec))
    for rec in match_results.get("review") or []:
        # Review is a proposed MATCH (amounts agree; date lag / low confidence).
        add("review", rec, "MATCH", _match_type_from_rule(rec.get("matched_by_rule"), "review", rec))
    for rec in match_results.get("pending") or []:
        add("pending", rec, "PENDING", None)
    for rec in match_results.get("exceptions") or []:
        classified = by_exc.get(rec["order_id"]) or {}
        add(
            "exceptions",
            rec,
            "EXCEPTION",
            "EXCEPTION",
            cause=classified.get("cause"),
            link_suspicion=classified.get("link_suspicion"),
        )

    return out
