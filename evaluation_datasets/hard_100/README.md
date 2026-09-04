# hard_100

Difficulty: hard · seed 847201 · 100 base transactions

Give the agent only these three files:

- `internal_ledger.csv`
- `gateway_settlement.csv`
- `bank_statement.csv`

Keep `ground_truth.csv` and `dataset_metadata.json` out of the run.

## Row counts

| File | Rows |
|------|------|
| internal_ledger.csv | 100 |
| gateway_settlement.csv | 108 |
| bank_statement.csv | 98 |
| ground_truth.csv (hidden) | 100 |

## Expected mix (totals only)

- MATCH: 46 · EXCEPTION: 54  
- EXACT: 24 · FUZZY/TOLERANCE: 14  
- MANY_TO_ONE: 5 · ONE_TO_MANY: 3  

Per-row answers are not listed here.

## What’s in the mix

- same amounts on unrelated orders  
- near-duplicate names  
- order ids off by one character  
- duplicate gateway and bank rows  
- split settlements and combined payouts  
- partial settlement without a refund story  
- long independent delays on settlement and bank  
- refunds that look like fee take-rates  
- ₹1 / ₹2 / ₹5 unexplained gaps  
- bigger bank shortfalls as amount mismatch (same rule as easy/medium)  
- blank bank references  
- gateway rows pointed at the wrong order  
- same customer + amount on nearby dates  
- good rows parked next to decoys  
- lookalike IDs that must not clear  
- month-boundary / holiday posting  
- swapped bank references  
- multi-candidate cases meant for a human  

## Columns

Ledger `order_id` is the merchant order. Gateway `merchant_order_id` usually matches it but may be formatted differently. Bank `reference` might be an order id, a payout batch, a UTR, or blank.

Amounts are INR. Gateway `net_amount` is meant as gross − fee − TDS (refunds have their own column and can also show up as bank reversals).
