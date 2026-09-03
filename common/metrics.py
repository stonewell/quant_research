"""Shared performance metrics used across every project in this workspace:
the standard backtesting toolkit (CAGR, Sharpe/Sortino, max drawdown, win
rate, profit factor, time-in-market) plus the Deflated Sharpe Ratio (Bailey
& Lopez de Prado, 2014) for correcting a Sharpe ratio for having picked the
best of many search trials.

Each project's own `metrics.py` re-exports the subset it needs and keeps its
own `summarize()` (the exact dict shape differs per project/report) and any
project-specific extras (e.g. expectancy stats, average holding period)
local.
"""

import numpy as np
import pandas as pd
from scipy import stats

EULER_MASCHERONI = 0.5772156649


def total_return(equity: pd.Series) -> float:
    return equity.iloc[-1] / equity.iloc[0] - 1.0


def cagr(equity: pd.Series, periods_per_year: int = 252) -> float:
    """NOTE -- undocumented-until-now "years" convention mismatch: this
    function annualizes using a TRADING-DAY-COUNT basis (`n_periods /
    periods_per_year`), whereas `common/allocation_backtester.py`'s own
    inline CAGR computation annualizes using actual CALENDAR days elapsed
    (`(last_date - first_date).days / 365.25`). The two will disagree
    whenever the equity curve has gaps or a non-standard trading calendar
    (holidays, weekends already excluded from `n_periods` but present in the
    calendar-day count). This is a genuine inconsistency, not a deliberate
    disclosed dual-convention like `win_rate`/`profit_factor` vs.
    `win_rate_from_returns`/`profit_factor_from_returns` above -- flagged
    here rather than changed, since altering either formula risks breaking
    other code/tests that depend on its current numeric behavior."""
    n_periods = len(equity)
    if n_periods < 2:
        return 0.0
    growth = equity.iloc[-1] / equity.iloc[0]
    years = n_periods / periods_per_year
    if growth <= 0 or years <= 0:
        return -1.0
    return growth ** (1 / years) - 1.0


