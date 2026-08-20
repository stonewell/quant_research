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

from selectorbot import (
    candlestick,
    correlation,
    liquidity,
    momentum,
    persistence,
    plotting,
    screening,
    scoring,
    selection,
    volatility,
)
from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.reporting import utc_timestamp
from common.universe import add_universe_cli_args, resolve_universe_from_args
from selectorbot.config import SelectionConfig
from selectorbot.data import fetch_fund_metadata

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()  # same workspace-wide <repo_root>/data cache dir that selectorbot.data.DATA_DIR resolves to


def build_arg_parser() -> argparse.ArgumentParser:
    d = SelectionConfig()
    p = argparse.ArgumentParser(description="Quant-strategy instrument screener")
    add_universe_cli_args(p, default_universe=d.universe)
    p.add_argument("--benchmark", default=d.benchmark)
    p.add_argument("--start", default=d.start)
    p.add_argument("--end", default=d.end)
    p.add_argument("--interval", default=d.interval)
    p.add_argument("--min-avg-dollar-volume", type=float, default=d.min_avg_dollar_volume,
                    help="hard liquidity gate -- instruments below this are excluded before scoring/selection, not just soft-scored")
    p.add_argument("--min-history-years", type=float, default=d.min_history_years,
                    help="hard history-length gate -- distinct from --min-history-years-for-full-credit, which is a soft scoring threshold")
    p.add_argument("--max-cluster-correlation", type=float, default=d.max_cluster_correlation)
    p.add_argument("--no-fund-metadata", action="store_false", dest="fetch_fund_metadata",
                    default=d.fetch_fund_metadata, help="skip best-effort expense-ratio/AUM lookup")
    p.add_argument("--top-n", type=int, default=8, help="how many top-ranked instruments to print")
    p.add_argument("--select-method", choices=["top_k", "cluster", "greedy", "threshold", "max_diversification"], default="threshold",
                   help="how to turn scores+correlation into a final chosen basket (see README); "
                        "'top_k' is the naive baseline all others are designed to improve on")
    p.add_argument("--select-k", type=int, default=None,
                   help="basket size for --select-method top_k/greedy (required for greedy; default 8 for top_k)")
    p.add_argument("--select-max-k", type=int, default=None,
                   help="optional cap on basket size for --select-method threshold/max_diversification "
                        "(which otherwise size themselves from the data)")
    add_data_provider_cli_args(p)   # default_provider="yfinance" matches current default
    p.add_argument("--no-plots", action="store_true")
    return p


def select_basket(args, config, scored: pd.DataFrame, corr: pd.DataFrame) -> list:
    """Dispatch `--select-method` to the right `selection.py` function,
    honoring each method's documented CLI knobs (see README's `--select-*`
    table) -- split out from `main()` so this wiring is unit-testable
    without needing to load market data."""
    scores = scored["overall_selection_score"]
    if args.select_method == "top_k":
        return list(scores.sort_values(ascending=False).head(args.select_k or args.top_n).index)
    elif args.select_method == "cluster":
        realized_vol = pd.to_numeric(scored["realized_vol_annualized_pct"], errors="coerce")
        # distance_threshold is a correlation-DISTANCE (d=sqrt(2*(1-rho))), not a raw correlation --
        # converted here so --max-cluster-correlation means the same thing across all select methods.
        distance_threshold = (2 * (1 - config.max_cluster_correlation)) ** 0.5
        return selection.select_cluster_representatives(
            scores, corr, distance_threshold=distance_threshold, volatility=realized_vol)
    elif args.select_method == "greedy":
        if not args.select_k:
            raise SystemExit("--select-method greedy requires --select-k")
        return selection.select_diversified_greedy(scores, corr, k=args.select_k)
    elif args.select_method == "max_diversification":
        realized_vol = pd.to_numeric(scored["realized_vol_annualized_pct"], errors="coerce")
        # k=args.select_max_k (not --select-k/--top-n): per README, this method
        # self-sizes to the full surviving universe unless --select-max-k caps it,
        # matching the --select-method threshold branch below.
        return selection.select_max_diversification_ratio(
            scores, corr, volatility=realized_vol, k=args.select_max_k)
    else:
        return selection.select_diversified_threshold_greedy(
            scores, corr, max_correlation=config.max_cluster_correlation, max_k=args.select_max_k)


