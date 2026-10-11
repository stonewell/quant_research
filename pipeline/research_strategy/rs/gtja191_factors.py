"""Guotai Junan GTJA 191 Microstructure & Cont-KS (2014) OFI Proxy Factor Library.

Academic & Quantitative Grounding:
1. Guotai Junan Securities Research. "基于短周期价量特征的多因子选股体系"
   (Multi-Factor Stock Selection System Based on Short-Cycle Price-Volume Characteristics).
   Report #93 (June 2017).
   Introduces 191 short-horizon microstructure, shadow, gap, and volume acceleration factors.
2. Cont, Rama, Arseniy Kukanov, and Sasha Stoikov. "The Price Impact of Order Book Events."
   Journal of Financial Econometrics 12.1 (2014): 47-88.
   Order Flow Imbalance (OFI) proxy: links trade/quote imbalances directly to price drift.
3. Cartea, Álvaro, Sebastian Jaimungal, and José Penalva. "Algorithmic and
   High-Frequency Trading." Cambridge University Press, 2015.
   Optimal execution with short-term alpha drift and turnover friction penalty.

Implementation Philosophy:
- High performance: Fully vectorized across time and cross-sectional axes.
- Numerical safety: Guarded against zero divisions, NaNs, and extreme outliers.
- Seamless compatibility: Built on top of Alpha101Data container and alpha101 operators.
"""

from typing import Callable, Dict, List, Optional, Union
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    apply_asset_inertia,
    _fill_out_columns,
    _inverse_vol_weights,
)
from common.alpha101_operators import (
    adv,
    cs_demean,
    cs_rank,
    cs_scale,
    cs_zscore,
    signed_power,
    ternary,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_decay_linear,
    ts_delay,
    ts_delta,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
    vwap_proxy,
)
from common.indicators import sma
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .alpha101_factors import Alpha101Data
from .config import StrategyConfig


# =============================================================================
# Microstructural & Order Flow Imbalance (OFI) Operators
# =============================================================================

def upper_shadow(data: Alpha101Data) -> pd.DataFrame:
    """Intraday upper shadow selling pressure ratio: (High - max(Open, Close)) / (High - Low + 1e-8)."""
    max_oc = np.maximum(data.open, data.close)
    rng = np.maximum(1e-6, data.high - data.low)
    return (data.high - max_oc) / rng


def lower_shadow(data: Alpha101Data) -> pd.DataFrame:
    """Intraday lower shadow buying support ratio: (min(Open, Close) - Low) / (High - Low + 1e-8)."""
    min_oc = np.minimum(data.open, data.close)
    rng = np.maximum(1e-6, data.high - data.low)
    return (min_oc - data.low) / rng


def shadow_imbalance(data: Alpha101Data) -> pd.DataFrame:
    """Net shadow buying pressure: lower_shadow - upper_shadow."""
    return lower_shadow(data) - upper_shadow(data)


def kline_body(data: Alpha101Data) -> pd.DataFrame:
    """K-line absolute real body ratio: |Close - Open| / (High - Low + 1e-8)."""
    rng = np.maximum(1e-6, data.high - data.low)
    return (data.close - data.open).abs() / rng


def cont_ks_ofi_proxy(data: Alpha101Data) -> pd.DataFrame:
    """Cont-Kukanov-Stoikov (2014) EOD Order Flow Imbalance proxy.
    Approximates buyer-initiated trade pressure using bar close location within the daily range:
    ((2 * Close - High - Low) / (High - Low + 1e-8)) * Volume.
    """
    rng = np.maximum(1e-6, data.high - data.low)
    loc = (2.0 * data.close - data.high - data.low) / rng
    return loc * data.volume


# =============================================================================
# Top 25 Selected GTJA 191 Microstructure Factors
# =============================================================================

