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
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from common.covariance import denoise_correlation, denoise_covariance
from common.indicator_features import compute_feature
from common.indicators import realized_vol, roc, rsi
from common.position_policy import kernel_apply_asset_inertia
from common.scheduling import get_rebalance_dates as _get_rebalance_dates


def _get_master_index(universe: Dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Builds a unified master trading calendar from the union of all active
    trading dates across non-empty symbols in the universe. If all symbols
    share the identical DatetimeIndex, returns the original index object to
    preserve its frequency and metadata."""
    non_empty = [s for s, df in universe.items() if not df.empty]
    if not non_empty:
        return pd.DatetimeIndex([])
    first_idx = universe[non_empty[0]].index
    if all(len(universe[s].index) == len(first_idx) and universe[s].index.equals(first_idx) for s in non_empty[1:]):
        return first_idx
    return pd.DatetimeIndex(sorted(set().union(*(universe[s].index for s in non_empty))))


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
    version: Optional[str] = None

    def __post_init__(self):
        if self.version is None:
            self.version = getattr(self.__class__, "version", "1.0.0") or "1.0.0"

    def get_version_info(self) -> Dict[str, Any]:
        return {
            "version": str(self.version),
            "template_name": self.name,
        }

    @property
    def version_string(self) -> str:
        return f"v{self.version}"

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


def _sector_groups(symbols: List[str], sector_map: Optional[dict] = None) -> List[np.ndarray]:
    """Groups integer column indices of `symbols` by resolved sector label, for
    `_turnover_penalized_weights`'s per-sector turnover-budget constraints. Uses
    `common.sector_metadata.resolve_sector` (the shared SSOT resolver, M2.1) with
    its 'auto' fallback: genuine A-share codes fall back to a ticker-prefix
    pseudo-class, everything else lands in a single 'other' bucket.

    HONEST CAVEAT: 'other' is a catch-all bucket, not an economic sector, so for
    universes whose symbols are not in the supplied `sector_map` this collapses
    into ONE group and the per-sector budget degenerates into a total-turnover
    budget. That is a disclosed approximation, not a bug -- supplying an explicit
    `opt_sector_map` param is what makes the constraint sector-meaningful.
    """
    from common.sector_metadata import resolve_sector

    buckets: dict = {}
    for i, sym in enumerate(symbols):
        try:
            label = resolve_sector(sym, sector_map or None, fallback="auto")
        except Exception:
            # Degrade, never throw: an unparsable ticker joins the catch-all
            # bucket so the constraint stays well-defined.
            label = "other"
        buckets.setdefault(label, []).append(i)
    return [np.asarray(idx, dtype=int) for idx in buckets.values()]


def _turnover_penalized_weights(
    cov: np.ndarray,
    w0: np.ndarray,
    turnover_penalty: float = 0.0,
    single_cap: float = 0.20,
    sector_groups: Optional[List[np.ndarray]] = None,
    sector_dev: float = 0.03,
    cash_allowance: float = 0.0,
) -> np.ndarray:
    """Turnover-penalized long-only minimum-variance weights (M1.3).

    Objective (mean-return term deliberately ABSENT, i.e. lambda = 0 -- see
    `_min_variance_weights` for why a reliable expected-return estimate is the
    unsolved half of Markowitz 1952):

        min_w  w' S w  +  gamma * sum_i sqrt((w_i - w0_i)^2 + 1e-6)
        s.t.   0 <= w_i <= single_cap
               lower_budget <= sum_i w_i <= upper_budget     (residual is CASH)
               |sum_{i in g} w_i - sum_{i in g} w0_i| <= sector_dev   for each group g

    where

        upper_budget = min(1.0, n * single_cap)
        lower_budget = min(1.0 - cash_allowance, upper_budget)   # never above upper

    BUDGET CORRECTION (deliberate and reasoned -- NOT an approximation). An
    earlier revision constrained only `sum(w) <= 1`, leaving a free cash
    residual. That program is degenerate, and provably so: with no lower bound
    on total exposure the budget multiplier is inactive, so first-order
    optimality gives `nu = 0` and `2 S w + gamma * sign(w - w0) = 0`, i.e.
    `w ~= -(gamma/2) S^-1 sign(w - w0)`. The VARIANCE term then drives total
    exposure toward zero while the fixed-magnitude L1 term can only resist with
    force `gamma`, so the fixed point is a tiny portfolio that is essentially
    INDEPENDENT of `w0` -- measured exposure decayed 1.00 -> 0.94 -> 0.88 ->
    0.82 over consecutive rebalances. Bounding total exposure from BELOW (a
    box, not free slack) restores the budget multiplier and removes that
    collapse. This still satisfies the row's literal requirement
    (`sum(w) <= 1` is never exceeded); with the default
    `cash_allowance = 0.0` the book is fully invested, and a positive
    allowance merely permits (it never forces) a cash residual.

    CAP FEASIBILITY (the cap is a HARD risk limit; cash absorbs any
    shortfall, never raises): a fully invested book requires
    `n * single_cap >= 1`, e.g. `cap=0.20` with `n=4` cannot fund a 100%
    book. The budget -- not the cap -- is what gives way:

        upper_budget = min(1.0, n * single_cap)
        lower_budget = min(1.0 - cash_allowance, upper_budget)   # never above upper

    so `n * cap >= 1` reproduces the previous behaviour exactly
    (`[1 - allowance, 1]`), while `n * cap < 1` solves for
    `sum(w) == n * cap` and holds the residual `1 - n * cap` in CASH.
    THE CAP IS NEVER RAISED. A per-position cap is a RISK LIMIT while cash is
    always a feasible alternative, so relaxing the cap to reach full
    investment would invert the safety ordering -- exactly the "silently
    degraded constraint" (静默失效) failure family this repo documents. Callers
    get a `RuntimeWarning` and an `explain_weights` note phrased as *budget
    constrained by the position cap => cash residual*, never as a cap
    relaxation.

    The L1 turnover term is smoothed as `sqrt(d^2 + 1e-6)` (a standard
    pseudo-Huber smoothing of |d|) so the objective stays differentiable for
    SLSQP; the smoothing error is bounded by 1e-3 in the turnover *scalar*.
    Grounding for the penalty itself: Garleanu & Pedersen (2013, Journal of
    Finance, "Dynamic Trading with Predictable Returns and Transaction
    Costs") -- a proportional trading cost is, to first order, an L1 penalty
    on the weight change.

    DISCLOSED SOLVER TUNING: `options={"ftol": 1e-12, "maxiter": 500}`. SLSQP's
    default ftol=1e-6 stops after ONE iteration on well-conditioned but
    small-magnitude covariances (daily variances ~1e-5), returning the
    equal-weight start point -- a real premature-convergence quirk that
    `_min_variance_weights` still exhibits and that is deliberately NOT fixed
    there (its output must stay bit-identical). Tightening ftol only removes
    that numerical artifact; it does not change the mathematical problem.

    `gamma == 0` must reduce EXACTLY to `_min_variance_weights` (same sum-to-one
    EQUALITY form, bounds [0, 1] -- the old path's form), so it delegates
    verbatim rather than re-deriving the same program differently. This
    equivalence is now EXACT BY CONSTRUCTION rather than merely close: the
    legacy path already imposes `sum(w) == 1`, which is precisely this
    helper's box with `cash_allowance = 0` and `single_cap >= 1/n`.

    FAILURE POLICY -- degrade, never raise (mirrors `_min_variance_weights`'s
    `return x0` and the backtester's `-inf`-Sharpe degradation convention). The
    ladder always returns a valid long-only vector:
      1. SLSQP on the smoothed L1 objective with sector constraints;
      2. retry once with auxiliary variables t_i >= |w_i - w0_i| for the EXACT L1;
      3. drop the sector constraints and retry the smoothed objective;
      4. `_min_variance_weights(cov)` (the legacy path), then cap/renormalize;
      5. equal weight 1/n clipped to the cap.

    Returns a vector of length `len(w0)` with `0 <= w <= single_cap` and
    `lower_budget - 1e-6 <= sum(w) <= upper_budget + 1e-12`.
    """
    import warnings

    from scipy.optimize import minimize

    cov = np.asarray(cov, dtype=float)
    n = cov.shape[0]
    w0 = np.asarray(w0, dtype=float).reshape(-1)

    # --- gamma == 0: exact legacy reduction (do not "unify" these paths;
    # the acceptance bar is bit-identity with the pre-M1.3 output).
    if turnover_penalty is None or float(turnover_penalty) <= 0.0:
        return _min_variance_weights(cov)

    gamma = float(turnover_penalty)
    if len(w0) != n:
        # Defensive: a mismatched prior vector cannot express a turnover cost,
        # so treat it as "coming from cash" rather than raising.
        w0 = np.zeros(n)

    cap = 1.0 if single_cap is None else float(single_cap)
    cap = float(np.clip(cap if np.isfinite(cap) else 1.0, 0.0, 1.0))
    dev = float(sector_dev) if sector_dev is not None else 0.0
    dev = dev if np.isfinite(dev) and dev >= 0.0 else 0.0

    allowance = float(cash_allowance) if cash_allowance is not None else 0.0
    allowance = float(np.clip(allowance if np.isfinite(allowance) else 0.0, 0.0, 1.0))

    # --- Budget bounds. The position cap is a HARD risk limit, so it is never
    # relaxed: instead the total-exposure budget is capped at what the cap can
    # actually fund, and CASH absorbs the shortfall. `n * cap >= 1` leaves this
    # identical to the previous `[1 - allowance, 1]` box (within 1e-12 of 1 we
    # snap to a full book so the feasible case keeps the exact same program).
    raw_total_cap = float(n * cap) if n > 0 else 0.0
    upper_budget = 1.0 if raw_total_cap >= 1.0 - 1e-12 else raw_total_cap
    # Never above `upper_budget`: a lower bound above the achievable total
    # would make the program infeasible (the very defect the box fixes).
    lower_budget = float(min(1.0 - allowance, upper_budget))

    if n > 0 and raw_total_cap < 1.0 - 1e-12:
        warnings.warn(
            f"single_cap={cap:.4f} x n={n} assets caps total exposure at "
            f"{upper_budget:.4f} < 1; the budget is constrained by the position "
            f"cap and the residual {1.0 - upper_budget:.4f} is held in cash "
            f"(the cap is a risk limit and is NOT relaxed)",
            RuntimeWarning,
            stacklevel=3,
        )

    groups: List[np.ndarray] = []
    if sector_groups:
        for g in sector_groups:
            idx = np.asarray(g, dtype=int).reshape(-1)
            idx = idx[(idx >= 0) & (idx < n)]
            if idx.size:
                groups.append(idx)

    def _try_solve(fun, x_start, bnds, cons) -> Optional[np.ndarray]:
        """One SLSQP attempt; ANY exception or non-convergence returns None so
        the caller's ladder can degrade. This is the only place `minimize` is
        called, so a solver that raises (bad covariance, NaN bounds) cannot
        escape as a template-level exception."""
        try:
            res = minimize(
                fun, x_start, method="SLSQP", bounds=bnds, constraints=cons, options=opts
            )
        except Exception:
            return None
        if not getattr(res, "success", False):
            return None
        x = np.asarray(getattr(res, "x", None), dtype=float).reshape(-1)
        if x.shape[0] != np.asarray(x_start).reshape(-1).shape[0] or not np.all(np.isfinite(x)):
            return None
        return x

    def _budget_ok(w) -> bool:
        """The feasible box on total exposure, up to solver tolerance."""
        total = float(np.sum(np.asarray(w, dtype=float).reshape(-1)))
        return (total >= lower_budget - 1e-6) and (total <= upper_budget + 1e-6)

    def _finalize(w) -> np.ndarray:
        """Project any solver output onto the feasible long-only box + budget
        box. Never raises; a non-finite/garbage vector degrades to the
        equal-weight rung."""
        arr = np.asarray(w, dtype=float).reshape(-1)
        if arr.shape[0] != n or not np.all(np.isfinite(arr)):
            return _equal_weight_rung()
        arr = np.clip(arr, 0.0, cap)
        total = float(arr.sum())
        if total > upper_budget:
            arr = arr * (upper_budget / total)
        # Clipping can pull exposure BELOW the lower budget bound (e.g. the
        # legacy min-variance vector clipped at a binding cap). Spread the
        # shortfall over the symbols that still have cap headroom rather than
        # returning an under-invested book -- under-investment is exactly the
        # instability this constraint set exists to remove.
        deficit = lower_budget - float(arr.sum())
        if deficit > 1e-12:
            headroom = cap - arr
            room = float(headroom.sum())
            if room > 0.0:
                arr = arr + headroom * min(1.0, deficit / room)
        return arr

    def _equal_weight_rung() -> np.ndarray:
        # `min(1/n, cap)` per slot sums to `min(1, n*cap) == upper_budget`, i.e.
        # this rung is budget-feasible by construction in both regimes (fully
        # invested, or cap-constrained with the residual in cash).
        base = min(1.0 / n, cap) if n > 0 else 0.0
        return np.full(n, base, dtype=float)

    def _smooth_l1(w) -> float:
        d = w - w0
        return float(np.sum(np.sqrt(d * d + 1e-6)))

    def _objective_smoothed(w) -> float:
        return float(w @ cov @ w) + gamma * _smooth_l1(w)

    def _build_constraints(with_sector: bool) -> list:
        # Total-exposure BOX: `lower_budget <= sum(w) <= upper_budget`. With no
        # cash allowance and a cap that can fund a full book the box
        # degenerates into an equality (`sum(w) == 1`), which SLSQP handles far
        # more accurately as such -- it is the same constraint set, just
        # written in the numerically better-behaved form.
        if lower_budget >= upper_budget - 1e-12:
            cons = [{"type": "eq", "fun": lambda w: float(np.sum(w)) - upper_budget}]
        else:
            cons = [
                {"type": "ineq", "fun": lambda w: upper_budget - float(np.sum(w))},
                {"type": "ineq", "fun": lambda w: float(np.sum(w)) - lower_budget},
            ]
        if with_sector:
            for idx in groups:
                prior = float(w0[idx].sum())
                cons.append({
                    "type": "ineq",
                    "fun": lambda w, idx=idx, prior=prior: dev - (float(w[idx].sum()) - prior),
                })
                cons.append({
                    "type": "ineq",
                    "fun": lambda w, idx=idx, prior=prior: dev + (float(w[idx].sum()) - prior),
                })
        return cons

    # Start point: the previous target, capped; a from-cash start (all-zero w0)
    # has no sensible direction, so equal weight is the neutral warm start.
    x0 = np.clip(w0, 0.0, cap)
    if x0.sum() > upper_budget:
        x0 = x0 * (upper_budget / x0.sum())
    if x0.sum() <= 1e-12:
        x0 = _equal_weight_rung()

    bounds = [(0.0, cap)] * n
    opts = {"ftol": 1e-12, "maxiter": 500}

    # --- Rung 1: smoothed L1 + sector constraints.
    sol = _try_solve(_objective_smoothed, x0, bounds, _build_constraints(True))
    if sol is not None and _budget_ok(sol):
        return _finalize(sol)

    # --- Rung 2: exact L1 via auxiliary variables t_i >= |w_i - w0_i|.
    def _exact_objective(z) -> float:
        w = z[:n]
        return float(w @ cov @ w) + gamma * float(np.sum(np.clip(z[n:], 0.0, None)))

    def _exact_constraints(with_sector: bool) -> list:
        cons = []
        for i in range(n):
            cons.append({"type": "ineq", "fun": lambda z, i=i: z[n + i] - (z[i] - w0[i])})
            cons.append({"type": "ineq", "fun": lambda z, i=i: z[n + i] - (w0[i] - z[i])})
        if lower_budget >= upper_budget - 1e-12:
            cons.append({"type": "eq", "fun": lambda z: float(np.sum(z[:n])) - upper_budget})
        else:
            cons.append({"type": "ineq", "fun": lambda z: upper_budget - float(np.sum(z[:n]))})
            cons.append({"type": "ineq", "fun": lambda z: float(np.sum(z[:n])) - lower_budget})
        if with_sector:
            for idx in groups:
                prior = float(w0[idx].sum())
                cons.append({
                    "type": "ineq",
                    "fun": lambda z, idx=idx, prior=prior: dev - (float(np.sum(z[:n][idx])) - prior),
                })
                cons.append({
                    "type": "ineq",
                    "fun": lambda z, idx=idx, prior=prior: dev + (float(np.sum(z[:n][idx])) - prior),
                })
        return cons

    z0 = np.concatenate([x0, np.abs(x0 - w0)])
    zbounds = [(0.0, cap)] * n + [(0.0, 1.0)] * n
    sol = _try_solve(_exact_objective, z0, zbounds, _exact_constraints(True))
    if sol is not None and _budget_ok(sol[:n]):
        return _finalize(sol[:n])

    # --- Rung 3: drop the sector constraints (a near-infeasible sector budget
    # is the most likely reason rungs 1-2 both failed).
    if groups:
        sol = _try_solve(_objective_smoothed, x0, bounds, _build_constraints(False))
        if sol is not None and _budget_ok(sol):
            return _finalize(sol)

    # --- Rung 4: the legacy long-only minimum-variance solution.
    try:
        legacy = _min_variance_weights(cov)
        if np.all(np.isfinite(legacy)) and float(np.sum(np.abs(legacy))) > 0:
            return _finalize(legacy)
    except Exception:
        pass

    # --- Rung 5: equal weight, clipped to the cap.
    return _equal_weight_rung()


def _fill_out_columns(daily: pd.DataFrame, symbols: list) -> pd.DataFrame:
    """Ensures `daily` has exactly `symbols` as its columns, in that order --
    any symbol not already present gets an explicit 0.0 column (never NaN,
    per the sparse-weights NaN-vs-0.0 contract). Shared by every single-asset
    timing template/aspect that builds its own weights per-symbol before
    reassembling the full universe-wide frame."""
    for s in symbols:
        if s not in daily.columns:
            daily[s] = 0.0
    return daily[symbols]


def _step_asset_inertia(
    t: int,
    ideal_w: pd.Series,
    current_w: pd.Series,
    risky_symbols: list,
    symbols: list,
    thresh_t: pd.Series,
    holding_age: Dict[str, int],
    last_buy_bar: Dict[str, int],
    cash_proxy: Optional[str] = "CASH",
    em_row: Optional[Union[bool, pd.Series]] = None,
    asymmetric_sells: bool = False,
    entry_threshold: Optional[float] = None,
    min_holding_bars: int = 0,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.Series:
    """Single-bar asset-level inertia filter step updating `holding_age` and `last_buy_bar` in place."""
    if em_row is not None:
        if isinstance(em_row, pd.Series):
            symbol_emergency = {s: bool(em_row.get(s, False)) for s in risky_symbols}
            is_global_emergency = all(symbol_emergency.values()) if symbol_emergency else False
        else:
            is_global_emergency = bool(em_row)
            symbol_emergency = {s: is_global_emergency for s in risky_symbols}
    else:
        is_global_emergency = False
        symbol_emergency = {s: False for s in risky_symbols}

    for s in risky_symbols:
        if current_w[s] > 1e-4:
            holding_age[s] += 1
        else:
            holding_age[s] = 0

    if is_global_emergency:
        return ideal_w.copy()

    sell_thresh = (thresh_t * float(asymmetric_sell_ratio)) if asymmetric_sells else thresh_t
    buy_thresh = thresh_t.copy()
    if entry_threshold is not None:
        new_entry_mask = (current_w[risky_symbols] <= 1e-4)
        buy_thresh[new_entry_mask] = entry_threshold

    diff = ideal_w[risky_symbols] - current_w[risky_symbols]
    sells_raw = diff[diff <= -sell_thresh].index.tolist()

    # Suppress sells if asset holding tenure is below min_holding_bars (unless emergency)
    # Note: Incremental adds to an existing mother position do not reset the mother position's holding tenure lock.
    sells = []
    for s in sells_raw:
        if symbol_emergency.get(s, False):
            sells.append(s)
        elif min_holding_bars > 0:
            if holding_age[s] < min_holding_bars:
                # Mother position has not elapsed min_holding_bars: suppress all sells
                pass
            elif ideal_w[s] > 1e-4 and (t - last_buy_bar[s]) < min_holding_bars:
                # Partial trimming of a recently added position (< min_holding_bars): suppress trim
                pass
            else:
                sells.append(s)
        else:
            sells.append(s)

    buys = diff[diff >= buy_thresh].index.tolist()

    if not sells and not buys:
        return current_w.copy()

    new_w = current_w.copy()
    # 1. Execute sells first to release cash capacity
    for s in sells:
        new_w[s] = ideal_w[s]
        if new_w[s] <= 1e-4:
            last_buy_bar[s] = -999

    # 2. Execute buys up to incremental available capacity without diluting untouched or held assets
    avail_inc = max(0.0, 1.0 - float(new_w[risky_symbols].sum()))

    buys_sorted = sorted(buys, key=lambda b: (-diff[b], b))
    for b in buys_sorted:
        desired_inc = max(0.0, float(ideal_w[b] - current_w[b]))
        actual_inc = min(desired_inc, avail_inc)

        if actual_inc >= buy_thresh[b]:
            new_w[b] = current_w[b] + actual_inc
            last_buy_bar[b] = t
            avail_inc = max(0.0, avail_inc - actual_inc)
        else:
            new_w[b] = current_w[b]

    if cash_proxy in symbols:
        new_w[cash_proxy] = max(0.0, 1.0 - float(new_w[risky_symbols].sum()))

    return new_w


def apply_asset_inertia(
    daily: pd.DataFrame,
    min_weight_change: Union[float, Dict[str, float], pd.Series, pd.DataFrame] = 0.02,
    cash_proxy: Optional[str] = "CASH",
    emergency_mask: Optional[Union[pd.Series, pd.DataFrame]] = None,
    asymmetric_sells: bool = False,
    entry_threshold: Optional[float] = None,
    min_holding_bars: int = 0,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.DataFrame:
    """Filters daily target-weight allocations using asset-level inertia (Option A).

    For each asset, if the change in ideal target weight versus the currently held
    target weight is below `min_weight_change`, the asset's target weight is frozen
    at its previous held value. Supports uniform float, per-asset dict, pd.Series,
    or date-indexed pd.DataFrame thresholds, tighter asymmetric sell bands (default
    60% threshold on trimming, parameterized via `asymmetric_sell_ratio`), an
    optional `entry_threshold` allowing new position initiations to enter without
    being blocked by volatility-scaled rebalancing inertia, and an optional
    `min_holding_bars` to suppress premature sell/trim flips before a minimum
    holding tenure has elapsed.

    When significant changes occur (|diff| >= min_weight_change):
    1. Sells execute first to release cash capacity (subject to `min_holding_bars` unless emergency).
    2. Buys execute in order of priority up to available incremental cash capacity without
       diluting or modifying untouched assets or exceeding 100% total risky weight.
    3. Residual unallocated cash is routed to `cash_proxy` (if present).
    4. Emergency dates (indicated by `emergency_mask`) bypass inertia and holding constraints
       to allow immediate liquidation or de-risking without threshold delay.

    M4.2 phase 2 (commit 1b): the body is now a single delegation to
    `common.position_policy.kernel_apply_asset_inertia`, which reproduces this
    function cell-for-cell (identical threshold-normalization preamble, first-row
    seed, tenure clocks, `emergency_mask` handling, and per-bar sequencing). The
    signature and all defaults above are UNCHANGED -- every existing caller keeps
    calling `apply_asset_inertia` and gets bit-identical frames, `NaN` = "no
    instruction" and explicit `0.0` = "exit" preserved.

    Why the delegation is unconditional here (no opt-in flag): the equivalence
    was proven over 240 randomized seeds before the flip
    (`common/tests/test_position_policy.py`, now re-pinned against this function's
    pre-delegation source so the gate stays non-vacuous). The flag
    (`blend_use_policy_kernel`) gates the blend engine's per-bar step SELECTION,
    not this panel-level helper.

    `_step_asset_inertia` is deliberately KEPT and still used directly by
    `pipeline/research/blend/blend_base.py`'s default (flag-off) branch; it is not
    a thin wrapper over the kernel.
    """
    return kernel_apply_asset_inertia(
        daily,
        min_weight_change=min_weight_change,
        cash_proxy=cash_proxy,
        emergency_mask=emergency_mask,
        asymmetric_sells=asymmetric_sells,
        entry_threshold=entry_threshold,
        min_holding_bars=min_holding_bars,
        asymmetric_sell_ratio=asymmetric_sell_ratio,
    )


def _sparse_from_daily(
    daily: pd.DataFrame,
    min_weight_change: Union[float, Dict[str, float], pd.Series] = 0.0,
    cash_proxy: Optional[str] = None,
    emergency_mask: Optional[pd.Series] = None,
    asymmetric_sells: bool = False,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.DataFrame:
    """Compress a dense daily target-weight DataFrame to the sparse contract:
    NaN except on a day the target actually differs from the previous day's.

    If `min_weight_change > 0.0`, applies asset-level inertia first so micro-shifts
    below `min_weight_change` are suppressed rather than triggering rebalance rows.
    """
    if daily.empty:
        return daily
    has_inertia = False
    if isinstance(min_weight_change, (int, float)):
        has_inertia = (min_weight_change > 0.0)
    elif isinstance(min_weight_change, (dict, pd.Series)):
        has_inertia = True
    if has_inertia:
        daily = apply_asset_inertia(
            daily,
            min_weight_change=min_weight_change,
            cash_proxy=cash_proxy,
            emergency_mask=emergency_mask,
            asymmetric_sells=asymmetric_sells,
            asymmetric_sell_ratio=asymmetric_sell_ratio,
        )
    changed = (daily != daily.shift(1)).any(axis=1)
    changed.iloc[0] = True
    return daily.where(changed)


def _cap_and_deroute_to_cash(
    daily: pd.DataFrame, symbols: list, cash_proxy: str, cap: float = 1.0, cash_target: float = None,
) -> pd.DataFrame:
    """Scales down any date whose total risky-symbol weight exceeds `cap`
    (proportionally, so relative weights among the day's positions are
    preserved), then routes whatever's left of `cash_target` (defaults to
    `cap` itself) after that scaling into `cash_proxy` (an explicit
    0.0-floor write, per the sparse-weights NaN-vs-0.0 contract). Both are
    full weight fractions, not percentages. `cash_target` only needs to
    differ from `cap` for a strategy that deliberately caps deployed capital
    below 1.0 (a capital reserve) while still routing the FULL remainder up
    to 1.0 into cash, e.g. `AdaptiveGridStrategy`."""
    if cash_target is None:
        cash_target = cap
    total_risky_raw = daily.sum(axis=1)
    scale = np.where(total_risky_raw > cap, cap / total_risky_raw, 1.0)
    daily = daily.mul(scale, axis=0)
    if cash_proxy in symbols:
        daily[cash_proxy] = np.maximum(0.0, cash_target - daily.sum(axis=1))
    return daily


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
    `pipeline/pattern_mining/pmine/pattern_mining.py`'s turning-point mining,
    which both need ONE aggregate curve to detect turning points/compute
    indicators on, not a per-symbol one.
    """
    symbols = [s for s, df in universe.items() if not df.empty]
    if not symbols:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    master_index = pd.DatetimeIndex(sorted(set().union(*(universe[s].index for s in symbols))))
    if len(master_index) < 2:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    out = {}
    for field in ("Open", "High", "Low", "Close"):
        normalized_cols = {}
        for sym in symbols:
            close_series = universe[sym]["Close"].dropna()
            if not close_series.empty:
                base_val = float(close_series.iloc[0])
                normalized_cols[sym] = universe[sym][field].reindex(master_index) / base_val
        normalized = pd.DataFrame(normalized_cols, index=master_index)
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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        # Fast path when all symbols share the identical calendar
        non_empty = [s for s in symbols if not universe[s].empty]
        first_idx = universe[non_empty[0]].index if non_empty else None
        if non_empty and all(len(universe[s].index) == len(first_idx) and universe[s].index.equals(first_idx) for s in non_empty):
            n_symbols = len(symbols)
            weight = 1.0 / n_symbols if n_symbols > 0 else 0.0
            weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
            weights_df.loc[rebalance_dates, :] = weight
            return weights_df

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        for d in rebalance_dates:
            active_symbols = [
                s for s in symbols
                if s in universe and d in universe[s].index and pd.notna(universe[s].loc[d, "Close"])
            ]
            if active_symbols:
                w = 1.0 / len(active_symbols)
                for s in symbols:
                    weights_df.loc[d, s] = w if s in active_symbols else 0.0
            else:
                for s in symbols:
                    weights_df.loc[d, s] = 0.0

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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        non_empty = [s for s in symbols if not universe[s].empty]
        if not non_empty:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        # Calculate daily volatility for all symbols
        vols = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
        for sym, df in universe.items():
            if not df.empty:
                vols[sym] = realized_vol(df["Close"], window=params["vol_lookback"]).reindex(master_index)

        # Only keep values on rebalance dates
        vols_rebal = vols.loc[rebalance_dates]

        # Invert + normalize so weights sum to 1.0 across the row
        weights_rebal = vols_rebal.apply(lambda row: _inverse_vol_weights(row), axis=1)
        has_valid = weights_rebal.notna().any(axis=1)
        weights_rebal.loc[has_valid] = weights_rebal.loc[has_valid].fillna(0.0)

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
    enable_rank_band: bool = False
    buy_band_pct: float = 0.10
    sell_band_pct: float = 0.30
    delta_score_sigma: float = 0.35
    version: str = "1.1.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        # Calculate momentum (Rate of Change) for all symbols
        moms = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                moms[sym] = roc(df["Close"], period=params["mom_lookback"]).reindex(master_index)

        moms_rebal = moms.loc[rebalance_dates]

        use_rank_band = bool(params.get("enable_rank_band", getattr(self, "enable_rank_band", False)))
        if use_rank_band:
            from common.selection_band import RankBandSelection

            buy_band_pct = float(params.get("buy_band_pct", getattr(self, "buy_band_pct", 0.10)))
            sell_band_pct = float(params.get("sell_band_pct", getattr(self, "sell_band_pct", 0.30)))
            delta_score_sigma = float(
                params.get("delta_score_sigma", getattr(self, "delta_score_sigma", 0.35))
            )
            weights_rebal = RankBandSelection.apply(
                moms_rebal,
                buy_band_pct=buy_band_pct,
                sell_band_pct=sell_band_pct,
                delta_score_sigma=delta_score_sigma,
            )
            weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
            weights_df.loc[rebalance_dates] = weights_rebal
            return weights_df

        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in moms_rebal.iterrows():
            if row.isna().all():
                continue
            # Get the top N symbols by momentum, breaking ties deterministically by symbol name
            top_symbols = sorted(row.dropna().index, key=lambda s: (-row[s], s))[:top_n]
            # Equal weight among the top N
            weights_rebal.loc[date, top_symbols] = 1.0 / top_n

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal

        return weights_df

    def explain_weights(self, params: dict) -> str:
        use_rank_band = bool(params.get("enable_rank_band", getattr(self, "enable_rank_band", False)))
        if use_rank_band:
            buy_pct = int(params.get("buy_band_pct", getattr(self, "buy_band_pct", 0.10)) * 100)
            sell_pct = int(params.get("sell_band_pct", getattr(self, "sell_band_pct", 0.30)) * 100)
            sigma = params.get("delta_score_sigma", getattr(self, "delta_score_sigma", 0.35))
            return (
                f"Cross-Sectional Momentum (Rank-Band): Rebalances every {params['rebalance_freq_days']} trading days. "
                f"Reasoning: Capital flows to momentum leaders with turnover friction damping. "
                f"New entries enter within top {buy_pct}%, held names exit only when falling beyond top {sell_pct}%, "
                f"and replacements require a score gap of at least {sigma:.2f} sigma."
            )
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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        non_empty = [s for s in symbols if not universe[s].empty]
        if not non_empty:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        returns_df = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                returns_df[sym] = df["Close"].pct_change().reindex(master_index)

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
            if len(valid_symbols) == len(symbols):
                weights_rebal.loc[date, :] = w_hrp
            else:
                weights_rebal.loc[date, :] = 0.0
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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        moms = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                moms[sym] = roc(df["Close"], period=params["mom_lookback"]).reindex(master_index)

        moms_rebal = moms.loc[rebalance_dates]
        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in moms_rebal.iterrows():
            if row.isna().all():
                continue
            top_symbols = sorted(row.dropna().index, key=lambda s: (-row[s], s))[:top_n]
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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        returns_df = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                returns_df[sym] = df["Close"].pct_change().reindex(master_index)

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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        rsis = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                rsis[sym] = rsi(df["Close"], period=params["rsi_period"]).reindex(master_index)

        rsis_rebal = rsis.loc[rebalance_dates]
        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date, row in rsis_rebal.iterrows():
            if row.isna().all():
                continue
            # LOWEST RSI = most oversold -- Connors-style RSI(2) mean-reversion, breaking ties deterministically
            oversold_symbols = sorted(row.dropna().index, key=lambda s: (row[s], s))[:top_n]
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
    version: str = "1.1.0"

    # --- M1.3: opt-in turnover-penalized long-only QP. ---------------------
    # These knobs are DELIBERATELY absent from `param_grid`: the
    # strategy_generator grid search must not vary them, so the default search
    # space (and every existing backtest) is unchanged. They are read from
    # `params` (falling back to these class defaults) only when
    # `opt_turnover_penalty > 0`, i.e. when a caller explicitly opts in.
    # `opt_turnover_penalty = 0.0` means "off" -- `generate_weights` then runs
    # the pre-M1.3 code path verbatim (bit-identical output).
    opt_turnover_penalty: float = 0.0   # gamma: L1 penalty on (w - w0)
    opt_single_cap: float = 0.20        # per-symbol weight cap
    opt_cash_allowance: float = 0.0     # allowed cash residual; 0 == fully invested
    opt_sector_dev: float = 0.03        # per-sector |delta exposure| budget
    opt_prune: float = 0.005            # round |w_i - w0_i| below this to w0_i
    opt_sector_map: Optional[dict] = None  # explicit symbol -> sector labels

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        # --- M1.3 opt-in guard. MUST stay first: with the penalty at its
        # default 0.0 this method must return exactly what it returned before
        # M1.3 (verified to the last digit by
        # common/tests/test_turnover_penalized_qp.py), so the new logic lives
        # entirely in a separate method that this branch never reaches.
        turnover_penalty = float(
            params.get("opt_turnover_penalty", getattr(self, "opt_turnover_penalty", 0.0))
        )
        if turnover_penalty > 0.0:
            return self._generate_weights_turnover_penalized(universe, params, turnover_penalty)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        returns_df = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                returns_df[sym] = df["Close"].pct_change().reindex(master_index)

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
            if len(valid_symbols) == len(symbols):
                weights_rebal.loc[date, :] = w_mv
            else:
                weights_rebal.loc[date, :] = 0.0
                weights_rebal.loc[date, valid_symbols] = w_mv

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def _generate_weights_turnover_penalized(
        self, universe: Dict[str, pd.DataFrame], params: dict, turnover_penalty: float
    ) -> pd.DataFrame:
        """M1.3 opt-in path: minimum-variance weights with an L1 penalty on the
        change from the PREVIOUS target (`opt_turnover_penalty`), a per-symbol
        cap, a total-exposure box, an optional per-sector turnover budget, and
        a prune dead-zone.

        BUDGET: `lower_budget <= sum(w) <= upper_budget` with

            upper_budget = min(1, n * opt_single_cap)
            lower_budget = min(1 - opt_cash_allowance, upper_budget)   # never above upper

        i.e. fully invested (`sum(w) == 1`) by default whenever the cap can
        fund it (`n * cap >= 1`). The lower bound is what makes the turnover
        penalty scale-consistent -- with free cash slack the variance term
        drives total exposure to zero and the solve becomes independent of
        `w0`; see `_turnover_penalized_weights`'s docstring for the `nu = 0`
        derivation. When the cap makes full investment infeasible
        (`n * cap < 1`) **cash absorbs the shortfall and the cap is never
        breached**: a cap is a hard risk limit, while cash is always a
        feasible alternative -- so the budget gives way, not the cap.

        WHERE `w0` COMES FROM (the one design point a reader will question):
        `generate_weights(universe, params)` is STATELESS -- it recomputes the
        whole weight frame from prices on every call, so there is no stored
        "current holdings" object to read. `w0` is therefore reconstructed
        INSIDE the rebalance loop from this run's own previous solved target:
          - symbol still valid  -> its previous solved weight;
          - symbol newly valid  -> 0.0 (a from-cash entry is full turnover);
          - first rebalance     -> the all-zero vector (everything from cash).
        A symbol that was held but is no longer valid over the lookback is
        pinned to an explicit 0.0 in that row. Its exit contributes exactly
        `|0 - w0|` to the L1 term -- a CONSTANT with respect to the free
        variables, so it cannot change the argmin and is not passed into the
        solver's objective (including it in the solver's vector would require
        a covariance entry for a symbol with no returns). It is still fully
        counted in the sparse row as an explicit exit, which is what the
        backtester charges turnover on.
        """
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        # Reset the per-call budget-constraint note consumed by `explain_weights`.
        self._budget_constrained_note = ""

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        returns_df = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                returns_df[sym] = df["Close"].pct_change().reindex(master_index)

        single_cap = float(params.get("opt_single_cap", getattr(self, "opt_single_cap", 0.20)))
        single_cap = float(
            np.clip(single_cap if np.isfinite(single_cap) else 1.0, 0.0, 1.0)
        )
        cash_allowance = float(
            params.get("opt_cash_allowance", getattr(self, "opt_cash_allowance", 0.0))
        )
        cash_allowance = float(np.clip(cash_allowance if np.isfinite(cash_allowance) else 0.0, 0.0, 1.0))
        sector_dev = float(params.get("opt_sector_dev", getattr(self, "opt_sector_dev", 0.03)))
        prune = float(params.get("opt_prune", getattr(self, "opt_prune", 0.005)))
        sector_map = params.get("opt_sector_map", getattr(self, "opt_sector_map", None)) or {}

        lookback = params["cov_lookback"]
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=np.nan)
        # symbol -> last SOLVED target weight (only symbols valid at that date).
        prev_target: Dict[str, float] = {}

        for date in rebalance_dates:
            loc = master_index.get_loc(date)
            if loc < lookback:
                continue
            sub_ret = returns_df.iloc[loc - lookback:loc]

            # Same full-history guard as the default path -- a late-listed
            # symbol would otherwise get a zero-filled (falsely "risk-free")
            # variance in the optimizer.
            valid_symbols = [s for s in symbols if sub_ret[s].notna().all()]
            if not valid_symbols:
                continue

            if single_cap is not None and len(valid_symbols) * single_cap < 1.0 - 1e-12:
                self._budget_constrained_note = (
                    f"budget constrained by the position cap: {single_cap:.2%} x "
                    f"{len(valid_symbols)} assets funds at most "
                    f"{len(valid_symbols) * single_cap:.2%} of the book, so the "
                    f"residual {1.0 - len(valid_symbols) * single_cap:.2%} is held "
                    f"in cash (the cap is a risk limit and is never relaxed)"
                )

            cov = sub_ret[valid_symbols].cov().to_numpy()
            cov = denoise_covariance(cov, n_obs=len(sub_ret))
            # w0 for symbols that are no longer valid is pinned at 0 by
            # construction (they are not in `valid_symbols` and are written as
            # explicit 0.0 below), so only the valid subset enters the solve.
            w0 = np.array([prev_target.get(s, 0.0) for s in valid_symbols], dtype=float)

            groups = None
            if sector_dev >= 0.0:
                groups = _sector_groups(valid_symbols, sector_map)
            # From a cash start there is no PRIOR sector exposure to deviate
            # from, so the budget reads `|sum_g w - 0| <= dev`, i.e. it caps
            # TOTAL investment at `n_groups * dev` -- which contradicts the
            # fully invested budget below (`lower_budget <= sum(w)`,
            # `1 - cash_allowance` when the cap can fund it) for any realistic
            # `dev`. Drop the (vacuous, jointly infeasible) sector constraints
            # on that one solve rather than spending two failed rungs
            # discovering the same infeasibility.
            if not np.any(w0 > 0.0):
                groups = None

            # NOTE (M1.3 amendment): an earlier revision special-cased this
            # from-cash start into a plain minimum-variance solve, because with
            # only `sum(w) <= 1` the L1 term degenerated into a proportional
            # entry cost and the all-cash portfolio won. That workaround is
            # gone -- and removed as PROVABLY unnecessary: the budget is now
            # closed from below (`lower_budget <= sum(w)`), so whenever the cap
            # can fund a full book (`n * cap >= 1`) and `cash_allowance == 0`
            # we have `sum(w) == 1` and therefore `||w - w0||_1 = sum(w) = 1`,
            # a CONSTANT over the whole feasible set. A constant addend cannot
            # change the argmin, so the first rebalance reduces to the plain
            # minimum-variance portfolio automatically (pinned by
            # `test_first_rebalance_equals_min_variance_under_penalty`). In the
            # cap-constrained case the same argument holds with `sum(w) = n*cap`
            # in place of 1: the penalty term is still constant, so the solve
            # is min-variance over the reduced budget, with cash taking the rest.
            w = _turnover_penalized_weights(
                cov, w0, turnover_penalty, single_cap, groups, sector_dev, cash_allowance
            )

            # Prune dead-zone: a weight move smaller than `prune` is not worth
            # the friction, so snap it back to the previous target exactly.
            if prune > 0.0:
                snapped = np.abs(w - w0) < prune
                w = np.where(snapped, w0, w)
                w = np.clip(w, 0.0, None)
                # The cap is used VERBATIM here -- it is a hard risk limit and
                # is never raised. What the cap implies is a ceiling on total
                # exposure (`n * cap`), so the budget box below is the capped
                # one; any resulting shortfall is held in cash.
                n_valid = len(valid_symbols)
                raw_total_cap = float(n_valid * single_cap)
                upper_budget = 1.0 if raw_total_cap >= 1.0 - 1e-12 else raw_total_cap
                lower_budget = float(min(1.0 - cash_allowance, upper_budget))
                w = np.minimum(w, single_cap)
                # Restore the total-exposure box WITHOUT touching the snapped
                # entries (their exact equality with the prior target is the
                # whole point of the dead-zone), otherwise the budget refill
                # would quietly undo every prune. Also keeps pruning from
                # re-introducing exposure decay through the back door.
                free = ~snapped
                surplus = float(w.sum()) - upper_budget
                deficit = lower_budget - float(w.sum())
                if surplus > 1e-12 and free.any():
                    free_sum = float(w[free].sum())
                    if free_sum > 0.0:
                        w[free] = w[free] * max(0.0, (free_sum - surplus) / free_sum)
                elif deficit > 1e-12 and free.any():
                    headroom = single_cap - w[free]
                    room = float(headroom.sum())
                    if room > 0.0:
                        w[free] = w[free] + headroom * min(1.0, deficit / room)

            # Explicit 0.0 for every excluded symbol -- NOT NaN. A NaN cell
            # inside a rebalance row is carried forward by the backtester's
            # column-wise ffill(), silently keeping a stale position (the
            # exact bug class documented in AGENTS.md).
            weights_rebal.loc[date, :] = 0.0
            weights_rebal.loc[date, valid_symbols] = w
            prev_target = {s: float(val) for s, val in zip(valid_symbols, w)}

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict) -> str:
        base = (
            f"Minimum Variance: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Solves a constrained quadratic program for the long-only portfolio of minimum "
            f"variance over the trailing {params['cov_lookback']} days (Markowitz 1952), subject to "
            f"weights summing to 100%. Unlike HierarchicalRiskParityAllocation's heuristic "
            f"recursive-bisection substitute, this is a genuine numerical optimization and can be less "
            f"stable when the covariance matrix is poorly conditioned (falls back to equal-weight on "
            f"non-convergence)."
        )
        penalty = float(params.get("opt_turnover_penalty", getattr(self, "opt_turnover_penalty", 0.0)))
        if penalty > 0.0:
            cap = params.get("opt_single_cap", getattr(self, "opt_single_cap", 0.20))
            dev = params.get("opt_sector_dev", getattr(self, "opt_sector_dev", 0.03))
            cash = float(
                params.get("opt_cash_allowance", getattr(self, "opt_cash_allowance", 0.0))
            )
            base += (
                f" Turnover-penalized mode: objective adds {penalty:g} * L1(w - w_prev) "
                f"(Garleanu & Pedersen 2013 proportional trading costs), each weight capped at "
                f"{float(cap):.0%} (hard limit, never relaxed), total exposure held between "
                f"{max(0.0, 1.0 - cash):.2%} and 100% (cash may hold up to {cash:.2%}), and each "
                f"sector's exposure change budgeted at {float(dev):.2%}. When the cap cannot fund "
                f"a full book (n * cap < 1) the budget is constrained by the position cap: total "
                f"exposure is capped at n * cap and the residual is held as CASH -- the cap is "
                f"never raised (never raises)."
            )
            note = getattr(self, "_budget_constrained_note", "")
            if note:
                base += f" ({note})"
        return base

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
    version: str = "1.0.0"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])

        moms = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            if not df.empty:
                moms[sym] = roc(df["Close"], period=params["mom_lookback"]).reindex(master_index)

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

            top_symbols = sorted(row.index, key=lambda s: (-row[s], s))[:top_n]
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
# which direction (peak/trough) it trades -- see pipeline/pattern_mining/pmine/pattern_mining.py
# and root README's pipeline docs for why this is what lets a mined pattern's
# pipeline/strategy_generator/stratgen/generator.py factor tie-break genuinely compare it
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
    version: str = "1.0.0"

    """A trading signal built from ONE indicator pattern discovered by the
    `pattern_mining` stage's (`pipeline/pattern_mining/pmine/pattern_mining.py`)
    turning-point pattern mining -- deliberately NOT in `ALLOCATION_TEMPLATES`
    above, unlike the static templates. Those are universe-agnostic formulas,
    zero-arg constructible, searched for every basket; this template's own
    threshold comes from mining a SPECIFIC basket's aggregate-portfolio
    turning-point history first, so it can't be a static class -- it's
    instantiated from a `pattern_report.json` (or, in-process, straight from
    a mining pass) and passed into `StrategyGenerator.generate(...,
    extra_templates=[...])`, competing through the exact same grid-search +
    Equivalent Random Search validation as every static template.

    HONEST CAVEAT (read `pipeline/pattern_mining/pmine/pattern_mining.py`'s module
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

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)
        for date in rebalance_dates:
            if date in invested.index and bool(invested.loc[date]):
                active_symbols = [
                    s for s in symbols
                    if s in universe and date in universe[s].index and pd.notna(universe[s].loc[date, "Close"])
                ]
                if active_symbols:
                    w = 1.0 / len(active_symbols)
                    for s in active_symbols:
                        weights_rebal.loc[date, s] = w

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
            f"pipeline/pattern_mining/pmine/pattern_mining.py. {action.capitalize()} (equal-weight across "
            f"the basket) for {hold_days} trading days whenever the {self.feature_name} reading "
            f"{self.comparison} {effective_threshold:.4g} (mined threshold x {threshold_mult}). HONEST "
            f"CAVEAT: the mining pass needed a few bars of hindsight to LABEL a historical date a turning "
            f"point at all (zigzag confirmation lag) -- but this live signal has no such lag itself, since "
            f"it only compares today's already-known reading against the mined threshold. Passed this "
            f"workspace's standard Equivalent Random Search validation like every other template; a "
            f"significant reading during mining reflects mechanism, not a guaranteed real edge -- see "
            f"pipeline/pattern_mining/README.md."
        )

    def warmup_bars(self, params: dict) -> int:
        lb = self.feature_lookback
        base_lookback = max(lb) if isinstance(lb, (tuple, list)) else lb
        return base_lookback + params.get("hold_days", 21)


# --------------------------------------------------------------------------- #
# Late registry entry: `common.sleeve_allocator` (M2.2 core-satellite template).
#
# That module imports THIS module's base class and helpers, so importing it
# eagerly here is circular by construction. It is therefore imported at the very
# bottom (after every name it needs already exists) AND guarded on both sides:
#   * this direct-import order -> the allocator module runs to completion here,
#     registers itself, and the guard below is a no-op;
#   * the reversed order (something imports `common.sleeve_allocator` first) ->
#     its own import of this module re-enters here while it is still partially
#     initialized, `SleeveAllocator` does not exist yet, this ImportError is the
#     expected one, and the allocator registers itself when it finishes.
# A real ImportError raised from inside the allocator module would leave the
# registry at 9 entries, which `common/tests/test_sleeve_allocator.py` catches.
try:
    from common.sleeve_allocator import SleeveAllocator as _SleeveAllocator
except ImportError:  # pragma: no cover - only reachable on the reversed import order
    _SleeveAllocator = None

if _SleeveAllocator is not None and _SleeveAllocator not in ALLOCATION_TEMPLATES:
    ALLOCATION_TEMPLATES.append(_SleeveAllocator)
