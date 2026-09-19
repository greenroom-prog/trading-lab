# KILLS

The most reused artifact either repo produced. A kill is only worth
keeping if the *number that killed it* is recorded — "it didn't work" is
not a kill record, it is a shrug, and it will not stop you re-testing the
same idea in eight months.

**Schema — every entry has all five fields.**

```
### <name>
killed     : YYYY-MM-DD
claim      : what it asserted, one sentence
falsifier  : the observation that would prove it wrong (written BEFORE testing)
number     : the measurement that fired the falsifier
lesson     : the generalisable rule, or "none - one-off"
```

---

### predecessor strategy, 1834% vs 743% buy-and-hold
```
killed     : pre-2026
claim      : returned 1834% against 743% B&H
falsifier  : edge disappears under a fill price knowable at decision time
number     : backtest filled at the CLOSING price
lesson     : passed out-of-sample, robustness AND permutation first.
             Statistical validation does not test execution. Anything
             that looks profitable is wrong until the execution
             assumption is checked.
```

### SPY overnight gap
```
killed     : 2026
claim      : an exploitable edge exists in the SPY overnight gap
falsifier  : edge requires a fill unavailable to a retail account
number     : the only edge lived at that unavailable fill price
lesson     : "a price exists" and "you can transact at it" are different
             claims. Check the second one first - it is cheaper.
```

### multi-timeframe continuation (GC=F, BTCUSD, US30, SPY)
```
killed     : 2026
claim      : 1h trend + lower-timeframe entry produces edge after costs
falsifier  : no significant positive mean return after realistic costs
number     : killed on initial run
lesson     : now instrumented. mtf/ measures the leaky-vs-clean 1h
             alignment delta. On a random walk the leak manufactures
             +0.48 bps from nothing. Suspect this before re-testing.
```

### 15+ price-based crypto and equity strategies
```
killed     : ongoing
claim      : various price-pattern edges
falsifier  : various
number     : various - see ~/trading-lab/strategies/
lesson     : price-pattern search has a 0/15+ record here. Treat any new
             price-only idea as needing an unusually good prior reason
             before it consumes a slot.
```

### forecast calibration at 98.2c (Kalshi/Polymarket carry)
```
killed     : 2026-09-04
claim      : a Brier-validated model can find edge in near-certainty
             contracts
falsifier  : the sample needed to measure a sub-1pp edge exceeds any
             sample the lab can collect
number     : 12,605 settlements for a 0.5pp edge. At n=20 the 95% CI
             half-width is +/-5.83pp - 11.7x wider than the edge.
             Kelly flips sign at exactly q == p, so a 0.7pp estimate
             error inverts the position.
lesson     : aggregate Brier is dominated by mid-band forecasts and says
             nothing about tail calibration. Estimating a RATE needs many
             observations; estimating a RELATIONSHIP needs one snapshot.
             Prefer the second wherever both are available.
```

### ruin.py v1 - "exact" closed form
```
killed     : 2026-09-04
claim      : P(ruin) = P(loss count at horizon >= k*), by order-independence
falsifier  : disagrees with an independent Monte Carlo estimate
number     : MC 3.91% vs "exact" 1.73% at p=0.95, f=0.20, 52 cycles
lesson     : terminal WEALTH is order-independent; RUIN is not. A path
             that takes six early losses and recovers is ruined, and v1
             scored it survived. Had both paths been written from the
             same idea the bug would have shipped agreeing with itself.
             Replaced with a lattice DP.
```

### mtf test fixture - no gap between bars
```
killed     : 2026-09-04
claim      : the fill-discipline test proves fills are not same-bar
falsifier  : the test passes on a generator where same-bar and next-bar
             fills are numerically identical
number     : generator set open[i] == close[i-1]; 1912/1912 "violations"
             that were actually the test being blind
lesson     : a fixture that cannot fail the test it guards is worse than
             no fixture. This was blind to the EXACT bug that killed the
             1834% strategy.
```

### mtf random-walk assertion - wrong sign
```
killed     : 2026-09-04
claim      : on a random walk the harness should show perm_p > 0.05
falsifier  : a correctly-behaving strategy fails the assertion
number     : mean -3.26 bps (= cost), perm_p 0.002 -> flagged as failure
lesson     : losing exactly your costs on a random walk is CORRECT. The
             failure to catch is fake POSITIVE edge. State the direction
             of the alternative hypothesis or the test catches nothing.
```

---

## Still open — not yet killed, not yet confirmed

