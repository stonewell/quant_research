"""Pure buy/sell rule evaluation over a CURRENT snapshot -- no I/O, no BNN
fitting. Mirrors `fundamental_screener/fscreen/rules.py`'s shape exactly: a
confidence gate plus an expected-return-vs-benchmark hurdle, with sell always
taking precedence over buy so a symbol never lands on both lists.

Used by `run_bnn_forecaster.py`'s CLI report, which evaluates one row per
symbol at the CURRENT date. `bnnf/strategy.py`'s backtester-facing strategy
needs the SAME logic applied per-bar across an entire time series instead of
once across symbols, so it reimplements the equivalent comparisons directly
as vectorized pandas operations rather than reusing these DataFrame-of-symbols
functions -- see that module's own docstring.
"""

import pandas as pd

from .config import ForecasterConfig


def expected_return(row) -> float:
    """The BNN's own median-forecast annualized return -- already computed
    by `fscreen.forecasting.fit_forecast`, so this is a direct passthrough
    (kept as its own function for the same API shape as
    `fundamental_screener/fscreen/rules.py`'s `expected_return`, which has
    real formula logic)."""
    return row["forecast_return"]


def confident(row, cfg: ForecasterConfig) -> bool:
    """A NaN ci_width (fit failed / degenerate) fails the gate rather than
    silently passing it."""
    if pd.isna(row["ci_width"]):
        return False
    return row["ci_width"] <= cfg.max_ci_width


def evaluate_buy_sell(forecast_df: pd.DataFrame, benchmark_return: float, cfg: ForecasterConfig) -> pd.DataFrame:
    """Adds `confident`, `expected_return`, `sell_flag`, `buy_flag` columns to
    `forecast_df` (indexed by symbol, with `forecast_return`/`ci_width`
    columns -- one row per symbol's CURRENT forecast).

    OVERLAP RESOLUTION ("a symbol in both lists"): sell always takes
    precedence over buy -- same rule, same rationale, as
    `fundamental_screener/fscreen/rules.py`'s `evaluate_buy_sell`: a
    capital-preservation trigger (an unconfident forecast, or one that no
    longer beats the benchmark) always outranks a return signal.
    """
    df = forecast_df.copy()
    df["confident"] = df.apply(lambda row: confident(row, cfg), axis=1)
    df["expected_return"] = df.apply(expected_return, axis=1)

    df["sell_flag"] = (~df["confident"]) | (df["expected_return"] < benchmark_return)
    df["buy_flag"] = df["confident"] & (df["expected_return"] >= cfg.required_return) & (~df["sell_flag"])
    return df


def rank_buy_sell(evaluated_df: pd.DataFrame, top_n: int) -> tuple:
    """Returns `(top_buy_df, top_sell_df)`: `top_buy` is `buy_flag` rows
    sorted by `expected_return` descending (best first); `top_sell` is
    `sell_flag` rows sorted by `expected_return` ascending (worst first)."""
    top_buy = evaluated_df[evaluated_df["buy_flag"]].sort_values("expected_return", ascending=False).head(top_n)
    top_sell = evaluated_df[evaluated_df["sell_flag"]].sort_values("expected_return", ascending=True).head(top_n)
    return top_buy, top_sell
