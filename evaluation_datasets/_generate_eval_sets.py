#!/usr/bin/env python3
"""Standalone eval-set generator. Does not import matcher or data_generator.

If both a delay and a fee variance apply, the label is FUZZY (lag is the join).
Customer-name spelling is not FUZZY — names are not join keys.
"""

from __future__ import annotations

import csv
import json
import os
import random
import zipfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CUSTOMERS = [
    "Ananya Sharma", "Rohit Kulkarni", "Meera Iyer", "Vikram Singh",
    "Kavya Reddy", "Sneha Pillai", "Arjun Malhotra", "Diya Banerjee",
    "Nikhil Joshi", "Pooja Nair", "Aarav Patel", "Ishita Ghosh",
    "Kunal Deshmukh", "Riya Chakraborty", "Harsh Vardhan", "Tanvi Rao",
    "Aditya Menon", "Shreya Bansal", "Yash Agarwal", "Nandini Kaur",
    "Siddharth Bose", "Aisha Khan", "Manish Tiwari", "Leela Krishnan",
    "Gaurav Saxena", "Fatima Sheikh", "Pranav Hegde", "Jhanvi Kapoor",
]

NEAR_NAMES = {
    "Rajesh Kumar": "Rajesh Kumarr",
    "Sanjay Mehta": "Sanjay Mehra",
    "Neha Gupta": "Neha Gupte",
    "Amit Verma": "Amit Varma",
}

BANKS = ["HDFC", "ICIC", "SBIN", "UTIB", "KKBK"]

# Bank/settlement residuals at or below this are unexplained_variance;
# anything larger without refund evidence is amount_mismatch.
UNEXPLAINED_VARIANCE_MAX_INR = 10.0


def reason_for_amount_gap(variance: float) -> str:
    if abs(float(variance)) <= UNEXPLAINED_VARIANCE_MAX_INR:
        return "unexplained_variance"
    return "amount_mismatch"


def inr(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            out = {}
            for k in fieldnames:
                v = r.get(k, "")
                if v is None:
                    out[k] = ""
                elif isinstance(v, float):
                    out[k] = f"{v:.2f}"
                else:
                    out[k] = v
            w.writerow(out)


def utr(rng: random.Random) -> str:
    bank = rng.choice(BANKS)
    return f"{bank}{rng.randint(10**11, 10**12 - 1)}"


def amount_easy(rng: random.Random) -> float:
    buckets = [
        rng.choice([499, 799, 999, 1299, 1499, 1999, 2499]),
        rng.uniform(2500, 12000),
        rng.uniform(12000, 38000),
    ]
    return inr(rng.choice(buckets))


def amount_medium(rng: random.Random) -> float:
    return inr(rng.choice([
        rng.choice([349, 599, 899, 1599, 2199, 3999]),
        rng.uniform(1800, 18000),
        rng.uniform(18000, 64000),
        rng.uniform(64000, 125000),
    ]))


def amount_hard(rng: random.Random) -> float:
    return inr(rng.choice([
        rng.choice([101, 250, 501, 1001, 2501]),
        rng.uniform(900, 9000),
        rng.uniform(9000, 45000),
        rng.uniform(45000, 180000),
    ]))


def daterange(rng: random.Random, start: date, end: date) -> date:
    span = (end - start).days
    return start + timedelta(days=rng.randint(0, span))


def skip_weekend(d: date, extra: int = 0) -> date:
    out = d + timedelta(days=extra)
    while out.weekday() >= 5:
        out += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# EASY
# ---------------------------------------------------------------------------

def build_easy(seed: int = 194827) -> dict:
    rng = random.Random(seed)
    start, end = date(2026, 4, 6), date(2026, 5, 22)
    ledger, gw, bank, gt = [], [], [], []
    used_ids = set()

    def new_id() -> str:
        while True:
            n = rng.randint(2401000, 2498999)
            tid = f"HSNE{n}"
            if tid not in used_ids:
                used_ids.add(tid)
                return tid

    cases = (
        ["exact"] * 70
        + ["fuzzy_ref"] * 4
        + ["fuzzy_delay"] * 3
        + ["fuzzy_round"] * 3
        + ["missing_settlement"] * 5
        + ["missing_bank"] * 5
        + ["refund_partial"] * 4
        + ["duplicate_bank"] * 3
        + ["amount_mismatch"] * 3
    )
    assert len(cases) == 100
    rng.shuffle(cases)

    for i, kind in enumerate(cases):
        tid = new_id()
        cust = rng.choice(CUSTOMERS)
        amt = amount_easy(rng)
        order_dt = daterange(rng, start, end)
        fee_rate = 0.02
        fee = inr(amt * fee_rate)
        tds = 0.0
        net = inr(amt - fee - tds)
        settle_dt = order_dt + timedelta(days=1)
        bank_dt = settle_dt
        ledger_oid = tid
        gw_oid = tid
        bank_ref = tid
        decision, match_type, reason, review, band = "MATCH", "EXACT", "", "false", "high"
        gw_gross, gw_fee, gw_tds, gw_net, gw_refund = amt, fee, tds, net, 0.0
        bank_amt = net
        emit_gw, emit_bank = True, True
        extra_bank = False
        narration = f"NEFT {cust.split()[0].upper()} {tid}"

        if kind == "fuzzy_ref":
            ledger_oid = f"{tid[:4]}-{tid[4:]}"
            gw_oid = tid
            bank_ref = tid
            match_type, band = "FUZZY", "medium"
        elif kind == "fuzzy_delay":
            bank_dt = settle_dt + timedelta(days=rng.randint(1, 2))
            match_type, reason, band = "FUZZY", "timing_difference", "medium"
        elif kind == "fuzzy_round":
            delta = rng.choice([-0.51, -0.37, 0.42, 0.64])
            bank_amt = inr(net + delta)
            match_type, band = "TOLERANCE", "medium"
        elif kind == "missing_settlement":
            emit_gw = False
            emit_bank = False
            gw_gross = gw_fee = gw_tds = gw_net = None
            bank_amt = None
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_settlement", "true", "high"
            )
        elif kind == "missing_bank":
            emit_bank = False
            bank_amt = None
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_bank_credit", "true", "high"
            )
        elif kind == "refund_partial":
            refund = inr(net * rng.uniform(0.18, 0.45))
            gw_refund = refund
            bank_amt = inr(net - refund)
            gw_net = inr(amt - fee)  # settlement before refund posting
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_partial", "true", "high"
            )
        elif kind == "duplicate_bank":
            extra_bank = True
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "duplicate_bank_credit", "true", "high"
            )
        elif kind == "amount_mismatch":
            bank_amt = inr(net - rng.choice([85, 140, 275, 410]))
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", reason_for_amount_gap(net - bank_amt), "true", "high"
            )

        ledger.append({
            "order_id": ledger_oid,
            "order_date": order_dt.isoformat(),
            "amount": amt,
            "currency": "INR",
            "customer_name": cust,
            "channel": rng.choice(["UPI", "CARD", "NETBANKING"]),
        })

        if emit_gw:
            gw.append({
                "settlement_id": f"STLE{800000 + i}",
                "merchant_order_id": gw_oid,
                "settlement_date": settle_dt.isoformat(),
                "gross_amount": gw_gross,
                "fee": gw_fee,
                "tds": gw_tds,
                "refund_amount": gw_refund,
                "net_amount": gw_net,
                "payment_mode": rng.choice(["UPI", "VISA", "RUPAY", "MASTERCARD"]),
            })

        if emit_bank:
            bank.append({
                "value_date": bank_dt.isoformat(),
                "credit_amount": bank_amt,
                "debit_amount": 0.0,
                "utr": utr(rng),
                "reference": bank_ref,
                "narration": narration,
            })
            if extra_bank:
                bank.append({
                    "value_date": (bank_dt + timedelta(days=1)).isoformat(),
                    "credit_amount": bank_amt,
                    "debit_amount": 0.0,
                    "utr": utr(rng),
                    "reference": bank_ref,
                    "narration": narration,
                })

        exp_gw = gw_gross if emit_gw else ""
        exp_set = gw_net if emit_gw else ""
        exp_bank = bank_amt if emit_bank else ""
        var = ""
        if emit_gw and emit_bank and gw_net is not None and bank_amt is not None:
            var = inr(gw_net - bank_amt)

        gt.append({
            "transaction_id": tid,
            "expected_decision": decision,
            "expected_reason": reason if decision == "EXCEPTION" else (
                "timing_difference" if kind == "fuzzy_delay" else ""
            ),
            "expected_ledger_amount": amt,
            "expected_gateway_amount": exp_gw,
            "expected_settlement_amount": exp_set,
            "expected_bank_amount": exp_bank,
            "expected_variance": var,
            "expected_match_type": match_type,
            "expected_confidence_band": band,
            "human_review_required": review,
        })

    return pack("easy_100", "easy", seed, ledger, gw, bank, gt, {
        "challenges": [
            "clean 2% MDR exact ties",
            "order-id hyphen vs concatenated formatting",
            "1–2 day bank posting lag",
            "sub-rupee rounding",
            "missing settlement",
            "missing bank credit",
            "obvious partial refunds",
            "duplicate bank credits",
            "clear amount mismatches",
        ],
    })


