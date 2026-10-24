"""Live-archive vs. backtest-engine parity harness -- `docs/development_plan.md` **P1.1**.

WHAT THIS TOOL IS FOR
=====================
A live Stage-2 run leaves an immutable archive (``holdings.json``,
``stage1_health_report.json``, ``live_trading_ticket.csv``, ``summary.json``).
This harness reads that archive, feeds the SAME books and the SAME prices into
:func:`common.allocation_backtester.run_allocation_backtest`, and then compares
the engine's resulting book against the book the live runner actually printed --
per symbol, in shares and in weights:

    |Delta w| < 1e-4   and   Delta Shares == 0

Both thresholds are module constants (:data:`MAX_ABS_DELTA_W`,
:data:`REQUIRED_DELTA_SHARES`), deliberately NOT command-line flags: a parity run
must not be able to loosen its own acceptance line. A run that cannot reproduce
the archive is reported as a divergence, with the source and magnitude of every
mismatch, instead of being quietly re-tuned until it passes.

WHY THE COMPARISON IS THE BOOK, NOT THE INSTRUCTION
===================================================
The archive carries two different weight numbers per row and they are NOT
interchangeable:

  * ``target_weight`` -- the STRATEGY instruction (a continuous fraction);
  * ``target_shares``  -- what the live runner actually decided to hold after
    lot rounding (100-share boards), purchasing-power scaling and the
    ``LIMIT_UP_LOCKED`` veto.

The acceptance metric here is the second one: ``archive_w := target_shares *
price / portfolio_value`` (the book the operator really owns) versus
``replay_w := actual_weights[as_of]`` (the book the ledger really owns, B1.1
definition). Comparing against ``target_weight`` instead would flag every
legitimate round-lot truncation as a parity failure -- a 100-share lot on a
1M book is ~0.5%, forty times the tolerance. The instruction-level number is
still reported, as the ``truncation_gap`` diagnostic column, so nothing is
hidden: ``truncation_gap = target_shares*price/PV - target_weight``.

WHAT THE REPLAY MODEL IS (disclosed, and why it is honest)
=========================================================
1. **Two sessions, flat marks.** The engine can only start from an all-cash
   book, so the archived book is seeded by a synthetic PREVIOUS session whose
   target weights are exactly the archived holdings and whose marks are the
   SAME prices the live run used (the ticket's own ``price`` column). Because
   the prices are identical on both sessions, ``equity`` stays at
   ``portfolio_value`` and the rebalance-day deltas the engine computes are
   comparable, in shares, with the live runner's. The seed is verified, not
   assumed: if the engine cannot reproduce the archived prior share count
   (e.g. an odd-lot holding the lot-granular ledger cannot open), the row is
   reported as ``seed_lot_indivisible`` rather than silently compared.
2. **Zero trading costs by default** (``--cost-model none``). The live runner
   sizes orders as ``delta_w * portfolio_value / price`` with no fee deduction
   (see ``scripts/run_live_strategy.py``, the ``raw_buy_shares`` /
   ``raw_sell_shares`` block), so a sizing comparison must not charge the
   engine's costs or the two books would differ by the fee drag on every run.
   ``--cost-model engine`` turns the engine's real cost model back on, which
   then shows up as a weight divergence -- an honest signal about what costs
   would have done to the archived book.
3. **No lifecycle layer.** The backtester has no Stage-2 position-lifecycle
   machine, so any archived row whose non-trade was caused by a Stage-2 gate
   (``COOLDOWN_BLOCKED``, ``TENURE_LOCKED``, ``MIN_ENTRY_THRESHOLD``,
   ``MARKET_CLOSED_FREEZE``, ``PENDING_ENTRY_CONFIRM``, ``LIMIT_UP_LOCKED``,
   ``CANCEL_PENDING``) can never be reproduced by the replay. Those rows are
   listed separately and are never counted as "aligned", even when the share
   count happens to match (see :data:`GATE_REASONS`).

WHAT THIS HARNESS CANNOT PROVE -- READ THIS BEFORE CITING A GREEN RUN
=====================================================================
It proves exactly one thing: *the archived book is what this engine produces
from the archived inputs*. It says NOTHING about whether the archived inputs or
the market data are correct, and it cannot detect an error that is present
identically on both sides of the comparison:

  * If the market data is contaminated (the `L2.8` forward-adjusted / QFQ
    price corruption is the canonical counter-example -- prices rewritten by a
    later dividend or split), the archive and the replay share the same
    poisoned prices and the harness reports a perfect, meaningless ALIGNED.
  * If the strategy's own signal was wrong (look-ahead, a stale bar reused as
    fresh), both sides replay the same wrong weights and align.
  * If a live gate silently did nothing, the archive records only its OUTCOME
    (no trade); the replay's inability to reproduce it is reported as
    ``gate_not_modelled``, not as proof of which side is right.
  * Point-in-time survivorship, corporate-action handling, and the Stage-1
    macro gate are all upstream of both sides and are taken as given.

So: parity is a necessary condition for trusting a live book, never a
sufficient one. A green runnable harness and a correct backtest are different
claims.

CLI / EXIT CODES
================
::

    python pipeline/parity_harness.py \
        --archive-dir <run dir | parent of run dirs | $QUANT_PARITY_ARCHIVE_DIR> \
        [--market auto|china|us|hk] [--market-data synthetic|<path.json>] \
        [--portfolio-value FLOAT] [--holdings-basis auto|shares|weights] \
        [--lot-size N] [--cost-model none|engine] [--json PATH|-]

Exit codes (asserted by ``pipeline/tests/test_parity_harness.py``):

  * ``0`` -- ALIGNED: no divergent row, ``max |Delta w| < 1e-4``, every
    ``Delta Shares == 0``. Non-replayable (gated / legacy) rows may still be
    listed -- they are reported, not hidden, and they do not by themselves
    claim a divergence;
  * ``1`` -- DIVERGED (at least one row the replay cannot reproduce) **or
    INCONCLUSIVE** (nothing comparable: every row is gated / unverifiable). An
    inconclusive run is not an alignment claim, so it does not exit 0;
  * ``2`` -- INVALID INPUT: unreadable / ambiguous archive, missing
    ``portfolio_value`` or ``as_of``, unsupported ``blocking_reason``-less
    archive that also has no prior book, bad ``--market-data`` file, etc.

Archive-root resolution never hardcodes a production path: it is
``--archive-dir``, else ``$QUANT_PARITY_ARCHIVE_DIR``, else an explicit error.
100% offline: no network, no DuckDB, no database or cache access -- market data
is either derived from the archive's own price column or read from an explicit
JSON file.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Same sys.path bootstrap the sibling pipeline tests use: `common/` and
# `backtester/` live at the repository root and have no venv of their own.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd  # noqa: E402

from common.allocation_backtester import CASH_PROXY_SYMBOLS, run_allocation_backtest  # noqa: E402
from common.position_lifecycle import coerce_blocking_reason  # noqa: E402

# --------------------------------------------------------------------------- #
# Acceptance line -- constants, NOT CLI flags (see module docstring).
# --------------------------------------------------------------------------- #
MAX_ABS_DELTA_W: float = 1e-4
REQUIRED_DELTA_SHARES: int = 0

#: Environment fallback for the archive root (`--archive-dir` wins).
ARCHIVE_DIR_ENV = "QUANT_PARITY_ARCHIVE_DIR"

#: Files that identify a run directory (superset of `web/livetrading/storage.is_run_dir`).
RUN_ARTIFACT_FILES = (
    "live_trading_ticket.csv",
    "live_trading_ticket.json",
    "summary.json",
    "holdings.json",
    "post_holdings.json",
    "stage1_health_report.json",
    "account_state.json",
)

#: Optional per-archive market data (`<run dir>/market_data.json`), used when
#: `--market-data` is not given, so a held symbol with no ticket row still has a
#: price. Format: `{symbol: {"YYYY-MM-DD": {"close": .., "volume": ..}}}`, or the
#: flat `{symbol: {"close": .., "volume": ..}}` form (applied to both sessions).
MARKET_DATA_FILE = "market_data.json"

#: Volume used for a synthetic bar. Must be > 0: the engine's B1.3 suspension
#: guard refuses BOTH directions on `Volume <= 0` / missing volume, so a
#: zero-volume fixture would be replayed as "suspended" and report a spurious
#: divergence in every row.
DEFAULT_VOLUME = 1_000_000.0

#: Stage-2 blocking labels. Every one of them is a decision the backtester has
#: NO model for, hence a row carrying one can never be reproduced by the replay
#: and must be listed separately (`non_replayable`) instead of "aligned".
GATE_REASONS = frozenset({
    "MARKET_CLOSED_FREEZE",
    "MIN_ENTRY_THRESHOLD",
    "PENDING_ENTRY_CONFIRM",
    "EXIT_CONFIRMING",
    "COOLDOWN_BLOCKED",
    "INERTIA",
    "TENURE_LOCKED",
    "LIMIT_UP_LOCKED",
})

#: Actions that mean "this row traded nothing" regardless of its target weight.
NO_TRADE_ACTIONS = frozenset({
    "HOLD", "HOLD_FILTERED", "NO_POSITION", "CANCEL_PENDING",
    "COOLDOWN_BLOCKED", "SWEEP_CASH", "RELEASE_CASH", "SKIP",
})

#: Row statuses (exactly one per symbol) and their meaning.
STATUS_ALIGNED = "aligned"                 # book reproduced, no gate in the archive row
STATUS_NON_REPLAYABLE = "non_replayable"   # gate/legacy row, book matched, NOT counted as aligned
STATUS_DIVERGENT = "divergent"             # book could not be reproduced -> unexplained

#: Divergence tags (why the replay could not reproduce a row).
TAG_GATE_NOT_MODELLED = "gate_not_modelled"
TAG_CANCEL_PENDING = "cancel_pending"
TAG_LEGACY_MISSING_BLOCKING_REASON = "legacy_missing_blocking_reason"
TAG_SILENT_DIVERGENCE = "silent_divergence"
TAG_SELL_ROUNDING_RULE = "sell_rounding_rule"
TAG_SEED_LOT_INDIVISIBLE = "seed_lot_indivisible"
TAG_MISSING_MARKET_PRICE = "missing_market_price"
TAG_ABSENT_TICKET_ROW = "absent_ticket_row"
TAG_UNVERIFIABLE_ROW = "unverifiable_row"

_MARKET_FLAGS = {
    "china": dict(china_trading=True),
    "us": dict(us_trading=True),
    "hk": dict(hk_trading=True),
    "none": dict(),
}

#: `--cost-model engine` reproduces the engine's documented defaults; `none` is
#: the sizing-comparable baseline (see the docstring, point 2).
_COST_MODELS = {
    "none": dict(commission_pct=0.0, slippage_pct=0.0, stamp_duty_pct=0.0,
                 sec_fee_pct=0.0, hk_stamp_duty_pct=0.0),
    "engine": dict(commission_pct=0.0005, slippage_pct=0.0005, stamp_duty_pct=0.0005,
                   sec_fee_pct=0.0000278, hk_stamp_duty_pct=0.001085),
}

_COST_MODEL_NOTE = {
    "none": (
        "zero costs: the live runner sizes orders as delta_w * portfolio_value / price "
        "with no fee deduction, so any engine cost would be a divergence by construction"
    ),
    "engine": (
        "engine costs ON: a divergence here is EXPECTED for buys/sells and measures the "
        "fee drag the archived book did not pay"
    ),
}


class ParityConfigError(Exception):
    """Unreadable / ambiguous archive or invalid CLI input -> process exit code 2."""


# --------------------------------------------------------------------------- #
# Archive model
# --------------------------------------------------------------------------- #
def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Lenient float read: '' / None / NaN / junk -> `default` (never raises)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return default if (isinstance(value, float) and not math.isfinite(value)) else float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in ("NAN", "NONE", "NULL", "-"):
        return default
    try:
        out = float(text)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    out = _as_float(value, None)
    return default if out is None else int(round(out))


@dataclass
class TicketRow:
    """One `live_trading_ticket.csv` row, normalized (never raises on junk)."""

    symbol: str
    action: str
    price: Optional[float]
    current_shares: Optional[int]
    target_shares: Optional[int]
    target_weight: Optional[float]
    current_weight: Optional[float]
    blocking_reason: str  # coerced: missing / unknown -> "NONE"
    blocking_reason_raw: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_gated(self) -> bool:
        return self.blocking_reason in GATE_REASONS

    @property
    def is_cancel_pending(self) -> bool:
        return self.action == "CANCEL_PENDING"

    @property
    def traded_nothing(self) -> bool:
        return self.action in NO_TRADE_ACTIONS or self.is_gated


@dataclass
class ArchiveSnapshot:
    """Everything the replay needs from one archived run directory."""

    run_dir: Path
    as_of: pd.Timestamp
    portfolio_value: float
    ticket: Dict[str, TicketRow]
    prior_shares: Dict[str, int]
    holdings_basis: str                 # "shares" | "weights" | "ticket_current_shares" | "none"
    health: Dict[str, Any]
    summary: Dict[str, Any]
    has_blocking_reason_column: bool
    market_bars: Dict[str, Dict[str, Dict[str, float]]]  # from `<run dir>/market_data.json`
    notes: List[str] = field(default_factory=list)


def resolve_archive_dir(
    value: Optional[str | os.PathLike],
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Resolve the archive ROOT: CLI `--archive-dir` > ``$QUANT_PARITY_ARCHIVE_DIR``.

    Never hardcodes a production path. Raises :class:`ParityConfigError` when
    neither is supplied, when the path does not exist, or when the tree holds
    more than one run directory (an ambiguous root would silently pick one).
    """
    env = os.environ if env is None else env
    raw = str(value).strip() if value not in (None, "") else ""
    source = "cli"
    if not raw:
        raw = str(env.get(ARCHIVE_DIR_ENV, "")).strip()
        source = "env"
    if not raw:
        raise ParityConfigError(
            "no archive root: pass --archive-dir or set "
            f"${ARCHIVE_DIR_ENV} (no production path is hardcoded on purpose)"
        )
    root = Path(raw).expanduser()
    if not root.exists():
        raise ParityConfigError(f"archive root does not exist ({source}): {root}")
    if not root.is_dir():
        raise ParityConfigError(f"archive root is not a directory ({source}): {root}")

    if _is_run_dir(root):
        return root
    runs = _find_run_dirs(root)
    if not runs:
        raise ParityConfigError(
            f"no run directory under {root}: expected one of {', '.join(RUN_ARTIFACT_FILES)}"
        )
    if len(runs) > 1:
        listing = ", ".join(str(p.relative_to(root)) for p in runs[:5])
        raise ParityConfigError(
            f"ambiguous archive root {root}: {len(runs)} run directories found ({listing}"
            f"{', ...' if len(runs) > 5 else ''}); point --archive-dir at ONE run directory"
        )
    return runs[0]


