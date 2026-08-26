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

from common.covariance import denoise_correlation, denoise_covariance
from common.indicator_features import compute_feature
from common.indicators import realized_vol, roc, rsi
from common.scheduling import get_rebalance_dates as _get_rebalance_dates


@dataclass
class AllocationTemplate:
    name: str
    param_grid: dict
    # Tags from common.factor_taxonomy.FACTOR_CATEGORIES describing which
    # quantitative factor(s) this template conditions on -- consumed by
    # strategy_generator's optional --factor-report hand-off (see
    # stratgen/generator.py) to contextualize/tie-break template selection.
    # Default empty: a template that doesn't declare tags simply never
    # participates in factor-based tie-breaking.
    factor_tags: list = field(default_factory=list)

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


def _min_variance_weights(cov: np.ndarray) -> np.ndarray:
    """Long-only minimum-variance portfolio weights via constrained
    quadratic minimization (weights sum to 1, each in [0, 1]). Grounding:
    Harry Markowitz (1952, Journal of Finance, "Portfolio Selection") --
    unlike `_hrp_portfolio`'s heuristic recursive-bisection substitute, this
    is a genuine numerical optimization of the classic mean-variance
    objective (variance only; no expected-return term, since a reliable
    expected-return estimate is the harder, unsolved half of Markowitz's
    original formulation). Falls back to equal weighting if the optimizer
    fails to converge (e.g. a near-singular covariance matrix) -- a
    portfolio can always be equal-weighted; it can't always be safely handed
    a degenerate optimizer result."""
    from scipy.optimize import minimize

    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])

    def objective(w):
        return w @ cov @ w

    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(0.0, 1.0)] * n
    x0 = np.full(n, 1.0 / n)

    result = minimize(objective, x0, method="SLSQP", bounds=bounds, constraints=constraints)
    if not result.success:
        return x0
    w = np.clip(result.x, 0.0, None)
    total = w.sum()
    return w / total if total > 0 else x0