# ---------------------------------------------------------------------------
# MEDIUM
# ---------------------------------------------------------------------------

def build_medium(seed: int = 561039) -> dict:
    rng = random.Random(seed)
    start, end = date(2026, 1, 8), date(2026, 3, 28)
    ledger, gw, bank, gt = [], [], [], []
    used_ids = set()

    def new_id() -> str:
        while True:
            tid = f"RZPM{rng.randint(4100000, 4899999)}"
            if tid not in used_ids:
                used_ids.add(tid)
                return tid

    cases = (
        ["exact"] * 28
        + ["fee_var"] * 4
        + ["tds"] * 4
        + ["fee_tds_delay"] * 4
        + ["rounding"] * 4
        + ["id_format"] * 4
        + ["name_var"] * 3
        + ["many_to_one"] * 3
        + ["one_to_many"] * 3
        + ["delayed_settle"] * 3
        + ["missing_settlement"] * 6
        + ["missing_bank"] * 8
        + ["refund_partial"] * 5
        + ["refund_full"] * 3
        + ["duplicate_bank"] * 5
        + ["duplicate_gateway"] * 5
        + ["amount_mismatch"] * 8
    )
    assert len(cases) == 100

    # many_to_one needs groups of 3 — keep them consecutive after shuffle of others
    m2o = [c for c in cases if c == "many_to_one"]
    o2m = [c for c in cases if c == "one_to_many"]
    rest = [c for c in cases if c not in ("many_to_one", "one_to_many")]
    rng.shuffle(rest)

    seq = rest[:]
    # inject m2o as a block of 3
    pos = rng.randint(10, 40)
    seq[pos:pos] = m2o
    pos2 = rng.randint(50, 70)
    seq[pos2:pos2] = o2m
    assert len(seq) == 100

    i = 0
    decoy_gw = 0
    while i < 100:
        kind = seq[i]

        if kind == "many_to_one":
            group = []
            batch_id = f"Payout{rng.randint(90020, 90990)}"
            batch_date = daterange(rng, start, end)
            total_net = 0.0
            for j in range(3):
                tid = new_id()
                cust = rng.choice(CUSTOMERS)
                amt = amount_medium(rng)
                order_dt = batch_date - timedelta(days=rng.randint(1, 3))
                fee = inr(amt * rng.choice([0.018, 0.02, 0.022]))
                tds = inr(amt * 0.01) if rng.random() < 0.4 else 0.0
                net = inr(amt - fee - tds)
                total_net = inr(total_net + net)
                ledger.append({
                    "order_id": tid, "order_date": order_dt.isoformat(),
                    "amount": amt, "currency": "INR", "customer_name": cust,
                    "channel": "UPI",
                })
                gw.append({
                    "settlement_id": f"STLM{70000 + i + j}",
                    "merchant_order_id": tid,
                    "settlement_date": (order_dt + timedelta(days=1)).isoformat(),
                    "gross_amount": amt, "fee": fee, "tds": tds,
                    "refund_amount": 0.0, "net_amount": net,
                    "payment_mode": "UPI", "payout_batch": batch_id,
                })
                group.append((tid, amt, net, fee, tds, cust))
            bank.append({
                "value_date": skip_weekend(batch_date, 2).isoformat(),
                "credit_amount": total_net, "debit_amount": 0.0,
                "utr": utr(rng), "reference": batch_id,
                "narration": f"RAZORPAY SETTLEMENT {batch_id}",
            })
            for tid, amt, net, fee, tds, cust in group:
                gt.append({
                    "transaction_id": tid,
                    "expected_decision": "MATCH",
                    "expected_reason": "",
                    "expected_ledger_amount": amt,
                    "expected_gateway_amount": amt,
                    "expected_settlement_amount": net,
                    "expected_bank_amount": total_net,
                    "expected_variance": 0.00,
                    "expected_match_type": "MANY_TO_ONE",
                    "expected_confidence_band": "medium",
                    "human_review_required": "false",
                })
            i += 3
            continue

        if kind == "one_to_many":
            tid = new_id()
            cust = rng.choice(CUSTOMERS)
            amt = amount_medium(rng)
            order_dt = daterange(rng, start, end)
            fee = inr(amt * 0.02)
            tds = 0.0
            net = inr(amt - fee)
            p1 = inr(net * rng.uniform(0.35, 0.55))
            p2 = inr(net - p1)
            ledger.append({
                "order_id": tid, "order_date": order_dt.isoformat(),
                "amount": amt, "currency": "INR", "customer_name": cust,
                "channel": "CARD",
            })
            d1 = order_dt + timedelta(days=1)
            d2 = order_dt + timedelta(days=2)
            gw.append({
                "settlement_id": f"STLM{71000 + i}A",
                "merchant_order_id": tid, "settlement_date": d1.isoformat(),
                "gross_amount": inr(amt * (p1 / net)), "fee": inr(fee * (p1 / net)),
                "tds": 0.0, "refund_amount": 0.0, "net_amount": p1, "payment_mode": "CARD",
            })
            gw.append({
                "settlement_id": f"STLM{71000 + i}B",
                "merchant_order_id": tid, "settlement_date": d2.isoformat(),
                "gross_amount": inr(amt - inr(amt * (p1 / net))),
                "fee": inr(fee - inr(fee * (p1 / net))),
                "tds": 0.0, "refund_amount": 0.0, "net_amount": p2, "payment_mode": "CARD",
            })
            bank.append({
                "value_date": d1.isoformat(), "credit_amount": p1, "debit_amount": 0.0,
                "utr": utr(rng), "reference": tid, "narration": f"SPLIT CAPTURE 1 {tid}",
            })
            bank.append({
                "value_date": d2.isoformat(), "credit_amount": p2, "debit_amount": 0.0,
                "utr": utr(rng), "reference": tid, "narration": f"SPLIT CAPTURE 2 {tid}",
            })
            gt.append({
                "transaction_id": tid, "expected_decision": "MATCH",
                "expected_reason": "", "expected_ledger_amount": amt,
                "expected_gateway_amount": amt, "expected_settlement_amount": net,
                "expected_bank_amount": inr(p1 + p2), "expected_variance": 0.00,
                "expected_match_type": "ONE_TO_MANY",
                "expected_confidence_band": "medium", "human_review_required": "false",
            })
            i += 1
            continue

        tid = new_id()
        cust = rng.choice(CUSTOMERS)
        amt = amount_medium(rng)
        order_dt = daterange(rng, start, end)
        fee_rate = 0.02
        tds_rate = 0.0
        settle_lag = 1
        bank_lag = 0
        ledger_oid, gw_oid, bank_ref = tid, tid, tid
        decision, match_type, reason, review, band = "MATCH", "EXACT", "", "false", "high"
        emit_gw = emit_bank = True
        extra_bank = extra_gw = False
        refund = 0.0
        bank_override = None
        gw_net_override = None

        if kind == "fee_var":
            fee_rate = rng.choice([0.012, 0.015, 0.018, 0.025, 0.03])
            match_type, band = "TOLERANCE", "medium"
        elif kind == "tds":
            tds_rate = 0.01
            match_type, band = "TOLERANCE", "medium"
        elif kind == "fee_tds_delay":
            # Fee/TDS plus a real settlement→bank lag. Lag wins the match-type.
            fee_rate = rng.choice([0.018, 0.02, 0.022])
            tds_rate = 0.01
            settle_lag = rng.randint(1, 3)
            bank_lag = rng.randint(1, 2)
            match_type, reason, band = "FUZZY", "timing_difference", "medium"
        elif kind == "rounding":
            bank_override = "round"
            match_type, band = "TOLERANCE", "medium"
        elif kind == "id_format":
            ledger_oid = f"{tid[:4]}-{tid[4:]}"
            gw_oid = tid.lower()
            bank_ref = tid
            match_type, band = "FUZZY", "medium"
        elif kind == "name_var":
            # Name spelling is not a join key — IDs still match exactly.
            cust = rng.choice(list(NEAR_NAMES.keys()))
            match_type, band = "EXACT", "high"
        elif kind == "missing_settlement":
            emit_gw = emit_bank = False
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_settlement", "true", "high"
            )
        elif kind == "missing_bank":
            emit_bank = False
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_bank_credit", "true", "high"
            )
        elif kind == "refund_partial":
            refund = inr(amt * rng.uniform(0.15, 0.4))
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_partial", "true", "high"
            )
        elif kind == "refund_full":
            refund = None  # set after net
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_full", "true", "high"
            )
        elif kind == "duplicate_bank":
            extra_bank = True
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "duplicate_bank_credit", "true", "high"
            )
        elif kind == "duplicate_gateway":
            extra_gw = True
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "duplicate_gateway", "true", "high"
            )
        elif kind == "amount_mismatch":
            bank_override = "gap"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "amount_mismatch", "true", "high"
            )
        elif kind == "delayed_settle":
            settle_lag = rng.randint(2, 3)
            bank_lag = rng.randint(1, 2)
            match_type, reason, band = "FUZZY", "timing_difference", "medium"

        fee = inr(amt * fee_rate)
        tds = inr(amt * tds_rate) if tds_rate else 0.0
        net = inr(amt - fee - tds)
        if kind == "refund_full":
            refund = net
        gw_net = net
        if kind in ("refund_partial", "refund_full"):
            # gateway settlement file often still shows original capture net
            gw_net = net
        bank_amt = net
        if kind == "refund_partial":
            bank_amt = inr(net - refund)
        elif kind == "refund_full":
            bank_amt = 0.0
            emit_bank = True
        if bank_override == "round":
            bank_amt = inr(net + rng.choice([-0.72, -0.41, 0.33, 0.58]))
        elif bank_override == "gap":
            bank_amt = inr(net - rng.choice([120, 260, 540, 980, 1500]))
            reason = reason_for_amount_gap(net - bank_amt)

        settle_dt = order_dt + timedelta(days=settle_lag)
        bank_dt = settle_dt + timedelta(days=bank_lag)

        display_cust = cust
        if kind == "name_var":
            # ledger vs gateway name spelling drift
            pass

        ledger.append({
            "order_id": ledger_oid, "order_date": order_dt.isoformat(),
            "amount": amt, "currency": "INR",
            "customer_name": display_cust,
            "channel": rng.choice(["UPI", "CARD", "NETBANKING", "WALLET"]),
        })
        gw_cust = NEAR_NAMES.get(cust, cust) if kind == "name_var" else cust
        if emit_gw:
            row = {
                "settlement_id": f"STLM{72000 + i}",
                "merchant_order_id": gw_oid,
                "settlement_date": settle_dt.isoformat(),
                "gross_amount": amt, "fee": fee, "tds": tds,
                "refund_amount": refund or 0.0, "net_amount": gw_net,
                "payment_mode": rng.choice(["UPI", "VISA", "RUPAY"]),
            }
            gw.append(row)
            if extra_gw:
                dup = dict(row)
                dup["settlement_id"] = f"STLM{72000 + i}D"
                gw.append(dup)
        if emit_bank:
            bank.append({
                "value_date": bank_dt.isoformat(),
                "credit_amount": bank_amt, "debit_amount": 0.0 if bank_amt else 0.0,
                "utr": utr(rng), "reference": bank_ref,
                "narration": f"IMPS {gw_cust.split()[0].upper()} {bank_ref}",
            })
            if extra_bank:
                bank.append({
                    "value_date": (bank_dt + timedelta(hours=0)).isoformat(),
                    "credit_amount": bank_amt, "debit_amount": 0.0,
                    "utr": utr(rng), "reference": bank_ref,
                    "narration": f"IMPS {gw_cust.split()[0].upper()} {bank_ref}",
                })
            if kind == "refund_full" and bank_amt == 0.0:
                # full refund: original credit then reversing debit (same UTR family)
                bank[-1]["credit_amount"] = net
                bank.append({
                    "value_date": (bank_dt + timedelta(days=1)).isoformat(),
                    "credit_amount": 0.0, "debit_amount": net,
                    "utr": utr(rng), "reference": bank_ref,
                    "narration": f"REFUND REVERSAL {bank_ref}",
                })

        # decoy rows unrelated to this order
        if rng.random() < 0.12:
            decoy_id = f"RZPX{rng.randint(9100000, 9199999)}"
            da = amount_medium(rng)
            gw.append({
                "settlement_id": f"STLD{80000 + decoy_gw}",
                "merchant_order_id": decoy_id,
                "settlement_date": settle_dt.isoformat(),
                "gross_amount": da, "fee": inr(da * 0.02), "tds": 0.0,
                "refund_amount": 0.0, "net_amount": inr(da * 0.98),
                "payment_mode": "UPI",
            })
            decoy_gw += 1
        if rng.random() < 0.1:
            bank.append({
                "value_date": settle_dt.isoformat(),
                "credit_amount": inr(rng.uniform(800, 6000)),
                "debit_amount": 0.0, "utr": utr(rng),
                "reference": f"NACH{rng.randint(10000, 99999)}",
                "narration": "NACH MANDATE SALARY CREDIT",
            })

        exp_gw = amt if emit_gw else ""
        exp_set = gw_net if emit_gw else ""
        if kind == "refund_full":
            exp_bank = 0.0
        elif emit_bank:
            exp_bank = bank_amt
        else:
            exp_bank = ""
        var = ""
        if emit_gw and exp_bank != "" and gw_net is not None:
            if kind == "refund_full":
                var = inr(gw_net - 0.0)
            else:
                var = inr(gw_net - float(exp_bank))

        gt.append({
            "transaction_id": tid,
            "expected_decision": decision,
            "expected_reason": reason,
            "expected_ledger_amount": amt,
            "expected_gateway_amount": exp_gw,
            "expected_settlement_amount": exp_set,
            "expected_bank_amount": exp_bank,
            "expected_variance": var,
            "expected_match_type": match_type,
            "expected_confidence_band": band,
            "human_review_required": review,
        })
        i += 1

    return pack("medium_100", "medium", seed, ledger, gw, bank, gt, {
        "challenges": [
            "variable MDR and TDS stacked with posting delay",
            "hyphen/case order-id drift and near-duplicate customer names",
            "N:1 payout batches and 1:N split captures",
            "partial and full refunds",
            "duplicate gateway and duplicate bank rows",
            "missing legs",
            "unrelated decoy gateway and bank lines",
            "T+1–T+3 settlement lag",
        ],
    })


