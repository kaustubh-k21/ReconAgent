"""Controller batch lifecycle. Does not change matcher or classifier rules."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
import exception_classifier as ec
import ingest_adapter
import matcher
from exception_copy import enrich_classified
from ingest_validate import (
    BANK_CREDIT,
    BANK_DEBIT,
    GATEWAY_MONEY,
    LEDGER_MONEY,
    IngestValidationFailed,
    STATUS_FAILED,
    STATUS_OK,
    apply_field_rules,
    parse_money,
    pick_field,
    validate_money_rows,
)
from money import INVALID, MISSING, VALID
from predictions import build_predictions

ROOT = Path(__file__).resolve().parent
BATCH_ROOT = ROOT / "controller_batches"

ROLES = ("ledger", "gateway", "bank")

ROLE_CANONICAL = {
    "ledger": "internal_ledger.csv",
    "gateway": None,  # settlement_report.csv or gateway_settlement.csv
    "bank": "bank_statement.csv",
}

# Source column → canonical field (first hit wins)
LEDGER_MAP = [
    ("order_id", ("merchant_order_id", "order_id", "transaction_id")),
    ("order_date", ("order_date", "txn_date")),
    ("amount", ("amount", "ledger_amount", "gross_amount")),
    ("customer", ("customer", "customer_name")),
    ("currency", ("currency",)),
]
GATEWAY_MAP = [
    ("order_id", ("merchant_order_id", "order_id")),
    ("settlement_date", ("settlement_date",)),
    ("gross_amount", ("gross_amount", "amount")),
    ("fee", ("fee",)),
    ("tds", ("tds",)),
    ("refund_amount", ("refund_amount",)),
    ("net_amount", ("net_amount", "settlement_amount")),
    ("settlement_batch_id", ("payout_batch", "settlement_batch_id")),
]
BANK_MAP = [
    ("reference", ("reference", "bank_reference")),
    ("credit_date", ("credit_date", "value_date")),
    ("credit_amount", ("credit_amount",)),
    ("debit_amount", ("debit_amount",)),
    ("utr", ("utr",)),
    ("narration", ("narration",)),
]

ROLE_MAPS = {"ledger": LEDGER_MAP, "gateway": GATEWAY_MAP, "bank": BANK_MAP}

MONEY_CANONICAL = {
    "amount", "gross_amount", "net_amount", "fee", "tds",
    "refund_amount", "credit_amount", "debit_amount",
}


def format_inr(value: object) -> str:
    """Display helper: 3499.00 → ₹3,499.00 (Indian grouping)."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    sign = "-" if n < 0 else ""
    n = abs(n).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    whole, frac = f"{n:.2f}".split(".")
    if len(whole) <= 3:
        grouped = whole
    else:
        last3 = whole[-3:]
        rest = whole[:-3]
        parts = []
        while rest:
            parts.append(rest[-2:])
            rest = rest[:-2]
        grouped = ",".join(list(reversed(parts)) + [last3])
    return f"{sign}₹{grouped}.{frac}"


def _ledger_amount(rec: dict) -> float:
    raw = rec.get("amount")
    if raw is None and isinstance(rec.get("ledger"), dict):
        raw = rec["ledger"].get("amount")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0

DETECT_HINTS = {
    "ledger": {
        "strong": {
            "order_id", "amount", "order_date", "ledger_amount", "transaction_id",
            "merchant_order_id", "customer_name", "currency",
        },
        "weak": {"customer", "channel", "gross_amount"},
    },
    "gateway": {
        "strong": {"net_amount", "settlement_date", "fee", "tds", "refund_amount"},
        "weak": {"gross_amount", "merchant_order_id", "payment_mode", "settlement_id", "amount"},
    },
    "bank": {
        "strong": {"credit_amount", "value_date", "credit_date", "utr", "narration"},
        "weak": {"debit_amount", "reference", "bank_reference"},
    },
}


