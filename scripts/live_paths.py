#!/usr/bin/env python3
"""Single source of truth for where the live-trading artifacts are written and read.

Why this module exists
----------------------
Until 2026-10-23 `scripts/run_live_strategy.py` defaulted `--output-dir` to
`<repo_root>/docs/ruleset/<strategy_key>`: a *live run* wrote its machine-generated
order ticket (`live_trading_ticket.csv` / `.json`), persistent account state
(`account_state.json`), `current_holdings.json`, `post_holdings.json` and a Stage-1
health report straight into the hand-authored documentation tree. `docs/ruleset/**`
holds committed operating manuals (`rules.md` / `rules_cn.md` / PDFs); running the
tool at all would dirty `docs/`, and any test that forgot `--output-dir` would write
real order tickets into a docs directory instead of a scratch one.

Why it is a module and not two literals
---------------------------------------
The Stage-1 -> Stage-2 contract makes the default a TWO-SITE value, not one:
  * Stage 1 (`scripts/check_live_portfolio_health.py`) WRITES
    `stage1_health_report.json` and `account_state.json` into *its* `--output-dir`;
  * Stage 2 (`scripts/run_live_strategy.py`) READS both of them back from *its*
    `--output-dir` (see `--health-report-file` / `--account-state-file`, which
    default to `<output-dir>/stage1_health_report.json` and
    `<output-dir>/account_state.json`).
Two independent literals that currently agree are exactly how "tickets written
here, health check reads there" splits silently -- the very class of bug already
documented for `resolve_cache_db_path` mirroring `duckdb_cache.py:774-778`. So the
default lives here, once, and the scripts call `default_live_output_dir()` /
import `DEFAULT_STRATEGY_KEY` from this module. None may re-spell the literal.

The multi-strategy netted runner is a second writer with a reader
---------------------------------------------------------
`scripts/run_live_multi_strategy.py` wrote the same class of machine-generated
artifact (netted ticket CSV/JSON, `friction_savings_report.json`, `sub_ledgers.json`)
into `docs/ruleset/multi_strategy_netted`, and it has a SECOND site as well:
`web/livetrading/storage.py::StorageManager.get_multi_netting_run` falls back to the
runner's default directory when the dated archive
(`<archive_dir>/<date>/multi_netted_<matrix>`) has no netted run yet -- i.e. when the
ticket was produced by the CLI rather than by the web deploy path. That fallback used
to be spelled independently in `storage.py`, which is exactly the drift this module
prevents. Both now resolve through `MULTI_STRATEGY_KEY` /
`default_live_output_dir()` here (the web module re-exports the resolved directory as
`MULTI_NETTING_FALLBACK_DIR`), so "CLI writes here, web reads there" cannot split.

Default layout
--------------
    <repo_root>/scripts/results/live/<strategy_key>/

Evidence this matches repo conventions rather than inventing a layout:
  * `scripts/run_batch_backtest.py:205` already defaults `--output-dir` to
    `<repo_root>/scripts/results/batch`. This module's root is the live-trading
    sibling of that directory: "a script under `scripts/` that emits
    machine-generated run artifacts writes them under `scripts/results/`".
  * It is NOT under `docs/` -- the whole point: that tree is hand-authored manuals.
  * It is NOT under `<repo_root>/data/` either. `<repo_root>/data` is hardcoded as
    the production market-cache root by `web/livetrading/storage.py`
    `production_path_roots()` ("a directory a pytest process must never resolve a
    writable path into"), and it holds `cache.duckdb`. Live order tickets must not
    default into that root.

Runner-up candidate (deliberately NOT chosen): `<repo_root>/data/live/<strategy_key>`,
justified by `AGENTS.md`'s "immutable daily snapshots under `data/runs/<date>/`" and
the `--output-dir data/test_run/` example in `docs/version.md`. It lost on two
counts: (a) the output dir must hold PERSISTENT per-strategy state
(`account_state.json`, `current_holdings.json`) that has to survive across runs, so a
date-keyed archive tree does not fit; (b) `data/` is the guarded production cache
root above.

`--output-dir` on any of these scripts remains an explicit operator override; nothing in
this module is consulted when one is passed.
"""

import os

# This file lives at <repo_root>/scripts/live_paths.py and is only ever imported as
# `scripts.live_paths` (every caller -- `scripts/run_live_strategy.py`,
# `scripts/check_live_portfolio_health.py`, `scripts/run_live_multi_strategy.py` and
# `web/livetrading/storage.py` -- puts <repo_root> on sys.path before importing).
# It is never executed standalone, so no AGENTS.md walk-up probe is needed -- the
# repo root is exactly one directory above `scripts/`.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The strategy whose per-strategy output directory Stage 1 and Stage 2 default to.
#: Shared so the two scripts cannot drift: Stage 2 binds its default to the
#: *resolved* strategy key, Stage 1 has no strategy argument of its own and must
#: therefore name the same key explicitly. Re-spelling "chan_four_state_blend" in
#: either file would re-open the split this module exists to close.
DEFAULT_STRATEGY_KEY = "chan_four_state_blend"

#: Output-directory key of the MULTI-strategy netted runner
#: (`scripts/run_live_multi_strategy.py`), which nets several sub-strategies into one
#: master-account ticket instead of being a strategy of its own. Shared for the same
#: reason as `DEFAULT_STRATEGY_KEY`: the reader
#: (`web/livetrading/storage.py::StorageManager.get_multi_netting_run`) must find the
#: exact directory the CLI writes, so the key may not be re-spelled at either end.
MULTI_STRATEGY_KEY = "multi_strategy_netted"

#: Root of the live-trading artifact tree: `<repo_root>/scripts/results/live`.
LIVE_OUTPUT_ROOT = os.path.join(REPO_ROOT, "scripts", "results", "live")


def default_live_output_dir(strategy_key: str = None) -> str:
    """The default `--output-dir` for Stage 1, Stage 2 and the netted runner (absolute path).

    Holds a per-strategy directory (not a per-date one) because it also carries
    persistent state files that must be found again on the next run. The directory
    is NOT created here: `scripts/run_live_strategy.py` creates it only at the point
    it actually writes a ticket (`os.makedirs(args.output_dir, exist_ok=True)`), so
    merely parsing arguments -- or a run that aborts at the gate -- leaves no empty
    directory behind in the workspace. `scripts/run_live_multi_strategy.py` likewise
    creates it at its single `os.makedirs(args.output_dir, exist_ok=True)` write site.
    """
    return os.path.join(LIVE_OUTPUT_ROOT, strategy_key or DEFAULT_STRATEGY_KEY)
