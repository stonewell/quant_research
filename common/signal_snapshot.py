"""Signal snapshot contract for live cross-sectional scoring (task `AT3`).

WHY THIS MODULE EXISTS
======================
`AT3` (`docs/development_plan.md` §2.24) asks for two things: a *snapshot* of the
strategy's daily cross-sectional signal, and an *advisory* monitor of the live
Rank IC / ICIR / half-life decay. This file is the snapshot side only; the
consumer is `common/ic_decay_monitor.py`.

THE DATA GAP THAT BLOCKS THE LIVE WRITE PATH (read before "wiring it up")
------------------------------------------------------------------------
There is **no cross-sectional score in the live chain today**. The only
per-symbol, per-strategy numbers Stage 2 writes are
`detail_reason.dimension_signals`, and those are **sub-strategy weights inside
the already-held book**, not a ranking of the candidate universe:

* they are aggregated from the blend's sub-strategy weights
  (`scripts/run_live_strategy.py:2735-2737`, `sub_strategy_weights_by_symbol[...]
  = round(float(val), 4)` -- 4dp at the aggregation site already),
* they are read back only for symbols that are being ticketed
  (`scripts/run_live_strategy.py:3345-3348`,
  `dim_signals = dict(sub_strategy_weights_by_symbol[sym])`, else a single
  `{strategy_key: round(tgt_w, 4)}` fallback),
* and written into `detail_reason["dimension_signals"]`
  (`scripts/run_live_strategy.py:3410`).

(The plan row cites `:2399-2413` / `:2986-2990` / `:3051` for the same three
sites; the line numbers drifted, the code shape did not.)

So they exist only for *selected* names, are already portfolio weights (a
selection-truncated object by construction), and are rounded to 4dp. Feeding
them to an IC calculation would measure "do the weights of the names I already
own rank their own forward returns", which is circular and structurally
selection-biased. **This module therefore ships as a pure contract with no
producer in `scripts/`**, and `scripts/run_live_strategy.py` is deliberately
left untouched: the Stage-2 write-path hookup is deferred until a real
cross-sectional score source exists (see `docs/development_plan.md` `AT3`
condition 1 and `W1`).

THE ROW CONTRACT (frozen)
------------------------
Exactly five columns, in this order::

    (date, strategy, symbol, score, universe_id)

* `date` -- the **session date** the score was computed for. A midnight-
  normalised `pandas.Timestamp`; a tz-aware or intraday value is rejected
  because "which session does 14:15 belong to" must never be guessed.
* `strategy` -- which strategy/leg produced the score (a blend sleeve, a timing
  template, ...). One column, so per-leg separation is a groupby, not a naming
  convention.
* `symbol` -- the candidate.
* `score` -- the raw cross-sectional prediction. Finite; **never** rounded to a
  display precision (the live path's `round(..., 4)` is an audit convenience and
  must not be copied here), and **never** NaN or 0-filled.
* `universe_id` -- which candidate pool this row belongs to. Required, not
  optional: two pools may legitimately hold the same ticker with different
  scores, and collapsing them would silently merge two cross-sections.

ALL CANDIDATES, INCLUDING UNSELECTED ONES (the hard rule)
---------------------------------------------------------
A snapshot that contains only the symbols that were bought is **structurally
truncated**: the resulting cross-section is the winner set, so any IC computed
from it is biased upward by construction. Callers therefore declare the
candidate universe (`candidates=`), and the validator refuses a snapshot whose
symbols are a strict subset of it (`SelectionTruncatedSnapshotError`). The
mirror-image error -- a row for a symbol outside the declared pool -- is also
refused; no symbol may be silently added to or dropped from a cross-section.

What this module does NOT do
----------------------------
* No IC math. That lives in `common/factor_analyzer.py` and is consumed by
  `common/ic_decay_monitor.py`.
* No network, no `scripts/`, no `web/`, no `subprocess`, no `requests`.
* No advisory-behaviour leakage: nothing here gates, reweights or orders. The
  only IO is the file write a caller explicitly asks for by passing `path=`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

#: Frozen row schema. Order is part of the contract; `to_frame()` emits it as-is.
SNAPSHOT_COLUMNS: Tuple[str, ...] = ("date", "strategy", "symbol", "score", "universe_id")

#: Envelope tag written by `serialize_snapshot`; a reader that sees anything else
#: must refuse the payload rather than guess at its meaning.
SNAPSHOT_SCHEMA = "signal_snapshot.v1"

#: Cross-sectional floor (shared with the monitor, single source of truth).
#: A single-day cross-section below this many symbols cannot support a Rank IC
#: estimate; the monitor reports `INSUFFICIENT_HISTORY` for it and never fills 0.
MIN_CROSS_SECTION_ASSETS = 15

#: Valid evaluation days needed before any conclusion may be drawn.
MIN_VALID_EVAL_DAYS = 10

#: Allowed values of `SignalSnapshot.information_set`. See the module docstring of
#: `common/ic_decay_monitor.py` for why only `close` may be scored against
#: close-to-close returns.
CLOSE_INFORMATION_SET = "close"
INTRADAY_INFORMATION_SET = "intraday"
INFORMATION_SETS: Tuple[str, str] = (CLOSE_INFORMATION_SET, INTRADAY_INFORMATION_SET)


class SnapshotError(ValueError):
    """Base class for every snapshot contract violation."""


class SnapshotSchemaError(SnapshotError):
    """The frame/row does not satisfy the frozen `(date, strategy, symbol, score, universe_id)` contract."""


class SelectionTruncatedSnapshotError(SnapshotError):
    """The snapshot's symbols are a strict subset of the declared candidate universe.

    This is the error `AT3` condition 2 is about: a snapshot of only the selected
    names produces a cross-section that is truncated by the selection itself, so
    every downstream IC/ICIR number would be biased. Recording the snapshot is
    cheap; recomputing the past is impossible (qfq revisions, config/dump drift),
    so the rejection happens at write time, not at analysis time.
    """


# ---------------------------------------------------------------------------
# coercion helpers
# ---------------------------------------------------------------------------
def _coerce_session_date(value: Any) -> pd.Timestamp:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotSchemaError(f"date is not a valid timestamp: {value!r}") from exc
    if ts is pd.NaT or pd.isna(ts):
        raise SnapshotSchemaError(f"date is missing (NaT): {value!r}")
    if ts.tzinfo is not None:
        raise SnapshotSchemaError(
            f"date must be tz-naive session date, got tz-aware {value!r}; "
            "convert with an explicit market clock before recording"
        )
    if ts != ts.normalize():
        raise SnapshotSchemaError(
            f"date must be a midnight-normalised session date, got {value!r}; "
            "an intraday timestamp makes the session ambiguous"
        )
    return ts


def _coerce_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SnapshotSchemaError(f"{field} must be a string, got {type(value).__name__}: {value!r}")
    if value != value.strip():
        raise SnapshotSchemaError(
            f"{field} has leading/trailing whitespace: {value!r}; "
            "a padded identifier silently creates a second entity"
        )
    if not value:
        raise SnapshotSchemaError(f"{field} must not be empty")
    return value


def _coerce_score(value: Any, symbol: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise SnapshotSchemaError(
            f"score for {symbol!r} must be a real number, got {type(value).__name__}: {value!r}"
        )
    score = float(value)
    if not math.isfinite(score):
        raise SnapshotSchemaError(
            f"score for {symbol!r} is not finite ({value!r}); "
            "a missing score must not be recorded as NaN/0 -- fix the score source "
            "instead, because NaN and 0.0 are indistinguishable once IC is computed"
        )
    return score


def _coerce_information_set(value: Any) -> str:
    text = _coerce_token(value, "information_set")
    if text not in INFORMATION_SETS:
        raise SnapshotSchemaError(
            f"information_set must be one of {INFORMATION_SETS}, got {value!r}"
        )
    return text


def _coerce_optional_timestamp(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotSchemaError(f"recorded_at is not a valid timestamp: {value!r}") from exc
    if ts is pd.NaT or pd.isna(ts):
        raise SnapshotSchemaError(f"recorded_at is missing (NaT): {value!r}")
    if ts.tzinfo is not None:
        raise SnapshotSchemaError(
            f"recorded_at must be tz-naive, got tz-aware {value!r}"
        )
    return ts


def _as_symbol_tuple(symbols: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(symbols, str):
        raise SnapshotSchemaError(
            "candidates must be an iterable of symbols, not a bare string"
        )
    out: List[str] = []
    seen = set()
    for sym in symbols:
        token = _coerce_token(sym, "candidate symbol")
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


# ---------------------------------------------------------------------------
# frame-level validation
# ---------------------------------------------------------------------------
def _expected_by_group(
    expected_candidates: Union[
        None, Mapping[Any, Iterable[str]], Iterable[str]
    ],
    groups: Sequence[Tuple[str, str]],
) -> Dict[Tuple[str, str], Tuple[str, ...]]:
    """Normalise `expected_candidates` to `{(universe_id, strategy): symbols}`."""
    if expected_candidates is None:
        return {}
    if isinstance(expected_candidates, Mapping):
        out: Dict[Tuple[str, str], Tuple[str, ...]] = {}
        for key, symbols in expected_candidates.items():
            if isinstance(key, (tuple, list)) and len(key) == 2:
                universe_id, strategy = str(key[0]), str(key[1])
            else:
                raise SnapshotSchemaError(
                    "mapping-form expected_candidates must be keyed by "
                    f"(universe_id, strategy), got key {key!r}"
                )
            out[(universe_id, strategy)] = _as_symbol_tuple(symbols)
        missing_groups = [g for g in groups if g not in out]
        if missing_groups:
            raise SnapshotSchemaError(
                "expected_candidates is missing an entry for group(s) "
                f"{missing_groups}; every (universe_id, strategy) in the frame must "
                "declare its candidate universe or the completeness check is skipped "
                "for it without saying so"
            )
        return out
    flat = _as_symbol_tuple(expected_candidates)
    return {g: flat for g in groups}


def validate_snapshot(
    frame: pd.DataFrame,
    *,
    expected_candidates: Union[
        None, Mapping[Any, Iterable[str]], Iterable[str]
    ] = None,
) -> pd.DataFrame:
    """Validate a snapshot frame against the frozen row contract.

    Parameters
    ----------
    frame : pandas.DataFrame
        Must carry exactly the columns in `SNAPSHOT_COLUMNS` (no extra, no
        missing). Empty frames are rejected: "no rows" is not "an empty
        cross-section", and a caller that cannot produce rows must not write a
        file that later reads as an all-zero IC day.
    expected_candidates : optional
        The declared candidate universe -- an iterable of symbols (applied to
        every `(universe_id, strategy)` group) or a mapping
        `{(universe_id, strategy): symbols}`. When supplied, a snapshot whose
        symbols are a strict subset raises `SelectionTruncatedSnapshotError`,
        and a symbol outside the declared pool raises `SnapshotSchemaError`.

    Returns
    -------
    pandas.DataFrame
        A normalised copy (`date` as midnight `Timestamp`, tokens as `str`,
        `score` as `float`), index reset.

    Raises
    ------
    SnapshotSchemaError, SelectionTruncatedSnapshotError
        See the message; every message names the offending values.
    """
    if not isinstance(frame, pd.DataFrame):
        raise SnapshotSchemaError(
            f"snapshot must be a pandas.DataFrame, got {type(frame).__name__}"
        )

    missing = [c for c in SNAPSHOT_COLUMNS if c not in frame.columns]
    extra = [str(c) for c in frame.columns if c not in SNAPSHOT_COLUMNS]
    if missing or extra:
        raise SnapshotSchemaError(
            f"snapshot columns must be exactly {list(SNAPSHOT_COLUMNS)}; "
            f"missing={missing} unexpected={extra}"
        )
    if len(frame) == 0:
        raise SnapshotSchemaError(
            "snapshot has no rows; an absent cross-section is not a zero-IC "
            "cross-section and must not be written"
        )

    dates = [_coerce_session_date(v) for v in frame["date"].tolist()]
    strategies = [_coerce_token(v, "strategy") for v in frame["strategy"].tolist()]
    symbols = [_coerce_token(v, "symbol") for v in frame["symbol"].tolist()]
    scores = [_coerce_score(v, s) for v, s in zip(frame["score"].tolist(), symbols)]
    universe_ids = [_coerce_token(v, "universe_id") for v in frame["universe_id"].tolist()]

    out = pd.DataFrame(
        {
            "date": dates,
            "strategy": strategies,
            "symbol": symbols,
            "score": scores,
            "universe_id": universe_ids,
        }
    )

    dup_mask = out.duplicated(subset=["date", "strategy", "symbol", "universe_id"], keep=False)
    if bool(dup_mask.any()):
        offenders = (
            out.loc[dup_mask, ["date", "strategy", "symbol", "universe_id"]]
            .drop_duplicates()
            .head(5)
            .astype(str)
            .to_dict(orient="records")
        )
        raise SnapshotSchemaError(
            f"snapshot has duplicate (date, strategy, symbol, universe_id) rows: "
            f"{offenders}; duplicates are two scores for one cell, which makes the "
            "cross-section ordering ambiguous"
        )

    groups: List[Tuple[str, str]] = []
    for universe_id, strategy in out[["universe_id", "strategy"]].drop_duplicates().itertuples(
        index=False, name=None
    ):
        groups.append((universe_id, strategy))

    expected = _expected_by_group(expected_candidates, groups)
    for (universe_id, strategy), declared in expected.items():
        mask = (out["universe_id"] == universe_id) & (out["strategy"] == strategy)
        present = set(out.loc[mask, "symbol"])
        declared_set = set(declared)
        absent = sorted(declared_set - present)
        if absent:
            raise SelectionTruncatedSnapshotError(
                f"snapshot for universe_id={universe_id!r} strategy={strategy!r} "
                f"omits {len(absent)} of {len(declared_set)} declared candidates "
                f"(e.g. {absent[:5]}). A snapshot that only holds selected names is "
                "selection-truncated: the cross-section is the winner set and any IC "
                "computed from it is biased upward by construction."
            )
        unknown = sorted(present - declared_set)
        if unknown:
            raise SnapshotSchemaError(
                f"snapshot for universe_id={universe_id!r} strategy={strategy!r} "
                f"contains symbols outside the declared candidate universe: "
                f"{unknown[:5]}; a symbol may not be silently added to a cross-section"
            )

    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# the snapshot value object
# ---------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class SignalSnapshot:
    """One run's cross-sectional scores for one `(date, strategy, universe_id)`.

    `scores` maps every candidate symbol to its raw score. `candidates` is the
    declared universe; when it is empty the completeness rule cannot be checked
    and `is_complete` is False -- callers that want the `AT3` guarantee must
    declare it.
    """

    date: pd.Timestamp
    strategy: str
    universe_id: str
    scores: Mapping[str, float]
    candidates: Tuple[str, ...] = ()
    information_set: str = CLOSE_INFORMATION_SET
    recorded_at: Optional[pd.Timestamp] = None
    source: str = ""

    def __post_init__(self) -> None:
        date = _coerce_session_date(self.date)
        strategy = _coerce_token(self.strategy, "strategy")
        universe_id = _coerce_token(self.universe_id, "universe_id")
        information_set = _coerce_information_set(self.information_set)
        recorded_at = _coerce_optional_timestamp(self.recorded_at)

        if not isinstance(self.scores, Mapping):
            raise SnapshotSchemaError(
                f"scores must be a mapping of symbol -> score, got {type(self.scores).__name__}"
            )
        if len(self.scores) == 0:
            raise SnapshotSchemaError(
                "scores is empty; a snapshot with no candidates is not a snapshot"
            )
        scores: Dict[str, float] = {}
        for symbol, value in self.scores.items():
            token = _coerce_token(symbol, "symbol")
            scores[token] = _coerce_score(value, token)

        candidates = _as_symbol_tuple(self.candidates) if self.candidates else ()
        if candidates:
            declared = set(candidates)
            absent = sorted(declared - set(scores))
            if absent:
                raise SelectionTruncatedSnapshotError(
                    f"snapshot for universe_id={universe_id!r} strategy={strategy!r} on "
                    f"{date.date()} omits {len(absent)} of {len(declared)} declared "
                    f"candidates (e.g. {absent[:5]}). A snapshot that only holds "
                    "selected names is selection-truncated and is rejected."
                )
            unknown = sorted(set(scores) - declared)
            if unknown:
                raise SnapshotSchemaError(
                    f"snapshot for universe_id={universe_id!r} strategy={strategy!r} "
                    f"contains symbols outside the declared candidate universe: {unknown[:5]}"
                )

        if recorded_at is not None and recorded_at < date:
            raise SnapshotSchemaError(
                f"recorded_at ({recorded_at}) precedes the evaluation date ({date.date()}); "
                "a snapshot cannot be written before the session it describes"
            )

        if not isinstance(self.source, str):
            raise SnapshotSchemaError(
                f"source must be a string, got {type(self.source).__name__}"
            )

        object.__setattr__(self, "date", date)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "universe_id", universe_id)
        object.__setattr__(self, "information_set", information_set)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "scores", MappingProxyType(dict(sorted(scores.items()))))
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "source", self.source)

    # -- views -------------------------------------------------------------
    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(self.scores.keys())

    @property
    def n_symbols(self) -> int:
        return len(self.scores)

    @property
    def is_complete(self) -> bool:
        """True only when a candidate universe was declared and fully covered."""
        return bool(self.candidates) and set(self.candidates) == set(self.scores)

    @property
    def group(self) -> Tuple[str, str]:
        return (self.universe_id, self.strategy)

    def to_frame(self) -> pd.DataFrame:
        """The row-contract view: exactly `SNAPSHOT_COLUMNS`, one row per candidate."""
        return pd.DataFrame(
            {
                "date": [self.date] * self.n_symbols,
                "strategy": [self.strategy] * self.n_symbols,
                "symbol": list(self.scores.keys()),
                "score": [self.scores[s] for s in self.scores],
                "universe_id": [self.universe_id] * self.n_symbols,
            },
            columns=list(SNAPSHOT_COLUMNS),
        )

    def score_series(self) -> pd.Series:
        return pd.Series(dict(self.scores), dtype=float, name=f"{self.universe_id}:{self.strategy}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SignalSnapshot):
            return NotImplemented
        return (
            self.date == other.date
            and self.strategy == other.strategy
            and self.universe_id == other.universe_id
            and dict(self.scores) == dict(other.scores)
            and tuple(self.candidates) == tuple(other.candidates)
            and self.information_set == other.information_set
            and self.recorded_at == other.recorded_at
            and self.source == other.source
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.date,
                self.strategy,
                self.universe_id,
                tuple(self.scores.items()),
                tuple(self.candidates),
                self.information_set,
                self.recorded_at,
                self.source,
            )
        )

    @classmethod
    def from_frame(
        cls,
        frame: pd.DataFrame,
        *,
        candidates: Optional[Iterable[str]] = None,
        information_set: str = CLOSE_INFORMATION_SET,
        recorded_at: Any = None,
        source: str = "",
    ) -> "SignalSnapshot":
        """Build a single-run snapshot from a 5-column frame.

        The frame must describe exactly one `(date, strategy, universe_id)`; a
        multi-date panel has to be split by the caller, because merging runs into
        one object would hide which run was truncated.
        """
        if candidates is None and "candidates" in getattr(frame, "attrs", {}):
            candidates = frame.attrs["candidates"]
        groups = frame[["date", "strategy", "universe_id"]].drop_duplicates()
        if len(groups) != 1:
            raise SnapshotSchemaError(
                "SignalSnapshot describes exactly one (date, strategy, universe_id) run; "
                f"frame carries {len(groups)}: "
                f"{groups.head(5).astype(str).to_dict(orient='records')}"
            )
        validated = validate_snapshot(frame, expected_candidates=candidates)
        row = validated.iloc[0]
        return cls(
            date=row["date"],
            strategy=row["strategy"],
            universe_id=row["universe_id"],
            scores=dict(zip(validated["symbol"], validated["score"])),
            candidates=_as_symbol_tuple(candidates) if candidates else (),
            information_set=information_set,
            recorded_at=recorded_at,
            source=source,
        )


# ---------------------------------------------------------------------------
# serialisation (JSON only; no parquet, no side effects)
# ---------------------------------------------------------------------------
def serialize_snapshot(snapshot: SignalSnapshot) -> str:
    """Serialise one snapshot to a JSON string (deterministic key order)."""
    if not isinstance(snapshot, SignalSnapshot):
        raise SnapshotError(
            f"serialize_snapshot expects a SignalSnapshot, got {type(snapshot).__name__}"
        )
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "date": snapshot.date.strftime("%Y-%m-%d"),
        "strategy": snapshot.strategy,
        "universe_id": snapshot.universe_id,
        "information_set": snapshot.information_set,
        "recorded_at": None if snapshot.recorded_at is None else snapshot.recorded_at.isoformat(),
        "candidates": list(snapshot.candidates),
        "source": snapshot.source,
        "rows": [
            {
                "date": snapshot.date.strftime("%Y-%m-%d"),
                "strategy": snapshot.strategy,
                "symbol": symbol,
                "score": float(snapshot.scores[symbol]),
                "universe_id": snapshot.universe_id,
            }
            for symbol in snapshot.symbols
        ],
    }
    # allow_nan=False: a non-finite score must fail here too, not become `NaN`
    # in JSON, which some parsers accept and others reject.
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)


_ENVELOPE_KEYS = {
    "schema",
    "date",
    "strategy",
    "universe_id",
    "information_set",
    "recorded_at",
    "candidates",
    "source",
    "rows",
}


def deserialize_snapshot(text: str) -> SignalSnapshot:
    """Inverse of `serialize_snapshot`; strict about the envelope and the row keys."""
    if not isinstance(text, str):
        raise SnapshotSchemaError(
            f"deserialize_snapshot expects a str, got {type(text).__name__}"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotSchemaError(f"snapshot payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SnapshotSchemaError("snapshot payload must be a JSON object")

    unknown = sorted(set(payload) - _ENVELOPE_KEYS)
    if unknown:
        raise SnapshotSchemaError(
            f"snapshot payload has unknown envelope keys {unknown}; refusing to guess"
        )
    missing = sorted(_ENVELOPE_KEYS - set(payload))
    if missing:
        raise SnapshotSchemaError(f"snapshot payload is missing envelope keys {missing}")
    if payload["schema"] != SNAPSHOT_SCHEMA:
        raise SnapshotSchemaError(
            f"unsupported snapshot schema {payload['schema']!r}; expected {SNAPSHOT_SCHEMA!r}"
        )

    rows = payload["rows"]
    if not isinstance(rows, list) or not rows:
        raise SnapshotSchemaError("snapshot payload 'rows' must be a non-empty list")
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotSchemaError("snapshot rows must be JSON objects")
        if set(row) != set(SNAPSHOT_COLUMNS):
            raise SnapshotSchemaError(
                f"snapshot row keys must be exactly {list(SNAPSHOT_COLUMNS)}, got {sorted(row)}"
            )

    frame = pd.DataFrame(rows, columns=list(SNAPSHOT_COLUMNS))
    candidates = payload["candidates"]
    if not isinstance(candidates, list):
        raise SnapshotSchemaError("snapshot payload 'candidates' must be a list")
    validated = validate_snapshot(frame, expected_candidates=candidates or None)
    return SignalSnapshot(
        date=payload["date"],
        strategy=payload["strategy"],
        universe_id=payload["universe_id"],
        scores=dict(zip(validated["symbol"], validated["score"])),
        candidates=tuple(candidates),
        information_set=payload["information_set"],
        recorded_at=payload["recorded_at"],
        source=payload["source"] or "",
    )


def write_snapshot(snapshot: SignalSnapshot, path: Union[str, Path], *, append: bool = False) -> Path:
    """Write one snapshot as JSON (`append=False`) or one JSON line (`append=True`).

    Returns the path actually written. This is the **only** IO in the module, and
    it happens only when a caller passes a path.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = serialize_snapshot(snapshot)
    if append:
        # one JSON object per line, so a history file stays append-only: rewriting
        # past snapshots is exactly the "backfill the last 10 days" move `AT3`
        # condition 5 forbids.
        with target.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        target.write_text(text + "\n", encoding="utf-8")
    return target