def ensure_batch_root() -> Path:
    BATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return BATCH_ROOT


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    return fields, rows


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def create_batch() -> dict:
    ensure_batch_root()
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    path = BATCH_ROOT / batch_id
    (path / "raw").mkdir(parents=True)
    (path / "engine").mkdir(parents=True)
    state = {
        "batch_id": batch_id,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "EMPTY",
        "files": {role: None for role in ROLES},
        "detection": {},
        "mapping": {},
        "validation": None,
        "results_summary": None,
        "error": None,
    }
    _write_json(path / "state.json", state)
    return state


def load_batch(batch_id: str) -> dict:
    path = BATCH_ROOT / batch_id / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"batch not found: {batch_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_batch(state: dict) -> dict:
    state["updated_at"] = _now()
    path = BATCH_ROOT / state["batch_id"] / "state.json"
    _write_json(path, state)
    return state


def batch_dir(batch_id: str) -> Path:
    return BATCH_ROOT / batch_id


def detect_role(headers: list[str], filename: str = "") -> dict:
    cols = {h.strip() for h in headers if h}
    lower_name = filename.lower()
    scores = {}
    for role, hints in DETECT_HINTS.items():
        score = 0.0
        hits = []
        for col in hints["strong"]:
            if col in cols:
                score += 2.0
                hits.append(col)
        for col in hints["weak"]:
            if col in cols:
                score += 0.5
                hits.append(col)
        if role == "ledger" and ("ledger" in lower_name or "internal" in lower_name):
            score += 1.5
        if role == "gateway" and ("gateway" in lower_name or "settlement" in lower_name):
            score += 1.5
        if role == "bank" and ("bank" in lower_name or "statement" in lower_name):
            score += 1.5
        # External ledger: merchant_order_id + gross, no settlement columns.
        if role == "ledger" and "merchant_order_id" in cols and "order_date" in cols:
            if "settlement_date" not in cols and "net_amount" not in cols:
                score += 2.0
        if role == "gateway" and ("settlement_date" in cols or "net_amount" in cols):
            score += 1.0
        scores[role] = {"score": score, "hits": hits}

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
    best_role, best = ranked[0]
    second = ranked[1][1]["score"] if len(ranked) > 1 else 0
    if best["score"] < 2:
        confidence = "low"
        role = "unknown"
    elif best["score"] - second < 1.0:
        confidence = "medium"
        role = best_role
    else:
        confidence = "high"
        role = best_role
    return {
        "detected_role": role,
        "confidence": confidence,
        "scores": {k: v["score"] for k, v in scores.items()},
        "matched_columns": best["hits"] if role != "unknown" else [],
        "columns": sorted(cols),
    }


def build_mapping(role: str, headers: list[str], sample_rows: list[dict]) -> dict:
    cols = set(headers)
    mappings = []
    for canonical, aliases in ROLE_MAPS[role]:
        source = next((a for a in aliases if a in cols), None)
        sample = ""
        canonical_sample = ""
        sample_display = ""
        warning = None
        status = "missing"
        if source is None:
            warning = "not mapped"
        else:
            status = "mapped"
            for row in sample_rows[:3]:
                raw = row.get(source, "")
                if raw is None:
                    raw = ""
                sample = str(raw)
                if canonical in MONEY_CANONICAL:
                    parsed = parse_money(raw)
                    if parsed.status == VALID:
                        canonical_sample = parsed.canonical()
                        sample_display = format_inr(canonical_sample)
                        status = "mapped"
                    elif parsed.status == MISSING:
                        canonical_sample = "MISSING"
                        if canonical in {"fee", "tds", "refund_amount", "debit_amount"}:
                            status = "optional_missing"
                            sample_display = format_inr("0.00")
                        else:
                            status = "critical_missing"
                            sample_display = "MISSING"
                            warning = "mapping unavailable"
                    else:
                        canonical_sample = str(parsed)
                        sample_display = str(parsed)
                        status = "invalid"
                        warning = "mapping unavailable"
                else:
                    canonical_sample = sample
                    sample_display = sample
                if sample != "":
                    break
        mappings.append({
            "source": source,
            "canonical": canonical,
            "status": status,
            "sample_raw": sample,
            "sample_canonical": canonical_sample,
            "sample_display": sample_display,
            "warning": warning,
        })
    return {
        "role": role,
        "columns": headers,
        "fields": mappings,
    }


