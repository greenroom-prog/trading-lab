# carry — near-certainty harvest

Built to answer one question: why does a $6.3M Polymarket book full of
98¢ positions look like it works, when a Brier-optimised forecast model
does not.

Answer: **they are not the same trade.** One is priced on forecast
accuracy. The other is priced on capital, queue position and resolution
mechanics, and its edge is not measurable at any sample size this lab can
reach. That is a finding about *which* edge to chase, not a strategy.

Drop into `~/settlement-edge/carry/`.

---

## The finding

The observed account's headline leg: **No @ 98.2¢, 3,088,869 shares.**

| | |
|---|---|
| capital committed | $3,033,269 |
| profit if it resolves | $55,600 |
| gross return | **1.833%** |
| annualised (42d cycle) | **17.10%** |
| one loss erases | **54.6 wins** |
| breakeven true probability | **98.200%** |

17% annualised, and it requires **zero forecasting skill** — only that
you are not wrong about a near-certainty, at size, repeatedly.

**The profile shape is the tell.** $6.3M deployed, 1,182 predictions,
biggest win ever **$36.7K**. A directional trader with $6.3M has a biggest
win in the hundreds of thousands. This one never wins big. That is the
P&L distribution of a *market maker*, not a forecaster — thousands of
small outcomes, no convex payoff. `evidence: inferred` from the numbers,
not confirmed.

The leg-1 profit alone ($55.6K) is **1.51× his biggest win ever**. Either
this is a size ramp beyond anything he has done, or the badge price is a
mark rather than an entry. Unresolved — see Open questions.

**Legs 1 and 2 sum to exactly 100.0%.** P(cut)=1.8%, P(hike)=42.6%,
P(hold)=55.6%. The book is internally coherent across all three states.
He is not taking a view; he is holding the simplex.

---

## Why ours isn't mathing — the gate

At 98.2¢ you need to know the true probability to about **0.3pp**. Kelly
sizing crosses zero at exactly `q == p`, so:

```
  q = 98.5%  ->  f* = +16.67% of bankroll
  q = 98.2%  ->  f* =   0.00%
  q = 97.8%  ->  f* = -22.22%      a 0.7pp error inverts the position
```

To distinguish a 0.5pp edge at that price you need **12,605 settlements.**
At n=20 the 95% CI half-width on the loss rate is **±5.83pp** — the
measurement is **11.7× wider than the edge you are hunting.**

```
edge  0.2pp -> needs 73,144 settlements. CI is 29.1x the edge. UNMEASURABLE
edge  0.5pp -> needs 12,605 settlements. CI is 11.7x the edge. UNMEASURABLE
edge  1.0pp -> needs  3,524 settlements. CI is  5.8x the edge. UNMEASURABLE
```

Verified two ways: two-proportion power calculation, and the sample size
at which a one-arm CI half-width equals the edge. The two routes differ
by a factor the test predicts analytically (4.861 predicted, 4.839
observed) — they agree.

**Consequence for `~/settlement-edge`.** Aggregate Brier 0.0518 vs market
0.0568 is dominated by mid-band forecasts. It says nothing about
calibration in the 95–99.5% bucket, which is the only bucket this trade
lives in, and which cannot be validated with the 20-settlement target.
The Brier harness needs re-scoring **by price bucket**, and the tail
bucket will not reach significance. Plan around that rather than waiting
for it.

