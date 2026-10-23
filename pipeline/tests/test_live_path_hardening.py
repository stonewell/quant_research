"""Regression tests for the 2026-10-22 live-trading defect batch (six coupled fixes).

Forensics on the 2026-10-22 live `chan_four_state_blend` Stage-2 run produced six
defects, all fixed in `scripts/run_live_strategy.py`,
`scripts/check_live_portfolio_health.py`, `common/position_lifecycle.py` and the
new `common/freshness.py`. Each test below pins one of them:

FIX 1 (honesty of 5m reporting) -- an EMPTY 5m frame used to be reported as
    "成功加载并聚合 5m 日内特征", and the Stage-1 health gate turned the resulting
    all-NaN features into NaN-substituted defaults (breadth 1.0 = a PASSING
    value) so a gate that never ran looked like a comfortably-passing gate.
FIX 2 (stale price) -- mark-to-market closes were taken unconditionally from the
    last available bar, so six symbols whose cache ended on 2026-10-21 were
    priced at yesterday's close while `as_of_date` was 2026-10-22, silently
    corrupting NAV/weights/share counts.
FIX 3/4 (invisible PENDING_ENTRY cancellation) -- a PENDING_ENTRY asset whose
    entry signal vanished returned a bare HOLD, was reclassified NO_POSITION and
    then filtered out of the ticket entirely (601857.SH disappeared).
FIX 5 (invisible streak reset) -- `entry_signal_streak` is persisted sparsely
    (only streak > 0), so a reset to 0 became an ABSENT key in
    `account_state.json` and left no log line.
FIX 6 (thrust attribution measured against the post-lifecycle target) -- the
    macro breadth-thrust residual was computed from `tgt_w` AFTER
    `step_position_lifecycle()` had flattened it, so `macro_breadth_thrust`
    vanished from `detail_reason.dimension_signals` for exactly the symbols
    whose allocation had been suppressed.

100% offline: synthetic provider only, no network, no real market data, and no
writes outside pytest's `tmp_path`.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common.data import SyntheticDataProvider
from common.freshness import StalePriceError, is_stale_bar, last_bar_session, latest_close


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_AS_OF_FIRST = "2024-05-09"   # PENDING_ENTRY confirmation day 1
_AS_OF_SECOND = "2024-05-10"  # entry signal gone -> cancellation


def _write_universe(tmp_path, symbols):
    path = str(tmp_path / "universe.txt")
    with open(path, "w") as f:
        f.write("\n".join(symbols) + "\n")
    return path


def _write_json(tmp_path, name, payload):
    path = str(tmp_path / name)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def _live_args(
    tmp_path,
    universe_file,
    state_file,
    as_of_date,
    extra=None,
    strategy="equal_weight",
):
    cmd = [
        "--strategy", strategy,
        "--universe-file", universe_file,
        "--account-state-file", state_file,
        "--output-dir", str(tmp_path),
        "--as-of-date", as_of_date,
        "--start-date", "2024-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--no-cache",
    ]
    if extra:
        cmd.extend(extra)
    return cmd


@pytest.fixture
def patched_weights():
    """Patch EqualWeightAllocation.generate_weights with a caller-supplied mapping.

    Yields a setter `(weights, first_day_only=None)` so a test can give the
    universe a deterministic target and then change it for the second run.
    """
    from unittest.mock import patch

    from common.allocation_templates import EqualWeightAllocation

    orig_gen = EqualWeightAllocation.generate_weights
    state = {"weights": {}, "active": False}

    def mock_gen(self, universe, params=None):
        df = orig_gen(self, universe, params)
        if not state["active"]:
            return df
        for sym, w in state["weights"].items():
            if sym in df.columns:
                df[sym] = w
        return df

    def setter(weights):
        state["weights"] = dict(weights)
        state["active"] = True

    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        yield setter


def _run_pending_entry_day_one(tmp_path, patched_weights, health_report=None):
    """Run 1 of the 601857 regression: create a PENDING_ENTRY confirmation.

    Returns `(order_df, state_file)`; the symbol is queued at 1/2 confirmations.
    """
    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings.json", {"BIL": 100000})
    state_file = str(tmp_path / "account_state.json")
    if health_report is not None:
        _write_json(tmp_path, "stage1_health_report.json", health_report)

    patched_weights({"600000.SH": 0.0, "601857.SH": 0.30, "BIL": 0.70})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_FIRST,
        extra=[
            "--current-holdings-file", holdings_file,
            "--entry-confirm-bars", "2",
            "--exit-confirm-bars", "1",
        ],
    )
    order_df = live_main(args)
    return order_df, state_file, universe_file, holdings_file


# ---------------------------------------------------------------------------
# FIX 2 / common/freshness.py -- single owner of freshness policy
# ---------------------------------------------------------------------------


def _stale_frame():
    """A deliberately ancient daily frame -- the 2026-10-22 incident shape."""
    idx = pd.bdate_range("2019-01-01", periods=40)
    return pd.DataFrame(
        {
            "Open": 10.0,
            "High": 10.5,
            "Low": 9.5,
            "Close": [10.0 + 0.01 * i for i in range(40)],
            "Volume": 1_000_000,
        },
        index=idx,
    )


def test_latest_close_without_expected_session_never_raises():
    """`expected_session=None` is the documented legacy never-raise path.

    This is the invariant that keeps backtests wall-clock independent: a
    historically-stale frame must still yield its last close when no session
    expectation is supplied. `strict=True` must NOT change that -- the session
    argument, not the strictness, decides whether anything can be stale.
    """
    df = _stale_frame()
    universe = {"AAA": df}
    for strict in (True, False):
        price = latest_close(universe, "AAA", None, strict)
        assert isinstance(price, float)
        assert price == pytest.approx(float(df["Close"].iloc[-1]))
    # And the comparison primitive agrees: no expectation => never stale.
    assert is_stale_bar(df, None) is False


def test_latest_close_strict_raises_on_stale_bar_and_loose_warns(capsys):
    """Strict => StalePriceError naming symbol / actual bar / expected session."""
    df = _stale_frame()
    universe = {"AAA": df}
    expected = "2024-05-10"
    real_bar = last_bar_session(df)
    assert real_bar is not None and real_bar < expected

    with pytest.raises(StalePriceError) as excinfo:
        latest_close(universe, "AAA", expected, strict=True)
    msg = str(excinfo.value)
    assert "AAA" in msg
    assert real_bar in msg
    assert expected in msg
    # StalePriceError subclasses ValueError so generic input-error handlers catch it.
    assert isinstance(excinfo.value, ValueError)

    # Non-strict: return the stale close, but be loud about it.
    price = latest_close(universe, "AAA", expected, strict=False)
    assert price == pytest.approx(float(df["Close"].iloc[-1]))
    out = capsys.readouterr().out
    assert "陈旧价格" in out or "STALE" in out


def test_latest_close_fresh_bar_is_returned_silently(capsys):
    """A bar ON the expected session is accepted with no warning at all."""
    idx = pd.bdate_range("2024-04-01", "2024-05-10")
    df = pd.DataFrame(
        {"Open": 10.0, "High": 10.5, "Low": 9.5, "Close": 10.0, "Volume": 1000},
        index=idx,
    )
    price = latest_close({"AAA": df}, "AAA", "2024-05-10", strict=True)
    assert price == pytest.approx(10.0)
    assert "STALE" not in capsys.readouterr().out
    assert is_stale_bar(df, "2024-05-10") is False


def _truncate_last_bar(symbol):
    """Patch helper: drop the `as_of_date` bar for one symbol only.

    This reproduces the incident exactly -- most symbols are fresh, ONE series
    ends a session early, and `has_market_bars` (which needs only one fresh
    symbol) therefore does not trip the closed-market path.
    """
    orig_fetch = SyntheticDataProvider.fetch_ohlcv

    def mock_fetch(self, sym, start, end, interval="1d"):
        df = orig_fetch(self, sym, start, end, interval)
        if sym == symbol and len(df) >= 2:
            df = df.iloc[:-1]
        return df

    return mock_fetch


def test_live_run_aborts_on_stale_price_by_default(tmp_path, patched_weights):
    """A stale mark-to-market close must ABORT Stage 2, not silently price the book."""
    from unittest.mock import patch

    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings.json", {"600000.SH": 1000, "BIL": 50000})
    state_file = str(tmp_path / "account_state.json")

    patched_weights({"600000.SH": 0.0, "601857.SH": 0.50, "BIL": 0.50})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=["--current-holdings-file", holdings_file],
    )

    with patch.object(SyntheticDataProvider, "fetch_ohlcv", _truncate_last_bar("600000.SH")):
        with pytest.raises(StalePriceError) as excinfo:
            live_main(args)

    msg = str(excinfo.value)
    assert "600000.SH" in msg
    assert _AS_OF_SECOND in msg


def test_allow_stale_prices_downgrades_to_flagged_ticket_row(tmp_path, patched_weights, capsys):
    """`--allow-stale-prices` continues the run but tags every affected ticket row."""
    from unittest.mock import patch

    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings.json", {"600000.SH": 1000, "BIL": 50000})
    state_file = str(tmp_path / "account_state.json")

    patched_weights({"600000.SH": 0.0, "601857.SH": 0.50, "BIL": 0.50})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=["--current-holdings-file", holdings_file, "--allow-stale-prices"],
    )

    with patch.object(SyntheticDataProvider, "fetch_ohlcv", _truncate_last_bar("600000.SH")):
        order_df = live_main(args)

    out = capsys.readouterr().out
    # Loud, operator-visible warning.
    assert "STALE PRICE WARNING" in out

    stale_row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert bool(stale_row["price_stale"]) is True
    assert stale_row["price_bar_date"] == "2024-05-09"
    assert "PRICE_STALE" in stale_row["reason"]

    # Fresh symbols are NOT smeared with the stale flag.
    fresh_rows = order_df[order_df["symbol"] != "600000.SH"]
    assert not any(bool(v) for v in fresh_rows["price_stale"])

    # Machine-readable columns exist on every row so downstream consumers can filter.
    assert "price_stale" in order_df.columns
    assert "price_bar_date" in order_df.columns


# ---------------------------------------------------------------------------
# FIX 3/4 -- PENDING_ENTRY cancellation is a record, not a disappearing row
# ---------------------------------------------------------------------------


def test_pending_entry_cancellation_is_recorded_in_ticket(tmp_path, patched_weights, capsys):
    """601857 regression: the cancelled confirmation must survive into the ticket."""
    from scripts.run_live_strategy import main as live_main

    day_one, state_file, universe_file, holdings_file = _run_pending_entry_day_one(
        tmp_path, patched_weights
    )
    capsys.readouterr()

    # Day 1: the entry is queued and explicitly NOT executed.
    first_row = day_one[day_one["symbol"] == "601857.SH"].iloc[0]
    assert first_row["action"] == "HOLD_FILTERED"
    assert float(first_row["target_weight"]) == pytest.approx(0.0)

    # Day 2: the entry signal is gone -> cancellation.
    patched_weights({"600000.SH": 0.0, "601857.SH": 0.0, "BIL": 1.0})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=[
            "--current-holdings-file", holdings_file,
            "--entry-confirm-bars", "2",
            "--exit-confirm-bars", "1",
        ],
    )
    order_df = live_main(args)
    out = capsys.readouterr().out

    row = order_df[order_df["symbol"] == "601857.SH"]
    assert not row.empty, "cancelled PENDING_ENTRY row must not be filtered out of the ticket"
    row = row.iloc[0]
    assert row["action"] == "CANCEL_PENDING"
    # Economics unchanged: nothing is bought or sold.
    assert float(row["target_weight"]) == pytest.approx(0.0)
    assert int(row["shares"]) == 0
    assert int(row["delta_shares"]) == 0

    # The lost confirmation progress is preserved for the audit trail.
    lifecycle = row["detail_reason"]["lifecycle_decision"]
    assert lifecycle["cancelled_entry_streak"] == 1

    # And it is printed as a record block, not as a Phase 1/2 order.
    assert "PHASE 3b" in out
    assert "CANCEL_PENDING" in out


# ---------------------------------------------------------------------------
# FIX 5 -- a streak reset is persisted as an explicit 0 and logged
# ---------------------------------------------------------------------------


def test_entry_streak_reset_is_persisted_and_logged(tmp_path, patched_weights, capsys):
    from scripts.run_live_strategy import main as live_main

    day_one, state_file, universe_file, holdings_file = _run_pending_entry_day_one(
        tmp_path, patched_weights
    )
    capsys.readouterr()

    with open(state_file) as f:
        state_after_day_one = json.load(f)
    assert state_after_day_one["entry_signal_streak"]["601857.SH"] == 1

    patched_weights({"600000.SH": 0.0, "601857.SH": 0.0, "BIL": 1.0})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=[
            "--current-holdings-file", holdings_file,
            "--entry-confirm-bars", "2",
            "--exit-confirm-bars", "1",
        ],
    )
    live_main(args)
    out = capsys.readouterr().out

    # The reset is now an EXPLICIT 0 (was: key silently disappeared).
    with open(state_file) as f:
        state_after_day_two = json.load(f)
    assert "601857.SH" in state_after_day_two["entry_signal_streak"]
    assert state_after_day_two["entry_signal_streak"]["601857.SH"] == 0
    # ...and it is logged with the value it lost.
    assert "entry_signal_streak reset: 601857.SH (was 1)" in out

    # Day-one ticket still executed nothing for the queued symbol.
    assert int(day_one[day_one["symbol"] == "601857.SH"].iloc[0]["shares"]) == 0


# ---------------------------------------------------------------------------
# FIX 6 -- thrust attribution reads the PRE-lifecycle target
# ---------------------------------------------------------------------------


def test_thrust_attribution_survives_lifecycle_flattening(tmp_path, patched_weights):
    """`macro_breadth_thrust` must not vanish when the lifecycle flattens the target."""
    order_df, _state_file, _u, _h = _run_pending_entry_day_one(
        tmp_path,
        patched_weights,
        health_report={"market_macro": {"thrust": 0.70, "breadth": 0.50}},
    )

    row = order_df[order_df["symbol"] == "601857.SH"].iloc[0]
    # Lifecycle still suppressed the trade (PENDING_ENTRY, day 1 of 2).
    assert row["action"] == "HOLD_FILTERED"
    assert float(row["target_weight"]) == pytest.approx(0.0)
    assert int(row["shares"]) == 0
    assert int(row["delta_shares"]) == 0

    # ...but the audit block now explains WHERE the 30% allocation came from.
    dim = row["detail_reason"]["dimension_signals"]
    assert dim.get("macro_breadth_thrust") == pytest.approx(0.30, abs=1e-6)


# ---------------------------------------------------------------------------
# FIX 1 -- 5m reporting honesty (Stage 2 runner and Stage 1 health gate)
# ---------------------------------------------------------------------------


def test_live_runner_5m_logging_reports_absence_honestly(tmp_path, patched_weights, capsys):
    """An empty 5m universe must be reported as UNAVAILABLE, never as success."""
    from unittest.mock import patch

    import common.intraday_features as intraday_mod
    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings.json", {"BIL": 100000})
    state_file = str(tmp_path / "account_state.json")

    patched_weights({"600000.SH": 0.5, "601857.SH": 0.0, "BIL": 0.5})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=[
            "--current-holdings-file", holdings_file,
            "--enable-intraday-5m",
            "--exit-confirm-bars", "1",
        ],
    )

    with patch.object(intraday_mod, "load_intraday_5m_universe", lambda *a, **k: {}):
        order_df = live_main(args)

    out = capsys.readouterr().out
    assert "5m 日内特征不可用" in out
    assert "provider does not support 5m" in out
    # The old lie is gone.
    assert "成功加载并聚合 5m 日内特征" not in out
    # The run still produced a daily-signal ticket.
    assert not order_df.empty


def test_health_report_marks_intraday_gate_skipped_not_passed(tmp_path, capsys):
    """Stage 1 must report SKIPPED/UNKNOWN (never a passing breadth) with no 5m data."""
    from unittest.mock import patch

    import common.intraday_features as intraday_mod

    health = _load_health_module()
    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    out_dir = str(tmp_path / "reports")

    args = [
        "--universe-file", universe_file,
        "--output-dir", out_dir,
        "--as-of-date", "2024-05-10",
        "--start-date", "2024-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--peak-nav", "100000",
        "--enable-intraday-5m",
    ]

    with patch.object(intraday_mod, "load_intraday_5m_universe", lambda *a, **k: {}):
        result = health["main"](args)

    out = capsys.readouterr().out
    info = result["intraday_5m_info"]

    # Nothing measured => nothing claimed.
    assert info["intraday_features_available"] is False
    assert info["distribution_gate_status"] == "SKIPPED"
    assert info["intraday_vwap_breadth"] is None
    assert info["afternoon_return_mean"] is None
    assert info["afternoon_vwap_breakdown_ratio"] is None
    assert info["distribution_alert"] is None
    # No scaling applied, and that must not be read as "gate passed".
    assert info["intraday_buy_scale"] == pytest.approx(1.0)
    assert "SKIPPED" in out

    # Persisted JSON carries the same truth (nulls, not fabricated numbers).
    with open(os.path.join(out_dir, "stage1_health_report.json")) as f:
        report = json.load(f)
    assert report["intraday_features_available"] is False
    assert report["intraday_5m_metrics"]["distribution_gate_status"] == "SKIPPED"
    assert report["intraday_5m_metrics"]["intraday_vwap_breadth"] is None

    # The dashboard must render N/A rather than a fabricated 100.0% breadth.
    assert "N/A" in out


# ---------------------------------------------------------------------------
# Stage-1 directive contract (cross-layer integration, 2026-10-22)
# ---------------------------------------------------------------------------


def _directive_fixtures():
    dd = {
        "tier": "NORMAL",
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 0,
        "linear_equity_scale": 1.0,
        "dd_trough": 0.0,
        "drawdown_pct": 0.0,
        "peak_nav": 1e5,
        "current_nav": 1e5,
        "tier_label": "x",
        "tier1_counter": 0,
        "tier1_max": 15,
    }
    macro = {
        "thrust_active": False,
        "breadth_active": True,
        "thrust": 0.5,
        "thrust_thresh": 0.6,
        "breadth_thresh": 0.3,
        "breadth": 0.6,
        "target_exposure": 0.5,
    }
    vol = {
        "realized_vol_21d": 0.1,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }
    return dd, macro, vol


def test_coarse_directive_covers_every_gate_code():
    """`directive` stays the 3-value enum for EVERY granular `gate_code`.

    The web layer (`execution_service.py`, `storage.py`, `app.js`) and its tests
    only speak GO / CAUTION / NO_GO. Stage 1 emits granular codes, so the
    producer must map them -- otherwise the UI's final `else` branch paints a
    "buy budget halved" caution as a full emergency halt.
    """
    health = _load_health_module()
    coarse = health["coarse_directive"]

    assert coarse("GO") == "GO"
    assert coarse("NO_GO_EMERGENCY") == "NO_GO"
    for code in (
        "CAUTION_DAMPING",
        "CAUTION_DEFENSIVE",
        "CAUTION_RECOVERING",
        "CAUTION_INTRADAY_DISTRIBUTION",
        "CAUTION_INTRADAY_TROUGH",
    ):
        assert coarse(code) == "CAUTION", code
    # Fail safe: an unrecognised (future) code must never claim risk is normal.
    assert coarse("SOMETHING_NEW") == "CAUTION"
    assert coarse(None) == "CAUTION"


@pytest.mark.parametrize(
    "info, expected_code, expect_skipped_line, expect_unknown_line",
    [
        # Gate could not run at all: must NOT emit a CAUTION risk verdict, and
        # must say out loud that it was skipped (never a silent PASS).
        (
            {
                "intraday_features_available": False,
                "unavailable_reason": "no 5m",
                "distribution_alert": None,
            },
            "GO",
            True,
            False,
        ),
        # Partially measured: aggregate unknowable -> explicit UNKNOWN, no verdict.
        (
            {
                "intraday_features_available": True,
                "distribution_gate_status": "UNKNOWN",
                "distribution_alert": None,
            },
            "GO",
            False,
            True,
        ),
        # Genuinely measured alert: the ONLY path allowed to emit a CAUTION code.
        (
            {
                "intraday_features_available": True,
                "distribution_gate_status": "EVALUATED",
                "distribution_alert": True,
            },
            "CAUTION_INTRADAY_DISTRIBUTION",
            False,
            False,
        ),
        # No intraday block at all (flag off) -> plain GO.
        (None, "GO", False, False),
    ],
)
def test_intraday_absence_never_masquerades_as_risk_verdict(
    info, expected_code, expect_skipped_line, expect_unknown_line
):
    """FIX 1 integrity in BOTH directions.

    (a) A missing/unmeasured intraday feature must never produce a PASS.
    (b) It must also never be dressed up as a genuine risk finding -- the old
        NaN-default path did the opposite (it faked a PASS), and a naive "treat
        unmeasured as 0.0" fix would fake an ALERT. Both are lies; only a
        measured detection may emit CAUTION_INTRADAY_*.
    """
    health = _load_health_module()
    dd, macro, vol = _directive_fixtures()

    code, _headline, actions = health["resolve_gate_directive"](dd, macro, vol, info)
    assert code == expected_code
    blob = " ".join(actions)
    assert ("SKIPPED" in blob) is expect_skipped_line
    assert ("gate UNKNOWN" in blob) is expect_unknown_line
    if info is None or not info.get("distribution_alert"):
        assert not code.startswith("CAUTION"), "unmeasured data must not become a risk verdict"


def _load_health_module():
    """Import scripts/check_live_portfolio_health.py as a module (standalone script)."""
    import importlib.util

    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    spec = importlib.util.spec_from_file_location("check_live_portfolio_health_hardening", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "main": mod.main,
        "resolve_gate_directive": mod.resolve_gate_directive,
        "coarse_directive": mod.coarse_directive,
    }


# ---------------------------------------------------------------------------
# Execution hierarchy -- SELLS must still be executable BEFORE BUYS
# ---------------------------------------------------------------------------


def test_execution_phase_ordering_sells_before_buys(tmp_path, patched_weights):
    """Phase order is a risk constraint: proceeds must exist before buys."""
    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601398.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings.json", {"600000.SH": 5000, "BIL": 40000})
    state_file = str(tmp_path / "account_state.json")

    # 600000.SH is liquidated, 601398.SH is bought.
    patched_weights({"600000.SH": 0.0, "601398.SH": 0.50, "BIL": 0.50})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=["--current-holdings-file", holdings_file, "--exit-confirm-bars", "1"],
    )
    order_df = live_main(args)

    actions = list(order_df["action"])
    assert "SELL" in actions
    assert "BUY" in actions
    assert actions.index("SELL") < actions.index("BUY")


# ---------------------------------------------------------------------------
# resolve_exit_cooldown archive heuristic -- CANCEL_PENDING must never anchor
# ---------------------------------------------------------------------------


def _write_archive_ticket(parent, day, rows):
    """Write `parent/<day>/live_trading_ticket.csv` with the given rows.

    Mirrors the two columns `resolve_exit_cooldown` reads from an archived
    ticket (`action`, `target_weight`), nothing more.
    """
    day_dir = parent / day
    day_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(str(day_dir / "live_trading_ticket.csv"), index=False)
    return day_dir


def _resolve_with_calendar_recorder(monkeypatch, symbols, output_dir, as_of_date, default_bars=999):
    """Call `resolve_exit_cooldown` with `check_missing_trading_days` recorded.

    Offline proof of the resolved anchor date: the real
    `check_missing_trading_days(exit_d, as_of_d)` call touches the trading
    calendar, and its call site sits inside `try/except Exception: exit_bars[sym]
    = default_bars` -- a calendar failure would silently look like "no anchor at
    all" and mask the assertion. So we replace it with a recorder returning a
    fixed empty list and assert on the `exit_d` it was handed.
    """
    from scripts import run_live_strategy as rls

    seen = []

    def _recorder(exit_d, as_of_d):
        seen.append(exit_d)
        return []

    monkeypatch.setattr(rls, "check_missing_trading_days", _recorder)
    exit_bars = rls.resolve_exit_cooldown(
        symbols,
        account_state=None,  # exercise the `unresolved` archive-scan path
        output_dir=str(output_dir),
        as_of_date=as_of_date,
        default_bars=default_bars,
    )
    return exit_bars, seen


def test_cancel_pending_row_never_anchors_exit_cooldown(tmp_path, monkeypatch):
    """REGRESSION: a CANCEL_PENDING record must not push the anchor one day later.

    2026-10-22 incident side effect (found in review): CANCEL_PENDING rows are
    now ARCHIVED (previously such rows were dropped as NO_POSITION), and they
    carry `target_weight == 0.0`. The archive heuristic used to read that as a
    full exit, anchoring a cooldown for a symbol that was NEVER held -- and, being
    the newest matching date, it SHADOWED the genuine day-1 exit anchor, moving
    it one day forward and keeping the symbol in COOLDOWN an extra day.

    Without the fix this resolves to day 2 (CANCEL_PENDING), so it cannot pass
    vacuously: the assertion pins the older date that only a correct skip yields.
    """
    day1, day2, as_of = "2024-05-09", "2024-05-10", "2024-05-13"
    _write_archive_ticket(tmp_path, day1, [{"symbol": "AAA", "action": "HOLD_FILTERED", "target_weight": 0.0}])
    _write_archive_ticket(tmp_path, day2, [{"symbol": "AAA", "action": "CANCEL_PENDING", "target_weight": 0.0}])

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of
    )

    assert seen, "the day-1 anchor must reach the calendar call (else the check is vacuous)"
    assert seen == [pd.Timestamp(day1).date()], f"resolved anchor must be day1, got {seen}"
    assert exit_bars["AAA"] == 0


def test_sell_row_still_anchors_at_newest_date(tmp_path, monkeypatch):
    """NO REGRESSION: `SELL` keeps anchoring at the newest matching date (day 2)."""
    day1, day2, as_of = "2024-05-09", "2024-05-10", "2024-05-13"
    _write_archive_ticket(tmp_path, day1, [{"symbol": "AAA", "action": "HOLD_FILTERED", "target_weight": 0.0}])
    _write_archive_ticket(tmp_path, day2, [{"symbol": "AAA", "action": "SELL", "target_weight": 0.0}])

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of
    )

    assert seen == [pd.Timestamp(day2).date()], f"SELL must anchor at day2, got {seen}"
    assert exit_bars["AAA"] == 0


def test_cancel_pending_only_archive_leaves_symbol_unanchored(tmp_path, monkeypatch):
    """A lone CANCEL_PENDING archive row anchors nothing -> `default_bars` fallback."""
    day1, as_of = "2024-05-09", "2024-05-13"
    _write_archive_ticket(tmp_path, day1, [{"symbol": "AAA", "action": "CANCEL_PENDING", "target_weight": 0.0}])

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    assert seen == [], f"a never-held symbol must not produce an exit anchor, got {seen}"
    assert exit_bars["AAA"] == 999
