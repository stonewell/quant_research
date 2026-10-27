"""Shared CLI scaffolding used by every project's `run_*.py` entrypoint in this
workspace: default results/data directory conventions, the standard
`--data-provider`/`--data-dir`/`--no-cache` argparse trio, a universe-loading
wrapper with the "Loading N symbols... Loaded M/N..." console banner, and a
`sys.path` bootstrap helper. Pairs with `common/universe.py`'s
`add_universe_cli_args`/`resolve_universe_from_args`.

It also owns the **live-run market-clock plumbing** (`L2.3`): the five
`--market` / `--allow-intraday` / `--strict-market-clock` / `--strict-freshness`
/ `--max-probe-attempts` (+ `--publication-grace-minutes`) flags, the as-of
resolver, the run-window gate and the `summary.json` clock stamp.

**How §5's "intraday => refuse by default" row is reconciled with §8/§14.** Taken
literally the two cancel out: Stage 2's designed window is `(14:10, close)`, i.e. itself
intraday, so "refuse every intraday run" would refuse its own window. The rule implemented
here is the window-based one (§14 row 2, "不按是否盘中判，按窗口判"):

* inside the declared market-local window => allowed; an intraday instant still gets a
  loud note, because the valuation as-of is the **previous published session** either way
  (today's daily bar is unfinished and is never marked to);
* `NON_TRADING_DAY` / `CLOSED`, or a pinned `--as-of-date` earlier than the clock's
  session (a deliberate replay) => the window does not apply;
* any other instant (the 2026-10-22 case: Beijing 10:05, intraday and ~4h early) =>
  **refused**, and `--allow-intraday` (or `--no-strict-market-clock`) is the single
  documented way to override it.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .data import load_universe
from .freshness import StalePriceError, build_freshness_summary, is_stale_bar
from .market_clock import (
    MARKET_CHINA,
    MARKET_HK,
    MARKET_US,
    MarketClock,
    MixedMarketUniverseError,
    SessionState,
    UnknownSymbolMarketError,
    market_now,
    normalize_market,
    resolve_market,
)

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
                         help=f"Override the shared, workspace-wide OHLCV DuckDB cache directory (default: {cache_dir_default})")


def default_results_dir(caller_file: str) -> str:
    """RESULTS_DIR = default_results_dir(__file__) -- a sibling 'results/'
    directory of the caller's own file."""
    return os.path.join(os.path.dirname(os.path.abspath(caller_file)), "results")


def shared_data_dir() -> str:
    """SHARED_DATA_DIR = shared_data_dir() -- resolves to <repo_root>/data, a
    single OHLCV cache directory shared by every project in this workspace,
    regardless of which project's run_*.py calls it (unlike a per-caller
    default, this always resolves relative to this file's own location, one
    level up from `common/`).

    This is the `--cache-dir` default, so it lands on the production cache
    (`<repo_root>/data/cache.duckdb`). Tests must not write there: the
    `DuckDBCache.__init__` guard refuses writable opens from a pytest process
    (`common/duckdb_cache.py::ProductionCachePathError`); pass an explicit
    temporary directory instead.
    """
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
                               expected_last_session: Optional[dt.date] = None) -> dict:
    """`common.data.load_universe` wrapped with the standard console banner
    ('Loading N symbols ...' / 'Loaded M/N symbols (see warnings above for any
    skipped).') and, when `require_nonempty`, a ValueError if every symbol
    failed to load. `loading_msg` overrides the opening line verbatim for
    callers whose existing wording includes extra context (e.g. a date range).

    `expected_last_session` (`L2.2`/`L2.3`) is the session the cache MUST hold; it
    is threaded straight into `DuckDBCache.is_range_covered`. Backtest/research
    callers leave it `None` (the bit-for-bit legacy path); every live caller passes
    the value resolved by `resolve_live_clock_context()`.
    """
    data_kwargs = data_kwargs or {}
    print(loading_msg if loading_msg is not None else f"Loading {len(symbols)} symbols ...")
    universe = load_universe(symbols, start, end, interval, use_cache=use_cache,
                              cache_dir=cache_dir, expected_last_session=expected_last_session,
                              **data_kwargs)
    print(f"Loaded {len(universe)}/{len(symbols)} symbols (see warnings above for any skipped).")
    if require_nonempty and not universe:
        raise ValueError("No symbols could be loaded successfully; see warnings above.")
    return universe