def sharpe_ratio(returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    excess = returns - risk_free / periods_per_year
    std = excess.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return (excess.mean() / std) * np.sqrt(periods_per_year)


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (peak - equity) / peak
    return dd.max()


def win_rate(trades: pd.DataFrame) -> float:
    sells = trades[trades["side"] == "sell"]
    if sells.empty:
        return 0.0
    return (sells["pnl"] > 0).mean()


def profit_factor(trades: pd.DataFrame) -> float:
    sells = trades[trades["side"] == "sell"]
    gains = sells.loc[sells["pnl"] > 0, "pnl"].sum()
    losses = -sells.loc[sells["pnl"] < 0, "pnl"].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def win_rate_from_returns(returns: pd.Series) -> float:
    """Fraction of positive-return days/periods in a returns series.

    Distinct from `win_rate()` above: that one takes a trades DataFrame
    (`side`/`pnl` columns, one row per closed trade); this one takes a plain
    daily-returns series and counts positive-return periods directly --
    the convention `common/allocation_backtester.py` uses, since a portfolio
    backtest doesn't produce discrete per-trade P&L the way a single-position
    event-driven backtester does. Same name, different input -- do not use
    interchangeably.
    """
    if returns.empty:
        return 0.0
    return float((returns > 0).sum() / len(returns))


def profit_factor_from_returns(returns: pd.Series) -> float:
    """Ratio of summed positive-return magnitude to summed negative-return
    magnitude across a returns series (see `win_rate_from_returns` docstring
    for why this is a separate function from the trades-based `profit_factor`
    above)."""
    pos = returns[returns > 0]
    neg = returns[returns < 0]
    if neg.empty or neg.sum() == 0:
        return float("nan")
    return float(pos.sum() / abs(neg.sum()))


# --- Relative/comparative metrics (strategy vs. a baseline return series) ---
#
# Everything above this point is an ABSOLUTE metric (computed from one return/
# equity series alone). The three functions below are this file's first
# metrics that compare TWO series -- e.g. a backtested strategy against a
# single-symbol buy-and-hold baseline. All three align the two input series on
# their date-index INTERSECTION (inner join) first, silently dropping any
# non-overlapping head/tail, matching `common/allocation_backtester.py`'s own
# `closes.index.intersection(target_weights.index)` pattern elsewhere in this
# workspace. Degenerate inputs (fewer than 2 overlapping periods, or a
# zero/NaN-variance denominator) return 0.0 for every value here -- the same
# "no ratio is computable" convention `sharpe_ratio` uses for a
# zero-std denominator above, deliberately NOT `profit_factor_from_returns`'s
# `NaN` convention (that one signals a different kind of degeneracy: "no
# losses exist to divide by", not "not enough data to compute anything").

def alpha_beta(strategy_returns: pd.Series, baseline_returns: pd.Series,
               risk_free: float = 0.0, periods_per_year: int = 252) -> dict:
    """Covariance-based CAPM-style alpha/beta of `strategy_returns` against
    `baseline_returns` (e.g. a single-symbol buy-and-hold baseline's daily
    returns).

    beta = Cov(strategy_excess, baseline_excess) / Var(baseline_excess)
    alpha = (mean(strategy_excess) - beta * mean(baseline_excess)) * periods_per_year

    `alpha` is annualized via LINEAR scaling (per-period value * periods_per_year),
    matching `sharpe_ratio`'s sqrt-scaling conventions elsewhere
    in this file -- not a compounding/CAGR-style annualization. Covariance-based
    beta is algebraically identical to a single-variable OLS slope, so this
    needs no regression library.

    Returns {"alpha": float, "beta": float}.
    """
    s, b = strategy_returns.align(baseline_returns, join="inner")
    mask = s.notna() & b.notna()
    s, b = s[mask], b[mask]
    if len(s) < 2:
        return {"alpha": 0.0, "beta": 0.0}
    rf_per_period = risk_free / periods_per_year
    s_ex = s - rf_per_period
    b_ex = b - rf_per_period
    var_b = b_ex.var(ddof=1)
    if var_b == 0 or np.isnan(var_b):
        return {"alpha": 0.0, "beta": 0.0}
    beta = s_ex.cov(b_ex) / var_b
    alpha = (s_ex.mean() - beta * b_ex.mean()) * periods_per_year
    return {"alpha": float(alpha), "beta": float(beta)}


def tracking_error(strategy_returns: pd.Series, baseline_returns: pd.Series,
                    periods_per_year: int = 252) -> float:
    """Annualized standard deviation of (strategy_returns - baseline_returns)
    on the two series' aligned intersection."""
    s, b = strategy_returns.align(baseline_returns, join="inner")
    diff = (s - b).dropna()
    if len(diff) < 2:
        return 0.0
    std = diff.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return float(std * np.sqrt(periods_per_year))


def information_ratio(strategy_returns: pd.Series, baseline_returns: pd.Series,
                       periods_per_year: int = 252) -> float:
    """Annualized mean(strategy_returns - baseline_returns) / annualized std of
    the same difference -- structurally identical to `sharpe_ratio`'s own
    `(excess.mean() / excess.std()) * sqrt(periods_per_year)` shape, with
    "excess over the baseline" standing in for "excess over the risk-free
    rate". Computed from ONE aligned diff (not by dividing by a separately
    recomputed `tracking_error(...)`, which would re-align/re-diff a second
    time and risk float drift from computing the same quantity twice)."""
    s, b = strategy_returns.align(baseline_returns, join="inner")
    diff = (s - b).dropna()
    if len(diff) < 2:
        return 0.0
    std = diff.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return float((diff.mean() / std) * np.sqrt(periods_per_year))


def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """Expected maximum Sharpe ratio achievable by pure luck across
    `n_trials` independent trials with cross-trial Sharpe std `sharpe_std`
    (Bailey & Lopez de Prado's Euler-Mascheroni approximation)."""
    if n_trials <= 1:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return sharpe_std * ((1 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def deflated_sharpe_ratio(observed_sharpe: float, n_trials: int, n_obs: int,
                           skewness: float = 0.0, kurtosis: float = 3.0, sharpe_std: float = None) -> float:
    """Probability the true Sharpe ratio exceeds zero, after correcting for
    (a) having picked the best of `n_trials` independent trials and (b)
    non-Normal returns (skewness/kurtosis -- pass regular, not excess,
    kurtosis; 3.0 is the Normal-distribution value).

    `sharpe_std` is the cross-trial standard deviation of Sharpe ratios --
    ideally computed from the actual candidate Sharpes your search tried; if
    unavailable, `1/sqrt(n_obs)` is used as a rough default (the asymptotic
    std of a Sharpe ratio estimate under IID Normal returns).
    """
    if sharpe_std is None:
        sharpe_std = 1.0 / np.sqrt(max(n_obs, 2))
    sr0 = expected_max_sharpe(n_trials, sharpe_std)
    denom = np.sqrt(max(1e-12, 1 - skewness * observed_sharpe + ((kurtosis - 1) / 4) * observed_sharpe ** 2))
    z = (observed_sharpe - sr0) * np.sqrt(max(n_obs - 1, 1)) / denom
    return stats.norm.cdf(z)
