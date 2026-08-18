"""Allocation templates for basket-level portfolio construction.

Unlike single-symbol timing templates that output boolean entry/exit signals,
these templates take a whole universe of price data and output a SPARSE
DataFrame of TARGET WEIGHTS (0.0 to 1.0) for each symbol: a row is NaN on
every day EXCEPT an actual rebalance date, where it holds the new target.

This sparseness is deliberate, not an artifact: the backtester (see
`allocation_backtester.py`) tells a real rebalance instruction apart from "no
rebalance today" by whether the row is present at all, not by whether its
VALUE differs from the previous day's -- a template like equal-weight
recomputes the identical 1/N target on every rebalance date, so a
value-equality check would (and, before this was fixed, silently did) treat
every rebalance after the first as a no-op. Templates must NOT forward-fill
their own output; the backtester forward-fills internally for simulation and
uses the pre-ffill sparsity to find the real rebalance dates.
"""

from dataclasses import dataclass, field
from typing import Dict

import numpy as np
import pandas as pd

from common.indicators import realized_vol, roc
from common.scheduling import get_rebalance_dates as _get_rebalance_dates


@dataclass
class AllocationTemplate:
    name: str
    param_grid: dict

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        """Returns a DataFrame indexed by date, with columns for each symbol.
        A row is NaN except on an actual rebalance date, where it holds the
        target weight (0.0 to 1.0) for that symbol; weights across such a row
        should sum to <= 1.0. Do NOT forward-fill before returning -- see the
        module docstring for why the backtester depends on the sparsity."""
        raise NotImplementedError

    def explain_weights(self, params: dict) -> str:
        """Returns a human-readable explanation of how weights are calculated
        and when rebalancing occurs."""
        raise NotImplementedError

    def warmup_bars(self, params: dict) -> int:
        """How many bars of price history BEFORE a target evaluation window
        this template's indicators need before they stop returning NaN.
        Callers that slice a universe into a sub-window (e.g. a walk-forward
        fold) must include this many extra bars ahead of the window so the
        indicator isn't cold at the window's own start -- see
        `backtester/run_backtest.py`'s `run_walkforward`. Default: no
        indicator, no warmup needed."""
        return 0


