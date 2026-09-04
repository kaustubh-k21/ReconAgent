# Extra stress suites

Separate from `easy_100` / `medium_100` / `hard_100`. Same path through the agent; matcher and classifier code stay as-is for the run.

```text
eval CSVs  →  adapt columns  →  match + classify  →  predictions.json
ground_truth.csv  →  evaluator.py  ←  predictions.json
```

Some suites only test upload/parse and should stop with `VALIDATION_FAILED`.

The agent never opens the ground-truth files while predicting. Pending scoring lives in `run_adversarial.py` only.

## Run

```bash
# rebuild CSVs only if the generator changed
./venv/bin/python evaluation_datasets/_generate_adversarial.py

./venv/bin/python evaluation_datasets/run_adversarial.py
```

Work copies go under `evaluation_datasets/_work/adversarial/` (gitignored).  
Summary: `evaluation_datasets/adversarial_evaluation.json`.

`sla_boundary` includes `recon_meta.json` (`as_of: 2026-08-05`). The runner copies that into the engine folder after adapt.

## Suites

| Suite | Kind | Must not happen |
|-------|------|-----------------|
| `near_id_collision` | matching | Similar IDs + same amount → silent match |
| `duplicate_identity` | matching | Dup bank / dup ledger get cleared |
| `amount_threshold` | matching | ₹0.99 may clear; ₹1 / ₹10 unexplained; bigger gaps mismatch |
| `n1_distractor` | matching | Real batch payout clears; same-amount distractor and short batch do not |
| `fee_tds_conflict` | matching | Broken fee math gets cleared |
| `sla_boundary` | matching | Inside SLA → pending; past SLA → missing bank; lag → fuzzy |
| `reversal_debit` | matching | Refund / debit evidence becomes a quiet match |
| `locale_money_ok` | matching | `₹4,500` / `Rs. 4,500` fail parse instead of matching |
| `malformed_money` | ingest | `abc` must fail validation |
| `missing_amount` | ingest | Blank required amount must fail validation |

## Score order

1. False clear (including pending auto-cleared as match)  
2. Decision accuracy (pending handled in this runner)  
3. Match type when both sides match  
4. Exception reason when both sides exception  
5. Ingest pass / fail  
