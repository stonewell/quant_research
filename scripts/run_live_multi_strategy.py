#!/usr/bin/env python3
"""Multi-Strategy Operational Live Order Netting Runner.

Executes a portfolio of quantitative strategies in a single master brokerage account,
performing automated internal crossing (内部对冲), order netting (撮合净额化),
friction tax savings audit (税费磨损节约量化), and sub-strategy virtual ledger tracking.

Execution Hierarchy:
  Phase 1: NET SELLS FIRST (releases master purchasing power)
  Phase 2: NET BUYS SECOND (lot-rounded entries in 100-share lots)
  Phase 3: NET POSITIONS HELD (inertia & tenure protected holds)
  Phase 4: GLOBAL CASH SWEEP (idle cash into BIL / 511880 / GC001)

Usage:
  # 1. Tri-Core Blend Netting Execution with synthetic data:
  python scripts/run_live_multi_strategy.py \
    --strategies chan_crisis_shield_blend:0.40 chan_risk_managed_blend:0.35 chan_four_state_shield_blend:0.25 \
    --universe-file docs/universe/china/pruned_core_satellite_blend_16.txt \
    --portfolio-value 100000 \
    --data-provider synthetic

  # 2. Netting with actual current holdings:
  python scripts/run_live_multi_strategy.py \
    --strategies chan_crisis_shield_blend:0.40 chan_risk_managed_blend:0.35 chan_four_state_shield_blend:0.25 \
    --current-holdings '{"300394.SZ": 500, "601872.SH": 1000, "BIL": 80000}' \
    --portfolio-value 100000 \
    --data-provider synthetic

5m intraday features (D11, docs/development_plan.md §4.50):
  The 5m layer is CONFIG-DRIVEN -- there is no CLI switch on this runner. Set
  `enable_intraday_5m: true` in a strategy's own `params` and this round loads the 5m
  panel ONCE for the whole symbol union, enriches the daily universe once, and reports
  the outcome honestly in `summary.json` (`observability.intraday_5m`, source
  `multi_strategy_netted`). No strategy asking for it => no 5m code path at all.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

def _find_repo_root() -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isfile(os.path.join(d, "AGENTS.md")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("Cannot locate repo root (AGENTS.md not found)")


_REPO_ROOT = _find_repo_root()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_REPO_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

from common.cli_utils import (
    add_data_provider_cli_args,
    add_market_clock_cli_args,
    build_data_kwargs,
    load_live_universe,
    persist_run_metadata,
    resolve_live_clock_context,
    shared_data_dir,
)
from common.data import get_data_provider
from common.freshness import assert_fresh_universe, latest_close
from common.order_netting import (
    FrictionSavings,
    MarketType,
    NettingResult,
    OrderNettingEngine,
    SubStrategyIntent,
    get_market_currency,
    get_market_label,
)
from common.strategy_spec import get_template, load_strategy_file
from common.universe import (
    get_stock_name,
    is_placeholder_stock_name,
    resolve_universe_from_args,
)
from pipeline.live_signal.lsig.signal import as_of_universe, latest_rebalance_rows
from scripts.live_paths import MULTI_STRATEGY_KEY, default_live_output_dir
from scripts.live_preflight import (
    LiveDumpPreflightError,
    verify_live_strategy_dump_preflight,
)
from scripts.run_live_strategy import (
    announce_run_mode,
    load_holdings,
    load_symbol_names,
    pad_east_asian,
    resolve_holding_bars,
    resolve_holdings_weights_and_shares,
    resolve_lot_size,
    resolve_run_mode,
    resolve_strategy_definition,
)

DEFAULT_UNIVERSE_FILE = os.path.join(
    _REPO_ROOT, "docs", "universe", "china", "pruned_core_satellite_blend_16.txt"
)
# Default netted-ticket output dir: `<repo_root>/scripts/results/live/multi_strategy_netted`.
# Owned by `scripts/live_paths.py` -- NOT `docs/ruleset/multi_strategy_netted` any more.
# That tree holds hand-authored operating manuals; a netting run must not write its
# generated ticket / sub-ledgers into it, and nothing may CREATE it by default. The
# web app reads this directory back when a dated archive run does not exist yet
# (`web/livetrading/storage.py::StorageManager.get_multi_netting_run`), so the path and
# the `multi_strategy_netted` key are derived here, never re-spelled at either end.
DEFAULT_OUTPUT_DIR = default_live_output_dir(MULTI_STRATEGY_KEY)


def resolve_prior_sub_ledgers(
    val_date: str,
    output_dir: Optional[str],
    sub_ledgers_file_arg: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolves prior sub-ledgers strictly before val_date to ensure re-run idempotency."""
    out_file = os.path.join(output_dir, "sub_ledgers.json") if output_dir else ""
    if sub_ledgers_file_arg and os.path.exists(sub_ledgers_file_arg):
        # Only use if it is not today's own output file
        if not out_file or os.path.abspath(sub_ledgers_file_arg) != os.path.abspath(out_file):
            try:
                with open(sub_ledgers_file_arg, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    # Search earlier date runs strictly before val_date
    cand_parents = []
    if output_dir:
        cand_parents.append(os.path.dirname(os.path.abspath(output_dir)))
        cand_parents.append(os.path.dirname(os.path.dirname(os.path.abspath(output_dir))))

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for parent in cand_parents:
        if not os.path.isdir(parent):
            continue
        sub_dates = sorted([d for d in os.listdir(parent) if date_re.match(d) and d < val_date], reverse=True)
        for d in sub_dates:
            d_path = os.path.join(parent, d)
            cands = [
                os.path.join(d_path, "sub_ledgers.json"),
            ]
            if output_dir:
                cands.append(os.path.join(d_path, os.path.basename(output_dir), "sub_ledgers.json"))
            if os.path.isdir(d_path):
                for sub in os.listdir(d_path):
                    if sub.startswith("multi_netted_"):
                        cands.append(os.path.join(d_path, sub, "sub_ledgers.json"))
            for cand in cands:
                if os.path.exists(cand):
                    try:
                        with open(cand, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if data and isinstance(data, dict):
                                return data
                    except Exception:
                        pass
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Multi-Strategy Operational Live Order Netting Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=[
            "chan_crisis_shield_blend:0.40",
            "chan_risk_managed_blend:0.35",
            "chan_four_state_shield_blend:0.25",
        ],
        help="List of strategy_key:capital_weight pairs (e.g. chan_crisis_shield_blend:0.40)",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="Path to JSON configuration file specifying multi-strategy setup",
    )
    parser.add_argument(
        "--universe-file",
        default=DEFAULT_UNIVERSE_FILE,
        help="Default universe symbols file",
    )
    parser.add_argument(
        "--as-of-date",
        default=None,
        help=(
            "Valuation as-of-date (YYYY-MM-DD). Default: the market clock's as_of_trading_date "
            "(close + publication grace, market-local), never the host's local date. An explicit "
            "value must be a --market trading day and may not be later than the latest published session. "
            "WARN: a PAST trading day is a deliberate history replay -- the run-window gate is skipped by "
            "design (盘中手工回放仍能出工单) and the run is marked run_mode=replay in summary.json; do not "
            "archive that netted ticket as a same-day live one or use it for attribution."
        ),
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Lookback start date for historical market data loading (YYYY-MM-DD, default: as_of_date - lookback_days)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=800,
        help="Lookback calendar days for indicator calculation (default: 800)",
    )
    parser.add_argument(
        "--portfolio-value",
        type=float,
        default=100000.0,
        help="Total portfolio NAV in RMB (default: 100,000.0)",
    )
    parser.add_argument(
        "--current-holdings",
        default=None,
        help="JSON string of master account holdings: '{\"SYMBOL\": shares, ...}'",
    )
    parser.add_argument(
        "--current-holdings-file",
        default=None,
        help="Path to JSON file containing master account holdings",
    )
    parser.add_argument(
        "--account-state-file",
        default=None,
        help="Path to account state file for NAV or holdings recovery",
    )
    parser.add_argument(
        "--sub-ledgers-file",
        default=None,
        help="Path to persistent virtual sub-ledgers JSON file",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Output directory to save netted trading ticket and reports (default: "
            "scripts/results/live/multi_strategy_netted, an ordinary workspace artifact "
            "directory -- never docs/ruleset/)"
        ),
    )
    parser.add_argument(
        "--min-holding-bars",
        type=int,
        default=2,
        help="Minimum tenure holding bars before liquidation is permitted (default: 2)",
    )
    parser.add_argument(
        "--inertia-threshold",
        type=float,
        default=0.05,
        help="Global weight inertia threshold below which rebalances are filtered (default: 0.05)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Market data cache directory",
    )
    parser.add_argument(
        "--allow-weighting-mode-override",
        action="store_true",
        help=(
            "EXPLORATION ONLY -- bypasses two pre-flight refusals: (1) a strategy dump whose "
            "blend_weighting_mode differs from strategies_config.json, and (2) a strategy key "
            "that is not present in strategies_config.json at all (this path prints a loud "
            "warning: the pinned mode was never verified). It does NOT bypass a malformed dump, "
            "an unidentifiable dump, or a registered strategy whose blend_weighting_mode is "
            "missing entirely. Do NOT archive the resulting run as a live ticket."
        ),
    )

    add_data_provider_cli_args(parser)
    # (L2.3) as-of / market / run-window gate / strict freshness / probe retry limit.
    add_market_clock_cli_args(parser, stage="stage2")
    return parser.parse_args()


def parse_strategy_specs(strategies_arg: List[str], config_file: Optional[str]) -> List[Dict[str, Any]]:
    """Parse strategy keys, weights, and optional custom universes."""
    specs = []
    if config_file and os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                specs = data
            elif isinstance(data, dict) and "strategies" in data:
                specs = data["strategies"]

    if not specs:
        for item in strategies_arg:
            parts = item.split(":")
            s_key = parts[0].strip()
            w = float(parts[1]) if len(parts) > 1 else 1.0
            specs.append({
                "strategy_key": s_key,
                "capital_weight": w,
            })

    # Normalize weights
    total_w = sum(s.get("capital_weight", 1.0) for s in specs)
    for s in specs:
        s["capital_weight"] = s.get("capital_weight", 1.0) / total_w
    return specs


# --------------------------------------------------------------------------- #
# (D11 / docs/development_plan.md §4.50) REAL 5m intraday support for the netted
# runner -- config-driven, round-level union, honest observability
# --------------------------------------------------------------------------- #
# WHY THIS BLOCK EXISTS
# ---------------------
# Until `D11` this runner had NO 5m code path at all: `load_live_universe(..., interval="1d")`
# was the only load, so `blend_base.py`'s `enable_intraday_5m` (and the Garman-Klass /
# UpVol / Chan-5m gates behind it) could only ever degrade to the daily signal.  The web
# layer's matrix argv stopped passing `--enable-intraday-5m` in `P4b` precisely because
# this script never defined it (`891df17`); re-adding a manual switch is NOT the fix.
#
# §4.50.0 (user ruling, 2026-11-06): the switch FOLLOWS CONFIGURATION -- there is no
# default-off CLI switch on this path.  The single criterion is the strategy's own
# `enable_intraday_5m` param, resolved through THAT template's own resolver
# (§4.50.1), so the loading decision and the consuming decision (`blend_base.py:714`)
# cannot disagree.  Re-implementing the precedence chain here (`params.get(...)` or a
# `*_enable_intraday_5m` scan) would miss `cfg.blend_enable_intraday_5m` and reproduce
# the 2026-10-22 "printed success, fields all empty" defect on this path.
#
# §4.50.2: the load/enrich is a ROUND-LEVEL UNION (one call over `all_symbols_to_load`,
# including out-of-universe holdings) and must stay OUTSIDE the per-strategy loop -- once
# per strategy would re-fetch the same panel N times.

#: Sentinel the web layer's `reconcile_intraday_5m_observability` greps for
#: (`web/livetrading/execution_service.py::INTRADAY_5M_WARN_MARKER`). Duplicated as a
#: literal on purpose: `scripts/` must not import the web app, and the two strings are
#: pinned equal by `web/livetrading/tests/test_multi_strategy_intraday_5m.py`.
INTRADAY_5M_WARN_MARKER = "5m 日内特征不可用"

#: The exact column `blend_base.py` gates on (`"UpVol_Ratio_5m" in df_s.columns`).
#: "enrichment produced a usable column" means THIS column, not merely "rows arrived".
INTRADAY_5M_INDICATOR_COLUMN = "UpVol_Ratio_5m"

#: `source` tag written into `observability.intraday_5m`; also proves to a reader that the
#: block came from the round's own child process, not from stdout/stage-1 reconciliation.
INTRADAY_5M_SOURCE = "multi_strategy_netted"

#: Same default as Stage 2 (`run_live_strategy.py --cutoff-time`) and
#: `pipeline/research/blend/blend_base.py:719`.
DEFAULT_INTRADAY_CUTOFF_TIME = "14:15"

_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


def resolve_intraday_5m_request(template: Any, params: Dict[str, Any]) -> Tuple[bool, bool]:
    """`(requested, resolved_by_template)` for ONE strategy (§4.50.1).

    Uses the template's OWN `resolve_param` when it exposes one (`BaseBlendAllocationStrategy`
    and every subclass), so the loading decision walks the very same precedence chain the
    consumer walks: `p.<prefix>_<name>` -> `p.blend_<name>` -> `p.<name>` -> `cfg.<prefix>_<name>`
    -> `cfg.blend_<name>` -> `cfg.<name>` -> default.

    Static allocation templates carry no such switch (they have no `resolve_param`), so they
    can never request 5m; `resolved_by_template=False` records that this was a "no switch to
    consult" answer rather than a configured `False`.
    """
    resolver = getattr(template, "resolve_param", None)
    if not callable(resolver):
        return False, False
    return bool(resolver(params, "enable_intraday_5m", False, bool)), True


def resolve_intraday_cutoff(template: Any, params: Dict[str, Any]) -> str:
    """The requesting strategy's own `intraday_cutoff_time`, defaulted to 14:15."""
    resolver = getattr(template, "resolve_param", None)
    if not callable(resolver):
        return DEFAULT_INTRADAY_CUTOFF_TIME
    raw = resolver(params, "intraday_cutoff_time", DEFAULT_INTRADAY_CUTOFF_TIME, str)
    text = str(raw).strip() if raw is not None else ""
    if not _HHMM_RE.match(text):
        print(
            f"[WARN] 无法解析 intraday_cutoff_time={raw!r}，回退默认 "
            f"{DEFAULT_INTRADAY_CUTOFF_TIME}（HH:MM）"
        )
        return DEFAULT_INTRADAY_CUTOFF_TIME
    return text


def combine_intraday_cutoffs(values: List[str]) -> str:
    """The EARLIEST requested cutoff -- one `enrich` call serves the whole round.

    DISCLOSED CHOICE: §4.50 freezes only the ENABLE switch, but `enrich_universe_with_intraday_features`
    can only run at ONE cutoff per round, so when two requesting strategies configure
    different cutoffs one of them is honoured exactly. The earliest value is the only
    conservative direction: no requesting strategy is handed features computed further into
    the session than its own configured cutoff (the alternative would silently widen one
    strategy's information window). Today no tracked config sets this key, so the choice is
    inert until someone does.
    """
    valid = [v.strip() for v in values if isinstance(v, str) and _HHMM_RE.match(v.strip())]
    if not valid:
        return DEFAULT_INTRADAY_CUTOFF_TIME
    return min(valid, key=lambda s: (int(s.split(":")[0]), int(s.split(":")[1])))


def load_intraday_5m_panel(
    symbols: List[str],
    *,
    start: str,
    end: str,
    data_provider: Any,
    cutoff_time: str,
    cache_dir: Optional[str] = None,
) -> Dict[str, pd.DataFrame]:
    """Thin import seam around `common.intraday_features.load_intraday_5m_universe`.

    Imported lazily (as Stage 2 does) so this runner keeps working in an environment where
    the optional intraday dependencies are absent, and exposed as a module-level name so a
    test can prove the loader is NOT called in the zero-request state.
    """
    from common.intraday_features import load_intraday_5m_universe

    return load_intraday_5m_universe(
        symbols,
        start=start,
        end=end,
        data_provider=data_provider,
        cutoff_time=cutoff_time,
        cache_dir=cache_dir,
    )


def enrich_intraday_5m_panel(
    universe: Dict[str, pd.DataFrame],
    universe_5m: Dict[str, pd.DataFrame],
    *,
    cutoff_time: str,
) -> Dict[str, pd.DataFrame]:
    """Thin import seam around `enrich_universe_with_intraday_features` (Stage 2 parity)."""
    from common.intraday_features import (
        IntradayFeatureConfig,
        enrich_universe_with_intraday_features,
    )

    return enrich_universe_with_intraday_features(
        universe,
        universe_5m,
        config=IntradayFeatureConfig(cutoff_time=cutoff_time),
        synthesize_intraday_bar=True,
    )


def summarize_intraday_5m_frames(
    universe_5m: Dict[str, pd.DataFrame], as_of_date: str
) -> Dict[str, Any]:
    """Count REAL 5m ROWS on the as-of session, never symbols and never daily bars.

    Same counting rule as the web layer's `summarize_intraday_5m_availability`
    (`web/livetrading/execution_service.py`), which is what the persisted block means: the
    2026-10-22 incident was a SUCCESS line whose count was the DAILY symbol count while the
    5m universe was empty. A frame carrying `attrs["intraday_5m_available"] = False` stays
    "unavailable" even if it accidentally holds rows.
    """
    rows = 0
    with_rows: List[str] = []
    unavailable: List[str] = []
    last_ts = None
    session = pd.Timestamp(as_of_date).normalize() if as_of_date else None
    for sym, df in (universe_5m or {}).items():
        name = str(sym)
        if (
            df is None
            or bool(getattr(df, "empty", True))
            or getattr(df, "attrs", {}).get("intraday_5m_available") is False
        ):
            unavailable.append(name)
            continue
        try:
            index = pd.DatetimeIndex(df.index)
        except (TypeError, ValueError):
            unavailable.append(name)
            continue
        if session is not None:
            try:
                n_on_session = int((index.normalize() == session).sum())
            except (TypeError, ValueError):
                n_on_session = 0
        else:
            n_on_session = int(len(index))
        rows += n_on_session
        (with_rows if n_on_session > 0 else unavailable).append(name)
        if len(index):
            ts = index.max()
            if last_ts is None or ts > last_ts:
                last_ts = ts
    return {
        "rows_5m": int(rows),
        "symbols_with_rows": sorted(with_rows),
        "symbols_unavailable": sorted(unavailable),
        "last_5m_timestamp": last_ts.isoformat() if last_ts is not None else None,
    }


def build_intraday_5m_block(
    *,
    requested: bool,
    frames: Dict[str, Any],
    as_of_date: Optional[str],
    usable_symbols: List[str],
    reason: Optional[str] = None,
    round_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The block persisted as `observability.intraday_5m` -- SAME SHAPE as the web layer's.

    `enabled` is true only when the round asked for 5m, REAL rows landed on the as-of
    session, AND enrichment actually produced `UpVol_Ratio_5m` for at least one symbol
    (§4.50.4 state 3 + "no silent degradation": rows without a usable column would make
    `blend_base.py:799` silently fall back to the daily signal while a naive `rows > 0`
    check claimed success).

    DISCLOSED STRICTNESS: `summarize_intraday_5m_availability` enables on `rows > 0` alone.
    This child-side block additionally requires the consumable column, i.e. it can only be
    MORE conservative than the web layer -- never `enabled=True` on data a strategy cannot use.

    ROUND IDENTITY (`round_id` + `generated_at`, B1): the netted run directory is FIXED per
    matrix+date (`<archive>/<date>/multi_netted_<matrix>`) and never cleaned, so a same-day
    rerun reuses it and every block it ever wrote carries the SAME `as_of_date`. That alone
    cannot distinguish "this round's report" from "last round's report", so the block also
    carries:

    * ``round_id`` -- the UTC ISO-8601 instant this round STARTED (chosen over a uuid4 so an
      operator can order two same-day rounds, from the block alone, without another file);
    * ``generated_at`` -- tz-aware UTC ISO-8601 of block CREATION.

    The web layer rejects a block whose ``generated_at`` predates its own round start (and
    a block with no/unparseable/naive ``generated_at`` -- fail closed), which is what stops
    a previous same-day round's block from being read as the current round's self-report.
    """
    rows = int(frames.get("rows_5m") or 0)
    enabled = bool(requested and rows > 0 and usable_symbols)
    block: Dict[str, Any] = {
        "requested": bool(requested),
        "enabled": enabled,
        "rows_5m": rows,
        "as_of_date": as_of_date,
        # (B1) Provenance stamps; see the docstring. `generated_at` is what the web layer
        # compares against its own round start, so it must stay tz-aware UTC.
        "round_id": round_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols_with_rows": list(frames.get("symbols_with_rows") or []),
        "symbols_unavailable": list(frames.get("symbols_unavailable") or []),
        "reason": reason,
        "source": INTRADAY_5M_SOURCE,
    }
    if block["requested"] and not enabled:
        message = (
            f"[WARNING] {INTRADAY_5M_WARN_MARKER} (enabled=False)：as_of={as_of_date} "
            f"当日真实 5m 行数为 {rows}（统计的是 5m 行数，不是标的数，也不是日线 bar 数）。"
        )
        if reason:
            message += f"原因：{reason}。"
        message += " 下游必须把日内 VWAP / Chan-5m 门槛视为 UNKNOWN/SKIPPED，绝不静默 PASS。"
        block["warning"] = message
    return block


def persist_intraday_5m_observability(output_dir: str, block: Dict[str, Any]) -> str:
    """MERGE `observability.intraday_5m` into `<output_dir>/summary.json`.

    A MERGE for the same reason `persist_run_metadata` is one: `web/livetrading` later
    `update()`s its own keys into this file, and replacing it here would delete the
    clock/freshness audit trail. Also why the zero-request state writes NOTHING at all --
    a round that never asked for 5m must leave the file byte-identical to the pre-D11 path.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "summary.json")
    payload: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                payload = loaded
        except Exception as exc:  # pragma: no cover - unreadable archive is reported, not fatal
            print(f"[WARN] 无法解析既有 summary.json ({path}): {exc}；将覆盖写入 5m 观测块")
    observability = payload.get("observability")
    if not isinstance(observability, dict):
        observability = {}
        payload["observability"] = observability
    observability["intraday_5m"] = block
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    print(f"[INTRADAY 5M] 日内特征观测块已写入: {path} (enabled={block.get('enabled')})")
    return path


def clear_intraday_5m_observability(output_dir: str) -> bool:
    """Drop a PREVIOUS round's `observability.intraday_5m` block at round start (B1).

    `persist_run_metadata` MERGES into `<output_dir>/summary.json` (the same merge contract
    `persist_intraday_5m_observability` relies on), and the netted run directory is FIXED
    per matrix+date and never cleaned -- so a same-day rerun inherits the previous round's
    5m block verbatim. Nothing later in this round can tell that block apart from its own
    report (both carry the same `as_of_date`), which is how a zero-request rerun used to
    publish `requested=True / enabled=True` for a round that never touched 5m.

    The block must only ever describe the round that just ran, so it is cleared BEFORE the
    5m request/gating block. If this round dies before writing its own block, the stale one
    is already gone -- absence is then the honest answer (the web layer falls through to
    `source="none"`).

    A round with no pre-existing block is left BYTE-IDENTICAL (fresh-run path unchanged),
    and if `observability` held nothing but the stale block it is removed entirely, so a
    zero-request round still persists no `observability` key at all (§4.50.4 clause 1).

    Returns ``True`` only when a stale block was actually removed. An UNREADABLE existing
    summary is reported and left untouched -- overwriting it here would delete the clock /
    freshness audit trail for a corruption we did not cause.
    """
    path = os.path.join(output_dir, "summary.json")
    if not os.path.exists(path):
        return False
    payload: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            payload = loaded
    except Exception as exc:
        print(f"[WARN] 无法解析既有 summary.json ({path}): {exc}；跳过 5m 观测块清理")
        return False
    observability = payload.get("observability")
    if not isinstance(observability, dict) or "intraday_5m" not in observability:
        return False
    observability.pop("intraday_5m", None)
    if not observability:
        payload.pop("observability", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    print(f"[INTRADAY 5M] 已清除上一轮残留的观测块: {path}")
    return True


def apply_intraday_5m_features(
    universe: Dict[str, pd.DataFrame],
    *,
    symbols: List[str],
    start_date: str,
    as_of_date: str,
    data_provider: Any,
    cutoff_time: str,
    cache_dir: Optional[str] = None,
    provider_label: str = "",
    round_id: Optional[str] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
    """ONE 5m load + ONE enrichment for the whole round; returns `(enriched, block)`.

    Ported from `scripts/run_live_strategy.py:2102-2168` (loading block AND the honest
    warnings -- §4.50.7 requires both, or the 2026-10-22 "printed success, fields all
    empty" success-message defect is copied onto the matrix path).
    """
    universe_5m = load_intraday_5m_panel(
        symbols,
        start=start_date,
        end=as_of_date,
        data_provider=data_provider,
        cutoff_time=cutoff_time,
        cache_dir=cache_dir,
    )
    enriched = enrich_intraday_5m_panel(universe, universe_5m, cutoff_time=cutoff_time)

    frames = summarize_intraday_5m_frames(universe_5m, as_of_date)
    n_5m_ok = sum(1 for _df in universe_5m.values() if _df is not None and not _df.empty)
    missing_5m = sorted(
        _s for _s in symbols
        if _s not in universe_5m or universe_5m[_s] is None or universe_5m[_s].empty
    )
    reason: Optional[str] = None
    if n_5m_ok == 0:
        reason = (
            f"{len(symbols)} 个标的的 5m 数据帧全部为空 "
            f"(provider={provider_label}, 截断时点={cutoff_time})"
        )
        # Same wording as Stage 2: report what ACTUALLY arrived, never a daily-symbol count.
        # `(enabled=False)` is part of the line (not only of the persisted block) so a human
        # reading the console cannot mistake this for the 2026-10-22 "success" message.
        print(
            f"[WARN] {INTRADAY_5M_WARN_MARKER} (enabled=False)：{len(symbols)} 个标的的 5m 数据帧全部为空 "
            f"(provider={provider_label}, 截断时点={cutoff_time})。"
            "常见原因：provider does not support 5m (该数据源仅支持日线 interval='1d')。"
            "后续 intraday_vwap / vwap_premium_pct / chan_5m_state 等日内字段将为空值，"
            "本轮仅按日线信号生成工单；请勿把本行理解为日内特征已生效。"
        )
    else:
        print(
            f"[INFO] 5m 日内特征已聚合: 有效标的数 {n_5m_ok}/{len(symbols)} "
            f"(截断时点: {cutoff_time}, 最新 5m K 线: {frames['last_5m_timestamp']})"
        )
        if missing_5m:
            print(
                f"[WARN] 以下 {len(missing_5m)} 个标的缺少 5m 数据，日内字段为空: "
                f"{', '.join(missing_5m[:20])}{' ...' if len(missing_5m) > 20 else ''}"
            )

    usable = sorted(
        _s for _s in symbols
        if _s in enriched
        and not bool(getattr(enriched[_s], "empty", True))
        and INTRADAY_5M_INDICATOR_COLUMN in getattr(enriched[_s], "columns", [])
    )
    if reason is None and not usable:
        reason = f"5m 行已到达但 enrich 后无 {INTRADAY_5M_INDICATOR_COLUMN} 列，日内特征未生效"
        print(f"[WARN] {INTRADAY_5M_WARN_MARKER} (enabled=False)：{reason}")

    block = build_intraday_5m_block(
        requested=True,
        frames=frames,
        as_of_date=as_of_date,
        usable_symbols=usable,
        reason=reason,
        round_id=round_id,
    )
    return enriched, block


def main():
    args = parse_args()
    # (B1) ROUND IDENTITY. The netted run directory is fixed per matrix+date and reused by
    # every same-day rerun, so `as_of_date` alone can NEVER distinguish this round's 5m
    # self-report from the previous round's. Captured once, before anything is loaded, and
    # stamped into the block as `round_id` + the freshness anchor `generated_at`.
    round_started_at = datetime.now(timezone.utc)
    round_id = round_started_at.isoformat()
    # (L2.3) Netted live deployment: as-of + run-window gate resolved from the market
    # clock exactly as in Stage 2, before any data load.
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe from {args.universe_file}")
    clock_ctx = resolve_live_clock_context(args, universe_symbols, stage="stage2")
    val_date = clock_ctx.as_of_date
    # (L2.30) This entrypoint resolves the clock EXACTLY as Stage 2 does
    # (`add_market_clock_cli_args` + `resolve_live_clock_context`) and declares its own
    # `--as-of-date`, so a pinned past session bypasses the run-window gate here too and a
    # full netted ticket still comes out. Without this the archived summary would look like a
    # same-day real run -- the precise failure mode L2.30 exists to prevent. Both helpers are
    # imported from `scripts.run_live_strategy` on purpose: there must be exactly ONE replay
    # predicate in the codebase, not a second copy that can drift.
    run_mode = resolve_run_mode(clock_ctx)
    announce_run_mode(clock_ctx, run_mode)
    if getattr(args, "start_date", None):
        start_date = args.start_date
    else:
        start_date = (pd.Timestamp(val_date) - timedelta(days=args.lookback_days)).date().isoformat()
    os.makedirs(args.output_dir, exist_ok=True)

    strategy_specs = parse_strategy_specs(args.strategies, args.config_file)
    print("=" * 115)
    print("MULTI-STRATEGY ORDER NETTING & VIRTUAL LEDGER RUNNER (多策略撮合净额化与内部对冲引擎)")
    print(f"调仓基准日期 (As-of-Date)   : {val_date}")
    print(f"数据回溯周期 (Lookback)     : {start_date} -> {val_date}")
    print(f"主账户总资产 (Portfolio NAV): ¥{args.portfolio_value:,.2f} RMB")
    print(f"股票池路径   (Universe)     : {args.universe_file}")
    print(f"数据源提供者 (Provider)     : {args.data_provider}")
    print(f"协同策略数量 (Strategies)   : {len(strategy_specs)} 个")
    for s in strategy_specs:
        print(f"  • {s['strategy_key']:<35} : 资金分配权重 {s['capital_weight']:>6.1%}")
    print("=" * 115)

    # 1. Load Universe symbols
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe from {args.universe_file}")
    symbol_names = load_symbol_names(args.universe_file)

    # 2. Load Raw Master Holdings
    raw_user_holdings = load_holdings(args)
    held_symbols = [
        s.strip() for s in (raw_user_holdings.keys() if raw_user_holdings else [])
        if s.strip() not in ("BIL", "CASH")
    ]
    all_symbols_to_load = sorted(list(set(universe_symbols).union(set(held_symbols))))

    cache_dir = args.cache_dir or shared_data_dir()
    universe, probe_attempts = load_live_universe(
        args,
        all_symbols_to_load,
        start_date,
        val_date,
        clock_ctx=clock_ctx,
        use_cache=not args.no_cache,
        cache_dir=cache_dir,
        data_kwargs=build_data_kwargs(args),
        require_nonempty=True,
    )
    universe = as_of_universe(universe, val_date)
    freshness_summary = assert_fresh_universe(
        universe,
        val_date,
        strict=clock_ctx.freshness_strict,
        expected_symbols=all_symbols_to_load,
        exclude=("BIL", "CASH"),
        stage="multi_strategy_netted",
    )
    persist_run_metadata(
        args.output_dir,
        clock_ctx,
        freshness_summary=freshness_summary,
        probe_attempts=probe_attempts,
        extra={"date": val_date, "stage": "multi_strategy_netted", "run_mode": run_mode},
    )

    # (B1) `persist_run_metadata` MERGES into the existing `summary.json`, and this round's
    # directory (`<archive>/<date>/multi_netted_<matrix>`) is fixed per matrix+date and never
    # cleaned -- so a same-day rerun inherits the PREVIOUS round's
    # `observability.intraday_5m` block. If this round requests nothing it writes no block,
    # and if it dies before building one it writes none either: either way the inherited
    # block would be read as this round's self-report (`requested=True / enabled=True` on a
    # round that never touched 5m). The block may only ever describe the round that just
    # ran, so the previous one is cleared HERE, before the request/gating block below.
    clear_intraday_5m_observability(args.output_dir)

    # (D11 / §4.50.1) Resolve the 5m REQUEST before anything is loaded -- one pre-pass using
    # the SAME `resolve_strategy_definition` the strategy loop uses, so the loading decision
    # and the consuming decision (`blend_base.py:714`) can never drift. A `params.get(...)`
    # here would miss `cfg.blend_enable_intraday_5m` and silently reproduce the 2026-10-22
    # "printed success, fields all empty" defect on this path.
    resolved_strategies: List[Dict[str, Any]] = []
    intraday_5m_requesters: List[str] = []
    intraday_cutoffs: List[str] = []
    for s_spec in strategy_specs:
        s_key = s_spec["strategy_key"]
        strat_def, actual_key, resolved_desc, template = resolve_strategy_definition(s_key, None)
        params = strat_def.get("params", {})
        requests_5m, template_has_switch = resolve_intraday_5m_request(template, params)
        if requests_5m:
            intraday_5m_requesters.append(s_key)
            intraday_cutoffs.append(resolve_intraday_cutoff(template, params))
        resolved_strategies.append({
            "spec": s_spec,
            "strategy_def": strat_def,
            "actual_key": actual_key,
            "resolved_desc": resolved_desc,
            "template": template,
            "params": params,
            "requests_intraday_5m": requests_5m,
            # False = the template exposes no such switch at all (a static allocation
            # template). Recorded so "no switch to consult" is not read as "configured off".
            "intraday_5m_switch_present": template_has_switch,
        })

    # (2026-10-22 incident, Fix 2 / L2.3) Netting prices come from the SAME single
    # freshness owner as Stage 2 (`common.freshness.latest_close`), not `.iloc[-1]`.
    latest_prices = {}
    for sym in all_symbols_to_load:
        if sym in universe and not universe[sym].empty:
            latest_prices[sym] = latest_close(
                universe, sym, val_date, strict=clock_ctx.freshness_strict
            )
        else:
            latest_prices[sym] = 1.0

    # (D11 / §4.50.2) ROUND-LEVEL union: ONE `load_intraday_5m_universe` + ONE
    # `enrich_universe_with_intraday_features` over EVERY symbol this round loaded
    # (`all_symbols_to_load`, out-of-universe holdings included), and only when at least one
    # strategy asked for it. Inside the loop this would become N full re-fetches.
    #
    # Prices above are deliberately resolved from the UN-ENRICHED frames: enrichment
    # synthesizes the partial as-of daily bar from truncated 5m data, and D11 is a SIGNAL
    # capability. Re-pricing the netted ticket on a 14:15-synthesized close would be a
    # separate, money-changing decision that §4.50 never authorises.
    universe_intraday_5m: Optional[Dict[str, pd.DataFrame]] = None
    if intraday_5m_requesters:
        cutoff_t = combine_intraday_cutoffs(intraday_cutoffs)
        print(
            f"\n[INTRADAY 5M] {len(intraday_5m_requesters)}/{len(strategy_specs)} 个策略请求日内 5m: "
            f"{', '.join(intraday_5m_requesters)} (截断时点: {cutoff_t})"
        )
        try:
            provider_5m = get_data_provider(args.data_provider, **build_data_kwargs(args))
            universe_intraday_5m, intraday_5m_block = apply_intraday_5m_features(
                universe,
                symbols=all_symbols_to_load,
                start_date=start_date,
                as_of_date=val_date,
                data_provider=provider_5m,
                cutoff_time=cutoff_t,
                cache_dir=cache_dir,
                provider_label=args.data_provider,
                round_id=round_id,
            )
        except Exception as exc:
            # Honest degradation, exactly like Stage 2: warn, keep the daily signal, and
            # still persist a block that says requested-but-unavailable (§4.50.4 state 3).
            print(
                "[WARN] Failed to load 5m intraday features in the multi-strategy netted "
                f"runner: {exc} {INTRADAY_5M_WARN_MARKER} (enabled=False)"
            )
            universe_intraday_5m = None
            intraday_5m_block = build_intraday_5m_block(
                requested=True,
                frames={
                    "rows_5m": 0,
                    "symbols_with_rows": [],
                    "symbols_unavailable": list(all_symbols_to_load),
                },
                as_of_date=val_date,
                usable_symbols=[],
                reason=f"5m 加载/聚合异常: {exc}",
                round_id=round_id,
            )
        persist_intraday_5m_observability(args.output_dir, intraday_5m_block)

    # 3. Evaluate each strategy to extract its rebalance targets
    intents: List[SubStrategyIntent] = []
    print("\n[STEP 1: 评估各子策略独立调仓意向 (Evaluating Sub-Strategy Targets)]")
    for s_ctx in resolved_strategies:
        s_spec = s_ctx["spec"]
        s_key = s_spec["strategy_key"]
        w_strat = s_spec["capital_weight"]
        strat_def = s_ctx["strategy_def"]
        actual_key = s_ctx["actual_key"]
        resolved_desc = s_ctx["resolved_desc"]
        template = s_ctx["template"]
        params = s_ctx["params"]

        # (D11 / §4.50.2) The strategy-level gate is unchanged, and it is enforced HERE as
        # well: a strategy that did not request 5m gets the ORIGINAL daily universe. Handing
        # everyone the enriched copy would move a non-requesting strategy's inputs too --
        # `enrich` synthesizes/replaces the partial as-of daily bar, so "only the requesting
        # strategy sees intraday columns" has to be structural, not merely hoped for.
        strategy_universe = (
            universe_intraday_5m
            if (s_ctx["requests_intraday_5m"] and universe_intraday_5m is not None)
            else universe
        )

        # (P1.8 / P1.9) Pre-flight consistency check between dump and strategies_config.json.
        # The static-template exemption is owned by the verifier (`is_static_template_exempt`);
        # gating on the bare name here would mis-exempt a research dump keyed `dual_momentum`.
        #
        # (LV7 defect B.5) NOTE: this verifier has NO version axis. The dump-vs-live-class
        # version parity check on this path is the one inside `resolve_strategy_definition`
        # (the pre-pass above, which runs before it re-stamps the live values). Do not remove
        # or relocate that check -- on this runner it is the only version gate, so a
        # `BASE_VERSION` bump would otherwise let stale dumps through untouched.
        verify_live_strategy_dump_preflight(
            strategy_def=strat_def,
            strategy_key=actual_key,
            strategy_file=resolved_desc,
            allow_override=getattr(args, "allow_weighting_mode_override", False),
        )
        strat_name = (
            strat_def.get("strategy_name")
            or strat_def.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
            or s_key.replace("_", " ").title()
        )

        sparse_weights = template.generate_weights(strategy_universe, params)
        rebalances = latest_rebalance_rows(sparse_weights)
        if rebalances.empty:
            print(f"  [WARN] Strategy {s_key} returned no rebalances. Defaulting to 0.")
            current_targets = pd.Series(0.0, index=all_symbols_to_load)
        else:
            current_targets = rebalances.iloc[-1].fillna(0.0)

        intent = SubStrategyIntent(
            strategy_key=s_key,
            strategy_name=strat_name,
            capital_weight=w_strat,
            allocated_capital=args.portfolio_value * w_strat,
            target_weights=current_targets.to_dict(),
        )
        intents.append(intent)
        print(f"  ✓ {strat_name} (权重: {w_strat:.1%}, 分配资金: ¥{intent.allocated_capital:,.2f}) 信号提取完毕")

    # 4. Resolve Master Account Holdings and Holding Bars
    master_holdings_dict: Dict[str, Any] = {}
    if raw_user_holdings:
        master_holdings_dict = dict(raw_user_holdings)
    else:
        master_holdings_dict = {"BIL": args.portfolio_value}

    prior_sub_ledgers = resolve_prior_sub_ledgers(
        val_date=val_date,
        output_dir=args.output_dir,
        sub_ledgers_file_arg=args.sub_ledgers_file,
    )

    holding_bars = resolve_holding_bars(
        raw_user_holdings or {},
        account_state=None,
        output_dir=args.output_dir,
        current_holdings_file=args.current_holdings_file,
        as_of_date=val_date,
        default_bars=args.min_holding_bars,
    )

    # 5. Run Netting Engine
    print("\n[STEP 2: 执行多策略撮合净额化与内部对冲 (Executing Order Netting)]")
    engine = OrderNettingEngine(
        lot_size_default=100,
        stamp_duty=0.0005,
        commission=0.0003,
        slippage=0.0005,
        cash_proxy="BIL",
    )

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=master_holdings_dict,
        portfolio_value=args.portfolio_value,
        latest_prices=latest_prices,
        as_of_date=val_date,
        holding_bars=holding_bars,
        min_holding_bars=args.min_holding_bars,
        inertia_threshold=args.inertia_threshold,
        symbol_names=symbol_names,
        prior_sub_ledgers=prior_sub_ledgers,
    )

    def curr_sym(m_or_c: str) -> str:
        if m_or_c in ("HONGKONG", "HKD"):
            return "HK$"
        if m_or_c in ("US", "USD"):
            return "$"
        return "¥"

    # 6. Display Internal Crossing Audit & Friction Savings
    fs = res.friction_savings
    print("\n" + "=" * 115)
    print("内部对冲与摩擦损耗节约审计报告 (INTERNAL CROSSING & FRICTION SAVINGS AUDIT)")
    print("=" * 115)
    print(f"多策略毛买卖股数 (Gross Shares) : {fs.total_gross_shares:>10,d} 股 (毛交易流水: ¥{fs.total_gross_turnover:>10,.2f})")
    print(f"主账户净买卖股数 (Net Shares)   : {fs.total_net_shares:>10,d} 股 (净交易流水: ¥{fs.total_net_turnover:>10,.2f})")
    print(f"内部对冲化解股数 (Crossed Shares: {fs.total_crossed_shares:>10,d} 股")
    print(f"无效换手削减比例 (Churn Reduction: {fs.churn_reduction_ratio:>10.2%}")
    print("-" * 115)
    print(f"  • 节约印花税及规费 (A股卖出0.05% / 港股双边0.1085%) : ¥{fs.saved_stamp_duty:>10,.2f} RMB")
    if fs.saved_sec_fee > 0:
        print(f"  • 节约美股监管规费 (SEC Section 31 0.00278%)        : ¥{fs.saved_sec_fee:>10,.2f} RMB")
    print(f"  • 节约双边券商佣金 (0.03% 券商佣金)                 : ¥{fs.saved_commission:>10,.2f} RMB")
    print(f"  • 节约双边冲击成本 (0.05% 买卖滑点)                 : ¥{fs.saved_slippage:>10,.2f} RMB")
    if fs.saved_by_market:
        m_parts = [f"{get_market_label(m)}: ¥{val:,.2f}" for m, val in fs.saved_by_market.items() if val > 0]
        if m_parts:
            print(f"  • 各市场节约分布   ({', '.join(m_parts)})")
    print(f"  ★ 合计为账户挽回摩擦损耗                           : ¥{fs.total_friction_saved:>10,.2f} RMB")
    print("=" * 115)

    # Display Active Crossing Symbols
    active_crossed = [c for c in res.crossed_details if c.crossed_shares > 0]
    if active_crossed:
        print("\n[高对冲标的明细表 (Internally Crossed Assets Breakdown)]")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'毛买股数':>9} {'毛卖股数':>9} {'对冲股数':>9} {'净执行股数':>10} {'节约金额':>10}")
        print("-" * 105)
        for c in active_crossed:
            cs = curr_sym(c.market)
            print(f"{c.symbol:<11} {get_market_label(c.market):<4} {c.lot_size:>5d} {pad_east_asian(c.name, 10)} {c.price:>8.2f} "
                  f"{c.gross_buy_shares:>9,d} {c.gross_sell_shares:>9,d} {c.crossed_shares:>9,d} {c.net_shares:>10,d} {cs}{c.saved_friction:>9,.2f}")
        print("-" * 105)

    # 7. Display Standard 4-Phase Netted Execution Ticket
    print("\n" + "=" * 115)
    print("主账户全局四阶段净额执行工单 (GLOBAL 4-PHASE NET EXECUTION TICKET)")
    print("=" * 115)

    df = res.net_orders_df
    sells = df[df["action"] == "SELL"]
    buys = df[df["action"] == "BUY"]
    holds = df[df["action"].str.startswith("HOLD")]
    cash = df[df["symbol"] == "BIL"]

    if not sells.empty:
        print("\n【阶段一：净卖出优先执行 (PHASE 1: NET SELLS FIRST - 释放购买力)】")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'当前股数':>9} {'目标股数':>9} {'净卖股数':>9} {'释放资金':>12} {'说明理由':<25}")
        print("-" * 115)
        for _, r in sells.iterrows():
            cs = curr_sym(r.get("market", "CHINA"))
            print(f"{r['symbol']:<11} {get_market_label(r.get('market', 'CHINA')):<4} {r.get('lot_size', 100):>5d} {pad_east_asian(r['name'], 10)} {r['price']:>8.2f} "
                  f"{r['current_shares']:>9,d} {r['target_shares']:>9,d} {abs(r['delta_shares']):>9,d} {cs}{r['trade_value']:>11,.2f} {r['reason']}")
    else:
        print("\n【阶段一：净卖出优先执行 (PHASE 1: NET SELLS FIRST)】: 今日无卖出指令 (No Sell Orders)")

    if not buys.empty:
        print("\n【阶段二：净买入规范执行 (PHASE 2: NET BUYS SECOND - 整手建仓)】")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'当前股数':>9} {'目标股数':>9} {'净买股数':>9} {'所需资金':>12} {'说明理由':<25}")
        print("-" * 115)
        for _, r in buys.iterrows():
            cs = curr_sym(r.get("market", "CHINA"))
            print(f"{r['symbol']:<11} {get_market_label(r.get('market', 'CHINA')):<4} {r.get('lot_size', 100):>5d} {pad_east_asian(r['name'], 10)} {r['price']:>8.2f} "
                  f"{r['current_shares']:>9,d} {r['target_shares']:>9,d} {r['delta_shares']:>9,d} {cs}{r['trade_value']:>11,.2f} {r['reason']}")
    else:
        print("\n【阶段二：净买入规范执行 (PHASE 2: NET BUYS SECOND)】: 今日无买入指令 (No Buy Orders)")

    if not holds.empty:
        print(f"\n【阶段三：持仓维持与锁仓 (PHASE 3: NET POSITIONS HELD - {len(holds)} 只标的)】")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'持有股数':>9} {'持仓市值':>12} {'当前权重':>8} {'目标权重':>8} {'说明状态':<25}")
        print("-" * 115)
        for _, r in holds.iterrows():
            mval = r['target_shares'] * r['price']
            cs = curr_sym(r.get("market", "CHINA"))
            print(f"{r['symbol']:<11} {get_market_label(r.get('market', 'CHINA')):<4} {r.get('lot_size', 100):>5d} {pad_east_asian(r['name'], 10)} {r['price']:>8.2f} "
                  f"{r['target_shares']:>9,d} {cs}{mval:>11,.2f} {r['current_weight']:>7.1%} {r['target_weight']:>7.1%} {r['reason']}")

    if not cash.empty:
        print("\n【阶段四：闲置资金现金管理 (PHASE 4: GLOBAL CASH SWEEP)】")
        r_cash = cash.iloc[0]
        print(f"  现金管理工具代码 : {r_cash['symbol']} (银华日利 / 国债逆回购 GC001 / 美元国债 BIL)")
        print(f"  执行后可用现金   : ¥{r_cash['target_shares']:>10,.2f} ({r_cash['target_weight']:>5.1%})")
        print(f"  调仓净发生变化   : ¥{r_cash['delta_shares']:>10,.2f}")
        print(f"  执行动作状态     : {r_cash['action']} ({r_cash['reason']})")
    print("=" * 115)

    # 8. Save Artifacts & Reports
    ticket_csv = os.path.join(args.output_dir, "netted_trading_ticket.csv")
    ticket_json = os.path.join(args.output_dir, "netted_trading_ticket.json")
    savings_json = os.path.join(args.output_dir, "friction_savings_report.json")
    ledgers_json = os.path.join(args.output_dir, "sub_ledgers.json")

    df.to_csv(ticket_csv, index=False, encoding="utf-8-sig")
    with open(ticket_json, "w", encoding="utf-8") as f:
        json.dump(res.net_orders, f, indent=2, ensure_ascii=False)
    with open(savings_json, "w", encoding="utf-8") as f:
        json.dump(res.to_dict()["friction_savings"], f, indent=2, ensure_ascii=False)
    with open(ledgers_json, "w", encoding="utf-8") as f:
        json.dump(res.sub_ledgers, f, indent=2, ensure_ascii=False)

    print(f"\n[✓] 净额化调仓票据已成功导出至:")
    print(f"    工单表格 (CSV)  : {ticket_csv}")
    print(f"    工单数据 (JSON) : {ticket_json}")
    print(f"    节约报告 (JSON) : {savings_json}")
    print(f"    虚拟账本 (JSON) : {ledgers_json}")


if __name__ == "__main__":
    main()
