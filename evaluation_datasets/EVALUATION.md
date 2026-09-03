# Held-out evaluation protocol

Predictions are produced **without** `ground_truth.csv` or `dataset_metadata.json`.

```text
eval CSVs  →  ingest_adapter  →  matcher + classifier  →  predictions.json
ground_truth.csv  →  evaluator.py  ←  predictions.json
```

## Run the three official sets

```bash
python evaluation_datasets/run_heldout.py
```

Writes per dataset under `evaluation_datasets/_work/<name>/`:

- `engine/` — adapted files the matcher actually saw
- `predictions.json` — agent output
- `evaluation.json` — independent scores (includes `per_row`)

Summary: `evaluation_datasets/heldout_evaluation.json`

## Adapter rules (fair)

- Renames columns only (`merchant_order_id` → `order_id`, `value_date` → `credit_date`, …).
- Does **not** copy labels into the engine folder.
- Does **not** invent `settlement_batch_id` from ground truth.
- **Declared ingest:** join keys are casefolded and hyphens stripped so `HSNE-1` and `HSNE1` can meet. Disable with `--no-normalize-ids`.

## What the evaluator reports (in order)

1. **False MATCH** — most important; auto-cleared a true break
2. False EXCEPTION
3. Decision accuracy (PENDING is not a correct MATCH or a caught EXCEPTION)
4. Match-type accuracy, only when both sides MATCH
5. Exception-reason accuracy, only when both sides EXCEPTION

## Shared taxonomy (easy / medium / hard)

Exception reasons for amount disagreement, with no refund or debit evidence:

| Residual | Label |
|---|---|
| ≤ ₹10 | `unexplained_variance` |
| > ₹10, or ledger↔gateway arithmetic break | `amount_mismatch` |

Match types when the expected decision is MATCH:

| Evidence | Label |
|---|---|
| Split captures / combined payouts | `ONE_TO_MANY` / `MANY_TO_ONE` |
| ID format difference, or settlement→bank lag > 0 | `FUZZY` |
| Fee-rate / TDS / sub-rupee rounding, same-day bank | `TOLERANCE` |
| Standard 2% fee, same-day bank, native IDs | `EXACT` |

If both a delay and a fee variance apply, the label is `FUZZY`. Customer-name spelling is not a join key, so it is `EXACT`.

## Score one predictions file

```bash
python evaluation_datasets/evaluator.py \
  --ground-truth evaluation_datasets/hard_100/ground_truth.csv \
  --predictions evaluation_datasets/_work/hard_100/predictions.json
```

## Adversarial protocol (independent)

Same ingest → agent → `evaluator.py` path. Does **not** touch the held-out
sets or matcher / classifier / evaluator code. PENDING is scored only in
`run_adversarial.py`.

```bash
./venv/bin/python evaluation_datasets/_generate_adversarial.py   # rebuild CSVs
./venv/bin/python evaluation_datasets/run_adversarial.py
```

Summary: `evaluation_datasets/adversarial_evaluation.json`.
Suite notes: `evaluation_datasets/adversarial/README.md`.

Report order is the same as held-out, plus ingest pass/fail. False MATCH
includes a PENDING row that the agent auto-cleared.

## Native demo data

`python main.py --regen` still builds synthetic `data/`. Missing files no longer silently regenerate a 70-row demo.
