"""Offline regression tests for `scripts/audit_qfq_alignment.py` (development_plan L2.8).

The audit script is the *reproduction* artefact for the QFQ bounded-window anomaly
documented in `docs/version.md` §9.2 / §13. Its contract is narrow and worth pinning:

1. it must reproduce the two shapes that were actually observed in the production
   cache, built here as synthetic patterns —
   * "tidy truncation": one contiguous window of rows multiplied by one constant
     scalar, unchanged outside (that is `600362.SH`);
   * "unrewritten middle": a material window, an untouched gap, then a ULP-only
     rewrite of a later window (that is `600660.SH`);
2. it must not confuse the two: a ULP-only rewrite is a *write*, not a price change,
   and must not be reported as an anomaly;
3. it must be read-only and must never depend on the production cache.

100% offline. Every database lives in pytest's `tmp_path`; no network, no real market
data, and `data/cache.duckdb` is neither opened nor referenced.
"""

import datetime
import json
import os
import sys

import duckdb
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import audit_qfq_alignment as audit


SYMBOL = "600000.SH"
PROVIDER = "ProviderA"
REFERENCE = "ProviderB"


# ---------------------------------------------------------------------------
# Synthetic cache builders
# ---------------------------------------------------------------------------

_START = datetime.date(2024, 1, 1)


def _sessions(n):
    """n consecutive weekday sessions (holidays irrelevant for these assertions)."""
    out = []
    d = _START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _base_frame(n=60, start_price=20.0, drift=0.004):
    """Deterministic pseudo-price series with non-round (float qfq-like) values."""
    dates = _sessions(n)
    rows = []
    price = start_price
    for i, d in enumerate(dates):
        price = price * (1.0 + drift * ((i % 5) - 2) + drift * 0.25)
        rows.append(
            {
                "date": d,
                "open": price * 0.9981234567,
                "high": price * 1.0076543211,
                "low": price * 0.9934567891,
                "close": price,
                "volume": 1_000_000.0 + 137.0 * i,
            }
        )
    return pd.DataFrame(rows)


def _ult_tiny(frame):
    """Copy with per-row float64 ULP-level perturbation (a measurable no-op rewrite)."""
    out = frame.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = (out[col] * (1.0 + 4.440892098500626e-16)).astype(float)
    return out


