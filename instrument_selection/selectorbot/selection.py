"""Turn per-instrument scores + a correlation matrix into an actual CHOSEN
SUBSET of K instruments -- the discrete selection step this project's
scoring/clustering/redundancy-flagging stopped short of solving.
`correlation.redundancy_flags()` only WARNS that, e.g., QQQ and XLK are
97.5% correlated; nothing previously resolved that into a final list. Naive
"top-K by `overall_selection_score`" is the obvious first idea, but it can
happily select a basket dominated by a handful of mutually-redundant names
that all happened to score well individually.

A deep-research pass specifically on this discrete-selection problem (as
distinct from portfolio WEIGHT optimization -- Markowitz, HRP, risk parity,
etc., which allocate capital across an already-chosen set) found three
citable, complementary method families with real evidentiary backing:

1. `select_cluster_representatives` -- cluster by correlation distance
   (reusing `correlation.hierarchical_clusters`), then pick ONE
   representative per cluster. The one peer-reviewed, theoretically-
   grounded method for exactly this (ACC / "Asset Clustering through
   Correlation," Tang, Xu & Zhou, Expert Systems with Applications 2022;
   19-year S&P 500 backtest beating SPY and sector-ETF baskets on
   Sharpe/Sortino/Calmar) proves a NARROWER and more specific rule than
   "pick the cluster's highest scorer": among portfolios built by picking
   one asset per correlation-cluster, choosing the LOWEST-VARIANCE asset in
   each cluster minimizes portfolio variance (their Theorem 2 -- a claim
   generalizing this into a universal "best representative" rule for any
   objective was separately checked and does NOT hold; the guarantee is
   conditional on that specific objective). `representative_rule=
   "lowest_volatility"` implements the proven rule when a volatility Series
   is available; `"highest_score"` is offered as a disclosed, unproven
   FALLBACK for when it isn't, or when minimizing portfolio variance isn't
   actually the caller's goal.

2. `select_diversified_greedy` -- Max-Sum Diversification (Borodin, Lee &
   Ye, PODS 2012 / ACM Transactions on Algorithms), which explicitly names
   "portfolio management" as an application domain: for a FIXED cardinality
   K, greedily add the candidate maximizing
   `score[i] + diversity_weight * sum(distance(i, j) for j already chosen)`.
   The source proves a constant-factor approximation guarantee for exactly
   this greedy construction (cardinality-constrained case, quality function
   monotone submodular -- a plain per-instrument score trivially qualifies
   as the modular special case). Rigorous CS/OR theory; no finance-specific
   backtest exists in the surviving literature for this exact algorithm, so
   treat the guarantee as "provably not worse than a bad heuristic," not as
   validated financial performance.

3. `select_diversified_threshold_greedy` -- simpler and more directly tied
   to this project's existing `max_cluster_correlation` config: walk
   candidates in descending score order, keep a candidate only if its
   correlation to EVERY already-selected instrument stays below the
   threshold. Determines the resulting subset size itself rather than
   fixing K in advance -- echoing Yang, Rea & Rea (2016, Journal of
   Investment Strategies)'s finding that the number of instruments needed
   for adequate diversification is not a fixed constant, it shrinks when
   correlations rise and grows when they fall.

Research found no head-to-head comparison of these three families against
each other (each is validated against a naive top-K or benchmark baseline
in its own source, not against the others) -- running more than one of
these on the same universe and comparing is this project's own exploration,
not a reproduction of a verified ranking among them.

Documented but deliberately NOT implemented this pass: the Generalized
MaxMean Dispersion Problem (Prokopyev et al. 2009), which folds the choice
of K itself into a ratio-maximization objective (maximize total pairwise
diversity divided by total selected weight, subject only to a minimum-size
constraint) rather than fixing K in advance -- a real fractional-programming
problem, more involved to solve correctly than the two greedy methods above;
and PCA-based backward-elimination variable selection (Yang, Rea & Rea
2016), which needs an eigendecomposition of the returns matrix this project
doesn't otherwise compute. Both are legitimate researched alternatives, left
undone to keep the implemented surface matched to what's actually tested.

Cross-cutting, UNRESOLVED failure mode inherited by all three methods below
(Page & Panariello 2018, *Financial Analysts Journal*, corroborating this
project's own prior Cotter & Suurlaht 2015 finding in correlation.py):
left-tail (crash) correlations run systematically higher than calm-period
correlations across asset classes -- a persistent, well-documented
regularity (also shown in Longin & Solnik 2001; Ang & Bekaert 2002), not a
one-off episode. Every method here consumes a single, unconditional
correlation matrix (`correlation.correlation_matrix`, computed over the
whole sample) -- none of them, nor the wider literature searched for this
revision, resolves the fact that a basket selected to be diversified in
normal times can still become far more correlated than the matrix suggests
exactly when a crash makes diversification matter most.
"""

import pandas as pd

from .correlation import correlation_distance, hierarchical_clusters


