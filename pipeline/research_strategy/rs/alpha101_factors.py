"""Complete Implementation of WorldQuant 101 Formulaic Alphas (1-101).

Academic Reference:
Kakushadze, Zura. "101 Formulaic Alphas." Wilmott Magazine 2016.84 (2016): 72-81.
arXiv:1601.00991 (2015).

This module contains explicit, vectorized implementations of ALL 101 formulaic
alphas described in the original paper. Every alpha from Alpha#1 through Alpha#101
is implemented as a standalone callable and registered in `ALPHA_REGISTRY`.

Design Architecture:
- `Alpha101Data`: High-performance precomputed panel container for OHLCV, returns,
  VWAP, market cap, and rolling average daily dollar volumes (adv{d}).
- Vectorized execution: Leverages `common.alpha101_operators` (ts_*, cs_*) for
  fast, row-loop-free DataFrame execution across all symbols.
- Numerical safety: Guarded against zero division, negative roots, and missing volumes.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union
import numpy as np
import pandas as pd

from common.alpha101_operators import (
    adv as _adv,
    cs_demean,
    cs_indneutralize,
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
    ts_prod,
    ts_rank,
    ts_std,
    ts_sum,
    vwap_proxy,
)


@dataclass
class Alpha101Data:
    """Precomputed panel container for cross-sectional multi-asset price and volume data.
    All fields are DataFrames indexed by DatetimeIndex with symbol tickers as columns.
    """
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    vwap: pd.DataFrame
    returns: pd.DataFrame
    cap: pd.DataFrame
    ind_class: Optional[Dict[str, str]] = None
    _adv_cache: Optional[Dict[int, pd.DataFrame]] = None

    def __post_init__(self):
        if self._adv_cache is None:
            self._adv_cache = {}

    def adv(self, d: Union[int, float]) -> pd.DataFrame:
        """Returns cached rolling average daily dollar volume over past d days."""
        key = int(np.floor(d))
        if key not in self._adv_cache:
            self._adv_cache[key] = _adv(self.volume, self.close, key)
        return self._adv_cache[key]

    @classmethod
    def from_universe(cls,
                      universe: Dict[str, pd.DataFrame],
                      symbols: Optional[List[str]] = None,
                      master_index: Optional[pd.DatetimeIndex] = None,
                      ind_class: Optional[Dict[str, str]] = None) -> "Alpha101Data":
        """Builds an Alpha101Data container from a universe dictionary."""
        tracked_symbols = symbols or list(universe.keys())
        if not tracked_symbols:
            empty = pd.DataFrame()
            return cls(empty, empty, empty, empty, empty, empty, empty, empty, ind_class)

        if master_index is None:
            # Union of all dates across tracked symbols
            idx = universe[tracked_symbols[0]].index
            for s in tracked_symbols[1:]:
                idx = idx.union(universe[s].index)
            master_index = idx.sort_values()

        open_dict = {}
        high_dict = {}
        low_dict = {}
        close_dict = {}
        volume_dict = {}
        vwap_dict = {}

        for sym in tracked_symbols:
            df = universe[sym].reindex(master_index)
            c = df["Close"].ffill().bfill()
            h = df["High"].ffill().bfill() if "High" in df.columns else c
            l = df["Low"].ffill().bfill() if "Low" in df.columns else c
            o = df["Open"].ffill().bfill() if "Open" in df.columns else c
            if "Volume" in df.columns:
                v = df["Volume"].ffill().fillna(1e6).clip(lower=1.0)
            else:
                v = pd.Series(1e6, index=master_index)

            if "VWAP" in df.columns:
                vw = df["VWAP"].ffill().bfill()
            else:
                vw = (h + l + c) / 3.0

            open_dict[sym] = o
            high_dict[sym] = h
            low_dict[sym] = l
            close_dict[sym] = c
            volume_dict[sym] = v
            vwap_dict[sym] = vw

        open_df = pd.DataFrame(open_dict, index=master_index)
        high_df = pd.DataFrame(high_dict, index=master_index)
        low_df = pd.DataFrame(low_dict, index=master_index)
        close_df = pd.DataFrame(close_dict, index=master_index)
        volume_df = pd.DataFrame(volume_dict, index=master_index)
        vwap_df = pd.DataFrame(vwap_dict, index=master_index)

        returns_df = close_df.pct_change().fillna(0.0)
        # Default market cap proxy: close * volume
        cap_df = close_df * volume_df

        return cls(
            open=open_df,
            high=high_df,
            low=low_df,
            close=close_df,
            volume=volume_df,
            vwap=vwap_df,
            returns=returns_df,
            cap=cap_df,
            ind_class=ind_class,
        )


# =============================================================================
# Formulaic Alphas 1 - 25
# =============================================================================

def alpha_1(d: Alpha101Data) -> pd.DataFrame:
    """(rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5)"""
    inner = ternary(d.returns < 0, ts_std(d.returns, 20), d.close)
    return cs_rank(ts_argmax(signed_power(inner, 2.0), 5)) - 0.5


def alpha_2(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * correlation(rank(delta(log(volume), 2)), rank(((close - open) / open)), 6))"""
    r1 = cs_rank(ts_delta(np.log(d.volume.clip(lower=1.0)), 2))
    r2 = cs_rank((d.close - d.open) / d.open.clip(lower=1e-4))
    return -1.0 * ts_corr(r1, r2, 6)


def alpha_3(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * correlation(rank(open), rank(volume), 10))"""
    return -1.0 * ts_corr(cs_rank(d.open), cs_rank(d.volume), 10)


def alpha_4(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * Ts_Rank(rank(low), 9))"""
    return -1.0 * ts_rank(cs_rank(d.low), 9)


def alpha_5(d: Alpha101Data) -> pd.DataFrame:
    """(rank((open - (sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vwap)))))"""
    term1 = cs_rank(d.open - (ts_sum(d.vwap, 10) / 10.0))
    term2 = -1.0 * cs_rank(d.close - d.vwap).abs()
    return term1 * term2


def alpha_6(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * correlation(open, volume, 10))"""
    return -1.0 * ts_corr(d.open, d.volume, 10)


def alpha_7(d: Alpha101Data) -> pd.DataFrame:
    """((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1 * 1))"""
    cond = d.adv(20) < d.volume
    t_val = (-1.0 * ts_rank(ts_delta(d.close, 7).abs(), 60)) * np.sign(ts_delta(d.close, 7))
    return ternary(cond, t_val, -1.0)


def alpha_8(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * rank(((sum(open, 5) * sum(returns, 5)) - delay((sum(open, 5) * sum(returns, 5)), 10))))"""
    x = ts_sum(d.open, 5) * ts_sum(d.returns, 5)
    return -1.0 * cs_rank(x - ts_delay(x, 10))


def alpha_9(d: Alpha101Data) -> pd.DataFrame:
    """((0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : ((ts_max(delta(close, 1), 5) < 0) ? delta(close, 1) : (-1 * delta(close, 1))))"""
    d_close = ts_delta(d.close, 1)
    cond1 = 0.0 < ts_min(d_close, 5)
    cond2 = ts_max(d_close, 5) < 0.0
    return ternary(cond1, d_close, ternary(cond2, d_close, -1.0 * d_close))


def alpha_10(d: Alpha101Data) -> pd.DataFrame:
    """rank(((0 < ts_min(delta(close, 1), 4)) ? delta(close, 1) : ((ts_max(delta(close, 1), 4) < 0) ? delta(close, 1) : (-1 * delta(close, 1)))))"""
    d_close = ts_delta(d.close, 1)
    cond1 = 0.0 < ts_min(d_close, 4)
    cond2 = ts_max(d_close, 4) < 0.0
    return cs_rank(ternary(cond1, d_close, ternary(cond2, d_close, -1.0 * d_close)))