def _write_db(path, frames, provider=PROVIDER):
    """``frames``: {symbol: DataFrame} -> a DuckDB file with the audit's expected schema."""
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE ohlcv_bars (
                provider VARCHAR, symbol VARCHAR, date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
            )
            """
        )
        for sym, df in frames.items():
            for _, r in df.iterrows():
                con.execute(
                    "INSERT INTO ohlcv_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        provider,
                        sym,
                        r["date"],
                        float(r["open"]),
                        float(r["high"]),
                        float(r["low"]),
                        float(r["close"]),
                        float(r["volume"]),
                    ],
                )
    finally:
        con.close()
    return str(path)


def _read_fuyao(path, symbol=SYMBOL, provider=PROVIDER):
    con = audit.connect_read_only(path)
    try:
        return audit.load_bars(con, provider, symbol)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Pattern 1: "tidy truncation" — one contiguous constant-scalar window
# ---------------------------------------------------------------------------


def test_constant_scalar_window_is_reported_as_bounded_rescale(tmp_path):
    base = _base_frame()
    k = 0.99663988
    scaled = base.copy()
    lo, hi = 10, 39  # 30 contiguous rows
    scaled.loc[lo:hi, ["open", "high", "low", "close"]] *= k

    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: scaled})

    blocks = audit.detect_blocks(
        _read_fuyao(new_db), _read_fuyao(old_db), SYMBOL, PROVIDER
    )
    assert len(blocks) == 1
    b = blocks[0]
    assert b.kind == "material_rescale"
    assert b.bounded is True
    assert b.rows == 30
    assert b.start == _sessions(60)[lo]
    assert b.end == _sessions(60)[hi]
    assert b.ratios["close"] == pytest.approx(k, rel=1e-12)
    # One constant scalar => the per-row ratio spread is float64 noise, nothing more.
    assert b.ratio_spread["close"] < 1e-12
    # The injected error shows up ONLY on the two returns straddling the edges. It is
    # not exactly (k-1): the bar's own return rides along, so the distortion is
    # (1 + r_edge) * (k - 1) on entry and (1 + r_edge) * (1/k - 1) on exit.
    sessions = _sessions(60)
    close = base.set_index("date")["close"]
    exp_left = (close[sessions[lo]] / close[sessions[lo - 1]]) * (k - 1.0) * 100.0
    exp_right = (close[sessions[hi + 1]] / close[sessions[hi]]) * (1.0 / k - 1.0) * 100.0
    assert b.boundary_distortion_pp["left_pp"] == pytest.approx(exp_left, rel=1e-9)
    assert b.boundary_distortion_pp["right_pp"] == pytest.approx(exp_right, rel=1e-9)
    assert b.boundary_distortion_pp["left_pp"] < 0 < b.boundary_distortion_pp["right_pp"]
    assert audit.is_anomaly(b) is True


def test_open_ended_scalar_window_is_flagged_as_unbounded(tmp_path):
    """A genuine ex-dividend re-basing has this shape (scaled to the last bar)."""
    base = _base_frame()
    scaled = base.copy()
    scaled.loc[30:, ["open", "high", "low", "close"]] *= 0.98

    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: scaled})

    blocks = audit.detect_blocks(_read_fuyao(new_db), _read_fuyao(old_db), SYMBOL, PROVIDER)
    assert len(blocks) == 1
    assert blocks[0].bounded is False
    assert blocks[0].right_anchor is None
    assert "right_pp" not in blocks[0].boundary_distortion_pp


# ---------------------------------------------------------------------------
# Pattern 2: "unrewritten middle" — material window + untouched gap + ULP window
# ---------------------------------------------------------------------------


def test_unrewritten_middle_keeps_windows_separate(tmp_path):
    base = _base_frame(n=80)
    new = base.copy()
    k = 0.98141264
    new.loc[10:29, ["open", "high", "low", "close"]] *= k  # material window
    # rows 30..64 are the "history that was never rewritten"
    ulp = _ult_tiny(base.loc[65:79])  # a later window that was rewritten as a no-op
    for col in ("open", "high", "low", "close"):
        new.loc[65:79, col] = ulp[col].values

    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: new})

    blocks = audit.detect_blocks(_read_fuyao(new_db), _read_fuyao(old_db), SYMBOL, PROVIDER)
    kinds = [b.kind for b in blocks]
    assert kinds == ["material_rescale", "ulp_noise"]
    material, noise = blocks
    assert material.rows == 20
    assert material.end == _sessions(80)[29]
    assert noise.start == _sessions(80)[65]
    assert noise.end == _sessions(80)[79]
    assert noise.max_rel_change < 1e-12
    # The untouched middle (row 30..64) must NOT appear inside any reported block.
    middle_lo, middle_hi = _sessions(80)[30], _sessions(80)[64]
    for b in blocks:
        assert not (b.start >= middle_lo and b.end <= middle_hi)
    assert material.end < middle_lo and noise.start > middle_hi

    summary = audit.summarise(blocks)
    assert summary["anomalies"] == 1  # the ULP rewrite is a write, not a price change
    assert summary["rows_affected"] == 20


def test_ulp_only_rewrite_is_clean_at_the_cli_level(tmp_path, capsys):
    base = _base_frame()
    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: _ult_tiny(base)})

    code = audit.main(["--db", new_db, "--baseline", old_db, "--symbol", SYMBOL,
                       "--provider", PROVIDER])
    out = capsys.readouterr().out
    assert code == audit.EXIT_OK
    assert "ulp_noise" in out
    assert "exit: 0 (clean)" in out


def test_non_constant_rewrite_is_classified_as_rewrite(tmp_path):
    base = _base_frame()
    new = base.copy()
    # A per-row jitter is what an upstream re-fetch looks like, not a local scalar.
    new.loc[10:29, ["open", "high", "low", "close"]] = (
        base.loc[10:29, ["open", "high", "low", "close"]] * 1.001234
    )
    new.loc[15, "close"] *= 1.01  # one extra outlier row => ratio no longer constant

    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: new})

    blocks = audit.detect_blocks(_read_fuyao(new_db), _read_fuyao(old_db), SYMBOL, PROVIDER)
    assert len(blocks) == 1
    assert blocks[0].kind == "rewrite"
    assert blocks[0].ratio_spread["close"] > 1e-9


# ---------------------------------------------------------------------------
# Cross-provider (single-DB) mode
# ---------------------------------------------------------------------------


def test_cross_provider_seams_bound_the_realigned_window(tmp_path, capsys):
    base = _base_frame(n=60)
    ref = base.copy()
    a = base.copy()
    lo, hi = 12, 31
    a.loc[lo:hi, ["open", "high", "low", "close"]] *= 0.98

    db = str(tmp_path / "one.duckdb")
    con = duckdb.connect(db)
    try:
        con.execute(
            """
            CREATE TABLE ohlcv_bars (
                provider VARCHAR, symbol VARCHAR, date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
            )
            """
        )
        for prov, df in ((PROVIDER, a), (REFERENCE, ref)):
            for _, r in df.iterrows():
                con.execute(
                    "INSERT INTO ohlcv_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [prov, SYMBOL, r["date"], float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r["volume"])],
                )
    finally:
        con.close()

    con = audit.connect_read_only(db)
    try:
        a_df = audit.load_bars(con, PROVIDER, SYMBOL)
        b_df = audit.load_bars(con, REFERENCE, SYMBOL)
    finally:
        con.close()

    seams = audit.detect_cross_provider_seams(a_df, b_df, SYMBOL, PROVIDER, REFERENCE)
    assert len(seams) == 2
    assert seams[0].date_after == _sessions(60)[lo]
    assert seams[1].date_before == _sessions(60)[hi]
    assert seams[0].step_pp == pytest.approx(-2.0, abs=1e-6)
    assert seams[1].step_pp == pytest.approx((1.0 / 0.98 - 1.0) * 100.0, abs=1e-6)

    windows = audit.seam_windows(seams)
    assert len(windows) == 1
    start, end, offset = windows[0]
    assert (start, end) == (_sessions(60)[lo], _sessions(60)[hi])
    assert offset == pytest.approx(-2.0, abs=1e-6)

    code = audit.main(["--db", db, "--symbol", SYMBOL, "--provider", PROVIDER,
                       "--reference-provider", REFERENCE])
    out = capsys.readouterr().out
    assert code == audit.EXIT_ANOMALY
    assert "bounded window" in out


def test_cross_provider_mode_is_clean_when_namespaces_agree(tmp_path, capsys):
    base = _base_frame()
    db = str(tmp_path / "clean.duckdb")
    con = duckdb.connect(db)
    try:
        con.execute(
            """
            CREATE TABLE ohlcv_bars (
                provider VARCHAR, symbol VARCHAR, date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
            )
            """
        )
        for prov in (PROVIDER, REFERENCE):
            for _, r in base.iterrows():
                con.execute(
                    "INSERT INTO ohlcv_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [prov, SYMBOL, r["date"], float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r["volume"])],
                )
    finally:
        con.close()

    code = audit.main(["--db", db, "--symbol", SYMBOL, "--provider", PROVIDER,
                       "--reference-provider", REFERENCE])
    out = capsys.readouterr().out
    assert code == audit.EXIT_OK
    assert "no ratio seams found" in out


# ---------------------------------------------------------------------------
# Contract: read-only, filterable, machine-readable, honest exit codes
# ---------------------------------------------------------------------------


def test_audit_opens_databases_read_only(tmp_path, monkeypatch):
    """A write-mode connection would let a future edit corrupt the production cache."""
    base = _base_frame()
    db = _write_db(tmp_path / "one.duckdb", {SYMBOL: base})

    seen = []
    real_connect = audit.duckdb.connect

    def _spy(path, *a, **kw):
        seen.append(kw.get("read_only", False))
        return real_connect(path, *a, **kw)

    monkeypatch.setattr(audit.duckdb, "connect", _spy)
    audit.main(["--db", db, "--symbol", SYMBOL, "--provider", PROVIDER])
    assert seen, "the audit never opened the database"
    assert all(seen), f"a connection was opened read-write: {seen}"


def test_audit_leaves_the_file_bytes_untouched(tmp_path):
    base = _base_frame()
    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    scaled = base.copy()
    scaled.loc[10:29, ["open", "high", "low", "close"]] *= 0.99
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: scaled})

    before = (os.stat(new_db).st_mtime_ns, os.stat(new_db).st_size)
    audit.main(["--db", new_db, "--baseline", old_db, "--symbol", SYMBOL,
                "--provider", PROVIDER])
    after = (os.stat(new_db).st_mtime_ns, os.stat(new_db).st_size)
    assert before == after
    assert not os.path.exists(new_db + ".wal")


def test_missing_file_is_a_data_error_not_an_anomaly(tmp_path, capsys):
    code = audit.main(["--db", str(tmp_path / "nope.duckdb"), "--symbol", SYMBOL])
    assert code == audit.EXIT_ERROR
    assert "cache file not found" in capsys.readouterr().err


def test_symbol_and_date_range_filters_are_honoured(tmp_path):
    base = _base_frame(n=60)
    scaled = base.copy()
    scaled.loc[10:29, ["open", "high", "low", "close"]] *= 0.99
    other = "600001.SH"
    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base, other: base.copy()})
    new_db = _write_db(
        tmp_path / "new.duckdb", {SYMBOL: scaled, other: scaled.copy()}
    )

    # Window that excludes the rewritten block => nothing to report for that range.
    con = audit.connect_read_only(new_db)
    con_old = audit.connect_read_only(old_db)
    try:
        a = audit.load_bars(con, PROVIDER, SYMBOL, "2024-03-01", "2024-12-31")
        b = audit.load_bars(con_old, PROVIDER, SYMBOL, "2024-03-01", "2024-12-31")
    finally:
        con.close()
        con_old.close()
    assert audit.detect_blocks(a, b, SYMBOL, PROVIDER) == []

    # Symbol filter: only the requested symbol shows up in the JSON payload.
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = audit.main(
            ["--db", new_db, "--baseline", old_db, "--symbol", other,
             "--provider", PROVIDER, "--json"]
        )
    payload = json.loads(buf.getvalue())
    assert code == audit.EXIT_ANOMALY
    assert {f["symbol"] for f in payload["findings"]} == {other}
    assert payload["mode"] == "diff"
    assert payload["summary"]["anomalies"] == 1
    assert payload["findings"][0]["kind"] == "material_rescale"


def test_unknown_symbol_in_diff_mode_is_a_data_error(tmp_path, capsys):
    base = _base_frame()
    old_db = _write_db(tmp_path / "old.duckdb", {SYMBOL: base})
    new_db = _write_db(tmp_path / "new.duckdb", {SYMBOL: base})
    code = audit.main(["--db", new_db, "--baseline", old_db, "--symbol", "999999.SH",
                       "--provider", PROVIDER])
    assert code == audit.EXIT_ERROR
    assert "no bars" in capsys.readouterr().err


def test_default_symbols_and_paths_are_the_documented_ones():
    """Guards the reproduction recipe quoted in docs/version.md §13."""
    assert audit._DEFAULT_SYMBOLS == ("600362.SH", "600660.SH")
    assert audit._DEFAULT_PROVIDER == "FuyaoDataProvider"
    assert audit._DEFAULT_REFERENCE_PROVIDER == "MarketDBDataProvider"
    assert audit._DEFAULT_DB.endswith(os.path.join("data", "cache.duckdb"))
    assert audit.EXIT_OK == 0 and audit.EXIT_ANOMALY == 1 and audit.EXIT_ERROR == 2


# ===========================================================================
# Repair writer: `scripts/repair_qfq_alignment.py` (docs/version.md §14)
# ===========================================================================
#
# The repair script is the "would-be" half of the L2.8 investigation: it plans and
# (only on an explicit tmp copy) applies the two fixes, and it must refuse every
# shape that could re-create the incident. These tests pin four things:
#
#   * the plan reproduces both real shapes — a bit-exact `restore` window and a
#     single-basis `rebase` of the whole prefix;
#   * `--dry-run` is the default and touches nothing;
#   * `--apply` backs up, writes in one transaction, self-verifies, and rolls back
#     completely when the check fails (all exercised on a `tmp_path` database);
#   * the write gate rejects appends-over-history and windowed re-basing.

from scripts import repair_qfq_alignment as repair

SYM_RESTORE = "600000.SH"   # the 600660.SH shape: a bounded window, restorable
SYM_REBASE = "600001.SH"    # the 600362.SH shape: a written block, stale prefix
# Explicit row lists, not slices: `.loc[a:b]` is label-INCLUSIVE and would silently
# widen the windows by one bar (which is exactly how a repair plan goes wrong).
_WINDOW = list(range(20, 50))   # SYM_RESTORE: 30 rows multiplied by 0.98
_BLOCK = list(range(50, 70))    # SYM_REBASE: 20 rows multiplied by 0.99664
_N = 80
_SEAM = _BLOCK[0]               # first bar of the written block = the seam to close


def _write_multi_db(path, series):
    """``series``: {(provider, symbol): DataFrame} -> one DuckDB file."""
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE ohlcv_bars (
                provider VARCHAR, symbol VARCHAR, date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
            )
            """
        )
        for (prov, sym), df in series.items():
            for _, r in df.iterrows():
                con.execute(
                    "INSERT INTO ohlcv_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [prov, sym, r["date"], float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r["volume"])],
                )
    finally:
        con.close()
    return str(path)


