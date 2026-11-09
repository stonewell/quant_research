"""Offline regression suite for `common/signal_snapshot.py` (development_plan `AT3`).

`AT3` (docs/development_plan.md §2.24) requires a cross-sectional score snapshot
with the exact row schema `(date, strategy, symbol, score, universe_id)` that
covers **every candidate, including unselected ones**, plus a serialize/deserialize
roundtrip and per-pool (`universe_id`) separation.

100% offline: no network, no real market data, no writes outside `tmp_path`.

What is pinned here, one test each:
1. the row schema is exactly the five frozen columns -- adding a column (e.g. a
   `weight` or `selected` flag) must fail, because a snapshot is not a ticket;
2. a snapshot holding only the *selected* names is rejected as
   `SelectionTruncatedSnapshotError` (`AT3` condition 2) -- this is the whole point
   of the contract;
3. mis-schema input, duplicate cells, padded tokens, tz-aware/intraday dates and
   non-finite scores are rejected with messages that name the offender (no zero
   fill, no silent normalisation);
4. `serialize_snapshot` / `deserialize_snapshot` roundtrip every field;
5. `universe_id` keeps two pools separable even when they share a ticker;
6. `record_signal_snapshot` performs no IO unless the caller passes a path, and an
   append history stays readable;
7. the module is advisory: no `subprocess` / `requests` / `scripts` / `web` import.
"""

import ast
import inspect
import json
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common import signal_snapshot as snapshot_mod  # noqa: E402
from common.signal_snapshot import (  # noqa: E402
    SNAPSHOT_COLUMNS,
    SelectionTruncatedSnapshotError,
    SignalSnapshot,
    SnapshotSchemaError,
    deserialize_snapshot,
    read_snapshots,
    record_signal_snapshot,
    serialize_snapshot,
    validate_snapshot,
)

SESSION = pd.Timestamp("2026-03-02")
CANDIDATES = tuple(f"SYM{i:02d}" for i in range(20))
HELD = CANDIDATES[:3]


def _scores(symbols=CANDIDATES, seed: float = 1.0):
    """Deterministic distinct scores; no NaN, no rounding to 4dp."""
    return {sym: seed + idx * 0.1 for idx, sym in enumerate(symbols)}


def _frame(scores=None, *, date=SESSION, strategy="blend_a", universe_id="us_core"):
    return pd.DataFrame(
        {
            "date": [date] * len(scores or _scores()),
            "strategy": [strategy] * len(scores or _scores()),
            "symbol": list(scores or _scores()),
            "score": list((scores or _scores()).values()),
            "universe_id": [universe_id] * len(scores or _scores()),
        },
        columns=list(SNAPSHOT_COLUMNS),
    )


# ---------------------------------------------------------------------------
# 1. row schema
# ---------------------------------------------------------------------------
def test_row_schema_is_exactly_the_five_frozen_columns():
    snapshot = SignalSnapshot(
        date=SESSION,
        strategy="blend_a",
        universe_id="us_core",
        scores=_scores(),
        candidates=CANDIDATES,
        recorded_at=SESSION,
    )
    frame = snapshot.to_frame()
    assert tuple(frame.columns) == SNAPSHOT_COLUMNS == (
        "date",
        "strategy",
        "symbol",
        "score",
        "universe_id",
    )
    assert len(frame) == len(CANDIDATES)
    assert set(frame["date"]) == {SESSION}
    assert set(frame["strategy"]) == {"blend_a"}
    assert set(frame["universe_id"]) == {"us_core"}
    assert frame["score"].dtype == float
    # every candidate is present, not just the ones that would be bought
    assert set(frame["symbol"]) == set(CANDIDATES)

    # An extra column is a schema violation, not a tolerated extension: a
    # `selected`/`weight` column would turn the cross-section back into a ticket.
    extra = frame.assign(selected=True)
    with pytest.raises(SnapshotSchemaError, match="exactly"):
        validate_snapshot(extra)


