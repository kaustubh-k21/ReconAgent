#!/usr/bin/env python3
"""Generate frozen adversarial suites. Independent of easy/medium/hard."""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADV = ROOT / "adversarial"

LEDGER_FIELDS = ["order_id", "order_date", "amount", "currency", "customer_name", "channel"]
GW_FIELDS = [
    "settlement_id", "merchant_order_id", "settlement_date", "gross_amount",
    "fee", "tds", "refund_amount", "net_amount", "payment_mode", "payout_batch",
]
BANK_FIELDS = ["value_date", "credit_amount", "debit_amount", "utr", "reference", "narration"]
GT_FIELDS = [
    "transaction_id", "expected_decision", "expected_reason",
    "expected_ledger_amount", "expected_gateway_amount", "expected_settlement_amount",
    "expected_bank_amount", "expected_variance", "expected_match_type",
    "expected_confidence_band", "human_review_required", "adversarial_tag",
]


def fee_net(gross: float, rate: float = 0.02, tds: float = 0.0, refund: float = 0.0) -> tuple[float, float]:
    fee = round(gross * rate, 2)
    net = round(gross - fee - tds - refund, 2)
    return fee, net


def ledger(oid, date, amount, customer="Adversarial User", **extra):
    row = {
        "order_id": oid,
        "order_date": date,
        "amount": amount,
        "currency": extra.get("currency", "INR"),
        "customer_name": customer,
        "channel": extra.get("channel", "UPI"),
    }
    return row


def gateway(oid, date, gross, fee=None, tds=0.0, refund=0.0, net=None, batch="", sid=None):
    if fee is None or net is None:
        auto_fee, auto_net = fee_net(float(gross) if _is_number(gross) else 0.0, tds=tds, refund=refund)
        fee = auto_fee if fee is None else fee
        net = auto_net if net is None else net
    return {
        "settlement_id": sid or f"st_{oid}",
        "merchant_order_id": oid,
        "settlement_date": date,
        "gross_amount": _money(gross),
        "fee": _money(fee),
        "tds": _money(tds),
        "refund_amount": _money(refund),
        "net_amount": _money(net),
        "payment_mode": "UPI",
        "payout_batch": batch,
    }


def bank(ref, date, credit, debit=0.0, utr=None, narration=None):
    return {
        "value_date": date,
        "credit_amount": _money(credit),
        "debit_amount": _money(debit),
        "utr": utr or f"UTR{ref}",
        "reference": ref,
        "narration": narration or f"NEFT {ref}",
    }


def gt(tid, decision, reason="", ledger_amt="", gw="", settle="", bank_amt="",
       variance="0.00", match_type="", tag="", review=None):
    if review is None:
        review = "true" if decision == "EXCEPTION" else "false"
    if decision == "EXCEPTION" and not match_type:
        match_type = "EXCEPTION"
    if decision == "MATCH" and not match_type:
        match_type = "EXACT"
    return {
        "transaction_id": tid,
        "expected_decision": decision,
        "expected_reason": reason,
        "expected_ledger_amount": _money(ledger_amt) if ledger_amt != "" else "",
        "expected_gateway_amount": _money(gw) if gw != "" else "",
        "expected_settlement_amount": _money(settle) if settle != "" else "",
        "expected_bank_amount": _money(bank_amt) if bank_amt != "" else "",
        "expected_variance": _money(variance) if variance != "" else "",
        "expected_match_type": match_type,
        "expected_confidence_band": "high",
        "human_review_required": review,
        "adversarial_tag": tag,
    }


def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _money(x):
    if x == "" or x is None:
        return ""
    if isinstance(x, str) and not _is_number(x):
        return x
    return f"{float(x):.2f}"


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def _meta(name: str, kind: str, n: int, extra: dict | None = None) -> dict:
    payload = {
        "dataset_name": name,
        "difficulty": "adversarial",
        "kind": kind,
        "seed": 20260830,
        "number_of_base_transactions": n,
        "currency": "INR",
        "agent_input_files": [
            "internal_ledger.csv",
            "gateway_settlement.csv",
            "bank_statement.csv",
        ],
        "hidden_files": ["ground_truth.csv", "dataset_metadata.json", "README.md"],
        "independent_of_heldout": True,
    }
    if extra:
        payload.update(extra)
    return payload