def _gateway_filename(headers: list[str]) -> str:
    cols = set(headers)
    if "merchant_order_id" in cols or ("gross_amount" in cols and "net_amount" in cols and "order_id" not in cols):
        return "gateway_settlement.csv"
    return "settlement_report.csv"


def store_upload(batch_id: str, role: str, filename: str, content: bytes) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    state = load_batch(batch_id)
    raw_dir = batch_dir(batch_id) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(filename).suffix.lower() or ".csv"
    dest = raw_dir / f"{role}{suffix}"
    dest.write_bytes(content)

    fields, rows = _read_csv(dest)
    detection = detect_role(fields, filename)
    # Controller-assigned role wins; detection is advisory.
    mapping = build_mapping(role, fields, rows)
    file_meta = {
        "role": role,
        "original_name": filename,
        "stored_as": dest.name,
        "bytes": len(content),
        "sha256": _sha256(dest),
        "rows": len(rows),
        "columns": fields,
        "uploaded_at": _now(),
        "detection": detection,
        "mapping": mapping,
    }
    state["files"][role] = file_meta
    state["detection"][role] = detection
    state["mapping"][role] = mapping
    state["validation"] = None
    state["results_summary"] = None
    state["error"] = None

    if all(state["files"][r] for r in ROLES):
        state["status"] = "FILES_UPLOADED"
    else:
        state["status"] = "UPLOADING"
    return save_batch(state)


def _stage_raw_for_adapter(batch_id: str) -> tuple[Path, str]:
    """Copy uploaded role files into a folder the adapter can recognize."""
    state = load_batch(batch_id)
    staged = batch_dir(batch_id) / "staged"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)

    ledger = state["files"]["ledger"]
    gateway = state["files"]["gateway"]
    bank = state["files"]["bank"]
    if not all([ledger, gateway, bank]):
        raise ValueError("all three files required: ledger, gateway, bank")

    raw = batch_dir(batch_id) / "raw"
    shutil.copy2(raw / ledger["stored_as"], staged / "internal_ledger.csv")

    gw_headers = gateway["columns"]
    gw_name = _gateway_filename(gw_headers)
    shutil.copy2(raw / gateway["stored_as"], staged / gw_name)
    shutil.copy2(raw / bank["stored_as"], staged / "bank_statement.csv")

    if gw_name == "gateway_settlement.csv":
        schema = "eval"
    else:
        schema = "engine" if (staged / "settlement_report.csv").exists() else "eval"
    return staged, schema


def _money_preview(role: str, rows: list[dict], filename: str) -> dict:
    kind = {"ledger": "ledger", "gateway": "gateway", "bank": "bank"}[role]
    report = validate_money_rows(filename, rows, kind)
    samples = []
    specs = {
        "ledger": LEDGER_MONEY,
        "gateway": GATEWAY_MONEY,
        "bank": (BANK_CREDIT, BANK_DEBIT),
    }[role]
    for i, row in enumerate(rows[:5], start=2):
        for spec in specs:
            if role == "bank" and spec.name == "debit_amount" and not any(a in row for a in spec.aliases):
                continue
            field_name, raw = pick_field(row, spec)
            if role == "bank" and field_name not in row and spec.name == "credit_amount":
                pass
            parsed = apply_field_rules(parse_money(raw if field_name in row else None), spec)
            samples.append({
                "row": i,
                "field": field_name,
                "raw": "" if raw is None else str(raw),
                "status": str(parsed) if parsed.status != VALID else f"VALID({parsed.canonical()})",
            })
    return {
        "status": report.status,
        "quarantine_count": len(report.quarantine),
        "samples": samples,
    }