| item | falsifier | cost to resolve |
|---|---|---|
| EDGAR short thesis | forced names are not shortable / not ETB | 25 API calls |
| EDGAR liquidity | no tradeable subset retains the signal | done once; re-check after borrow |
| overnight premium overlay | adjusted closes contaminate the overnight/intraday split | 1 evening |
| BRTI settlement model | Brier advantage vanishes over 20+ real settlements | 20 settlements |
| term structure of yield | rho(yield, days) not significantly negative in a price band | 1 snapshot |
| ladder incoherence, illiquid | rung sums within fees of 1.00 everywhere | 1 snapshot |
| cross-venue rule mismatch | paired markets resolve identically | reading, per pair |
| vision gate (video-memory) | ticker+timeframe correct < 4/5 frames | 5 frames |

### constraint.py — all 424B forms read as "a raise is happening now"
found : 2026-09-05
claim : a 424B filing means the company is raising capital
falsifier : a 424B form exists where the company raises nothing
number : CRMT had 3x 424B3 in 180d -> raise_in_progress=true.
424B3/424B7 are RESALE prospectuses: existing holders
selling their own shares. Company receives $0.
fix : split 424B into takedown (B5/B2/B4) vs resale (B3/B7);
_classify_form now longest-prefix-match so dict order
cannot reintroduce it. raise_in_progress fires on
takedown only; added resale_in_progress.
lesson : a prefix match on a form code silently merges forms with
opposite meanings. "424B" is not a form, it is a family.


### TrendMTF (2-timeframe EMA trend) — killed by the benchmarkkilled : 2026-09-05
claim : higher-TF trend filter + lower-TF pullback entry has an edge
falsifier : strategy underperforms buy-and-hold over the same window
number : Coinbase BTC/ETH 1h, 2023-12 to 2026-09, real fees.
market change +54.43%. strategy -6.02% over 185 trades.
v2 (volume + ADX + ATR stop) -6.45%. Every added filter
made it worse: all-off -0.97%, all-on -3.71%.
lesson : the earlier "+34% on BTC over 5.5y" result was never
compared to buy-and-hold. BTC rose far more over that
window. Measuring against ZERO instead of against the
honest alternative is the same class of error as filling
at the close. EVERY future result gets a benchmark row.


### TrendAlign (1h trend / 5m entry) — the fee curve, measured
killed : 2026-09-05
claim : faster timeframe pair cuts swap and rescues trend-following
falsifier : loss scales linearly with trade count, i.e. it IS the fees
number : Coinbase BTC/ETH 5m, market +19.95%. Cooldown sweep:
435 trades -23.31% | 380 -21.58% | 276 -14.79%
84 trades -6.23% | 0 0.00%
Perfectly monotonic. Zero trades = zero loss.
lesson : a strategy whose best outcome is not trading has no edge.
Fourth independent route to the same wall: 16 in-house
strategies, 64 matrix variants, 23 freqtrade community
strategies, and now a cooldown sweep. THE VENUE IS THE
PROBLEM. Test the fee hypothesis directly before building
anything else price-based.

### the fee hypothesis — tested directly, only HALF true
killed : 2026-09-05
claim : price strategies fail only because of retail fees
falsifier : at near-zero fees they still lose to buy-and-hold
number : same 5 strategies at 5bps vs 120bps. At 5bps four of five
turn POSITIVE (Heracles +3.28%, MultiMa +1.67%,
Strategy003 +0.61%, PatternRecognition +4.64%) with
profit factors 1.29-1.80. But market was +20.14%.
Best excess still -15.50.
lesson : fees turn losers into SMALL winners, not into edges. Even
free, these capture a quarter of what holding captures.
The escape hatch is closed: it is not just the venue.
Do not fetch more price strategies from GitHub - that is
now five independent routes to one wall.


### NostalgiaForInfinity X6 — the most-used community strategy, tested properly
killed : 2026-09-06
claim : the flagship freqtrade strategy (3.3k stars, years of live
use) has an edge on its designed setup
falsifier : underperforms buy-and-hold on its own recommended config
number : Coinbase 40 USD pairs, 5m tf, 6 slots, 26bps fee, 200 days.
21 trades, -1.19%. Market +53.40%. Excess -54.6.
Given exactly the universe/timeframe/slots its docs specify.
caveat : only 21 trades - it found almost nothing it liked. 4h
informative was RESAMPLED from 1h (Coinbase serves no 4h),
which may suppress signals. Not a clean kill, but the
direction matches five other routes.
lesson : sixth independent route to the same wall. If NFI cannot
clear buy-and-hold, further GitHub strategy hunting has a
very poor prior. Stop searching for strategies.

