# MAP — every component, what it is, what blocks it

Sourced from `EDGAR/README.md`, `INFRASTRUCTURE.md`,
`VIDEO_MEMORY_BRIEF.md` and prior sessions. Anything marked *(unverified)*
is inferred and should be confirmed against the machine before it is
relied on. `ops/status.py` checks the file-level claims automatically.

Status vocabulary — deliberately non-directional:

`VALIDATED` survived a real test · `BUILT` runs, untested ·
`PARTIAL` runs, known gap · `BLOCKED` cannot proceed, reason stated ·
`KILLED` dead, reason in `KILLS.md` · `UNBUILT` designed only

---

## A. EDGAR — SEC filing condition engine

The only validated finding in the estate.

| component | status | note |
|---|---|---|
| `edgar/sec.py` | BUILT | rate-limited ~8 req/s. **Never parallelise** — SEC blocks silently |
| `edgar/filings.py` | BUILT | 8-K item classification. 8-K is a container, not an event type |
| `edgar/facts.py` | BUILT | XBRL facts |
| `edgar/state.py` | VALIDATED | the core judgement. cash, burn, runway, coverage, dilution |
| `edgar/insiders.py` | PARTIAL | Form 4 open-market buys. Untested at scale |
| `edgar/shorts.py` | PARTIAL | **infers** borrow from SI + volume. Never confirmed against a broker |
| `edgar/macro.py` | BUILT | FRED. HY spread gate — same balance sheet, opposite outcome by regime |
| `edgar/universe.py` | BUILT | mcap + liquidity filter |
| `api/server.py` | BUILT | FastAPI :8000, 6 endpoints, every response carries `disclaimer` |
| `scripts/healthcheck.py` | BUILT | 14 deterministic assertions. Exit 0/1/2 |
| `scripts/event_study.py` | VALIDATED | 26,287 8-Ks, two windows, no ticker pre-selection |
| `scripts/market_adjust.py` | VALIDATED | strips benchmark. The test that separates signal from beta tilt |
| `scripts/timing_study.py` | VALIDATED | 2–6% of 5d move in minute one; 63–71% by 1 day |

**The finding.** 20-day median return, IWM-adjusted, monotonic in all
eight rankings across both credit regimes:

```
condition          2022 (tight)   2026 (open)
forced                 -2.75%        -7.63%
pressured              -2.59%        -3.64%
funded                 -1.04%        -2.03%
cash generative        +0.61%        -1.06%
```

**Open blockers, in priority order**

1. **BORROW UNCONFIRMED.** Direction is short; small-cap borrow unknown.
   → `ops/borrow_check.py`, tonight. Cheapest test of the best asset.
2. **LIQUIDITY.** Signal lives almost entirely in untradeable names; in
   the cleanly tradeable subset it collapses substantially. *Primary
   unresolved challenge.* Note: this kills the trade, not the finding.
3. **POINT-IN-TIME XBRL.** `data.sec.gov` serves CURRENT values for PAST
   periods. Correct for "condition today," look-ahead for history. The
   2022 window is contaminated by restatements. Fixing = parsing as-filed
   by accession. Expensive; defer until 1 and 2 resolve.
4. `borrowing_capacity` enum vocabulary in `state.py` — still an open
   assumption in the n8n classifier. One `grep`.
5. Beyond 20 days unexamined. Effect strengthens with horizon in both
   windows and nobody has looked further.

**n8n** — health monitor (daily 06:00, `GET /health`) and filing watch
(weekdays 07:00 → scored scan → enrich → classify → Telegram digest).
`splitInBatches` size **1**, `retryOnFail` **off**. A retry storm is the
likeliest path to an SEC breach, and a breach is silent.

---

## B. settlement — Kalshi / Polymarket

| component | status | note |
|---|---|---|
| BRTI settlement model | PARTIAL | Brier 0.0518 vs market 0.0568. **Needs 20+ settlements. Not a confirmed edge** |
| live WebSocket stream | BUILT | header-name bug already found and fixed |
| final-window pricer | BUILT | — |
| ladder coherence checker | BUILT | found **zero** arbitrage across all **liquid** ladders |
| cross-venue scanner | BUILT | Poly vs Kalshi, polarity conflict detection. Matches on title, **not resolution rules** |
| `carry/economics.py` | BUILT | 2 independent derivations per number |
| `carry/ruin.py` | BUILT | MC + exact lattice DP. v1 bug in `KILLS.md` |
| `carry/book.py` | BUILT | depth-constrained VWAP |
| `carry/termstructure.py` | BUILT | known-cases pass. **The lead structural test** |
| `carry/angles.py` | BUILT | ranks angles by cost-to-know |
| `carry/scan.py` | **BLOCKED** | HTTP layer never tested — no route from the sandbox. `--probe` first |

