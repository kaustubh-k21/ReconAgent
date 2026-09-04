"""Two-leg matcher: ledger ↔ settlement ↔ bank. Unresolved rows become exceptions."""

import csv
import json
import os
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from itertools import combinations

DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(__file__), "matching_policy.json")

MONEY_QUANTUM = Decimal("0.01")

_DEFAULT_POLICY = {
    "sla_days": 7,
    "exact_max_settle_lag_days": 3,
    "standard_fee_rate": 0.02,
    "tds_rate": 0.01,
    "fee_rate_candidates": [0.012, 0.015, 0.02, 0.025, 0.03],
    "rounding_tolerance_rupees": 1.0,
    "date_lag_tolerance_days": 7,
    "lookalike": {
        "amount_epsilon_rupees": 0.02,
        "date_window_days": 7,
        "require_nonblank_bank_ref_for_amount_match": True,
    },
    "confidence": {"auto": 0.9, "review": 0.6},
    "severity": {
        "critical_amount": 10000,
        "critical_age_days": 5,
        "high_amount": 5000,
        "high_age_days": 3,
        "medium_amount": 1000,
        "medium_age_days": 1,
    },
    "rules": [
        {"id": "exact_standard_fee", "priority": 1, "type": "exact",
         "auto_confirm": True, "confidence": 1.0},
        {"id": "tolerance_fee_tds_round", "priority": 10, "type": "tolerance",
         "auto_confirm": True, "confidence": 0.9},
        {"id": "rounding_review", "priority": 11, "type": "rounding",
         "auto_confirm": False, "confidence": 0.75},
        {"id": "batch_n_to_1", "priority": 15, "type": "batch",
         "auto_confirm": True, "confidence": 0.9},
        {"id": "date_lag", "priority": 20, "type": "date_lag",
         "auto_confirm": True, "confidence": 0.9, "max_days": 7},
    ],
}


def load_policy(path=None):
    path = path or DEFAULT_POLICY_PATH
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return dict(_DEFAULT_POLICY)


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def to_money(x, default=None):
    """Parse a money field as Decimal quantized to paise."""
    if x is None or x == "":
        return default
    if isinstance(x, Decimal):
        return x.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    try:
        return Decimal(str(x).strip()).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return default


def to_float(x, default=None):
    """Money-safe float via Decimal (JSON / call-site compatibility)."""
    m = to_money(x, None)
    if m is None:
        return default
    return float(m)


