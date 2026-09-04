"""Presentation copy for exception rows. Does not change classifier labels."""

from __future__ import annotations

SLA_DAYS = 7

CAUSE_LABEL = {
    "wrong_transaction": "Possible reference mismatch",
    "ambiguous_match": "Ambiguous candidates",
    "aged_missing_bank": "Missing bank credit (past SLA)",
    "missing_settlement": "Missing settlement",
    "refund_partial": "Partial refund",
    "refund_full": "Full refund",
    "chargeback": "Chargeback / dispute",
    "reversal": "Settlement reversal",
    "duplicate_credit": "Duplicate bank credit",
    "duplicate_entry": "Duplicate ledger entry",
    "duplicate_gateway": "Duplicate settlement",
    "amount_mismatch": "Amount mismatch",
    "unexplained_variance": "Unexplained residual",
    "unexplained": "Needs review",
    "timing_lag": "Settlement-to-bank lag",
    "fee_variance": "Fee variance",
    "tds_deduction": "TDS withheld",
    "rounding": "Rounding difference",
    "pending_bank": "Pending bank credit",
    "operator_override": "Operator override",
}

DEFAULT_ACTION = "Review the source rows before clearing."


def format_inr(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return f"₹{n:,.2f}"


def _clean_refs(values) -> list[str]:
    out = []
    for raw in values or []:
        ref = str(raw or "").strip()
        if not ref or ref in ("<blank>", "None"):
            continue
        out.append(ref)
    return out


def _first_settlement(ex: dict) -> dict:
    rows = ex.get("settlement") or []
    return rows[0] if rows else {}


def _bank_rows(ex: dict) -> list[dict]:
    return list(ex.get("bank") or [])


def present_exception(ex: dict) -> dict:
    row = dict(ex)
    cause = row.get("cause") or "unexplained"
    link_suspicion = row.get("link_suspicion")
    symptoms = set(row.get("symptoms") or [])
    oid = str(row.get("order_id") or "")
    age = row.get("age_days")
    try:
        age_n = int(age) if age is not None else None
    except (TypeError, ValueError):
        age_n = None

    settlement = _first_settlement(row)
    banks = _bank_rows(row)
    net = format_inr(settlement.get("net_amount"))
    refund_raw = settlement.get("refund_amount")
    try:
        refund_n = float(refund_raw or 0)
    except (TypeError, ValueError):
        refund_n = 0.0
    refund = format_inr(refund_raw) if refund_n > 0.01 else None
    credit = format_inr(banks[0].get("credit_amount")) if banks else None
    debit_n = 0.0
    for b in banks:
        try:
            debit_n += float(b.get("debit_amount") or 0)
        except (TypeError, ValueError):
            pass
    debit = format_inr(debit_n) if debit_n > 0.01 else None
    bank_refs = _clean_refs(row.get("candidate_bank_refs"))
    settle_ids = _clean_refs(row.get("candidate_settlement_ids"))
    amount = format_inr(row.get("amount"))
    delta = (row.get("leg_b") or {}).get("delta")
    delta_disp = format_inr(abs(float(delta))) if delta not in (None, "") else None

    facts: list[str] = []
    action = DEFAULT_ACTION

    missing_bank = (
        "one_sided_no_bank_past_sla" in symptoms or cause == "aged_missing_bank"
    )
    if missing_bank:
        facts.append(f"Expected bank credit not found under {oid}." if oid else
                     "Expected bank credit not found under this order’s reference.")
        if age_n is not None:
            overdue = max(0, age_n - SLA_DAYS)
            if overdue:
                facts.append(
                    f"Credit is {age_n} days old — {overdue} day(s) past the {SLA_DAYS}-day SLA."
                )
            else:
                facts.append(f"Age is {age_n} day(s); SLA window is {SLA_DAYS} days.")
        if net:
            facts.append(f"Settled net {net} has no matching credit on this reference.")
        elif amount:
            facts.append(f"Ledger amount {amount} has no matching bank credit on this reference.")

    if "one_sided_no_settlement" in symptoms or cause == "missing_settlement":
        facts.append(
            f"{oid} is in the ledger but has no settlement row."
            if oid else "Ledger row has no matching settlement."
        )
        action = "Confirm whether the payment actually captured at the gateway."

    suspicion = link_suspicion or (
        "wrong_transaction" if "wrong_transaction_candidate" in symptoms or cause == "wrong_transaction"
        else "ambiguous_match" if "ambiguous_match_candidate" in symptoms or cause == "ambiguous_match"
        else None
    )
    if suspicion == "wrong_transaction":
        if bank_refs:
            shown = ", ".join(bank_refs[:3])
            facts.append(f"A similar credit appears under {shown} and was not auto-linked.")
        elif settle_ids:
            shown = ", ".join(settle_ids[:3])
            facts.append(f"A similar settlement appears under {shown} and was not auto-linked.")
        else:
            facts.append(
                "A similar amount or near-ID candidate exists on another reference and was not auto-linked."
            )
        action = "Verify the UTR and bank reference before clearing."

    if suspicion == "ambiguous_match":
        pool = bank_refs or settle_ids
        if pool:
            facts.append(
                f"Multiple plausible counterparts ({', '.join(pool[:4])}); no unique link."
            )
        else:
            facts.append("Multiple or unreferenced counterparts exist; no unique link.")
        action = "Choose the correct counterpart from the source files, or leave unmatched."

    if cause == "refund_partial" or "refund_partial" in symptoms:
        if refund:
            facts.append(f"Gateway reports a refund of {refund}.")
        if debit:
            facts.append(f"Bank shows a debit of {debit}.")
        if net and credit:
            facts.append(f"Settled net {net} versus bank credit {credit}.")
        if not any("refund" in f.lower() or "debit" in f.lower() or "versus" in f.lower() for f in facts):
            facts.append("Gateway refund or bank debit evidence for a partial reversal.")
        action = "Confirm the refund against the gateway payout and the bank debit."

    if cause == "refund_full" or "refund_full" in symptoms:
        if refund:
            facts.append(f"Gateway reports a full refund of {refund}.")
        if debit:
            facts.append(f"Bank debit of {debit} offsets the original credit.")
        if not facts:
            facts.append("Refund or reversing debit evidence nets the settled amount to zero.")
        action = "Confirm the reversal posted on both gateway and bank."

    if cause == "chargeback" or "chargeback" in symptoms:
        if debit:
            facts.append(f"Bank debit of {debit} looks like a chargeback/dispute pullback.")
        elif delta_disp:
            facts.append(f"Shortfall of {delta_disp} with chargeback/dispute markers and no gateway refund memo.")
        else:
            facts.append("Chargeback or dispute markers on the bank side without a gateway refund memo.")
        if net:
            facts.append(f"Original settled net was {net}.")
        action = "Open the dispute case with the acquirer; do not clear as a normal shortfall."

    if cause == "reversal" or "reversal" in symptoms:
        facts.append("Settlement or bank evidence indicates a void/reversal of the original capture.")
        if net and credit:
            facts.append(f"Settled net {net} versus bank credit {credit}.")
        action = "Confirm the void with the gateway and whether a replacement capture exists."

    if cause == "duplicate_credit" or "duplicate_credit" in symptoms or (
        "cardinality_break" in symptoms and len(banks) > 1
    ):
        n = len(banks) if len(banks) > 1 else 2
        if credit:
            facts.append(f"{n} identical bank credits of {credit} on this reference.")
        else:
            facts.append("Duplicate bank credits posted against this reference.")
        action = "Confirm with the bank whether one credit should be reversed."

    if cause == "duplicate_entry" or "ledger_dupe" in symptoms:
        facts.append("This order ID appears more than once in the ledger.")
        action = "Remove or merge the duplicate ledger entry."

    if cause == "duplicate_gateway":
        facts.append("Economically identical duplicate settlement rows exist.")
        action = "Keep one settlement row and investigate the duplicate capture."

    if cause == "amount_mismatch" or "under_amount" in symptoms or "over_amount" in symptoms:
        if net and credit:
            facts.append(f"Settled net {net} does not equal bank credit {credit}.")
        elif delta_disp:
            facts.append(f"Unexplained gap of {delta_disp} between settlement and bank.")
        elif not facts:
            facts.append("Ledger, settlement, and bank amounts do not agree.")
        if cause == "amount_mismatch":
            action = "Trace the residual to fee, TDS, refund, or a posting error."

    if cause == "unexplained_variance" or "unexplained_variance" in symptoms:
        if delta_disp:
            facts.append(f"Residual of {delta_disp} with no fee, TDS, or refund memo.")
        else:
            facts.append("A small residual remains with no supporting fee, TDS, or refund memo.")
        action = "Decide whether the residual is rounding or needs a manual adjustment."

    seen = set()
    unique_facts = []
    for fact in facts:
        if fact not in seen:
            seen.add(fact)
            unique_facts.append(fact)
    facts = unique_facts

    if not facts:
        reasoning = (row.get("reasoning") or "").strip()
        if reasoning and "symptoms=" not in reasoning:
            facts.append(reasoning)
        else:
            facts.append("The matcher could not tie this row out automatically.")

    if cause in ("aged_missing_bank",) and suspicion == "wrong_transaction":
        teaser = "Bank credit missing past SLA. Similar credit found elsewhere — not auto-linked."
    elif cause == "wrong_transaction" and missing_bank:
        teaser = "Bank credit missing past SLA. Similar credit found elsewhere — not auto-linked."
    elif cause == "wrong_transaction" or (
        cause not in ("ambiguous_match",) and suspicion == "wrong_transaction" and not missing_bank
    ):
        teaser = "Similar counterpart found on another reference — not auto-linked."
    elif cause == "aged_missing_bank" or missing_bank:
        teaser = "Bank credit still missing after the SLA window."
    elif cause in ("refund_partial", "refund_full", "chargeback", "reversal"):
        teaser = facts[0]
    elif cause == "ambiguous_match" or suspicion == "ambiguous_match":
        teaser = "More than one plausible counterpart — not auto-linked."
    else:
        teaser = facts[0]

    row["display_cause"] = CAUSE_LABEL.get(cause, cause.replace("_", " ").capitalize())
    if link_suspicion:
        row["display_link_suspicion"] = CAUSE_LABEL.get(
            link_suspicion, link_suspicion.replace("_", " ").capitalize()
        )
    row["evidence_teaser"] = teaser
    row["evidence_facts"] = facts
    row["evidence_action"] = action
    for bulky in ("settlement", "bank", "ledger"):
        row.pop(bulky, None)
    return row


def enrich_classified(classified: list[dict], matcher_exceptions: list[dict]) -> list[dict]:
    by_oid = {r.get("order_id"): r for r in matcher_exceptions or []}
    out = []
    for rec in classified:
        src = by_oid.get(rec.get("order_id")) or {}
        merged = dict(rec)
        for key in (
            "candidate_bank_refs",
            "candidate_settlement_ids",
            "settlement",
            "bank",
            "ledger",
        ):
            if not merged.get(key) and src.get(key) is not None:
                merged[key] = src.get(key)
        out.append(present_exception(merged))
    return out
