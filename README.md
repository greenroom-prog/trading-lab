# Trading Lab

R&D stage. Nothing is adopted until it is proven.

## What this is
A candidate factory. Ideas and repos get tested, most get killed, findings get written down.

Rule: **Research proposes. The backtester measures. Out-of-sample survival decides.**

## Status
- Stage: R&D — evaluating tools
- Candidates evaluated: 1 of 9
- Code written: none
- Money at risk: none

## Findings

### R-002 NautilusTrader — engine candidate
Verified 2026-08-18 by reading the source.

| Question | Answer |
|---|---|
| Licence | LGPL-3.0. Usable as a library. Do not modify its core. |
| Same code backtest and live? | Yes. EMACross imported by both examples/backtest and examples/live. |
| Fees | Explicit maker_fee / taker_fee per instrument. We set the values. |
| Slippage | ProbabilisticFillModel with random_seed. Configurable and reproducible. |

No blockers found. Prior of "leaning adopt" survives. Adoption not decided.

### Not yet evaluated
CCXT, VectorBT, Backtrader, Freqtrade, Lumibot, Hummingbot, TradingAgents, Polymarket API

## Structure
Repos being read live in ~/research, outside this project.

## Rules
1. A README, a blog post, or an AI answer is not a check. Only running something is.
2. Every claim carries a label: verified / assumed / guessed.
3. Kill criteria are written before a backtest runs, never after.
4. .env is never committed.
5. Nothing goes live until it survives data it has never seen.