# ---------------------------------------------------------------------------
# HARD
# ---------------------------------------------------------------------------

def build_hard(seed: int = 847201) -> dict:
    rng = random.Random(seed)
    start, end = date(2025, 11, 28), date(2026, 2, 8)  # crosses month + year
    ledger, gw, bank, gt = [], [], [], []
    used_ids = set()

    def new_id(prefix="pay_") -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        while True:
            tid = prefix + "".join(rng.choice(alphabet) for _ in range(10))
            if tid not in used_ids:
                used_ids.add(tid)
                return tid

    planner = []
    planner += [("exact", {})] * 14
    planner += [("tolerance", {"mode": m}) for m in
                ["fee", "fee", "fee", "tds", "tds", "round", "round"]]
    planner += [("fuzzy", {"mode": m}) for m in
                ["delay", "delay", "delay", "weekend", "month"]]
    planner += [("m2o_member", {"group": 0})] * 3
    planner += [("m2o_member", {"group": 1})] * 2
    planner += [("o2m", {})] * 3
    planner += [
        ("twin", {"pair": 0, "role": "a"}),
        ("twin", {"pair": 0, "role": "b"}),
        ("near_name", {"pair": 0, "role": "a"}),
        ("near_name", {"pair": 0, "role": "b"}),
        ("offbyone", {"role": "legit"}),
        ("offbyone", {"role": "trap"}),
        ("dup_gw", {}), ("dup_gw", {}),
        ("dup_bank", {}), ("dup_bank", {}), ("dup_bank", {}),
        ("partial_settle", {}), ("partial_settle", {}), ("partial_settle", {}),
        ("long_settle", {}),
        ("long_bank", {}),
        ("refund_after", {}),
        ("refund_partial", {}), ("refund_partial", {}),
        ("refund_as_fee", {}),
        ("small_gap", {"gap": 1.0}),
        ("small_gap", {"gap": 2.0}),
        ("small_gap", {"gap": 5.0}),
        ("large_gap", {}), ("large_gap", {}), ("large_gap", {}),
        ("miss_gw", {}), ("miss_gw", {}), ("miss_gw", {}), ("miss_gw", {}),
        ("miss_bank", {}), ("miss_bank", {}), ("miss_bank", {}),
        ("bank_no_id", {"unique": True}),
        ("bank_no_id", {"unique": False}),
        ("wrong_oid", {}), ("wrong_oid", {}), ("wrong_oid", {}),
        ("same_cust_amt", {"pair": 0, "role": "a"}),
        ("same_cust_amt", {"pair": 0, "role": "b"}),
        ("valid_decoy_pair", {"role": "valid"}),
        ("valid_decoy_pair", {"role": "decoy"}),
        ("dup_looking", {}),
        ("high_sim_nomatch", {"role": "real"}),
        ("high_sim_nomatch", {"role": "lookalike"}),
        ("ambiguous", {}),
        ("holiday_miss", {}),
        ("fee_unexplained", {}),
        ("tds_refund_messy", {}),
        ("bank_swap", {"role": "a"}),
        ("bank_swap", {"role": "b"}),
        ("miss_bank", {}), ("miss_bank", {}),
        ("dup_gw", {}),
        ("large_gap", {}),
        ("ambiguous", {}),
        ("refund_partial", {}),
        ("partial_settle", {}),
        ("wrong_oid", {}),
        ("small_gap", {"gap": 5.0}),
        ("miss_gw", {}),
        ("holiday_miss", {}),
        ("fee_unexplained", {}),
        ("exact", {}),
        ("dup_bank", {}),
        ("miss_gw", {}),
    ]
    assert len(planner) == 100, len(planner)

    # Shared amounts for twin / same_cust pairs
    twin_amt = inr(18499.00)
    same_amt = inr(7600.00)
    same_cust = "Karthik Raman"
    near_a, near_b = "Rajesh Kumar", "Rajesh Kumarr"
    swap_net = inr(9320.50)

    m2o_acc = {0: [], 1: []}
    pending_m2o_bank = {}

    offby_legit_id = None
    high_sim_real = None
    bank_swap_rows = []

    ids = [new_id() for _ in range(100)]

    shared_ambiguous_amt = None

    for i, (kind, meta) in enumerate(planner):
        tid = ids[i]
        cust = rng.choice(CUSTOMERS)
        amt = amount_hard(rng)
        order_dt = daterange(rng, start, end)
        fee_rate = 0.02
        tds = 0.0
        settle_lag = 1
        bank_lag = 0
        ledger_oid = gw_oid = bank_ref = tid
        decision, match_type, reason, review, band = "MATCH", "EXACT", "", "false", "high"
        emit_gw = emit_bank = True
        extra_gw = extra_bank = False
        refund = 0.0
        bank_amt_override = None
        gw_net_override = None
        narration_extra = ""
        skip_bank_id = False
        decoy_bank = None
        decoy_gw = None

        if kind == "tolerance":
            mode = meta["mode"]
            if mode == "fee":
                fee_rate = rng.choice([0.014, 0.018, 0.027])
            elif mode == "tds":
                tds = None  # after fee
            elif mode == "round":
                bank_amt_override = "round"
            match_type, band = "TOLERANCE", "medium"
        elif kind == "fuzzy":
            mode = meta["mode"]
            if mode == "delay":
                bank_lag = rng.randint(3, 5)
            elif mode == "weekend":
                # force Saturday order
                while order_dt.weekday() != 5:
                    order_dt += timedelta(days=1)
                settle_lag = 1
                bank_lag = 2  # posts Monday/Tuesday
            elif mode == "month":
                order_dt = date(2026, 1, 31)
                settle_lag = 1
                bank_lag = 2
            match_type, reason, band = "FUZZY", "timing_difference", "medium"
        elif kind == "m2o_member":
            match_type, band = "MANY_TO_ONE", "medium"
            review = "false"
        elif kind == "o2m":
            match_type, band = "ONE_TO_MANY", "medium"
        elif kind == "twin":
            amt = twin_amt
            cust = rng.choice(["Vivek Anand", "Mohan Lal"]) if meta["role"] == "a" else rng.choice(["Farhan Qureshi", "Leela Krishnan"])
            # both fully reconcilable via distinct IDs → MATCH
            match_type, band = "EXACT", "high"
        elif kind == "near_name":
            cust = near_a if meta["role"] == "a" else near_b
            amt = inr(5299.00)
            # distinct IDs, same amount, similar names → still MATCH each if IDs present
            match_type, band = "EXACT", "medium"
            review = "true"  # human should glance
        elif kind == "offbyone":
            if meta["role"] == "legit":
                offby_legit_id = tid
                match_type = "EXACT"
            else:
                # lookalike id, different customer/amount should NOT steal the legit bank
                decision, match_type, reason, review, band = (
                    "EXCEPTION", "EXCEPTION", "wrong_transaction", "true", "low"
                )
                emit_gw = True
                emit_bank = False  # no own bank; a decoy similar id exists on legit's bank? 
                # Actually: trap has gw with id off-by-one from legit, no bank of its own
                reason = "ambiguous_match"
        elif kind == "dup_gw":
            extra_gw = True
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "duplicate_gateway", "true", "high"
            )
        elif kind == "dup_bank":
            extra_bank = True
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "duplicate_bank_credit", "true", "high"
            )
        elif kind == "partial_settle":
            gw_net_override = "partial"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "amount_mismatch", "true", "high"
            )
        elif kind == "long_settle":
            settle_lag = rng.randint(8, 14)
            bank_lag = rng.randint(1, 3)
            match_type, reason, review, band = "FUZZY", "timing_difference", "true", "medium"
        elif kind == "long_bank":
            bank_lag = rng.randint(9, 16)
            match_type, reason, review, band = "FUZZY", "timing_difference", "true", "medium"
        elif kind == "refund_after":
            refund = "fullish"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_full", "true", "high"
            )
        elif kind == "refund_partial":
            refund = "partial"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_partial", "true", "high"
            )
        elif kind == "refund_as_fee":
            # bank looks like 2% fee takeout but is actually a refund of a similar size
            refund = "looks_fee"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_partial", "true", "high"
            )
        elif kind == "small_gap":
            gap = meta["gap"]
            bank_amt_override = gap
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION",
                reason_for_amount_gap(gap),
                "true", "medium" if gap <= 2 else "high",
            )
        elif kind == "large_gap":
            bank_amt_override = "large"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "amount_mismatch", "true", "high"
            )
        elif kind == "miss_gw":
            emit_gw = False
            emit_bank = False
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_settlement", "true", "high"
            )
        elif kind == "miss_bank":
            emit_bank = False
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_bank_credit", "true", "high"
            )
        elif kind == "bank_no_id":
            skip_bank_id = True
            if meta["unique"]:
                # unique amount → still not safe without id in hard set
                decision, match_type, reason, review, band = (
                    "EXCEPTION", "EXCEPTION", "ambiguous_match", "true", "low"
                )
            else:
                amt = twin_amt  # collides with twin amounts
                decision, match_type, reason, review, band = (
                    "EXCEPTION", "EXCEPTION", "ambiguous_match", "true", "low"
                )
        elif kind == "wrong_oid":
            gw_oid = new_id("ord_")
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "wrong_transaction", "true", "high"
            )
        elif kind == "same_cust_amt":
            cust = same_cust
            amt = same_amt
            order_dt = date(2026, 1, 14) if meta["role"] == "a" else date(2026, 1, 16)
            # two real payments — MATCH each via distinct ids
            match_type, band, review = "EXACT", "medium", "true"
        elif kind == "valid_decoy_pair":
            if meta["role"] == "valid":
                match_type = "EXACT"
                # decoy bank with similar amount nearby
                decoy_bank = True
            else:
                # decoy ledger row that looks similar but never settled
                emit_gw = False
                emit_bank = False
                amt = inr(amt)  # independent
                decision, match_type, reason, review, band = (
                    "EXCEPTION", "EXCEPTION", "missing_settlement", "true", "high"
                )
        elif kind == "dup_looking":
            extra_gw = True  # second gw slightly different settlement id, same amounts
            extra_bank = False
            # only one bank credit — duplicate-looking gw, one legitimate
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "duplicate_gateway", "true", "high"
            )
        elif kind == "high_sim_nomatch":
            if meta["role"] == "real":
                high_sim_real = tid
                match_type = "EXACT"
            else:
                # id is real's id with last char flipped; MUST NOT match real's bank
                if high_sim_real:
                    ledger_oid = high_sim_real[:-1] + ("X" if high_sim_real[-1] != "X" else "Y")
                    gw_oid = ledger_oid
                    tid = ledger_oid
                    used_ids.add(tid)
                    ids[i] = tid
                emit_bank = False
                decision, match_type, reason, review, band = (
                    "EXCEPTION", "EXCEPTION", "wrong_transaction", "true", "low"
                )
        elif kind == "ambiguous":
            # two gw candidates, one bank, ids stripped-ish
            skip_bank_id = True
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "ambiguous_match", "true", "low"
            )
            extra_gw = True
        elif kind == "holiday_miss":
            order_dt = date(2026, 1, 26)  # Republic Day
            emit_bank = False
            settle_lag = 1
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "missing_bank_credit", "true", "medium"
            )
        elif kind == "fee_unexplained":
            fee_rate = 0.02
            bank_amt_override = "fee_wrong"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "amount_mismatch", "true", "high"
            )
        elif kind == "tds_refund_messy":
            tds = "yes"
            refund = "partial"
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "refund_partial", "true", "high"
            )
        elif kind == "bank_swap":
            # two orders; banks assigned crossed — both EXCEPTION wrong_transaction
            decision, match_type, reason, review, band = (
                "EXCEPTION", "EXCEPTION", "wrong_transaction", "true", "high"
            )

        fee = inr(amt * fee_rate)
        if tds == "yes" or (kind == "tolerance" and meta.get("mode") == "tds"):
            tds_amt = inr(amt * 0.01)
        else:
            tds_amt = 0.0
        net = inr(amt - fee - tds_amt)

        if gw_net_override == "partial":
            gw_gross = inr(amt * rng.uniform(0.4, 0.7))
            fee = inr(gw_gross * fee_rate)
            net = inr(gw_gross - fee)
            gw_amt = gw_gross
        else:
            gw_amt = amt

        if refund == "partial" or (kind == "tds_refund_messy"):
            refund = inr(net * rng.uniform(0.2, 0.45))
        elif refund == "fullish":
            refund = net
        elif refund == "looks_fee":
            refund = fee  # bank = gross - 2*fee ≈ looks like 4% or like fee twice
        else:
            refund = refund if isinstance(refund, float) else 0.0

        bank_amt = net
        if kind in ("refund_partial", "tds_refund_messy") or (
            isinstance(refund, float) and refund and kind not in ("dup_gw", "dup_bank", "exact")
        ):
            if kind == "refund_after":
                bank_amt = 0.0
            elif kind == "refund_as_fee":
                # net already deducted 2% fee; extra refund ≈ fee so bank = amt - 2*fee
                bank_amt = inr(amt - fee - fee)
            elif refund and kind not in ("exact", "tolerance", "fuzzy", "m2o_member", "o2m", "twin"):
                bank_amt = inr(net - refund) if kind != "refund_after" else 0.0

        if bank_amt_override == 1.0 or (isinstance(bank_amt_override, float)):
            bank_amt = inr(net - float(bank_amt_override))
        elif bank_amt_override == "round":
            bank_amt = inr(net + rng.choice([-0.72, -0.41, 0.33, 0.58]))
        elif bank_amt_override == "large":
            bank_amt = inr(net - rng.choice([2200, 4500, 8800, 15000]))
            reason = reason_for_amount_gap(net - bank_amt)
        elif bank_amt_override == "fee_wrong":
            bank_amt = inr(amt - inr(amt * 0.035))  # not a listed fee schedule
            reason = reason_for_amount_gap(net - bank_amt)

        settle_dt = order_dt + timedelta(days=settle_lag)
        bank_dt = skip_weekend(settle_dt, bank_lag) if kind == "fuzzy" and meta.get("mode") == "weekend" else settle_dt + timedelta(days=bank_lag)

        # ONE TO MANY
        if kind == "o2m":
            p1 = inr(net * 0.6)
            p2 = inr(net - p1)
            ledger.append({
                "order_id": tid, "order_date": order_dt.isoformat(),
                "amount": amt, "currency": "INR", "customer_name": cust, "channel": "CARD",
            })
            gw.append({
                "settlement_id": f"st_{tid}_1", "merchant_order_id": tid,
                "settlement_date": (order_dt + timedelta(days=1)).isoformat(),
                "gross_amount": inr(amt * 0.6), "fee": inr(fee * 0.6), "tds": 0.0,
                "refund_amount": 0.0, "net_amount": p1, "payment_mode": "CARD",
            })
            gw.append({
                "settlement_id": f"st_{tid}_2", "merchant_order_id": tid,
                "settlement_date": (order_dt + timedelta(days=3)).isoformat(),
                "gross_amount": inr(amt - inr(amt * 0.6)), "fee": inr(fee - inr(fee * 0.6)),
                "tds": 0.0, "refund_amount": 0.0, "net_amount": p2, "payment_mode": "CARD",
            })
            bank.append({
                "value_date": (order_dt + timedelta(days=1)).isoformat(),
                "credit_amount": p1, "debit_amount": 0.0, "utr": utr(rng),
                "reference": tid, "narration": f"NEFT PART {tid}",
            })
            bank.append({
                "value_date": (order_dt + timedelta(days=4)).isoformat(),
                "credit_amount": p2, "debit_amount": 0.0, "utr": utr(rng),
                "reference": tid, "narration": f"NEFT PART {tid}",
            })
            gt.append({
                "transaction_id": tid, "expected_decision": "MATCH", "expected_reason": "",
                "expected_ledger_amount": amt, "expected_gateway_amount": amt,
                "expected_settlement_amount": net, "expected_bank_amount": inr(p1 + p2),
                "expected_variance": 0.00, "expected_match_type": "ONE_TO_MANY",
                "expected_confidence_band": "medium", "human_review_required": "false",
            })
            continue

        if kind == "m2o_member":
            g = meta["group"]
            m2o_acc[g].append({"tid": tid, "amt": amt, "fee": fee, "tds": tds_amt, "net": net,
                               "cust": cust, "order_dt": order_dt})
            ledger.append({
                "order_id": tid, "order_date": order_dt.isoformat(),
                "amount": amt, "currency": "INR", "customer_name": cust, "channel": "UPI",
            })
            gw.append({
                "settlement_id": f"st_{tid}", "merchant_order_id": tid,
                "settlement_date": (order_dt + timedelta(days=1)).isoformat(),
                "gross_amount": amt, "fee": fee, "tds": tds_amt, "refund_amount": 0.0,
                "net_amount": net, "payment_mode": "UPI",
                "payout_batch": f"SWEEP{g+17}",
            })
            gt.append({
                "transaction_id": tid, "expected_decision": "MATCH", "expected_reason": "",
                "expected_ledger_amount": amt, "expected_gateway_amount": amt,
                "expected_settlement_amount": net, "expected_bank_amount": None,  # fill later
                "expected_variance": 0.00, "expected_match_type": "MANY_TO_ONE",
                "expected_confidence_band": "medium", "human_review_required": "false",
            })
            continue

        if kind == "offbyone" and meta["role"] == "trap":
            if offby_legit_id:
                trap_id = offby_legit_id[:-1] + ("0" if offby_legit_id[-1] != "0" else "1")
                ledger_oid = trap_id
                gw_oid = trap_id
                tid = trap_id
                ids[i] = tid
            cust = "Bhavya Nanda"
            amt = inr(amt + 17.5)

        ledger.append({
            "order_id": ledger_oid, "order_date": order_dt.isoformat(),
            "amount": amt, "currency": "INR", "customer_name": cust,
            "channel": rng.choice(["UPI", "CARD", "NETBANKING"]),
        })

        if emit_gw:
            gw.append({
                "settlement_id": f"st_{i:04d}_{tid[-4:]}",
                "merchant_order_id": gw_oid,
                "settlement_date": settle_dt.isoformat(),
                "gross_amount": gw_amt, "fee": fee, "tds": tds_amt,
                "refund_amount": refund if isinstance(refund, float) else 0.0,
                "net_amount": net, "payment_mode": rng.choice(["UPI", "VISA", "RUPAY"]),
            })
            if extra_gw:
                g2 = dict(gw[-1])
                g2["settlement_id"] = g2["settlement_id"] + "b"
                gw.append(g2)

        if emit_bank:
            bref = "" if skip_bank_id else bank_ref
            nar = f"UPI/{cust.split()[0]}/{bref or 'NA'}"
            bank.append({
                "value_date": bank_dt.isoformat(),
                "credit_amount": bank_amt if kind != "refund_after" else net,
                "debit_amount": 0.0 if kind != "refund_after" else 0.0,
                "utr": utr(rng),
                "reference": bref,
                "narration": nar,
            })
            if kind == "refund_after":
                bank.append({
                    "value_date": (bank_dt + timedelta(days=2)).isoformat(),
                    "credit_amount": 0.0, "debit_amount": net,
                    "utr": utr(rng), "reference": bref,
                    "narration": f"UPI REVERSAL {bref}",
                })
                bank_amt = 0.0
            if extra_bank:
                bank.append({
                    "value_date": (bank_dt + timedelta(days=1)).isoformat(),
                    "credit_amount": net, "debit_amount": 0.0,
                    "utr": utr(rng), "reference": bref,
                    "narration": nar,
                })
            if decoy_bank:
                bank.append({
                    "value_date": bank_dt.isoformat(),
                    "credit_amount": inr(bank_amt + rng.choice([-1, 1, 5])),
                    "debit_amount": 0.0, "utr": utr(rng),
                    "reference": "",
                    "narration": f"UPI/{cust.split()[0]}/NA",
                })

        if kind == "bank_swap":
            bank_swap_rows.append((tid, net, len(bank) - 1))

        exp_bank = ""
        if kind == "refund_after":
            exp_bank = 0.0
        elif emit_bank:
            exp_bank = bank_amt
        var = ""
        if emit_gw and exp_bank != "":
            var = inr(net - float(exp_bank))

        gt.append({
            "transaction_id": tid,
            "expected_decision": decision,
            "expected_reason": reason,
            "expected_ledger_amount": amt,
            "expected_gateway_amount": gw_amt if emit_gw else "",
            "expected_settlement_amount": net if emit_gw else "",
            "expected_bank_amount": exp_bank,
            "expected_variance": var,
            "expected_match_type": match_type,
            "expected_confidence_band": band,
            "human_review_required": review,
        })

    # Fill many-to-one bank credits + GT bank amounts
    for g, members in m2o_acc.items():
        if not members:
            continue
        total = inr(sum(m["net"] for m in members))
        bid = f"SWEEP{g+17}"
        last_dt = max(m["order_dt"] for m in members) + timedelta(days=2)
        bank.append({
            "value_date": last_dt.isoformat(),
            "credit_amount": total, "debit_amount": 0.0, "utr": utr(rng),
            "reference": bid,
            "narration": f"MERCHANT PAYOUT {bid}",
        })
        for m in members:
            for row in gt:
                if row["transaction_id"] == m["tid"]:
                    row["expected_bank_amount"] = total

    # Cross the two bank_swap credits if we have two
    # (left as independent EXCEPTION wrong_transaction — banks still tagged with own ids
    # so to make swap real, retag references)
    swap_gts = [g for g in gt if g.get("expected_reason") == "wrong_transaction"
                and g["transaction_id"] in {x[0] for x in bank_swap_rows}]
    if len(bank_swap_rows) >= 2:
        a, b = bank_swap_rows[0], bank_swap_rows[1]
        # find bank rows by reference == tid and swap references
        for brow in bank:
            if brow.get("reference") == a[0] and brow.get("debit_amount") == 0.0:
                brow["reference"] = b[0]
                break
        for brow in bank:
            if brow.get("reference") == b[0] and brow.get("credit_amount") == b[1]:
                brow["reference"] = a[0]
                break

    # A few extra decoy bank/gw lines
    for _ in range(8):
        bank.append({
            "value_date": daterange(rng, start, end).isoformat(),
            "credit_amount": inr(rng.uniform(500, 9000)),
            "debit_amount": 0.0, "utr": utr(rng),
            "reference": f"NEFT{rng.randint(100000, 999999)}",
            "narration": rng.choice(["TDS REFUND ITD", "GST INPUT CREDIT", "FD INTEREST"]),
        })
    for _ in range(6):
        did = new_id("payX_")
        da = amount_hard(rng)
        gw.append({
            "settlement_id": f"st_decoy_{did[-6:]}",
            "merchant_order_id": did,
            "settlement_date": daterange(rng, start, end).isoformat(),
            "gross_amount": da, "fee": inr(da * 0.02), "tds": 0.0,
            "refund_amount": 0.0, "net_amount": inr(da * 0.98),
            "payment_mode": "UPI",
        })

    return pack("hard_100", "hard", seed, ledger, gw, bank, gt, {
        "challenges": [
            "identical amounts on unrelated orders",
            "near-duplicate customer names",
            "order ids off by one character",
            "duplicate gateway and bank rows",
            "split settlements and combined payouts",
            "partial settlement without supporting refund",
            "long independent settlement and bank delays",
            "refunds that mimic fee take-rates",
            "₹1 / ₹2 / ₹5 unexplained gaps",
            "material bank shortfalls labeled amount_mismatch (same rule as easy/medium)",
            "bank credits with blank references",
            "gateway rows pointing at the wrong order id",
            "same customer + amount on nearby dates",
            "valid rows parked next to decoys",
            "high string-similarity IDs that must not match",
            "month-boundary and holiday posting",
            "swapped bank references",
            "ambiguous many-candidate cases marked for human review",
        ],
    })