def alpha_11(d: Alpha101Data) -> pd.DataFrame:
    """((rank(ts_max((vwap - close), 3)) + rank(ts_min((vwap - close), 3))) * rank(delta(volume, 3)))"""
    t1 = cs_rank(ts_max(d.vwap - d.close, 3)) + cs_rank(ts_min(d.vwap - d.close, 3))
    t2 = cs_rank(ts_delta(d.volume, 3))
    return t1 * t2


def alpha_12(d: Alpha101Data) -> pd.DataFrame:
    """(sign(delta(volume, 1)) * (-1 * delta(close, 1)))"""
    return np.sign(ts_delta(d.volume, 1)) * (-1.0 * ts_delta(d.close, 1))


def alpha_13(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * rank(covariance(rank(close), rank(volume), 5)))"""
    return -1.0 * cs_rank(ts_cov(cs_rank(d.close), cs_rank(d.volume), 5))


def alpha_14(d: Alpha101Data) -> pd.DataFrame:
    """((-1 * rank(delta(returns, 3))) * correlation(open, volume, 10))"""
    return (-1.0 * cs_rank(ts_delta(d.returns, 3))) * ts_corr(d.open, d.volume, 10)


def alpha_15(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3))"""
    return -1.0 * ts_sum(cs_rank(ts_corr(cs_rank(d.high), cs_rank(d.volume), 3)), 3)


def alpha_16(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * rank(covariance(rank(high), rank(volume), 5)))"""
    return -1.0 * cs_rank(ts_cov(cs_rank(d.high), cs_rank(d.volume), 5))


def alpha_17(d: Alpha101Data) -> pd.DataFrame:
    """(((-1 * rank(ts_rank(close, 10))) * rank(delta(delta(close, 1), 1))) * rank(ts_rank((volume / adv20), 5)))"""
    t1 = -1.0 * cs_rank(ts_rank(d.close, 10))
    t2 = cs_rank(ts_delta(ts_delta(d.close, 1), 1))
    t3 = cs_rank(ts_rank(d.volume / d.adv(20).clip(lower=1e-4), 5))
    return t1 * t2 * t3


def alpha_18(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * rank(((stddev(abs((close - open)), 5) + (close - open)) + correlation(close, open, 10))))"""
    x = ts_std((d.close - d.open).abs(), 5) + (d.close - d.open) + ts_corr(d.close, d.open, 10)
    return -1.0 * cs_rank(x)


def alpha_19(d: Alpha101Data) -> pd.DataFrame:
    """((-1 * sign(((close - delay(close, 7)) + delta(close, 7)))) * (1 + rank((1 + sum(returns, 250)))))"""
    t1 = -1.0 * np.sign((d.close - ts_delay(d.close, 7)) + ts_delta(d.close, 7))
    t2 = 1.0 + cs_rank(1.0 + ts_sum(d.returns, 250))
    return t1 * t2


def alpha_20(d: Alpha101Data) -> pd.DataFrame:
    """(((-1 * rank((open - delay(high, 1)))) * rank((open - delay(close, 1)))) * rank((open - delay(low, 1))))"""
    t1 = -1.0 * cs_rank(d.open - ts_delay(d.high, 1))
    t2 = cs_rank(d.open - ts_delay(d.close, 1))
    t3 = cs_rank(d.open - ts_delay(d.low, 1))
    return t1 * t2 * t3


def alpha_21(d: Alpha101Data) -> pd.DataFrame:
    """((((sum(close, 8) / 8) + stddev(close, 8)) < (sum(close, 2) / 2)) ? (-1 * 1) : (((sum(close, 2) / 2) < ((sum(close, 8) / 8) - stddev(close, 8))) ? 1 : (((1 < (volume / adv20)) || ((volume / adv20) == 1)) ? 1 : (-1 * 1))))"""
    c8 = ts_sum(d.close, 8) / 8.0
    s8 = ts_std(d.close, 8)
    c2 = ts_sum(d.close, 2) / 2.0
    cond1 = (c8 + s8) < c2
    cond2 = c2 < (c8 - s8)
    cond3 = (d.volume / d.adv(20).clip(lower=1e-4)) >= 1.0
    return ternary(cond1, -1.0, ternary(cond2, 1.0, ternary(cond3, 1.0, -1.0)))


def alpha_22(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * (delta(correlation(high, volume, 5), 5) * rank(stddev(close, 20))))"""
    return -1.0 * (ts_delta(ts_corr(d.high, d.volume, 5), 5) * cs_rank(ts_std(d.close, 20)))


def alpha_23(d: Alpha101Data) -> pd.DataFrame:
    """(((sum(high, 20) / 20) < high) ? (-1 * delta(high, 2)) : 0)"""
    cond = (ts_sum(d.high, 20) / 20.0) < d.high
    return ternary(cond, -1.0 * ts_delta(d.high, 2), 0.0)


def alpha_24(d: Alpha101Data) -> pd.DataFrame:
    """((((delta((sum(close, 100) / 100), 100) / delay(close, 100)) < 0.05) || ((delta((sum(close, 100) / 100), 100) / delay(close, 100)) == 0.05)) ? (-1 * (close - ts_min(close, 100))) : (-1 * delta(close, 3)))"""
    cond = (ts_delta(ts_sum(d.close, 100) / 100.0, 100) / ts_delay(d.close, 100).clip(lower=1e-4)) <= 0.05
    return ternary(cond, -1.0 * (d.close - ts_min(d.close, 100)), -1.0 * ts_delta(d.close, 3))


def alpha_25(d: Alpha101Data) -> pd.DataFrame:
    """rank(((((-1 * returns) * adv20) * vwap) * (high - close)))"""
    return cs_rank((((-1.0 * d.returns) * d.adv(20)) * d.vwap) * (d.high - d.close))


# =============================================================================
# Formulaic Alphas 26 - 50
# =============================================================================

def alpha_26(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * ts_max(correlation(ts_rank(volume, 5), ts_rank(high, 5), 5), 3))"""
    return -1.0 * ts_max(ts_corr(ts_rank(d.volume, 5), ts_rank(d.high, 5), 5), 3)


def alpha_27(d: Alpha101Data) -> pd.DataFrame:
    """((0.5 < rank((sum(correlation(rank(volume), rank(vwap), 6), 2) / 2.0))) ? (-1 * 1) : 1)"""
    c = ts_corr(cs_rank(d.volume), cs_rank(d.vwap), 6)
    cond = cs_rank(ts_sum(c, 2) / 2.0) > 0.5
    return ternary(cond, -1.0, 1.0)


def alpha_28(d: Alpha101Data) -> pd.DataFrame:
    """scale(((correlation(adv20, low, 5) + ((high + low) / 2)) - close))"""
    return cs_scale((ts_corr(d.adv(20), d.low, 5) + ((d.high + d.low) / 2.0)) - d.close)


def alpha_29(d: Alpha101Data) -> pd.DataFrame:
    """(min(product(rank(rank(scale(log(sum(ts_min(rank(rank((-1 * rank(delta((close - 1), 5))))), 2), 1))))), 1), 5) + ts_rank(delay((-1 * returns), 6), 5))"""
    x1 = -1.0 * cs_rank(ts_delta(d.close - 1.0, 5))
    x2 = ts_min(cs_rank(cs_rank(x1)), 2)
    x3 = np.log(ts_sum(x2, 1).clip(lower=1e-4))
    t1 = ts_min(ts_prod(cs_rank(cs_rank(cs_scale(x3))), 1), 5)
    t2 = ts_rank(ts_delay(-1.0 * d.returns, 6), 5)
    return t1 + t2


