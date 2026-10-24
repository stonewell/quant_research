#!/usr/bin/env python3
"""Cross-Ablation Matrix Orchestrator for Institutional Blend Strategies.

Orchestrates the 6-cell cross-ablation matrix comparing:
- Run 0: Baseline (All Track C & Track T = False)
- Run 1: Track C Core (regime=True, cyq_gate=True)
- Run 2: Track C Full (All Track C = True)
- Run 3: Track T Core (intraday=True, nesting=True)
- Run 4: Track T Full (All Track T = True)
- Run 5: Synergy All (All Track C = True, All Track T = True)

Across Round 1 (2022-01-01 to 2026-01-01) and Round 2 (2025-11-28 to 2026-10-01).
Parses walkforward_summary.json across all strategies and generates comprehensive
Markdown and CSV comparison tables.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional


MATRIX_CONFIGS = [
    {
        "id": 0,
        "name": "baseline",
        "description": "Baseline (All Track C & Track T = False)",
        "params": {},
    },
    {
        "id": 1,
        "name": "track_c_core",
        "description": "Track C Core (regime=True, cyq_gate=True)",
        "params": {
            "blend_enable_structural_regime": True,
            "blend_enable_cyq_quality_gate": True,
            "enable_structural_regime": True,
            "enable_cyq_quality_gate": True,
        },
    },
    {
        "id": 2,
        "name": "track_c_full",
        "description": "Track C Full (All Track C = True)",
        "params": {
            "blend_enable_structural_regime": True,
            "blend_enable_cyq_quality_gate": True,
            "blend_min_cyq_winner_ratio": 0.20,
            "blend_enable_vp_confluence_sizing": True,
            "blend_enable_spring_fast_entry": True,
            "blend_enable_structural_chandelier_stop": True,
            "enable_structural_regime": True,
            "enable_cyq_quality_gate": True,
            "min_cyq_winner_ratio": 0.20,
            "enable_vp_confluence_sizing": True,
            "enable_spring_fast_entry": True,
            "enable_structural_chandelier_stop": True,
        },
    },
    {
        "id": 3,
        "name": "track_t_core",
        "description": "Track T Core (intraday=True, nesting=True)",
        "params": {
            "blend_enable_intraday_5m": True,
            "blend_enable_5m_interval_nesting": True,
            "enable_intraday_5m": True,
            "enable_5m_interval_nesting": True,
        },
    },
    {
        "id": 4,
        "name": "track_t_full",
        "description": "Track T Full (All Track T = True)",
        "params": {
            "blend_enable_intraday_5m": True,
            "blend_enable_5m_interval_nesting": True,
            "blend_intraday_upvol_thresh": 0.58,
            "blend_use_intraday_downside_vol": True,
            "blend_enable_5m_stop_filter": True,
            "enable_intraday_5m": True,
            "enable_5m_interval_nesting": True,
            "intraday_upvol_thresh": 0.58,
            "use_intraday_downside_vol": True,
            "enable_5m_stop_filter": True,
        },
    },
    {
        "id": 5,
        "name": "synergy_all",
        "description": "Synergy All (All Track C = True, All Track T = True)",
        "params": {
            # Track C Full
            "blend_enable_structural_regime": True,
            "blend_enable_cyq_quality_gate": True,
            "blend_min_cyq_winner_ratio": 0.20,
            "blend_enable_vp_confluence_sizing": True,
            "blend_enable_spring_fast_entry": True,
            "blend_enable_structural_chandelier_stop": True,
            "enable_structural_regime": True,
            "enable_cyq_quality_gate": True,
            "min_cyq_winner_ratio": 0.20,
            "enable_vp_confluence_sizing": True,
            "enable_spring_fast_entry": True,
            "enable_structural_chandelier_stop": True,
            # Track T Full
            "blend_enable_intraday_5m": True,
            "blend_enable_5m_interval_nesting": True,
            "blend_intraday_upvol_thresh": 0.58,
            "blend_use_intraday_downside_vol": True,
            "blend_enable_5m_stop_filter": True,
            "enable_intraday_5m": True,
            "enable_5m_interval_nesting": True,
            "intraday_upvol_thresh": 0.58,
            "use_intraday_downside_vol": True,
            "enable_5m_stop_filter": True,
        },
    },
]

ROUNDS_CONFIG = [
    {
        "round": 1,
        "name": "Round 1 (2022-01-01 to 2026-01-01)",
        "start": "2022-01-01",
        "end": "2026-01-01",
    },
    {
        "round": 2,
        "name": "Round 2 (2025-11-28 to 2026-10-01)",
        "start": "2025-11-28",
        "end": "2026-10-01",
    },
]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (P1.6, corrected 2026-10-24) The pre-P1.6 default was a developer-machine
# absolute path (`.../simulate/202609/core_satellite_20_stocks.txt`, outside the
# repo; docs/version.md §16.15.8 records it as unrecoverable from git). P1.6
# redirected it to `docs/universe/china/core_satellite_20_stocks.txt` -- a path that
# was only ever a PROPOSAL in §16.15.8 and was never committed, so a bare invocation
# died with FileNotFoundError. The in-repo pool the China blend reports in
# `docs/reports/china/chan_*_blend/` were actually produced with (and which
# docs/chan_volume_profile_and_multi_theory_quant_synthesis_plan.md,
# docs/development_plan.md and docs/version.md all cite as
# `--universe docs/universe/china/core_satellite_22_stocks.txt`) is this one: 22
# symbols, `#`-comments, `<6 digits>.<SH|SZ>` rows -- exactly the format
# `backtest_blend_strategies.sh --universe` -> `--universe-file` ->
# `common.universe.FileUniverseProvider` reads.
_DEFAULT_UNIVERSE = os.path.join(_REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt")

# `FileUniverseProvider` also accepts bare tickers, but every pool file under
# docs/universe/china is `<6 digits>.<SH|SZ|BJ>`. A default that does not parse to that
# shape points at the wrong kind of file, not at a universe -- fail loudly instead of
# handing 22 garbage "symbols" to 6 matrix cells x 2 rounds of backtests.
_UNIVERSE_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_MIN_UNIVERSE_SYMBOLS = 2


def load_universe_symbols(path: str) -> List[str]:
    """Parse `path` as a universe file, naming every failure mode explicitly.

    `--universe` is forwarded to `scripts/backtest_blend_strategies.sh` ->
    `--universe-file` -> `FileUniverseProvider`, i.e. it is consumed deep inside a
    batch worker pool; a bad path therefore surfaced as an opaque subprocess failure
    (or, worse, a silent short universe). This guard runs in the orchestrator before
    any subprocess so the operator sees which file failed and why.

    Format mirrors `FileUniverseProvider`: blank lines and `#` comments are skipped,
    commas count as separators, symbols are upper-cased.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Universe file not found: {path!r}. Pass --universe <path-to-symbols-file>; "
            f"the in-repo default is {_DEFAULT_UNIVERSE!r}."
        )

    symbols: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            symbols.extend(tok.strip().upper() for tok in line.replace(",", " ").split() if tok.strip())

    if len(symbols) < _MIN_UNIVERSE_SYMBOLS:
        raise ValueError(
            f"Universe file {path!r} yielded {len(symbols)} symbol(s); at least "
            f"{_MIN_UNIVERSE_SYMBOLS} are required. Is this a universe file "
            f"(one symbol per line, '#' for comments)?"
        )

    malformed = [sym for sym in symbols if not _UNIVERSE_SYMBOL_RE.match(sym)]
    if malformed:
        preview = malformed[:5]
        suffix = "..." if len(malformed) > len(preview) else ""
        raise ValueError(
            f"Universe file {path!r} holds {len(malformed)} symbol(s) that are not in the "
            f"'<6 digits>.<SH|SZ|BJ>' form, e.g. {preview}{suffix}. This orchestrator runs "
            f"the China A-share blend batch, which resolves .SH/.SZ tickers."
        )
    return symbols