def pack(name, difficulty, seed, ledger, gw, bank, gt, extra) -> dict:
    for row in gt:
        for k in ("expected_ledger_amount", "expected_gateway_amount",
                  "expected_settlement_amount", "expected_bank_amount",
                  "expected_variance"):
            v = row.get(k)
            if v is None:
                row[k] = ""
            elif isinstance(v, float):
                row[k] = inr(v)
        if row["expected_decision"] == "MATCH" and not row.get("expected_reason"):
            row["expected_reason"] = ""
        if row["expected_decision"] == "EXCEPTION" and not row.get("expected_reason"):
            row["expected_reason"] = "unexplained_variance"

    return {
        "name": name,
        "difficulty": difficulty,
        "seed": seed,
        "ledger": ledger,
        "gateway": gw,
        "bank": bank,
        "gt": gt,
        "extra": extra,
    }


GT_FIELDS = [
    "transaction_id", "expected_decision", "expected_reason",
    "expected_ledger_amount", "expected_gateway_amount",
    "expected_settlement_amount", "expected_bank_amount",
    "expected_variance", "expected_match_type",
    "expected_confidence_band", "human_review_required",
]
LEDGER_FIELDS = ["order_id", "order_date", "amount", "currency", "customer_name", "channel"]
GW_FIELDS = ["settlement_id", "merchant_order_id", "settlement_date", "gross_amount",
             "fee", "tds", "refund_amount", "net_amount", "payment_mode"]