def alpha_30(d: Alpha101Data) -> pd.DataFrame:
    """(((1.0 - rank(((sign((close - delay(close, 1))) + sign((delay(close, 1) - delay(close, 2)))) + sign((delay(close, 2) - delay(close, 3)))))) * sum(volume, 5)) / sum(volume, 20))"""
    s1 = np.sign(d.close - ts_delay(d.close, 1))
    s2 = np.sign(ts_delay(d.close, 1) - ts_delay(d.close, 2))
    s3 = np.sign(ts_delay(d.close, 2) - ts_delay(d.close, 3))
    num = (1.0 - cs_rank(s1 + s2 + s3)) * ts_sum(d.volume, 5)
    return num / ts_sum(d.volume, 20).clip(lower=1.0)


def alpha_31(d: Alpha101Data) -> pd.DataFrame:
    """((rank(rank(rank(decay_linear((-1 * rank(rank(delta(close, 10)))), 10)))) + rank((-1 * delta(close, 3)))) + sign(scale(correlation(adv20, low, 12))))"""
    t1 = cs_rank(cs_rank(cs_rank(ts_decay_linear(-1.0 * cs_rank(cs_rank(ts_delta(d.close, 10))), 10))))
    t2 = cs_rank(-1.0 * ts_delta(d.close, 3))
    t3 = np.sign(cs_scale(ts_corr(d.adv(20), d.low, 12)))
    return t1 + t2 + t3


def alpha_32(d: Alpha101Data) -> pd.DataFrame:
    """(scale(((sum(close, 7) / 7) - close)) + (20 * scale(correlation(vwap, delay(close, 5), 230))))"""
    t1 = cs_scale((ts_sum(d.close, 7) / 7.0) - d.close)
    t2 = 20.0 * cs_scale(ts_corr(d.vwap, ts_delay(d.close, 5), 230))
    return t1 + t2


def alpha_33(d: Alpha101Data) -> pd.DataFrame:
    """rank((-1 * ((1 - (open / close))^1)))"""
    return cs_rank(-1.0 * (1.0 - (d.open / d.close.clip(lower=1e-4))))


def alpha_34(d: Alpha101Data) -> pd.DataFrame:
    """rank(((1 - rank((stddev(returns, 2) / stddev(returns, 5)))) + (1 - rank(delta(close, 1)))))"""
    t1 = 1.0 - cs_rank(ts_std(d.returns, 2) / ts_std(d.returns, 5).clip(lower=1e-4))
    t2 = 1.0 - cs_rank(ts_delta(d.close, 1))
    return cs_rank(t1 + t2)


def alpha_35(d: Alpha101Data) -> pd.DataFrame:
    """((Ts_Rank(volume, 32) * (1 - Ts_Rank(((close + high) - low), 16))) * (1 - Ts_Rank(returns, 32)))"""
    return (ts_rank(d.volume, 32) * (1.0 - ts_rank((d.close + d.high) - d.low, 16))) * (1.0 - ts_rank(d.returns, 32))


def alpha_36(d: Alpha101Data) -> pd.DataFrame:
    """(((((2.21 * rank(correlation((close - open), delay(volume, 1), 15))) + (0.7 * rank((open - close)))) + (0.73 * rank(Ts_Rank(delay((-1 * returns), 6), 5)))) + rank(abs(correlation(vwap, adv20, 6)))) + (0.6 * rank((((sum(close, 200) / 200) - open) * (close - open)))))"""
    t1 = 2.21 * cs_rank(ts_corr(d.close - d.open, ts_delay(d.volume, 1), 15))
    t2 = 0.7 * cs_rank(d.open - d.close)
    t3 = 0.73 * cs_rank(ts_rank(ts_delay(-1.0 * d.returns, 6), 5))
    t4 = cs_rank(ts_corr(d.vwap, d.adv(20), 6).abs())
    t5 = 0.6 * cs_rank(((ts_sum(d.close, 200) / 200.0) - d.open) * (d.close - d.open))
    return t1 + t2 + t3 + t4 + t5


def alpha_37(d: Alpha101Data) -> pd.DataFrame:
    """(rank(correlation(delay((open - close), 1), close, 200)) + rank((open - close)))"""
    return cs_rank(ts_corr(ts_delay(d.open - d.close, 1), d.close, 200)) + cs_rank(d.open - d.close)


def alpha_38(d: Alpha101Data) -> pd.DataFrame:
    """((-1 * rank(Ts_Rank(close, 10))) * rank((close / open)))"""
    return (-1.0 * cs_rank(ts_rank(d.close, 10))) * cs_rank(d.close / d.open.clip(lower=1e-4))


def alpha_39(d: Alpha101Data) -> pd.DataFrame:
    """((-1 * rank((delta(close, 7) * (1 - rank(decay_linear((volume / adv20), 9)))))) * (1 + rank(sum(returns, 250))))"""
    vol_ratio = d.volume / d.adv(20).clip(lower=1e-4)
    t1 = -1.0 * cs_rank(ts_delta(d.close, 7) * (1.0 - cs_rank(ts_decay_linear(vol_ratio, 9))))
    t2 = 1.0 + cs_rank(ts_sum(d.returns, 250))
    return t1 * t2


def alpha_40(d: Alpha101Data) -> pd.DataFrame:
    """((-1 * rank(stddev(high, 10))) * correlation(high, volume, 10))"""
    return (-1.0 * cs_rank(ts_std(d.high, 10))) * ts_corr(d.high, d.volume, 10)


def alpha_41(d: Alpha101Data) -> pd.DataFrame:
    """(((high * low)^0.5) - vwap)"""
    return np.sqrt((d.high * d.low).clip(lower=0.0)) - d.vwap


def alpha_42(d: Alpha101Data) -> pd.DataFrame:
    """(rank((vwap - close)) / rank((vwap + close)))"""
    return cs_rank(d.vwap - d.close) / cs_rank(d.vwap + d.close).clip(lower=1e-4)


def alpha_43(d: Alpha101Data) -> pd.DataFrame:
    """(ts_rank((volume / adv20), 20) * ts_rank((-1 * delta(close, 7)), 8))"""
    return ts_rank(d.volume / d.adv(20).clip(lower=1e-4), 20) * ts_rank(-1.0 * ts_delta(d.close, 7), 8)


def alpha_44(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * correlation(high, rank(volume), 5))"""
    return -1.0 * ts_corr(d.high, cs_rank(d.volume), 5)


def alpha_45(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * ((rank((sum(delay(close, 5), 20) / 20)) * correlation(close, volume, 2)) * rank(correlation(sum(close, 5), sum(close, 20), 2))))"""
    t1 = cs_rank(ts_sum(ts_delay(d.close, 5), 20) / 20.0)
    t2 = ts_corr(d.close, d.volume, 2)
    t3 = cs_rank(ts_corr(ts_sum(d.close, 5), ts_sum(d.close, 20), 2))
    return -1.0 * (t1 * t2 * t3)


def alpha_46(d: Alpha101Data) -> pd.DataFrame:
    """((0.25 < (((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10))) ? (-1 * 1) : (((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10)) < 0) ? 1 : ((-1 * 1) * (close - delay(close, 1)))))"""
    diff = ((ts_delay(d.close, 20) - ts_delay(d.close, 10)) / 10.0) - ((ts_delay(d.close, 10) - d.close) / 10.0)
    return ternary(diff > 0.25, -1.0, ternary(diff < 0.0, 1.0, -1.0 * (d.close - ts_delay(d.close, 1))))


