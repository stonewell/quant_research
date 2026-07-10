"""RSI (Wilder), SMA, and ATR -- the small, deliberately restricted set of
long-established indicator primitives used to build strategy templates.
Re-exported from the shared `common/indicators.py` module.

Research grounding: unconstrained/highly flexible primitive sets in
automated rule search are documented as especially prone to data-snooping
bias (Allen & Karjalainen 1999). Their own mitigation -- restricting the
primitive set to a small number of simple, economically-motivated
constructs rather than an arbitrary function set -- is deliberately followed
here instead of building a full genetic-programming symbolic search.
"""

from common.indicators import atr, atr_pct, rsi, sma
