"""Named indicator "features" (indicator name + lookback) shared by
`pipeline/pattern_mining/pmine/pattern_mining.py`'s turning-point pattern mining and
`common.allocation_templates.PatternBasedAllocationTemplate`'s live trading
signal.

The SAME `compute_feature` dispatch backs both call sites deliberately: a
mined pattern's threshold is tested during mining, and later evaluated
during live trading, against the IDENTICAL computation -- there must be no
drift between "what was found significant" and "what the strategy actually
trades on". This is a shared module specifically so `common/`
(project-agnostic) and `pipeline/pattern_mining/` (the mining orchestrator) both
import one definition rather than keeping two that could diverge.

Never used by any of the 9 static `AllocationTemplate` classes -- those use
a small, fixed primitive set directly (see
`pipeline/strategy_generator/stratgen/indicators.py`'s own docstring on why that set
is deliberately restricted, per Allen & Karjalainen 1999). This module's
broader "popular indicators" menu is a disclosed exception used only for
pattern mining, which guards against data-snooping a different way (a
Bonferroni-corrected significance test across the whole menu, plus the
standard Equivalent Random Search every template must still clear).
"""

import pandas as pd

from common.indicators import (
    adx,
    atr_pct,
    bollinger_bands,
    cci,
    macd,
    roc,
    rsi,
    sma,
    stochastic_oscillator,
    williams_r,
)

# (feature_name, lookback) -- lookback is a plain int for single-parameter
# indicators, or a (fast, slow, signal) tuple for macd_hist.
DEFAULT_FEATURE_MENU = [
    ("rsi", 5), ("rsi", 14), ("rsi", 21),
    ("sma_rel", 10), ("sma_rel", 20), ("sma_rel", 50), ("sma_rel", 200),
    ("roc", 21), ("roc", 63), ("roc", 126),
    ("atr_pct", 14),
    ("adx", 14),
    ("bb_pctb", 20),
    ("stoch_k", 14),
    ("macd_hist", (12, 26, 9)),
    ("cci", 20),
    ("williams_r", 14),
]


def feature_label(name: str, lookback) -> str:
    """Column-name-safe label for a (name, lookback) feature, e.g.
    'rsi_14' or 'macd_hist_12_26_9'."""
    lb_str = "_".join(str(x) for x in lookback) if isinstance(lookback, (tuple, list)) else str(lookback)
    return f"{name}_{lb_str}"


def compute_feature(curve: pd.DataFrame, name: str, lookback) -> pd.Series:
    """Computes ONE named feature series over a whole OHLC curve. `curve`
    must have a Close column (and High/Low for the indicators that need
    them). Backward-looking only, like every function in
    `common/indicators.py` -- a value at date T never uses data after T."""
    close = curve["Close"]
    if name == "rsi":
        return rsi(close, lookback)
    if name == "sma_rel":
        return close / sma(close, lookback) - 1.0
    if name == "roc":
        return roc(close, lookback)
    if name == "atr_pct":
        return atr_pct(curve, lookback)
    if name == "adx":
        return adx(curve, lookback)
    if name == "bb_pctb":
        return bollinger_bands(close, lookback)["pctb"]
    if name == "stoch_k":
        return stochastic_oscillator(curve, lookback)["k"]
    if name == "macd_hist":
        fast, slow, signal = lookback
        return macd(close, fast, slow, signal)["hist"]
    if name == "cci":
        return cci(curve, lookback)
    if name == "williams_r":
        return williams_r(curve, lookback)
    raise ValueError(f"Unknown feature name '{name}'")


def longest_lookback(feature_menu) -> int:
    """Longest scalar lookback across a feature menu -- used as the default
    exclusion-buffer width around real turning points in
    `pattern_mining.mine_indicator_patterns`."""
    lookbacks = []
    for _, lb in feature_menu:
        if isinstance(lb, (tuple, list)):
            lookbacks.extend(lb)
        else:
            lookbacks.append(lb)
    return max(lookbacks) if lookbacks else 0
