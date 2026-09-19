# crypto settlement edge

Prices a Kalshi crypto ladder against the number that actually settles it.

## what settles the contract

    "the simple average of the sixty seconds of CF Benchmarks' BRTI
     before 5 PM EDT"

The market quotes off SPOT. The contract pays on a 60-second MEAN. Kalshi
broadcasts both on its own websocket (`cfbenchmarks_value`), so the
settlement input is readable live, with credentials you already have.

## the model

    sd_settle(T) = sigma_1s * sqrt( max(T-60,0) + 60/3 )

`60/3` is the variance of the mean of a Brownian path over the window —
averaging shrinks uncertainty. `sigma_1s` is MEASURED from the tick
stream (EWMA-smoothed), never assumed; the engine refuses to price until
it has enough ticks.

Inside the final minute the observed part of the average is locked in and
only the remainder still moves, so both the centre and the sd tighten.

The raw formula is validated by Monte Carlo against a random-walk
simulation at three horizons, within 6%.

BUT BTC IS NOT A RANDOM WALK. Measured from 590 live BRTI samples:

    horizon    5s     15s    30s    60s    120s
    actual/sqrt-scaled   1.18   1.08   1.00   0.97   0.77

It mean-reverts, so plain sqrt(t) OVERSTATES how far the index can
travel — and an overstated sd manufactures edges near the money that do
not exist. The first live run produced exactly that: 998 rows over 2c,
all clustered in five near-money strikes. Those were model error.

On 2026-09-01 the run priced a strike at 0.7%, 3.9%, 5.4%, 11.8% on four
consecutive seconds. A settled estimate does not move 17x in four
seconds — sigma had been computed from only 12 ticks and was far too
small, which made the model wildly overconfident. It happened to be
directionally right about that strike; that was luck, not skill.

Hence three hard gates:
  - ~90 ticks of WARMUP before any price is produced
  - the estimate must be SETTLED: the recent-window sigma must agree with
    the long-run EWMA to within the estimator's own sampling error
    (z/sqrt(2n)), not a guessed percentage
  - the model ABSTAINS beyond 5 minutes to settlement, because the
    correction is only measured to 2 minutes and beyond that it is
    extrapolation
  - it abstains until volatility has actually been measured

## files

    settle_model.py    pricing math, no I/O
    live_edge.py       stream -> price -> CSV.  --replay runs with no network
    tests/             21 + 20 assertions, all offline
    run_tests.sh       run everything

## use

    cp .env.example .env && chmod 600 .env      # fill in your keys
    ./run_tests.sh
    python3 live_edge.py --replay tests/fixtures/brti_ticks.json
    python3 live_edge.py --series KXBTCD --secs 1200

## what this does NOT do

- does not place orders
- does not prove an edge exists; it logs model-vs-market so that can be
  measured over many settlements
- assumes a driftless walk between now and settlement
- says nothing about whether you can be filled at the quoted size
