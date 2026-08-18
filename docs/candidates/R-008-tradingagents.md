R-008 TradingAgents
Licence: Apache 2.0 (VERIFIED 2026-08-18).
VERIFIED: agents/trader/trader.py prompts an LLM for 'a specific
recommendation to buy, sell, or hold'. That is a decision, in prose,
non-reproducible - cannot be walk-forward tested.
VERDICT: never emits an Intent. Architectural rule, not a test result.
Usable half: analysts/ and researchers/ as feature extractors from text,
with model name and prompt version recorded in provenance.
