"""Classify unmatched rows. LLM if a key is set; otherwise rules."""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import Counter

CAUSE_LABELS = [
    "duplicate_entry",
    "duplicate_credit",
    "duplicate_gateway",
    "missing_settlement",
    "aged_missing_bank",
    "refund_partial",
    "refund_full",
    "amount_mismatch",
    "wrong_transaction",
    "ambiguous_match",
    "timing_lag",
    "fee_variance",
    "tds_deduction",
    "rounding",
    "unexplained_variance",
    "unexplained",
]


def to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def rule_based_classify(record):
    oid = record["order_id"]
    ledger = record["ledger"]
    settlement_rows = record.get("settlement") or []
    bank_rows = record.get("bank") or []
    symptoms = set(record.get("symptoms") or [])

    if record.get("ledger_dupe_count", 1) > 1 or "ledger_dupe" in symptoms:
        return {
            "cause": "duplicate_entry",
            "confidence": 0.9,
            "reasoning": f"{oid} appears {record.get('ledger_dupe_count', 1)} times in the internal ledger "
                         f"but only once downstream — looks like a duplicate order entry, not a payment issue.",
        }

    if "wrong_transaction_candidate" in symptoms:
        refs = (
            record.get("candidate_settlement_ids")
            or record.get("candidate_bank_refs")
            or []
        )
        return {
            "cause": "wrong_transaction",
            "confidence": 0.78,
            "reasoning": f"{oid} has no safe direct link, but a different transaction is the unique "
                         f"amount/date or near-ID candidate ({refs}) — do not auto-link it.",
        }

    if "ambiguous_match_candidate" in symptoms:
        refs = (
            record.get("candidate_settlement_ids")
            or record.get("candidate_bank_refs")
            or []
        )
        return {
            "cause": "ambiguous_match",
            "confidence": 0.8,
            "reasoning": f"{oid} has multiple or unreferenced plausible downstream candidates "
                         f"({refs}); the evidence cannot establish a unique link.",
        }

    if "duplicate_gateway" in symptoms:
        return {
            "cause": "duplicate_gateway",
            "confidence": 0.97,
            "reasoning": f"{oid} has economically identical duplicate gateway settlement rows.",
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

    return {
        "cause": "unexplained_variance",
        "confidence": 0.4,
        "reasoning": f"{oid} doesn't fit a known pattern from the evidence available "
                     f"(symptoms={sorted(symptoms) or 'none'}) — needs manual review.",
    }


def llm_classify(record):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        res = rule_based_classify(record)
        res["actually_used"] = "rule_based_fallback"
        return res

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    prompt = f"""You are a finance-ops reconciliation analyst. An order didn't cleanly
match across our three record sources. Given the evidence below, decide which single
cause from this list best explains it: {", ".join(CAUSE_LABELS)}.

To help you map evidence to cause labels:
- duplicate_entry: The order appears multiple times in the internal ledger (ledger_dupe_count > 1).
- duplicate_credit: The same order ID has duplicate credit entries with identical amounts in the bank statement.
- missing_settlement: The order exists in the ledger but is completely missing from settlement and bank records.
- aged_missing_bank: Settlement exists but bank credit is still missing past the SLA window (symptom one_sided_no_bank_past_sla).
- refund_partial / refund_full: only with explicit gateway refund_amount or bank debit evidence.
- amount_mismatch: ledger vs settlement vs bank amounts disagree, and it is not a refund.
- unexplained_variance: tiny residual (policy rounding cap) with no supporting story.
- wrong_transaction / ambiguous_match: a different or multiple downstream candidates exist; do not auto-link.
- timing_lag: The bank credit date is after the settlement date (usually still a match).
- fee_variance / tds_deduction / rounding: usually cleared by tolerance matching, not exceptions.

Matcher symptoms already computed: {json.dumps(record.get('symptoms') or [])}
Leg A: {json.dumps(record.get('leg_a'))}
Leg B: {json.dumps(record.get('leg_b'))}

Evidence:
- Order ID: {record['order_id']}
- Ledger entries: {json.dumps(record['ledger'])} (appeared {record.get('ledger_dupe_count', 1)}x in ledger)
- Settlement entries: {json.dumps(record.get('settlement') or [])}
- Bank entries: {json.dumps(record.get('bank') or [])}

Respond ONLY with JSON matching this schema:
{{
  "cause": "<one of the labels>",
  "confidence": <0-1 float>,
  "reasoning": "<one sentence, concrete, cites the actual numbers>"
}}"""

    req_data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }

    time.sleep(5)
    try:
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            data=json.dumps(req_data).encode("utf-8")
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            text = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            parsed = json.loads(text)
            parsed["cause"] = parsed.get("cause", "unexplained")
            parsed["confidence"] = to_float(parsed.get("confidence"), 0.5)
            parsed["reasoning"] = parsed.get("reasoning", "LLM reasoning response was missing details.")
            parsed["actually_used"] = "gemini_api"
            return parsed
    except Exception as e:
        print(f"Gemini LLM call failed, falling back to rule-based: {e}", file=sys.stderr)
        res = rule_based_classify(record)
        res["actually_used"] = "rule_based_fallback"
        return res