# --------------------------------------------------------------------------- #
# Market-clock plumbing for the live entrypoints (L2.3)
# --------------------------------------------------------------------------- #
#: Design §8 default run windows, in **market-local** wall clock. Stage 1 is the
#: pre-trade health gate; Stage 2's window ends at each market's own close, which
#: is why it cannot be a single literal (the old hard-coded "14:15"/"14:00"
#: strings were bare A-share numbers that meant nothing for HK/US).
_STAGE1_WINDOW: Tuple[dt.time, dt.time] = (dt.time(13, 55), dt.time(14, 20))
_STAGE2_WINDOW_START: dt.time = dt.time(14, 10)
_MARKET_CLOSE_TIMES: Dict[str, dt.time] = {
    MARKET_CHINA: dt.time(15, 0),
    MARKET_HK: dt.time(16, 0),
    MARKET_US: dt.time(16, 0),
}

_STAGE_CHOICES = ("stage1", "stage2")

#: Liquidity / cash-parking vehicles that may sit in a single-market book without
#: changing which market the book belongs to (design §14 row 3 only forbids guessing a
#: calendar for a *cross-market equity* book). They are excluded from market INFERENCE,
#: never from freshness checking. `511880` is China-listed, `BIL`/`SHV`/`SGOV` are US
#: T-bill ETFs and `GC001` a repo code -- without this set every A-share universe that
#: carries a `BIL` cash leg would be rejected as "cross-market".
CASH_PROXY_SYMBOLS: frozenset = frozenset(
    {"BIL", "CASH", "MONEY", "SHV", "SGOV", "USFR", "511880", "511990", "GC001"}
)


class RunWindowError(RuntimeError):
    """The run-window gate refused this instant (design §8).

    Deliberately NOT a `ValueError`: this is an operational-policy refusal, not a
    malformed input, and callers that catch `ValueError` for config errors must not
    accidentally swallow it. The message always carries stage, market, market-local
    time, session state and the expected window -- "not in window" alone gives an
    operator nothing to act on.
    """


def market_run_window(market: str, stage: str = "stage2") -> Tuple[dt.time, dt.time]:
    """Default run window for `market`/`stage`, as naive market-LOCAL times (§8)."""
    canonical = normalize_market(market)
    if stage not in _STAGE_CHOICES:
        raise ValueError(f"stage must be one of {_STAGE_CHOICES}, got {stage!r}")
    if stage == "stage1":
        return _STAGE1_WINDOW
    return (_STAGE2_WINDOW_START, _MARKET_CLOSE_TIMES[canonical])


@dataclass(frozen=True)
class RunWindowVerdict:
    """Result of evaluating the gate; `outcome` is persisted verbatim in `summary.json`."""

    outcome: str  # in_window | not_applicable | intraday_allowed | downgraded_outside_window
    note: str
    in_window: bool