def _repair_fixture(tmp_path):
    """A cache holding both real shapes, plus the pre-incident baseline snapshot.

    * ``SYM_RESTORE``  — reference namespace agrees everywhere except one window
      (the 600660.SH corruption);
    * ``SYM_REBASE``   — the reference namespace only covers history from the block
      onwards (exactly like MarketDB's 2024-07-22 start for 600362.SH), so the
      residual step can only be found against the baseline snapshot.
    """
    base = _base_frame(n=_N)
    restore = base.copy()
    restore.loc[_WINDOW, ["open", "high", "low", "close"]] *= 0.98

    rebase = base.copy()
    k = 0.99664
    rebase.loc[_BLOCK, ["open", "high", "low", "close"]] *= k

    # Reference covers SYM_RESTORE fully (the pre-incident level) and SYM_REBASE only
    # from the block onward, where it agrees with the target bit-for-bit.
    ref_restore = base.copy()
    ref_rebase = rebase.loc[_SEAM:].reset_index(drop=True).copy()

    db = _write_multi_db(
        tmp_path / "cache.duckdb",
        {(PROVIDER, SYM_RESTORE): restore, (REFERENCE, SYM_RESTORE): ref_restore,
         (PROVIDER, SYM_REBASE): rebase, (REFERENCE, SYM_REBASE): ref_rebase},
    )
    baseline = _write_multi_db(
        tmp_path / "cache.duckdb.bak", {(PROVIDER, SYM_RESTORE): base,
                                        (PROVIDER, SYM_REBASE): base}
    )
    return db, baseline, base, restore, rebase, k


def _plan(db, baseline, symbols=(SYM_RESTORE, SYM_REBASE)):
    con = audit.connect_read_only(db)
    con_base = audit.connect_read_only(baseline)
    try:
        return repair.build_plan(con, con_base, list(symbols), PROVIDER, REFERENCE)
    finally:
        con.close()
        con_base.close()


def _dump(db_path, symbols=(SYM_RESTORE, SYM_REBASE)):
    con = audit.connect_read_only(db_path)
    try:
        return {s: audit.load_bars(con, PROVIDER, s) for s in symbols}
    finally:
        con.close()


def _file_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Planning: both real shapes, no writes
# ---------------------------------------------------------------------------


def test_plan_detects_a_bit_exact_restore_window(tmp_path):
    db, baseline, base, _, _, _ = _repair_fixture(tmp_path)
    ops = {op.symbol: op for op in _plan(db, baseline, (SYM_RESTORE,))}
    op = ops[SYM_RESTORE]
    sessions = _sessions(_N)

    assert op.kind == "restore"
    assert (op.start, op.end) == (sessions[_WINDOW[0]], sessions[_WINDOW[-1]])
    assert op.rows == len(_WINDOW)
    assert op.factor == pytest.approx(1.0 / 0.98, rel=1e-12)
    assert op.seam_before_pp == pytest.approx(2.0, abs=1e-6)
    assert op.seam_after_pp == 0.0
    # The plan must prefer the pre-incident snapshot: only it restores the original
    # bits (the independent namespace is a ULP or two away from them in production).
    assert op.source_kind == "baseline"
    for d, vals in op.expected.items():
        row = base[base["date"] == d].iloc[0]
        assert vals == (row["open"], row["high"], row["low"], row["close"])


def test_plan_rebases_the_whole_prefix_up_to_the_seam(tmp_path):
    db, baseline, base, _, rebase, k = _repair_fixture(tmp_path)
    ops = {op.symbol: op for op in _plan(db, baseline, (SYM_REBASE,))}
    op = ops[SYM_REBASE]
    sessions = _sessions(_N)

    assert op.kind == "rebase"
    # Prefix completeness is the whole point: it must start at the first bar, not at
    # some convenient mid-history date (that would be the incident's own shape).
    assert op.start == sessions[0]
    assert op.end == sessions[_SEAM - 1]
    assert op.seam_date == sessions[_SEAM]
    assert op.rows == _SEAM
    assert op.factor == pytest.approx(k, rel=1e-12)
    # The contaminated boundary return is what the repair has to restore, and it is
    # measured against the baseline, not assumed.
    assert op.boundary_return_before_pp != pytest.approx(
        op.boundary_return_after_pp, abs=1e-6
    )
    assert abs(op.boundary_return_before_pp - op.boundary_return_after_pp) == pytest.approx(
        op.seam_before_pp, abs=1e-6
    )
    assert set(op.expected) == set(sessions[:_SEAM])


def test_dry_run_is_the_default_and_touches_nothing(tmp_path, capsys):
    db, baseline, *_ = _repair_fixture(tmp_path)
    before = (os.stat(db).st_mtime_ns, os.stat(db).st_size, _file_bytes(db))

    code = repair.main(["--db", db, "--baseline", baseline, "--provider", PROVIDER,
                        "--reference-provider", REFERENCE,
                        "--symbol", SYM_RESTORE, "--symbol", SYM_REBASE])

    out = capsys.readouterr().out
    assert code == repair.EXIT_ACTIONABLE
    assert "nothing was written" in out
    assert "restore" in out and "rebase" in out
    after = (os.stat(db).st_mtime_ns, os.stat(db).st_size, _file_bytes(db))
    assert before == after, "the dry-run modified the database"
    assert not os.path.exists(db + ".wal")