def alpha_47(d: Alpha101Data) -> pd.DataFrame:
    """((((rank((1 / close)) * volume) / adv20) * ((high * rank((high - close))) / (sum(high, 5) / 5))) - rank((vwap - delay(vwap, 5))))"""
    term1 = (cs_rank(1.0 / d.close.clip(lower=1e-4)) * d.volume) / d.adv(20).clip(lower=1e-4)
    term2 = (d.high * cs_rank(d.high - d.close)) / (ts_sum(d.high, 5) / 5.0).clip(lower=1e-4)
    term3 = cs_rank(d.vwap - ts_delay(d.vwap, 5))
    return (term1 * term2) - term3


def alpha_48(d: Alpha101Data) -> pd.DataFrame:
    """(indneutralize(((correlation(delta(close, 1), delta(delay(close, 1), 1), 250) * delta(close, 1)) / close), IndClass.subindustry) / sum(((delta(close, 1) / delay(close, 1))^2), 250))"""
    c_diff = ts_delta(d.close, 1)
    c_diff_lag = ts_delta(ts_delay(d.close, 1), 1)
    num = cs_indneutralize((ts_corr(c_diff, c_diff_lag, 250) * c_diff) / d.close.clip(lower=1e-4), d.ind_class)
    denom = ts_sum((c_diff / ts_delay(d.close, 1).clip(lower=1e-4)) ** 2, 250).clip(lower=1e-6)
    return num / denom


def alpha_49(d: Alpha101Data) -> pd.DataFrame:
    """(((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10)) < (-1 * 0.1)) ? 1 : ((-1 * 1) * (close - delay(close, 1))))"""
    diff = ((ts_delay(d.close, 20) - ts_delay(d.close, 10)) / 10.0) - ((ts_delay(d.close, 10) - d.close) / 10.0)
    return ternary(diff < -0.1, 1.0, -1.0 * (d.close - ts_delay(d.close, 1)))


def alpha_50(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * ts_max(rank(correlation(rank(volume), rank(vwap), 5)), 5))"""
    return -1.0 * ts_max(cs_rank(ts_corr(cs_rank(d.volume), cs_rank(d.vwap), 5)), 5)


# =============================================================================
# Formulaic Alphas 51 - 75
# =============================================================================

def alpha_51(d: Alpha101Data) -> pd.DataFrame:
    """(((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10)) < (-1 * 0.05)) ? 1 : ((-1 * 1) * (close - delay(close, 1))))"""
    diff = ((ts_delay(d.close, 20) - ts_delay(d.close, 10)) / 10.0) - ((ts_delay(d.close, 10) - d.close) / 10.0)
    return ternary(diff < -0.05, 1.0, -1.0 * (d.close - ts_delay(d.close, 1)))


def alpha_52(d: Alpha101Data) -> pd.DataFrame:
    """((((-1 * ts_min(low, 5)) + delay(ts_min(low, 5), 5)) * rank(((sum(returns, 240) - sum(returns, 20)) / 220))) * ts_rank(volume, 5))"""
    t1 = -1.0 * ts_min(d.low, 5) + ts_delay(ts_min(d.low, 5), 5)
    t2 = cs_rank((ts_sum(d.returns, 240) - ts_sum(d.returns, 20)) / 220.0)
    t3 = ts_rank(d.volume, 5)
    return t1 * t2 * t3


def alpha_53(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * delta((((close - low) - (high - close)) / (close - low)), 9))"""
    wick_imbalance = ((d.close - d.low) - (d.high - d.close)) / (d.close - d.low + 1e-4)
    return -1.0 * ts_delta(wick_imbalance, 9)


def alpha_54(d: Alpha101Data) -> pd.DataFrame:
    """((-1 * ((low - close) * (open^5))) / ((low - high) * (close^5)))"""
    num = -1.0 * (d.low - d.close) * (d.open ** 5)
    denom = ((d.low - d.high) * (d.close ** 5)).replace(0.0, np.nan).fillna(1e-6)
    return num / denom


def alpha_55(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * correlation(rank(((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12)))), rank(volume), 6))"""
    stoch = (d.close - ts_min(d.low, 12)) / (ts_max(d.high, 12) - ts_min(d.low, 12) + 1e-6)
    return -1.0 * ts_corr(cs_rank(stoch), cs_rank(d.volume), 6)


def alpha_56(d: Alpha101Data) -> pd.DataFrame:
    """(0 - (1 * (rank((sum(returns, 10) / sum(sum(returns, 2), 3))) * rank((returns * cap)))))"""
    t1 = cs_rank(ts_sum(d.returns, 10) / ts_sum(ts_sum(d.returns, 2), 3).clip(lower=1e-6))
    t2 = cs_rank(d.returns * d.cap)
    return 0.0 - (t1 * t2)


def alpha_57(d: Alpha101Data) -> pd.DataFrame:
    """(0 - (1 * ((close - vwap) / decay_linear(rank(ts_argmax(close, 30)), 2))))"""
    denom = ts_decay_linear(cs_rank(ts_argmax(d.close, 30)), 2).clip(lower=1e-4)
    return 0.0 - ((d.close - d.vwap) / denom)


def alpha_58(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndClass.sector), volume, 3.92795), 7.89291), 5.50322))"""
    c = ts_corr(cs_indneutralize(d.vwap, d.ind_class), d.volume, 3.92795)
    return -1.0 * ts_rank(ts_decay_linear(c, 7.89291), 5.50322)


def alpha_59(d: Alpha101Data) -> pd.DataFrame:
    """(-1 * Ts_Rank(decay_linear(correlation(IndNeutralize(((vwap * 0.728317) + (vwap * (1 - 0.728317))), IndClass.industry), volume, 4.25197), 16.2289), 8.19648))"""
    vw = cs_indneutralize(d.vwap * 0.728317 + d.vwap * (1.0 - 0.728317), d.ind_class)
    c = ts_corr(vw, d.volume, 4.25197)
    return -1.0 * ts_rank(ts_decay_linear(c, 16.2289), 8.19648)


def alpha_60(d: Alpha101Data) -> pd.DataFrame:
    """(0 - (1 * ((2 * scale(rank(((((close - low) - (high - close)) / (high - low)) * volume)))) - scale(rank(ts_argmax(close, 10))))))"""
    flow = (((d.close - d.low) - (d.high - d.close)) / (d.high - d.low + 1e-6)) * d.volume
    t1 = 2.0 * cs_scale(cs_rank(flow))
    t2 = cs_scale(cs_rank(ts_argmax(d.close, 10)))
    return 0.0 - (t1 - t2)


def alpha_61(d: Alpha101Data) -> pd.DataFrame:
    """(rank((vwap - ts_min(vwap, 16.1219))) < rank(correlation(vwap, adv180, 17.9282)))"""
    cond = cs_rank(d.vwap - ts_min(d.vwap, 16.1219)) < cs_rank(ts_corr(d.vwap, d.adv(180), 17.9282))
    return ternary(cond, 1.0, 0.0)


def alpha_62(d: Alpha101Data) -> pd.DataFrame:
    """((rank(correlation(vwap, sum(adv20, 22.4101), 9.91009)) < rank(((rank(open) + rank(open)) < (rank(((high + low) / 2)) + rank(high))))) * -1)"""
    cond1 = cs_rank(d.open) + cs_rank(d.open) < cs_rank((d.high + d.low) / 2.0) + cs_rank(d.high)
    cond2 = cs_rank(ts_corr(d.vwap, ts_sum(d.adv(20), 22.4101), 9.91009)) < cs_rank(ternary(cond1, 1.0, 0.0))
    return ternary(cond2, -1.0, 0.0)


