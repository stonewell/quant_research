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

import numpy as np
import pandas as pd
from typing import Any

from common.data import FundamentalMetrics
from .config import ScreenerConfig
from .valuation_models import (
    classify_industry_and_terminal_pe,
    evaluate_snowball_valuation,
    evaluate_sell_trigger,
)


def expected_return(row) -> float:
    """The document's own Model 2 formula, verbatim: predicted annualized
    total return ~= trailing earnings growth + dividend yield."""
    growth = row.get("earnings_growth", row.get("net_profit_5yr_cagr", 0.0))
    div_yield = row.get("dividend_yield", 0.0)
    return float(growth + div_yield)


def quality_ok(row, cfg: ScreenerConfig) -> bool:
    """Checks all 5 hard Snowball quality gates plus blacklist:
    1. 5% <= earnings_growth <= 12% (min_earnings_growth <= growth <= max_earnings_growth)
    2. roe >= cfg.min_roe
    3. cfo_to_net_profit >= cfg.min_cfo_to_profit (or cfo >= 0.8 * net_profit)
    4. debt_to_assets <= cfg.max_debt_to_assets (or debt_to_equity <= cfg.max_debt_to_equity)
    5. dividend_payout_ratio in [min_dividend_payout, max_dividend_payout] (or dividend_yield > cfg.min_dividend_yield)
    6. Not blacklisted
    Any missing mandatory field returns False.
    """
    # 6. Blacklist check
    if bool(row.get("is_blacklisted", False)):
        return False
    if cfg.enable_industry_blacklist:
        ind = str(row.get("industry_name", ""))
        sym = str(row.get("symbol", getattr(row, "name", "")))
        if ind or sym:
            _, _, is_bl, _ = classify_industry_and_terminal_pe(ind, sym)
            if is_bl:
                return False

    # 2. ROE gate (mandatory)
    roe = row.get("roe")
    if roe is None or pd.isna(roe) or roe < cfg.min_roe:
        return False

    # 1. Earnings growth gate (mandatory)
    growth = row.get("earnings_growth")
    if growth is None or pd.isna(growth):
        growth = row.get("net_profit_5yr_cagr")
    if growth is None or pd.isna(growth):
        return False
    if growth < cfg.min_earnings_growth:
        return False
    if cfg.max_earnings_growth is not None and growth > cfg.max_earnings_growth:
        return False

    # 4. Solvency / Leverage gate (mandatory: must have debt_to_assets or debt_to_equity)
    has_d_assets = "debt_to_assets" in row and not pd.isna(row["debt_to_assets"])
    has_d_equity = "debt_to_equity" in row and not pd.isna(row["debt_to_equity"])
    if not (has_d_assets or has_d_equity):
        return False
    if has_d_assets and row["debt_to_assets"] > cfg.max_debt_to_assets:
        return False
    elif not has_d_assets and has_d_equity and row["debt_to_equity"] > cfg.max_debt_to_equity:
        return False

    # 5. Dividend gate (mandatory: must have dividend_payout_ratio or dividend_yield)
    has_payout = "dividend_payout_ratio" in row and not pd.isna(row["dividend_payout_ratio"])
    has_yield = "dividend_yield" in row and not pd.isna(row["dividend_yield"])
    if not (has_payout or has_yield):
        return False
    if has_payout:
        payout = row["dividend_payout_ratio"]
        if payout < cfg.min_dividend_payout or payout > cfg.max_dividend_payout:
            return False
    elif has_yield:
        if row["dividend_yield"] <= cfg.min_dividend_yield:
            return False

    # 3. Cash flow quality gate (enforced strictly if cfo_to_net_profit or cfo is present)
    if "cfo_to_net_profit" in row:
        cfo_ratio = row["cfo_to_net_profit"]
        if pd.isna(cfo_ratio) or cfo_ratio < cfg.min_cfo_to_profit:
            return False
    elif "cfo" in row and "net_profit" in row:
        cfo = row["cfo"]
        np_val = row["net_profit"]
        if pd.isna(cfo) or pd.isna(np_val) or np_val <= 0 or (cfo / np_val) < cfg.min_cfo_to_profit:
            return False

    return True