def main():
    args = build_arg_parser().parse_args()
    d = SelectionConfig()
    resolved_universe = resolve_universe_from_args(args, default_symbols=d.universe)
    config = SelectionConfig(
        universe=resolved_universe, benchmark=args.benchmark, start=args.start, end=args.end, interval=args.interval,
        min_avg_dollar_volume=args.min_avg_dollar_volume, min_history_years=args.min_history_years,
        max_cluster_correlation=args.max_cluster_correlation, fetch_fund_metadata=args.fetch_fund_metadata,
    )
    universe = list(dict.fromkeys(config.universe + [config.benchmark]))  # ensure benchmark is included, no dupes

    data_kwargs = build_data_kwargs(args)

    data = load_universe_with_banner(universe, config.start, config.end, config.interval,
                                      use_cache=not args.no_cache, cache_dir=DATA_DIR, data_kwargs=data_kwargs,
                                      require_nonempty=False, cache_max_age_days=args.cache_ttl_days,
                                      loading_msg=f"Loading {len(universe)} symbols from {config.start} to {config.end} ...")

    rows = {}
    for symbol, df in data.items():
        liq = liquidity.liquidity_summary(df, config.liquidity_window)
        vol = volatility.volatility_summary(df, config)
        per = persistence.persistence_summary(df["Close"], config)
        cnd = candlestick.candlestick_summary(df, config)
        mom = momentum.momentum_summary(df, config)
        history_years = (df.index[-1] - df.index[0]).days / 365.25
        rows[symbol] = {**liq, **vol, **per, **cnd, **mom, "history_years": history_years}
    metrics = pd.DataFrame(rows).T
    non_numeric = {"regime_label", "candlestick_label", "momentum_label"}
    for col in metrics.columns:
        if col not in non_numeric:
            metrics[col] = pd.to_numeric(metrics[col], errors="coerce")

    metrics, screened_out = screening.screen_universe(metrics, config, benchmark=config.benchmark)
    print(f"\n=== Hard screen: liquidity >= ${config.min_avg_dollar_volume:,.0f}/day, "
          f"history >= {config.min_history_years} years ===")
    if not screened_out.empty:
        print(f"  excluded {len(screened_out)}/{len(screened_out) + len(metrics)}: "
              f"{dict(zip(screened_out.index, screened_out['screen_fail_reason']))}")
    else:
        print("  none excluded")
    data = {symbol: df for symbol, df in data.items() if symbol in metrics.index}

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
        meta_rows = {symbol: fetch_fund_metadata(symbol, **data_kwargs) for symbol in metrics.index}
        meta_df = pd.DataFrame(meta_rows).T
        metrics["expense_ratio"] = pd.to_numeric(meta_df["expense_ratio"], errors="coerce")
        metrics["total_assets"] = pd.to_numeric(meta_df["total_assets"], errors="coerce")

    scored = scoring.score_universe(metrics, min_history_years_for_full_credit=config.min_history_years_for_full_credit)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print("\n=== Liquidity, volatility, downside risk, and persistence metrics ===")
    display_cols = ["avg_dollar_volume", "median_spread_pct", "realized_vol_annualized_pct",
                    "downside_vol_annualized_pct", "downside_vol_ratio",
                    "atr_pct_mean", "adx_mean", "hurst", "hurst_significant", "regime_label", "history_years"]
    print(scored[[c for c in display_cols if c in scored.columns]].round(3))

    print("\n=== Candlestick reversal-pattern predictability (mostly no edge is the EXPECTED result -- see README) ===")
    candle_cols = ["candlestick_edge", "candlestick_significant", "candlestick_p_value",
                   "candlestick_n_signals", "candlestick_label"]
    print(scored[[c for c in candle_cols if c in scored.columns]].round(4))

    print("\n=== Time-series-momentum predictability (bootstrap-tested per instrument; crash-caveated -- see README) ===")
    mom_cols = ["momentum_edge", "momentum_significant", "momentum_p_value",
                "momentum_lookback_return", "pct_days_above_trend_ma", "momentum_label"]
    print(scored[[c for c in mom_cols if c in scored.columns]].round(3))

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
                  "momentum_score", "candlestick_score", "diversification_score", "history_adequacy_score"]
    if "etf_expense_score" in scored.columns:
        score_cols += ["etf_expense_score", "etf_aum_score"]
    print(scored[score_cols + ["overall_selection_score"]].round(1))

    print(f"\n=== Top {args.top_n} candidates by overall selection score (individual ranking, ignores redundancy) ===")
    print(scored["overall_selection_score"].sort_values(ascending=False).head(args.top_n).round(1))

    chosen = select_basket(args, config, scored, corr)

    print(f"\n=== Chosen basket ({args.select_method}, {len(chosen)} instruments) -- "
          f"see README for what each method does and doesn't guarantee ===")
    print(scored.loc[chosen, "overall_selection_score"].sort_values(ascending=False).round(1))

    os.makedirs(RESULTS_DIR, exist_ok=True)
    scored_path = os.path.join(RESULTS_DIR, "screening_report.csv")
    scored.to_csv(scored_path)
    corr.to_csv(os.path.join(RESULTS_DIR, "correlation_matrix.csv"))
    if not screened_out.empty:
        screened_out.to_csv(os.path.join(RESULTS_DIR, "screened_out.csv"))
    print(f"\nSaved full report to {scored_path}")

    import json
    basket_json_path = os.path.join(RESULTS_DIR, "basket.json")
    with open(basket_json_path, "w") as f:
        json.dump({
            "basket": list(chosen),
            "method": args.select_method,
            "date_generated": utc_timestamp()
        }, f, indent=2)
    print(f"Saved chosen basket to {basket_json_path}")

    if not args.no_plots:
        p1 = plotting.plot_correlation_heatmap(corr)
        p2 = plotting.plot_dendrogram(corr)
        p3 = plotting.plot_hurst_vs_volatility(scored)
        print(f"Saved chart to {p1}")
        print(f"Saved chart to {p2}")
        print(f"Saved chart to {p3}")


if __name__ == "__main__":
    main()