def test_dry_run_is_clean_on_an_already_repaired_cache(tmp_path, capsys):
    db, baseline, *_ = _repair_fixture(tmp_path)
    common = ["--db", db, "--baseline", baseline, "--provider", PROVIDER,
              "--reference-provider", REFERENCE,
              "--symbol", SYM_RESTORE, "--symbol", SYM_REBASE]
    assert repair.main(common + ["--apply"]) == repair.EXIT_OK
    capsys.readouterr()
    code = repair.main(common)
    assert code == repair.EXIT_OK
    assert "nothing to repair" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Apply: backup, one transaction, self-verification, idempotence
# ---------------------------------------------------------------------------


def test_apply_restores_rebases_and_self_verifies(tmp_path, capsys):
    db, baseline, base, restore, rebase, k = _repair_fixture(tmp_path)
    pre_image = _file_bytes(db)
    con = audit.connect_read_only(db)
    try:
        ref_restore = audit.load_bars(con, REFERENCE, SYM_RESTORE)
        ref_rebase = audit.load_bars(con, REFERENCE, SYM_REBASE)
    finally:
        con.close()

    code = repair.main(["--db", db, "--baseline", baseline, "--provider", PROVIDER,
                        "--reference-provider", REFERENCE,
                        "--symbol", SYM_RESTORE, "--symbol", SYM_REBASE,
                        "--apply", "--backup-dir", str(tmp_path / "bak"), "--stamp", "test"])
    assert code == repair.EXIT_OK, capsys.readouterr().err

    backup = tmp_path / "bak" / f"{os.path.basename(db)}.bak.test"
    assert backup.exists() and _file_bytes(backup) == pre_image

    got = _dump(db)
    sessions = _sessions(_N)
    # 1) the restore window is back to the pre-incident bits ...
    for d in [sessions[i] for i in _WINDOW]:
        want = base[base["date"] == d].iloc[0]
        have = got[SYM_RESTORE][got[SYM_RESTORE]["date"] == d].iloc[0]
        for col in ("open", "high", "low", "close"):
            assert have[col] == want[col]
        assert have["volume"] == want["volume"]
    # 2) ... and continuous with the untouched bars on both sides (no seam left):
    a = got[SYM_RESTORE]
    for i in (_WINDOW[0], _WINDOW[-1] + 1):
        step = a["close"].iloc[i] / a["close"].iloc[i - 1] / (
            ref_restore["close"].iloc[i] / ref_restore["close"].iloc[i - 1]
        ) - 1.0
        assert abs(step) < 1e-12
    # 3) the rebase moved exactly the prefix, by exactly the planned factor:
    b = got[SYM_REBASE]
    for i in range(_SEAM):
        assert b["close"].iloc[i] == pytest.approx(
            rebase["close"].iloc[i] * k, rel=1e-15
        )
        assert b["volume"].iloc[i] == rebase["volume"].iloc[i]
    for i in range(_SEAM, _N):
        assert b["close"].iloc[i] == rebase["close"].iloc[i]
    # 4) the boundary return is the basis-free one the pre-incident snapshot had:
    assert b["close"].iloc[_SEAM] / b["close"].iloc[_SEAM - 1] == pytest.approx(
        base["close"].iloc[_SEAM] / base["close"].iloc[_SEAM - 1], rel=1e-13
    )
    # 5) no cross-provider seam survives on the restorable symbol:
    con = audit.connect_read_only(db)
    try:
        seams = audit.detect_cross_provider_seams(
            audit.load_bars(con, PROVIDER, SYM_RESTORE), ref_restore,
            SYM_RESTORE, PROVIDER, REFERENCE,
        )
        assert seams == []
        # the reference-covered tail of the rebased symbol is untouched
        tail = audit.load_bars(con, PROVIDER, SYM_REBASE, str(sessions[_SEAM]))
        assert tail.reset_index(drop=True).equals(ref_rebase.reset_index(drop=True))
    finally:
        con.close()


def test_apply_rolls_back_completely_when_the_self_check_fails(tmp_path, monkeypatch, capsys):
    db, baseline, *_ = _repair_fixture(tmp_path)
    pre_image = _dump(db)
    pre_bytes = _file_bytes(db)

    def _boom(*a, **kw):
        raise RuntimeError("injected self-check failure")

    monkeypatch.setattr(repair, "_self_check", _boom)
    code = repair.main(["--db", db, "--baseline", baseline, "--provider", PROVIDER,
                        "--reference-provider", REFERENCE,
                        "--symbol", SYM_RESTORE, "--symbol", SYM_REBASE,
                        "--apply", "--backup-dir", str(tmp_path / "bak"), "--stamp", "test"])

    assert code == repair.EXIT_ERROR
    assert "rolled back" in capsys.readouterr().err
    post = _dump(db)
    for sym in (SYM_RESTORE, SYM_REBASE):
        pd.testing.assert_frame_equal(pre_image[sym], post[sym])
    # The backup is kept on failure too: it is the operator's fallback.
    assert _file_bytes(tmp_path / "bak" / f"{os.path.basename(db)}.bak.test") == pre_bytes


def test_apply_is_idempotent(tmp_path):
    db, baseline, *_ = _repair_fixture(tmp_path)
    args = ["--db", db, "--baseline", baseline, "--provider", PROVIDER,
            "--reference-provider", REFERENCE,
            "--symbol", SYM_RESTORE, "--symbol", SYM_REBASE,
            "--apply", "--backup-dir", str(tmp_path / "bak")]
    assert repair.main(args) == repair.EXIT_OK
    once = _dump(db)
    assert repair.main(args) == repair.EXIT_OK  # nothing to do, still exit 0
    twice = _dump(db)
    for sym in (SYM_RESTORE, SYM_REBASE):
        pd.testing.assert_frame_equal(once[sym], twice[sym])


# ---------------------------------------------------------------------------
# Hard guards: no production writes, no appends-over-history, no windowed rebase
# ---------------------------------------------------------------------------


def test_apply_refuses_the_production_cache_without_an_explicit_flag(tmp_path):
    prod = os.path.join(_REPO_ROOT, "data", "cache.duckdb")
    assert repair.is_production_cache(prod) is True
    assert repair.is_production_cache(str(tmp_path / "copy.duckdb")) is False
    with pytest.raises(repair.RepairRefused, match="production market cache"):
        repair.refuse_production_apply(prod, allow_production=False)
    # ... and the operator can still override deliberately
    repair.refuse_production_apply(prod, allow_production=True)
    repair.refuse_production_apply(str(tmp_path / "copy.duckdb"), allow_production=False)


def test_append_only_contract_rejects_writing_over_existing_history():
    last = datetime.date(2024, 6, 28)
    with pytest.raises(repair.RepairRefused, match="append-only contract violated"):
        repair.assert_append_only(last, last)
    with pytest.raises(repair.RepairRefused):
        repair.assert_append_only(datetime.date(2024, 1, 2), last)
    repair.assert_append_only(datetime.date(2024, 7, 1), last)  # a real append is fine


def test_windowed_rebase_is_refused(tmp_path):
    db, baseline, *_ = _repair_fixture(tmp_path)
    op = _plan(db, baseline, (SYM_REBASE,))[0]
    sessions = _sessions(_N)
    series_min, series_max = sessions[0], sessions[-1]

    good = repair.RepairOp(**{**op.__dict__})
    repair.assert_single_basis_prefix(good, series_min, series_max, sessions[_SEAM])

    windowed = repair.RepairOp(**{**op.__dict__, "start": sessions[10]})
    with pytest.raises(repair.RepairRefused, match="windowed re-alignment"):
        repair.assert_single_basis_prefix(windowed, series_min, series_max, sessions[_SEAM])

    overshoot = repair.RepairOp(**{**op.__dict__, "end": sessions[_SEAM]})
    with pytest.raises(repair.RepairRefused, match="must end before the seam"):
        repair.assert_single_basis_prefix(overshoot, series_min, series_max, sessions[_SEAM])

    noop = repair.RepairOp(**{**op.__dict__, "factor": 1.0})
    with pytest.raises(repair.RepairRefused, match="implausible rebase factor"):
        repair.assert_single_basis_prefix(noop, series_min, series_max, sessions[_SEAM])


