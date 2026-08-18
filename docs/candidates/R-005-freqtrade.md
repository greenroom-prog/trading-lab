R-005 Freqtrade
Licence: GPL-3.0 (VERIFIED 2026-08-18). Copyleft. We distribute nothing, so
no restriction on us. Confirms: wrap it, never merge its code into ours.
Isolation: VERIFIED. strategy/interface.py imports only timeframe helpers
from freqtrade.exchange - time math, no network. A strategy is a function
over a dataframe. Wrappable with no exchange access.