def alpha_63(d: Alpha101Data) -> pd.DataFrame:
    """((rank(decay_linear(delta(IndNeutralize(close, IndClass.industry), 2.25164), 8.22237)) - rank(decay_linear(correlation(((vwap * 0.318108) + (open * (1 - 0.318108))), sum(adv180, 37.2467), 13.557), 12.2883))) * -1)"""
    t1 = cs_rank(ts_decay_linear(ts_delta(cs_indneutralize(d.close, d.ind_class), 2.25164), 8.22237))
    v_mix = d.vwap * 0.318108 + d.open * (1.0 - 0.318108)
    t2 = cs_rank(ts_decay_linear(ts_corr(v_mix, ts_sum(d.adv(180), 37.2467), 13.557), 12.2883))
    return -1.0 * (t1 - t2)


def alpha_64(d: Alpha101Data) -> pd.DataFrame:
    """((rank(correlation(sum(((open * 0.178404) + (low * (1 - 0.178404))), 12.7054), sum(adv120, 12.7054), 16.6208)) < rank(delta(((((high + low) / 2) * 0.178404) + (vwap * (1 - 0.178404))), 3.69741))) * -1)"""
    x1 = ts_sum(d.open * 0.178404 + d.low * (1.0 - 0.178404), 12.7054)
    c1 = cs_rank(ts_corr(x1, ts_sum(d.adv(120), 12.7054), 16.6208))
    x2 = ((d.high + d.low) / 2.0) * 0.178404 + d.vwap * (1.0 - 0.178404)
    c2 = cs_rank(ts_delta(x2, 3.69741))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_65(d: Alpha101Data) -> pd.DataFrame:
    """((rank(correlation(((open * 0.00817205) + (vwap * (1 - 0.00817205))), sum(adv60, 8.6911), 6.40374)) < rank((open - ts_min(open, 13.635)))) * -1)"""
    x = d.open * 0.00817205 + d.vwap * (1.0 - 0.00817205)
    c1 = cs_rank(ts_corr(x, ts_sum(d.adv(60), 8.6911), 6.40374))
    c2 = cs_rank(d.open - ts_min(d.open, 13.635))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_66(d: Alpha101Data) -> pd.DataFrame:
    """((rank(decay_linear(delta(vwap, 3.51013), 7.23052)) + Ts_Rank(decay_linear(((((low * 0.96633) + (low * (1 - 0.96633))) - vwap) / (open - ((high + low) / 2))), 11.4157), 6.72611)) * -1)"""
    t1 = cs_rank(ts_decay_linear(ts_delta(d.vwap, 3.51013), 7.23052))
    ratio = (d.low * 0.96633 + d.low * (1.0 - 0.96633) - d.vwap) / (d.open - (d.high + d.low) / 2.0 + 1e-6)
    t2 = ts_rank(ts_decay_linear(ratio, 11.4157), 6.72611)
    return -1.0 * (t1 + t2)


def alpha_67(d: Alpha101Data) -> pd.DataFrame:
    """((rank((high - ts_min(high, 2.14593)))^rank(correlation(IndNeutralize(vwap, IndClass.sector), IndNeutralize(adv20, IndClass.subindustry), 6.02936))) * -1)"""
    b = cs_rank(d.high - ts_min(d.high, 2.14593))
    p = cs_rank(ts_corr(cs_indneutralize(d.vwap, d.ind_class), cs_indneutralize(d.adv(20), d.ind_class), 6.02936))
    return -1.0 * signed_power(b, p)


def alpha_68(d: Alpha101Data) -> pd.DataFrame:
    """((Ts_Rank(correlation(rank(high), rank(adv15), 8.91644), 13.9333) < rank(delta(((close * 0.518371) + (low * (1 - 0.518371))), 1.06157))) * -1)"""
    c1 = ts_rank(ts_corr(cs_rank(d.high), cs_rank(d.adv(15)), 8.91644), 13.9333)
    c2 = cs_rank(ts_delta(d.close * 0.518371 + d.low * (1.0 - 0.518371), 1.06157))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_69(d: Alpha101Data) -> pd.DataFrame:
    """((rank(ts_max(delta(IndNeutralize(vwap, IndClass.industry), 2.72412), 4.79344))^Ts_Rank(correlation(((close * 0.490655) + (vwap * (1 - 0.490655))), adv20, 4.92416), 9.0615)) * -1)"""
    b = cs_rank(ts_max(ts_delta(cs_indneutralize(d.vwap, d.ind_class), 2.72412), 4.79344))
    p = ts_rank(ts_corr(d.close * 0.490655 + d.vwap * (1.0 - 0.490655), d.adv(20), 4.92416), 9.0615)
    return -1.0 * signed_power(b, p)


def alpha_70(d: Alpha101Data) -> pd.DataFrame:
    """((rank(delta(vwap, 1.29456))^Ts_Rank(correlation(IndNeutralize(close, IndClass.industry), adv50, 17.8256), 17.9171)) * -1)"""
    b = cs_rank(ts_delta(d.vwap, 1.29456))
    p = ts_rank(ts_corr(cs_indneutralize(d.close, d.ind_class), d.adv(50), 17.8256), 17.9171)
    return -1.0 * signed_power(b, p)


def alpha_71(d: Alpha101Data) -> pd.DataFrame:
    """max(Ts_Rank(decay_linear(correlation(Ts_Rank(close, 3.43976), Ts_Rank(adv180, 12.0647), 18.0175), 4.20501), 15.6948), Ts_Rank(decay_linear((rank(((low + open) - (vwap + vwap)))^2), 16.4662), 4.4388))"""
    c = ts_corr(ts_rank(d.close, 3.43976), ts_rank(d.adv(180), 12.0647), 18.0175)
    t1 = ts_rank(ts_decay_linear(c, 4.20501), 15.6948)
    diff = cs_rank((d.low + d.open) - 2.0 * d.vwap)
    t2 = ts_rank(ts_decay_linear(signed_power(diff, 2.0), 16.4662), 4.4388)
    return ternary(t1 > t2, t1, t2)


def alpha_72(d: Alpha101Data) -> pd.DataFrame:
    """(rank(decay_linear(correlation(((high + low) / 2), adv40, 8.93345), 10.1519)) / rank(decay_linear(correlation(Ts_Rank(vwap, 3.72469), Ts_Rank(volume, 18.5188), 6.86671), 2.95011)))"""
    num = cs_rank(ts_decay_linear(ts_corr((d.high + d.low) / 2.0, d.adv(40), 8.93345), 10.1519))
    denom = cs_rank(ts_decay_linear(ts_corr(ts_rank(d.vwap, 3.72469), ts_rank(d.volume, 18.5188), 6.86671), 2.95011)).clip(lower=1e-4)
    return num / denom


def alpha_73(d: Alpha101Data) -> pd.DataFrame:
    """(max(rank(decay_linear(delta(vwap, 4.72775), 2.91864)), Ts_Rank(decay_linear(((delta(((open * 0.147155) + (low * (1 - 0.147155))), 2.03608) / ((open * 0.147155) + (low * (1 - 0.147155)))) * -1), 3.33829), 16.7411)) * -1)"""
    t1 = cs_rank(ts_decay_linear(ts_delta(d.vwap, 4.72775), 2.91864))
    x = d.open * 0.147155 + d.low * (1.0 - 0.147155)
    ratio = -1.0 * (ts_delta(x, 2.03608) / x.clip(lower=1e-4))
    t2 = ts_rank(ts_decay_linear(ratio, 3.33829), 16.7411)
    return -1.0 * ternary(t1 > t2, t1, t2)