def classify_exceptions(exceptions, ledger_dupe_counts, use_llm=False):
    classified = []
    for record in exceptions:
        record = dict(record)
        record["ledger_dupe_count"] = ledger_dupe_counts.get(
            record["order_id"], record.get("ledger_dupe_count", 1)
        )
        if use_llm:
            result = llm_classify(record)
        else:
            result = rule_based_classify(record)
            result["actually_used"] = "rule_based"

        classified.append({
            "order_id": record["order_id"],
            "amount": to_float(record["ledger"].get("amount"), 0.0),
            "cause": result["cause"],
            "confidence": result["confidence"],
            "reasoning": result["reasoning"],
            "actually_used": result["actually_used"],
            "symptoms": record.get("symptoms") or [],
            "severity": record.get("severity", "low"),
            "age_days": record.get("age_days", 0),
            "leg_a": record.get("leg_a"),
            "leg_b": record.get("leg_b"),
        })
    return classified


def cause_breakdown(classified):
    return dict(Counter(c["cause"] for c in classified))


def escalate_to_gemini(record, ml_result):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    prompt = f"""You are a finance-ops reconciliation analyst. An order didn't cleanly
match across our three record sources.

A machine learning classifier predicted this exception's cause as {ml_result['cause']} with only {ml_result['confidence']:.0%} confidence — low enough that we want a second opinion.
Given the evidence below, do you agree, or does a different cause fit better?

Cause labels: {", ".join(CAUSE_LABELS)}
Matcher symptoms: {json.dumps(record.get('symptoms') or [])}

Evidence:
- Order ID: {record['order_id']}
- Ledger: {json.dumps(record['ledger'])} (appeared {record.get('ledger_dupe_count', 1)}x)
- Settlement: {json.dumps(record.get('settlement') or [])}
- Bank: {json.dumps(record.get('bank') or [])}

Respond ONLY with JSON:
{{
  "cause": "<one of the labels>",
  "confidence": <0-1 float>,
  "reasoning": "<one sentence citing numbers>",
  "agrees_with_ml": <true or false>
}}"""

    req_data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }

    time.sleep(5)
    try:
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            data=json.dumps(req_data).encode("utf-8")
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            text = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            parsed = json.loads(text)
            return {
                "cause": parsed.get("cause", "unexplained"),
                "confidence": to_float(parsed.get("confidence"), 0.5),
                "reasoning": parsed.get("reasoning", "Gemini escalation was completed without detailed text.")
            }
    except Exception as e:
        print(f"Gemini escalation call failed: {e}", file=sys.stderr)
        return None