def assert_run_window(
    clock: MarketClock,
    stage: str,
    window: Tuple[dt.time, dt.time],
    *,
    allow_intraday: bool = False,
    strict: bool = True,
) -> RunWindowVerdict:
    """Refuse (or warn) when *now* is outside the market's declared run window.

    Semantics (design §5 + §8 + §14 row 2, reconciled -- see the module docstring):

    * **inside** the window => allowed. An intraday instant inside the window still
      gets a loud note, because the valuation as-of for an intraday run is the
      *previous* published session (the current day's daily bar is not finished and
      must never be marked to).
    * `NON_TRADING_DAY` / `CLOSED` => the window is **not applicable** (there is no
      continuous session to be early or late for). This is the weekend/holiday and
      post-close valuation case §5 explicitly allows: as_of is the last session.
    * `PRE_OPEN` / `OPENING_AUCTION` / `OPEN` / `LUNCH_BREAK` **outside** the window
      => refused by default. `--allow-intraday` (or `--no-strict-market-clock`)
      downgrades it to a warning; the 2026-10-22 run (Beijing 10:05, i.e. intraday
      and ~4h before the designed window) is exactly this branch.
    """
    if stage not in _STAGE_CHOICES:
        raise ValueError(f"stage must be one of {_STAGE_CHOICES}, got {stage!r}")
    state = clock.session_state()
    in_window = clock.in_run_window(window)

    if in_window:
        note = ""
        if clock.is_intraday:
            note = (
                f"盘中窗口内运行 (session={state.value})：估值 as_of 仍为上一已收盘交易日 "
                f"{clock.as_of_trading_date().isoformat()}，当日未完成的日线 bar 不参与估值。"
            )
        return RunWindowVerdict(outcome="in_window", note=note, in_window=True)

    if state in (SessionState.NON_TRADING_DAY, SessionState.CLOSED):
        return RunWindowVerdict(
            outcome="not_applicable",
            note=(
                f"{state.value}: 运行窗口不适用（非连续竞价时段），as_of="
                f"{clock.as_of_trading_date().isoformat()}。"
            ),
            in_window=False,
        )

    if allow_intraday:
        return RunWindowVerdict(
            outcome="intraday_allowed",
            note=(
                f"显式 --allow-intraday：market={clock.market} session={state.value} "
                f"在窗口 {window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')} 之外运行，已放行并告警。"
            ),
            in_window=False,
        )

    if not strict:
        return RunWindowVerdict(
            outcome="downgraded_outside_window",
            note=(
                f"--no-strict-market-clock：market={clock.market} session={state.value} "
                f"在窗口 {window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')} 之外运行，已降级为警告。"
            ),
            in_window=False,
        )

    raise RunWindowError(
        f"[{stage}] 运行窗口门禁拒绝 (RUN WINDOW GATE REFUSED): "
        f"stage={stage} market={clock.market} "
        f"local_now={clock.local_now().isoformat()} session={state.value} "
        f"expected_window={window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')} "
        f"(市场本地时间) as_of={clock.as_of_trading_date().isoformat()}。"
        "窗口外运行会把「此刻」当成实时，而手里实际是过期的收盘价"
        "（2026-10-22 事故：北京 10:05 运行，估值日 10-22，6 只标的 K 线停在 10-21）。"
        "处理：在市场本地窗口内运行；或显式 --allow-intraday 放行盘中运行；"
        "或 --no-strict-market-clock 降级为警告。"
    )


@dataclass(frozen=True)
class LiveClockContext:
    """Everything a live stage resolved from the market clock, in one immutable object.

    One instance per run. `as_of_date` is the valuation session; `expected_last_session`
    is what the DuckDB cache must hold for it to be usable (identical unless an
    explicit `--as-of-date` was given, in which case the override is what is required,
    never the newer clock session -- demanding a bar the operator did not ask for
    would make a historical replay impossible).
    """

    clock: MarketClock
    stage: str
    market: str
    market_inference: str
    as_of_date: str
    clock_as_of: str
    as_of_source: str
    expected_last_session: dt.date
    freshness_strict: bool
    window: Tuple[dt.time, dt.time]
    window_gate: str
    window_note: str
    allow_intraday: bool
    strict_market_clock: bool
    publication_grace_minutes: int
    max_probe_attempts: int

    def stamp_fields(self) -> Dict[str, Any]:
        """Design §9 fields: tz-aware UTC + market-local + session + as_of + gate state."""
        stamp = self.clock.stamp().to_dict()
        stamp.update(
            {
                "stage": self.stage,
                "market_inference": self.market_inference,
                "as_of_source": self.as_of_source,
                # Design §9: `as_of_trading_date` is "the trading day THIS valuation is
                # based on". For a `--as-of-date` replay that is the pinned session, not
                # the clock's newer one, so the two are split explicitly rather than
                # letting the bare clock stamp answer for a run it did not describe.
                "as_of_trading_date": self.as_of_date,
                "clock_as_of_trading_date": self.clock_as_of,
                "expected_last_session": self.expected_last_session.isoformat(),
                "run_window": f"{self.window[0].strftime('%H:%M')}-{self.window[1].strftime('%H:%M')}",
                "run_window_gate": self.window_gate,
                "run_window_note": self.window_note,
                "allow_intraday": self.allow_intraday,
                "strict_market_clock": self.strict_market_clock,
                "strict_freshness": self.freshness_strict,
                "publication_grace_minutes": self.publication_grace_minutes,
                "max_probe_attempts": self.max_probe_attempts,
            }
        )
        return stamp

    def render_banner(self) -> str:
        return (
            f"[MARKET CLOCK] stage={self.stage} market={self.market}"
            f"({self.market_inference}) session={self.clock.session_state().value} "
            f"utc={self.clock.now.isoformat()} local={self.clock.local_now().isoformat()} "
            f"as_of={self.as_of_date}({self.as_of_source}) "
            f"expected_last_session={self.expected_last_session.isoformat()} "
            f"window={self.window[0].strftime('%H:%M')}-{self.window[1].strftime('%H:%M')}"
            f"[{self.window_gate}] strict_freshness={self.freshness_strict}"
        )