BANK_FIELDS = ["value_date", "credit_amount", "debit_amount", "utr", "reference", "narration"]


def metadata(bundle: dict) -> dict:
    gt = bundle["gt"]
    reasons = Counter(r["expected_reason"] for r in gt if r["expected_decision"] == "EXCEPTION")
    types = Counter(r["expected_match_type"] for r in gt)
    return {
        "dataset_name": bundle["name"],
        "difficulty": bundle["difficulty"],
        "seed": bundle["seed"],
        "number_of_base_transactions": len(gt),
        "number_of_ledger_rows": len(bundle["ledger"]),
        "number_of_gateway_rows": len(bundle["gateway"]),
        "number_of_bank_rows": len(bundle["bank"]),
        "expected_match_count": sum(1 for r in gt if r["expected_decision"] == "MATCH"),
        "expected_exception_count": sum(1 for r in gt if r["expected_decision"] == "EXCEPTION"),
        "expected_exact_count": types.get("EXACT", 0),
        "expected_fuzzy_count": types.get("FUZZY", 0) + types.get("TOLERANCE", 0),
        "expected_many_to_one_count": types.get("MANY_TO_ONE", 0),
        "expected_one_to_many_count": types.get("ONE_TO_MANY", 0),
        "match_type_distribution": dict(types),
        "exception_reason_distribution": dict(reasons),
        "currency": "INR",
        "agent_input_files": [
            "internal_ledger.csv",
            "gateway_settlement.csv",
            "bank_statement.csv",
        ],
        "hidden_files": ["ground_truth.csv", "dataset_metadata.json", "README.md"],
    }


