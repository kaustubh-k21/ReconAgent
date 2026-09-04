# medium_100

Difficulty: medium · seed 561039 · 100 base transactions

Give the agent only these three files:

- `internal_ledger.csv`
- `gateway_settlement.csv`
- `bank_statement.csv`

Keep `ground_truth.csv` and `dataset_metadata.json` out of the run.

## Row counts

| File | Rows |
|------|------|
| internal_ledger.csv | 100 |
| gateway_settlement.csv | 113 |
| bank_statement.csv | 107 |
| ground_truth.csv (hidden) | 100 |

## Expected mix (totals only)

- MATCH: 60 · EXCEPTION: 40  
- EXACT: 31 · FUZZY/TOLERANCE: 23  
- MANY_TO_ONE: 3 · ONE_TO_MANY: 3  

Per-row answers are not listed here.

## What’s in the mix

- variable fees and TDS mixed with posting delay  
- hyphen / case drift on order ids, near-duplicate customer names  
- batch payouts and split captures  
- partial and full refunds  
- duplicate gateway and bank rows  
- missing legs  
- decoy gateway / bank lines that don’t belong  
- T+1–T+3 settlement lag  

## Columns

Ledger `order_id` is the merchant order. Gateway `merchant_order_id` usually matches it but may be formatted differently. Bank `reference` might be an order id, a payout batch, a UTR, or blank.

Amounts are INR. Gateway `net_amount` is meant as gross − fee − TDS (refunds have their own column and can also show up as bank reversals).