def add_market_clock_cli_args(parser: argparse.ArgumentParser, *, stage: str) -> None:
    """Add the market-clock flags shared by every live entrypoint (design §8).

    stage is only used to pick the default window in `--help` text; the gate itself
    re-derives it via `market_run_window()`.

    `--as-of-date` is NOT added here (each script already declares it); its help text
    is updated in place to say the default now comes from the market clock.
    """
    if stage not in _STAGE_CHOICES:
        raise ValueError(f"stage must be one of {_STAGE_CHOICES}, got {stage!r}")
    window = _STAGE1_WINDOW if stage == "stage1" else (dt.time(14, 10), dt.time(15, 0))
    parser.add_argument(
        "--market",
        choices=(MARKET_CHINA, MARKET_HK, MARKET_US),
        default=None,
        help=(
            "Authoritative market of this book (china/hk/us). Selects the timezone, session table and "
            "holiday calendar used to resolve the as-of date. Default: inferred from the universe file's "
            "symbol suffixes; a mixed universe with no --market is an error (design §14 row 3)."
        ),
    )
    parser.add_argument(
        "--allow-intraday",
        action=argparse.BooleanOptionalAction,
        default=(stage == "stage1"),
        help=(
            "Explicitly allow an INTRADAY run outside the default run window "
            f"({window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')} market-local for {stage}). "
            "Stage 1 defaults to True (health check on latest published session); Stage 2 defaults to False. "
            "Without it, an OPEN/LUNCH_BREAK market outside that window refuses to generate orders: that is "
            "the 2026-10-22 failure (run at Beijing 10:05, valuation 10-22, six symbols' bars stuck at 10-21). "
            "As-of stays the last PUBLISHED session either way -- today's unfinished daily bar is never used."
        ),
    )
    parser.add_argument(
        "--strict-market-clock",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Live default True: refuse to run outside the market-local run window. "
            "--no-strict-market-clock downgrades that refusal to a loud warning (rollback escape hatch)."
        ),
    )
    parser.add_argument(
        "--strict-freshness",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Live default True: any symbol whose latest bar is NOT the as-of session is a hard failure "
            "(StalePriceError) -- stale closes silently corrupt NAV, weights and share counts. "
            "--no-strict-freshness downgrades it to a loud warning and marks the ticket."
        ),
    )
    parser.add_argument(
        "--publication-grace-minutes",
        type=int,
        default=None,
        help=(
            "Minutes after the market close before that session's daily bar is assumed published "
            "(default: 30 China/HK, 15 US; an estimate, see design §14 row 4). Controls the as-of date."
        ),
    )
    parser.add_argument(
        "--max-probe-attempts",
        type=int,
        default=3,
        help=(
            "LIVE only: how many times to re-probe the provider when bars still stop before the expected "
            "session (exponential backoff 0.5*2^n seconds). Default 3; 1 disables re-probing."
        ),
    )