def _is_run_dir(path: Path) -> bool:
    try:
        names = set(os.listdir(path))
    except OSError:
        return False
    return any(name in names for name in RUN_ARTIFACT_FILES)


def _find_run_dirs(root: Path, max_depth: int = 5) -> List[Path]:
    """Run directories under `root`, bounded-depth (never walks an unbounded tree)."""
    found: List[Path] = []
    root_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and d != "__pycache__"]
        here = Path(current)
        if len(here.parts) - root_depth > max_depth:
            dirs[:] = []
            continue
        if any(name in files for name in RUN_ARTIFACT_FILES):
            found.append(here)
            dirs[:] = []  # a run dir's children are not run dirs (nested layouts)
    return found


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ParityConfigError(f"cannot parse {path.name} in {path.parent}: {exc}") from exc


def parse_ticket_csv(csv_path: Path) -> Tuple[List[Dict[str, Any]], bool]:
    """Parse `live_trading_ticket.csv` -> (raw rows, has_blocking_reason_column).

    Mirrors `web/livetrading/storage.parse_ticket_csv` (numeric coercion, no
    exception on junk) but is deliberately standalone: the web module imports a
    storage layer this harness must not depend on. PRE-`L2.24` archives have no
    `blocking_reason` column at all; that is detected here and surfaced as
    ``has_blocking_reason_column=False`` so those rows are reported as
    unverifiable (``legacy_missing_blocking_reason``) instead of silently
    reading as ``NONE`` -- which is exactly the inference `L2.24` forbids.
    """
    if not csv_path.exists():
        return [], False
    rows: List[Dict[str, Any]] = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            for row in reader:
                if row:
                    rows.append(dict(row))
    except OSError as exc:
        raise ParityConfigError(f"cannot read {csv_path}: {exc}") from exc
    return rows, "blocking_reason" in fieldnames


