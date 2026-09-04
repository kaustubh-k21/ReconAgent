# Held-out evaluation

The agent only sees the three input CSVs. It never opens `ground_truth.csv` or `dataset_metadata.json` while scoring.

```text
eval CSVs  →  adapt columns  →  match + classify  →  predictions.json
ground_truth.csv  →  evaluator.py  ←  predictions.json
```

## Run the three sets

```bash
python evaluation_datasets/run_heldout.py
```

Per set under `evaluation_datasets/_work/<name>/`:

- `engine/` — files the matcher actually used  
- `predictions.json` — what the agent said  
- `evaluation.json` — scores, including per-row detail  

Rollup: `evaluation_datasets/heldout_evaluation.json`

## What the adapter is allowed to do

- Rename columns (`merchant_order_id` → `order_id`, `value_date` → `credit_date`, and similar)  
- Optionally normalize IDs (casefold, strip hyphens) so `HSNE-1` and `HSNE1` can meet — turn off with `--no-normalize-ids`  

It does **not** copy labels into the engine folder or invent batch IDs from ground truth.

## What we care about first

1. **False clear** — called match when it should have stayed open (worst miss)  
2. False exception  
3. Decision accuracy (pending is neither a good match nor a caught break)  
4. Match type, only when both sides say match  
5. Exception reason, only when both sides say exception  

## Labels used on easy / medium / hard

Amount gaps with no refund or debit story:

| Gap | Label |
|-----|--------|
| ≤ ₹10 | `unexplained_variance` |
| > ₹10, or ledger vs gateway math break | `amount_mismatch` |

When the expected answer is match:

| Situation | Label |
|-----------|--------|
| Split captures / combined payouts | `ONE_TO_MANY` / `MANY_TO_ONE` |
| ID format difference, or settlement→bank lag | `FUZZY` |
| Fee / TDS / tiny rounding, same-day bank | `TOLERANCE` |
| Standard fee, same-day bank, normal IDs | `EXACT` |

If both delay and fee variance apply, use `FUZZY`. Customer-name spelling is not a join key.

## Score one file by hand

```bash
python evaluation_datasets/evaluator.py \
  --ground-truth evaluation_datasets/hard_100/ground_truth.csv \
  --predictions evaluation_datasets/_work/hard_100/predictions.json
```

## Harder “break it” suites

Same path: adapt → agent → `evaluator.py`. Does not change the held-out CSVs or the matcher / classifier / evaluator code. Pending is scored in `run_adversarial.py` only.

```bash
./venv/bin/python evaluation_datasets/_generate_adversarial.py   # rebuild if the generator changed
./venv/bin/python evaluation_datasets/run_adversarial.py
```

Summary: `evaluation_datasets/adversarial_evaluation.json`  
Suite notes: `evaluation_datasets/adversarial/README.md`

Same priority order as held-out, plus whether ingest passed. Clearing a pending row as a match counts as a false clear.

## Demo data in `data/`

`python main.py --regen` rebuilds the small synthetic demo. Missing files no longer quietly regenerate a 70-row set without you asking.
