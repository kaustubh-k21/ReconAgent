"""Classify unmatched rows with deterministic rules.

Structural cause (what broke) is separate from link_suspicion (lookalike /
near-ID counterparts that must not be auto-linked).
"""

from collections import Counter


def to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _lookalike_suspicion(record, symptoms):
    """Return (suspicion_label, refs) or (None, [])."""
    if "wrong_transaction_candidate" in symptoms:
        refs = (
            record.get("candidate_settlement_ids")
            or record.get("candidate_bank_refs")
            or []
        )
        return "wrong_transaction", refs
    if "ambiguous_match_candidate" in symptoms:
        refs = (
            record.get("candidate_settlement_ids")
            or record.get("candidate_bank_refs")
            or []
        )
        return "ambiguous_match", refs
    return None, []


def _structural_classify(record, symptoms):
    """Cause from ledger/settlement/bank evidence — ignores lookalike tags."""
    oid = record["order_id"]
    settlement_rows = record.get("settlement") or []
    bank_rows = record.get("bank") or []

    if record.get("ledger_dupe_count", 1) > 1 or "ledger_dupe" in symptoms:
        return {
            "cause": "duplicate_entry",
            "confidence": 0.9,
            "reasoning": f"{oid} appears {record.get('ledger_dupe_count', 1)} times in the internal ledger "
                         f"but only once downstream — looks like a duplicate order entry, not a payment issue.",
        }

    if "duplicate_gateway" in symptoms:
        return {
            "cause": "duplicate_gateway",
            "confidence": 0.97,
            "reasoning": f"{oid} has economically identical duplicate gateway settlement rows.",
        }

    if "chargeback" in symptoms:
        return {
            "cause": "chargeback",
            "confidence": 0.9,
            "reasoning": f"{oid} shows chargeback/dispute markers with a bank pullback and no "
                         f"matching gateway refund memo — treat as a card-network reclaim.",
        }

    if "reversal" in symptoms:
        return {
            "cause": "reversal",
            "confidence": 0.9,
            "reasoning": f"{oid} shows reversal/void markers (or a negative settlement) undoing "
                         f"the original capture — not a normal amount mismatch.",
        }

    if "cardinality_break" in symptoms and len(bank_rows) > 1:
        credits = [to_float(b.get("credit_amount")) for b in bank_rows]
        if all(c is not None for c in credits) and len(set(credits)) == 1:
            val = credits[0]
            return {
                "cause": "duplicate_credit",
                "confidence": 0.95,
                "reasoning": f"{oid} has duplicate credit rows ({len(bank_rows)}x) in the bank statement "
                             f"with identical amounts of ₹{val:.2f} — looks like duplicate credit payouts.",
            }

    if "refund_full" in symptoms:
        return {
            "cause": "refund_full",
            "confidence": 0.96,
            "reasoning": f"{oid} has explicit refund/debit evidence that reverses the settled amount.",
        }

    if "refund_partial" in symptoms:
        return {
            "cause": "refund_partial",
            "confidence": 0.94,
            "reasoning": f"{oid} has explicit gateway refund or bank debit evidence for a partial reversal.",
        }

    if "one_sided_no_settlement" in symptoms or (not settlement_rows and not bank_rows):
        return {
            "cause": "missing_settlement",
            "confidence": 0.85,
            "reasoning": f"{oid} exists in the ledger but never appears in settlement or the bank "
                         f"statement — the payment likely never actually settled.",
        }

    if "one_sided_no_bank_past_sla" in symptoms or (
        settlement_rows and not bank_rows and "one_sided_no_bank_within_sla" not in symptoms
    ):
        age = record.get("age_days")
        leg_b = record.get("leg_b") or {}
        note = leg_b.get("note") or (f"aged {age}d" if age else "past SLA")
        return {
            "cause": "aged_missing_bank",
            "confidence": 0.88,
            "reasoning": f"{oid} settled but the bank credit is still missing ({note}) — "
                         f"timing artifact that aged past SLA, not a never-settled payment.",
        }

    if "unexplained_variance" in symptoms:
        return {
            "cause": "unexplained_variance",
            "confidence": 0.82,
            "reasoning": f"{oid} has a one-rupee (or policy-cap) variance with no fee, TDS, or refund story.",
        }

    leg_a = record.get("leg_a") or {}
    if (
        "leg_a_amount_break" in symptoms
        or "data_mismatch" in symptoms
        or leg_a.get("status") in ("under_or_over", "data_mismatch")
    ):
        return {
            "cause": "amount_mismatch",
            "confidence": 0.9,
            "reasoning": f"{oid} gateway gross/net arithmetic does not reconcile to the ledger "
                         f"({leg_a.get('note') or 'Leg A amount break'}).",
        }

    if "under_amount" in symptoms or "over_amount" in symptoms:
        if settlement_rows and bank_rows:
            s = settlement_rows[0]
            b = bank_rows[0]
            net = to_float(s.get("net_amount"))
            credit = to_float(b.get("credit_amount"))
            debit = sum(to_float(row.get("debit_amount"), 0.0) for row in bank_rows)
            refund = sum(to_float(row.get("refund_amount"), 0.0) for row in settlement_rows)
            if net is not None and credit is not None and (refund > 0.01 or debit > 0.01):
                shortfall = round(net - credit + debit, 2)
                cause = "refund_full" if shortfall >= net - 1.0 else "refund_partial"
                return {
                    "cause": cause,
                    "confidence": 0.92,
                    "reasoning": f"{oid} has explicit refund/debit evidence of {refund or debit:.2f}; "
                                 f"the bank shortfall is {shortfall:.2f}.",
                }
        delta = abs(to_float((record.get("leg_b") or {}).get("delta"), 0.0) or 0.0)
        delta_note = (record.get("leg_b") or {}).get("note") or "amount gap"
        if delta <= 10:
            return {
                "cause": "unexplained_variance",
                "confidence": 0.8,
                "reasoning": f"{oid} has a small residual of ₹{delta:.2f} with no fee, TDS, or refund memo.",
            }
        return {
            "cause": "amount_mismatch",
            "confidence": 0.88,
            "reasoning": f"{oid} settlement and bank amounts disagree ({delta_note}) "
                         f"with no refund or debit evidence.",
        }

    if len(bank_rows) > 1:
        credits = [to_float(b.get("credit_amount")) for b in bank_rows]
        if all(c is not None for c in credits) and len(set(credits)) == 1:
            val = credits[0]
            return {
                "cause": "duplicate_credit",
                "confidence": 0.95,
                "reasoning": f"{oid} has duplicate credit rows ({len(bank_rows)}x) with identical "
                             f"amounts of ₹{val:.2f}.",
            }

    return None


