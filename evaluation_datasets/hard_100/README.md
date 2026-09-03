# hard_100

**Difficulty:** hard
**Seed:** 847201
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
| gateway_settlement.csv | 108 |
| bank_statement.csv | 98 |
| ground_truth.csv (hidden) | 100 |

## Expected decision mix (aggregate)

- MATCH: 46
- EXCEPTION: 54
- EXACT: 24
- FUZZY/TOLERANCE: 14
- MANY_TO_ONE: 5
- ONE_TO_MANY: 3

Individual transaction answers are not listed here.

## Anomaly / challenge categories

- identical amounts on unrelated orders
- near-duplicate customer names
- order ids off by one character
- duplicate gateway and bank rows
- split settlements and combined payouts
- partial settlement without supporting refund
- long independent settlement and bank delays
- refunds that mimic fee take-rates
- ₹1 / ₹2 / ₹5 unexplained gaps
- material bank shortfalls labeled amount_mismatch (same rule as easy/medium)
- bank credits with blank references
- gateway rows pointing at the wrong order id
- same customer + amount on nearby dates
- valid rows parked next to decoys
- high string-similarity IDs that must not match
- month-boundary and holiday posting
- swapped bank references
- ambiguous many-candidate cases marked for human review

## Column notes

Ledger `order_id` is the merchant order reference. Gateway `merchant_order_id` usually corresponds to it but may be formatted differently or, in hard cases, wrong. Bank `reference` may be an order id, a payout batch id, a UTR-only narration, or blank.

Amounts are INR. Gateway `net_amount` is intended as gross − fee − TDS (refunds are a separate column and may also appear as bank reversals).