def _row_to_fundamental_metrics(sym: Any, row: pd.Series, close_price: float = float("nan")) -> FundamentalMetrics:
    symbol_str = str(row.get("symbol", sym))
    np_val = row.get("net_profit", 100.0)
    growth = row.get("earnings_growth", row.get("net_profit_5yr_cagr", 0.08))
    mcap = row.get("market_cap", 1000.0)
    shares = row.get("total_shares", 10.0)
    cp = row.get("close_price", close_price)
    cp_val = float(cp) if cp is not None and not pd.isna(cp) else float("nan")
    return FundamentalMetrics(
        symbol=symbol_str,
        report_year=int(row.get("report_year", 2024)),
        stat_date=str(row.get("stat_date", "2024-12-31")),
        roe=float(row.get("roe", float("nan"))),
        net_profit=float(np_val),
        net_profit_5yr_cagr=float(growth),
        cfo=float(row.get("cfo", float("nan"))),
        cfo_to_net_profit=float(row.get("cfo_to_net_profit", float("nan"))),
        total_assets=float(row.get("total_assets", float("nan"))),
        total_liabilities=float(row.get("total_liabilities", float("nan"))),
        debt_to_assets=float(row.get("debt_to_assets", float("nan"))),
        debt_to_equity=float(row.get("debt_to_equity", float("nan"))),
        interest_bearing_debt=float(row.get("interest_bearing_debt", float("nan"))),
        dividend_yield=float(row.get("dividend_yield", float("nan"))),
        dividend_payout_ratio=float(row.get("dividend_payout_ratio", float("nan"))),
        pe_ttm=float(row.get("pe_ttm", float("nan"))),
        pb=float(row.get("pb", float("nan"))),
        total_shares=float(shares),
        market_cap=float(mcap),
        close_price=cp_val,
        industry_name=str(row.get("industry_name", "Unknown")),
        industry_tier=str(row.get("industry_tier", "tier3_manufacturing")),
        terminal_pe=float(row.get("terminal_pe", 10.0)),
        is_blacklisted=bool(row.get("is_blacklisted", False)),
        blacklist_reason=row.get("blacklist_reason", None),
    )


def evaluate_buy_sell(fundamentals_df: pd.DataFrame, benchmark_return: float, cfg: ScreenerConfig) -> pd.DataFrame:
    """Adds `quality_ok`, `expected_return`, `sell_flag`, `buy_flag` columns
    as well as Snowball valuation metrics (`model1_mv`, `model2_mv`, `fair_mv`,
    `buy_mv`, `buy_price`, `mos_ratio`, `sell_reason`) to `fundamentals_df`.

    OVERLAP RESOLUTION: Sell always overrides Buy.
    """
    df = fundamentals_df.copy()
    df["quality_ok"] = df.apply(lambda row: quality_ok(row, cfg), axis=1)
    df["expected_return"] = df.apply(expected_return, axis=1)

    snowball_results = []
    sell_trigger_flags = []
    sell_reasons = []

    for sym, row in df.iterrows():
        metrics = _row_to_fundamental_metrics(sym, row)
        val = evaluate_snowball_valuation(metrics, cfg)
        snowball_results.append(val)

        exp_ret = row["expected_return"] if "expected_return" in row and not pd.isna(row["expected_return"]) else val["expected_return"]
        should_sell, reason = evaluate_sell_trigger(metrics, exp_ret, benchmark_return, cfg)
        sell_trigger_flags.append(should_sell)
        sell_reasons.append(reason)

    val_df = pd.DataFrame(snowball_results, index=df.index)
    val_cols = [
        "model1_mv", "model2_mv", "fair_mv", "buy_mv", "buy_price",
        "mos_ratio", "terminal_pe", "industry_tier", "is_blacklisted", "blacklist_reason"
    ]
    for col in val_cols:
        if col in val_df.columns:
            df[col] = val_df[col]

    df["sell_reason"] = sell_reasons
    df["sell_flag"] = (~df["quality_ok"]) | (df["expected_return"] < benchmark_return) | pd.Series(sell_trigger_flags, index=df.index)
    if cfg.enable_snowball_valuation:
        df["buy_flag"] = df["quality_ok"] & (df["expected_return"] >= cfg.required_return) & (df["mos_ratio"] > 0) & (~df["sell_flag"])
    else:
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
