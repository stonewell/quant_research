"""Bollinger Bands Quantitative Strategy (John Bollinger Methods I & III).

Method I: Squeeze Breakout Strategy (bollinger_breakout)
- Monitors BandWidth = (Upper - Lower) / Mid over `bb_squeeze_lookback` (default 126 bars).
- Flags a volatility squeeze when BandWidth compresses into its lowest quantile (`bb_squeeze_quantile`, default 20%).
- Triggers long entry when price breaks out above the upper band (%B > 1.0 or Close > Upper Band)
  following a squeeze, confirmed by macro 200-day SMA trend alignment (if `bb_require_trend_filter`).
- Exits when price drops below the 20-day SMA middle band (equilibrium exit) or triggers trailing stop / stop-loss / timeout.

Method III: Statistical Mean Reversion Fade (bollinger_mean_reversion)
- Triggers long entry when price pierces below the lower band (%B < 0.0 or Close < Lower Band)
  with RSI(14) oversold confirmation (< 35.0 if `bb_rsi_filter`).
- Macro filter: ensures asset is in an overall uptrend (Close > SMA_200) to avoid catching falling knives during secular bear markets.
- Exits when price reverts back up to the 20-day SMA middle band (%B >= 0.5 or Close >= Mid Band) or triggers stop-loss / timeout.
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.indicators import (
    bollinger_bands,
    rsi,
    sma,
)
from .config import StrategyConfig


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL"):
    from .strategy import _get_risky_symbols
    return _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


class BollingerBandsStrategy(AllocationTemplate):
    """Bollinger Bands Quantitative Trading Strategy supporting Breakout and Mean-Reversion modes."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="bollinger_bands", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        mode = p.get("bb_mode", cfg.bb_mode)
        bb_period = p.get("bb_period", cfg.bb_period)
        bb_num_std = p.get("bb_num_std", cfg.bb_num_std)
        squeeze_lookback = p.get("bb_squeeze_lookback", cfg.bb_squeeze_lookback)
        squeeze_quantile = p.get("bb_squeeze_quantile", cfg.bb_squeeze_quantile)
        require_trend_filter = p.get("bb_require_trend_filter", cfg.bb_require_trend_filter)
        trend_ma_period = p.get("bb_trend_ma_period", cfg.bb_trend_ma_period)
        rsi_filter = p.get("bb_rsi_filter", cfg.bb_rsi_filter)
        rsi_period = p.get("bb_rsi_period", cfg.bb_rsi_period)
        rsi_oversold = p.get("bb_rsi_oversold", cfg.bb_rsi_oversold)
        exit_mode = p.get("bb_exit_mode", cfg.bb_exit_mode)
        stop_loss_pct = p.get("bb_stop_loss_pct", cfg.bb_stop_loss_pct)
        trailing_stop_pct = p.get("bb_trailing_stop_pct", cfg.bb_trailing_stop_pct)
        trailing_activate_pct = p.get("bb_trailing_activate_pct", cfg.bb_trailing_activate_pct)
        max_holding_days = p.get("bb_max_holding_days", cfg.bb_max_holding_days)
        position_size_pct = p.get("bb_position_size_pct", cfg.bb_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(
            universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy
        )
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        active_mask = {}

        for sym in risky_symbols:
            df = universe[sym]
            close = df["Close"]
            high = df["High"] if "High" in df.columns else close
            n_bars = len(close)
            if n_bars == 0:
                continue

            bb = bollinger_bands(close, period=bb_period, num_std=bb_num_std)
            mid = bb["mid"]
            upper = bb["upper"]
            lower = bb["lower"]
            pctb = bb["pctb"]
            bw = bb["bandwidth"]

            trend_ma = sma(close, trend_ma_period) if require_trend_filter else None
            rsi_series = rsi(close, rsi_period) if (rsi_filter and mode == "mean_reversion") else None

            # Squeeze detection: bandwidth in bottom quantile of rolling window
            rolling_bw_q = bw.rolling(window=squeeze_lookback, min_periods=bb_period).quantile(squeeze_quantile)
            squeeze_condition = (bw <= rolling_bw_q).fillna(False)
            # Squeeze active recently (within last 15 bars)
            recent_squeeze = squeeze_condition.rolling(window=15, min_periods=1).max() > 0

            active = np.zeros(n_bars, dtype=bool)
            in_position = False
            entry_idx = 0
            highest_price = 0.0

            for i in range(n_bars):
                c = close.iloc[i]
                h = high.iloc[i]

                if pd.isna(c) or c <= 0:
                    in_position = False
                    active[i] = False
                    continue

                if in_position:
                    held = i - entry_idx
                    entry_p = close.iloc[entry_idx]
                    ret = c / entry_p - 1.0 if entry_p > 0 else 0.0

                    if h > highest_price:
                        highest_price = h

                    stopped = stop_loss_pct is not None and ret <= -stop_loss_pct
                    timed_out = max_holding_days is not None and held >= max_holding_days

                    peak_ret = highest_price / entry_p - 1.0 if entry_p > 0 else 0.0
                    trail_activated = trailing_activate_pct is None or peak_ret >= trailing_activate_pct
                    trail_hit = (
                        trailing_stop_pct is not None
                        and trail_activated
                        and highest_price > 0
                        and (c / highest_price - 1.0) <= -trailing_stop_pct
                    )

                    # Exit condition
                    m_val = mid.iloc[i]
                    l_val = lower.iloc[i]
                    if mode == "breakout":
                        # Breakout exit: close falls below mid band (or lower band if opposite_band)
                        if exit_mode == "opposite_band":
                            exit_signal = pd.notna(l_val) and c < l_val
                        else:
                            exit_signal = pd.notna(m_val) and c < m_val
                    else:
                        # Mean reversion exit: revert to mid band
                        exit_signal = pd.notna(m_val) and c >= m_val

                    if exit_signal or stopped or timed_out or trail_hit:
                        in_position = False
                    active[i] = in_position
                else:
                    # Check entry conditions
                    trend_ok = True
                    if require_trend_filter:
                        ma = trend_ma.iloc[i] if trend_ma is not None else None
                        trend_ok = pd.notna(ma) and c > ma

                    if mode == "breakout":
                        u_val = upper.iloc[i]
                        pb_val = pctb.iloc[i]
                        breakout_signal = (pd.notna(u_val) and c > u_val) or (pd.notna(pb_val) and pb_val > 1.0)
                        sq_ok = bool(recent_squeeze.iloc[i]) if pd.notna(recent_squeeze.iloc[i]) else False
                        if breakout_signal and sq_ok and trend_ok:
                            in_position = True
                            entry_idx = i
                            highest_price = h
                    else:  # mean_reversion
                        l_val = lower.iloc[i]
                        pb_val = pctb.iloc[i]
                        oversold_band = (pd.notna(l_val) and c < l_val) or (pd.notna(pb_val) and pb_val < 0.0)
                        rsi_ok = True
                        if rsi_filter and rsi_series is not None:
                            r_val = rsi_series.iloc[i]
                            rsi_ok = pd.notna(r_val) and r_val < rsi_oversold
                        if oversold_band and rsi_ok and trend_ok:
                            in_position = True
                            entry_idx = i
                            highest_price = h

                    active[i] = in_position

            active_mask[sym] = pd.Series(active, index=close.index).reindex(master_index).fillna(False).to_numpy()

        if not active_mask:
            return pd.DataFrame()

        mask_df = pd.DataFrame(active_mask, index=master_index).astype(float)
        sum_active = mask_df.sum(axis=1)
        scale = np.where(sum_active > 0, 1.0 / np.maximum(1.0, sum_active), 0.0)
        daily_weights = mask_df.mul(scale * min(1.0, max(0.0, position_size_pct)), axis=0)

        if cash_proxy in symbols:
            daily_weights[cash_proxy] = np.maximum(0.0, 1.0 - daily_weights.sum(axis=1))

        daily_weights = _fill_out_columns(daily_weights, symbols)
        return _sparse_from_daily(daily_weights)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        mode = p.get("bb_mode", cfg.bb_mode)
        period = p.get("bb_period", cfg.bb_period)
        num_std = p.get("bb_num_std", cfg.bb_num_std)
        tf = p.get("bb_require_trend_filter", cfg.bb_require_trend_filter)
        ma_period = p.get("bb_trend_ma_period", cfg.bb_trend_ma_period)
        tf_str = f"with {ma_period}d SMA trend filter" if tf else "without trend filter"
        if mode == "breakout":
            sq_lookback = p.get("bb_squeeze_lookback", cfg.bb_squeeze_lookback)
            sq_q = p.get("bb_squeeze_quantile", cfg.bb_squeeze_quantile)
            return (
                f"Bollinger Bands Squeeze Breakout (Method I, {period}d, {num_std} std): "
                f"buys upper band breakout after {sq_lookback}d BandWidth squeeze (bottom {int(sq_q*100)}%) {tf_str}. "
                f"Exits at {period}d SMA middle band or trailing stop."
            )
        else:
            rsi_f = p.get("bb_rsi_filter", cfg.bb_rsi_filter)
            rsi_os = p.get("bb_rsi_oversold", cfg.bb_rsi_oversold)
            rsi_str = f", RSI < {rsi_os}" if rsi_f else ""
            return (
                f"Bollinger Bands Mean Reversion (Method III, {period}d, {num_std} std): "
                f"buys lower band %B < 0 oversold{rsi_str} {tf_str}. "
                f"Exits at {period}d SMA middle band mean-reversion."
            )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        cfg = self.config
        p = params or {}
        period = p.get("bb_period", cfg.bb_period)
        sq_lookback = p.get("bb_squeeze_lookback", cfg.bb_squeeze_lookback)
        ma_period = p.get("bb_trend_ma_period", cfg.bb_trend_ma_period) if p.get("bb_require_trend_filter", cfg.bb_require_trend_filter) else 0
        rsi_p = p.get("bb_rsi_period", cfg.bb_rsi_period) if p.get("bb_rsi_filter", cfg.bb_rsi_filter) else 0
        return max(period, sq_lookback, ma_period, rsi_p) + 1