def rule_based_classify(record):
    oid = record["order_id"]
    symptoms = set(record.get("symptoms") or [])
    suspicion, refs = _lookalike_suspicion(record, symptoms)
    structural = _structural_classify(record, symptoms)

    if structural is not None:
        result = dict(structural)
        result["link_suspicion"] = suspicion
        if suspicion:
            result["reasoning"] = (
                result["reasoning"].rstrip(".")
                + f". Also flagged {suspicion.replace('_', ' ')} candidate(s) {refs} — not auto-linked."
            )
        return result

    if suspicion == "wrong_transaction":
        return {
            "cause": "wrong_transaction",
            "link_suspicion": None,
            "confidence": 0.78,
            "reasoning": f"{oid} has no safe direct link, but a different transaction is the unique "
                         f"amount/date or near-ID candidate ({refs}) — do not auto-link it.",
        }

    if suspicion == "ambiguous_match":
        return {
            "cause": "ambiguous_match",
            "link_suspicion": None,
            "confidence": 0.8,
            "reasoning": f"{oid} has multiple or unreferenced plausible downstream candidates "
                         f"({refs}); the evidence cannot establish a unique link.",
        }

    return {
        "cause": "unexplained_variance",
        "link_suspicion": None,
        "confidence": 0.4,
        "reasoning": f"{oid} doesn't fit a known pattern from the evidence available "
                     f"(symptoms={sorted(symptoms) or 'none'}) — needs manual review.",
    }


def classify_exceptions(exceptions, ledger_dupe_counts):
    classified = []
    for record in exceptions:
        record = dict(record)
        record["ledger_dupe_count"] = ledger_dupe_counts.get(
            record["order_id"], record.get("ledger_dupe_count", 1)
        )
        result = rule_based_classify(record)
        result["actually_used"] = "rule_based"

        classified.append({
            "order_id": record["order_id"],
            "amount": to_float(record["ledger"].get("amount"), 0.0),
            "cause": result["cause"],
            "link_suspicion": result.get("link_suspicion"),
            "confidence": result["confidence"],
            "reasoning": result["reasoning"],
            "actually_used": result["actually_used"],
            "symptoms": record.get("symptoms") or [],
            "severity": record.get("severity", "low"),
            "age_days": record.get("age_days", 0),
            "leg_a": record.get("leg_a"),
            "leg_b": record.get("leg_b"),
            "candidate_bank_refs": record.get("candidate_bank_refs"),
            "candidate_settlement_ids": record.get("candidate_settlement_ids"),
        })
    return classified


def cause_breakdown(classified):
    return dict(Counter(c["cause"] for c in classified))