def resolve_live_clock_context(
    args,
    symbols,
    stage: str = "stage2",
    *,
    now: Optional[dt.datetime] = None,
    holidays: Optional[Iterable[dt.date]] = None,
) -> LiveClockContext:
    """Resolve market + as-of + gate verdict for one live stage (design §5/§8).

    `now`/`holidays` are injected by tests; production passes neither, so the single
    wall-clock read happens inside `common.market_clock.market_now()`.

    Raises `RunWindowError` when the gate refuses, and `ValueError` for an
    `--as-of-date` that cannot be honoured (non-trading day, or a session whose close
    has not been published yet -- the latter is precisely what would have let the
    2026-10-22 run value at a bar the cache did not hold).
    """
    if stage not in _STAGE_CHOICES:
        raise ValueError(f"stage must be one of {_STAGE_CHOICES}, got {stage!r}")

    explicit_market = getattr(args, "market", None)
    if explicit_market:
        market_inference = "explicit"
    else:
        market_inference = "inferred"
    if isinstance(symbols, dict):
        symbols = list(symbols.keys())
    equity_symbols = [
        s for s in (symbols or []) if str(s).strip().upper() not in CASH_PROXY_SYMBOLS
    ]
    if explicit_market is None and len(equity_symbols) != len(list(symbols or [])):
        market_inference = "inferred_excluding_cash_proxies"
    if explicit_market is None and not equity_symbols:
        raise ValueError(
            "universe 只包含现金/流动性代理标的 ("
            f"{sorted({str(s) for s in (symbols or [])})})，无法推断所属市场；"
            "请显式传入 --market {china,hk,us} 以选择时区与节假日历。"
        )
    try:
        market = resolve_market(explicit_market, equity_symbols)
    except MixedMarketUniverseError as exc:
        raise ValueError(
            f"{exc}. 实盘必须以单一市场的时区/节假日历计算 as_of；请显式传入 "
            "--market {china,hk,us}，或把跨市场标的拆成两个组合。"
        ) from exc
    except UnknownSymbolMarketError as exc:
        raise ValueError(
            f"{exc}. 无法从标的推断市场，请显式传入 --market {{china,hk,us}}。"
        ) from exc

    clock = market_now(
        market,
        now=now,
        holidays=holidays,
        publication_grace_minutes=getattr(args, "publication_grace_minutes", None),
    )
    clock_as_of = clock.as_of_trading_date()

    override = getattr(args, "as_of_date", None)
    if override:
        try:
            parsed = dt.date.fromisoformat(str(override).strip())
        except ValueError as exc:
            raise ValueError(
                f"--as-of-date 必须是 YYYY-MM-DD，收到 {override!r}: {exc}"
            ) from exc
        if not clock.is_trading_day(parsed):
            raise ValueError(
                f"--as-of-date {parsed.isoformat()} 不是 {market} 的交易日 "
                f"(calendar={clock.calendar_status()})。估值日必须是交易日；"
                "休市日请使用其前一个交易日。"
            )
        if parsed > clock_as_of:
            raise ValueError(
                f"--as-of-date {parsed.isoformat()} 晚于市场时钟公布的最近已收盘交易日 "
                f"{clock_as_of.isoformat()} (market={market} "
                f"local={clock.local_now().isoformat()} session={clock.session_state().value})。"
                "该交易日的收盘价尚未收盘/尚未发布，拒绝用未完成的日线 bar 估值。"
                "若确需按更晚日期运行，请等到收盘+发布宽限之后（或调整 --publication-grace-minutes）。"
            )
        as_of = parsed
        expected_session = parsed
        as_of_source = "explicit"
    else:
        as_of = clock_as_of
        expected_session = clock_as_of
        as_of_source = "market_clock"

    allow_intraday = bool(getattr(args, "allow_intraday", stage == "stage1"))
    strict_market_clock = bool(getattr(args, "strict_market_clock", True))
    window = market_run_window(market, stage)
    # A pinned as-of EARLIER than the clock's published session is a deliberate historical
    # replay/backfill: "is now a good moment to trade?" is the wrong question for it (no
    # live order is being generated from today's data), and the cache/freshness expectation
    # has already been re-anchored to that session above. The window gate therefore reports
    # not-applicable and the run is labelled a replay -- while a run whose as-of IS the
    # clock's session (the normal live case, and what the web passes) is gated normally.
    is_replay = as_of_source == "explicit" and as_of < clock_as_of
    if is_replay:
        verdict = RunWindowVerdict(
            outcome="not_applicable_replay",
            note=(
                f"历史回放模式: --as-of-date={as_of.isoformat()} 早于时钟最近已收盘交易日 "
                f"{clock_as_of.isoformat()}，运行窗口门禁不适用（本工单不是当前时点的实盘指令）。"
            ),
            in_window=False,
        )
    else:
        verdict = assert_run_window(
            clock, stage, window, allow_intraday=allow_intraday, strict=strict_market_clock
        )

    # One decision, two spellings: `--strict-freshness` (design §8) and the older
    # `--allow-stale-prices` (emergency override documented on the runners). Both feed
    # a single boolean so no call site can honour one and ignore the other.
    freshness_strict = bool(getattr(args, "strict_freshness", True)) and not bool(
        getattr(args, "allow_stale_prices", False)
    )

    ctx = LiveClockContext(
        clock=clock,
        stage=stage,
        market=market,
        market_inference=market_inference,
        as_of_date=as_of.isoformat(),
        clock_as_of=clock_as_of.isoformat(),
        as_of_source=as_of_source,
        expected_last_session=expected_session,
        freshness_strict=freshness_strict,
        window=window,
        window_gate=verdict.outcome,
        window_note=verdict.note,
        allow_intraday=allow_intraday,
        strict_market_clock=strict_market_clock,
        publication_grace_minutes=clock.grace_minutes,
        max_probe_attempts=int(getattr(args, "max_probe_attempts", 3) or 1),
    )
    print(ctx.render_banner())
    if ctx.window_note:
        print(f"[MARKET CLOCK] {ctx.window_note}")
    if as_of_source == "explicit" and ctx.as_of_date != ctx.clock_as_of:
        print(
            f"[MARKET CLOCK] 显式 --as-of-date={ctx.as_of_date} 早于时钟最近已收盘交易日 "
            f"{ctx.clock_as_of}：按历史口径估值（回放/补算），新鲜度期望值同步降为 {ctx.as_of_date}。"
        )
    return ctx