def gtja_alpha1(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#1: (-1 * ts_corr(cs_rank(ts_delta(log(volume), 1)), cs_rank((close - open) / open), 6))."""
    vol_ret = ts_delta(np.log(np.maximum(1.0, data.volume)), 1)
    ret_oc = (data.close - data.open) / np.maximum(1e-6, data.open)
    return -1.0 * ts_corr(cs_rank(vol_ret), cs_rank(ret_oc), 6)


def gtja_alpha2(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#2: -1 * ts_delta(((close - low) - (high - close)) / (high - low + 1e-8), 1)."""
    rng = np.maximum(1e-6, data.high - data.low)
    cl_hc = (data.close - data.low) - (data.high - data.close)
    return -1.0 * ts_delta(cl_hc / rng, 1)


def gtja_alpha3(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#3: Price jump spread relative to prior close."""
    c_prev = ts_delay(data.close, 1)
    cond_eq = data.close == c_prev
    cond_gt = data.close > c_prev
    jump = ternary(cond_gt, np.minimum(data.low, c_prev), np.maximum(data.high, c_prev))
    return ternary(cond_eq, 0.0, data.close - jump)


def gtja_alpha4(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#4: Moving average band decoupling with volume surge confirmation."""
    ma8 = ts_mean(data.close, 8)
    std8 = ts_std(data.close, 8)
    ma2 = ts_mean(data.close, 2)
    vol_ratio = data.volume / np.maximum(1.0, ts_mean(data.volume, 20))
    c1 = ma8 + std8 < ma2
    c2 = ma2 < ma8 - std8
    return ternary(c1, -1.0, ternary(c2, 1.0, ternary(vol_ratio >= 1.0, 1.0, -1.0)))


def gtja_alpha5(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#5: -1 * ts_max(ts_corr(ts_rank(volume, 5), ts_rank(high, 5), 5), 3)."""
    corr = ts_corr(ts_rank(data.volume, 5), ts_rank(data.high, 5), 5)
    return -1.0 * ts_max(corr, 3)


def gtja_alpha6(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#6: cs_rank(ts_delta(close, 4) / ts_delay(close, 4)) * cs_rank(volume / ts_mean(volume, 20))."""
    ret4 = ts_delta(data.close, 4) / np.maximum(1e-6, ts_delay(data.close, 4))
    vol_ratio = data.volume / np.maximum(1.0, ts_mean(data.volume, 20))
    return cs_rank(ret4) * cs_rank(vol_ratio)


def gtja_alpha7(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#7: cs_rank(ts_max(vwap - close, 3)) + cs_rank(ts_min(vwap - close, 3)) * cs_rank(ts_delta(volume, 3))."""
    vwap_diff = data.vwap - data.close
    max_vd = ts_max(vwap_diff, 3)
    min_vd = ts_min(vwap_diff, 3)
    return cs_rank(max_vd) + cs_rank(min_vd) * cs_rank(ts_delta(data.volume, 3))


def gtja_alpha8(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#8: cs_rank(ts_delta((high + low) / 2, 4) * -1 * ts_delta(volume, 4))."""
    mid = (data.high + data.low) / 2.0
    return cs_rank(ts_delta(mid, 4) * -1.0 * ts_delta(data.volume, 4))


def gtja_alpha9(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#9: ts_mean(ts_delta(close, 1) / (ts_max(high - low, 5) + 1e-8), 5)."""
    rng5 = ts_max(data.high - data.low, 5)
    eff = ts_delta(data.close, 1) / np.maximum(1e-6, rng5)
    return ts_mean(eff, 5)


def gtja_alpha10(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#10: cs_rank(ts_std(ternary(close < ts_delay(close, 1), close - ts_delay(close, 1), 0), 20))."""
    c_prev = ts_delay(data.close, 1)
    downside = ternary(data.close < c_prev, data.close - c_prev, 0.0)
    return cs_rank(ts_std(downside, 20))


def gtja_alpha11(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#11: ts_sum((close - low) - (high - close), 6) / (ts_sum(high - low, 6) + 1e-8) * volume."""
    net_press = (data.close - data.low) - (data.high - data.close)
    rng = data.high - data.low
    ratio = ts_sum(net_press, 6) / np.maximum(1e-6, ts_sum(rng, 6))
    return ratio * data.volume


def gtja_alpha12(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#12: cs_rank(open - ts_mean(open, 10)) * (-1 * cs_rank(abs(close - open)))."""
    o_dev = data.open - ts_mean(data.open, 10)
    body = (data.close - data.open).abs()
    return cs_rank(o_dev) * (-1.0 * cs_rank(body))


def gtja_alpha13(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#13: signed_power(sqrt(high * low) - vwap, 1.0)."""
    geo_mid = np.sqrt(np.maximum(1e-6, data.high * data.low))
    return signed_power(geo_mid - data.vwap, 1.0)


def gtja_alpha14(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#14: 5-day close momentum: close - ts_delay(close, 5)."""
    return data.close - ts_delay(data.close, 5)


def gtja_alpha15(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#15: Overnight gap: open / ts_delay(close, 1) - 1.0."""
    return data.open / np.maximum(1e-6, ts_delay(data.close, 1)) - 1.0


def gtja_alpha16(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#16: Upper shadow selling exhaustion reversal: -1 * ts_mean(upper_shadow, 5)."""
    return -1.0 * ts_mean(upper_shadow(data), 5)


def gtja_alpha17(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#17: cs_rank(vwap - ts_max(vwap, 15)) * ts_delta(close, 5)."""
    vwap_drawdown = data.vwap - ts_max(data.vwap, 15)
    return cs_rank(vwap_drawdown) * ts_delta(data.close, 5)


def gtja_alpha18(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#18: 5-day price ratio: close / ts_delay(close, 5)."""
    return data.close / np.maximum(1e-6, ts_delay(data.close, 5))


def gtja_alpha19(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#19: Asymmetric 5-day return normalized by previous or current close."""
    c5 = ts_delay(data.close, 5)
    diff = data.close - c5
    return ternary(data.close < c5, diff / np.maximum(1e-6, c5), diff / np.maximum(1e-6, data.close))


def gtja_alpha20(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#20: 6-day ROC percent: (close - ts_delay(close, 6)) / ts_delay(close, 6) * 100."""
    c6 = ts_delay(data.close, 6)
    return (data.close - c6) / np.maximum(1e-6, c6) * 100.0


def gtja_alpha21(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#21: Short-term mean reversion ratio: ts_mean(close, 6) / close."""
    return ts_mean(data.close, 6) / np.maximum(1e-6, data.close)


def gtja_alpha22(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#22: 12-day smoothed deviation from 6-day SMA."""
    ma6 = ts_mean(data.close, 6)
    dev = (data.close - ma6) / np.maximum(1e-6, ma6)
    return ts_mean(dev, 12)


def gtja_alpha23(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#23: Upside volatility persistence: ts_mean(ternary(close > ts_delay(close, 1), ts_std(close, 20), 0), 20)."""
    c_prev = ts_delay(data.close, 1)
    cond = data.close > c_prev
    up_vol = ternary(cond, ts_std(data.close, 20), 0.0)
    return ts_mean(up_vol, 20)


def gtja_alpha24(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#24: Short vs medium momentum divergence: delta(close, 5)/close - delta(close, 20)/close."""
    ret5 = ts_delta(data.close, 5) / np.maximum(1e-6, data.close)
    ret20 = ts_delta(data.close, 20) / np.maximum(1e-6, data.close)
    return ret5 - ret20


def gtja_alpha33(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#33: Rebound from 5d low with volume acceleration."""
    low5 = ts_min(data.low, 5)
    high5 = ts_max(data.high, 5)
    stoch = (data.close - low5) / np.maximum(1e-6, high5 - low5)
    vol_accel = data.volume / np.maximum(1.0, ts_mean(data.volume, 20))
    return stoch * cs_rank(vol_accel)


def gtja_alpha44(data: Alpha101Data) -> pd.DataFrame:
    """GTJA#44: 5-day VWAP velocity: ts_delta(vwap, 5) / vwap."""
    return ts_delta(data.vwap, 5) / np.maximum(1e-6, data.vwap)


def gtja_alpha_ofi(data: Alpha101Data) -> pd.DataFrame:
    """Cont-Kukanov-Stoikov (2014) OFI Proxy factor: 5-day smoothed OFI."""
    return ts_mean(cont_ks_ofi_proxy(data), 5)


# Registry of Top 25 GTJA 191 factors + OFI
GTJA_ALPHA_REGISTRY: Dict[Union[int, str], Callable[[Alpha101Data], pd.DataFrame]] = {
    1: gtja_alpha1,
    2: gtja_alpha2,
    3: gtja_alpha3,
    4: gtja_alpha4,
    5: gtja_alpha5,
    6: gtja_alpha6,
    7: gtja_alpha7,
    8: gtja_alpha8,
    9: gtja_alpha9,
    10: gtja_alpha10,
    11: gtja_alpha11,
    12: gtja_alpha12,
    13: gtja_alpha13,
    14: gtja_alpha14,
    15: gtja_alpha15,
    16: gtja_alpha16,
    17: gtja_alpha17,
    18: gtja_alpha18,
    19: gtja_alpha19,
    20: gtja_alpha20,
    21: gtja_alpha21,
    22: gtja_alpha22,
    23: gtja_alpha23,
    24: gtja_alpha24,
    33: gtja_alpha33,
    44: gtja_alpha44,
    "ofi": gtja_alpha_ofi,
}


def compute_gtja_alpha(
    alpha_id: Union[int, str],
    data: Alpha101Data,
) -> pd.DataFrame:
    """Dispatches and computes any registered GTJA 191 / OFI factor.

    Parameters:
    -----------
    alpha_id : int or str
        Factor identifier (e.g. 1, 16, 24, 33, 44, 'ofi').
    data : Alpha101Data
        Precomputed market data panel container.

    Returns:
    --------
    pd.DataFrame:
        Computed factor matrix aligned to data.close index and columns.
    """
    key = alpha_id
    if isinstance(key, str):
        key_lower = key.lower().replace("gtja_alpha", "").replace("gtja", "").replace("alpha_", "").strip()
        if key_lower in ("ofi", "cont_ks_ofi"):
            key = "ofi"
        elif key_lower.isdigit():
            key = int(key_lower)

    if key not in GTJA_ALPHA_REGISTRY:
        # Fallback to alpha 16 (upper shadow exhaustion)
        key = 16

    fn = GTJA_ALPHA_REGISTRY[key]
    out = fn(data)
    # Sanitize NaNs and infinities
    return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


# =============================================================================
# Systematic Allocation Strategy Implementation
# =============================================================================

class GTJAAlphaStrategy(AllocationTemplate):
    """Systematic allocation template driven by any GTJA 191 Microstructure Factor.
    Configurable via `gtja_alpha_id` (default: 16).
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="gtja_alpha",
            param_grid={},
            factor_tags=["mean_reversion", "relative_momentum"],
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        p = params or {}
        ma_period = p.get("gtja_trend_ma_period", getattr(self.config, "gtja_trend_ma_period", 200))
        return max(250, ma_period + 20)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        p = params or {}
        aid = p.get("gtja_alpha_id", getattr(self.config, "gtja_alpha_id", 16))
        freq = p.get("gtja_rebalance_freq_days", getattr(self.config, "gtja_rebalance_freq_days", 5))
        k = p.get("gtja_top_k", getattr(self.config, "gtja_top_k", 3))
        mode = p.get("gtja_weighting_mode", getattr(self.config, "gtja_weighting_mode", "equal"))
        return (
            f"GTJA Alpha#{aid} Allocation: Rebalances every {freq} days. "
            f"Selects top {k} assets by cross-sectional microstructure factor score with {mode} weighting. "
            f"Preserves cash proxy on unallocated capital."
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        alpha_id = p.get("gtja_alpha_id", getattr(cfg, "gtja_alpha_id", 16))
        rebal_freq = p.get("gtja_rebalance_freq_days", getattr(cfg, "gtja_rebalance_freq_days", 5))
        top_k = p.get("gtja_top_k", getattr(cfg, "gtja_top_k", 3))
        weighting_mode = p.get("gtja_weighting_mode", getattr(cfg, "gtja_weighting_mode", "equal"))
        require_trend = p.get("gtja_require_trend_filter", getattr(cfg, "gtja_require_trend_filter", True))
        trend_period = p.get("gtja_trend_ma_period", getattr(cfg, "gtja_trend_ma_period", 200))
        smoothing_days = p.get("gtja_smoothing_days", getattr(cfg, "gtja_smoothing_days", 1))
        min_weight_change = p.get("gtja_min_weight_change", getattr(cfg, "gtja_min_weight_change", 0.03))
        cash_proxy = p.get("cash_proxy", getattr(cfg, "cash_proxy", "BIL"))

        symbols = list(universe.keys())
        from .strategy import _get_risky_symbols, _aligned_master_index
        risky_symbols = _get_risky_symbols(universe, p, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        data = Alpha101Data.from_universe(universe, symbols=risky_symbols, master_index=master_index)
        alpha_scores = compute_gtja_alpha(alpha_id, data)

        if smoothing_days > 1:
            alpha_scores = ts_mean(alpha_scores, smoothing_days).fillna(0.0)

        trend_mask = pd.DataFrame(True, index=master_index, columns=risky_symbols)
        if require_trend:
            for s in risky_symbols:
                if s in universe and not universe[s].empty:
                    c = universe[s]["Close"].reindex(master_index).ffill()
                    ma = sma(c, trend_period)
                    trend_mask[s] = (c >= ma).fillna(False)

        daily_weights = pd.DataFrame(0.0, index=master_index, columns=symbols)
        rebalance_dates = set(_get_rebalance_dates(master_index, rebal_freq))
        cur_target = {s: 0.0 for s in symbols}

        for t, date in enumerate(master_index):
            if date in rebalance_dates:
                scores_t = alpha_scores.loc[date].copy()
                trend_t = trend_mask.loc[date]
                valid_scores = scores_t[trend_t].dropna()

                if len(valid_scores) > 0:
                    ranked = valid_scores.sort_values(ascending=False)
                    k_actual = min(top_k, len(ranked))
                    selected = list(ranked.index[:k_actual])

                    if weighting_mode == "rank":
                        weights_raw = np.arange(k_actual, 0, -1, dtype=float)
                        weights_norm = weights_raw / weights_raw.sum()
                        w_map = dict(zip(selected, weights_norm))
                    elif weighting_mode == "inv_vol":
                        vol_slice = {}
                        for s in selected:
                            if t >= 20:
                                rets = data.returns[s].iloc[max(0, t - 20) : t]
                                vol = float(rets.std()) if rets.std() > 1e-4 else 0.02
                            else:
                                vol = 0.02
                            vol_slice[s] = vol
                        inv_v = {s: 1.0 / v for s, v in vol_slice.items()}
                        tot_inv = sum(inv_v.values())
                        w_map = {s: inv_v[s] / tot_inv for s in selected}
                    else:  # equal
                        w_map = {s: 1.0 / k_actual for s in selected}

                    cur_target = {s: 0.0 for s in symbols}
                    for s, w in w_map.items():
                        cur_target[s] = w
                    if cash_proxy in symbols:
                        cur_target[cash_proxy] = max(0.0, 1.0 - sum(cur_target[s] for s in risky_symbols))
                else:
                    cur_target = {s: 0.0 for s in symbols}
                    if cash_proxy in symbols:
                        cur_target[cash_proxy] = 1.0

            for s in symbols:
                daily_weights.loc[date, s] = cur_target.get(s, 0.0)

        if min_weight_change > 0.0:
            daily_weights = apply_asset_inertia(
                daily_weights,
                min_weight_change=min_weight_change,
                cash_proxy=cash_proxy,
            )

        daily_weights = _fill_out_columns(daily_weights, symbols)
        from common.allocation_templates import _sparse_from_daily
        return _sparse_from_daily(daily_weights)