def select_cluster_representatives(scores: pd.Series, corr: pd.DataFrame, distance_threshold: float = 0.5,
                                    volatility: pd.Series = None, representative_rule: str = None) -> list:
    """One representative per correlation cluster. `distance_threshold` uses
    the same correlation-distance metric (`d = sqrt(2*(1-rho))`) as
    `correlation.hierarchical_clusters` -- a lower threshold means more,
    smaller clusters (stricter deduplication)."""
    symbols = [s for s in corr.index if s in scores.index]
    corr = corr.loc[symbols, symbols]
    clusters = hierarchical_clusters(corr, distance_threshold=distance_threshold)

    if representative_rule is None:
        representative_rule = "lowest_volatility" if volatility is not None else "highest_score"
    if representative_rule not in ("lowest_volatility", "highest_score"):
        raise ValueError(f"Unknown representative_rule: {representative_rule!r}")
    if representative_rule == "lowest_volatility" and volatility is None:
        raise ValueError("representative_rule='lowest_volatility' requires a volatility Series")

    chosen = []
    for cluster_id in sorted(clusters.unique()):
        members = clusters[clusters == cluster_id].index.tolist()
        if representative_rule == "lowest_volatility":
            member_vol = volatility.reindex(members).dropna()
            best = member_vol.idxmin() if not member_vol.empty else scores.reindex(members).idxmax()
        else:
            best = scores.reindex(members).idxmax()
        chosen.append(best)
    return chosen


def select_diversified_greedy(scores: pd.Series, corr: pd.DataFrame, k: int, diversity_weight: float = 1.0) -> list:
    """Max-Sum Diversification greedy (Borodin, Lee & Ye 2012): fixed
    cardinality `k`, marginal gain = own score + `diversity_weight` times
    the sum of correlation-distance to every already-selected instrument."""
    symbols = [s for s in corr.index if s in scores.index]
    dist = correlation_distance(corr.loc[symbols, symbols])
    remaining = list(symbols)
    selected = []

    while remaining and len(selected) < k:
        if not selected:
            best = scores.reindex(remaining).idxmax()
        else:
            def marginal_gain(sym):
                diversity = sum(dist.loc[sym, s] for s in selected)
                return scores[sym] + diversity_weight * diversity
            best = max(remaining, key=marginal_gain)
        selected.append(best)
        remaining.remove(best)
    return selected


def select_diversified_threshold_greedy(scores: pd.Series, corr: pd.DataFrame, max_correlation: float = 0.85,
                                        max_k: int = None) -> list:
    """Walk candidates in descending score order; keep one only if its
    correlation to every already-selected instrument stays below
    `max_correlation` -- the subset size is determined by the data, not
    fixed in advance, unless `max_k` caps it."""
    symbols = [s for s in corr.index if s in scores.index]
    ranked = scores.reindex(symbols).sort_values(ascending=False).index.tolist()
    selected = []
    for sym in ranked:
        if max_k is not None and len(selected) >= max_k:
            break
        if all(corr.loc[sym, s] < max_correlation for s in selected):
            selected.append(sym)
    return selected


def select_max_diversification_ratio(scores: pd.Series, corr: pd.DataFrame, volatility: pd.Series,
                                     k: int = None, score_weight: float = 0.1) -> list:
    """Maximum Diversification Ratio selection (Choueifaty & Coignard 2008,
    Journal of Portfolio Management): greedily selects assets to maximize the
    portfolio Diversification Ratio DR = (w^T sigma) / sqrt(w^T Sigma w),
    blended with individual quality scores.
    
    DR measures the ratio of weighted average asset volatilities to total
    portfolio volatility. Highly correlated assets reduce DR, while uncorrelated
    or negatively correlated assets increase DR."""
    import numpy as np

    symbols = [s for s in corr.index if s in scores.index and s in volatility.index]
    if not symbols:
        return []

    vol = volatility.reindex(symbols).fillna(volatility.median())
    corr_sub = corr.loc[symbols, symbols]
    
    # Precompute covariance elements: cov_ij = vol_i * vol_j * corr_ij
    vol_arr = vol.to_numpy()
    cov_matrix = pd.DataFrame(
        np.outer(vol_arr, vol_arr) * corr_sub.to_numpy(),
        index=symbols, columns=symbols
    )

    k = k or len(symbols)
    remaining = list(symbols)
    selected = []

    def calc_dr(candidate_set):
        if not candidate_set:
            return 0.0
        n = len(candidate_set)
        sum_vol = vol.reindex(candidate_set).sum()
        sub_cov = cov_matrix.loc[candidate_set, candidate_set].to_numpy()
        port_var = sub_cov.sum() / (n * n)
        if port_var <= 0:
            return 0.0
        return sum_vol / (np.sqrt(port_var) * n)

    while remaining and len(selected) < k:
        if not selected:
            # Start with the highest individual score
            best = max(remaining, key=lambda s: scores[s])
        else:
            def marginal_gain(sym):
                cand = selected + [sym]
                dr = calc_dr(cand)
                # Normalize individual score to 0-1 range for blending
                norm_score = scores[sym] / 100.0 if 100.0 in scores.values or scores.max() > 1 else scores[sym]
                return dr + score_weight * norm_score

            best = max(remaining, key=marginal_gain)

        selected.append(best)
        remaining.remove(best)

    return selected