def validate_batch(batch_id: str) -> dict:
    state = load_batch(batch_id)
    if not all(state["files"][r] for r in ROLES):
        raise ValueError("upload ledger, gateway, and bank files first")

    state["status"] = "VALIDATING"
    save_batch(state)

    staged, schema = _stage_raw_for_adapter(batch_id)
    engine_dir = batch_dir(batch_id) / "engine"
    if engine_dir.exists():
        shutil.rmtree(engine_dir)
    engine_dir.mkdir(parents=True)

    money_reports = {}
    for role in ROLES:
        meta = state["files"][role]
        path = batch_dir(batch_id) / "raw" / meta["stored_as"]
        _, rows = _read_csv(path)
        fname = {
            "ledger": "internal_ledger.csv",
            "gateway": "gateway_settlement.csv" if schema == "eval" else "settlement_report.csv",
            "bank": "bank_statement.csv",
        }[role]
        money_reports[role] = _money_preview(role, rows, fname)

    quarantine = []
    validation_status = STATUS_OK
    adapter_meta = None
    try:
        if ingest_adapter.looks_like_eval_schema(str(staged)):
            adapter_meta = ingest_adapter.adapt_eval_dir(str(staged), str(engine_dir), normalize_ids=True)
            schema = "eval"
        elif ingest_adapter.looks_like_engine_schema(str(staged)):
            adapter_meta = ingest_adapter.canonicalize_engine_dir(str(staged), str(engine_dir))
            schema = "engine"
        else:
            gw = staged / "settlement_report.csv"
            alt = staged / "gateway_settlement.csv"
            if gw.exists() and not alt.exists():
                adapter_meta = ingest_adapter.canonicalize_engine_dir(str(staged), str(engine_dir))
                schema = "engine"
            else:
                raise ValueError(
                    "Could not detect a usable schema. Expected ledger + gateway/settlement + bank columns."
                )
    except IngestValidationFailed as e:
        validation_status = STATUS_FAILED
        quarantine = [q.as_dict() for q in e.report.quarantine]
        adapter_meta = None

    structural_errors = []
    if adapter_meta is not None:
        import validate_sources
        ok, errs = validate_sources.validate_sources(str(engine_dir))
        if not ok:
            # Skip money errors already captured in quarantine.
            for err in errs:
                if err == STATUS_FAILED:
                    validation_status = STATUS_FAILED
                    continue
                if "INVALID(" in err or "MISSING" in err:
                    validation_status = STATUS_FAILED
                structural_errors.append(err)
            if not ok and validation_status == STATUS_OK:
                validation_status = STATUS_FAILED

    mapping_preview = {role: state["mapping"].get(role) for role in ROLES}
    for role in ROLES:
        if state["mapping"].get(role):
            mapping_preview[role] = state["mapping"][role]

    validation = {
        "status": validation_status,
        "schema": schema,
        "quarantine": quarantine,
        "structural_errors": structural_errors,
        "money_preview": money_reports,
        "adapter": adapter_meta,
        "row_counts": {
            role: state["files"][role]["rows"] for role in ROLES
        },
        "validated_at": _now(),
    }
    _write_json(batch_dir(batch_id) / "validation.json", validation)

    state["validation"] = validation
    state["status"] = "READY" if validation_status == STATUS_OK else "VALIDATION_FAILED"
    state["error"] = None if validation_status == STATUS_OK else STATUS_FAILED
    return save_batch(state)


def _serialize_match(rec: dict) -> dict:
    return {
        "order_id": rec["order_id"],
        "amount": rec["ledger"]["amount"],
        "confidence": rec.get("confidence"),
        "note": rec.get("note"),
        "matched_by_rule": rec.get("matched_by_rule"),
        "auto_confirmed": rec.get("auto_confirmed", True),
        "leg_a": rec.get("leg_a"),
        "leg_b": rec.get("leg_b"),
    }