def parse_args():
    parser = argparse.ArgumentParser(description="Cross-Ablation Matrix Orchestrator for Blend Strategies")
    parser.add_argument(
        "--universe",
        type=str,
        default=_DEFAULT_UNIVERSE,
        help="Path to universe symbols file",
    )
    parser.add_argument(
        "--start-run-id",
        type=int,
        default=17,
        help="Starting integer run ID for folder naming (default: 17 for blend_17_baseline)",
    )
    parser.add_argument(
        "--matrix-runs",
        type=str,
        default="0,1,2,3,4,5",
        help="Comma-separated list of matrix run IDs to execute (default: '0,1,2,3,4,5')",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=2,
        help="Maximum rounds to execute (default: 2, runs Round 1 and Round 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prints backtest commands without executing them",
    )
    parser.add_argument(
        "--weighting-mode",
        type=str,
        default="regime_ic_ir_hybrid",
        help="Blend sub-strategy weighting mode (default: regime_ic_ir_hybrid)",
    )
    parser.add_argument(
        "--enable-orthogonalization",
        type=str,
        default="true",
        help="Enable Löwdin symmetric orthogonalization (default: true)",
    )
    return parser.parse_args()


def collect_matrix_results(pipeline_dir: str, executed_runs: List[Dict[str, Any]], max_rounds: int) -> List[Dict[str, Any]]:
    """Gathers and parses walkforward_summary.json results across matrix runs and rounds."""
    records = []
    for cfg in executed_runs:
        folder = cfg["folder"]
        run_name = cfg["name"]
        run_desc = cfg["description"]
        for r_cfg in ROUNDS_CONFIG[:max_rounds]:
            round_num = r_cfg["round"]
            pattern = os.path.join(pipeline_dir, "results", folder, str(round_num), "*", "walkforward_summary.json")
            summary_files = glob.glob(pattern)
            for s_file in summary_files:
                try:
                    with open(s_file, "r") as f:
                        data = json.load(f)
                    strat_dir = os.path.basename(os.path.dirname(s_file))
                    strat_name = data.get("strategy") or data.get("strategy_name") or strat_dir
                    records.append({
                        "run_id": cfg["id"],
                        "folder": folder,
                        "run_name": run_name,
                        "run_description": run_desc,
                        "round": round_num,
                        "round_start": r_cfg["start"],
                        "round_end": r_cfg["end"],
                        "strategy": strat_name,
                        "strategy_dir": strat_dir,
                        "sharpe": data.get("mean_sharpe_ratio", 0.0),
                        "cagr": data.get("mean_cagr", 0.0),
                        "max_drawdown": data.get("mean_max_drawdown", 0.0),
                        "calmar": data.get("mean_calmar_ratio", 0.0),
                        "dsr": data.get("deflated_sharpe_ratio", 0.0),
                        "n_valid_folds": data.get("n_valid_folds", 0),
                        "summary_file": s_file,
                    })
                except Exception as e:
                    print(f"Warning: Failed to parse {s_file}: {e}", file=sys.stderr)
    return records


