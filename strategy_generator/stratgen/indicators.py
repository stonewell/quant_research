"""RSI (Wilder), SMA, ATR, and ROC -- the small, deliberately restricted set
of long-established indicator primitives used to build strategy templates.
Re-exported from the shared `common/indicators.py` module.

Research grounding: unconstrained/highly flexible primitive sets in
automated rule search are documented as especially prone to data-snooping
bias (Allen & Karjalainen 1999). Their own mitigation -- restricting the
primitive set to a small number of simple, economically-motivated
constructs rather than an arbitrary function set -- is deliberately followed
here instead of building a full genetic-programming symbolic search.

`roc` (rate of change / trailing return) is the primitive behind the
time-series/absolute-momentum construct (Moskowitz, Ooi & Pedersen 2012;
Faber 2007) used by `AbsoluteMomentumTemplate`. MACD is intentionally NOT
re-exported here even though `common/indicators.py` implements it: its
stand-alone profitability is weak once data-snooping is corrected for (Park
& Irwin 2007), so no template is built on it and, per this workspace's
"re-export only the subset you actually use" convention, it stays unexposed.
"""

from common.indicators import atr, atr_pct, realized_vol, roc, rsi, sma