# ---------------------------------------------------------------------------
# 2. selection truncation
# ---------------------------------------------------------------------------
def test_snapshot_of_only_held_symbols_is_rejected_as_selection_truncated():
    # exactly what the live chain can produce today: the book's symbols only
    held_only = _scores(HELD)
    with pytest.raises(SelectionTruncatedSnapshotError) as excinfo:
        SignalSnapshot(
            date=SESSION,
            strategy="blend_a",
            universe_id="us_core",
            scores=held_only,
            candidates=CANDIDATES,
        )
    message = str(excinfo.value)
    assert "selection" in message.lower()
    assert "17 of 20" in message  # 20 declared, 3 present -> 17 missing

    # the frame-level validator enforces the same rule
    with pytest.raises(SelectionTruncatedSnapshotError):
        validate_snapshot(_frame(held_only), expected_candidates=CANDIDATES)

    # ... and accepts the complete cross-section
    validated = validate_snapshot(_frame(_scores()), expected_candidates=CANDIDATES)
    assert len(validated) == len(CANDIDATES)

    # a symbol outside the declared pool is refused too: no silent addition
    padded = _scores(CANDIDATES + ("EXTRA1",))
    with pytest.raises(SnapshotSchemaError, match="outside the declared candidate"):
        validate_snapshot(_frame(padded), expected_candidates=CANDIDATES)


# ---------------------------------------------------------------------------
# 3. schema / value rejections
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda f: f.drop(columns=["universe_id"]), "missing="),
        (lambda f: f.iloc[0:0], "no rows"),
        (lambda f: pd.concat([f, f.iloc[[0]]], ignore_index=True), "duplicate"),
        (
            lambda f: f.assign(score=[float("nan")] + list(f["score"])[1:]),
            "not finite",
        ),
        (
            lambda f: f.assign(score=[float("inf")] + list(f["score"])[1:]),
            "not finite",
        ),
        (lambda f: f.assign(symbol=[" " + s for s in f["symbol"]]), "whitespace"),
        (lambda f: f.assign(strategy=[""] * len(f)), "must not be empty"),
        (
            lambda f: f.assign(date=[pd.Timestamp("2026-03-02 14:15")] + [SESSION] * (len(f) - 1)),
            "midnight-normalised",
        ),
        (
            lambda f: f.assign(
                date=[pd.Timestamp("2026-03-02", tz="Asia/Shanghai")] + [SESSION] * (len(f) - 1)
            ),
            "tz-aware",
        ),
        (lambda f: f.assign(score=["abc"] * len(f)), "must be a real number"),
    ],
)
def test_validate_rejects_malformed_frames(mutate, expected):
    with pytest.raises(SnapshotSchemaError, match=expected):
        validate_snapshot(mutate(_frame()))


def test_nan_score_message_says_why_zero_fill_is_forbidden():
    bad = _frame()
    bad.loc[0, "score"] = float("nan")
    with pytest.raises(SnapshotSchemaError) as excinfo:
        validate_snapshot(bad)
    # The message must explain the rule, not just refuse: NaN/0 are indistinguishable
    # once the IC is computed, so neither may be recorded.
    assert "indistinguishable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. serialisation roundtrip
# ---------------------------------------------------------------------------
def test_serialize_deserialize_roundtrip_preserves_every_field():
    recorded_at = pd.Timestamp("2026-03-02 16:05:00")
    original = SignalSnapshot(
        date=SESSION,
        strategy="blend_a",
        universe_id="us_core",
        scores=_scores(),
        candidates=CANDIDATES,
        information_set="close",
        recorded_at=recorded_at,
        source="synthetic unit test",
    )
    text = serialize_snapshot(original)
    payload = json.loads(text)
    assert payload["schema"] == snapshot_mod.SNAPSHOT_SCHEMA
    assert len(payload["rows"]) == len(CANDIDATES)
    assert set(payload["rows"][0]) == set(SNAPSHOT_COLUMNS)

    restored = deserialize_snapshot(text)
    assert restored == original
    assert dict(restored.scores) == dict(original.scores)
    assert restored.recorded_at == recorded_at
    assert restored.candidates == CANDIDATES
    # re-serialisation is byte-identical (sort_keys + sorted symbols)
    assert serialize_snapshot(restored) == text


def test_deserialize_rejects_foreign_schema_and_extra_row_keys():
    text = serialize_snapshot(
        SignalSnapshot(
            date=SESSION,
            strategy="blend_a",
            universe_id="us_core",
            scores=_scores(),
            candidates=CANDIDATES,
        )
    )
    payload = json.loads(text)
    payload["schema"] = "signal_snapshot.v0"
    with pytest.raises(SnapshotSchemaError, match="unsupported snapshot schema"):
        deserialize_snapshot(json.dumps(payload))

    payload = json.loads(text)
    payload["rows"][0]["weight"] = 0.1
    with pytest.raises(SnapshotSchemaError, match="row keys"):
        deserialize_snapshot(json.dumps(payload))

    payload = json.loads(text)
    payload["extra"] = 1
    with pytest.raises(SnapshotSchemaError, match="unknown envelope"):
        deserialize_snapshot(json.dumps(payload))