def test_admit_write_refuses_appends_and_unknown_shapes(tmp_path):
    db, baseline, *_ = _repair_fixture(tmp_path)
    ops = {op.symbol: op for op in _plan(db, baseline)}
    sessions = _sessions(_N)
    con = audit.connect_read_only(db)
    con_base = audit.connect_read_only(baseline)
    try:
        append = repair.RepairOp(
            symbol=SYM_REBASE, provider=PROVIDER, kind="rebase",
            start=sessions[-1] + datetime.timedelta(days=1),
            end=sessions[-1] + datetime.timedelta(days=30), rows=1, factor=0.99,
            seam_date=sessions[-1] + datetime.timedelta(days=31),
        )
        with pytest.raises(repair.RepairRefused, match="appends belong to"):
            repair.admit_write(con, append, REFERENCE)

        windowed = repair.RepairOp(**{**ops[SYM_REBASE].__dict__, "start": sessions[5]})
        with pytest.raises(repair.RepairRefused, match="windowed re-alignment"):
            repair.admit_write(con, windowed, REFERENCE)

        unknown = repair.RepairOp(**{**ops[SYM_REBASE].__dict__, "kind": "tweak"})
        with pytest.raises(repair.RepairRefused, match="unknown op kind"):
            repair.admit_write(con, unknown, REFERENCE)

        # a baseline-sourced restore needs the baseline connection: substituting the
        # independent namespace silently would be a different repair
        with pytest.raises(repair.RepairRefused, match="no baseline was supplied"):
            repair.admit_write(con, ops[SYM_RESTORE], REFERENCE)

        # the two shapes the plan actually produced are admitted
        repair.admit_write(con, ops[SYM_REBASE], REFERENCE, con_base)
        repair.admit_write(con, ops[SYM_RESTORE], REFERENCE, con_base)
    finally:
        con.close()
        con_base.close()


def test_restore_is_refused_when_the_source_disagrees_outside_the_window(tmp_path):
    db, baseline, *_ = _repair_fixture(tmp_path)
    op = _plan(db, baseline, (SYM_RESTORE,))[0]
    con = audit.connect_read_only(db)
    try:
        target = audit.load_bars(con, PROVIDER, SYM_RESTORE)
        reference = audit.load_bars(con, REFERENCE, SYM_RESTORE)
    finally:
        con.close()

    drifted = reference.copy()
    drifted.loc[0, "close"] *= 1.01  # a namespace that is not on the same basis
    with pytest.raises(repair.RepairRefused, match="OUTSIDE the restore window"):
        repair.assert_restore_is_reference_exact(
            op, target, drifted, (op.start, op.end)
        )


def test_repair_defaults_and_exit_codes_match_the_documented_recipe():
    assert repair._DEFAULT_SYMBOLS == ("600362.SH", "600660.SH")
    assert repair._DEFAULT_PROVIDER == "FuyaoDataProvider"
    assert repair._DEFAULT_REFERENCE_PROVIDER == "MarketDBDataProvider"
    assert repair._DEFAULT_DB.endswith(os.path.join("data", "cache.duckdb"))
    assert repair.EXIT_OK == 0 and repair.EXIT_ACTIONABLE == 1 and repair.EXIT_ERROR == 2
    # "the plan ran" and "the cache is clean" must not share a code path: the residual
    # exit is non-zero by construction.
    assert repair.EXIT_RESIDUAL == 1 != repair.EXIT_OK


# ---------------------------------------------------------------------------
# Default repair SCOPE: the census damage set, not the documented pair
# ---------------------------------------------------------------------------
#
# The defect these tests pin: `--apply`/`--dry-run` used to default to
# `_DEFAULT_SYMBOLS` (the two symbols §14 had documented). In the production rehearsal
# that planned 2 of the 5 ops, left three damaged blocks (601225/601288/601398) in the
# cache, and the follow-up dry-run still printed "nothing to repair" — a repair that
# looks finished while it is only a third done. The default must therefore BE the
# whole-universe damage set, and any narrowing must say what it left behind.
#
# The fixture below is built so that the old default and the census disagree: three
# damage blocks, the first two on the two documented symbols. Old code plans 2 of 3.

SCOPE_WINDOW = "600660.SH"    # bounded window, restorable bit-exact (the §14 shape)
SCOPE_OPEN_A = "600362.SH"    # open-ended block, reference namespace starts mid-history
SCOPE_OPEN_B = "601225.SH"    # second open-ended block: the one the old scope missed
_S_WINDOW = list(range(20, 41))   # 21 rows, closed on both sides
_S_OPEN = list(range(30, 100))    # 70 rows, up to the newest stored bar
_S_REF_START = 50                 # the reference namespace starts mid-history
_S_N = 100
_OHLC = ["open", "high", "low", "close"]


def _scope_fixture(tmp_path):
    """Three damage blocks over three symbols; two of them are the documented pair."""
    base = _base_frame(n=_S_N)

    window = base.copy()
    window.loc[_S_WINDOW, _OHLC] *= 0.98
    open_a = base.copy()
    open_a.loc[_S_OPEN, _OHLC] *= 0.99
    open_b = base.copy()
    open_b.loc[_S_OPEN, _OHLC] *= 0.985

    live = _write_multi_db(
        tmp_path / "cache.duckdb",
        {
            (PROVIDER, SCOPE_WINDOW): window,
            (REFERENCE, SCOPE_WINDOW): base,
            (PROVIDER, SCOPE_OPEN_A): open_a,
            (REFERENCE, SCOPE_OPEN_A): base.iloc[_S_REF_START:].reset_index(drop=True),
            (PROVIDER, SCOPE_OPEN_B): open_b,
            (REFERENCE, SCOPE_OPEN_B): base.iloc[_S_REF_START:].reset_index(drop=True),
        },
    )
    baseline = _write_multi_db(
        tmp_path / "cache.duckdb.bak",
        {
            (PROVIDER, SCOPE_WINDOW): base,
            (PROVIDER, SCOPE_OPEN_A): base,
            (PROVIDER, SCOPE_OPEN_B): base,
        },
    )
    return live, baseline, base


def _scope_args(db, baseline):
    return ["--db", db, "--baseline", baseline, "--provider", PROVIDER,
            "--reference-provider", REFERENCE]


