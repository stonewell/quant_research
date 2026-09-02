"""Shared CLI scaffolding used by every project's `run_*.py` entrypoint in this
workspace: default results/data directory conventions, the standard
`--data-provider`/`--data-dir`/`--no-cache` argparse trio, a universe-loading
wrapper with the "Loading N symbols... Loaded M/N..." console banner, and a
`sys.path` bootstrap helper. Pairs with `common/universe.py`'s
`add_universe_cli_args`/`resolve_universe_from_args`.
"""

import os
import sys
from typing import Optional

from .data import load_universe

_PROVIDER_ROTATION = ["yfinance", "csv", "synthetic"]


def bootstrap_project_paths(repo_root: str, caller_file: str, groups: tuple = ("pipeline", "ml")) -> None:
    """Adds the caller's own directory and each sibling group directory
    (`pipeline/`, `ml/`) to `sys.path`, so bare `import research_strategy...`-
    style modules resolve regardless of which project's `run_*.py` imports
    this first. Callers must add `repo_root` to `sys.path` THEMSELVES in one
    line before importing this module -- `common/` itself isn't reachable
    until that's already done, a real chicken-and-egg exception every caller
    keeps inline:

        _REPO_ROOT = os.path.dirname(...)  # however many dirname() calls the caller's own depth needs
        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        from common.cli_utils import bootstrap_project_paths
        bootstrap_project_paths(_REPO_ROOT, __file__)
    """
    own_dir = os.path.dirname(os.path.abspath(caller_file))
    for d in (own_dir, *(os.path.join(repo_root, g) for g in groups)):
        if d not in sys.path:
            sys.path.insert(0, d)


def add_output_dir_override_args(parser, results_dir_default: str, cache_dir_default: str,
                                  results_artifact_desc: str) -> None:
    """Adds the standard `--results-dir`/`--cache-dir` override pair (shared
    verbatim by `backtester/run_backtest.py` and `pipeline/live_signal/run_live_signal.py`
    before this was centralized). Callers still resolve the actual value
    themselves (`args.results_dir or RESULTS_DIR`) -- too trivial a line to
    abstract further."""
    parser.add_argument("--results-dir", type=str, default=None,
                         help=f"Override the output directory for {results_artifact_desc} (default: {results_dir_default})")
    parser.add_argument("--cache-dir", type=str, default=None,
                         help=f"Override the shared, workspace-wide OHLCV CSV cache directory (default: {cache_dir_default})")


def default_results_dir(caller_file: str) -> str:
    """RESULTS_DIR = default_results_dir(__file__) -- a sibling 'results/'
    directory of the caller's own file."""
    return os.path.join(os.path.dirname(os.path.abspath(caller_file)), "results")


def shared_data_dir() -> str:
    """SHARED_DATA_DIR = shared_data_dir() -- resolves to <repo_root>/data, a
    single OHLCV cache directory shared by every project in this workspace,
    regardless of which project's run_*.py calls it (unlike a per-caller
    default, this always resolves relative to this file's own location, one
    level up from `common/`)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def add_data_provider_cli_args(parser, default_provider: str = "yfinance",
                                no_cache_help: Optional[str] = None) -> None:
    """Adds the standard --data-provider/--data-dir/--no-cache trio. The help
    text for --data-provider lists the default provider first, matching every
    project's own hand-written wording, by rotating the canonical provider
    order to start at `default_provider`."""
    if default_provider in _PROVIDER_ROTATION:
        i = _PROVIDER_ROTATION.index(default_provider)
        order = _PROVIDER_ROTATION[i:] + _PROVIDER_ROTATION[:i]
    else:
        order = [default_provider] + [p for p in _PROVIDER_ROTATION if p != default_provider]
    provider_list = ", ".join(f"'{p}'" for p in order)
    parser.add_argument(
        "--data-provider", default=default_provider,
        help=f"Market data source provider ({provider_list}, or custom module specifier "
             f"string e.g. 'script.py:CustomProvider')",
    )
    parser.add_argument("--data-dir", type=str, default=None, help="Folder path for CSV data provider")
    kwargs = {"help": no_cache_help} if no_cache_help else {}
    parser.add_argument("--no-cache", action="store_true", **kwargs)
    parser.add_argument(
        "--cache-ttl-days", type=float, default=None,
        help="Max age, in days, of a cached OHLCV CSV file before it's treated as stale and "
             "re-fetched from the provider (default: None = cached files never expire, today's "
             "unchanged behavior).",
    )


def build_data_kwargs(args) -> dict:
    """{'provider': args.data_provider} plus an optional 'folder_path' -- the
    load_universe/load_ohlcv kwargs every run_*.py builds from parsed args."""
    kwargs = {"provider": args.data_provider}
    if getattr(args, "data_dir", None):
        kwargs["folder_path"] = args.data_dir
    return kwargs


def load_universe_with_banner(symbols, start, end, interval: str = "1d", *,
                               use_cache: bool = True, cache_dir: Optional[str] = None,
                               data_kwargs: Optional[dict] = None,
                               require_nonempty: bool = True,
                               loading_msg: Optional[str] = None,
                               cache_max_age_days: Optional[float] = None) -> dict:
    """`common.data.load_universe` wrapped with the standard console banner
    ('Loading N symbols ...' / 'Loaded M/N symbols (see warnings above for any
    skipped).') and, when `require_nonempty`, a ValueError if every symbol
    failed to load. `loading_msg` overrides the opening line verbatim for
    callers whose existing wording includes extra context (e.g. a date range).
    `cache_max_age_days` is passed straight through to `load_universe` (see
    its docstring) -- None (default) preserves today's never-expire behavior.
    """
    data_kwargs = data_kwargs or {}
    print(loading_msg if loading_msg is not None else f"Loading {len(symbols)} symbols ...")
    universe = load_universe(symbols, start, end, interval, use_cache=use_cache,
                              cache_dir=cache_dir, cache_max_age_days=cache_max_age_days, **data_kwargs)
    print(f"Loaded {len(universe)}/{len(symbols)} symbols (see warnings above for any skipped).")
    if require_nonempty and not universe:
        raise ValueError("No symbols could be loaded successfully; see warnings above.")
    return universe