def alpha_74(d: Alpha101Data) -> pd.DataFrame:
    """((rank(correlation(close, sum(adv30, 37.4843), 15.1365)) < rank(correlation(rank(((high * 0.0261661) + (vwap * (1 - 0.0261661)))), rank(volume), 11.4791))) * -1)"""
    c1 = cs_rank(ts_corr(d.close, ts_sum(d.adv(30), 37.4843), 15.1365))
    h_mix = d.high * 0.0261661 + d.vwap * (1.0 - 0.0261661)
    c2 = cs_rank(ts_corr(cs_rank(h_mix), cs_rank(d.volume), 11.4791))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_75(d: Alpha101Data) -> pd.DataFrame:
    """(rank(correlation(vwap, volume, 4.24304)) < rank(correlation(rank(low), rank(adv50), 12.4413)))"""
    c1 = cs_rank(ts_corr(d.vwap, d.volume, 4.24304))
    c2 = cs_rank(ts_corr(cs_rank(d.low), cs_rank(d.adv(50)), 12.4413))
    return ternary(c1 < c2, 1.0, 0.0)


# =============================================================================
# Formulaic Alphas 76 - 101
# =============================================================================

def alpha_76(d: Alpha101Data) -> pd.DataFrame:
    """(max(rank(decay_linear(delta(vwap, 1.24383), 11.8259)), Ts_Rank(decay_linear(Ts_Rank(correlation(IndNeutralize(low, IndClass.sector), adv81, 8.14941), 19.569), 17.1543), 19.383)) * -1)"""
    t1 = cs_rank(ts_decay_linear(ts_delta(d.vwap, 1.24383), 11.8259))
    c = ts_corr(cs_indneutralize(d.low, d.ind_class), d.adv(81), 8.14941)
    t2 = ts_rank(ts_decay_linear(ts_rank(c, 19.569), 17.1543), 19.383)
    return -1.0 * ternary(t1 > t2, t1, t2)


def alpha_77(d: Alpha101Data) -> pd.DataFrame:
    """min(rank(decay_linear(((((high + low) / 2) + high) - (vwap + high)), 20.0451)), rank(decay_linear(correlation(((high + low) / 2), adv40, 3.1614), 5.64125)))"""
    x1 = ((d.high + d.low) / 2.0 + d.high) - (d.vwap + d.high)
    t1 = cs_rank(ts_decay_linear(x1, 20.0451))
    c = ts_corr((d.high + d.low) / 2.0, d.adv(40), 3.1614)
    t2 = cs_rank(ts_decay_linear(c, 5.64125))
    return ternary(t1 < t2, t1, t2)


def alpha_78(d: Alpha101Data) -> pd.DataFrame:
    """(rank(correlation(sum(((low * 0.352233) + (vwap * (1 - 0.352233))), 19.7428), sum(adv40, 19.7428), 6.83313))^rank(correlation(rank(vwap), rank(volume), 5.77492)))"""
    x = d.low * 0.352233 + d.vwap * (1.0 - 0.352233)
    b = cs_rank(ts_corr(ts_sum(x, 19.7428), ts_sum(d.adv(40), 19.7428), 6.83313))
    p = cs_rank(ts_corr(cs_rank(d.vwap), cs_rank(d.volume), 5.77492))
    return signed_power(b, p)


def alpha_79(d: Alpha101Data) -> pd.DataFrame:
    """(rank(delta(IndNeutralize(((close * 0.60733) + (open * (1 - 0.60733))), IndClass.sector), 1.23438)) < rank(correlation(Ts_Rank(vwap, 3.60973), Ts_Rank(adv150, 9.18637), 14.6644)))"""
    x = cs_indneutralize(d.close * 0.60733 + d.open * (1.0 - 0.60733), d.ind_class)
    c1 = cs_rank(ts_delta(x, 1.23438))
    c2 = cs_rank(ts_corr(ts_rank(d.vwap, 3.60973), ts_rank(d.adv(150), 9.18637), 14.6644))
    return ternary(c1 < c2, 1.0, 0.0)


def alpha_80(d: Alpha101Data) -> pd.DataFrame:
    """((rank(Sign(delta(IndNeutralize(((open * 0.868128) + (high * (1 - 0.868128))), IndClass.industry), 4.04545)))^Ts_Rank(correlation(high, adv10, 5.11456), 5.53756)) * -1)"""
    x = cs_indneutralize(d.open * 0.868128 + d.high * (1.0 - 0.868128), d.ind_class)
    b = cs_rank(np.sign(ts_delta(x, 4.04545)))
    p = ts_rank(ts_corr(d.high, d.adv(10), 5.11456), 5.53756)
    return -1.0 * signed_power(b, p)


def alpha_81(d: Alpha101Data) -> pd.DataFrame:
    """((rank(Log(product(rank((rank(correlation(vwap, sum(adv10, 49.6054), 8.47743))^4)), 14.9655))) < rank(correlation(rank(vwap), rank(volume), 5.07914))) * -1)"""
    c = cs_rank(ts_corr(d.vwap, ts_sum(d.adv(10), 49.6054), 8.47743))
    inner = cs_rank(signed_power(c, 4.0))
    p_val = ts_prod(inner, 14.9655).clip(lower=1e-6)
    c1 = cs_rank(np.log(p_val))
    c2 = cs_rank(ts_corr(cs_rank(d.vwap), cs_rank(d.volume), 5.07914))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_82(d: Alpha101Data) -> pd.DataFrame:
    """(min(rank(decay_linear(delta(open, 1.46063), 14.8717)), Ts_Rank(decay_linear(correlation(IndNeutralize(volume, IndClass.sector), ((open * 0.634196) + (open * (1 - 0.634196))), 17.4842), 6.92131), 13.4283)) * -1)"""
    t1 = cs_rank(ts_decay_linear(ts_delta(d.open, 1.46063), 14.8717))
    o_mix = d.open * 0.634196 + d.open * (1.0 - 0.634196)
    c = ts_corr(cs_indneutralize(d.volume, d.ind_class), o_mix, 17.4842)
    t2 = ts_rank(ts_decay_linear(c, 6.92131), 13.4283)
    return -1.0 * ternary(t1 < t2, t1, t2)


def alpha_83(d: Alpha101Data) -> pd.DataFrame:
    """((rank(delay(((high - low) / (sum(close, 5) / 5)), 2)) * rank(rank(volume))) / (((high - low) / (sum(close, 5) / 5)) / (vwap - close)))"""
    spread = (d.high - d.low) / (ts_sum(d.close, 5) / 5.0).clip(lower=1e-4)
    num = cs_rank(ts_delay(spread, 2)) * cs_rank(cs_rank(d.volume))
    denom = (spread / (d.vwap - d.close + 1e-4)).clip(lower=1e-4)
    return num / denom


def alpha_84(d: Alpha101Data) -> pd.DataFrame:
    """SignedPower(Ts_Rank((vwap - ts_max(vwap, 15.3217)), 20.7127), delta(close, 4.96796))"""
    b = ts_rank(d.vwap - ts_max(d.vwap, 15.3217), 20.7127)
    p = ts_delta(d.close, 4.96796)
    return signed_power(b, p)


def alpha_85(d: Alpha101Data) -> pd.DataFrame:
    """(rank(correlation(((high * 0.876703) + (close * (1 - 0.876703))), adv30, 9.61331))^rank(correlation(Ts_Rank(((high + low) / 2), 3.70596), Ts_Rank(volume, 10.1595), 7.11408)))"""
    x = d.high * 0.876703 + d.close * (1.0 - 0.876703)
    b = cs_rank(ts_corr(x, d.adv(30), 9.61331))
    p = cs_rank(ts_corr(ts_rank((d.high + d.low) / 2.0, 3.70596), ts_rank(d.volume, 10.1595), 7.11408))
    return signed_power(b, p)


