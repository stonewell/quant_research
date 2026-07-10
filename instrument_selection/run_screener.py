#!/usr/bin/env python
"""CLI entry point: screen a universe of stocks/ETFs for quant-strategy
suitability -- strategy-agnostic (liquidity, volatility, statistical
predictability, diversification, history/fund-quality), not tied to any one
strategy family.

Example:
    python run_screener.py --start 2015-01-01 --end 2024-12-31
    python run_screener.py --universe SPY QQQ AAPL MSFT NVDA GLD TLT
"""

import argparse
import os

import pandas as pd

from selectorbot import correlation, liquidity, persistence, plotting, scoring, volatility
from selectorbot.config import SelectionConfig
from selectorbot.data import fetch_fund_metadata, load_universe

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    d = SelectionConfig()
    p = argparse.ArgumentParser(description="Quant-strategy instrument screener")
    p.add_argument("--universe", nargs="+", default=d.universe)
    p.add_argument("--benchmark", default=d.benchmark)
    p.add_argument("--start", default=d.start)
    p.add_argument("--end", default=d.end)
    p.add_argument("--interval", default=d.interval)
    p.add_argument("--min-avg-dollar-volume", type=float, default=d.min_avg_dollar_volume)
    p.add_argument("--max-cluster-correlation", type=float, default=d.max_cluster_correlation)
    p.add_argument("--no-fund-metadata", action="store_false", dest="fetch_fund_metadata",
                    default=d.fetch_fund_metadata, help="skip best-effort expense-ratio/AUM lookup")
    p.add_argument("--top-n", type=int, default=8, help="how many top-ranked instruments to print")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-plots", action="store_true")
    return p


def main():
    args = build_arg_parser().parse_args()
    config = SelectionConfig(
        universe=args.universe, benchmark=args.benchmark, start=args.start, end=args.end, interval=args.interval,
        min_avg_dollar_volume=args.min_avg_dollar_volume, max_cluster_correlation=args.max_cluster_correlation,
        fetch_fund_metadata=args.fetch_fund_metadata,
    )
    universe = list(dict.fromkeys(config.universe + [config.benchmark]))  # ensure benchmark is included, no dupes

    print(f"Loading {len(universe)} symbols from {config.start} to {config.end} ...")
    data = load_universe(universe, config.start, config.end, config.interval, use_cache=not args.no_cache)
    print(f"Loaded {len(data)}/{len(universe)} symbols (see warnings above for any skipped).")

    rows = {}
    for symbol, df in data.items():
        liq = liquidity.liquidity_summary(df, config.liquidity_window)
        vol = volatility.volatility_summary(df, config)
        per = persistence.persistence_summary(df["Close"], config)
        history_years = (df.index[-1] - df.index[0]).days / 365.25
        rows[symbol] = {**liq, **vol, **per, "history_years": history_years}
    metrics = pd.DataFrame(rows).T
    for col in metrics.columns:
        if col != "regime_label":
            metrics[col] = pd.to_numeric(metrics[col], errors="coerce")

    returns = correlation.returns_matrix(data)
    corr = correlation.correlation_matrix(returns)
    betas = correlation.beta_to_benchmark(returns, config.benchmark)
    redundant = correlation.redundancy_flags(corr, config.max_cluster_correlation)
    regime_shift = correlation.correlation_regime_shift(returns, config.benchmark)

    # Average correlation to the rest of the universe, feeding the
    # diversification component of the selection score.
    n = len(corr)
    avg_corr = (corr.sum(axis=1) - 1) / max(n - 1, 1)
    metrics["avg_correlation_to_universe"] = avg_corr.reindex(metrics.index)

    if config.fetch_fund_metadata:
        print("Fetching best-effort fund metadata (expense ratio, AUM) ...")
        meta_rows = {symbol: fetch_fund_metadata(symbol) for symbol in metrics.index}
        meta_df = pd.DataFrame(meta_rows).T
        metrics["expense_ratio"] = pd.to_numeric(meta_df["expense_ratio"], errors="coerce")
        metrics["total_assets"] = pd.to_numeric(meta_df["total_assets"], errors="coerce")

    scored = scoring.score_universe(metrics, min_history_years_for_full_credit=config.min_history_years_for_full_credit)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print("\n=== Liquidity, volatility, and persistence metrics ===")
    display_cols = ["avg_dollar_volume", "median_spread_pct", "realized_vol_annualized_pct",
                     "atr_pct_mean", "adx_mean", "hurst", "hurst_significant", "regime_label", "history_years"]
    print(scored[display_cols].round(3))

    print("\n=== Beta to benchmark ===")
    print(betas.round(2).sort_values(ascending=False))

    print(f"\n=== Redundant pairs (correlation >= {config.max_cluster_correlation}) ===")
    if redundant:
        for a, b, rho in redundant:
            print(f"  {a} <-> {b}: {rho:.3f}")
    else:
        print("  none found")

    print("\n=== Correlation regime-shift check (does correlation spike in stress on THIS universe?) ===")
    print(f"  calm-period avg pairwise correlation:   {regime_shift['calm_avg_corr']:.3f}")
    print(f"  stress-period avg pairwise correlation: {regime_shift['stress_avg_corr']:.3f}")
    print(f"  spike ratio (stress/calm):              {regime_shift['spike_ratio']:.2f}x")

    print("\n=== Selection score components (0-100 each) ===")
    score_cols = ["liquidity_score", "vol_adequacy_score", "predictability_score",
                  "diversification_score", "history_adequacy_score"]
    if "etf_expense_score" in scored.columns:
        score_cols += ["etf_expense_score", "etf_aum_score"]
    print(scored[score_cols + ["overall_selection_score"]].round(1))

    print(f"\n=== Top {args.top_n} candidates by overall selection score ===")
    print(scored["overall_selection_score"].sort_values(ascending=False).head(args.top_n).round(1))

    os.makedirs(RESULTS_DIR, exist_ok=True)
    scored_path = os.path.join(RESULTS_DIR, "screening_report.csv")
    scored.to_csv(scored_path)
    corr.to_csv(os.path.join(RESULTS_DIR, "correlation_matrix.csv"))
    print(f"\nSaved full report to {scored_path}")

    if not args.no_plots:
        p1 = plotting.plot_correlation_heatmap(corr)
        p2 = plotting.plot_dendrogram(corr)
        p3 = plotting.plot_hurst_vs_volatility(scored)
        print(f"Saved chart to {p1}")
        print(f"Saved chart to {p2}")
        print(f"Saved chart to {p3}")


if __name__ == "__main__":
    main()
