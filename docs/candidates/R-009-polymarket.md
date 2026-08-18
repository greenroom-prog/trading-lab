R-009 Polymarket
VERIFIED 2026-08-18: py-clob-client exposes get_price and get_prices only.
No price history method in this client.
CONSEQUENCE: live-forward-only signal source. Cannot be backtested unless
history is found elsewhere (gamma/data API - unchecked).
Also note: Lumibot already ships a polymarket broker.
VERDICT: defer. Not a venue for us. Possible Ring 0 policy-expectation
feature later, and only if point-in-time history exists.
