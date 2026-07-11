"""RSI (two calculation methods) and supporting moving averages.

Wilder's RSI recursively smooths average gain/loss with weight 1/n on the
newest bar (equivalent to a (2n-1)-period EMA). Cutler's/"plain" RSI instead
uses a simple moving average of gains/losses, trading Wilder's recency
weighting for a result that doesn't depend on how far back the calculation
window starts. Both are documented, real variants -- which one a backtest
uses is a genuine, reproducibility-relevant choice, not just an implementation
detail, so it's exposed as a config option rather than hard-coded.

`rsi_wilder`, `rsi_cutler`, `cumulative_rsi`, and `sma` are re-exported from
the shared `common/indicators.py` module; the `rsi(..., method=...)`
dispatcher is specific to this project.
"""

from common.indicators import cumulative_rsi, rsi_cutler, rsi_wilder, sma


def rsi(close, period: int, method: str = "wilder"):
    if method == "wilder":
        return rsi_wilder(close, period)
    if method == "cutler":
        return rsi_cutler(close, period)
    raise ValueError(f"Unknown RSI method: {method!r} (expected 'wilder' or 'cutler')")
