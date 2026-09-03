# medium_100

**Difficulty:** medium
**Seed:** 561039
**Base transactions:** 100

## Intended use

Held-out evaluation only. Feed the agent **only**:

- `internal_ledger.csv`
- `gateway_settlement.csv`
- `bank_statement.csv`

Do **not** provide `ground_truth.csv` or `dataset_metadata.json` at inference time.

## Record counts

| File | Rows |
|---|---|
| internal_ledger.csv | 100 |
| gateway_settlement.csv | 113 |
| bank_statement.csv | 107 |
| ground_truth.csv (hidden) | 100 |

## Expected decision mix (aggregate)

- MATCH: 60
- EXCEPTION: 40
- EXACT: 31
- FUZZY/TOLERANCE: 23
- MANY_TO_ONE: 3
- ONE_TO_MANY: 3

Individual transaction answers are not listed here.

## Anomaly / challenge categories

- variable MDR and TDS stacked with posting delay
- hyphen/case order-id drift and near-duplicate customer names
- N:1 payout batches and 1:N split captures
- partial and full refunds
- duplicate gateway and duplicate bank rows
- missing legs
- unrelated decoy gateway and bank lines
- T+1–T+3 settlement lag

## Column notes

Ledger `order_id` is the merchant order reference. Gateway `merchant_order_id` usually corresponds to it but may be formatted differently or, in hard cases, wrong. Bank `reference` may be an order id, a payout batch id, a UTR-only narration, or blank.

Amounts are INR. Gateway `net_amount` is intended as gross − fee − TDS (refunds are a separate column and may also appear as bank reversals).