def readme(bundle: dict, meta: dict) -> str:
    extra = bundle["extra"]
    cats = "\n".join(f"- {c}" for c in extra["challenges"])
    return f"""# {bundle['name']}

**Difficulty:** {bundle['difficulty']}
**Seed:** {bundle['seed']}
**Base transactions:** {meta['number_of_base_transactions']}

## Intended use

Held-out evaluation only. Feed the agent **only**:

- `internal_ledger.csv`
- `gateway_settlement.csv`
- `bank_statement.csv`

Do **not** provide `ground_truth.csv` or `dataset_metadata.json` at inference time.

## Record counts

| File | Rows |
|---|---|
| internal_ledger.csv | {meta['number_of_ledger_rows']} |
| gateway_settlement.csv | {meta['number_of_gateway_rows']} |
| bank_statement.csv | {meta['number_of_bank_rows']} |
| ground_truth.csv (hidden) | {meta['number_of_base_transactions']} |

## Expected decision mix (aggregate)

- MATCH: {meta['expected_match_count']}
- EXCEPTION: {meta['expected_exception_count']}
- EXACT: {meta['expected_exact_count']}
- FUZZY/TOLERANCE: {meta['expected_fuzzy_count']}
- MANY_TO_ONE: {meta['expected_many_to_one_count']}
- ONE_TO_MANY: {meta['expected_one_to_many_count']}

Individual transaction answers are not listed here.

## Anomaly / challenge categories

{cats}

## Column notes

Ledger `order_id` is the merchant order reference. Gateway `merchant_order_id` usually corresponds to it but may be formatted differently or, in hard cases, wrong. Bank `reference` may be an order id, a payout batch id, a UTR-only narration, or blank.

Amounts are INR. Gateway `net_amount` is intended as gross − fee − TDS (refunds are a separate column and may also appear as bank reversals).
"""