def build_aggregate_curve(universe: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Equal-weight aggregate OHLCV curve for a whole universe: each symbol's
    own OHLC is rebased to start at the same level (100), then averaged
    cross-sectionally per day. This is a standard "normalized index"
    construction, and it guarantees the aggregate retains a sane
    High >= Close >= Low relationship, since averaging preserves that
    per-symbol inequality elementwise (High_i >= Close_i for every symbol
    and every day implies mean(High) >= mean(Close)).

    Symbols are aligned via inner join on their shared trading calendar
    (matching backtester/run_backtest.py's `_align_universe`) before
    averaging -- every symbol is fully present at every date in the
    returned curve, none are partially included or zero-filled.

    DISCLOSED APPROXIMATION: a real portfolio's own intraday high/low isn't
    literally the average of its constituents' individual highs/lows (those
    don't necessarily occur at the same moment); this is the same standard
    simplification used to approximate a basket's own OHLC from constituent
    OHLC when true simultaneous-quote data isn't available. Used by
    `PatternBasedAllocationTemplate` below and by
    `pattern_mining/pmine/pattern_mining.py`'s turning-point mining,
    which both need ONE aggregate curve to detect turning points/compute
    indicators on, not a per-symbol one.
    """
    symbols = [s for s, df in universe.items() if not df.empty]
    if not symbols:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    master_index = universe[symbols[0]].index
    for sym in symbols[1:]:
        master_index = master_index.intersection(universe[sym].index)
    master_index = master_index.sort_values()
    if len(master_index) < 2:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    out = {}
    for field in ("Open", "High", "Low", "Close"):
        normalized = pd.DataFrame({
            sym: universe[sym][field].reindex(master_index) / universe[sym]["Close"].reindex(master_index).iloc[0]
            for sym in symbols
        })
        out[field] = 100.0 * normalized.mean(axis=1, skipna=True)

    if all("Volume" in universe[sym].columns for sym in symbols):
        volumes = pd.DataFrame({sym: universe[sym]["Volume"].reindex(master_index) for sym in symbols})
        out["Volume"] = volumes.sum(axis=1, skipna=True)

    return pd.DataFrame(out, index=master_index)


@dataclass
class EqualWeightAllocation(AllocationTemplate):
    name: str = "equal_weight"
    param_grid: dict = field(default_factory=lambda: {
        "rebalance_freq_days": [5, 21, 63]  # Weekly, Monthly, Quarterly
    })
    factor_tags: list = field(default_factory=lambda: ["static_fixed_weight"])

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


def _inverse_vol_weights(vols: pd.Series, scale: float = 1.0, on_invalid: str = "nan") -> pd.Series:
    """1/vol, normalized so the returned weights (indexed like `vols`) sum to
    `scale` (default 1.0) -- the core inverse-volatility math shared by
    `InverseVolatilityAllocation` (whole-universe, scale=1.0) and
    `research_strategy`'s dual-momentum inverse-vol branch (a pre-filtered
    subset, scale=n_selected/top_k for a partial-fill risky sleeve). Zero vol
    is treated as missing. `on_invalid` controls what's returned when every
    symbol's vol is invalid/zero (total <= 0 or NaN) -- the two existing call
    sites disagree here, so this is NOT unified silently: `"nan"`
    (`InverseVolatilityAllocation`'s existing behavior, via natural division
    propagation) or `"zero"` (research_strategy's existing behavior, via its
    0.0-initialized weights frame)."""
    inv_v = 1.0 / vols.replace(0, np.nan)
    total = inv_v.sum()
    if not (pd.notna(total) and total > 0):
        return pd.Series(np.nan if on_invalid == "nan" else 0.0, index=vols.index)
    return (inv_v / total) * scale


@dataclass
class InverseVolatilityAllocation(AllocationTemplate):
    name: str = "inverse_volatility"
    param_grid: dict = field(default_factory=lambda: {
        "vol_lookback": [20, 60, 120],
        "rebalance_freq_days": [5, 21, 63]
    })
    factor_tags: list = field(default_factory=lambda: ["volatility_targeting"])

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        # Calculate daily volatility for all symbols
        vols = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
        for sym, df in universe.items():
            vols[sym] = realized_vol(df["Close"], window=params["vol_lookback"])

        # Only keep values on rebalance dates
        vols_rebal = vols.loc[rebalance_dates]

        # Invert + normalize so weights sum to 1.0 across the row
        weights_rebal = vols_rebal.apply(lambda row: _inverse_vol_weights(row), axis=1)

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
    factor_tags: list = field(default_factory=lambda: ["relative_momentum"])

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
    factor_tags: list = field(default_factory=lambda: ["correlation_diversification"])

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
            cov = denoise_covariance(cov, n_obs=len(sub_ret))
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
    factor_tags: list = field(default_factory=lambda: ["absolute_momentum_trend", "relative_momentum"])

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
    factor_tags: list = field(default_factory=lambda: ["volatility_targeting", "correlation_diversification"])

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

            # Same guard as HierarchicalRiskParityAllocation/
            # MinimumVarianceAllocation: a symbol without a full lookback
            # history is excluded rather than left in the correlation/
            # volatility computation, where an all-NaN return column would
            # otherwise get its correlation `fillna(0)`'d to look maximally
            # diversifying (and its own weight would come out NaN, breaking
            # the row's weight-sum invariant).
            valid_symbols = [s for s in symbols if sub_ret[s].notna().all()]
            if not valid_symbols:
                continue
            sub_ret = sub_ret[valid_symbols]

            vols = sub_ret.std() * np.sqrt(252)
            corr = denoise_correlation(sub_ret.corr().fillna(0), n_obs=len(sub_ret))

            n = len(valid_symbols)
            avg_corr = (corr.sum(axis=1) - 1.0) / max(n - 1, 1)

            denom = (1.0 + avg_corr.clip(lower=0.0)).replace(0, np.nan)
            raw_w = vols / denom
            sum_w = raw_w.sum()
            if sum_w > 0:
                weights_rebal.loc[date, valid_symbols] = (raw_w / sum_w).values

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


@dataclass
class MeanReversionAllocation(AllocationTemplate):
    name: str = "mean_reversion"
    param_grid: dict = field(default_factory=lambda: {
        "rsi_period": [2, 5, 14],
        "top_n_fraction": [0.25, 0.5],
        "rebalance_freq_days": [5, 21],
    })
    factor_tags: list = field(default_factory=lambda: ["mean_reversion"])

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        rsis = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            rsis[sym] = rsi(df["Close"], period=params["rsi_period"])

        rsis_rebal = rsis.loc[rebalance_dates]
        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in rsis_rebal.iterrows():
            if row.isna().all():
                continue
            # LOWEST RSI = most oversold -- Connors-style RSI(2) mean-reversion.
            oversold_symbols = row.nsmallest(top_n).index
            weights_rebal.loc[date, oversold_symbols] = 1.0 / top_n

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Mean Reversion: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Ranks all assets by {params['rsi_period']}-period RSI (Connors-style short-term "
            f"RSI mean-reversion) and equally weights the most-oversold (lowest RSI) "
            f"{int(params['top_n_fraction'] * 100)}% of the basket. NOTE: short rebalance frequencies "
            f"(5 trading days) are included so the grid search can empirically test whether this signal "
            f"survives realistic transaction costs -- mean-reversion strategies are unusually sensitive to "
            f"the commission/slippage charged on every rebalance's turnover (allocation_backtester.py), "
            f"since the edge per trade is typically small relative to a fixed per-turnover cost."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["rsi_period"]


@dataclass
class MinimumVarianceAllocation(AllocationTemplate):
    name: str = "minimum_variance"
    param_grid: dict = field(default_factory=lambda: {
        "cov_lookback": [60, 126, 252],
        "rebalance_freq_days": [21, 63],
    })
    factor_tags: list = field(default_factory=lambda: ["correlation_diversification"])

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

            # Same guard as HierarchicalRiskParityAllocation: a symbol
            # without a full lookback history is excluded rather than
            # zero-filled into the covariance matrix (zero variance would
            # otherwise look "risk-free" to the optimizer).
            valid_symbols = [s for s in symbols if sub_ret[s].notna().all()]
            if not valid_symbols:
                continue

            cov = sub_ret[valid_symbols].cov().to_numpy()
            cov = denoise_covariance(cov, n_obs=len(sub_ret))
            w_mv = _min_variance_weights(cov)
            weights_rebal.loc[date, valid_symbols] = w_mv

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Minimum Variance: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Solves a constrained quadratic program for the long-only portfolio of minimum "
            f"variance over the trailing {params['cov_lookback']} days (Markowitz 1952), subject to "
            f"weights summing to 100%. Unlike HierarchicalRiskParityAllocation's heuristic "
            f"recursive-bisection substitute, this is a genuine numerical optimization and can be less "
            f"stable when the covariance matrix is poorly conditioned (falls back to equal-weight on "
            f"non-convergence)."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["cov_lookback"]


@dataclass
class BreadthGatedMomentumAllocation(AllocationTemplate):
    name: str = "breadth_gated_momentum"
    param_grid: dict = field(default_factory=lambda: {
        "mom_lookback": [63, 126, 252],
        "top_n_fraction": [0.25, 0.5],
        "protection_factor": [1, 2],
        "rebalance_freq_days": [21, 63],
    })
    factor_tags: list = field(default_factory=lambda: ["breadth", "relative_momentum"])

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
        n1 = params["protection_factor"] * n_symbols / 4.0

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in moms_rebal.iterrows():
            row = row.dropna()
            if len(row) < n_symbols:
                # Whole-basket warmup gate: breadth's denominator N must be
                # the FULL basket for the invested fraction to mean anything
                # -- stricter than HRP/MinVariance's per-symbol exclusion.
                # One late-listed symbol can stall this template's
                # rebalancing until every symbol has warmed up.
                continue

            n_positive = int((row > 0).sum())
            denom = n_symbols - n1
            if n_positive <= n1:
                derisked_fraction = 1.0
            elif denom > 0:
                derisked_fraction = max(0.0, (n_symbols - n_positive) / denom)
            else:
                derisked_fraction = 0.0
            invested_fraction = 1.0 - derisked_fraction

            top_symbols = row.nlargest(top_n).index
            if len(top_symbols) > 0:
                weights_rebal.loc[date, top_symbols] = invested_fraction / len(top_symbols)

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Breadth-Gated Momentum: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: generalizes Keller & Keuning (2016, SSRN #2759734, Protective Asset Allocation)'s "
            f"breadth-based crash-protection mechanism to an arbitrary basket with no dedicated "
            f"protection/bond symbol -- the de-risked fraction becomes idle cash (this codebase's "
            f"weights-sum-under-100% convention) rather than flowing to a named defensive instrument. "
            f"Scores all assets by {params['mom_lookback']}-day momentum; the TOTAL invested fraction "
            f"scales continuously with breadth (the count of assets with positive momentum), fully "
            f"de-risking when breadth falls to or below protection_factor={params['protection_factor']} "
            f"* N / 4 assets. The remainder splits equally across the top "
            f"{int(params['top_n_fraction'] * 100)}% by momentum rank, regardless of individual sign. "
            f"NOTE: as with this workspace's own ProtectiveAssetAllocation, the exact breakpoint/scaling "
            f"constants are a disclosed, reasonable reconstruction of the documented mechanism, not a "
            f"verified reproduction of the primary paper's exact formula."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["mom_lookback"]


ALLOCATION_TEMPLATES = [
    EqualWeightAllocation,
    InverseVolatilityAllocation,
    CrossSectionalMomentumAllocation,
    HierarchicalRiskParityAllocation,
    DualMomentumAllocation,
    MaxDiversificationAllocation,
    MeanReversionAllocation,
    MinimumVarianceAllocation,
    BreadthGatedMomentumAllocation,
]


# Maps a mined feature_name to the FACTOR_CATEGORIES tag (common/factor_taxonomy.py)
# that actually describes it -- grounded directly in that taxonomy's own category
# descriptions (e.g. "regime_trend_strength" names ADX explicitly; "mean_reversion"
# names RSI explicitly), not invented here. Used by PatternBasedAllocationTemplate
# below so a mined template's factor_tags reflect WHICH INDICATOR was mined, not just
# which direction (peak/trough) it trades -- see pattern_mining/pmine/pattern_mining.py
# and root README's pipeline docs for why this is what lets a mined pattern's
# strategy_generator/stratgen/generator.py factor tie-break genuinely compare it
# against research_strategy's trend/factor evidence for the RIGHT reference class.
_FEATURE_FACTOR_TAGS = {
    "rsi": "mean_reversion",
    "stoch_k": "mean_reversion",
    "cci": "mean_reversion",
    "williams_r": "mean_reversion",
    "bb_pctb": "mean_reversion",
    "adx": "regime_trend_strength",
    "roc": "absolute_momentum_trend",
    "sma_rel": "absolute_momentum_trend",
    "macd_hist": "absolute_momentum_trend",
    "atr_pct": "volatility_targeting",
}


class PatternBasedAllocationTemplate(AllocationTemplate):
    """A trading signal built from ONE indicator pattern discovered by the
    `pattern_mining` stage's (`pattern_mining/pmine/pattern_mining.py`)
    turning-point pattern mining -- deliberately NOT in `ALLOCATION_TEMPLATES`
    above, unlike the 9 static templates. Those are universe-agnostic formulas,
    zero-arg constructible, searched for every basket; this template's own
    threshold comes from mining a SPECIFIC basket's aggregate-portfolio
    turning-point history first, so it can't be a static class -- it's
    instantiated from a `pattern_report.json` (or, in-process, straight from
    a mining pass) and passed into `StrategyGenerator.generate(...,
    extra_templates=[...])`, competing through the exact same grid-search +
    Equivalent Random Search validation as every static template.

    HONEST CAVEAT (read `pattern_mining/pmine/pattern_mining.py`'s module
    docstring for the full version): the mining pass that discovered this
    template's threshold needed a few bars of hindsight to LABEL a
    historical date a "turning point" at all (zigzag confirmation lag) --
    but this live signal has NO such lag itself: it only compares today's
    already-known indicator reading against the mined threshold, never
    trying to detect a turning point in real time. A mined pattern passing
    its own (Bonferroni-corrected) significance test is necessary, not
    sufficient, for it to be presented as trustworthy -- it must ALSO clear
    the same ERS bar every other template does; on synthetic data, most
    mining passes are expected to find nothing significant at all, matching
    this workspace's own repeated finding elsewhere that most series show
    no significant structure.
    """

    def __init__(self, feature_name: str, feature_lookback, threshold: float,
                 comparison: str, event_type: str, mined_p_value: float = None,
                 mined_n_events: int = None):
        if comparison not in ("below", "above"):
            raise ValueError(f"comparison must be 'below' or 'above', got {comparison!r}")
        if event_type not in ("trough", "peak"):
            raise ValueError(f"event_type must be 'trough' or 'peak', got {event_type!r}")

        self.feature_name = feature_name
        self.feature_lookback = feature_lookback
        self.threshold = threshold
        self.comparison = comparison
        self.event_type = event_type
        self.mined_p_value = mined_p_value
        self.mined_n_events = mined_n_events

        lb = feature_lookback
        lb_str = "_".join(str(x) for x in lb) if isinstance(lb, (tuple, list)) else str(lb)
        name = f"pattern_{feature_name}_{lb_str}_{event_type}"
        # Falls back to the old peak/trough-only heuristic ONLY for a
        # feature_name outside common/indicator_features.py's own
        # DEFAULT_FEATURE_MENU (shouldn't happen via normal mining, but
        # degrades sensibly rather than raising for a hand-built instance).
        tag = _FEATURE_FACTOR_TAGS.get(
            feature_name, "regime_trend_strength" if event_type == "peak" else "mean_reversion"
        )
        super().__init__(
            name=name,
            param_grid={
                "threshold_mult": [0.9, 1.0, 1.1],
                "hold_days": [10, 21, 42],
                "rebalance_freq_days": [5, 21],
            },
            factor_tags=[tag],
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        curve = build_aggregate_curve(universe)
        if curve.empty or len(curve) < 2:
            return pd.DataFrame()

        master_index = curve.index
        rebalance_dates = _get_rebalance_dates(master_index, params.get("rebalance_freq_days", 21))
        if len(rebalance_dates) == 0:
            return pd.DataFrame()

        threshold_mult = params.get("threshold_mult", 1.0)
        hold_days = params.get("hold_days", 21)
        effective_threshold = self.threshold * threshold_mult

        feature = compute_feature(curve, self.feature_name, self.feature_lookback)
        trigger = (feature <= effective_threshold) if self.comparison == "below" else (feature >= effective_threshold)
        # A trigger STAYS active for `hold_days` bars after it fires --
        # vectorized, backward-only (no explicit stateful loop needed).
        active = trigger.rolling(hold_days, min_periods=1).max().fillna(0).astype(bool)

        # Trough (bullish finding): cash baseline, invest while active.
        # Peak (bearish finding): invested baseline, de-risk to cash while active.
        invested = active if self.event_type == "trough" else ~active

        n_symbols = len(symbols)
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)
        for date in rebalance_dates:
            if date in invested.index and bool(invested.loc[date]):
                weights_rebal.loc[date, :] = 1.0 / n_symbols

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        threshold_mult = params.get("threshold_mult", 1.0)
        hold_days = params.get("hold_days", 21)
        rebal = params.get("rebalance_freq_days", 21)
        effective_threshold = self.threshold * threshold_mult
        action = "invests" if self.event_type == "trough" else "de-risks to cash"
        p_str = f"p={self.mined_p_value:.4f}" if self.mined_p_value is not None else "p=n/a"
        return (
            f"Pattern-Based ({self.event_type}-associated {self.feature_name}[{self.feature_lookback}]): "
            f"rebalances every {rebal} trading days. Mined from this basket's own aggregate-portfolio "
            f"turning-point history via a Bonferroni-corrected shuffle-null significance test "
            f"({p_str}, n_events={self.mined_n_events}) -- see "
            f"pattern_mining/pmine/pattern_mining.py. {action.capitalize()} (equal-weight across "
            f"the basket) for {hold_days} trading days whenever the {self.feature_name} reading "
            f"{self.comparison} {effective_threshold:.4g} (mined threshold x {threshold_mult}). HONEST "
            f"CAVEAT: the mining pass needed a few bars of hindsight to LABEL a historical date a turning "
            f"point at all (zigzag confirmation lag) -- but this live signal has no such lag itself, since "
            f"it only compares today's already-known reading against the mined threshold. Passed this "
            f"workspace's standard Equivalent Random Search validation like every other template; a "
            f"significant reading during mining reflects mechanism, not a guaranteed real edge -- see "
            f"pattern_mining/README.md."
        )

    def warmup_bars(self, params: dict) -> int:
        lb = self.feature_lookback
        base_lookback = max(lb) if isinstance(lb, (tuple, list)) else lb
        return base_lookback + params.get("hold_days", 21)
