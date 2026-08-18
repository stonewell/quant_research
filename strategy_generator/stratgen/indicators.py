"""RSI (Wilder), SMA, ATR, and ROC -- the small, deliberately restricted set
of long-established indicator primitives used to build the 9 static
allocation templates in `common/allocation_templates.py`. Re-exported from
the shared `common/indicators.py` module.

Research grounding: unconstrained/highly flexible primitive sets in
automated rule search are documented as especially prone to data-snooping
bias (Allen & Karjalainen 1999). Their own mitigation -- restricting the
primitive set to a small number of simple, economically-motivated
constructs rather than an arbitrary function set -- is deliberately followed
here for the STATIC templates instead of building a full genetic-programming
symbolic search.

`roc` (rate of change / trailing return) is the primitive behind the
time-series/absolute-momentum construct (Moskowitz, Ooi & Pedersen 2012;
Faber 2007) used by `AbsoluteMomentumTemplate`.

`adx`, `macd`, `ema`, `bollinger_bands`, `stochastic_oscillator`, `cci`,
`williams_r`, and `obv` are re-exported ONLY for `pattern_mining.py`'s
turning-point feature menu, NOT for any static template -- that menu is a
deliberate, disclosed EXCEPTION to the "restrict the primitive set" rule
above: it needs a genuinely broad "popular indicators" menu to mine against,
and guards against the resulting data-snooping risk a different way (a
Bonferroni-corrected shuffle-null significance test across the whole menu,
plus the same ERS/backtested-Sharpe bar every template must clear) rather
than by keeping the menu small. See `pattern_mining.py`'s module docstring.
"""

from common.indicators import (
    adx,
    atr,
    atr_pct,
    bollinger_bands,
    cci,
    ema,
    macd,
    obv,
    realized_vol,
    roc,
    rsi,
    sma,
    stochastic_oscillator,
    williams_r,
)