def alpha_86(d: Alpha101Data) -> pd.DataFrame:
    """((Ts_Rank(correlation(close, sum(adv20, 14.7444), 6.00049), 20.4195) < rank(((open + close) - (vwap + open)))) * -1)"""
    c1 = ts_rank(ts_corr(d.close, ts_sum(d.adv(20), 14.7444), 6.00049), 20.4195)
    c2 = cs_rank((d.open + d.close) - (d.vwap + d.open))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_87(d: Alpha101Data) -> pd.DataFrame:
    """(max(rank(decay_linear(delta(((close * 0.369701) + (vwap * (1 - 0.369701))), 1.91233), 2.65461)), Ts_Rank(decay_linear(abs(correlation(IndNeutralize(adv81, IndClass.industry), close, 13.4132)), 4.89768), 14.4535)) * -1)"""
    x = d.close * 0.369701 + d.vwap * (1.0 - 0.369701)
    t1 = cs_rank(ts_decay_linear(ts_delta(x, 1.91233), 2.65461))
    c = ts_corr(cs_indneutralize(d.adv(81), d.ind_class), d.close, 13.4132).abs()
    t2 = ts_rank(ts_decay_linear(c, 4.89768), 14.4535)
    return -1.0 * ternary(t1 > t2, t1, t2)


def alpha_88(d: Alpha101Data) -> pd.DataFrame:
    """min(rank(decay_linear(((rank(open) + rank(low)) - (rank(high) + rank(close))), 8.06882)), Ts_Rank(decay_linear(correlation(Ts_Rank(close, 8.44728), Ts_Rank(adv60, 20.6966), 8.01266), 6.65053), 2.61957))"""
    spread = (cs_rank(d.open) + cs_rank(d.low)) - (cs_rank(d.high) + cs_rank(d.close))
    t1 = cs_rank(ts_decay_linear(spread, 8.06882))
    c = ts_corr(ts_rank(d.close, 8.44728), ts_rank(d.adv(60), 20.6966), 8.01266)
    t2 = ts_rank(ts_decay_linear(c, 6.65053), 2.61957)
    return ternary(t1 < t2, t1, t2)


def alpha_89(d: Alpha101Data) -> pd.DataFrame:
    """(Ts_Rank(decay_linear(correlation(((low * 0.967285) + (low * (1 - 0.967285))), adv10, 6.94279), 5.51607), 3.79744) - Ts_Rank(decay_linear(delta(IndNeutralize(vwap, IndClass.industry), 3.48158), 10.1466), 15.3012))"""
    x = d.low * 0.967285 + d.low * (1.0 - 0.967285)
    c = ts_corr(x, d.adv(10), 6.94279)
    t1 = ts_rank(ts_decay_linear(c, 5.51607), 3.79744)
    t2 = ts_rank(ts_decay_linear(ts_delta(cs_indneutralize(d.vwap, d.ind_class), 3.48158), 10.1466), 15.3012)
    return t1 - t2


def alpha_90(d: Alpha101Data) -> pd.DataFrame:
    """((rank((close - ts_max(close, 4.66719)))^Ts_Rank(correlation(IndNeutralize(adv40, IndClass.subindustry), low, 5.38375), 3.21856)) * -1)"""
    b = cs_rank(d.close - ts_max(d.close, 4.66719))
    p = ts_rank(ts_corr(cs_indneutralize(d.adv(40), d.ind_class), d.low, 5.38375), 3.21856)
    return -1.0 * signed_power(b, p)


def alpha_91(d: Alpha101Data) -> pd.DataFrame:
    """((Ts_Rank(decay_linear(decay_linear(correlation(IndNeutralize(close, IndClass.industry), volume, 9.74928), 16.398), 3.83219), 4.8667) - rank(decay_linear(correlation(vwap, adv30, 4.01303), 2.6809))) * -1)"""
    c1 = ts_corr(cs_indneutralize(d.close, d.ind_class), d.volume, 9.74928)
    t1 = ts_rank(ts_decay_linear(ts_decay_linear(c1, 16.398), 3.83219), 4.8667)
    t2 = cs_rank(ts_decay_linear(ts_corr(d.vwap, d.adv(30), 4.01303), 2.6809))
    return -1.0 * (t1 - t2)


def alpha_92(d: Alpha101Data) -> pd.DataFrame:
    """min(Ts_Rank(decay_linear(((((high + low) / 2) + close) < (low + open)), 14.7221), 18.8683), Ts_Rank(decay_linear(correlation(rank(low), rank(adv30), 7.58555), 6.94024), 6.80584))"""
    cond = ((d.high + d.low) / 2.0 + d.close) < (d.low + d.open)
    t1 = ts_rank(ts_decay_linear(ternary(cond, 1.0, 0.0), 14.7221), 18.8683)
    c = ts_corr(cs_rank(d.low), cs_rank(d.adv(30)), 7.58555)
    t2 = ts_rank(ts_decay_linear(c, 6.94024), 6.80584)
    return ternary(t1 < t2, t1, t2)


def alpha_93(d: Alpha101Data) -> pd.DataFrame:
    """(Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndClass.industry), adv81, 17.4193), 19.848), 7.54455) / rank(decay_linear(delta(((close * 0.524434) + (vwap * (1 - 0.524434))), 2.77377), 16.2664)))"""
    c = ts_corr(cs_indneutralize(d.vwap, d.ind_class), d.adv(81), 17.4193)
    num = ts_rank(ts_decay_linear(c, 19.848), 7.54455)
    x = d.close * 0.524434 + d.vwap * (1.0 - 0.524434)
    denom = cs_rank(ts_decay_linear(ts_delta(x, 2.77377), 16.2664)).clip(lower=1e-4)
    return num / denom


def alpha_94(d: Alpha101Data) -> pd.DataFrame:
    """((rank((vwap - ts_min(vwap, 11.5783)))^Ts_Rank(correlation(Ts_Rank(vwap, 19.6462), Ts_Rank(adv60, 4.02992), 18.0926), 2.70756)) * -1)"""
    b = cs_rank(d.vwap - ts_min(d.vwap, 11.5783))
    p = ts_rank(ts_corr(ts_rank(d.vwap, 19.6462), ts_rank(d.adv(60), 4.02992), 18.0926), 2.70756)
    return -1.0 * signed_power(b, p)


def alpha_95(d: Alpha101Data) -> pd.DataFrame:
    """(rank((open - ts_min(open, 12.4105))) < Ts_Rank((rank(correlation(sum(((high + low) / 2), 19.1351), sum(adv40, 19.1351), 12.8742))^5), 11.7584))"""
    c1 = cs_rank(d.open - ts_min(d.open, 12.4105))
    corr = ts_corr(ts_sum((d.high + d.low) / 2.0, 19.1351), ts_sum(d.adv(40), 19.1351), 12.8742)
    c2 = ts_rank(signed_power(cs_rank(corr), 5.0), 11.7584)
    return ternary(c1 < c2, 1.0, 0.0)


def alpha_96(d: Alpha101Data) -> pd.DataFrame:
    """(max(Ts_Rank(decay_linear(correlation(rank(vwap), rank(volume), 3.83878), 4.16783), 8.38151), Ts_Rank(decay_linear(Ts_ArgMax(correlation(Ts_Rank(close, 7.45404), Ts_Rank(adv60, 4.13242), 3.65459), 12.6556), 14.0365), 13.4143)) * -1)"""
    c1 = ts_corr(cs_rank(d.vwap), cs_rank(d.volume), 3.83878)
    t1 = ts_rank(ts_decay_linear(c1, 4.16783), 8.38151)
    c2 = ts_corr(ts_rank(d.close, 7.45404), ts_rank(d.adv(60), 4.13242), 3.65459)
    t2 = ts_rank(ts_decay_linear(ts_argmax(c2, 12.6556), 14.0365), 13.4143)
    return -1.0 * ternary(t1 > t2, t1, t2)