def _coerce_ticket_rows(raw_rows: Sequence[Dict[str, Any]]) -> Dict[str, TicketRow]:
    ticket: Dict[str, TicketRow] = {}
    for raw in raw_rows:
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        br_raw = raw.get("blocking_reason")
        ticket[symbol] = TicketRow(
            symbol=symbol,
            action=str(raw.get("action", "")).strip().upper(),
            price=_as_float(raw.get("price")),
            current_shares=_as_int(raw.get("current_shares")),
            target_shares=_as_int(raw.get("target_shares")),
            target_weight=_as_float(raw.get("target_weight")),
            current_weight=_as_float(raw.get("current_weight")),
            blocking_reason=coerce_blocking_reason(br_raw).value,
            blocking_reason_raw=None if br_raw is None else str(br_raw),
            raw=raw,
        )
    return ticket


def _resolve_prior_shares(
    holdings: Any,
    ticket: Dict[str, TicketRow],
    holdings_basis: str,
) -> Tuple[Dict[str, int], str]:
    """Prior share counts for the seed session, plus the basis actually used.

    Precedence (each documented in the report):

      1. the ticket's own ``current_shares`` for every symbol it has a row for --
         that IS the book the live run sized against, so it is authoritative;
      2. ``holdings.json`` for symbols with no ticket row, interpreted per
         ``--holdings-basis`` (``auto`` = weights when every value is <= 1.0 and
         the total <= 1.05, else shares; live's ``--current-holdings`` accepts
         both, so the ambiguity is real and must be resolved explicitly).

    Missing entirely (and no ticket) -> an empty prior book, recorded as
    ``holdings_basis="none"`` rather than invented.
    """
    prior: Dict[str, int] = {}
    for sym, row in ticket.items():
        if row.current_shares is not None:
            prior[sym] = int(row.current_shares)

    basis_used = "ticket_current_shares"
    extra = {str(k).strip().upper(): v for k, v in (holdings or {}).items()} if isinstance(holdings, dict) else {}
    if extra:
        basis = holdings_basis
        if basis == "auto":
            numeric = [_as_float(v) for v in extra.values()]
            numeric = [v for v in numeric if v is not None]
            total = sum(numeric)
            basis = "weights" if (numeric and all(v <= 1.0 for v in numeric) and total <= 1.05) else "shares"
        if basis not in ("shares", "weights"):
            raise ParityConfigError(f"unknown holdings basis {basis!r}")
        basis_used = basis if not prior else f"ticket_current_shares+{basis}"
        for sym, value in extra.items():
            if sym in prior:
                continue  # ticket wins (see docstring)
            if sym.upper() in ("CASH",) or sym.startswith("__CASH"):
                continue
            # A weights-based entry needs a price, which the caller resolves;
            # here we store the raw magnitude and let `replay_inputs` convert.
            num = _as_float(value)
            if num is None:
                continue
            prior[sym] = float(num) if basis == "weights" else int(round(num))
    elif not prior:
        basis_used = "none"
    return prior, basis_used


