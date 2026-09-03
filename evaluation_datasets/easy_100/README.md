# easy_100

**Difficulty:** easy
**Seed:** 194827
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
| gateway_settlement.csv | 95 |
| bank_statement.csv | 93 |
| ground_truth.csv (hidden) | 100 |

## Expected decision mix (aggregate)

- MATCH: 80
- EXCEPTION: 20
- EXACT: 70
- FUZZY/TOLERANCE: 10
- MANY_TO_ONE: 0
- ONE_TO_MANY: 0

Individual transaction answers are not listed here.

## Anomaly / challenge categories

- clean 2% MDR exact ties
- order-id hyphen vs concatenated formatting
- 1–2 day bank posting lag
- sub-rupee rounding
- missing settlement
- missing bank credit
- obvious partial refunds
- duplicate bank credits
- clear amount mismatches

## Column notes

Ledger `order_id` is the merchant order reference. Gateway `merchant_order_id` usually corresponds to it but may be formatted differently or, in hard cases, wrong. Bank `reference` may be an order id, a payout batch id, a UTR-only narration, or blank.

Amounts are INR. Gateway `net_amount` is intended as gross − fee − TDS (refunds are a separate column and may also appear as bank reversals).
