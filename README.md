# ReconAgent

ReconAgent lines up three files that rarely agree on their own:

1. **Internal ledger** — what the business booked  
2. **Settlement / payment report** — what the gateway settled after fees, TDS, refunds  
3. **Bank statement** — what actually hit the account  

It clears rows when the proof is strong enough. Everything else stays open with a reason a person can read. Wrong clears are worse than open exceptions, so it leans that way on purpose.

There is **no LLM** in the path. Default explanation is rule-based. Optional ML (`--ml`) is only for the leftover exception list if you train a model.

## Two screens (easy to mix up)

| Screen | File / command | What it is |
|--------|----------------|------------|
| **Upload workspace** | `./venv/bin/python controller_server.py` → http://127.0.0.1:8765/ | Drop three CSVs, validate, run recon, review exceptions, override with a note |
| **Offline report** | open `dashboard.html` in a browser | Snapshot after `python3 main.py` — no upload, no server |

If you need the drop-zone story, use the controller. `dashboard.html` is only the results write-up.

## Flow

```text
Three CSVs
    ↓
Check money cells and columns
    ↓
Match what is safe
    ↓
Name the leftovers
    ↓
Review (and override if needed)
```

Rough match order: exact → small fee / TDS / rounding → lag / batch payouts → leave the rest as exceptions. Credits still inside the SLA stay **pending**, not failed.

## Try it

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Demo report (no server):**

```bash
python3 main.py          # writes results.json and refreshes dashboard.html
open dashboard.html      # or just open the file in your browser
```

**With upload UI:**

```bash
./venv/bin/python controller_server.py
# browser: http://127.0.0.1:8765/
```

Upload ledger + settlement + bank → validate → run. Exception queue is the default. You can force match, force exception, or mark pending; it asks for a short audit note.

**Optional ML on exceptions:**

```bash
python3 train_model.py   # once, if you want the model file
python3 main.py --ml
```

**Held-out score check:**

```bash
./venv/bin/python evaluation_datasets/run_heldout.py
```

More on scoring: `evaluation_datasets/EVALUATION.md`.

## What it usually catches

- Exact ties  
- ID formatting quirks (hyphens, case)  
- Rounding and fee / TDS gaps  
- Settlement lag and batch payouts  
- Duplicate ledger or bank rows  
- Missing settlement or missing bank credit  
- Refunds / reversals when the files show it  
- Chargeback-style wording when it shows up in narration  

If proof is thin, it does not invent a clear.

## Upload checks (controller)

Before matching, money fields are parsed carefully:

```text
0.00     ok (real zero)
blank    missing
₹4,500   ok
abc      bad
```

A bad required amount stops the run (`VALIDATION_FAILED`) instead of becoming a quiet zero. The UI shows file, row, and field.

## CSV shapes

Point `python3 main.py --data-dir PATH` at a folder with the three files, or upload the same kinds in the controller.

**Built-in demo style** (`data/`):

| File | Need at least |
|------|----------------|
| `internal_ledger.csv` | `order_id`, `order_date`, `amount`, `customer` |
| `settlement_report.csv` | `order_id`, `gross_amount`, `net_amount` (+ dates, fee, tds, refund helps) |
| `bank_statement.csv` | `reference`, `credit_amount` (+ dates, debit, narration helps) |

**Held-out / eval style:**

| File | Need at least |
|------|----------------|
| `internal_ledger.csv` | `order_id` or `merchant_order_id` |
| `gateway_settlement.csv` | order id + `gross_amount`, `net_amount` |
| `bank_statement.csv` | `credit_amount` (+ date / reference) |

Optional `recon_meta.json`:

```json
{ "as_of": "2026-08-05", "sla_days": 7 }
```

Without an `as_of` date, missing bank rows are treated as aged (closed books), not pending.

Wrong layout → `SCHEMA_ERROR`. Bad money → `VALIDATION_FAILED`.

## Matching stance

Clear only when evidence is enough. Otherwise exception or pending. The point is not “match everything”; it is “don’t say money is fine when it isn’t.”

## Naming leftovers

Default: rules. Optional: `--ml` after training.

Each open row gets a cause, short plain-English evidence, and severity. Confidence shows mainly when the call is shaky — not on every obvious case.

## Scores on held-out sets

Three separate 100-row sets the matcher did not train on:

|                           | Easy | Medium | Hard |
| ------------------------- | ---: | -----: | ---: |
| Decision (match vs break) | 100% |   100% | 100% |
| False clears              |    0 |      0 |    0 |
| Match precision / recall  | 100% |   100% | 100% |
| Exception reason (when both sides exception) | 100% | 100% | 94.4% |

So **300/300** match-vs-exception decisions and **zero false clears** on these synthetic sets. Real production books will look messier; treat the numbers as a lab check, not a guarantee.

```bash
./venv/bin/python evaluation_datasets/run_heldout.py
./venv/bin/python evaluation_datasets/run_adversarial.py
```

## Layout

```text
reconagent/
├── main.py                 # offline pipeline → results + dashboard
├── matcher.py
├── exception_classifier.py # rules (default)
├── ingest_adapter.py
├── ingest_validate.py
├── money.py
├── schema_contract.py
├── validate_sources.py
├── predictions.py
├── train_model.py          # optional ML
├── ml_classifier.py
├── ml_features.py
├── requirements.txt
├── matching_policy.json
│
├── controller.html         # upload UI
├── controller_server.py
├── controller_batch.py
├── dashboard.html          # offline report (embedded results)
├── dashboard_template.html
│
├── data/                   # demo CSVs + recon_meta.json
├── evaluation_datasets/
├── tests/
└── exception_model.joblib  # only after train_model.py
```

## One-liner

Prove the clear. Leave the rest open with a reason someone can check.