def load_archive(
    archive_dir: str | os.PathLike,
    *,
    portfolio_value: Optional[float] = None,
    holdings_basis: str = "auto",
    env: Optional[Mapping[str, str]] = None,
) -> ArchiveSnapshot:
    """Read one archived run directory into an :class:`ArchiveSnapshot`."""
    run_dir = resolve_archive_dir(archive_dir, env=env)
    notes: List[str] = []

    summary = _load_json(run_dir / "summary.json", default={}) or {}
    health = _load_json(run_dir / "stage1_health_report.json", default={}) or {}
    holdings = _load_json(run_dir / "holdings.json", default=None)
    if holdings is None:
        holdings = _load_json(run_dir / "current_holdings.json", default=None)
        if holdings is not None:
            notes.append("prior book read from current_holdings.json (no holdings.json)")
    if not isinstance(summary, dict):
        raise ParityConfigError(f"summary.json in {run_dir} is not an object")
    if not isinstance(health, dict):
        raise ParityConfigError(f"stage1_health_report.json in {run_dir} is not an object")

    raw_rows, has_br_column = parse_ticket_csv(run_dir / "live_trading_ticket.csv")
    if not raw_rows:
        json_rows = _load_json(run_dir / "live_trading_ticket.json", default=None)
        if isinstance(json_rows, list) and json_rows:
            raw_rows = [r for r in json_rows if isinstance(r, dict)]
            has_br_column = any("blocking_reason" in r for r in raw_rows)
            notes.append("ticket read from live_trading_ticket.json (no CSV)")
    if not raw_rows:
        raise ParityConfigError(
            f"{run_dir} has no live_trading_ticket.csv/.json rows -- nothing to compare"
        )
    ticket = _coerce_ticket_rows(raw_rows)
    if not ticket:
        raise ParityConfigError(f"{run_dir}: every ticket row lacks a `symbol`")

    # --- portfolio value -------------------------------------------------- #
    pv = _as_float(portfolio_value)
    if pv is None:
        pv = _as_float(summary.get("portfolio_value"))
    if pv is None:
        pv = _as_float(summary.get("initial_capital"))
    if pv is None or not (pv > 0):
        raise ParityConfigError(
            f"{run_dir}: no usable `portfolio_value` in summary.json -- pass --portfolio-value"
        )

    # --- as-of date -------------------------------------------------------- #
    as_of = None
    for key in ("date", "as_of", "as_of_date", "valuation_date"):
        value = summary.get(key)
        if value:
            as_of = pd.Timestamp(str(value)[:10])
            break
    if as_of is None:
        bar_dates = [str(r.raw.get("price_bar_date", "")).strip() for r in ticket.values()]
        bar_dates = [d for d in bar_dates if d and d.upper() not in ("NAN", "NONE")]
        if bar_dates:
            as_of = pd.Timestamp(max(bar_dates)[:10])
            notes.append("as_of taken from max(ticket.price_bar_date); summary.json has no date")
    if as_of is None:
        raise ParityConfigError(
            f"{run_dir}: cannot determine the as-of date (summary.json has no `date` and the "
            "ticket has no `price_bar_date` column)"
        )

    prior, basis_used = _resolve_prior_shares(holdings, ticket, holdings_basis)

    # --- optional per-archive market data --------------------------------- #
    market_bars: Dict[str, Dict[str, Dict[str, float]]] = {}
    md = run_dir / MARKET_DATA_FILE
    if md.exists():
        payload = _load_json(md, default={}) or {}
        if not isinstance(payload, dict):
            raise ParityConfigError(f"{md} is not a {{symbol: bars}} object")
        for sym, bars in payload.items():
            if str(sym).startswith("_"):
                continue  # documentation keys (`_comment`) are not symbols
            if not isinstance(bars, dict):
                raise ParityConfigError(f"{md}: {sym!r} is not a bar mapping")
            market_bars[str(sym).strip().upper()] = {
                str(k): dict(v) if isinstance(v, dict) else {"close": v}
                for k, v in bars.items()
            }
        notes.append(f"prior-book prices supplemented from {MARKET_DATA_FILE}")

    return ArchiveSnapshot(
        run_dir=run_dir,
        as_of=as_of,
        portfolio_value=float(pv),
        ticket=ticket,
        prior_shares=prior,
        holdings_basis=basis_used,
        health=health,
        summary=summary,
        has_blocking_reason_column=has_br_column,
        market_bars=market_bars,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def infer_market(symbols: Iterable[str]) -> str:
    """Venue from the symbol suffixes; `none` when they disagree (no rules applied)."""
    suffixes = set()
    for sym in symbols:
        text = str(sym).upper()
        if text.endswith((".SH", ".SZ", ".BJ")):
            suffixes.add("china")
        elif text.endswith((".HK",)):
            suffixes.add("hk")
        elif "." in text:
            suffixes.add("us")
    if not suffixes:
        return "us"
    return suffixes.pop() if len(suffixes) == 1 else "none"


def _default_lot(market: str) -> int:
    return 1 if market in ("us", "none") else 100


def _price_for(
    sym: str,
    as_of: pd.Timestamp,
    snapshot: ArchiveSnapshot,
    file_bars: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> Tuple[Optional[float], str]:
    """Price precedence: the archived ticket price, then market data. Never invented."""
    row = snapshot.ticket.get(sym)
    if row is not None and row.price is not None and row.price > 0:
        return float(row.price), "ticket"
    for source in (file_bars, snapshot.market_bars):
        bars = source.get(sym) or {}
        for key in (as_of.strftime("%Y-%m-%d"), "close", "default"):
            bar = bars.get(key)
            if isinstance(bar, dict):
                price = _as_float(bar.get("close"))
                if price is not None and price > 0:
                    return float(price), "market_data"
            else:
                price = _as_float(bar)
                if price is not None and price > 0:
                    return float(price), "market_data"
    return None, "none"


def _volume_for(
    sym: str,
    as_of: pd.Timestamp,
    file_bars: Mapping[str, Mapping[str, Mapping[str, float]]],
    snapshot: ArchiveSnapshot,
) -> float:
    for source in (file_bars, snapshot.market_bars):
        bars = source.get(sym) or {}
        for key in (as_of.strftime("%Y-%m-%d"), "close", "default"):
            bar = bars.get(key)
            if isinstance(bar, dict) and bar.get("volume") is not None:
                vol = _as_float(bar.get("volume"))
                if vol is not None:
                    return float(vol)
        flat_vol = bars.get("volume")  # flat form: {symbol: {"close": .., "volume": ..}}
        if not isinstance(flat_vol, dict):
            vol = _as_float(flat_vol)
            if vol is not None:
                return float(vol)
    return DEFAULT_VOLUME


@dataclass
class ReplayInputs:
    symbols: List[str]
    prices: Dict[str, float]
    price_sources: Dict[str, str]
    volumes: Dict[str, float]
    excluded: Dict[str, str]        # symbol -> reason it cannot be replayed
    seed_date: pd.Timestamp
    as_of: pd.Timestamp
    seed_weights: Dict[str, float]
    as_of_weights: Dict[str, float]
    prior_shares: Dict[str, int]
    market: str
    lot: int


def _load_market_data_file(path: str) -> Dict[str, Dict[str, Dict[str, float]]]:
    target = Path(path).expanduser()
    if not target.exists():
        raise ParityConfigError(f"--market-data file does not exist: {target}")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ParityConfigError(f"cannot parse --market-data {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParityConfigError(f"--market-data {target} must be a {{symbol: bars}} object")
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for sym, bars in payload.items():
        if str(sym).startswith("_"):
            continue
        if not isinstance(bars, dict):
            raise ParityConfigError(f"--market-data {target}: {sym!r} is not a bar mapping")
        out[str(sym).strip().upper()] = {
            str(k): (dict(v) if isinstance(v, dict) else {"close": v}) for k, v in bars.items()
        }
    return out


def build_replay_inputs(
    snapshot: ArchiveSnapshot,
    *,
    market: str = "auto",
    lot: Optional[int] = None,
    file_bars: Optional[Mapping[str, Mapping[str, Mapping[str, float]]]] = None,
) -> ReplayInputs:
    """Turn an archive snapshot into the exact books + bars the engine is fed."""
    file_bars = file_bars or {}
    as_of = snapshot.as_of
    universe_symbols = list(dict.fromkeys(
        list(snapshot.ticket.keys()) + list(snapshot.prior_shares.keys())
    ))
    if not universe_symbols:
        raise ParityConfigError(f"{snapshot.run_dir}: no symbols in ticket or holdings")

    resolved_market = infer_market(universe_symbols) if market == "auto" else market
    if resolved_market not in _MARKET_FLAGS:
        raise ParityConfigError(f"unknown market {market!r}; use auto|china|us|hk|none")
    lot_size = int(lot) if lot is not None else _default_lot(resolved_market)
    if lot_size < 0:
        raise ParityConfigError(f"--lot-size must be >= 0, got {lot_size}")

    prices: Dict[str, float] = {}
    price_sources: Dict[str, str] = {}
    volumes: Dict[str, float] = {}
    excluded: Dict[str, str] = {}
    for sym in universe_symbols:
        price, source = _price_for(sym, as_of, snapshot, file_bars)
        if price is None:
            excluded[sym] = TAG_MISSING_MARKET_PRICE
            continue
        prices[sym] = price
        price_sources[sym] = source
        volumes[sym] = _volume_for(sym, as_of, file_bars, snapshot)

    # A symbol with no price at all is EXCLUDED from the replay rather than
    # priced at an invented 1.0; `compare` then reports the row (as divergent
    # when it was a real position, advisory when it is a cash substitute).

    seed_date = pd.Timestamp(as_of) - pd.Timedelta(days=1)
    # Copy: `build_replay_inputs` converts weights-basis entries into share
    # counts, and the snapshot must stay a faithful record of the archive.
    prior: Dict[str, Any] = dict(snapshot.prior_shares)
    seed_weights: Dict[str, float] = {}
    for sym, price in prices.items():
        raw = prior.get(sym, 0)
        if isinstance(raw, float):  # weights-basis holdings entry
            shares = int(round(raw * snapshot.portfolio_value / price / max(lot_size, 1))) * max(lot_size, 1)
        else:
            shares = int(raw)
        if shares <= 0:
            continue
        seed_weights[sym] = shares * price / snapshot.portfolio_value
        prior[sym] = shares

    as_of_weights: Dict[str, float] = {}
    for sym in prices:
        row = snapshot.ticket.get(sym)
        if row is not None and row.target_weight is not None:
            as_of_weights[sym] = float(row.target_weight)
        else:
            # Explicit hold, NEVER NaN: inside a rebalance row a NaN cell means
            # "carry the previous weight" for the engine, which would silently
            # hold a stale book instead of the intended one (the cell-level
            # NaN-vs-0.0 contract in AGENTS.md).
            as_of_weights[sym] = float(seed_weights.get(sym, 0.0))

    return ReplayInputs(
        symbols=list(prices.keys()),
        prices=prices,
        price_sources=price_sources,
        volumes=volumes,
        excluded=excluded,
        seed_date=seed_date,
        as_of=pd.Timestamp(as_of),
        seed_weights=seed_weights,
        as_of_weights=as_of_weights,
        prior_shares={s: int(v) for s, v in prior.items() if s in prices},
        market=resolved_market,
        lot=lot_size,
    )


@dataclass
class ReplayOutcome:
    shares: Dict[str, int]
    weights: Dict[str, float]
    equity: float
    fills: List[Dict[str, Any]]
    seed_shares: Dict[str, int]
    result: Dict[str, Any]


def run_replay(
    snapshot: ArchiveSnapshot,
    inputs: ReplayInputs,
    *,
    cost_model: str = "none",
) -> ReplayOutcome:
    """Feed the archived books + identical marks through `run_allocation_backtest`."""
    if cost_model not in _COST_MODELS:
        raise ParityConfigError(f"unknown --cost-model {cost_model!r}; use none|engine")
    symbols = inputs.symbols
    if not symbols:
        raise ParityConfigError("no replayable symbol: every ticket/holding price is missing")

    index = pd.DatetimeIndex([inputs.seed_date, inputs.as_of])
    seed_row = {sym: inputs.seed_weights.get(sym, 0.0) for sym in symbols}
    as_of_row = {sym: inputs.as_of_weights.get(sym, 0.0) for sym in symbols}
    target_weights = pd.DataFrame([seed_row, as_of_row], index=index, columns=symbols)

    universe: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        price = float(inputs.prices[sym])
        frame = pd.DataFrame(
            {
                "Open": [price, price],
                "High": [price, price],
                "Low": [price, price],
                "Close": [price, price],
                "Volume": [float(inputs.volumes[sym]), float(inputs.volumes[sym])],
            },
            index=index,
        )
        universe[sym] = frame

    result = run_allocation_backtest(
        universe=universe,
        target_weights=target_weights,
        initial_capital=float(snapshot.portfolio_value),
        min_shares=int(inputs.lot),
        min_weight_change=0.0,
        **_MARKET_FLAGS[inputs.market],
        **_COST_MODELS[cost_model],
    )
    if result.get("equity_curve") is None or result["equity_curve"].empty:
        raise ParityConfigError(
            "the engine produced no equity curve: the replayed price index and the target weights "
            "do not overlap (or every symbol was untradable)"
        )

    equity_curve = result["equity_curve"]
    equity = float(equity_curve.loc[inputs.as_of, "equity"])
    asof_w = result["actual_weights"].loc[inputs.as_of]

    weights: Dict[str, float] = {}
    shares: Dict[str, int] = {}
    for sym in symbols:
        weight = float(asof_w.get(sym, 0.0))
        if not math.isfinite(weight):
            weight = 0.0
        weights[sym] = weight
        shares[sym] = int(round(weight * equity / inputs.prices[sym])) if equity > 0 else 0

    report = result.get("rebalance_report")
    fills: List[Dict[str, Any]] = []
    if isinstance(report, pd.DataFrame) and not report.empty:
        asof_str = inputs.as_of.strftime("%Y-%m-%d")
        asof_fills = report[report["date"] == asof_str]
        for _, row in asof_fills.iterrows():
            sym = str(row["symbol"])
            stated = _as_int(row["target_shares"])
            if stated is not None and sym in shares and stated != shares[sym]:
                # The ledger and the trade report must agree; if they do not,
                # the weight-derived count is the one the ledger path produced.
                shares[sym] = int(stated)
            fills.append(
                {
                    "symbol": sym,
                    "action": str(row["action"]),
                    "shares": _as_int(row["shares"]),
                    "prior_shares": _as_int(row["prior_shares"]),
                    "target_shares": _as_int(row["target_shares"]),
                    "price": _as_float(row["price"]),
                    "target_weight": _as_float(row["target_weight"]),
                    "portfolio_equity": _as_float(row["portfolio_equity"]),
                }
            )

    seed_shares: Dict[str, int] = {}
    if isinstance(report, pd.DataFrame) and not report.empty:
        seed_str = inputs.seed_date.strftime("%Y-%m-%d")
        for _, row in report[report["date"] == seed_str].iterrows():
            seed_shares[str(row["symbol"])] = _as_int(row["target_shares"], 0) or 0

    return ReplayOutcome(
        shares=shares,
        weights=weights,
        equity=equity,
        fills=fills,
        seed_shares=seed_shares,
        result=result,
    )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@dataclass
class RowVerdict:
    symbol: str
    status: str
    tag: Optional[str]
    severity: str                    # "ok" | "advisory" | "divergent"
    action: str
    blocking_reason: str
    archive_shares: Optional[int]
    replay_shares: Optional[int]
    delta_shares: Optional[int]
    archive_w: Optional[float]
    replay_w: Optional[float]
    delta_w: Optional[float]
    archive_target_weight: Optional[float]
    truncation_gap: Optional[float]
    price: Optional[float]
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "tag": self.tag,
            "severity": self.severity,
            "action": self.action,
            "blocking_reason": self.blocking_reason,
            "archive_shares": self.archive_shares,
            "replay_shares": self.replay_shares,
            "delta_shares": self.delta_shares,
            "archive_w": self.archive_w,
            "replay_w": self.replay_w,
            "delta_w": self.delta_w,
            "archive_target_weight": self.archive_target_weight,
            "truncation_gap": self.truncation_gap,
            "price": self.price,
            "explanation": self.explanation,
        }


@dataclass
class ParityReport:
    archive_dir: str
    as_of: str
    verdict: str
    rows: List[RowVerdict]
    unexplained_rows: List[RowVerdict]
    non_replayable_rows: List[RowVerdict]
    max_abs_delta_w: float
    max_abs_delta_shares: int
    divergent_max_abs_delta_w: float = 0.0
    divergent_max_abs_delta_shares: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict == "ALIGNED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "archive": {"root": self.archive_dir, "as_of": self.as_of},
            "thresholds": {
                "max_abs_delta_weight": MAX_ABS_DELTA_W,
                "delta_shares": REQUIRED_DELTA_SHARES,
            },
            "summary": {
                "rows": len(self.rows),
                "aligned": sum(1 for r in self.rows if r.status == STATUS_ALIGNED),
                "non_replayable": len(self.non_replayable_rows),
                "unexplained": len(self.unexplained_rows),
                "max_abs_delta_w": self.max_abs_delta_w,
                "max_abs_delta_shares": self.max_abs_delta_shares,
                "divergent_max_abs_delta_w": self.divergent_max_abs_delta_w,
                "divergent_max_abs_delta_shares": self.divergent_max_abs_delta_shares,
                "weight_basis": "realized = target_shares * price / portfolio_value",
            },
            "meta": self.meta,
            "rows": [r.to_dict() for r in self.rows],
            # Everything that is NOT part of the alignment claim, in one place:
            # divergent (no archive-side explanation) + non_replayable (the
            # archive explains it, the engine cannot model it).
            "not_aligned_rows": [
                r.to_dict() for r in self.rows if r.status != STATUS_ALIGNED
            ],
            "unexplained_rows": [r.to_dict() for r in self.unexplained_rows],
            "non_replayable_rows": [r.to_dict() for r in self.non_replayable_rows],
        }


def _row_explanation(tag: Optional[str], gate: str) -> str:
    if tag == TAG_GATE_NOT_MODELLED:
        return (
            f"archive gate {gate} suppressed the trade; the backtester has no position-lifecycle "
            "layer, so it executed the row's target weight instead -- not reproducible by replay"
        )
    if tag == TAG_CANCEL_PENDING:
        return "CANCEL_PENDING is a never-held record: the archive traded nothing by design"
    if tag == TAG_LEGACY_MISSING_BLOCKING_REASON:
        return (
            "pre-L2.24 archive: no blocking_reason column, so the non-trade cannot be attributed "
            "to a gate or to the replay -- unverifiable, not aligned"
        )
    if tag == TAG_SELL_ROUNDING_RULE:
        return (
            "SELL sizing rule mismatch: live rounds the sell lot UP (np.ceil) so it never leaves a "
            "sub-lot remainder, the engine floors it (// lot) -- a one-lot difference by construction"
        )
    if tag == TAG_SEED_LOT_INDIVISIBLE:
        return (
            "the archived prior share count is not a whole number of lots, so the lot-granular "
            "ledger cannot open the same book; the comparison starts from different holdings"
        )
    if tag == TAG_MISSING_MARKET_PRICE:
        return "no price for this symbol in the ticket or in market data -- it cannot be replayed"
    if tag == TAG_ABSENT_TICKET_ROW:
        return "the position exists in holdings.json but has no ticket row, so the archive has no instruction to compare"
    if tag == TAG_UNVERIFIABLE_ROW:
        return (
            "the archive row carries neither a target nor a prior share count, so nothing can be "
            "compared for this symbol -- reported instead of being counted as aligned"
        )
    return "book mismatch with no archive-side explanation available"


def compare(
    snapshot: ArchiveSnapshot,
    inputs: ReplayInputs,
    outcome: ReplayOutcome,
    *,
    cost_model: str = "none",
) -> ParityReport:
    """Per-symbol Delta Shares / Delta w, then the three status buckets."""
    rows: List[RowVerdict] = []
    symbols = list(dict.fromkeys(
        list(outcome.shares.keys()) + list(inputs.excluded.keys())
        + list(snapshot.prior_shares.keys()) + list(snapshot.ticket.keys())
    ))
    pv = snapshot.portfolio_value

    for sym in sorted(symbols):
        row = snapshot.ticket.get(sym)
        action = row.action if row is not None else "ABSENT"
        gate = row.blocking_reason if row is not None else "NONE"
        price = inputs.prices.get(sym)
        replay_shares = outcome.shares.get(sym) if sym not in inputs.excluded else None
        replay_w = outcome.weights.get(sym) if sym not in inputs.excluded else None

        if row is not None:
            if row.target_shares is not None:
                archive_shares = int(row.target_shares)
            elif row.current_shares is not None:
                archive_shares = int(row.current_shares)
            else:
                archive_shares = None  # row carries neither share count: unverifiable
        else:
            # No ticket row: the archive's book is the prior holdings, resolved
            # to shares by `build_replay_inputs` (weights-basis entries included).
            resolved = inputs.prior_shares.get(sym)
            if resolved is not None:
                archive_shares = int(resolved)
            else:
                raw = snapshot.prior_shares.get(sym, 0)
                archive_shares = None if isinstance(raw, float) else int(raw)

        archive_w = None
        if price is not None and archive_shares is not None and pv > 0:
            archive_w = archive_shares * price / pv

        delta_shares = (
            None if (replay_shares is None or archive_shares is None)
            else int(replay_shares) - int(archive_shares)
        )
        delta_w = (
            None if (replay_w is None or archive_w is None)
            else float(replay_w) - float(archive_w)
        )
        truncation_gap = None
        target_weight = row.target_weight if row is not None else None
        if archive_w is not None and target_weight is not None:
            truncation_gap = archive_w - target_weight

        # ---- status / tag / severity ------------------------------------- #
        tag: Optional[str] = None
        status = STATUS_ALIGNED
        severity = "ok"
        diverged = bool(delta_shares not in (None, 0)) or bool(
            delta_w is not None and abs(delta_w) >= MAX_ABS_DELTA_W
        )

        if sym in inputs.excluded:
            # A cash proxy / zero-share holding with no price is not a divergence:
            # the engine models idle cash as cash anyway. A real position that
            # cannot be priced IS one -- it silently drops out of the replay book.
            held = int(snapshot.prior_shares.get(sym, 0) or 0)
            is_proxy = sym.upper() in {s.upper() for s in CASH_PROXY_SYMBOLS}
            tag = inputs.excluded[sym]
            status = STATUS_NON_REPLAYABLE
            severity = "divergent" if (held > 0 and not is_proxy) else "advisory"
            if severity == "divergent":
                status = STATUS_DIVERGENT
        elif sym not in snapshot.ticket and diverged:
            status, severity, tag = STATUS_DIVERGENT, "divergent", TAG_ABSENT_TICKET_ROW
        elif row is not None and row.is_cancel_pending:
            tag = TAG_CANCEL_PENDING
            status = STATUS_NON_REPLAYABLE
            severity = "divergent" if diverged else "advisory"
            if diverged:
                status = STATUS_DIVERGENT
        elif row is not None and row.is_gated:
            tag = TAG_GATE_NOT_MODELLED
            status = STATUS_NON_REPLAYABLE
            severity = "advisory"
            if diverged:
                status, severity = STATUS_DIVERGENT, "divergent"
        elif not snapshot.has_blocking_reason_column:
            tag = TAG_LEGACY_MISSING_BLOCKING_REASON
            status = STATUS_NON_REPLAYABLE
            severity = "advisory"
            if diverged:
                status, severity = STATUS_DIVERGENT, "divergent"
        elif diverged:
            status, severity = STATUS_DIVERGENT, "divergent"
            # A one-lot SELL gap is the known `ceil` vs `//` rule difference;
            # label it precisely instead of calling it an anonymous mismatch.
            tag = TAG_SELL_ROUNDING_RULE if (
                row is not None and action == "SELL"
                and delta_shares is not None and abs(delta_shares) == max(inputs.lot, 1)
            ) else TAG_SILENT_DIVERGENCE

        # The seed must be able to open the archived book at all.
        if tag is None and not diverged and sym in inputs.prior_shares:
            seeded = outcome.seed_shares.get(sym)
            if seeded is not None and seeded != int(inputs.prior_shares[sym]):
                tag = TAG_SEED_LOT_INDIVISIBLE
                status = STATUS_NON_REPLAYABLE
                severity = "advisory"

        # Never claim "aligned" without a comparison: a row with no share count
        # and no implied weight is unverifiable, not equal.
        if status == STATUS_ALIGNED and (delta_shares is None or delta_w is None):
            status = STATUS_NON_REPLAYABLE
            severity = "advisory"
            tag = TAG_UNVERIFIABLE_ROW

        rows.append(
            RowVerdict(
                symbol=sym,
                status=status,
                tag=tag,
                severity=severity,
                action=action,
                blocking_reason=gate,
                archive_shares=archive_shares,
                replay_shares=replay_shares,
                delta_shares=delta_shares,
                archive_w=archive_w,
                replay_w=replay_w,
                delta_w=delta_w,
                archive_target_weight=target_weight,
                truncation_gap=truncation_gap,
                price=price,
                explanation=_row_explanation(tag, gate),
            )
        )

    aligned_rows = [r for r in rows if r.status == STATUS_ALIGNED]
    unexplained = [r for r in rows if r.status == STATUS_DIVERGENT]
    non_replayable = [r for r in rows if r.status == STATUS_NON_REPLAYABLE]

    max_dw = max(
        (abs(r.delta_w) for r in aligned_rows if r.delta_w is not None), default=0.0
    )
    max_ds = max(
        (abs(r.delta_shares) for r in aligned_rows if r.delta_shares is not None), default=0
    )
    div_dw = max(
        (abs(r.delta_w) for r in unexplained if r.delta_w is not None), default=0.0
    )
    div_ds = max(
        (abs(r.delta_shares) for r in unexplained if r.delta_shares is not None), default=0
    )
    diverged_any = bool(unexplained) or max_ds != REQUIRED_DELTA_SHARES or max_dw >= MAX_ABS_DELTA_W
    if diverged_any:
        verdict = "DIVERGED"
    elif not aligned_rows:
        # Nothing diverged, but nothing was comparable either: refusing to call
        # that "aligned" is the whole point of a harness (e.g. an archive whose
        # every row is gated, or whose rows carry no share counts at all).
        verdict = "INCONCLUSIVE"
    else:
        verdict = "ALIGNED"

    meta = {
        "replay": {
            "market": inputs.market,
            "lot_size": inputs.lot,
            "cost_model": cost_model,
            "cost_model_note": _COST_MODEL_NOTE[cost_model],
            "execution_price": "close",
            "seed_date": inputs.seed_date.strftime("%Y-%m-%d"),
            "as_of": inputs.as_of.strftime("%Y-%m-%d"),
            "portfolio_value": pv,
            "equity_at_as_of": outcome.equity,
            "replayed_symbols": inputs.symbols,
            "excluded_symbols": inputs.excluded,
            "price_sources": inputs.price_sources,
            "seed_book": inputs.seed_weights,
            "fills": outcome.fills,
            "seed_shares": outcome.seed_shares,
        },
        "archive": {
            "run_dir": str(snapshot.run_dir),
            "holdings_basis": snapshot.holdings_basis,
            "has_blocking_reason_column": snapshot.has_blocking_reason_column,
            "ticket_rows": len(snapshot.ticket),
            "prior_shares": {k: v for k, v in snapshot.prior_shares.items()},
            "health": _health_digest(snapshot.health),
            "notes": snapshot.notes,
        },
        "disclosures": [
            "compares the archived BOOK against a replay of the SAME books at the SAME marks; the "
            "market data and the strategy signal are identical on both sides, so neither is validated",
            "two flat sessions: the seed session opens the archived holdings at the ticket's own "
            "prices, so the replay measures sizing/rounding/accounting, not a return path",
            "the backtester has no Stage-2 position-lifecycle layer; gated rows are listed separately"
            " and never counted as aligned",
        ],
    }

    return ParityReport(
        archive_dir=str(snapshot.run_dir),
        as_of=inputs.as_of.strftime("%Y-%m-%d"),
        verdict=verdict,
        rows=rows,
        unexplained_rows=unexplained,
        non_replayable_rows=non_replayable,
        max_abs_delta_w=max_dw,
        max_abs_delta_shares=max_ds,
        divergent_max_abs_delta_w=div_dw,
        divergent_max_abs_delta_shares=div_ds,
        meta=meta,
    )


def _health_digest(health: Mapping[str, Any]) -> Dict[str, Any]:
    """Provenance only: the harness does NOT re-run Stage 1."""
    keys = (
        "date", "as_of", "as_of_date", "market_state", "market_open",
        "breadth", "market_breadth", "thrust", "momentum_thrust",
        "circuit_breaker", "status", "directive", "directive_summary",
    )
    digest = {k: health[k] for k in keys if k in health}
    if not digest and health:
        digest = {"keys": sorted(str(k) for k in health)[:12]}
    return digest


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def run_parity(
    archive_dir: str | os.PathLike,
    *,
    market: str = "auto",
    market_data: Optional[str] = None,
    portfolio_value: Optional[float] = None,
    holdings_basis: str = "auto",
    lot: Optional[int] = None,
    cost_model: str = "none",
    env: Optional[Mapping[str, str]] = None,
) -> ParityReport:
    """Load -> replay -> compare. Raises :class:`ParityConfigError` on bad input."""
    file_bars: Dict[str, Dict[str, Dict[str, float]]] = {}
    if market_data and market_data != "synthetic":
        file_bars = _load_market_data_file(market_data)
    snapshot = load_archive(
        archive_dir,
        portfolio_value=portfolio_value,
        holdings_basis=holdings_basis,
        env=env,
    )
    inputs = build_replay_inputs(
        snapshot, market=market, lot=lot, file_bars=file_bars
    )
    outcome = run_replay(snapshot, inputs, cost_model=cost_model)
    return compare(snapshot, inputs, outcome, cost_model=cost_model)


def format_report(report: ParityReport) -> str:
    """Human-readable table (JSON is the machine contract; see `--json`)."""
    lines: List[str] = []
    meta = report.meta.get("replay", {})
    lines.append("=" * 118)
    lines.append(f"Parity Harness (P1.1) -- archive {report.archive_dir}")
    lines.append(
        f"as_of={report.as_of}  market={meta.get('market')}  lot={meta.get('lot_size')}  "
        f"cost_model={meta.get('cost_model')}  equity={meta.get('equity_at_as_of'):,.2f}  "
        f"PV={meta.get('portfolio_value'):,.2f}"
    )
    lines.append("=" * 118)
    header = (
        f"{'symbol':<12}{'action':<15}{'gate':<22}{'arc_s':>10}{'rep_s':>10}{'dS':>7}"
        f"{'arc_w':>9}{'rep_w':>9}{'dW':>11}  {'status':<15}{'tag'}"
    )
    lines.append(header)
    lines.append("-" * 118)
    for row in report.rows:
        def _f(value: Any, spec: str) -> str:
            return "-" if value is None else format(value, spec)
        lines.append(
            f"{row.symbol:<12}{row.action:<15}{row.blocking_reason:<22}"
            f"{_f(row.archive_shares, '>10')}{_f(row.replay_shares, '>10')}"
            f"{_f(row.delta_shares, '>7')}{_f(row.archive_w, '>9.4f')}"
            f"{_f(row.replay_w, '>9.4f')}{_f(row.delta_w, '>11.2e')}"
            f"  {row.status:<15}{row.tag or ''}"
        )
    lines.append("-" * 118)
    lines.append(
        f"ALIGNED rows: max |dW| = {report.max_abs_delta_w:.3e} (limit {MAX_ABS_DELTA_W:g}), "
        f"max |dShares| = {report.max_abs_delta_shares} (limit {REQUIRED_DELTA_SHARES})"
    )
    if report.unexplained_rows:
        lines.append(
            f"DIVERGENT rows: max |dW| = {report.divergent_max_abs_delta_w:.3e}, "
            f"max |dShares| = {report.divergent_max_abs_delta_shares}"
        )
    if report.non_replayable_rows:
        lines.append("")
        lines.append(
            f"NON-REPLAYABLE rows ({len(report.non_replayable_rows)}) -- listed separately, "
            "never counted as aligned:"
        )
        for row in report.non_replayable_rows:
            lines.append(
                f"  - {row.symbol:<12}{row.action:<15}{row.blocking_reason:<22}{row.tag}: "
                f"{row.explanation}"
            )
    if report.unexplained_rows:
        lines.append("")
        lines.append(f"UNEXPLAINED rows ({len(report.unexplained_rows)}) -- the replay cannot account for these:")
        for row in report.unexplained_rows:
            lines.append(
                f"  ! {row.symbol:<12}dS={row.delta_shares} dW={row.delta_w} "
                f"[{row.tag}] {row.explanation}"
            )
    lines.append("")
    lines.append(f"VERDICT: {report.verdict}")
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parity_harness",
        description=(
            "Replay an archived live run through common/allocation_backtester.py and assert "
            "share-level parity (P1.1). Exit 0 = aligned, 1 = divergence, 2 = invalid input."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The archive root comes from --archive-dir or $QUANT_PARITY_ARCHIVE_DIR.\n"
            "Thresholds are fixed (|dw| < 1e-4, shares identical) and are not flags: a parity\n"
            "run must not be able to relax its own acceptance line."
        ),
    )
    parser.add_argument(
        "--archive-dir", default=None,
        help=f"run directory, or a parent holding exactly one (env: ${ARCHIVE_DIR_ENV})",
    )
    parser.add_argument(
        "--market", default="auto", choices=["auto", "china", "us", "hk", "none"],
        help="trading rules (default: infer from symbol suffixes)",
    )
    parser.add_argument(
        "--market-data", default=None,
        help=f"'synthetic' (ticket prices) or a JSON file; '<run dir>/{MARKET_DATA_FILE}' is auto-used",
    )
    parser.add_argument("--portfolio-value", type=float, default=None,
                        help="override summary.json's portfolio_value")
    parser.add_argument("--holdings-basis", default="auto", choices=["auto", "shares", "weights"],
                        help="how to read holdings.json for symbols with no ticket row")
    parser.add_argument("--lot-size", type=int, default=None,
                        help="override the lot size (default: 100 asia / 1 us)")
    parser.add_argument("--cost-model", default="none", choices=["none", "engine"],
                        help="none (default, sizing-comparable) or the engine's own costs")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="write the JSON report here ('-' = stdout)")
    parser.add_argument("--quiet", action="store_true", help="suppress the human-readable table")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        report = run_parity(
            args.archive_dir,
            market=args.market,
            market_data=args.market_data,
            portfolio_value=args.portfolio_value,
            holdings_basis=args.holdings_basis,
            lot=args.lot_size,
            cost_model=args.cost_model,
        )
    except ParityConfigError as exc:
        print(f"[parity] INVALID INPUT: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=_json_default)
    if args.json_out == "-":
        print(payload)
    elif args.json_out:
        target = Path(args.json_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload + "\n", encoding="utf-8")
    if not args.quiet and args.json_out != "-":
        print(format_report(report))
    elif args.json_out and args.json_out != "-" and not args.quiet:
        print(f"[parity] JSON report written to {args.json_out}", file=sys.stderr)
    return 0 if report.ok else 1


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return str(value)
    if hasattr(value, "item"):
        return value.item()
    return str(value)


if __name__ == "__main__":  # pragma: no cover - exercised through `main()`
    sys.exit(main())
