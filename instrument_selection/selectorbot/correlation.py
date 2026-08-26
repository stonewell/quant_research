"""Correlation and diversification analysis for selecting a basket of
instruments to trade simultaneously: pairwise correlation, beta to a
benchmark, correlation-distance hierarchical clustering to flag redundant
candidates, and an empirical check of whether correlations on THIS universe
actually spike during stress -- since research found this diversification
benefit is not uniform across regimes (a peer-reviewed study found
correlation-cluster-based selection sometimes had the WORST, not best, risk
reduction during a market crash, because pairwise correlations tend toward 1
exactly when a crash hits).
"""

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from common.covariance import denoise_correlation


def returns_matrix(data: dict) -> pd.DataFrame:
    """Aligned daily log returns across all symbols (inner join on common dates)."""
    series = {sym: np.log(df["Close"] / df["Close"].shift(1)) for sym, df in data.items()}
    return pd.DataFrame(series).dropna(how="all").dropna(axis=0, how="any")


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    """RMT-denoised (see `common.covariance.denoise_correlation`) sample
    correlation -- the single choke point every diversification/clustering
    function below (and `selectorbot/selection.py`'s basket selectors)
    consumes, so the whole instrument-selection pipeline benefits from one
    change here."""
    return denoise_correlation(returns.corr(), n_obs=len(returns))


def beta_to_benchmark(returns: pd.DataFrame, benchmark: str) -> pd.Series:
    if benchmark not in returns.columns:
        return pd.Series(dtype=float)
    bench = returns[benchmark]
    bench_var = bench.var()
    betas = {}
    for col in returns.columns:
        if col == benchmark:
            continue
        cov = returns[col].cov(bench)
        betas[col] = cov / bench_var if bench_var != 0 else np.nan
    return pd.Series(betas)


def correlation_distance(corr: pd.DataFrame) -> pd.DataFrame:
    """d_ij = sqrt(2*(1-rho_ij)) -- a proper metric distance derived from
    correlation, as used in the correlation-clustering literature."""
    return np.sqrt(2 * (1 - corr.clip(-1, 1)))


def hierarchical_clusters(corr: pd.DataFrame, distance_threshold: float = 0.5) -> pd.Series:
    """Cluster symbols by correlation distance; symbols in the same cluster
    are redundant/highly-correlated candidates for the basket. Returns a
    Series mapping symbol -> cluster id.

    With fewer than 2 symbols (e.g. hard liquidity/history screening left
    only the benchmark), there's nothing to cluster -- `squareform` on an
    empty/1x1 distance matrix and `linkage` on the resulting empty condensed
    matrix both raise `ValueError`. Skip straight to the trivial result
    instead: 0 symbols -> empty Series, 1 symbol -> that symbol alone in its
    own cluster (still a valid, consumable result for
    `select_cluster_representatives`, which just picks the best/lowest-vol
    member of each cluster -- trivially itself here)."""
    if len(corr.index) < 2:
        labels = [0] if len(corr.index) == 1 else []
        return pd.Series(labels, index=corr.index, name="cluster")
    dist = correlation_distance(corr)
    condensed = squareform(dist.to_numpy(), checks=False)
    z = linkage(condensed, method="average")
    labels = fcluster(z, t=distance_threshold, criterion="distance")
    return pd.Series(labels, index=corr.index, name="cluster")


def redundancy_flags(corr: pd.DataFrame, threshold: float = 0.85) -> list:
    """Pairs whose correlation exceeds `threshold` -- candidates to
    deduplicate before building a diversified basket."""
    flags = []
    cols = corr.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            rho = corr.iloc[i, j]
            if rho >= threshold:
                flags.append((cols[i], cols[j], rho))
    return sorted(flags, key=lambda t: -t[2])


def correlation_regime_shift(returns: pd.DataFrame, benchmark: str, high_vol_quantile: float = 0.75) -> dict:
    """Empirically checks whether average pairwise correlation on THIS
    universe is higher during high-volatility (stress) periods than calm
    ones, rather than assuming the documented crash-correlation-spike
    phenomenon applies here without checking."""
    if benchmark not in returns.columns:
        return {"calm_avg_corr": np.nan, "stress_avg_corr": np.nan, "spike_ratio": np.nan}

    bench_vol = returns[benchmark].rolling(20, min_periods=20).std()
    threshold = bench_vol.quantile(high_vol_quantile)
    stress_dates = bench_vol[bench_vol >= threshold].index
    calm_dates = bench_vol[bench_vol < threshold].index

    others = [c for c in returns.columns if c != benchmark]

    def avg_pairwise_corr(dates):
        sub = returns.loc[returns.index.isin(dates), others]
        if len(sub) < 30 or len(others) < 2:
            return np.nan
        c = sub.corr()
        n = len(others)
        return (c.to_numpy().sum() - n) / (n * n - n)  # mean off-diagonal correlation

    calm_corr = avg_pairwise_corr(calm_dates)
    stress_corr = avg_pairwise_corr(stress_dates)
    ratio = stress_corr / calm_corr if calm_corr not in (0, np.nan) and not np.isnan(calm_corr) else np.nan
    return {"calm_avg_corr": calm_corr, "stress_avg_corr": stress_corr, "spike_ratio": ratio}