def alpha_97(d: Alpha101Data) -> pd.DataFrame:
    """((rank(decay_linear(delta(IndNeutralize(((low * 0.721001) + (vwap * (1 - 0.721001))), IndClass.industry), 3.3705), 20.4523)) - Ts_Rank(decay_linear(Ts_Rank(correlation(Ts_Rank(low, 7.87871), Ts_Rank(adv60, 17.255), 4.97547), 18.5925), 15.7152), 6.71659)) * -1)"""
    x = cs_indneutralize(d.low * 0.721001 + d.vwap * (1.0 - 0.721001), d.ind_class)
    t1 = cs_rank(ts_decay_linear(ts_delta(x, 3.3705), 20.4523))
    c = ts_corr(ts_rank(d.low, 7.87871), ts_rank(d.adv(60), 17.255), 4.97547)
    t2 = ts_rank(ts_decay_linear(ts_rank(c, 18.5925), 15.7152), 6.71659)
    return -1.0 * (t1 - t2)


def alpha_98(d: Alpha101Data) -> pd.DataFrame:
    """(rank(decay_linear(correlation(vwap, sum(adv5, 26.4719), 4.58418), 7.18088)) - rank(decay_linear(Ts_Rank(Ts_ArgMin(correlation(rank(open), rank(adv15), 20.8187), 8.62571), 6.95668), 8.07206)))"""
    c1 = ts_corr(d.vwap, ts_sum(d.adv(5), 26.4719), 4.58418)
    t1 = cs_rank(ts_decay_linear(c1, 7.18088))
    c2 = ts_corr(cs_rank(d.open), cs_rank(d.adv(15)), 20.8187)
    t2 = cs_rank(ts_decay_linear(ts_rank(ts_argmin(c2, 8.62571), 6.95668), 8.07206))
    return t1 - t2


def alpha_99(d: Alpha101Data) -> pd.DataFrame:
    """((rank(correlation(sum(((high + low) / 2), 19.8975), sum(adv60, 19.8975), 8.8136)) < rank(correlation(low, volume, 6.28259))) * -1)"""
    c1 = cs_rank(ts_corr(ts_sum((d.high + d.low) / 2.0, 19.8975), ts_sum(d.adv(60), 19.8975), 8.8136))
    c2 = cs_rank(ts_corr(d.low, d.volume, 6.28259))
    return ternary(c1 < c2, -1.0, 0.0)


def alpha_100(d: Alpha101Data) -> pd.DataFrame:
    """(0 - (1 * (((1.5 * scale(indneutralize(indneutralize(rank(((((close - low) - (high - close)) / (high - low)) * volume)), IndClass.subindustry), IndClass.subindustry))) - scale(indneutralize((correlation(close, rank(adv20), 5) - rank(ts_argmin(close, 30))), IndClass.subindustry))) * (volume / adv20))))"""
    flow = cs_rank((((d.close - d.low) - (d.high - d.close)) / (d.high - d.low + 1e-6)) * d.volume)
    neu1 = cs_indneutralize(cs_indneutralize(flow, d.ind_class), d.ind_class)
    s1 = 1.5 * cs_scale(neu1)
    diff = ts_corr(d.close, cs_rank(d.adv(20)), 5) - cs_rank(ts_argmin(d.close, 30))
    s2 = cs_scale(cs_indneutralize(diff, d.ind_class))
    vol_ratio = d.volume / d.adv(20).clip(lower=1e-4)
    return 0.0 - ((s1 - s2) * vol_ratio)


def alpha_101(d: Alpha101Data) -> pd.DataFrame:
    """((close - open) / ((high - low) + .001))"""
    return (d.close - d.open) / ((d.high - d.low) + 0.001)


# =============================================================================
# Registry & Dispatcher
# =============================================================================

ALPHA_REGISTRY: Dict[int, Callable[[Alpha101Data], pd.DataFrame]] = {
    1: alpha_1, 2: alpha_2, 3: alpha_3, 4: alpha_4, 5: alpha_5,
    6: alpha_6, 7: alpha_7, 8: alpha_8, 9: alpha_9, 10: alpha_10,
    11: alpha_11, 12: alpha_12, 13: alpha_13, 14: alpha_14, 15: alpha_15,
    16: alpha_16, 17: alpha_17, 18: alpha_18, 19: alpha_19, 20: alpha_20,
    21: alpha_21, 22: alpha_22, 23: alpha_23, 24: alpha_24, 25: alpha_25,
    26: alpha_26, 27: alpha_27, 28: alpha_28, 29: alpha_29, 30: alpha_30,
    31: alpha_31, 32: alpha_32, 33: alpha_33, 34: alpha_34, 35: alpha_35,
    36: alpha_36, 37: alpha_37, 38: alpha_38, 39: alpha_39, 40: alpha_40,
    41: alpha_41, 42: alpha_42, 43: alpha_43, 44: alpha_44, 45: alpha_45,
    46: alpha_46, 47: alpha_47, 48: alpha_48, 49: alpha_49, 50: alpha_50,
    51: alpha_51, 52: alpha_52, 53: alpha_53, 54: alpha_54, 55: alpha_55,
    56: alpha_56, 57: alpha_57, 58: alpha_58, 59: alpha_59, 60: alpha_60,
    61: alpha_61, 62: alpha_62, 63: alpha_63, 64: alpha_64, 65: alpha_65,
    66: alpha_66, 67: alpha_67, 68: alpha_68, 69: alpha_69, 70: alpha_70,
    71: alpha_71, 72: alpha_72, 73: alpha_73, 74: alpha_74, 75: alpha_75,
    76: alpha_76, 77: alpha_77, 78: alpha_78, 79: alpha_79, 80: alpha_80,
    81: alpha_81, 82: alpha_82, 83: alpha_83, 84: alpha_84, 85: alpha_85,
    86: alpha_86, 87: alpha_87, 88: alpha_88, 89: alpha_89, 90: alpha_90,
    91: alpha_91, 92: alpha_92, 93: alpha_93, 94: alpha_94, 95: alpha_95,
    96: alpha_96, 97: alpha_97, 98: alpha_98, 99: alpha_99, 100: alpha_100,
    101: alpha_101,
}


def compute_alpha(alpha_id: int,
                  data: Union[Alpha101Data, Dict[str, pd.DataFrame]]) -> pd.DataFrame:
    """Computes a specific WorldQuant 101 Alpha (1 <= alpha_id <= 101)."""
    if alpha_id not in ALPHA_REGISTRY:
        raise ValueError(f"Unknown alpha_id: {alpha_id}. Must be between 1 and 101.")

    if not isinstance(data, Alpha101Data):
        data = Alpha101Data.from_universe(data)

    func = ALPHA_REGISTRY[alpha_id]
    result = func(data)
    # Ensure DataFrame is clean (replace infs with nan and fill)
    return result.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def compute_all_alphas(data: Union[Alpha101Data, Dict[str, pd.DataFrame]],
                       alpha_ids: Optional[List[int]] = None) -> Dict[int, pd.DataFrame]:
    """Computes all requested alphas (or all 1-101 if alpha_ids is None)."""
    if not isinstance(data, Alpha101Data):
        data = Alpha101Data.from_universe(data)

    target_ids = alpha_ids or sorted(ALPHA_REGISTRY.keys())
    res = {}
    for aid in target_ids:
        res[aid] = compute_alpha(aid, data)
    return res