def write_suite(name: str, kind: str, ledgers, gws, banks, truths, readme: str,
                extra_meta=None, recon_meta=None, expected_ingest=None):
    dest = ADV / name
    dest.mkdir(parents=True, exist_ok=True)
    _write(dest / "internal_ledger.csv", LEDGER_FIELDS, ledgers)
    _write(dest / "gateway_settlement.csv", GW_FIELDS, gws)
    _write(dest / "bank_statement.csv", BANK_FIELDS, banks)
    _write(dest / "ground_truth.csv", GT_FIELDS, truths)
    meta = _meta(name, kind, len(truths), extra_meta)
    (dest / "dataset_metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (dest / "README.md").write_text(readme.strip() + "\n", encoding="utf-8")
    if recon_meta is not None:
        (dest / "recon_meta.json").write_text(json.dumps(recon_meta, indent=2) + "\n", encoding="utf-8")
    if expected_ingest is not None:
        (dest / "expected_ingest.json").write_text(
            json.dumps(expected_ingest, indent=2) + "\n", encoding="utf-8"
        )
    return dest


def clean_pair(oid: str, date: str, gross: float, customer: str):
    fee, net = fee_net(gross)
    return (
        ledger(oid, date, gross, customer),
        gateway(oid, date, gross, fee=fee, net=net),
        bank(oid, date, net),
        gt(oid, "MATCH", ledger_amt=gross, gw=gross, settle=net, bank_amt=net,
           match_type="EXACT", tag="ballast"),
    )


def ballast(prefix: str, n: int, date: str = "2026-07-01"):
    L, G, B, T = [], [], [], []
    for i in range(1, n + 1):
        oid = f"{prefix}{i:03d}"
        gross = round(1000 + i * 250.25, 2)
        l, g, b, t = clean_pair(oid, date, gross, f"Ballast {i}")
        L.append(l)
        G.append(g)
        B.append(b)
        T.append(t)
    return L, G, B, T


def suite_near_id_collision():
    L, G, B, T = ballast("ADV", 6)
    # Same amount, similar IDs. Join is on order_id — ADV0128 must not steal ADV0182.
    L.append(ledger("ADV0128", "2026-07-02", 5000.00, "Near Miss Source"))
    # Unique same-amount lookalike → wrong_transaction, never a MATCH.
    T.append(gt("ADV0128", "EXCEPTION", "wrong_transaction", ledger_amt=5000,
                match_type="EXCEPTION", tag="near_id_no_settlement"))

    fee, net = fee_net(5000.00)
    L.append(ledger("ADV0182", "2026-07-02", 5000.00, "Near Miss Target"))
    G.append(gateway("ADV0182", "2026-07-02", 5000.00, fee=fee, net=net))
    B.append(bank("ADV0182", "2026-07-02", net))
    T.append(gt("ADV0182", "MATCH", ledger_amt=5000, gw=5000, settle=net, bank_amt=net,
                match_type="EXACT", tag="near_id_true_pair"))

    L.append(ledger("ADV0129", "2026-07-02", 2500.00, "Orphan Ledger"))
    T.append(gt("ADV0129", "EXCEPTION", "wrong_transaction", ledger_amt=2500,
                tag="near_id_orphan_ledger"))
    # Orphan settlement/bank for a lookalike ID with no ledger row.
    fee2, net2 = fee_net(2500.00)
    G.append(gateway("ADV0192", "2026-07-02", 2500.00, fee=fee2, net=net2))
    B.append(bank("ADV0192", "2026-07-02", net2))
    return write_suite(
        "near_id_collision", "matching", L, G, B, T,
        "Similar IDs with identical amounts must not auto-match. "
        "ADV0128 has no settlement (lookalike is a wrong_transaction candidate); "
        "ADV0182 is a true pair.",
    )


def suite_duplicate_bank():
    L, G, B, T = ballast("DUP", 4)
    fee, net = fee_net(3499.00)
    L.append(ledger("ADVDUP1", "2026-07-03", 3499.00, "Duplicate Credit"))
    G.append(gateway("ADVDUP1", "2026-07-03", 3499.00, fee=fee, net=net))
    B.append(bank("ADVDUP1", "2026-07-03", net, utr="UTRDUPA"))
    B.append(bank("ADVDUP1", "2026-07-03", net, utr="UTRDUPE"))
    T.append(gt("ADVDUP1", "EXCEPTION", "duplicate_bank_credit", ledger_amt=3499,
                gw=3499, settle=net, bank_amt=net, tag="duplicate_bank_credit"))

    fee2, net2 = fee_net(2200.00)
    L.append(ledger("ADVDUP2", "2026-07-03", 2200.00, "Duplicate Ledger A"))
    L.append(ledger("ADVDUP2", "2026-07-03", 2200.00, "Duplicate Ledger B"))
    G.append(gateway("ADVDUP2", "2026-07-03", 2200.00, fee=fee2, net=net2))
    B.append(bank("ADVDUP2", "2026-07-03", net2))
    T.append(gt("ADVDUP2", "EXCEPTION", "duplicate_entry", ledger_amt=2200,
                gw=2200, settle=net2, bank_amt=net2, tag="duplicate_ledger_entry"))
    return write_suite(
        "duplicate_identity", "matching", L, G, B, T,
        "Duplicate bank credits and duplicate ledger rows must stay exceptions.",
    )


def suite_amount_threshold():
    L, G, B, T = ballast("THR", 4)
    cases = [
        # Distinct IDs — one-edit lookalikes are covered in near_id_collision.
        # abs(delta) < 1.00 → auto TOLERANCE
        ("ADVROUND99", 4000.00, 0.99, "MATCH", "", "TOLERANCE", "sub_rupee_rounding"),
        # abs(delta) == 1.00 → unexplained, never auto-clear
        ("ADVONE00", 4000.00, 1.00, "EXCEPTION", "unexplained_variance", "EXCEPTION", "one_rupee_cap"),
        # 1.01 < residual <= 10 → unexplained_variance
        ("ADVJUST01", 4100.00, 1.01, "EXCEPTION", "unexplained_variance", "EXCEPTION", "just_over_rupee"),
        ("ADVTEN00", 4200.00, 10.00, "EXCEPTION", "unexplained_variance", "EXCEPTION", "ten_rupee_residual"),
        # residual > 10 → amount_mismatch
        ("ADVFIFTEEN", 4300.00, 15.00, "EXCEPTION", "amount_mismatch", "EXCEPTION", "over_ten_mismatch"),
    ]
    for oid, gross, shortfall, dec, reason, mtype, tag in cases:
        fee, net = fee_net(gross)
        credit = round(net - shortfall, 2)
        L.append(ledger(oid, "2026-07-04", gross, tag))
        G.append(gateway(oid, "2026-07-04", gross, fee=fee, net=net))
        B.append(bank(oid, "2026-07-04", credit))
        T.append(gt(oid, dec, reason, ledger_amt=gross, gw=gross, settle=net,
                    bank_amt=credit, variance=shortfall, match_type=mtype, tag=tag,
                    review=dec == "EXCEPTION"))
    return write_suite(
        "amount_threshold", "matching", L, G, B, T,
        "₹0.99 auto-clears; ₹1.00 at the rounding cap does not; >₹10 is amount_mismatch.",
    )


def suite_n1_distractor():
    L, G, B, T = ballast("N1B", 3)
    # Honest N:1 batch
    members = [("ADVN1A", 1000.00), ("ADVN1B", 2000.00), ("ADVN1C", 3000.00)]
    nets = []
    for oid, gross in members:
        fee, net = fee_net(gross)
        nets.append(net)
        L.append(ledger(oid, "2026-07-05", gross, "Batch Honest"))
        G.append(gateway(oid, "2026-07-05", gross, fee=fee, net=net, batch="BATCHOK"))
        T.append(gt(oid, "MATCH", ledger_amt=gross, gw=gross, settle=net,
                    bank_amt=sum(nets) if oid == "ADVN1C" else "",
                    match_type="MANY_TO_ONE", tag="honest_n_to_1"))
    B.append(bank("BATCHOK", "2026-07-05", round(sum(nets), 2),
                  narration="PAYOUT SWEEP BATCHOK"))
    # Distractor credit: same amount, lookalike reference, no ledger
    B.append(bank("ADVN1Z", "2026-07-05", round(sum(nets), 2),
                  narration="PAYOUT SWEEP LOOKALIKE"))

    # Broken batch: credit is short of the sum and equals neither member net.
    fee4, net4 = fee_net(1500.00)
    fee5, net5 = fee_net(2500.00)
    short_credit = round(net4 + net5 - 15.00, 2)
    L.append(ledger("ADVN1SHORT", "2026-07-05", 1500.00, "Broken Batch"))
    L.append(ledger("ADVN1GAP", "2026-07-05", 2500.00, "Broken Batch"))
    G.append(gateway("ADVN1SHORT", "2026-07-05", 1500.00, fee=fee4, net=net4, batch="BATCHBAD"))
    G.append(gateway("ADVN1GAP", "2026-07-05", 2500.00, fee=fee5, net=net5, batch="BATCHBAD"))
    B.append(bank("BATCHBAD", "2026-07-05", short_credit, narration="PAYOUT SWEEP BATCHBAD"))
    # Frozen classifier: batch credit is not keyed to the order id, so
    # settlement-with-empty-order-bank maps to missing_bank_credit.
    T.append(gt("ADVN1SHORT", "EXCEPTION", "missing_bank_credit", ledger_amt=1500, gw=1500,
                settle=net4, bank_amt=short_credit, tag="batch_shortfall"))
    T.append(gt("ADVN1GAP", "EXCEPTION", "missing_bank_credit", ledger_amt=2500, gw=2500,
                settle=net5, bank_amt=short_credit, tag="batch_shortfall"))
    return write_suite(
        "n1_distractor", "matching", L, G, B, T,
        "Honest N:1 batch plus a same-amount distractor credit, and a short-batch that must not clear.",
    )


def suite_fee_tds_conflict():
    L, G, B, T = ballast("FEE", 3)
    # Net does not equal gross - fee - tds
    L.append(ledger("ADVFEE1", "2026-07-06", 10000.00, "Broken Arithmetic"))
    G.append(gateway("ADVFEE1", "2026-07-06", 10000.00, fee=200.00, tds=0.00,
                     refund=0.00, net=9000.00))
    B.append(bank("ADVFEE1", "2026-07-06", 9000.00))
    T.append(gt("ADVFEE1", "EXCEPTION", "amount_mismatch", ledger_amt=10000,
                gw=10000, settle=9000, bank_amt=9000, tag="gateway_math_break"))

    # Ledger vs gateway gross disagree
    fee, net = fee_net(8000.00)
    L.append(ledger("ADVFEE2", "2026-07-06", 8200.00, "Gross Mismatch"))
    G.append(gateway("ADVFEE2", "2026-07-06", 8000.00, fee=fee, net=net))
    B.append(bank("ADVFEE2", "2026-07-06", net))
    T.append(gt("ADVFEE2", "EXCEPTION", "amount_mismatch", ledger_amt=8200,
                gw=8000, settle=net, bank_amt=net, tag="ledger_vs_gross"))
    return write_suite(
        "fee_tds_conflict", "matching", L, G, B, T,
        "Gateway arithmetic breaks and ledger↔gross disagreement must stay exceptions.",
    )


def suite_sla_boundary():
    L, G, B, T = ballast("SLA", 3, date="2026-07-20")
    fee, net = fee_net(3100.00)
    # Distant IDs so SLA is tested, not near-ID candidate escalation.
    L.append(ledger("ADVPENDSLA", "2026-08-03", 3100.00, "Pending Bank"))
    G.append(gateway("ADVPENDSLA", "2026-08-03", 3100.00, fee=fee, net=net))
    T.append(gt("ADVPENDSLA", "PENDING", "", ledger_amt=3100, gw=3100, settle=net,
                match_type="", tag="within_sla_pending", review="false"))

    fee2, net2 = fee_net(2800.00)
    L.append(ledger("ADVAGEDBANK", "2026-07-01", 2800.00, "Aged Missing Bank"))
    G.append(gateway("ADVAGEDBANK", "2026-07-01", 2800.00, fee=fee2, net=net2))
    T.append(gt("ADVAGEDBANK", "EXCEPTION", "missing_bank_credit", ledger_amt=2800,
                gw=2800, settle=net2, tag="past_sla_missing_bank"))

    # Date-lag match: settlement 2026-07-20, bank 2026-07-24
    fee3, net3 = fee_net(1900.00)
    L.append(ledger("ADVLAGFUZZY", "2026-07-20", 1900.00, "Lag Match"))
    G.append(gateway("ADVLAGFUZZY", "2026-07-20", 1900.00, fee=fee3, net=net3))
    B.append(bank("ADVLAGFUZZY", "2026-07-24", net3))
    T.append(gt("ADVLAGFUZZY", "MATCH", ledger_amt=1900, gw=1900, settle=net3,
                bank_amt=net3, match_type="FUZZY", tag="settlement_bank_lag"))
    return write_suite(
        "sla_boundary", "matching", L, G, B, T,
        "Declared as_of clock: pending inside SLA, exception past SLA, FUZZY for bank lag.",
        recon_meta={"as_of": "2026-08-05", "sla_days": 7, "declared": True},
        extra_meta={"declared_as_of": "2026-08-05"},
    )


def suite_reversal_debit():
    L, G, B, T = ballast("REV", 3)
    fee, net = fee_net(4500.00)
    # Full reversal via equal debit
    L.append(ledger("ADVREV1", "2026-07-07", 4500.00, "Full Reversal"))
    G.append(gateway("ADVREV1", "2026-07-07", 4500.00, fee=fee, net=net, refund=net))
    B.append(bank("ADVREV1", "2026-07-07", net, debit=0.0, utr="UTRREV1C"))
    B.append(bank("ADVREV1", "2026-07-08", 0.0, debit=net, utr="UTRREV1D",
                  narration="REVERSAL ADVREV1"))
    T.append(gt("ADVREV1", "EXCEPTION", "refund_full", ledger_amt=4500, gw=4500,
                settle=net, tag="full_reversal_debit"))

    fee2, net2 = fee_net(3600.00)
    L.append(ledger("ADVREV2", "2026-07-07", 3600.00, "Partial Refund"))
    G.append(gateway("ADVREV2", "2026-07-07", 3600.00, fee=fee2, net=net2, refund=800.00))
    B.append(bank("ADVREV2", "2026-07-07", round(net2 - 800.00, 2)))
    T.append(gt("ADVREV2", "EXCEPTION", "refund_partial", ledger_amt=3600, gw=3600,
                settle=net2, bank_amt=round(net2 - 800.00, 2), tag="partial_refund"))
    return write_suite(
        "reversal_debit", "matching", L, G, B, T,
        "Explicit refund/debit evidence must classify as refund, not a silent match.",
    )


def suite_locale_money_ok():
    # ₹4,500 is VALID and must canonicalize, then match.
    L = [ledger("ADVLOC1", "2026-07-08", "₹4,500", "Locale Rupee")]
    G = [gateway("ADVLOC1", "2026-07-08", "Rs. 4,500", fee=90.00, net=4410.00)]
    B = [bank("ADVLOC1", "2026-07-08", "4,410.00")]
    T = [gt("ADVLOC1", "MATCH", ledger_amt=4500, gw=4500, settle=4410, bank_amt=4410,
            match_type="EXACT", tag="locale_currency_ok")]
    L2, G2, B2, T2 = ballast("LOC", 3, date="2026-07-08")
    return write_suite(
        "locale_money_ok", "matching", L + L2, G + G2, B + B2, T + T2,
        "Locale-formatted ₹4,500 / Rs. 4,500 must validate and match, not quarantine.",
    )


def suite_malformed_money():
    L, G, B, T = ballast("BAD", 2)
    L.append(ledger("ADVBAD1", "2026-07-09", "abc", "Malformed Amount"))
    G.append(gateway("ADVBAD1", "2026-07-09", 1200.00))
    B.append(bank("ADVBAD1", "2026-07-09", 1176.00))
    T.append(gt("ADVBAD1", "EXCEPTION", "invalid_amount", ledger_amt="",
                tag="malformed_abc", review="true"))
    return write_suite(
        "malformed_money", "ingest", L, G, B, T,
        "Critical amount 'abc' must fail the batch as VALIDATION_FAILED.",
        expected_ingest={
            "status": "VALIDATION_FAILED",
            "must_quarantine_field": "amount",
            "must_quarantine_value": "abc",
            "block_reconcile": True,
        },
    )


def suite_missing_amount():
    L, G, B, T = ballast("MIS", 2)
    L.append(ledger("ADVMIS1", "2026-07-09", "", "Blank Amount"))
    G.append(gateway("ADVMIS1", "2026-07-09", 800.00))
    B.append(bank("ADVMIS1", "2026-07-09", 784.00))
    T.append(gt("ADVMIS1", "EXCEPTION", "missing_amount", tag="blank_critical_amount",
                review="true"))
    return write_suite(
        "missing_amount", "ingest", L, G, B, T,
        "Blank critical ledger amount must fail the batch — never coerce to 0.",
        expected_ingest={
            "status": "VALIDATION_FAILED",
            "must_quarantine_field": "amount",
            "block_reconcile": True,
        },
    )


GENERATORS = [
    suite_near_id_collision,
    suite_duplicate_bank,
    suite_amount_threshold,
    suite_n1_distractor,
    suite_fee_tds_conflict,
    suite_sla_boundary,
    suite_reversal_debit,
    suite_locale_money_ok,
    suite_malformed_money,
    suite_missing_amount,
]


def main():
    ADV.mkdir(parents=True, exist_ok=True)
    names = []
    for fn in GENERATORS:
        dest = fn()
        names.append(dest.name)
        print(f"wrote {dest.relative_to(ROOT)}")
    index = {
        "suites": names,
        "protocol": "adversarial_independent",
        "heldout_untouched": True,
        "generator": "_generate_adversarial.py",
    }
    (ADV / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"{len(names)} adversarial suites")


if __name__ == "__main__":
    main()