def test_default_scope_covers_every_census_damage_block(tmp_path, capsys):
    """The default plan == the census damage set (3 blocks here, not the old pair's 2)."""
    db, baseline, base = _scope_fixture(tmp_path)

    code = repair.main(_scope_args(db, baseline) + ["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == repair.EXIT_ACTIONABLE
    # Discriminating assertion: the old default scope planned 2 ops (SCOPE_WINDOW +
    # SCOPE_OPEN_A) and silently skipped SCOPE_OPEN_B.
    assert len(payload["ops"]) == 3
    assert {o["symbol"] for o in payload["ops"]} == {SCOPE_WINDOW, SCOPE_OPEN_A, SCOPE_OPEN_B}
    assert all(o["kind"] == "restore" for o in payload["ops"])
    assert sum(o["rows"] for o in payload["ops"]) == len(_S_WINDOW) + 2 * len(_S_OPEN)
    # The scope is certified by the census, and nothing is left over:
    assert payload["damage_census_certified"] is True
    assert len(payload["damage_blocks"]) == 3
    assert payload["residual_damage"] == []
    assert payload["residual_damage_count"] == 0


def test_narrowed_scope_reports_residual_damage_and_exits_nonzero(tmp_path, capsys):
    """A narrowing switch must self-expose what it leaves damaged (stdout + JSON + exit)."""
    db, baseline, base = _scope_fixture(tmp_path)

    code = repair.main(
        _scope_args(db, baseline) + ["--symbol", SCOPE_WINDOW, "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == repair.EXIT_RESIDUAL != repair.EXIT_OK
    assert len(payload["ops"]) == 1
    assert payload["residual_damage_count"] == 2
    assert {b["symbol"] for b in payload["residual_damage"]} == {SCOPE_OPEN_A, SCOPE_OPEN_B}
    # the residual is measured on the whole provider universe, not on the narrowed slice
    assert len(payload["damage_blocks"]) == 3

    code = repair.main(_scope_args(db, baseline) + ["--symbol", SCOPE_WINDOW])
    out = capsys.readouterr().out
    assert code == repair.EXIT_RESIDUAL
    assert "未修复的剩余致损块" in out
    assert "仍有 2 块残差" in out
    assert SCOPE_OPEN_A in out and SCOPE_OPEN_B in out
    assert "nothing to repair" not in out


def test_all_symbols_flag_still_means_the_default_scope(tmp_path, capsys):
    """Backward compatibility: --all-symbols is an explicit restatement, not a change."""
    db, baseline, _ = _scope_fixture(tmp_path)

    default_code = repair.main(_scope_args(db, baseline) + ["--json"])
    default = json.loads(capsys.readouterr().out)
    flag_code = repair.main(_scope_args(db, baseline) + ["--all-symbols", "--json"])
    flagged = json.loads(capsys.readouterr().out)

    assert default_code == flag_code == repair.EXIT_ACTIONABLE
    assert default["ops"] == flagged["ops"]
    assert default["scope"] == "all-symbols" and flagged["scope"] == "all-symbols"
    # ... and the two switches contradict each other instead of silently picking one.
    assert repair.main(
        _scope_args(db, baseline) + ["--symbol", SCOPE_WINDOW, "--all-symbols"]
    ) == repair.EXIT_ERROR
    capsys.readouterr()


def test_full_apply_clears_the_census_and_is_idempotent(tmp_path, capsys):
    db, baseline, base = _scope_fixture(tmp_path)
    args = _scope_args(db, baseline) + ["--apply", "--backup-dir", str(tmp_path / "bak")]

    assert repair.main(args) == repair.EXIT_OK, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "post-apply census: nothing to repair" in out
    assert "仍有" not in out

    # every window is back to the pre-incident bits, volumes untouched
    sessions = _sessions(_S_N)
    got = _dump(db, symbols=(SCOPE_WINDOW, SCOPE_OPEN_A, SCOPE_OPEN_B))
    for sym, idx in ((SCOPE_WINDOW, _S_WINDOW), (SCOPE_OPEN_A, _S_OPEN),
                     (SCOPE_OPEN_B, _S_OPEN)):
        for i in idx:
            d = sessions[i]
            want = base[base["date"] == d].iloc[0]
            have = got[sym][got[sym]["date"] == d].iloc[0]
            for col in _OHLC:
                assert have[col] == want[col]
            assert have["volume"] == want["volume"]
    once = got

    # idempotent: the second apply derives zero ops and must not back up/write anything
    assert repair.main(args) == repair.EXIT_OK
    out = capsys.readouterr().out
    assert "nothing to repair" in out and "applied" not in out
    twice = _dump(db, symbols=(SCOPE_WINDOW, SCOPE_OPEN_A, SCOPE_OPEN_B))
    for sym in (SCOPE_WINDOW, SCOPE_OPEN_A, SCOPE_OPEN_B):
        pd.testing.assert_frame_equal(once[sym], twice[sym])

    # ... and the dry-run agrees, with exit 0 (the census damage set is empty)
    code = repair.main(_scope_args(db, baseline))
    out = capsys.readouterr().out
    assert code == repair.EXIT_OK
    assert "nothing to repair" in out
    assert "1 op(s)" not in out


def test_apply_without_a_baseline_is_unverified_not_clean(tmp_path, capsys):
    """No snapshot ⇒ no census ⇒ "nothing to repair" would be a lie; exit non-zero."""
    db, baseline, _ = _scope_fixture(tmp_path)
    code = repair.main(
        ["--db", db, "--provider", PROVIDER, "--reference-provider", REFERENCE]
    )
    out = capsys.readouterr().out
    assert code == repair.EXIT_RESIDUAL != repair.EXIT_OK
    assert "UNVERIFIED" in out
    assert "nothing to repair" not in out


# ===========================================================================
# Whole-universe census (docs/version.md §15)
# ===========================================================================
#
# §14 knew two damaged symbols because it looked at the two that a backtest had
# surfaced. The census is the "how many are there really" pass, and these tests pin the
# three things it must get right:
#
#   * scan *every* symbol of the provider, not a curated list;
#   * judge each rewrite's direction from the reference namespace level on the same
#     sessions — a write that moved a reference-matching series off it is damage, a
#     write that moved it onto it is a fix, and a block with no reference overlap is
#     *undetermined* rather than guessed;
#   * use the oldest snapshot as the baseline, because the newest one can already
#     contain the write (this is the production timeline of the bank rebasing).

SYM_DAMAGE_WINDOW = "600010.SH"   # closed window moved off the reference
SYM_DAMAGE_OPEN = "600011.SH"     # block from a date to the newest bar, off the reference
SYM_FIX = "600012.SH"             # block that moved onto the reference
SYM_CLEAN = "600013.SH"           # never rewritten at all
SYM_NOREF = "600014.SH"           # rewritten, but no reference coverage to judge it
SYM_GAP = "600015.SH"             # cross-provider window that pre-dates every snapshot

_C_WINDOW = list(range(20, 41))   # 21 rows, closed on both sides
_C_OPEN = list(range(30, 95))     # 65 rows: the block the write actually touched
_C_TAIL = list(range(95, 100))    # 5 bars appended *after* the write (never scaled)
_C_REF_START = 50                 # the reference namespace starts mid-history


def _census_fixture(tmp_path):
    """A cache with one symbol per census verdict, plus two snapshots of different ages."""
    base = _base_frame(n=100)

    window = base.copy()                                   # damage, closed window
    window.loc[_C_WINDOW, ["open", "high", "low", "close"]] *= 0.98

    opened = base.copy()                                   # damage, open-ended block
    opened.loc[_C_OPEN, ["open", "high", "low", "close"]] *= 0.99
    # _C_TAIL stays untouched: those bars arrived after the write, from upstream, and
    # are therefore still on the pre-write basis (the production 2026-10-21 shape).

    fixed = base.copy()                                    # no change to the live series
    # The older snapshot had the *whole* series on a stale basis and the live cache is
    # on the reference: one unbounded block, judged "fix", nothing to repair. (A
    # *bounded* fix block is the 600362.SH shape and is covered by the §14 fixture.)
    stale = base.copy()
    stale[["open", "high", "low", "close"]] *= 1.01

    noref = base.copy()                                    # damage with no reference at all
    noref.loc[_C_WINDOW, ["open", "high", "low", "close"]] *= 0.97

    gap = base.copy()                                      # pre-existing basis gap
    gap_ref = base.copy()
    gap_ref.loc[_C_WINDOW, ["open", "high", "low", "close"]] *= 1.0019

    live = _write_multi_db(
        tmp_path / "cache.duckdb",
        {
            (PROVIDER, SYM_DAMAGE_WINDOW): window,
            (REFERENCE, SYM_DAMAGE_WINDOW): base,
            (PROVIDER, SYM_DAMAGE_OPEN): opened,
            (REFERENCE, SYM_DAMAGE_OPEN): base.iloc[_C_REF_START:].reset_index(drop=True),
            (PROVIDER, SYM_FIX): fixed,
            (REFERENCE, SYM_FIX): base.iloc[_C_REF_START:].reset_index(drop=True),
            (PROVIDER, SYM_CLEAN): base,
            (REFERENCE, SYM_CLEAN): base,
            (PROVIDER, SYM_NOREF): noref,
            (PROVIDER, SYM_GAP): gap,
            (REFERENCE, SYM_GAP): gap_ref,
        },
    )
    # The oldest snapshot is the only one that still holds the pre-incident bits.
    older = _write_multi_db(
        tmp_path / "cache.duckdb.bak",
        {
            (PROVIDER, SYM_DAMAGE_WINDOW): base,
            # The snapshot predates the last bars: only what was stored when the write
            # happened can be restored, and that is exactly the block above.
            (PROVIDER, SYM_DAMAGE_OPEN): base.iloc[:_C_TAIL[0]].reset_index(drop=True),
            (PROVIDER, SYM_FIX): stale,
            (PROVIDER, SYM_CLEAN): base,
            (PROVIDER, SYM_NOREF): base,
            (PROVIDER, SYM_GAP): basis_gap_baseline(base),
        },
    )
    # The newer one already contains the open-ended write (the production trap).
    newer = _write_multi_db(
        tmp_path / "cache.duckdb.bak.newer",
        {
            # The older incident predates this snapshot, so it is still visible ...
            (PROVIDER, SYM_DAMAGE_WINDOW): base,
            # ... while the open-ended write is already inside it (the production trap).
            (PROVIDER, SYM_DAMAGE_OPEN): opened,
            (PROVIDER, SYM_FIX): stale,
            (PROVIDER, SYM_CLEAN): base,
            (PROVIDER, SYM_NOREF): noref,
            (PROVIDER, SYM_GAP): basis_gap_baseline(base),
        },
    )
    return live, older, newer, base, window, opened, stale, gap_ref


def basis_gap_baseline(base):
    """The pre-existing basis gap is present *before* any incident: same as the live rows."""
    return base


def _census(db, baseline, symbols=None):
    con = audit.connect_read_only(db)
    con_base = audit.connect_read_only(baseline)
    try:
        return audit.census(
            con,
            con_base,
            PROVIDER,
            REFERENCE,
            symbols,
            baseline_label=os.path.basename(baseline),
        )
    finally:
        con.close()
        con_base.close()


def test_census_scans_every_symbol_and_judges_direction(tmp_path):
    live, older, newer, base, window, opened, stale, _ = _census_fixture(tmp_path)
    records = {r.symbol: r for r in _census(live, older)}

    assert set(records) == {SYM_DAMAGE_WINDOW, SYM_DAMAGE_OPEN, SYM_FIX, SYM_NOREF}
    assert SYM_CLEAN not in records, "an untouched symbol must never be reported"

    closed = records[SYM_DAMAGE_WINDOW]
    assert closed.direction == "damage"
    assert closed.bounded is True
    assert closed.rows == len(_C_WINDOW)
    assert closed.factor == pytest.approx(0.98, rel=1e-12)
    assert closed.spread <= 1e-9
    assert closed.volume_unchanged is True
    # ref_rows counts the reference sessions *inside* the block, not the whole series.
    assert closed.ref_rows == len(_C_WINDOW)

    open_ended = records[SYM_DAMAGE_OPEN]
    assert open_ended.direction == "damage"
    assert open_ended.bounded is False, "the block runs to the newest bar, it is not a window"
    assert open_ended.rows == len(_C_OPEN)
    assert open_ended.right_pp is None
    # The judgement is level-based: the pre-incident series sat on the reference.
    assert open_ended.ratio_old == pytest.approx(1.0, abs=1e-9)
    assert open_ended.ratio_new == pytest.approx(0.99, rel=1e-9)
    assert open_ended.ref_rows == _C_OPEN[-1] - _C_REF_START + 1

    assert records[SYM_FIX].direction == "fix"
    assert records[SYM_FIX].ratio_new == pytest.approx(1.0, abs=1e-9)

    undetermined = records[SYM_NOREF]
    assert undetermined.direction == "undetermined"
    assert undetermined.ref_rows == 0
    assert "reference" in undetermined.reason


def test_census_on_the_newer_snapshot_already_contains_the_write(tmp_path):
    """The production trap: a snapshot taken *after* the write proves nothing."""
    live, older, newer, *_ = _census_fixture(tmp_path)
    older_symbols = {r.symbol for r in _census(live, older)}
    newer_symbols = {r.symbol for r in _census(live, newer)}

    assert SYM_DAMAGE_OPEN in older_symbols
    assert SYM_DAMAGE_OPEN not in newer_symbols, "the newer snapshot already holds the write"
    assert SYM_DAMAGE_WINDOW in newer_symbols, "a closed window is still visible"


def test_judge_rewrite_direction_prefers_the_reference_matching_side():
    base = _base_frame(n=60)
    start, end = base["date"].iloc[10], base["date"].iloc[40]
    reference = base.copy()

    damage = base.copy()
    damage.loc[10:40, ["open", "high", "low", "close"]] *= 0.98
    verdict = audit.judge_rewrite_direction(damage, base, reference, start, end)
    assert verdict[0] == "damage"
    assert verdict[2] == pytest.approx(1.0, abs=1e-9)   # old level
    assert verdict[3] == pytest.approx(0.98, rel=1e-9)  # new level

    fix = base.copy()
    stale = base.copy()
    stale.loc[10:40, ["open", "high", "low", "close"]] *= 0.98
    verdict = audit.judge_rewrite_direction(fix, stale, reference, start, end)
    assert verdict[0] == "fix"

    # No reference overlap at all: refuse to guess instead of assuming the snapshot wins.
    verdict = audit.judge_rewrite_direction(damage, base, base.iloc[0:0], start, end)
    assert verdict[0] == "undetermined"
    assert "reference namespace covers only 0 session" in verdict[4]


def test_census_is_read_only_and_reports_coverage(tmp_path):
    live, older, *_ = _census_fixture(tmp_path)
    before = (os.stat(live).st_mtime_ns, os.stat(live).st_size, _file_bytes(live))
    _census(live, older)
    after = (os.stat(live).st_mtime_ns, os.stat(live).st_size, _file_bytes(live))
    assert before == after
    assert not os.path.exists(live + ".wal")

    con = audit.connect_read_only(live)
    try:
        coverage = audit.census_coverage(con, PROVIDER, REFERENCE)
    finally:
        con.close()
    assert coverage["symbols"] == 6
    assert coverage["no_reference"] == [SYM_NOREF]
    assert coverage["thin_reference"] == []
    assert coverage["judgeable"] == 5


def test_census_cli_reports_counts_and_exits_actionable(tmp_path, capsys):
    live, older, *_ = _census_fixture(tmp_path)
    code = audit.main(["--db", live, "--baseline", older, "--provider", PROVIDER,
                       "--reference-provider", REFERENCE, "--census", "--all-symbols",
                       "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == audit.EXIT_ANOMALY
    assert data["counts"] == {"damage": 2, "fix": 1, "undetermined": 1}
    assert data["coverage"]["symbols"] == 6
    assert data["per_baseline_blocks"][os.path.basename(older)] == 4
    assert len(data["records"]) == 4


# ---------------------------------------------------------------------------
# Repair: the census shapes (open-ended block, pre-existing gap, direction gate)
# ---------------------------------------------------------------------------


def test_plan_restores_an_open_ended_damage_block_from_the_older_snapshot(tmp_path):
    live, older, newer, base, window, opened, stale, _ = _census_fixture(tmp_path)
    notes = []
    con = audit.connect_read_only(live)
    con_old = audit.connect_read_only(older)
    con_new = audit.connect_read_only(newer)
    try:
        ops = repair.build_plan(
            con, con_old, [SYM_DAMAGE_OPEN, SYM_FIX], PROVIDER, REFERENCE,
            extra_baselines=[(os.path.basename(newer), con_new)], notes=notes,
            # Provenance is a *caller* responsibility (build_plan only receives a
            # connection, so it cannot know the snapshot's filename); the CLI does
            # the same. Without it the label falls back to "baseline" and the
            # assertion below could not tell which snapshot the bits came from.
            baseline_label=os.path.basename(older),
        )
    finally:
        con.close()
        con_old.close()
        con_new.close()

    by_symbol = {op.symbol: op for op in ops}
    assert set(by_symbol) == {SYM_DAMAGE_OPEN}, "a fix must not be planned as a repair"
    op = by_symbol[SYM_DAMAGE_OPEN]
    assert op.kind == "restore"
    assert op.source_kind == "baseline"
    assert os.path.basename(older) in (op.source_provider or "")
    assert (op.start, op.end) == (base["date"].iloc[_C_OPEN[0]], base["date"].iloc[_C_OPEN[-1]])
    assert op.rows == len(_C_OPEN)
    assert op.factor == pytest.approx(1.0 / 0.99, rel=1e-12)
    for d, vals in op.expected.items():
        row = base[base["date"] == d].iloc[0]
        assert vals == (row["open"], row["high"], row["low"], row["close"])
    assert any("fix" in n and SYM_FIX in n for n in notes), notes


def test_plan_skips_a_pre_existing_cross_provider_basis_gap(tmp_path):
    """A closed Fuyao↔reference step that no snapshot ever changed is not damage."""
    live, older, *_ = _census_fixture(tmp_path)
    notes = []
    con = audit.connect_read_only(live)
    con_old = audit.connect_read_only(older)
    try:
        ops = repair.build_plan(con, con_old, [SYM_GAP], PROVIDER, REFERENCE, notes=notes)
    finally:
        con.close()
        con_old.close()
    assert ops == []
    assert any("pre-existing basis gap" in n for n in notes), notes


def test_plan_never_repairs_a_symbol_without_reference_coverage(tmp_path):
    live, older, *_ = _census_fixture(tmp_path)
    con = audit.connect_read_only(live)
    con_old = audit.connect_read_only(older)
    try:
        ops = repair.build_plan(con, con_old, [SYM_NOREF], PROVIDER, REFERENCE)
    finally:
        con.close()
        con_old.close()
    assert ops == [], "undetermined direction must not be auto-repaired"


def test_admit_write_refuses_undoing_a_write_that_aligned_the_series(tmp_path):
    live, older, newer, base, window, opened, stale, _ = _census_fixture(tmp_path)
    con = audit.connect_read_only(live)
    con_old = audit.connect_read_only(older)
    try:
        # Hand-build the *wrong* op: restore SYM_FIX from the stale snapshot. The live
        # series is the aligned one, so undoing it would re-create a 1 % offset.
        start, end = base["date"].iloc[_C_OPEN[0]], base["date"].iloc[_C_OPEN[-1]]
        stale_rows = stale[(stale["date"] >= start) & (stale["date"] <= end)]
        bad = repair.RepairOp(
            symbol=SYM_FIX,
            provider=PROVIDER,
            kind="restore",
            start=start,
            end=end,
            rows=len(stale_rows),
            factor=1.01,
            source_provider="stale snapshot",
            source_kind="baseline",
            expected={
                r["date"]: (r["open"], r["high"], r["low"], r["close"])
                for _, r in stale_rows.iterrows()
            },
        )
        with pytest.raises(repair.RepairRefused, match="direction is"):
            repair.admit_write(con, bad, REFERENCE, con_old)
    finally:
        con.close()
        con_old.close()


def test_apply_restores_an_open_ended_block_and_keeps_the_tail(tmp_path, capsys):
    live, older, newer, base, window, opened, stale, _ = _census_fixture(tmp_path)
    pre = _file_bytes(live)
    code = repair.main([
        "--db", live, "--baseline", older,
        "--additional-baseline", newer,
        "--provider", PROVIDER, "--reference-provider", REFERENCE,
        "--symbol", SYM_DAMAGE_OPEN, "--symbol", SYM_DAMAGE_WINDOW,
        "--apply", "--backup-dir", str(tmp_path / "bak"), "--stamp", "census",
    ])
    assert code == repair.EXIT_OK, capsys.readouterr().err
    assert _file_bytes(tmp_path / "bak" / f"{os.path.basename(live)}.bak.census") == pre

    got = _dump(live, (SYM_DAMAGE_OPEN, SYM_DAMAGE_WINDOW))
    for sym, rows in ((SYM_DAMAGE_OPEN, _C_OPEN), (SYM_DAMAGE_WINDOW, _C_WINDOW)):
        frame = got[sym]
        for i in rows:
            for col in ("open", "high", "low", "close"):
                assert frame[col].iloc[i] == base[col].iloc[i]
            assert frame["volume"].iloc[i] == base["volume"].iloc[i]
        # No bar was added, dropped or re-dated.
        assert frame["date"].tolist() == base["date"].tolist()

    # The bars *after* the open-ended block were appended after the write and must be
    # left alone: the restore is history-only, and those bars already sit on the
    # pre-write basis (which is why restoring the block does not create a tail seam).
    for i in _C_TAIL:
        for col in ("open", "high", "low", "close"):
            assert got[SYM_DAMAGE_OPEN][col].iloc[i] == opened[col].iloc[i]

    con = audit.connect_read_only(live)
    try:
        for sym in (SYM_DAMAGE_OPEN, SYM_DAMAGE_WINDOW):
            seams = audit.detect_cross_provider_seams(
                audit.load_bars(con, PROVIDER, sym),
                audit.load_bars(con, REFERENCE, sym),
                sym, PROVIDER, REFERENCE,
            )
            assert seams == []
    finally:
        con.close()


def test_repair_census_constants_match_the_documented_set():
    assert repair._CENSUS_SYMBOLS == (
        "600660.SH", "600362.SH", "601225.SH", "601288.SH", "601398.SH"
    )
    names = [os.path.basename(p) for p in repair._DEFAULT_BASELINES]
    assert names == ["cache.duckdb.bak", "cache.duckdb.bak.20261022-213155"]
    # The oldest snapshot must come first: it is the only pre-incident reference for the
    # three banks, and the census/plan both rely on that ordering.
    assert repair._DEFAULT_BASELINES[0].endswith(names[0])


# ---------------------------------------------------------------------------
# L2.28: the sync writer obeys the same append-only criterion as the repair path
# ---------------------------------------------------------------------------


def test_sync_write_gate_matches_the_repair_append_only_criterion():
    """One contract, two artefacts: the sync and the repair must agree on "append only".

    `scripts/sync_china_5m_and_indices.py` gained the writer-side gate this task (`L2.28`)
    added; `scripts/repair_qfq_alignment.py` already had `assert_append_only` (§14.5). If the
    two ever disagree, the sync could quietly do what the repair path refuses to do, so the
    boundary is pinned on both sides at once: `write_start <= MAX(date)` is a refusal -- never
    a "shift the frame to fit" adjustment.
    """
    from scripts import sync_china_5m_and_indices as sync

    sessions = _sessions(_N)
    existing_max = sessions[-1]
    for write_start in (sessions[-1], sessions[-2], sessions[0]):
        with pytest.raises(repair.RepairRefused, match="append-only contract violated"):
            repair.assert_append_only(write_start, existing_max)
        with pytest.raises(sync.QfqWriteRefused, match="append-only contract violated"):
            sync.assert_append_only(write_start, existing_max, symbol=SYMBOL, provider=PROVIDER)

    after_max = existing_max + datetime.timedelta(days=1)
    repair.assert_append_only(after_max, existing_max)  # both accept the ordinary tail
    sync.assert_append_only(after_max, existing_max, symbol=SYMBOL, provider=PROVIDER)
