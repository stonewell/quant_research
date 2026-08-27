"""Pure buy/sell rule evaluation -- no I/O, no network. Given a per-symbol
fundamentals table (see `fscreen/fundamentals.py`) and a benchmark's own
trailing return, decides which symbols pass the buy rule, which pass the
sell rule, and resolves the case where a symbol would otherwise qualify for
both.

Grounding: `docs/snowball_strategy.txt`'s conservative valuation framework.
`expected_return` is that document's own "Model 2" growth+dividend formula,
applied close to verbatim; the quality gate covers the document's ROE/
dividend/leverage/growth screen using real fundamentals (see
`common/data.py`'s `YFinanceDataProvider.fetch_metadata` for exactly which
yfinance fields feed each one, and its unit-convention caveats).
"""

import pandas as pd

from .config import ScreenerConfig


def expected_return(row) -> float:
    """The document's own Model 2 formula, verbatim: predicted annualized
    total return ~= trailing earnings growth + dividend yield."""
    return row["earnings_growth"] + row["dividend_yield"]


def quality_ok(row, cfg: ScreenerConfig) -> bool:
    """The document's moat/high-ROE/dividend/growth screen, applied to real
    fundamentals. Any NaN input (missing/unavailable field) fails the gate
    rather than silently passing it -- a symbol this workspace can't verify
    the fundamentals for is not a "quality compounder" by this method's own
    standard, it's just unproven."""
    fields = (row["roe"], row["dividend_yield"], row["debt_to_equity"], row["earnings_growth"])
    if any(pd.isna(v) for v in fields):
        return False
    return (
        row["roe"] >= cfg.min_roe
        and row["dividend_yield"] > cfg.min_dividend_yield
        and row["debt_to_equity"] <= cfg.max_debt_to_equity
        and row["earnings_growth"] >= cfg.min_earnings_growth
    )


def evaluate_buy_sell(fundamentals_df: pd.DataFrame, benchmark_return: float, cfg: ScreenerConfig) -> pd.DataFrame:
    """Adds `quality_ok`, `expected_return`, `sell_flag`, `buy_flag` columns
    to `fundamentals_df` (indexed by symbol, with `roe`/`dividend_yield`/
    `earnings_growth`/`debt_to_equity` columns -- see `fscreen/fundamentals.py`).

    OVERLAP RESOLUTION ("a symbol in both lists"): sell always takes
    precedence over buy. `sell_flag` fires on EITHER a quality-gate failure
    OR an expected-return that no longer beats the benchmark; `buy_flag`
    additionally requires `not sell_flag`. This makes the two lists mutually
    exclusive by construction, and matches the source document's own
    conservatism: a capital-preservation trigger always outranks a return
    signal, so a symbol whose safety margin has broken down is never also
    presented as a buy candidate just because its raw numbers still look
    good on the return side.
    """
    df = fundamentals_df.copy()
    df["quality_ok"] = df.apply(lambda row: quality_ok(row, cfg), axis=1)
    df["expected_return"] = df.apply(expected_return, axis=1)

    df["sell_flag"] = (~df["quality_ok"]) | (df["expected_return"] < benchmark_return)
    df["buy_flag"] = df["quality_ok"] & (df["expected_return"] >= cfg.required_return) & (~df["sell_flag"])
    return df


def rank_buy_sell(evaluated_df: pd.DataFrame, top_n: int) -> tuple:
    """Returns `(top_buy_df, top_sell_df)`: `top_buy` is `buy_flag` rows
    sorted by `expected_return` descending (best first); `top_sell` is
    `sell_flag` rows sorted by `expected_return` ascending (worst first) --
    the symbols furthest below the bar are the most urgent sells."""
    top_buy = evaluated_df[evaluated_df["buy_flag"]].sort_values("expected_return", ascending=False).head(top_n)
    top_sell = evaluated_df[evaluated_df["sell_flag"]].sort_values("expected_return", ascending=True).head(top_n)
    return top_buy, top_sell