**The reframe.** Forecast calibration at 98.2¢ needs ~12,605 settlements
to measure; you have ~20 and the CI is 11.7× wider than the edge. That
angle is closed. Structural angles need one snapshot. Six of eight
angles in `angles.py` are reachable; the two that are not are the two the
lab has been working on.

**Two existing results the reframe re-opens**

- Ladder checker found zero across **liquid** ladders. Liquid is where
  the competition is. *The filter may be the finding.* Re-run unfiltered.
- Cross-venue scanner matches title + polarity. Two markets with the same
  title and different resolution **sources** are not the same market.

**Blocking for everything here:** venue fee schedules. No default exists
anywhere in the code, deliberately — at a 1.8% gross return a guessed fee
is the difference between a trade and a donation.

---

## C. mtf — multi-timeframe trend harness

| component | status |
|---|---|
| `mtf/mtf.py` | BUILT — 1h EMA+volume trend, 5m entries, next-bar-open fills |
| `mtf/test_mtf.py` | BUILT — 7 known-case assertions, ALL PASS |
| `mtf/run.py` | BUILT — yfinance / ccxt / csv |

**This is a gauntlet, not a signal generator.** An MTF continuation
strategy on GC=F/BTCUSD/US30/SPY is already killed. The harness runs
clean and leaky alignment and reports the delta: on a pure random walk it
manufactures **+0.48 bps** of fake edge from data with no edge in it.

**Blocking:** yfinance caps 5m bars at 60 days (~4,700 bars, one regime).
`run.py` refuses a verdict under `--min-trades`. Use ccxt for crypto.

---

## D. strategies — the graveyard

15+ killed price-based strategies. Per `INFRASTRUCTURE.md §7`, the kill
reasons are **the most reusable artifact either repo produced.** See
`KILLS.md`.

| item | status |
|---|---|
| SPY overnight gap | KILLED — edge existed only at a fill price unavailable to retail |
| MTF continuation (GC=F, BTCUSD, US30, SPY) | KILLED on initial run |
| predecessor 1834% vs 743% B&H | KILLED — filled at the closing price. Passed OOS, robustness, and permutation first |
| overnight return premium overlay (1.0/1.5/0.5x) | **UNDER REVIEW** — adjusted-close contamination of the overnight/intraday decomposition is the highest-priority untested failure mode |

---

## E. video-memory

| component | status |
|---|---|
| design + schema | BUILT — separate claims/results files, sha256 ID ledger |
| `gate_test.py` | BUILT, **UNRUN** |
| vision model | **BLOCKED** — qwen3-vl:32b not installed |

**§2 of the brief is a hard gate.** Do not build the vision layer until
five real frames have been run and the result recorded in the repo. If
the model cannot read a ticker, `chart_ticker` fills with confident wrong
answers, and wrong data is worse than absent data — the agent downstream
cannot tell the difference.

Cheap fallback that works regardless: speakers say the ticker out loud.
Audio wins on conflict; record both.

---

## F. infrastructure

| service | where | port |
|---|---|---|
| EDGAR API | host | 8000 |
| n8n | container `n8n` | 5678 |
| dashboard | host | 3000 — **not built** |

Ollama: `qwen3-next-128k`, `qwen3-32b-40k`, `nomic-embed-text`.
`qwen3-vl:32b` planned.

APIs configured: FRED, Polygon/Massive (5/min free tier — minute bars
only), Alpaca **paper**, Kalshi (live, small balance), Coinbase,
Polymarket.

Data: SEC EDGAR, CF Benchmarks BRTI, NWS CLI / IEM AFOS, AAA gas, EIA,
BLS, FRED, IMF PortWatch.

---

## G. What is NOT built and must not be faked

Verbatim from `INFRASTRUCTURE.md §8`, still true:

- **No live trading.** One Alpaca *paper* position from a strategy that
  was subsequently killed. Historical.
- **No validated strategy.** One validated *research finding*. Different
  thing.
- **No borrow confirmation.**
- **No point-in-time XBRL.**
- **No position sizing, no cost model, no entry timing.**

Add:

- **No confirmed venue fee schedules.**
- **No vision gate result.**
- **No tested HTTP layer** in `carry/scan.py`.
