# Adversarial evaluation suites

Independent of `easy_100` / `medium_100` / `hard_100`. Same protocol, frozen engine.

```text
eval CSVs  →  ingest_adapter  →  matcher + classifier  →  predictions.json
ground_truth.csv  →  evaluator.py  ←  predictions.json
```

Ingest-only suites stop at the adapter: `VALIDATION_FAILED` is the expected outcome.

The agent never opens `ground_truth.csv` or `dataset_metadata.json`.
Matcher, classifier, and `evaluator.py` are not modified for this protocol.
PENDING is scored in `run_adversarial.py` only.

## Run

```bash
# rebuild committed CSVs (only if the generator changed)
./venv/bin/python evaluation_datasets/_generate_adversarial.py

./venv/bin/python evaluation_datasets/run_adversarial.py
```

Work copies land in `evaluation_datasets/_work/adversarial/` (gitignored).
Summary: `evaluation_datasets/adversarial_evaluation.json`.

`sla_boundary` ships `recon_meta.json` (`as_of: 2026-08-05`). The runner copies
it into the engine folder after adapt — the adapter still does not.

## Suites

| Suite | Kind | What must not happen |
|---|---|---|
| `near_id_collision` | matching | Similar IDs, same amount → no false MATCH |
| `duplicate_identity` | matching | Duplicate bank credit / duplicate ledger stay exceptions |
| `amount_threshold` | matching | ₹0.99 clears; ₹1.00 / ₹10 unexplained; >₹10 mismatch |
| `n1_distractor` | matching | Honest N:1 clears; same-amount distractor and short batch do not. A short batch is labeled `missing_bank_credit` (credit is not keyed to the order id). |
| `fee_tds_conflict` | matching | Broken gateway math / ledger↔gross stay exceptions |
| `sla_boundary` | matching | Inside SLA → PENDING; past SLA → missing bank; lag → FUZZY |
| `reversal_debit` | matching | Refund / debit evidence is refund, not a silent match |
| `locale_money_ok` | matching | `₹4,500` / `Rs. 4,500` validate then MATCH |
| `malformed_money` | ingest | `abc` → `VALIDATION_FAILED` |
| `missing_amount` | ingest | Blank critical amount → `VALIDATION_FAILED` |

## What is scored first

1. **False MATCH** (including PENDING auto-cleared as MATCH)
2. Decision accuracy (PENDING credited only in this runner)
3. Match-type accuracy, only when both sides MATCH
4. Exception-reason accuracy, only when both sides EXCEPTION
5. Ingest pass/fail