def reconcile_batch(batch_id: str) -> dict:
    state = load_batch(batch_id)
    if state.get("status") != "READY":
        raise ValueError("batch must be READY (validation passed) before reconciliation")

    engine_dir = batch_dir(batch_id) / "engine"
    if not engine_dir.exists():
        raise ValueError("engine directory missing; re-run validation")

    state["status"] = "RECONCILING"
    save_batch(state)

    results = matcher.run_matching(data_dir=str(engine_dir))
    with open(engine_dir / "internal_ledger.csv", newline="", encoding="utf-8") as f:
        ids = [row["order_id"] for row in csv.DictReader(f)]
    dupe_counts = dict(Counter(ids))
    classified = ec.classify_exceptions(results["exceptions"], dupe_counts, use_llm=False)
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    classified = sorted(
        classified,
        key=lambda x: (sev_rank.get(x.get("severity", "low"), 9), -float(x.get("amount") or 0)),
    )
    classified = enrich_classified(classified, results["exceptions"])
    breakdown = ec.cause_breakdown(classified)

    n_exact = len(results["exact"])
    n_fuzzy = len(results["fuzzy"])
    n_review = len(results.get("review", []))
    n_pending = len(results.get("pending", []))
    n_exc = len(results["exceptions"])
    total = n_exact + n_fuzzy + n_review + n_pending + n_exc
    auto = (n_exact + n_fuzzy) / total if total else 0.0
    safely = n_exact + n_fuzzy
    require_review = n_review + n_exc
    reconciled_value = round(
        sum(_ledger_amount(r) for r in results["exact"]) +
        sum(_ledger_amount(r) for r in results["fuzzy"]),
        2,
    )
    exception_value = round(sum(float(c.get("amount") or 0) for c in classified), 2)

    output = {
        "summary": {
            "total_transactions": total,
            "exact_matches": n_exact,
            "fuzzy_matches": n_fuzzy,
            "review_matches": n_review,
            "pending_bank": n_pending,
            "exceptions": n_exc,
            "match_rate": round(auto, 4),
            "auto_match_rate": round(auto, 4),
            "auto_plus_review_rate": round((n_exact + n_fuzzy + n_review) / total, 4) if total else 0.0,
            "classifier_mode": "rule_based",
            "batch_id": batch_id,
            "generated_at": _now(),
            "leg_summary": results.get("leg_summary", {}),
            "rule_counts": results.get("rule_counts", {}),
            "severity_breakdown": dict(Counter(c.get("severity", "low") for c in classified)),
            "policy": results.get("policy", {}),
            "ingest": state.get("validation", {}).get("adapter"),
            "controller": {
                "processed": total,
                "safely_reconciled": safely,
                "require_review": require_review,
                "pending_bank": n_pending,
                "exceptions": n_exc,
                "reconciled_value": reconciled_value,
                "exception_value": exception_value,
                "reconciled_value_display": format_inr(reconciled_value),
                "exception_value_display": format_inr(exception_value),
                "guardrails": {
                    "label": "AUTO-MATCH GUARDRAILS: ACTIVE",
                    "notes": [
                        "Threshold policy v1",
                        "Invalid data blocked",
                        "Review tier enabled",
                    ],
                },
            },
        },
        "exact_matches": [_serialize_match(r) for r in results["exact"]],
        "fuzzy_matches": [_serialize_match(r) for r in results["fuzzy"]],
        "review_matches": [_serialize_match(r) for r in results.get("review", [])],
        "pending": [_serialize_match(r) for r in results.get("pending", [])],
        "exceptions": classified,
        "exception_cause_breakdown": breakdown,
        "predictions": build_predictions(results, classified),
    }
    out_path = batch_dir(batch_id) / "results.json"
    _write_json(out_path, output)

    state["results_summary"] = output["summary"]
    state["status"] = "COMPLETED"
    state["error"] = None
    return save_batch(state)


def get_results(batch_id: str) -> dict:
    path = batch_dir(batch_id) / "results.json"
    if not path.exists():
        raise FileNotFoundError("results not ready")
    return json.loads(path.read_text(encoding="utf-8"))


def list_batches(limit: int = 20) -> list[dict]:
    ensure_batch_root()
    items = []
    for p in sorted(BATCH_ROOT.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        state_path = p / "state.json"
        if state_path.exists():
            items.append(json.loads(state_path.read_text(encoding="utf-8")))
        if len(items) >= limit:
            break
    return items
