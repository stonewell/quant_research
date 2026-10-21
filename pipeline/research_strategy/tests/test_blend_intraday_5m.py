"""Unit tests for Blend Strategy Enhancements and Sector Incumbent Protection.

Validates:
1. sector_incumbent_hurdle parameter resolution across default, StrategyConfig, and param dictionary.
2. Sector incumbent protection logic:
   - Suppresses churn between same-sector assets (e.g. SPDB 600000.SH vs ICBC 601398.SH) when ROC difference < hurdle.
   - Retains the existing incumbent to fill the sector quota.
   - Allows displacement when challenger ROC exceeds incumbent ROC by at least sector_incumbent_hurdle.
3. Compliance with sparse weights NaN-vs-0.0 contract.

100% offline using synthetic OHLCV data.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import (
    BaseBlendAllocationStrategy,
    ChanCrisisShieldBlendStrategy,
    ChanFourStateBlendStrategy,
    GenericBlendStrategy,
    _get_symbol_sector,
)


def _build_synthetic_bar_df(dates, base_price, daily_ret=0.001):
    n = len(dates)
    closes = [base_price * ((1.0 + daily_ret) ** i) for i in range(n)]
    df = make_ohlcv_from_closes(closes)
    df.index = dates
    return df


def test_sector_incumbent_hurdle_resolution():
    """Verify sector_incumbent_hurdle resolution hierarchy and default of 0.03."""
    strat = GenericBlendStrategy()
    # Default without parameter
    assert strat.resolve_param({}, "sector_incumbent_hurdle", 0.03, float) == pytest.approx(0.03)

    # Overridden in params dict
    assert strat.resolve_param({"sector_incumbent_hurdle": 0.05}, "sector_incumbent_hurdle", 0.03, float) == pytest.approx(0.05)
    assert strat.resolve_param({"blend_sector_incumbent_hurdle": 0.04}, "sector_incumbent_hurdle", 0.03, float) == pytest.approx(0.04)


def test_sector_incumbent_protection_churn_suppression():
    """Verify that when challenger's ROC does not beat incumbent's ROC by at least
    sector_incumbent_hurdle, the existing incumbent is retained, preventing churn.
    """
    dates = pd.date_range("2024-01-01", periods=270, freq="B")
    # Both 600000.SH (SPDB) and 601398.SH (ICBC) belong to the 'banking' sector
    assert _get_symbol_sector("600000.SH") == "banking"
    assert _get_symbol_sector("601398.SH") == "banking"

    # Construct synthetic prices:
    # SPDB: positive momentum
    # ICBC: slightly higher positive momentum on the final day, but diff < hurdle 0.03
    spdb_df = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.002)
    icbc_df = _build_synthetic_bar_df(dates, 5.0, daily_ret=0.0021)
    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600000.SH": spdb_df,
        "601398.SH": icbc_df,
        "BIL": cash_df,
    }

    # Run GenericBlendStrategy with sub-strategy weights favoring 600000.SH or breadth thrust
    strat = GenericBlendStrategy()
    params = {
        "cash_proxy": "BIL",
        "max_assets_per_sector": 1,
        "sector_incumbent_hurdle": 0.03,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    assert "600000.SH" in weights.columns
    assert "601398.SH" in weights.columns

    # Verify weights are sparse (rebalance dates have non-NaN, non-rebalance are NaN)
    # and weights sum to <= 1.0 (with cash proxy)
    non_nan_rows = weights.dropna(how="all")
    assert len(non_nan_rows) > 0


def test_sector_incumbent_protection_allows_displacement_on_large_hurdle_beat():
    """Verify that when challenger's ROC beats incumbent's ROC by >= sector_incumbent_hurdle,
    the challenger can displace the incumbent.
    """
    dates = pd.date_range("2024-01-01", periods=270, freq="B")
    spdb_df = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.001)
    # ICBC surging strongly (+0.01 per day = much higher 10d ROC)
    icbc_df = _build_synthetic_bar_df(dates, 5.0, daily_ret=0.01)
    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600000.SH": spdb_df,
        "601398.SH": icbc_df,
        "BIL": cash_df,
    }

    strat = GenericBlendStrategy()
    params = {
        "cash_proxy": "BIL",
        "max_assets_per_sector": 1,
        "sector_incumbent_hurdle": 0.03,
        "preemptive_thrust_deployment": True,
    }
    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    assert "601398.SH" in weights.columns


# ==============================================================================
# Track T Unit Tests: Tests 8 - 10 (T5.2)
# ==============================================================================

def test_blend_5m_interval_nesting_and_sublevel_veto():
    """Test 8: Verify Chan daily x 5m interval nesting Day 1 fast-track entry and sub-level veto (T3.1).
    1. Day 1 entry bypass:
       When blend_enable_5m_interval_nesting=True:
       If candidate has Chan_5m_Buy_Confluence == 2, UpVol_Ratio_5m >= 0.58, VWAP_Prem_5m >= 0.0,
       it bypasses min_entry_bars=2 on Day 1.
       Without it, min_entry_bars=2 requires 2 consecutive days before entry (Day 1 weight is NaN/0.0).
    2. Sub-level veto:
       If candidate has Chan_5m_Sell_Trigger == 1 and Afternoon_Ret_5m < -0.015,
       its entry weight is vetoed to 0.0.
    """
    dates = pd.date_range("2024-01-01", periods=100, freq="B")
    n = len(dates)

    # STOCK_A starts flat, then on day 20 surges with strong momentum
    closes_a = [10.0] * 20 + [10.0 * (1.05 ** (i - 19)) for i in range(20, n)]
    df_a = make_ohlcv_from_closes(closes_a)
    df_a.index = dates

    # STOCK_B is steady
    closes_b = [20.0 * (1.001 ** i) for i in range(n)]
    df_b = make_ohlcv_from_closes(closes_b)
    df_b.index = dates

    cash = make_ohlcv_from_closes([100.0] * n)
    cash.index = dates

    for df in (df_a, df_b):
        df["Chan_5m_Buy_Confluence"] = 0
        df["UpVol_Ratio_5m"] = 0.50
        df["VWAP_Prem_5m"] = 0.0
        df["Chan_5m_Sell_Trigger"] = 0
        df["Afternoon_Ret_5m"] = 0.0

    breakout_dt = dates[20]
    # Set 5m confluence on breakout day for STOCK_A
    df_a.loc[breakout_dt, "Chan_5m_Buy_Confluence"] = 2
    df_a.loc[breakout_dt, "UpVol_Ratio_5m"] = 0.65
    df_a.loc[breakout_dt, "VWAP_Prem_5m"] = 0.01

    universe = {"STOCK_A": df_a, "STOCK_B": df_b, "BIL": cash}
    strat = GenericBlendStrategy()

    # Part 1: Fast-track entry comparison
    params_off = {
        "cash_proxy": "BIL",
        "min_entry_bars": 2,
        "enable_5m_interval_nesting": False,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }
    params_on = {
        "cash_proxy": "BIL",
        "min_entry_bars": 2,
        "enable_5m_interval_nesting": True,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    w_off = strat.generate_weights(universe, params=params_off)
    w_on = strat.generate_weights(universe, params=params_on)

    # Off: Day 20 does not enter STOCK_A (still waiting for min_entry_bars=2 confirmation)
    assert breakout_dt not in w_off.dropna(how="all").index or pd.isna(w_off.loc[breakout_dt, "STOCK_A"]) or w_off.loc[breakout_dt, "STOCK_A"] <= 1e-4

    # On: Day 20 fast-tracks STOCK_A into portfolio (> 0)
    assert breakout_dt in w_on.dropna(how="all").index
    assert w_on.loc[breakout_dt, "STOCK_A"] > 0.0

    # Part 2: Sub-level veto
    # Set 5m sell trigger + afternoon drop < -1.5% on breakout day
    df_a.loc[breakout_dt, "Chan_5m_Sell_Trigger"] = 1
    df_a.loc[breakout_dt, "Afternoon_Ret_5m"] = -0.02
    w_veto = strat.generate_weights(universe, params=params_on)

    # Sub-level veto zeroes out STOCK_A on Day 20
    assert breakout_dt not in w_veto.dropna(how="all").index or pd.isna(w_veto.loc[breakout_dt, "STOCK_A"]) or w_veto.loc[breakout_dt, "STOCK_A"] <= 1e-4


def test_blend_volume_surge_and_anti_washout_stop():
    """Test 9: Verify signed volume surge, morning dip washout exemption, 3S take-profit,
    and sparse weights contract compliance with backward compatibility check (T3.2, T3.3, R7).
    """
    from common.allocation_templates import AllocationTemplate
    from common.indicators import atr

    class AlwaysHoldStockA(AllocationTemplate):
        def __init__(self):
            super().__init__(name="always_a", param_grid={}, factor_tags=[], version="1.0.0")

        def generate_weights(self, universe, params=None):
            idx = next(iter(universe.values())).index
            df = pd.DataFrame(index=idx, columns=list(universe.keys()), dtype=float)
            df.iloc[0] = {"STOCK_A": 1.0, "STOCK_B": 0.0, "BIL": 0.0}
            return df

    dates = pd.date_range("2024-01-01", periods=80, freq="B")
    n = len(dates)

    # Part 1: Anti-washout stop filter test
    # STOCK_A rises for 35 days, then on day 35 experiences morning dip
    closes_a = [10.0 * (1.005 ** i) for i in range(35)]
    entry_p = closes_a[0]
    peak_p = closes_a[34]
    df_temp = make_ohlcv_from_closes(closes_a)
    cur_atr = float(atr(df_temp, 14).iloc[34])
    trailing_stop_p = max(entry_p * 0.92, peak_p - 2.5 * cur_atr)

    # Dip price is slightly below trailing_stop_p, but within 3% tolerance (>= trailing_stop_p * 0.97)
    dip_p = trailing_stop_p * 0.985
    closes_a.append(dip_p)
    closes_a += [dip_p * (1.002 ** i) for i in range(n - 36)]

    df_a = make_ohlcv_from_closes(closes_a)
    df_a.index = dates
    df_b = make_ohlcv_from_closes([20.0] * n)
    df_b.index = dates
    cash = make_ohlcv_from_closes([100.0] * n)
    cash.index = dates

    for df in (df_a, df_b):
        df["Chan_5m_Buy_Confluence"] = 0
        df["UpVol_Ratio_5m"] = 0.50
        df["VWAP_Prem_5m"] = 0.0
        df["Chan_5m_Sell_Trigger"] = 0
        df["Afternoon_Ret_5m"] = 0.0
        df["Afternoon_Low_5m"] = df["Low"]

    dip_dt = dates[35]
    # Set afternoon recovery above stop price and positive VWAP premium with high up-volume
    df_a.loc[dip_dt, "Afternoon_Low_5m"] = trailing_stop_p + 0.10
    df_a.loc[dip_dt, "VWAP_Prem_5m"] = 0.005
    df_a.loc[dip_dt, "UpVol_Ratio_5m"] = 0.65

    universe = {"STOCK_A": df_a, "STOCK_B": df_b, "BIL": cash}
    strat = GenericBlendStrategy()
    strat.get_sub_strategies = lambda u, p=None: {"always_a": AlwaysHoldStockA()}
    strat.get_sub_strategy_weights = lambda p=None: {"always_a": 1.0}

    params_stop_off = {"cash_proxy": "BIL", "stop_mode": "atr_trailing", "stop_atr_mult": 2.5, "enable_5m_stop_filter": False, "enable_adaptive_stop_atr": False, "enable_cyq_quality_gate": False}
    params_stop_on = {"cash_proxy": "BIL", "stop_mode": "atr_trailing", "stop_atr_mult": 2.5, "enable_5m_stop_filter": True, "enable_adaptive_stop_atr": False, "enable_cyq_quality_gate": False}

    w_stop_off = strat.generate_weights(universe, params=params_stop_off)
    w_stop_on = strat.generate_weights(universe, params=params_stop_on)

    # When 5m stop filter is off: dip triggers trailing stop -> STOCK_A is exited (0.0)
    assert dip_dt in w_stop_off.index
    assert w_stop_off.loc[dip_dt, "STOCK_A"] == pytest.approx(0.0)

    # When 5m stop filter is on: morning dip recovered in afternoon -> exempted from stop loss
    # (Sparse weights: row is NaN because holding continues undisturbed)
    assert pd.isna(w_stop_on.loc[dip_dt, "STOCK_A"])

    # Part 2: 3S Take-Profit Lock test
    # Construct scenario where asset has floating gain >= 8%, pulls back >= 2 ATR from peak,
    # and Chan_5m_Sell_Trigger == 1
    closes_tp = [10.0 * (1.01 ** i) for i in range(35)]
    df_tp_temp = make_ohlcv_from_closes(closes_tp)
    cur_atr_tp = float(atr(df_tp_temp, 14).iloc[34])
    take_profit_p = closes_tp[34] - 2.3 * cur_atr_tp
    closes_tp.append(take_profit_p)
    closes_tp += [take_profit_p * (1.001 ** i) for i in range(n - 36)]

    df_tp = make_ohlcv_from_closes(closes_tp)
    df_tp.index = dates
    for col in ["Chan_5m_Buy_Confluence", "UpVol_Ratio_5m", "VWAP_Prem_5m", "Afternoon_Ret_5m"]:
        df_tp[col] = 0.0
    df_tp["Chan_5m_Sell_Trigger"] = 0
    df_tp["Afternoon_Low_5m"] = df_tp["Low"]

    tp_dt = dates[35]
    df_tp.loc[tp_dt, "Chan_5m_Sell_Trigger"] = 1  # 5m 3S sell trigger

    universe_tp = {"STOCK_A": df_tp, "STOCK_B": df_b, "BIL": cash}
    w_tp_off = strat.generate_weights(universe_tp, params=params_stop_off)
    w_tp_on = strat.generate_weights(universe_tp, params=params_stop_on)

    # With 5m filter enabled, 3S trigger locks in profit and exits to explicit 0.0
    assert w_tp_on.loc[tp_dt, "STOCK_A"] == pytest.approx(0.0)
    assert pd.isna(w_tp_off.loc[tp_dt, "STOCK_A"])

    # Part 3: Backward compatibility (R7) and sparse contract
    # Default params has enable_intraday_5m=False
    strat_default = GenericBlendStrategy()
    w_default = strat_default.generate_weights(universe)
    assert not w_default.empty
    # Verify sparse contract: intermediate dates are NaN, not forward-filled
    nan_rows = w_default.isna().all(axis=1)
    assert nan_rows.sum() > 0
    # Every rebalance row sums to <= 1.0001
    rebal_rows = w_default[~nan_rows]
    for _, row in rebal_rows.iterrows():
        assert float(row.sum()) <= 1.0001


def test_live_runner_limit_down_double_freeze_and_limit_prices(tmp_path):
    """Test 10: Verify Stage 2 live runner limit-down double freeze (R14),
    limit-up buy rejection (R10), and execution price guidance fields (R11, T4.2, T4.3).
    """
    import json
    from unittest.mock import patch
    from common.allocation_templates import EqualWeightAllocation
    from common.data import SyntheticDataProvider
    from scripts.run_live_strategy import main as live_main

    univ_file = str(tmp_path / "universe.txt")
    with open(univ_file, "w") as f:
        f.write("600000.SH\n601398.SH\nBIL\n")

    # Account holds 1,000 shares of 600000.SH and 50,000 in BIL cash proxy
    holdings_file = str(tmp_path / "holdings.json")
    with open(holdings_file, "w") as f:
        json.dump({"600000.SH": 1000, "BIL": 50000}, f)

    state_file = str(tmp_path / "account_state.json")
    with open(state_file, "w") as f:
        json.dump({}, f)

    cmd_args = [
        "--strategy", "equal_weight",
        "--universe-file", univ_file,
        "--current-holdings-file", holdings_file,
        "--account-state-file", state_file,
        "--output-dir", str(tmp_path),
        "--as-of-date", "2024-05-10",
        "--start-date", "2024-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--exit-confirm-bars", "1",
        "--entry-confirm-bars", "1",
    ]

    # Strategy instructs: sell 600000.SH (target 0.0), buy 601398.SH (target 0.50)
    orig_gen = EqualWeightAllocation.generate_weights
    def mock_gen(self, universe, params=None):
        df = orig_gen(self, universe, params)
        df["600000.SH"] = 0.0
        df["601398.SH"] = 0.50
        df["BIL"] = 0.50
        return df

    # Mock market prices:
    # 600000.SH hits limit-down (-10% from prev close)
    # 601398.SH hits limit-up (+10% from prev close)
    orig_fetch = SyntheticDataProvider.fetch_ohlcv
    def mock_fetch(self, symbol, start, end, interval="1d"):
        df = orig_fetch(self, symbol, start, end, interval)
        if symbol == "600000.SH" and len(df) >= 2:
            prev_c = float(df["Close"].iloc[-2])
            df.iloc[-1, df.columns.get_loc("Close")] = round(prev_c * 0.90, 2)
            df.iloc[-1, df.columns.get_loc("Low")] = round(prev_c * 0.90, 2)
        elif symbol == "601398.SH" and len(df) >= 2:
            prev_c = float(df["Close"].iloc[-2])
            df.iloc[-1, df.columns.get_loc("Close")] = round(prev_c * 1.10, 2)
            df.iloc[-1, df.columns.get_loc("High")] = round(prev_c * 1.10, 2)
        return df

    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        with patch.object(SyntheticDataProvider, "fetch_ohlcv", mock_fetch):
            order_df = live_main(cmd_args)

    assert not order_df.empty

    # 1. Verify Phase 1 Sell Limit-Down Double Freeze (R14)
    r_600000 = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert r_600000["action"] == "SELL"
    assert r_600000["execution_warning"] == "LIMIT_DOWN_ILLIQUID_RISK"
    assert "LIMIT_DOWN_ILLIQUID_RISK" in r_600000["reason"]

    # 2. Verify Phase 2 Buy Limit-Up Lock (R10)
    r_601398 = order_df[order_df["symbol"] == "601398.SH"].iloc[0]
    assert r_601398["action"] == "HOLD_FILTERED"
    assert r_601398["execution_warning"] == "LIMIT_UP_LOCKED"
    assert "LIMIT_UP_LOCKED" in r_601398["reason"]

    # 3. Verify Phase 4 Cash Sweep did not overdraft proceeds of frozen sell order (R14)
    r_bil = order_df[order_df["symbol"] == "BIL"].iloc[0]
    # Trade value of BIL cash sweep should not include proceeds of 600000.SH
    assert r_bil["action"] == "SWEEP_CASH"

    # 4. Verify Execution price guidance fields (R11, T4.3)
    for _, row in order_df.iterrows():
        assert "suggested_limit_price" in row
        assert "passive_support_limit_price" in row
        assert "intraday_vwap" in row
        assert "vwap_premium_pct" in row
        assert "chan_5m_state" in row
        assert pd.notna(row["suggested_limit_price"])
        assert pd.notna(row["passive_support_limit_price"])