LEAK_TOKENS = (
    "THIS_IS_A_REFUND", "TEST_DUPLICATE", "FAKE_TRANSACTION", "EXCEPTION",
    "GROUND_TRUTH", "SHOULD_MATCH", "HUMAN_REVIEW", "DECOY_LABEL",
)


def validate(bundle: dict) -> list[str]:
    errs = []
    gt = bundle["gt"]
    ledger = bundle["ledger"]
    if len(gt) != 100:
        errs.append(f"{bundle['name']}: gt has {len(gt)} rows, expected 100")
    ids = [r["transaction_id"] for r in gt]
    if len(ids) != len(set(ids)):
        errs.append(f"{bundle['name']}: duplicate transaction_id in ground truth")
    if len(ledger) < 100:
        errs.append(f"{bundle['name']}: ledger has {len(ledger)} rows, expected >= 100")

    allowed_dec = {"MATCH", "EXCEPTION"}
    allowed_mt = {"EXACT", "FUZZY", "TOLERANCE", "MANY_TO_ONE", "ONE_TO_MANY", "EXCEPTION"}
    allowed_rs = {
        "", "missing_settlement", "missing_bank_credit", "amount_mismatch",
        "refund_partial", "refund_full", "duplicate_gateway", "duplicate_bank_credit",
        "tds_issue", "timing_difference", "unexplained_variance",
        "wrong_transaction", "ambiguous_match",
    }
    for r in gt:
        if r["expected_decision"] not in allowed_dec:
            errs.append(f"bad decision {r['expected_decision']}")
        if r["expected_match_type"] not in allowed_mt:
            errs.append(f"bad match type {r['expected_match_type']}")
        if r["expected_reason"] not in allowed_rs:
            errs.append(f"bad reason {r['expected_reason']} on {r['transaction_id']}")
        if r["expected_decision"] == "MATCH" and r["expected_match_type"] == "EXCEPTION":
            errs.append(f"{r['transaction_id']}: MATCH with EXCEPTION type")
        if r["expected_decision"] == "EXCEPTION" and r["expected_match_type"] != "EXCEPTION":
            errs.append(f"{r['transaction_id']}: EXCEPTION with type {r['expected_match_type']}")
        try:
            la = float(r["expected_ledger_amount"])
            ga = r["expected_gateway_amount"]
            sa = r["expected_settlement_amount"]
            ba = r["expected_bank_amount"]
            if ga != "" and sa != "":
                if float(sa) - float(ga) > 1.01:
                    errs.append(f"{r['transaction_id']}: settlement > gateway gross")
            if ga != "" and abs(float(ga) - la) > 0.01 and r["expected_reason"] not in (
                "amount_mismatch", "wrong_transaction", "ambiguous_match", "missing_settlement"
            ) and r["expected_match_type"] not in ("ONE_TO_MANY",):
                pass
            sa = r["expected_settlement_amount"]
            ba = r["expected_bank_amount"]
            reason = r["expected_reason"]
            if (
                reason in ("amount_mismatch", "unexplained_variance")
                and sa not in ("", None)
                and ba not in ("", None)
            ):
                var = abs(float(sa) - float(ba))
                if var <= 0.01:
                    if reason == "unexplained_variance":
                        errs.append(
                            f"{r['transaction_id']}: zero bank gap labeled unexplained_variance"
                        )
                elif reason != reason_for_amount_gap(var):
                    errs.append(
                        f"{r['transaction_id']}: gap {var:.2f} labeled {reason}, "
                        f"expected {reason_for_amount_gap(var)}"
                    )
        except (TypeError, ValueError):
            pass

    blob = json.dumps(bundle["ledger"] + bundle["gateway"] + bundle["bank"])
    for tok in LEAK_TOKENS:
        if tok in blob:
            errs.append(f"{bundle['name']}: leak token {tok} in agent inputs")

    types = Counter(r["expected_match_type"] for r in gt)
    if bundle["difficulty"] == "medium":
        n_match = sum(1 for r in gt if r["expected_decision"] == "MATCH")
        n_exc = 100 - n_match
        if not (50 <= n_match <= 65):
            errs.append(f"medium: match count {n_match} outside 50–65")
        if n_exc < 25:
            errs.append(f"medium: only {n_exc} exceptions")
    if bundle["difficulty"] == "hard":
        if sum(1 for r in gt if r["expected_decision"] == "EXCEPTION") < 40:
            errs.append("hard: not enough exceptions to be adversarial")
        if types.get("MANY_TO_ONE", 0) < 2:
            errs.append("hard: missing many-to-one")
        if types.get("ONE_TO_MANY", 0) < 2:
            errs.append("hard: missing one-to-many")

    return errs


