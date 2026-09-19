# START HERE

*One page. If you read nothing else, read §1.*

---

## 1. Run this tonight

```bash
python3 ops/borrow_check.py --top 25
```

**Why this one.** EDGAR is the only **validated** thing in the estate:
n=1,926, IWM-adjusted, monotonic across two credit regimes and two
benchmarks. Everything else is a hypothesis. The finding implies a
**short**, and `INFRASTRUCTURE.md §8` has said the whole time:

> *No borrow confirmation. `shorts.py` infers borrow difficulty from
> short interest and volume; only a broker locate confirms it.*

That check has never been run. It is one API call per ticker against an
Alpaca account you already have. It is **binary** and it gates the only
asset in the building.

| outcome | what it means |
|---|---|
| forced names are shortable | the liquidity study was measuring the wrong constraint — re-open the trade |
| not shortable / not ETB | the short thesis is closed. Stop funding it. Pivot EDGAR to information, not execution |

Either answer is worth more than another backtest. **You cannot lose this
test** — it returns a decision regardless of which way it lands. Nothing
else in the queue has that property.

---

## 2. The queue behind it

Ranked by **cost to kill**, not by expected profit. Your estate is
over-built and under-killed; throughput is the constraint.

| # | test | cost | kills what | status |
|---|---|---|---|---|
| 1 | **borrow check on forced names** | 25 API calls | the EDGAR short thesis | **built, unrun** |
| 2 | **adjusted-close contamination** on the overnight overlay | 1 evening | your highest-priority untested failure mode | unbuilt |
| 3 | **term structure of yield** | 1 snapshot | the Kalshi carry angle | built (`settlement/carry/termstructure.py`) |
| 4 | **ladder incoherence, ILLIQUID rungs** | 1 snapshot | re-opens a "zero found" result | needs filter removed |
| 5 | **Brier by price bucket** | 1 afternoon, data already collected | tells you where your model is actually good | unbuilt |
| 6 | **MTF continuation, leakage-instrumented** | 1 run | re-confirms an existing kill, cheaply | built (`mtf/`) |
| 7 | **cross-venue resolution-RULE mismatch** | reading, per pair | scanner matches titles, not rules | unbuilt |
| 8 | **point-in-time XBRL** | weeks | the 2022 EDGAR window | expensive, defer |
| 9 | **vision gate** (§2 of the video brief) | 5 frames | half the video-memory build | blocked on qwen3-vl install |

**Rule:** nothing below line 1 gets touched until line 1 has an answer.
Failure at any gate blocks everything downstream of it — your own
sequencing rule.

---

## 3. "Eat off the land" — the straight version

I can't hand you a profitable system, and nothing in this repo is one
yet. What I can rank is where revenue is *closest*:

**Closest.** EDGAR as an information product rather than a trade. The
liquidity study killed the *execution*, not the *finding*. A validated,
IWM-adjusted, regime-stable signal on filing-driven distress plus a
working API and a daily n8n pipeline is a sellable artifact whether or
not you can borrow the shares. This path does not require the signal to
survive execution costs, which is the exact constraint that has blocked
it for months.

**Second.** The carry/structural angles in `settlement/carry/`. Small,
capital-gated, but measurable — six of eight angles are reachable at your
volume (`angles.py`), which is six more than the forecast angle.

**Distant.** Anything price-based. 15+ kills. The MTF harness exists to
confirm that fast, not to revive it.

**Not a path.** Scaling the 98¢ Polymarket carry. At $50k, correctly
sized, that is $277/cycle and ~$7.2k/yr *if you never lose once*. Its
math works because of a $3M balance sheet, not because of the trade.

---

## 4. What consolidation does and does not change

`ops/migrate.sh` **copies** into `~/trading-lab/` and leaves symlinks at
the old paths. Nothing breaks on the first run — that is deliberate.

Things that WILL break if you move without symlinks:

- `~/EDGAR/.venv` — absolute paths inside; recreate, don't move
- `.env` files — three keys, not committed, easy to lose in a move
- n8n HTTP nodes — they call `host.docker.internal:8000`, which is
  path-independent, so they survive. **But** the API must be started
  from the new directory.
- `data/db/states.json` — the expensive event-study cache. Deleting it
  throws away hours. `migrate.sh` copies it and verifies the byte count
  before touching anything.

Run `ops/status.py` after migrating. It reports what is present,
runnable, and blocked, so this map self-verifies instead of drifting.
