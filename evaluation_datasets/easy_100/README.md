# easy_100

Difficulty: easy · seed 194827 · 100 base transactions

Give the agent only these three files:

- `internal_ledger.csv`
- `gateway_settlement.csv`
- `bank_statement.csv`

Keep `ground_truth.csv` and `dataset_metadata.json` out of the run.

## Row counts

| File | Rows |
|------|------|
| internal_ledger.csv | 100 |
| gateway_settlement.csv | 95 |
| bank_statement.csv | 93 |
| ground_truth.csv (hidden) | 100 |

## Expected mix (totals only)

- MATCH: 80 · EXCEPTION: 20  
- EXACT: 70 · FUZZY/TOLERANCE: 10  
- MANY_TO_ONE / ONE_TO_MANY: 0  

Per-row answers are not listed here.

## What’s in the mix

- clean 2% fee exact ties  
- hyphen vs concatenated order ids  
- 1–2 day bank lag  
- tiny rounding  
- missing settlement / missing bank  
- obvious partial refunds  
- duplicate bank credits  
- clear amount mismatches  

## Columns

Ledger `order_id` is the merchant order. Gateway `merchant_order_id` usually matches it but may be formatted differently. Bank `reference` might be an order id, a payout batch, a UTR, or blank.

Amounts are INR. Gateway `net_amount` is meant as gross − fee − TDS (refunds have their own column and can also show up as bank reversals).