def generate_matrix_summary_markdown(records: List[Dict[str, Any]], executed_runs: List[Dict[str, Any]]) -> str:
    """Generates Markdown comparison report across all matrix cells and rounds."""
    lines = []
    lines.append("# Cross-Ablation Matrix Comparison Summary: Track C (Structural) × Track T (Intraday)")
    lines.append("")
    lines.append("## 1. Matrix Configuration Overview")
    lines.append("")
    lines.append("| Cell ID | Folder | Configuration Name | Description | Key Parameters |")
    lines.append("| :---: | :--- | :--- | :--- | :--- |")
    for r in executed_runs:
        param_str = ", ".join(f"`{k}={v}`" for k, v in list(r["params"].items())[:3])
        if len(r["params"]) > 3:
            param_str += f" (+{len(r['params']) - 3} more)"
        if not param_str:
            param_str = "Baseline defaults"
        lines.append(f"| Run {r['id']} | `{r['folder']}` | {r['name']} | {r['description']} | {param_str} |")
    lines.append("")

    if not records:
        lines.append("*(No execution results parsed yet - run without --dry-run to execute)*\n")
        return "\n".join(lines)

    lines.append("## 2. Multi-Strategy Performance Comparison Table")
    lines.append("")
    lines.append("| Cell ID | Configuration | Strategy | Round 1 Sharpe | Round 1 CAGR | Round 1 MDD | Round 2 Sharpe | Round 2 CAGR | Round 2 MDD | DSR |")
    lines.append("| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    # Group records by (folder, strategy_dir)
    grouped = {}
    for rec in records:
        key = (rec["folder"], rec["strategy_dir"])
        grouped.setdefault(key, {})[rec["round"]] = rec

    for r in executed_runs:
        folder = r["folder"]
        strat_dirs = sorted({rec["strategy_dir"] for rec in records if rec["folder"] == folder})
        for sdir in strat_dirs:
            r1 = grouped.get((folder, sdir), {}).get(1, {})
            r2 = grouped.get((folder, sdir), {}).get(2, {})
            sname = r1.get("strategy") or r2.get("strategy") or sdir

            s_r1_sharpe = f"{r1.get('sharpe', 0.0):.3f}" if r1 else "N/A"
            s_r1_cagr = f"{r1.get('cagr', 0.0) * 100:.2f}%" if r1 else "N/A"
            s_r1_mdd = f"{r1.get('max_drawdown', 0.0) * 100:.2f}%" if r1 else "N/A"

            s_r2_sharpe = f"{r2.get('sharpe', 0.0):.3f}" if r2 else "N/A"
            s_r2_cagr = f"{r2.get('cagr', 0.0) * 100:.2f}%" if r2 else "N/A"
            s_r2_mdd = f"{r2.get('max_drawdown', 0.0) * 100:.2f}%" if r2 else "N/A"

            dsr_val = r1.get("dsr") if r1 else r2.get("dsr")
            s_dsr = f"{dsr_val:.4f}" if dsr_val is not None else "N/A"

            lines.append(f"| Run {r['id']} | `{r['folder']}` | {sname} | {s_r1_sharpe} | {s_r1_cagr} | {s_r1_mdd} | {s_r2_sharpe} | {s_r2_cagr} | {s_r2_mdd} | {s_dsr} |")

    lines.append("")
    return "\n".join(lines)


def main():
    args = parse_args()

    # Determine script and directory paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pipeline_dir = os.path.abspath(os.path.join(script_dir, ".."))
    repo_root = os.path.abspath(os.path.join(pipeline_dir, ".."))
    backtest_script = os.path.join(pipeline_dir, "scripts", "backtest_blend_strategies.sh")

    if not os.path.exists(backtest_script):
        print(f"Error: Backtest script not found at {backtest_script}", file=sys.stderr)
        sys.exit(1)

    # Fail here, not inside a worker pool, if the universe is missing or malformed.
    try:
        universe_symbols = load_universe_symbols(args.universe)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Parse requested matrix runs
    try:
        requested_ids = [int(x.strip()) for x in args.matrix_runs.split(",") if x.strip()]
    except ValueError:
        print(f"Error: Invalid --matrix-runs argument: {args.matrix_runs}", file=sys.stderr)
        sys.exit(1)

    configs_by_id = {c["id"]: c for c in MATRIX_CONFIGS}
    executed_runs = []
    for rid in requested_ids:
        if rid not in configs_by_id:
            print(f"Warning: Unknown matrix run id {rid}, skipping.", file=sys.stderr)
            continue
        cfg = dict(configs_by_id[rid])
        actual_num = args.start_run_id + rid
        cfg["folder"] = f"blend_{actual_num}_{cfg['name']}"
        executed_runs.append(cfg)

    print("=" * 80)
    print("Blend Strategies Cross-Ablation Matrix Orchestrator")
    print(f"Universe:                 {args.universe} ({len(universe_symbols)} symbols)")
    print(f"Weighting Mode:           {args.weighting_mode}")
    print(f"Enable Orthogonalization: {args.enable_orthogonalization}")
    print(f"Start Run ID:             {args.start_run_id}")
    print(f"Max Rounds:               {args.max_rounds}")
    print(f"Dry Run:                  {args.dry_run}")
    print(f"Total Cells to Run:       {len(executed_runs)}")
    for r in executed_runs:
        print(f"  - Run {r['id']}: {r['folder']} ({r['description']})")
    print("=" * 80)

    # Execution Loop
    for cell in executed_runs:
        folder = cell["folder"]
        extra_json = json.dumps(cell["params"])

        print(f"\n>>> Executing Matrix Cell: Run {cell['id']} -> {folder}")
        print(f"    Description: {cell['description']}")
        print(f"    Params:      {extra_json}")

        for r_cfg in ROUNDS_CONFIG[:args.max_rounds]:
            round_num = r_cfg["round"]
            results_dir = f"results/{folder}/{round_num}"

            cmd = [
                "bash",
                "scripts/backtest_blend_strategies.sh",
                "--universe", args.universe,
                "--results", results_dir,
                "--start", r_cfg["start"],
                "--end", r_cfg["end"],
                "--weighting-mode", args.weighting_mode,
                "--enable-orthogonalization", args.enable_orthogonalization,
                "--extra-params", extra_json,
            ]

            cmd_str = " ".join(f"'{x}'" if " " in x or "{" in x else x for x in cmd)
            print(f"\n  [Round {round_num}] {r_cfg['name']}")
            print(f"  Command: {cmd_str}")

            if args.dry_run:
                print(f"  [DRY-RUN] Command skipped.")
            else:
                proc = subprocess.run(cmd, cwd=pipeline_dir)
                if proc.returncode != 0:
                    print(f"Error: Backtest failed for {folder} Round {round_num} (code {proc.returncode})", file=sys.stderr)

    # Generate matrix summary
    records = collect_matrix_results(pipeline_dir, executed_runs, args.max_rounds)
    md_content = generate_matrix_summary_markdown(records, executed_runs)

    summary_dir = os.path.join(pipeline_dir, "results")
    os.makedirs(summary_dir, exist_ok=True)
    summary_md_path = os.path.join(summary_dir, "matrix_comparison_summary.md")
    with open(summary_md_path, "w") as f:
        f.write(md_content)
    print(f"\nMatrix summary report written to: {summary_md_path}")

    # Output CSV if records exist
    if records:
        import csv
        summary_csv_path = os.path.join(summary_dir, "matrix_comparison_summary.csv")
        fieldnames = [
            "run_id", "folder", "run_name", "run_description", "round",
            "round_start", "round_end", "strategy", "strategy_dir",
            "sharpe", "cagr", "max_drawdown", "calmar", "dsr", "n_valid_folds"
        ]
        with open(summary_csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                writer.writerow(rec)
        print(f"Matrix summary CSV written to:    {summary_csv_path}")

    print("\n" + md_content)


if __name__ == "__main__":
    main()