def read_snapshots(path: Union[str, Path]) -> Tuple[SignalSnapshot, ...]:
    """Read a snapshot file: a single JSON object or a JSON-lines history."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        raise SnapshotSchemaError(f"snapshot file {target} is empty")
    try:
        return (deserialize_snapshot(stripped),)
    except SnapshotSchemaError:
        lines = [line for line in stripped.splitlines() if line.strip()]
        if len(lines) < 2:
            raise
        return tuple(deserialize_snapshot(line) for line in lines)


# ---------------------------------------------------------------------------
# the Stage-2 entry point (callable today, unwired by design)
# ---------------------------------------------------------------------------
def record_signal_snapshot(
    *,
    date: Any,
    strategy: str,
    universe_id: str,
    scores: Union[Mapping[str, float], pd.Series],
    candidates: Optional[Iterable[str]] = None,
    information_set: str = CLOSE_INFORMATION_SET,
    recorded_at: Any = None,
    source: str = "",
    path: Union[str, Path, None] = None,
    append: bool = False,
) -> SignalSnapshot:
    """Validate one run's scores and optionally persist them.

    This is the function a future Stage-2 hook calls once a real cross-sectional
    score source exists. **It is not wired into `scripts/run_live_strategy.py`**:
    the live chain has no such score today (`detail_reason.dimension_signals` is a
    within-book sub-strategy weight, see the module docstring), so calling this
    with those numbers would manufacture an input that does not exist.

    `scores` must cover every declared candidate -- the validator raises
    `SelectionTruncatedSnapshotError` otherwise. No path means no IO.
    """
    if isinstance(scores, pd.Series):
        items: List[Tuple[str, float]] = [(str(k), v) for k, v in scores.items()]
    elif isinstance(scores, Mapping):
        items = list(scores.items())
    else:
        raise SnapshotSchemaError(
            f"scores must be a mapping or pandas.Series, got {type(scores).__name__}"
        )
    snapshot = SignalSnapshot(
        date=date,
        strategy=strategy,
        universe_id=universe_id,
        scores=dict(items),
        candidates=_as_symbol_tuple(candidates) if candidates is not None else (),
        information_set=information_set,
        recorded_at=recorded_at,
        source=source,
    )
    if path is not None:
        write_snapshot(snapshot, path, append=append)
    return snapshot