**So if the edge is real it is structural, not predictive:** maker
rebates, queue priority, fee capture, or an edge in *resolution
mechanics* (reading the rules for the case where the oracle does
something the forecast didn't contemplate). Those are verifiable by
reading, in an afternoon. Forecast calibration at 98¢ is not verifiable
at all here.

---

## Files

| file | does | tested |
|---|---|---|
| `economics.py` | return, Kelly, loss:gain, measurability | yes, 2 paths each |
| `ruin.py` | P(ruin) — Monte Carlo **and** exact lattice DP | yes, cross-checked |
| `book.py` | depth-constrained VWAP, max size at a return | yes, vs hand-calc |
| `verdict.py` | one trade in, size-or-refusal out | yes, on fixture |
| `scan.py` | live Polymarket/Kalshi scanner | **network layer UNTESTED** |
| `test_carry.py` | 9 groups, every number derived twice | — |
| `fixtures/observed_positions.json` | the screenshot, as data | — |

```bash
cd ~/settlement-edge/carry
python3 test_carry.py                    # must print ALL PASS first

python3 verdict.py --price 0.982 --days 42 --capital 50000 \
                   --fee-bps 0 --true-prob 0.985 --cycles 26

python3 scan.py --probe                  # ALWAYS before scanning
python3 scan.py --venue kalshi --band 0.92 0.995 \
                --capital 25000 --fee-bps <look_it_up>
```

`--fee-bps` has no default anywhere. A guessed fee schedule at a 1.8%
gross return is the difference between a trade and a donation.

---

## Kill record

**`ruin.py` v1 was wrong and the two-path rule caught it.** v1's "exact"
solution computed `P(loss count at horizon >= k*)` — ruin at the *end*.
Terminal wealth is order-independent, so that felt like a valid closed
form. Ruin is not order-independent: a path that takes six early losses
and recovers is ruined, and v1 scored it survived. Monte Carlo said
3.91%, "exact" said 1.73%, at p=0.95 f=0.20. Replaced with a forward DP
over the (cycle, loss-count) lattice with absorbing ruin states. Now
3.9080% vs 3.9758%.

Had both paths been written from the same idea, the bug would have
shipped agreeing with itself.

---

## What this is not

- **Not evidence the account is profitable.** *Positions Value* is size.
  *Biggest Win* is $36.7K. **No total P&L appears anywhere in the crop.**
  The premise "his math is mathing" is unverified — the screenshot proves
  he is large, not that he is right.
- **Not a strategy.** No fee schedule confirmed, no fill data, no
  resolution-rule review, nothing paper traded.
- **Not scalable down.** At $50K bankroll, correctly sized at 1% ruin:
  **$277 per cycle, $7,211 per year, and only if you never lose once.**
  The strategy's math maths *because of the capital*. Copying the shape
  without the balance sheet copies the tail risk and not the income.
- **Not survivorship-corrected.** Every account that ran this and got
  hit by one 98¢ leg going to zero is invisible. One loss on leg 1 costs
  $3.03M and needs 55 clean cycles to repay. The graveyard is unobserved
  and it is the strongest attack on the whole idea.

---

## Open questions

1. **Is the badge price avg entry or current mark?** Everything above is
   conditional on entry. Scroll right on the profile, read Avg / Current
   / P&L. One minute of work, changes the entire read: at mark, he is a
   directional trader sitting on a gain, not a carry harvester.
2. **Is he maker or taker?** 3.09M shares cannot be lifted from a book.
   That size is *built* by resting bids. If he is a maker, his edge
   includes the spread and none of the forecast math above applies to
   him. Check via the trade tape.
3. **What is the actual fee schedule, per venue, per side?** Blocking for
   every number in `scan.py`.
4. **Does re-scoring the settlement-edge Brier by price bucket show any
   tail calibration at all?** Expect no — but the *absence* is the
   result that redirects the lab.
5. **Resolution-rule edge cases.** The 1.8% is not "chance the Fed cuts."
   It is chance-of-cut *plus* oracle risk *plus* rule ambiguity. That
   third term is readable, not forecastable, and it is where a real
   structural edge would live.

---

## The pivot — and why it is not just a different unfalsifiable hunt

`python3 angles.py` ranks every candidate edge by **cost to know**, not
by expected profit. That ranking key is the whole argument.

```
     n req  unit                          ok  angle
         1  market snapshot                Y  term structure of yield
         1  book snapshot                  Y  ladder incoherence, ILLIQUID rungs
         1  market pair                    Y  cross-venue RESOLUTION-RULE mismatch
         1  year of market history         Y  capital lockup / redeploy frequency
        20  settlements you already have   Y  Brier by price bucket (diagnostic)
       100  observed fills                 Y  maker vs taker decomposition
     3,524  settlements                    N  forecast calibration, 1pp edge
    12,605  settlements                    N  forecast calibration @ 98.2c
```

Six of eight are reachable. The two that are not are the two the lab has
been working on.

### The lead angle: the venue prices probability, not time

Identical 98.2c on screen:

```
  resolves in   3d ->    811.5% annualised
  resolves in   5d ->    276.6% annualised
  resolves in  42d ->     17.1% annualised
  resolves in 200d ->      3.4% annualised     82x spread, same display
```

`termstructure.py` tests whether the market corrects for this. **The null
can kill it in one night**, which is the point:

> H0: within a price band, annualised yield has no relationship to
> days-to-resolution. If H0 survives, the angle is dead — say so and stop.

Two independent paths on the slope: OLS on `log(days)` with bootstrap CI,
and Spearman rank correlation. They cannot share a linearity error.

**The confound is checked explicitly.** Short-dated markets are near
resolution so price *should* converge to 1.0 — that is rational, not an
inefficiency. Only "price converges **and** yield still slopes" is a
finding. Known-case B plants a perfectly yield-flat market with maximal
price convergence (rho = −0.997) and the method correctly reports DEAD.

```bash
python3 termstructure.py --known-cases     # must pass before real data
python3 scan.py --venue kalshi --band 0.85 0.999 \
                --capital 5000 --fee-bps <x> --json snap.json
python3 termstructure.py --scan-json snap.json
```

### Two things in the existing lab that the reframe re-opens

1. **The ladder coherence checker found zero arbitrage across all
   *liquid* ladders.** Liquid is where the competition is. That is the
   EDGAR liquidity finding wearing a different hat — except here it
   points the other way: the filter that returned nothing may itself be
   the finding. Re-run unfiltered and report what the illiquid rungs do.
2. **The cross-venue scanner matches on title and polarity.** Two markets
   with the same title and opposite resolution *sources* are not the same
   market. The gap, if there is one, lives in the rules text, not the
   title.

### The blocking constraint on all of it

`capital lockup / redeploy frequency`. 276% annualised on a 5-day trade
is a *ranking* metric, not a return. If only three qualifying cycles
exist per year, realised return is 3 x 1.833% = 5.5%. Count the cycles
before valuing any of this. Listed as BLOCKING in `angles.py` for that
reason.