def money_round(value) -> float:
    """Quantize an accumulated amount to paise and return float."""
    if value is None:
        return 0.0
    if not isinstance(value, Decimal):
        value = to_money(value, Decimal("0"))
    return float(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def lookalike_settings(policy):
    cfg = (policy or {}).get("lookalike") or {}
    defaults = _DEFAULT_POLICY["lookalike"]
    eps = to_money(
        cfg.get("amount_epsilon_rupees", defaults["amount_epsilon_rupees"]),
        Decimal("0.02"),
    )
    window = int(cfg.get("date_window_days", defaults["date_window_days"]))
    require_ref = bool(cfg.get(
        "require_nonblank_bank_ref_for_amount_match",
        defaults["require_nonblank_bank_ref_for_amount_match"],
    ))
    return eps, window, require_ref


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def rule_settings(policy, rule_id, default_confidence=0.5, default_auto=False):
    for rule in policy.get("rules", []):
        if rule.get("id") == rule_id:
            return (
                float(rule.get("confidence", default_confidence)),
                bool(rule.get("auto_confirm", default_auto)),
            )
    return default_confidence, default_auto


def _same_gateway_economics(a, b):
    fields = ("gross_amount", "fee", "tds", "refund_amount", "net_amount", "settlement_date")
    return all((a.get(k) or "") == (b.get(k) or "") for k in fields)


def _one_edit_apart(a, b):
    """True for one insertion/deletion/substitution; used only to flag review candidates."""
    a = (a or "").casefold()
    b = (b or "").casefold()
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = j = differences = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            differences += 1
            j += 1
            if differences > 1:
                return False
    differences += (len(b) - j) + (len(a) - i)
    return differences <= 1


_CHARGEBACK_TOKENS = (
    "chargeback", "charge back", "charge-back", "cbk", "dispute",
    "retrieval request", "representment",
)
_REVERSAL_TOKENS = (
    "reversal", "reversed", "void", "cancel", "cancelled", "canceled",
    "auth reverse", "settlement reverse",
)


def _bank_blob(bank_rows) -> str:
    parts = []
    for b in bank_rows or []:
        for key in ("narration", "reference", "utr"):
            parts.append(str(b.get(key) or ""))
    return " ".join(parts).casefold()


def _settlement_blob(settlement_row) -> str:
    parts = []
    for key in ("narration", "status", "settlement_batch_id", "order_id"):
        parts.append(str((settlement_row or {}).get(key) or ""))
    return " ".join(parts).casefold()


def looks_like_chargeback(bank_rows, settlement_row=None) -> bool:
    blob = _bank_blob(bank_rows) + " " + _settlement_blob(settlement_row)
    return any(tok in blob for tok in _CHARGEBACK_TOKENS)


def looks_like_reversal(bank_rows, settlement_row=None) -> bool:
    blob = _bank_blob(bank_rows) + " " + _settlement_blob(settlement_row)
    if any(tok in blob for tok in _REVERSAL_TOKENS):
        return True
    gross = to_money((settlement_row or {}).get("gross_amount"))
    net = to_money((settlement_row or {}).get("net_amount"))
    if gross is not None and gross < 0:
        return True
    if net is not None and net < 0:
        return True
    return False


def has_id_format_variance(record):
    """True when raw join keys differ only in formatting (case, hyphens)."""
    forms = []
    ledger = record.get("ledger") or {}
    src = (ledger.get("source_order_id") or "").strip()
    if src:
        forms.append(src)
    for row in record.get("settlement") or []:
        val = (row.get("source_order_id") or "").strip()
        if val:
            forms.append(val)
    for row in record.get("bank") or []:
        val = (row.get("source_reference") or "").strip()
        if val:
            forms.append(val)
    return len(set(forms)) > 1


def load_sources(data_dir="data"):
    ledger = load_csv(f"{data_dir}/internal_ledger.csv")
    settlement = load_csv(f"{data_dir}/settlement_report.csv")
    bank = load_csv(f"{data_dir}/bank_statement.csv")

    settlement_by_id = {}
    for row in settlement:
        settlement_by_id.setdefault(row["order_id"], []).append(row)

    bank_by_ref = {}
    for row in bank:
        bank_by_ref.setdefault(row["reference"], []).append(row)

    # N:1 batch index: batch_id -> list of settlement rows
    settlements_by_batch = defaultdict(list)
    for row in settlement:
        bid = (row.get("settlement_batch_id") or "").strip()
        if bid:
            settlements_by_batch[bid].append(row)

    return ledger, settlement_by_id, bank_by_ref, settlements_by_batch


def check_leg_a(ledger_row, settlement_rows, policy):
    """Leg A: ledger ↔ settlement. Returns status dict or None if no settlement."""
    if not settlement_rows:
        return {"leg": "A", "status": "missing_settlement"}

    amount = to_float(ledger_row["amount"])

    if len(settlement_rows) > 1:
        if all(_same_gateway_economics(settlement_rows[0], row)
               for row in settlement_rows[1:]):
            return {
                "leg": "A", "status": "cardinality_break",
                "subtype": "duplicate_gateway",
                "note": f"{len(settlement_rows)} economically identical settlement rows",
            }

        gross_total = money_round(sum(
            (to_money(r.get("gross_amount"), Decimal("0")) for r in settlement_rows),
            Decimal("0"),
        ))
        net_total = money_round(sum(
            (to_money(r.get("net_amount"), Decimal("0")) for r in settlement_rows),
            Decimal("0"),
        ))
        math_ok = all(
            abs(
                (to_money(r.get("net_amount"), Decimal("0"))
                 - (
                     to_money(r.get("gross_amount"), Decimal("0"))
                     - to_money(r.get("fee"), Decimal("0"))
                     - to_money(r.get("tds"), Decimal("0"))
                 ))
            ) <= Decimal("0.02")
            for r in settlement_rows
        )
        amt = to_money(amount)
        if amt is not None and abs(to_money(gross_total) - amt) <= Decimal("0.02") and math_ok:
            return {
                "leg": "A", "status": "posted", "rule_id": "one_to_many",
                "confidence": 0.9, "auto_confirm": True,
                "aggregated_net": net_total,
                "reasons": [f"one_to_many ({len(settlement_rows)} settlements)"],
            }
        return {
            "leg": "A", "status": "cardinality_break",
            "subtype": "multiple_gateway_records",
            "note": f"{len(settlement_rows)} settlement rows do not aggregate to the ledger",
        }

    s = settlement_rows[0]
    gross = to_float(s.get("gross_amount"))
    fee = to_float(s.get("fee"), 0.0)
    tds = to_float(s.get("tds"), 0.0)
    net = to_float(s.get("net_amount"))
    std = policy["standard_fee_rate"]
    candidates = policy["fee_rate_candidates"]

    if None in (amount, gross, net):
        return {"leg": "A", "status": "data_mismatch", "note": "unparseable amounts"}

    if abs(amount - gross) > 0.01:
        return {"leg": "A", "status": "data_mismatch",
                "note": f"ledger {amount} vs gross {gross}"}

    expected_fee = round(amount * std, 2)
    expected_net = round(amount - expected_fee, 2)
    if abs(net - expected_net) < 0.01:
        return {"leg": "A", "status": "posted", "rule_id": "exact_standard_fee",
                "confidence": 1.0}

    if abs(net - (amount - fee - tds)) < 0.01:
        reasons = []
        implied_fee = round(fee / amount, 4) if amount else std
        if abs(implied_fee - std) > 0.001:
            reasons.append(f"fee_variance ({implied_fee:.2%})")
        if tds and tds > 0.01:
            reasons.append(f"tds_withheld ({tds:.2f})")
        confidence, auto = rule_settings(policy, "tolerance_fee_tds_round", 0.9, True)
        return {
            "leg": "A", "status": "posted", "rule_id": "tolerance_fee_tds_round",
            "confidence": confidence, "auto_confirm": auto,
            "reasons": reasons or ["fee_tds_math"],
        }

    for rate in candidates:
        if abs(net - (amount - round(amount * rate, 2))) < 0.01:
            confidence, auto = rule_settings(policy, "tolerance_fee_tds_round", 0.9, True)
            return {
                "leg": "A", "status": "posted", "rule_id": "tolerance_fee_tds_round",
                "confidence": confidence, "auto_confirm": auto,
                "reasons": [f"fee_variance ({rate:.2%})"],
            }

    expected_tds = round(amount * policy.get("tds_rate", 0.01), 2)
    if abs(net - (amount - expected_fee - expected_tds)) < 0.01:
        confidence, auto = rule_settings(policy, "tolerance_fee_tds_round", 0.9, True)
        return {
            "leg": "A", "status": "posted", "rule_id": "tolerance_fee_tds_round",
            "confidence": confidence, "auto_confirm": auto,
            "reasons": ["tds_withheld"],
        }

    return {"leg": "A", "status": "under_or_over", "note": f"net {net} vs expected {expected_net}"}


def check_leg_b_one_to_one(settlement_row, bank_rows, policy, as_of, closed_batch=False):
    """Leg B 1:1: one settlement vs bank rows keyed by order_id."""
    sla = policy["sla_days"]
    rounding = policy["rounding_tolerance_rupees"]
    lag_max = policy["date_lag_tolerance_days"]

    settle_date = parse_date(settlement_row.get("settlement_date"))
    net = to_float(settlement_row.get("net_amount"))

    if not bank_rows:
        if settle_date and as_of:
            age = (as_of - settle_date).days
            # Closed historical files have no live SLA clock.
            if not closed_batch and age <= sla:
                return {"leg": "B", "status": "pending", "rule_id": "pending_bank",
                        "confidence": 0.95,
                        "note": f"bank credit not yet received ({age}d < SLA {sla}d)"}
            return {"leg": "B", "status": "aged_missing", "rule_id": None,
                    "note": f"bank credit missing {age}d past SLA {sla}d",
                    "age_days": age}
        return {"leg": "B", "status": "aged_missing", "note": "no bank credit"}

    if len(bank_rows) > 1:
        credits = [to_float(b.get("credit_amount"), 0.0) for b in bank_rows]
        debits = [to_float(b.get("debit_amount"), 0.0) for b in bank_rows]
        positive_credits = [c for c in credits if c > 0]
        total_debits = sum(d for d in debits if d > 0) + sum(-c for c in credits if c < 0)
        refund = to_float(settlement_row.get("refund_amount"), 0.0)

        if (len(positive_credits) >= 2 and len(set(positive_credits)) == 1
                and total_debits < 0.01):
            return {"leg": "B", "status": "cardinality_break",
                    "subtype": "duplicate_bank_credit",
                    "note": f"{len(positive_credits)} identical bank credits"}

        total_credit = money_round(
            sum((to_money(c, Decimal("0")) for c in positive_credits), Decimal("0"))
            - to_money(total_debits, Decimal("0"))
        )
        if refund > 0.01 or total_debits > 0.01:
            if abs(total_credit) <= rounding:
                return {
                    "leg": "B", "status": "refund_full",
                    "note": f"bank credits/debits net to {total_credit:.2f}",
                    "refund_amount": refund or total_debits,
                }
            if net is not None and 0 <= total_credit < net - rounding:
                return {
                    "leg": "B", "status": "refund_partial",
                    "note": f"bank net {total_credit:.2f} after refund/debit evidence",
                    "refund_amount": refund or total_debits,
                }
        return {"leg": "B", "status": "cardinality_break",
                "note": f"{len(bank_rows)} bank rows"}

    b = bank_rows[0]
    credit = to_float(b.get("credit_amount"))
    bank_date = parse_date(b.get("credit_date"))
    if net is None or credit is None:
        return {"leg": "B", "status": "data_mismatch", "note": "unparseable bank/net"}

    delta = money_round(to_money(credit) - to_money(net))
    lag_days = (bank_date - settle_date).days if (bank_date and settle_date) else 0

    if abs(delta) < 0.01:
        if lag_days <= 0:
            return {"leg": "B", "status": "posted", "rule_id": "exact_standard_fee",
                    "confidence": 1.0, "lag_days": lag_days}
        # Same amount with lag is still a match; same-day stays exact.
        confidence, auto = rule_settings(policy, "date_lag", 0.9, True)
        if lag_days > lag_max:
            confidence = min(confidence, 0.85)
        return {
            "leg": "B", "status": "posted", "rule_id": "date_lag",
            "confidence": confidence, "auto_confirm": auto,
            "reasons": [f"settlement_to_bank_lag ({lag_days}d)"],
            "lag_days": lag_days,
        }

    # Sub-rupee can auto-clear. A full rupee at the cap cannot (Hard false MATCH).
    if abs(delta) < rounding:
        confidence, auto = rule_settings(policy, "tolerance_fee_tds_round", 0.9, True)
        return {
            "leg": "B", "status": "posted", "rule_id": "tolerance_fee_tds_round",
            "confidence": confidence, "auto_confirm": auto,
            "reasons": [f"rounding_diff ({delta:+.2f})"],
        }

    if abs(delta - rounding) < 0.005 or abs(delta + rounding) < 0.005:
        return {
            "leg": "B", "status": "unexplained_variance",
            "note": f"unexplained {delta:+.2f} at rounding cap {rounding:.2f}",
            "delta": delta,
        }

    if delta < -rounding:
        refund = to_float(settlement_row.get("refund_amount"), 0.0)
        if refund > 0.01:
            cause = "refund_full" if credit <= rounding or refund >= net - rounding else "refund_partial"
            return {
                "leg": "B", "status": cause,
                "note": f"gateway reports refund {refund:.2f}; bank shortfall {abs(delta):.2f}",
                "refund_amount": refund,
            }
        # No gateway refund memo — classify reverse flows before generic shortfall.
        if looks_like_chargeback(bank_rows, settlement_row):
            return {
                "leg": "B", "status": "chargeback",
                "note": f"chargeback/dispute markers with bank shortfall {abs(delta):.2f}",
                "delta": delta,
            }
        if looks_like_reversal(bank_rows, settlement_row):
            return {
                "leg": "B", "status": "reversal",
                "note": f"reversal markers with bank shortfall {abs(delta):.2f}",
                "delta": delta,
            }
        return {"leg": "B", "status": "under_amount",
                "note": f"shortfall {abs(delta):.2f}", "delta": delta}

    return {"leg": "B", "status": "over_amount",
            "note": f"overage {delta:.2f}", "delta": delta}


def check_leg_b_one_to_many(settlement_rows, bank_rows, policy):
    """Reconcile split settlements against one or more bank postings."""
    expected = money_round(sum(
        (to_money(r.get("net_amount"), Decimal("0")) for r in settlement_rows),
        Decimal("0"),
    ))
    positive = sum(
        (max(to_money(r.get("credit_amount"), Decimal("0")), Decimal("0")) for r in bank_rows),
        Decimal("0"),
    )
    debits = sum(
        (max(to_money(r.get("debit_amount"), Decimal("0")), Decimal("0")) for r in bank_rows),
        Decimal("0"),
    )
    actual = money_round(positive - debits)
    if bank_rows and abs(to_money(actual) - to_money(expected)) <= Decimal("0.02"):
        return {
            "leg": "B", "status": "posted", "rule_id": "one_to_many",
            "confidence": 0.9, "auto_confirm": True,
            "reasons": [
                f"one_to_many ({len(settlement_rows)} settlements, "
                f"{len(bank_rows)} bank postings)"
            ],
        }
    delta = money_round(actual - expected)
    return {
        "leg": "B",
        "status": "under_amount" if delta < 0 else "over_amount",
        "delta": delta,
        "note": f"split settlement total {expected:.2f} vs bank net {actual:.2f}",
    }


def check_leg_b_batch(settlement_row, settlements_by_batch, bank_by_ref, policy):
    """Leg B N:1: sum nets in settlement_batch_id, match bank reference = batch_id."""
    bid = (settlement_row.get("settlement_batch_id") or "").strip()
    if not bid:
        return None

    members = settlements_by_batch.get(bid, [])
    if len(members) < 2:
        return None

    bank_rows = bank_by_ref.get(bid, [])
    if not bank_rows:
        return {"leg": "B", "status": "aged_missing",
                "note": f"batch {bid} has no bank credit", "batch_id": bid}

    if len(bank_rows) != 1:
        return {"leg": "B", "status": "cardinality_break",
                "note": f"batch {bid} has {len(bank_rows)} bank rows", "batch_id": bid}

    total_net = money_round(sum(
        (to_money(m.get("net_amount"), Decimal("0")) for m in members),
        Decimal("0"),
    ))
    credit = to_money(bank_rows[0].get("credit_amount"))
    if credit is None:
        return {"leg": "B", "status": "data_mismatch", "batch_id": bid}

    if abs(credit - to_money(total_net)) < Decimal("0.01"):
        return {
            "leg": "B", "status": "posted", "rule_id": "batch_n_to_1",
            "confidence": 0.9, "auto_confirm": True,
            "batch_id": bid, "batch_size": len(members),
            "reasons": [f"n_to_1 batch ({len(members)} settlements → 1 credit)"],
            "bank": bank_rows,
        }

    delta = money_round(credit - to_money(total_net))
    if delta < 0:
        return {"leg": "B", "status": "under_amount", "batch_id": bid,
                "note": f"batch shortfall {abs(delta):.2f}", "delta": delta}
    return {"leg": "B", "status": "over_amount", "batch_id": bid,
            "note": f"batch overage {delta:.2f}", "delta": delta}


def _looks_like_batch_payout(bank_row):
    ref = (bank_row.get("reference") or "").casefold()
    nar = (bank_row.get("narration") or "").casefold()
    tokens = ("payout", "sweep", "settlement", "batch")
    return any(token in ref or token in nar for token in tokens)


def _subset_sum_unique(pool, target, bank_date, max_size=6, max_date_gap=90):
    """Return the unique 2..N settlement subset that sums to target, or None."""
    hits = []
    n = min(max_size, len(pool))
    for k in range(2, n + 1):
        for combo in combinations(pool, k):
            total = money_round(sum(
                (to_money(row.get("net_amount"), Decimal("0")) for row in combo),
                Decimal("0"),
            ))
            if abs(to_money(total) - to_money(target, Decimal("0"))) > Decimal("0.02"):
                continue
            if bank_date:
                too_far = False
                for row in combo:
                    settle_date = parse_date(row.get("settlement_date"))
                    if settle_date and abs((bank_date - settle_date).days) > max_date_gap:
                        too_far = True
                        break
                if too_far:
                    continue
            hits.append(combo)
            if len(hits) > 1:
                return None
    return hits[0] if hits else None


def discover_n_to_1(settlement_by_id, bank_by_ref, ledger_ids, policy=None):
    """Recover N:1 payouts when the settlement file has no batch column."""
    discovered = {}
    known_ids = set(ledger_ids) | set(settlement_by_id.keys())
    leftover_banks = []
    for ref, rows in bank_by_ref.items():
        if not ref or ref in known_ids:
            continue
        leftover_banks.extend(rows)

    leftover_banks = [row for row in leftover_banks if _looks_like_batch_payout(row)]

    unmatched = []
    for oid, rows in settlement_by_id.items():
        if bank_by_ref.get(oid):
            continue
        unmatched.extend(rows)

    used = set()
    for bank_row in leftover_banks:
        credit = to_float(bank_row.get("credit_amount"))
        if credit is None:
            continue
        pool = [row for row in unmatched if row.get("order_id") not in used]
        combo = _subset_sum_unique(pool, credit, parse_date(bank_row.get("credit_date")))
        if not combo:
            continue
        members = list(combo)
        for row in members:
            oid = row.get("order_id")
            used.add(oid)
            discovered[oid] = {
                "leg": "B",
                "status": "posted",
                "rule_id": "batch_n_to_1",
                "confidence": 0.9,
                "auto_confirm": True,
                "batch_id": bank_row.get("reference"),
                "batch_size": len(members),
                "reasons": [
                    f"n_to_1 batch ({len(members)} settlements → 1 credit "
                    f"{bank_row.get('reference')})"
                ],
                "bank": [bank_row],
            }
    return discovered


def candidate_symptoms(record, leg_a, leg_b, all_settlements, all_bank, policy=None):
    """Flag lookalike counterparts. Never auto-links them."""
    symptoms = []
    oid = record["order_id"]
    ledger = record["ledger"]
    amount = to_money(ledger.get("amount"))
    order_date = parse_date(ledger.get("order_date"))
    eps, window, require_ref = lookalike_settings(policy)

    if leg_a and leg_a.get("status") == "missing_settlement" and amount is not None:
        candidates = []
        for row in all_settlements:
            other_id = row.get("order_id") or ""
            gross = to_money(row.get("gross_amount"))
            settle_date = parse_date(row.get("settlement_date"))
            close_date = (
                order_date is None or settle_date is None
                or abs((settle_date - order_date).days) <= window
            )
            if (
                other_id != oid
                and gross is not None
                and abs(gross - amount) <= eps
                and close_date
            ):
                candidates.append(row)
        if len(candidates) == 1:
            symptoms.append("wrong_transaction_candidate")
            record["candidate_settlement_ids"] = [c.get("order_id") for c in candidates]
        elif len(candidates) > 1:
            symptoms.append("ambiguous_match_candidate")
            record["candidate_settlement_ids"] = [c.get("order_id") for c in candidates[:5]]

    if leg_b and leg_b.get("status") in (
        "pending", "aged_missing", "under_amount", "over_amount",
        "chargeback", "reversal", "refund_partial", "refund_full",
    ):
        settlement_rows = record.get("settlement") or []
        expected = None
        settle_date = None
        if settlement_rows:
            expected = sum(
                (to_money(r.get("net_amount"), Decimal("0")) for r in settlement_rows),
                Decimal("0"),
            )
            settle_date = parse_date(settlement_rows[0].get("settlement_date"))
        candidates = []
        similar_ref = []
        for row in all_bank:
            ref = row.get("reference") or ""
            credit = to_money(row.get("credit_amount"), Decimal("0"))
            debit = to_money(row.get("debit_amount"), Decimal("0"))
            value = credit - debit
            bank_date = parse_date(row.get("credit_date"))
            close_date = (
                settle_date is None or bank_date is None
                or abs((bank_date - settle_date).days) <= window
            )
            if (
                ref != oid
                and expected is not None
                and abs(value - expected) <= eps
                and close_date
            ):
                if require_ref and not ref:
                    continue
                candidates.append(row)
            if ref and ref != oid and _one_edit_apart(ref, oid):
                similar_ref.append(row)
        if similar_ref:
            symptoms.append("wrong_transaction_candidate")
            record["candidate_bank_refs"] = [r.get("reference") for r in similar_ref[:5]]
        elif candidates:
            # Blank-ref / same-value lookalikes are unsafe to auto-link.
            symptoms.append(
                "ambiguous_match_candidate" if len(candidates) > 1 or not candidates[0].get("reference")
                else "wrong_transaction_candidate"
            )
            record["candidate_bank_refs"] = [r.get("reference") or "<blank>" for r in candidates[:5]]

    return symptoms


def collect_symptoms(record, leg_a, leg_b):
    symptoms = []
    if record.get("ledger_dupe_count", 1) > 1:
        symptoms.append("ledger_dupe")

    if leg_a and leg_a.get("status") == "missing_settlement":
        symptoms.append("one_sided_no_settlement")
    elif leg_a and leg_a.get("subtype") == "duplicate_gateway":
        symptoms.append("duplicate_gateway")
    elif leg_a and leg_a.get("status") == "cardinality_break":
        symptoms.append("multiple_gateway_records")
    elif leg_a and leg_a.get("status") in ("under_or_over", "data_mismatch"):
        symptoms.append("data_mismatch" if leg_a["status"] == "data_mismatch" else "leg_a_amount_break")

    if leg_b:
        st = leg_b.get("status")
        if st == "pending":
            symptoms.append("one_sided_no_bank_within_sla")
        elif st == "aged_missing":
            symptoms.append("one_sided_no_bank_past_sla")
        elif st == "under_amount":
            symptoms.append("under_amount")
        elif st == "over_amount":
            symptoms.append("over_amount")
        elif st == "cardinality_break":
            symptoms.append("cardinality_break")
        elif st == "data_mismatch":
            symptoms.append("data_mismatch")
        elif st == "refund_full":
            symptoms.append("refund_full")
        elif st == "refund_partial":
            symptoms.append("refund_partial")
        elif st == "chargeback":
            symptoms.append("chargeback")
        elif st == "reversal":
            symptoms.append("reversal")
        elif st == "unexplained_variance":
            symptoms.append("unexplained_variance")

    return symptoms


def severity_for(amount, age_days, policy):
    sev = policy.get("severity", {})
    crit_a = sev.get("critical_amount", 10000)
    crit_d = sev.get("critical_age_days", 5)
    high_a = sev.get("high_amount", 5000)
    high_d = sev.get("high_age_days", 3)
    med_a = sev.get("medium_amount", 1000)
    med_d = sev.get("medium_age_days", 1)
    age = age_days or 0
    amt = amount or 0
    if amt >= crit_a or age >= crit_d:
        return "critical"
    if amt >= high_a or age >= high_d:
        return "high"
    if amt >= med_a or age >= med_d:
        return "medium"
    return "low"


def _infer_as_of(ledger, settlement_by_id, bank_by_ref):
    dates = []
    for row in ledger:
        d = parse_date(row.get("order_date"))
        if d:
            dates.append(d)
    for rows in settlement_by_id.values():
        for r in rows:
            d = parse_date(r.get("settlement_date"))
            if d:
                dates.append(d)
    for rows in bank_by_ref.values():
        for r in rows:
            d = parse_date(r.get("credit_date"))
            if d:
                dates.append(d)
    if not dates:
        return date.today()
    # Latest source date, not wall-clock today, so pending rows stay pending.
    return max(dates)


def run_matching(data_dir="data", policy_path=None, as_of=None):
    policy = load_policy(policy_path)
    ledger, settlement_by_id, bank_by_ref, settlements_by_batch = load_sources(data_dir)
    all_settlements = [row for rows in settlement_by_id.values() for row in rows]
    all_bank = [row for rows in bank_by_ref.values() for row in rows]
    ledger_ids = [row["order_id"] for row in ledger]

    as_of_from_meta = False
    if as_of is None:
        meta_path = os.path.join(data_dir, "recon_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            as_of = parse_date(meta.get("as_of"))
            as_of_from_meta = as_of is not None
        if as_of is None:
            as_of = _infer_as_of(ledger, settlement_by_id, bank_by_ref)
    elif isinstance(as_of, str):
        as_of = parse_date(as_of)
    closed_batch = not as_of_from_meta
    discovered_batches = discover_n_to_1(
        settlement_by_id, bank_by_ref, ledger_ids, policy
    )

    order_counts = {}
    for row in ledger:
        order_counts[row["order_id"]] = order_counts.get(row["order_id"], 0) + 1

    results = {
        "exact": [],
        "fuzzy": [],
        "review": [],
        "pending": [],
        "exceptions": [],
        "leg_summary": {"A_posted": 0, "A_failed": 0, "B_posted": 0,
                        "B_pending": 0, "B_review": 0, "B_failed": 0},
        "rule_counts": defaultdict(int),
        "policy": {
            "sla_days": policy["sla_days"],
            "as_of": as_of.isoformat() if as_of else None,
        },
    }
    seen_duplicate_ids = set()

    for row in ledger:
        oid = row["order_id"]
        s_rows = settlement_by_id.get(oid, [])
        # Prefer order_id bank rows; batch members resolve via the batch path.
        b_rows = bank_by_ref.get(oid, [])

        record = {
            "order_id": oid,
            "ledger": row,
            "settlement": s_rows,
            "bank": b_rows,
            "ledger_dupe_count": order_counts[oid],
        }

        if order_counts[oid] > 1:
            if oid not in seen_duplicate_ids:
                seen_duplicate_ids.add(oid)
                leg_a = check_leg_a(row, s_rows, policy) if s_rows else {
                    "leg": "A", "status": "missing_settlement"}
                # Keep bank evidence on duplicate ledger rows.
                leg_b = None
                if s_rows:
                    batch = check_leg_b_batch(s_rows[0], settlements_by_batch, bank_by_ref, policy)
                    leg_b = batch or check_leg_b_one_to_one(
                        s_rows[0], b_rows, policy, as_of, closed_batch=closed_batch
                    )
                symptoms = collect_symptoms(record, leg_a, leg_b)
                if "ledger_dupe" not in symptoms:
                    symptoms.insert(0, "ledger_dupe")
                amount = to_float(row.get("amount"), 0.0)
                age = 0
                od = parse_date(row.get("order_date"))
                if od and as_of:
                    age = (as_of - od).days
                results["exceptions"].append({
                    **record,
                    "leg_a": leg_a,
                    "leg_b": leg_b,
                    "symptoms": symptoms,
                    "severity": severity_for(amount, age, policy),
                    "age_days": age,
                })
                results["leg_summary"]["A_failed"] += 1
            continue

        # --- Leg A ---
        leg_a = check_leg_a(row, s_rows, policy)
        if leg_a["status"] != "posted":
            results["leg_summary"]["A_failed"] += 1
            leg_b = None
            if s_rows:
                leg_b = discovered_batches.get(oid)
                if leg_b is None:
                    leg_b = check_leg_b_one_to_one(
                        s_rows[0], b_rows, policy, as_of, closed_batch=closed_batch
                    )
                if leg_b.get("bank"):
                    record["bank"] = leg_b["bank"]
            symptoms = collect_symptoms(record, leg_a, leg_b)
            symptoms.extend(candidate_symptoms(
                record, leg_a, leg_b, all_settlements, all_bank, policy=policy
            ))
            amount = to_float(row.get("amount"), 0.0)
            age = 0
            od = parse_date(row.get("order_date"))
            if od and as_of:
                age = (as_of - od).days
            results["exceptions"].append({
                **record,
                "leg_a": leg_a,
                "leg_b": None,
                "symptoms": symptoms,
                "severity": severity_for(amount, age, policy),
                "age_days": age,
            })
            continue

        results["leg_summary"]["A_posted"] += 1

        # --- Leg B: try batch N:1 first, else 1:1 ---
        s0 = s_rows[0]
        if leg_a.get("rule_id") == "one_to_many":
            leg_b = check_leg_b_one_to_many(s_rows, b_rows, policy)
        else:
            leg_b = check_leg_b_batch(s0, settlements_by_batch, bank_by_ref, policy)
            if leg_b is None:
                leg_b = discovered_batches.get(oid)
            if leg_b is None:
                leg_b = check_leg_b_one_to_one(
                    s0, b_rows, policy, as_of, closed_batch=closed_batch
                )
            if leg_b.get("bank"):
                record["bank"] = leg_b["bank"]

        st = leg_b["status"]
        combined_reasons = list(leg_a.get("reasons") or []) + list(leg_b.get("reasons") or [])
        if has_id_format_variance(record) and "id_format" not in combined_reasons:
            combined_reasons.append("id_format")
        # Prefer Leg B's rule when it did real work; else Leg A.
        rule_id = leg_b.get("rule_id") or leg_a.get("rule_id")
        if st == "posted" and leg_b.get("rule_id") == "exact_standard_fee" and leg_a.get("rule_id") != "exact_standard_fee":
            # Bank posted exact; ledger still had a fee/TDS variance — keep that rule.
            rule_id = leg_a.get("rule_id")
            combined_reasons = list(leg_a.get("reasons") or []) + list(leg_b.get("reasons") or [])
        conf = min(leg_a.get("confidence", 1.0), leg_b.get("confidence", 1.0))

        base = {
            **record,
            "leg_a": leg_a,
            "leg_b": leg_b,
            "matched_by_rule": rule_id,
            "confidence": conf,
            "note": "; ".join(combined_reasons) if combined_reasons else leg_b.get("note") or leg_a.get("note"),
        }

        if st == "posted":
            selected_confidence, selected_auto = rule_settings(
                policy, rule_id, conf, bool(leg_a.get("auto_confirm", True)
                                           and leg_b.get("auto_confirm", True))
            )
            auto_threshold = float(policy.get("confidence", {}).get("auto", 0.9))
            auto_confirmed = (
                selected_auto and conf >= auto_threshold
                and selected_confidence >= auto_threshold
            )
            if not auto_confirmed:
                results["leg_summary"]["B_review"] += 1
                if rule_id:
                    results["rule_counts"][rule_id] += 1
                results["review"].append({
                    **base,
                    "tier": "review",
                    "auto_confirmed": False,
                    "note": (
                        f"Needs review: confidence {conf:.2f} below auto policy "
                        f"{auto_threshold:.2f}"
                    ),
                })
                continue
            results["leg_summary"]["B_posted"] += 1
            if rule_id:
                results["rule_counts"][rule_id] += 1
            # Exact only if both legs are exact and no tolerance reasons.
            if (leg_a.get("rule_id") == "exact_standard_fee"
                    and leg_b.get("rule_id") == "exact_standard_fee"
                    and not combined_reasons):
                results["exact"].append({**base, "tier": "exact", "auto_confirmed": True,
                                         "matched_by_rule": "exact_standard_fee"})
            elif leg_b.get("rule_id") == "batch_n_to_1":
                results["fuzzy"].append({
                    **base, "tier": "fuzzy", "auto_confirmed": True,
                    "matched_by_rule": "batch_n_to_1",
                    "note": "Matched after allowing for: " + "; ".join(combined_reasons)
                    if combined_reasons else base.get("note"),
                })
            else:
                results["fuzzy"].append({
                    **base, "tier": "fuzzy", "auto_confirmed": True,
                    "note": "Matched after allowing for: " + "; ".join(combined_reasons)
                    if combined_reasons else base.get("note"),
                })
            continue

        if st == "pending":
            # Lookalike bank rows are hints only — do not demote within-SLA pending
            # into an exception. Pending means the credit is still expected.
            results["leg_summary"]["B_pending"] += 1
            results["pending"].append({
                **base, "tier": "pending", "auto_confirmed": False,
                "matched_by_rule": "pending_bank",
            })
            continue

        if st == "review":
            results["leg_summary"]["B_review"] += 1
            if rule_id:
                results["rule_counts"][rule_id] += 1
            results["review"].append({
                **base, "tier": "review", "auto_confirmed": False,
                "matched_by_rule": rule_id or "date_lag",
                "note": "Needs review: " + "; ".join(combined_reasons)
                if combined_reasons else base.get("note"),
            })
            continue

        results["leg_summary"]["B_failed"] += 1
        symptoms = collect_symptoms(record, leg_a, leg_b)
        symptoms.extend(candidate_symptoms(
            record, leg_a, leg_b, all_settlements, all_bank, policy=policy
        ))
        amount = to_float(row.get("amount"), 0.0)
        age = leg_b.get("age_days")
        if age is None:
            settle_date = parse_date(s0.get("settlement_date"))
            if settle_date and as_of:
                age = max(0, (as_of - settle_date).days)
            else:
                age = 0
        results["exceptions"].append({
            **record,
            "leg_a": leg_a,
            "leg_b": leg_b,
            "symptoms": symptoms,
            "severity": severity_for(amount, age, policy),
            "age_days": age,
        })

    results["rule_counts"] = dict(results["rule_counts"])
    return results


if __name__ == "__main__":
    r = run_matching()
    total = (len(r["exact"]) + len(r["fuzzy"]) + len(r["review"])
             + len(r["pending"]) + len(r["exceptions"]))
    auto = len(r["exact"]) + len(r["fuzzy"])
    print(f"Exact: {len(r['exact'])}  Fuzzy: {len(r['fuzzy'])}  Review: {len(r['review'])}  "
          f"Pending: {len(r['pending'])}  Exceptions: {len(r['exceptions'])}")
    print(f"Auto-match rate: {auto / total:.1%}  Auto+review: {(auto + len(r['review'])) / total:.1%}")
    print(f"Leg summary: {r['leg_summary']}")
    print(f"Rules: {r['rule_counts']}")