def load_live_universe(
    args,
    symbols: Sequence[str],
    start: str,
    end: str,
    *,
    clock_ctx: LiveClockContext,
    use_cache: bool = True,
    cache_dir: Optional[str] = None,
    data_kwargs: Optional[dict] = None,
    interval: str = "1d",
    require_nonempty: bool = True,
    loading_msg: Optional[str] = None,
) -> Tuple[dict, int]:
    """`load_universe_with_banner` + bounded re-probe on stale bars (design §6, LIVE only).

    Every attempt passes `expected_last_session=clock_ctx.expected_last_session` into the
    cache gate, so a "covered" answer means the bars really reach the valuation session.
    If the provider still returns bars that stop short, we re-probe up to
    `--max-probe-attempts` times with exponential backoff (0.5*2^n s) and return the
    last answer plus the attempt count for `summary.json`.

    Fresheness JUDGEMENT is not re-implemented here: the retry loop only asks
    `common.freshness.is_stale_bar()` (the single owner) whether to try again; the hard
    assertion is `assert_fresh_universe()` at the call site.
    """
    attempts_max = max(1, int(getattr(clock_ctx, "max_probe_attempts", 1) or 1))
    expected_iso = clock_ctx.expected_last_session.isoformat()
    delay = 0.5
    universe: dict = {}
    attempt = 0
    for attempt in range(1, attempts_max + 1):
        universe = load_universe_with_banner(
            list(symbols),
            start,
            end,
            interval=interval,
            use_cache=use_cache,
            cache_dir=cache_dir,
            data_kwargs=data_kwargs,
            require_nonempty=require_nonempty,
            loading_msg=loading_msg,
            expected_last_session=clock_ctx.expected_last_session,
        )
        stale_now = [
            sym
            for sym, df in universe.items()
            if df is not None and not df.empty and is_stale_bar(df, expected_iso)
        ]
        if not stale_now or attempt >= attempts_max:
            if stale_now:
                print(
                    f"[WARN] 探测重试 {attempt}/{attempts_max} 后仍有 {len(stale_now)} 个标的的 bar 早于 "
                    f"expected_last_session={expected_iso}: {', '.join(sorted(stale_now)[:20])}"
                )
            return universe, attempt
        print(
            f"[WARN] 探测 {attempt}/{attempts_max}: {len(stale_now)} 个标的的最新 bar 早于 "
            f"expected_last_session={expected_iso}，{delay:.1f}s 后重取 "
            f"({', '.join(sorted(stale_now)[:10])})"
        )
        time.sleep(delay)
        delay *= 2
    return universe, attempt


def persist_run_metadata(
    output_dir: str,
    clock_ctx: LiveClockContext,
    *,
    freshness_summary: Optional[dict] = None,
    probe_attempts: Optional[int] = None,
    extra: Optional[dict] = None,
) -> str:
    """Merge the §9 observability block into `<output_dir>/summary.json`.

    A MERGE, not an overwrite: both live stages write this same file and the web
    layer (`web/livetrading/execution_service.py`) later `update()`s its own keys into
    it, so the clock/freshness fields survive a Stage-1 -> Stage-2 -> web sequence.
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
            print(f"[WARN] 无法解析既有 summary.json ({path}): {exc}；将覆盖写入本次字段")

    payload["clock"] = clock_ctx.stamp_fields()
    if probe_attempts is not None:
        payload["clock"]["probe_attempts"] = int(probe_attempts)
    if freshness_summary is not None:
        payload["freshness_summary"] = freshness_summary
    if extra:
        payload.update(extra)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    print(f"[MARKET CLOCK] 时钟/新鲜度审计字段已写入: {path}")
    return path