# ---------------------------------------------------------------------------
# 5. universe separation
# ---------------------------------------------------------------------------
def test_universe_id_keeps_pools_separable():
    us = _scores(("AAA", "BBB", "CCC"), seed=0.5)
    hk = {"00700": 1.0, "00939": 2.0, "00005": 3.0}
    frame = pd.concat(
        [
            _frame(us, universe_id="us_core"),
            _frame(hk, universe_id="hk_core", strategy="blend_b"),
        ],
        ignore_index=True,
    )
    expected = {("us_core", "blend_a"): tuple(us), ("hk_core", "blend_b"): tuple(hk)}
    validated = validate_snapshot(frame, expected_candidates=expected)
    assert len(validated) == 6

    # Same ticker, two pools, two different scores: both survive as distinct rows.
    a = _frame({"AAPL": 0.1, "MSFT": 0.2}, universe_id="pool_x")
    b = _frame({"AAPL": 9.9, "MSFT": -9.9}, universe_id="pool_y")
    both = pd.concat([a, b], ignore_index=True)
    validated = validate_snapshot(both)
    assert sorted(validated.loc[validated["symbol"] == "AAPL", "score"]) == [0.1, 9.9]

    # One snapshot object is one run: a two-pool frame must be split by the caller,
    # never silently merged into one cross-section.
    with pytest.raises(SnapshotSchemaError, match="exactly one"):
        SignalSnapshot.from_frame(both)

    # ... and a per-pool completeness check does not leak across pools
    with pytest.raises(SelectionTruncatedSnapshotError):
        validate_snapshot(
            a, expected_candidates={("pool_x", "blend_a"): ("AAPL", "MSFT", "NVDA")}
        )


# ---------------------------------------------------------------------------
# 6. record entry point / IO only when asked
# ---------------------------------------------------------------------------
def test_record_signal_snapshot_writes_only_when_a_path_is_given(tmp_path):
    snapshot = record_signal_snapshot(
        date=SESSION,
        strategy="blend_a",
        universe_id="us_core",
        scores=_scores(),
        candidates=CANDIDATES,
        recorded_at=pd.Timestamp("2026-03-02 15:30"),
    )
    assert snapshot.is_complete
    assert list(tmp_path.iterdir()) == []  # no path -> no IO side effect

    target = tmp_path / "history.jsonl"
    record_signal_snapshot(
        date=SESSION,
        strategy="blend_a",
        universe_id="us_core",
        scores=_scores(),
        candidates=CANDIDATES,
        path=target,
    )
    record_signal_snapshot(
        date=pd.Timestamp("2026-03-03"),
        strategy="blend_a",
        universe_id="us_core",
        scores=_scores(seed=2.0),
        candidates=CANDIDATES,
        path=target,
        append=True,
    )
    history = read_snapshots(target)
    assert [s.date for s in history] == [SESSION, pd.Timestamp("2026-03-03")]
    assert len(target.read_text(encoding="utf-8").splitlines()) == 2
    # the appended run is a *different* snapshot, not a rewrite of the first one
    assert history[0].scores["SYM00"] != history[1].scores["SYM00"]

    # a series is accepted as a score container, and truncation is still refused
    with pytest.raises(SelectionTruncatedSnapshotError):
        record_signal_snapshot(
            date=SESSION,
            strategy="blend_a",
            universe_id="us_core",
            scores=pd.Series(_scores(HELD)),
            candidates=CANDIDATES,
        )


# ---------------------------------------------------------------------------
# 7. advisory-only module boundary
# ---------------------------------------------------------------------------
def test_module_is_advisory_with_no_io_dependencies():
    tree = ast.parse(inspect.getsource(snapshot_mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)
    for forbidden in ("subprocess", "requests", "urllib", "http", "socket", "scripts", "web"):
        assert forbidden not in imported, f"snapshot module must stay offline/advisory, imports {forbidden}"
    # The module must not compute IC itself: that is factor_analyzer's job.
    source = inspect.getsource(snapshot_mod)
    for delegated in ("spearmanr", "compute_rank_ic", "ic_ir", "icir"):
        assert delegated not in source