def write_bundle(bundle: dict) -> Path:
    d = ROOT / bundle["name"]
    d.mkdir(parents=True, exist_ok=True)
    write_csv(d / "internal_ledger.csv", bundle["ledger"], LEDGER_FIELDS)
    write_csv(d / "gateway_settlement.csv", bundle["gateway"], GW_FIELDS)
    write_csv(d / "bank_statement.csv", bundle["bank"], BANK_FIELDS)
    write_csv(d / "ground_truth.csv", bundle["gt"], GT_FIELDS)
    meta = metadata(bundle)
    (d / "dataset_metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (d / "README.md").write_text(readme(bundle, meta), encoding="utf-8")
    return d


def main():
    easy = build_easy()
    medium = build_medium()
    hard = build_hard()

    ids = {}
    for b in (easy, medium, hard):
        ids[b["name"]] = {r["transaction_id"] for r in b["gt"]}
    overlap = (ids["easy_100"] & ids["medium_100"]) | (ids["easy_100"] & ids["hard_100"]) | (
        ids["medium_100"] & ids["hard_100"]
    )
    if overlap:
        raise SystemExit(f"ID overlap across datasets: {list(overlap)[:5]}")

    all_err = []
    for b in (easy, medium, hard):
        all_err.extend(validate(b))
    if all_err:
        raise SystemExit("VALIDATION FAILED:\n" + "\n".join(all_err))

    paths = [write_bundle(b) for b in (easy, medium, hard)]

    zip_path = ROOT / "all_evaluation_datasets.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            for f in p.iterdir():
                z.write(f, arcname=f"{p.name}/{f.name}")

    def summary(b):
        meta = metadata(b)
        return {
            "transactions": 100,
            "expected_MATCH": meta["expected_match_count"],
            "expected_EXCEPTION": meta["expected_exception_count"],
            "match_types": meta["match_type_distribution"],
            "exception_reasons": meta["exception_reason_distribution"],
            "ledger_rows": meta["number_of_ledger_rows"],
            "gateway_rows": meta["number_of_gateway_rows"],
            "bank_rows": meta["number_of_bank_rows"],
        }

    report = {
        "easy_100": summary(easy),
        "medium_100": summary(medium),
        "hard_100": summary(hard),
        "zip": str(zip_path),
    }
    (ROOT / "generation_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print("OK: three datasets + zip written under", ROOT)


if __name__ == "__main__":
    main()