def _hrp_portfolio(cov: np.ndarray) -> np.ndarray:
    """Computes Hierarchical Risk Parity (HRP) weights given a covariance matrix.
    Grounding: Marcos López de Prado (2016, J. Portfolio Management)."""
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import squareform

    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])

    v = np.sqrt(np.diag(cov))
    v[v == 0] = 1e-8
    corr = cov / np.outer(v, v)
    corr = np.clip(corr, -1.0, 1.0)

    dist = np.sqrt(np.clip(2.0 * (1.0 - corr), 0.0, 4.0))
    condensed = squareform(dist, checks=False)

    try:
        link = linkage(condensed, method="single")
    except Exception:
        return np.ones(n) / n

    def get_quasi_diag(link_mat):
        link_mat = link_mat.astype(int)
        sort_idx = [link_mat[-1, 0], link_mat[-1, 1]]
        num_items = link_mat[-1, 3]
        while sort_idx[0] >= num_items or sort_idx[1] >= num_items:
            for i in range(len(sort_idx)):
                if sort_idx[i] >= num_items:
                    idx = sort_idx[i] - num_items
                    sort_idx[i] = link_mat[idx, 0]
                    sort_idx.insert(i + 1, link_mat[idx, 1])
                    break
        return sort_idx

    sort_idx = get_quasi_diag(link)
    weights = pd.Series(1.0, index=sort_idx)
    cluster_items = [sort_idx]

    while len(cluster_items) > 0:
        cluster_items = [
            i[j:k]
            for i in cluster_items
            for j, k in ((0, len(i) // 2), (len(i) // 2, len(i)))
            if len(i) > 1
        ]
        for i in range(0, len(cluster_items), 2):
            c_items_l = cluster_items[i]
            c_items_r = cluster_items[i + 1]

            cov_l = cov[np.ix_(c_items_l, c_items_l)]
            cov_r = cov[np.ix_(c_items_r, c_items_r)]

            diag_l = np.diag(cov_l).copy()
            diag_l[diag_l == 0] = 1e-8
            inv_diag_l = 1.0 / diag_l
            w_l = inv_diag_l / inv_diag_l.sum()
            var_l = np.dot(np.dot(w_l, cov_l), w_l)

            diag_r = np.diag(cov_r).copy()
            diag_r[diag_r == 0] = 1e-8
            inv_diag_r = 1.0 / diag_r
            w_r = inv_diag_r / inv_diag_r.sum()
            var_r = np.dot(np.dot(w_r, cov_r), w_r)

            denom = var_l + var_r
            alpha = 1.0 - var_l / denom if denom > 0 else 0.5
            weights[c_items_l] *= alpha
            weights[c_items_r] *= 1.0 - alpha

    w_arr = weights.sort_index().to_numpy()
    sum_w = w_arr.sum()
    return w_arr / sum_w if sum_w > 0 else np.ones(n) / n


@dataclass
class EqualWeightAllocation(AllocationTemplate):
    name: str = "equal_weight"
    param_grid: dict = field(default_factory=lambda: {
        "rebalance_freq_days": [5, 21, 63]  # Weekly, Monthly, Quarterly
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        # Use the first symbol's index as the master calendar (assumes aligned universe)
        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        n_symbols = len(symbols)
        weight = 1.0 / n_symbols if n_symbols > 0 else 0.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates, :] = weight

        # Sparse: only the actual rebalance-date rows are set. The backtester
        # forward-fills for simulation and uses this sparsity, not value
        # equality, to find real rebalance dates (see module docstring).
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Equal Weight (1/N): Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Capital is distributed evenly across all assets in the basket, "
            f"calculated simply as 1 / (number of assets). This prevents concentration risk "
            f"but ignores relative volatility or performance."
        )


@dataclass
class InverseVolatilityAllocation(AllocationTemplate):
    name: str = "inverse_volatility"
    param_grid: dict = field(default_factory=lambda: {
        "vol_lookback": [20, 60, 120],
        "rebalance_freq_days": [5, 21, 63]
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        # Calculate daily inverse volatility for all symbols
        inv_vols = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            vol = realized_vol(df["Close"], window=params["vol_lookback"])
            # Avoid division by zero
            inv_vols[sym] = 1.0 / vol.replace(0, np.nan)

        # Only keep values on rebalance dates
        inv_vols_rebal = inv_vols.loc[rebalance_dates]

        # Normalize so weights sum to 1.0 across the row
        weights_rebal = inv_vols_rebal.div(inv_vols_rebal.sum(axis=1), axis=0)

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal

        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Inverse Volatility: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Allocates more capital to lower-risk assets to achieve risk parity. "
            f"Calculated by taking the {params['vol_lookback']}-day realized volatility of each asset, "
            f"computing its inverse (1/vol), and normalizing across the basket so the total weights sum to 100%."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["vol_lookback"]


@dataclass
class CrossSectionalMomentumAllocation(AllocationTemplate):
    name: str = "cross_sectional_momentum"
    param_grid: dict = field(default_factory=lambda: {
        "mom_lookback": [63, 126, 252],  # 3m, 6m, 12m
        "top_n_fraction": [0.25, 0.5],   # Top 25% or Top 50% of basket
        "rebalance_freq_days": [21, 63]  # Monthly, Quarterly
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        # Calculate momentum (Rate of Change) for all symbols
        moms = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            moms[sym] = roc(df["Close"], period=params["mom_lookback"])

        moms_rebal = moms.loc[rebalance_dates]

        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in moms_rebal.iterrows():
            if row.isna().all():
                continue
            # Get the top N symbols by momentum
            top_symbols = row.nlargest(top_n).index
            # Equal weight among the top N
            weights_rebal.loc[date, top_symbols] = 1.0 / top_n

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal

        return weights_df

    def explain_weights(self, params: dict) -> str:
        frac_pct = int(params['top_n_fraction'] * 100)
        return (
            f"Cross-Sectional Momentum: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Capital flows to the strongest recent performers, cutting losers entirely. "
            f"Calculated by ranking all assets by their {params['mom_lookback']}-day trailing return, "
            f"selecting the top {frac_pct}% of the basket, and equally weighting those winners."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["mom_lookback"]


@dataclass
class HierarchicalRiskParityAllocation(AllocationTemplate):
    name: str = "hierarchical_risk_parity"
    param_grid: dict = field(default_factory=lambda: {
        "cov_lookback": [60, 126, 252],
        "rebalance_freq_days": [21, 63]
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        returns_df = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            returns_df[sym] = df["Close"].pct_change()

        lookback = params["cov_lookback"]
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=np.nan)

        for date in rebalance_dates:
            loc = master_index.get_loc(date)
            if loc < lookback:
                continue
            sub_ret = returns_df.iloc[loc - lookback:loc]

            # Only symbols with a FULL, real return history over this
            # lookback are eligible. A symbol that hasn't started trading
            # yet (or has any other gap) would otherwise have its covariance
            # zero-filled -- and inverse-variance weighting reads "zero
            # variance" as "risk-free", handing it almost the entire
            # portfolio instead of correctly excluding it.
            valid_symbols = [s for s in symbols if sub_ret[s].notna().all()]
            if not valid_symbols:
                continue

            cov = sub_ret[valid_symbols].cov().to_numpy()
            w_hrp = _hrp_portfolio(cov)
            weights_rebal.loc[date, valid_symbols] = w_hrp

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Hierarchical Risk Parity (HRP): Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Builds a hierarchical tree on {params['cov_lookback']}-day correlation distances "
            f"and recursively bisects clusters to assign inverse-variance risk weights (López de Prado 2016). "
            f"Avoids Markowitz matrix-inversion instability while capturing asset correlation structure."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["cov_lookback"]


@dataclass
class DualMomentumAllocation(AllocationTemplate):
    name: str = "dual_momentum"
    param_grid: dict = field(default_factory=lambda: {
        "mom_lookback": [63, 126, 252],
        "top_n_fraction": [0.25, 0.5],
        "rebalance_freq_days": [21, 63]
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        moms = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            moms[sym] = roc(df["Close"], period=params["mom_lookback"])

        moms_rebal = moms.loc[rebalance_dates]
        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in moms_rebal.iterrows():
            if row.isna().all():
                continue
            top_symbols = row.nlargest(top_n).index
            for sym in top_symbols:
                if row[sym] > 0.0:
                    weights_rebal.loc[date, sym] = 1.0 / top_n
                else:
                    weights_rebal.loc[date, sym] = 0.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        frac_pct = int(params['top_n_fraction'] * 100)
        return (
            f"Dual Momentum: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Combines Relative Momentum (top {frac_pct}% performers) with Absolute Momentum "
            f"(requires trailing {params['mom_lookback']}-day return > 0, Antonacci 2014). "
            f"If an asset's trend is negative, its allocation steps to cash (0% weight) to protect capital during market declines."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["mom_lookback"]


@dataclass
class MaxDiversificationAllocation(AllocationTemplate):
    name: str = "max_diversification"
    param_grid: dict = field(default_factory=lambda: {
        "vol_lookback": [60, 126],
        "rebalance_freq_days": [21, 63]
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        returns_df = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            returns_df[sym] = df["Close"].pct_change()

        lookback = params["vol_lookback"]
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=np.nan)

        for date in rebalance_dates:
            loc = master_index.get_loc(date)
            if loc < lookback:
                continue
            sub_ret = returns_df.iloc[loc - lookback:loc]
            if sub_ret.dropna(how="all").empty:
                continue

            vols = sub_ret.std() * np.sqrt(252)
            corr = sub_ret.corr().fillna(0)

            n = len(symbols)
            avg_corr = (corr.sum(axis=1) - 1.0) / max(n - 1, 1)

            denom = (1.0 + avg_corr.clip(lower=0.0)).replace(0, np.nan)
            raw_w = vols / denom
            sum_w = raw_w.sum()
            if sum_w > 0:
                weights_rebal.loc[date] = raw_w / sum_w

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Maximum Diversification: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Allocates capital proportional to asset volatility divided by average peer correlation "
            f"over a {params['vol_lookback']}-day window (Choueifaty & Coignard 2008), favoring volatile "
            f"assets that are uncorrelated with the rest of the basket."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["vol_lookback"]


ALLOCATION_TEMPLATES = [
    EqualWeightAllocation,
    InverseVolatilityAllocation,
    CrossSectionalMomentumAllocation,
    HierarchicalRiskParityAllocation,
    DualMomentumAllocation,
    MaxDiversificationAllocation,
]
