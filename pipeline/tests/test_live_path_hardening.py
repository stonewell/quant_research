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
    out_dir=None,
):
    cmd = [
        "--strategy", strategy,
        "--universe-file", universe_file,
        "--account-state-file", state_file,
        "--output-dir", str(out_dir if out_dir is not None else tmp_path),
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
# resolve_exit_cooldown archive heuristic -- ONLY `SELL` may anchor a cooldown
# (2026-10-23 narrowing, development_plan.md L2.21; supersedes the 2026-10-22
# CANCEL_PENDING-only hotfix)
# ---------------------------------------------------------------------------


def _write_archive_ticket(parent, day, rows):
    """Write `parent/<day>/live_trading_ticket.csv` with the given rows.

    Mirrors the columns an archived ticket row carries (`action`,
    `target_weight`, optional `current_shares` / `target_shares` / `reason` /
    `blocking_reason`). Since L2.22 the anchor verdict reads `action` AND the
    post-fill share count, so a fixture that means "a real liquidation" should
    carry `target_shares: 0` and one that means "a trim" should carry the shares
    still held; omitting the column entirely is the legacy-tolerance shape.
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
    """REGRESSION: a CANCEL_PENDING record must never SHADOW a genuine day-1 `SELL`.

    2026-10-22 incident side effect (found in review): CANCEL_PENDING rows are
    ARCHIVED (previously such rows were dropped as NO_POSITION) and carry
    `target_weight == 0.0`; the archive heuristic used to read that as a full exit
    for a symbol that was NEVER held and, being the newest matching date, it
    SHADOWED a genuine exit anchor, moving it one day forward.

    NOTE (2026-10-23, L2.21): the anchor condition is now `action == "SELL"` only,
    so the old shape of this test -- a day-1 `HOLD_FILTERED` with `tgt_w == 0.0`
    anchoring the cooldown -- is exactly the phantom anchor that got removed, and
    such a fixture would (correctly) anchor nothing. The regression intent is kept
    by anchoring day 1 with a REAL `SELL` instead: "cancel must not shadow a real
    exit". This one passes before AND after the narrowing by design (it pins
    CANCEL_PENDING transparency, not the phantom removal -- the phantom cases are
    covered by the parametrized `test_non_sell_zero_target_rows_never_anchor_cooldown`
    below, which DOES fail on the pre-narrowing implementation).
    """
    day1, day2, as_of = "2024-05-09", "2024-05-10", "2024-05-13"
    _write_archive_ticket(tmp_path, day1, [{"symbol": "AAA", "action": "SELL", "target_weight": 0.0}])
    _write_archive_ticket(tmp_path, day2, [{"symbol": "AAA", "action": "CANCEL_PENDING", "target_weight": 0.0}])

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of
    )

    assert seen, "the day-1 anchor must reach the calendar call (else the check is vacuous)"
    assert seen == [pd.Timestamp(day1).date()], f"resolved anchor must be day1, got {seen}"
    assert exit_bars["AAA"] == 0


@pytest.mark.parametrize(
    "reason",
    [
        # 1) min-entry-threshold filter -- `common/position_lifecycle.py:458`
        #    returns HOLD_FILTERED with target_weight == 0.0 when the raw target
        #    is below `min_entry_threshold`, i.e. the symbol was never bought.
        "目标仓位未达最低建仓/调仓门槛 (目标 0.4% < 门槛 1%，防微量试错损耗)",
        # 2) PENDING_ENTRY confirmation window -- `position_lifecycle.py:543`
        #    holds 0% while the entry signal is still being confirmed.
        "开仓/再入场信号进入确认期 (第 1/2 日待确认，防脉冲假突破)",
        # 3) market-closed freeze while flat -- `position_lifecycle.py` freeze branch
        #    writes `target_weight = state.current_weight`, which is 0.0 when flat.
        #    (Text updated for L2.23: the freeze reason now says "未评估任何信号"
        #    instead of "冻结维持" -- the label-independence point is unchanged.)
        "非交易日/休市日：休市冻结，未评估任何信号(状态机、确认期、锁定期与冷却期计数器全部冻结，无任何交易指令)",
    ],
    ids=["min_entry_threshold", "pending_entry_confirm", "market_closed_flat"],
)
def test_non_sell_zero_target_rows_never_anchor_cooldown(tmp_path, monkeypatch, reason):
    """A non-`SELL`, zero-target archive row must never anchor the re-entry cooldown.

    HONEST SCOPE NOTE (L2.21) -- read this before counting the parametrization as
    extra coverage. The three `reason` strings are produced by three genuinely
    different upstream branches (min-entry threshold, PENDING_ENTRY confirmation,
    market-closed freeze while flat), but they all reach `resolve_exit_cooldown`
    as the SAME archived ticket shape -- `action == "HOLD_FILTERED"`,
    `target_weight == 0.0` -- and the narrowed anchor condition
    (`scripts/run_live_strategy.py:565`) reads `action` ONLY: never `reason`,
    never `target_weight`. This is therefore ONE code path exercised three times.
    The parametrization proves LABEL INDEPENDENCE (no `reason` text can
    resurrect a phantom anchor), NOT three distinct mechanisms; it is one
    regression guard, not three.

    Pre-2026-10-23 the heuristic anchored on `tgt_w == 0.0`, so every case here
    FAILED -- the symbol was pinned to COOLDOWN -> HOLD_FILTERED right before
    Phase 2 would buy it. Today the `default_bars` fallback (999 = cooling down
    is disabled) wins.
    """
    day1, as_of = "2024-05-09", "2024-05-13"
    _write_archive_ticket(
        tmp_path,
        day1,
        [{
            "symbol": "AAA",
            "action": "HOLD_FILTERED",
            "target_weight": 0.0,
            "reason": reason,
        }],
    )

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    assert seen == [], f"a non-SELL zero-target row must not anchor, got {seen} (reason={reason})"
    assert exit_bars["AAA"] == 999


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
    """A lone CANCEL_PENDING archive row anchors nothing -> `default_bars` fallback.

    Strengthened for L2.21 so it is not vacuous on the narrowed condition: the
    same archive also holds an OLDER `SELL`, so "anchors nothing" can only mean
    "the CANCEL_PENDING row was skipped", and the follow-up assertion pins that
    the older `SELL` is the one that wins. (The lone-CANCEL_PENDING half is the
    2026-10-22 hotfix guard -- it passed before this narrowing too, by design.)
    """
    day0, day1, as_of = "2024-05-08", "2024-05-09", "2024-05-13"
    _write_archive_ticket(tmp_path, day0, [{"symbol": "AAA", "action": "SELL", "target_weight": 0.0}])
    _write_archive_ticket(tmp_path, day1, [{"symbol": "AAA", "action": "CANCEL_PENDING", "target_weight": 0.0}])

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    assert seen == [pd.Timestamp(day0).date()], (
        f"the CANCEL_PENDING row must be transparent and the older SELL must anchor, got {seen}"
    )
    assert exit_bars["AAA"] == 0

    # And with NO `SELL` anywhere: nothing anchors at all.
    only_cancel = tmp_path / "cancel_only"
    _write_archive_ticket(only_cancel, day1, [{"symbol": "AAA", "action": "CANCEL_PENDING", "target_weight": 0.0}])

    exit_bars2, seen2 = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], only_cancel / as_of, as_of, default_bars=999
    )

    assert seen2 == [], f"a never-held symbol must not produce an exit anchor, got {seen2}"
    assert exit_bars2["AAA"] == 999


# ---------------------------------------------------------------------------
# L2.22 (decided 2026-10-23) -- only a FULL liquidation anchors the re-entry
# cooldown; a partial trim, and a market-closed freeze, must never restart it.
#
# PRODUCT RULING (verbatim): "不锚定。只有持仓清零才算退出。部分减仓不重置冷却期。"
# So the anchor needs three independent facts at once:
#   (a) a real SELL executed            -- `is_liquidation_event`,
#   (b) the position is GONE afterwards -- post-trade shares == 0 (share truth),
#   (c) the row was EVALUATED           -- `blocking_reason != MARKET_CLOSED_FREEZE`.
# `target_weight == 0.0` may NEVER be reverse-read as a liquidation again.
# ---------------------------------------------------------------------------


def _synthetic_close(symbol, as_of="2024-05-10"):
    """Deterministic synthetic last close -- offline, no network."""
    df = SyntheticDataProvider().fetch_ohlcv(symbol, "2024-01-01", as_of)
    return float(df["Close"].iloc[-1])


def _l222_weight(symbol, shares, nav=100000.0, as_of="2024-05-10"):
    """Target weight that corresponds to holding exactly `shares` of `symbol`."""
    return (shares * _synthetic_close(symbol, as_of)) / nav


def _l222_run(
    tmp_path,
    patched_weights,
    as_of,
    weights,
    held_shares,
    state_file,
    extra=None,
    out_dir=None,
):
    """One offline live-runner invocation with a fixed brokerage holding."""
    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601398.SH", "BIL"])
    holdings_file = _write_json(
        tmp_path, f"holdings_{as_of}.json", {"600000.SH": held_shares}
    )
    patched_weights(weights)
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        as_of,
        extra=["--current-holdings-file", holdings_file] + list(extra or []),
        out_dir=out_dir,
    )
    return live_main(args)


def _l222_state(state_file):
    with open(state_file) as f:
        return json.load(f)


def test_partial_trim_does_not_anchor_reentry_cooldown(tmp_path, patched_weights):
    """L2.22 case 1: hold 1000 -> sell down to 600 (still held) => cooldown NOT reset.

    Two anchor paths are checked, because a partial trim can leak into either:

      1. the ticket-writing site (`last_exit_date` in `account_state.json`) --
         MUST stay empty;
      2. the ARCHIVE reconstruction on the next run (`resolve_exit_cooldown` scans
         day-1's `live_trading_ticket.csv`, which holds `action == "SELL"` with
         `target_shares == 600`). Under the pre-L2.22 reader that row anchored a
         cooldown dated day 1 and the day-2 ticket reported
         `bars_since_exit == 1`; the fix requires zero post-fill shares, so day 2
         must fall back to the "no known exit" sentinel (999).

    OLD IMPLEMENTATION: path 2 FAILED -- `bars_since_exit` was 1 (anchored on the
    day-1 PARTIAL trim), i.e. the still-held asset's cooldown clock was restarted.
    Path 1 could not fail on the old code because a partial trim carries
    `target_shares > 0` and therefore never entered the zero-target anchor branch;
    it is pinned here as the semantic guard the ruling actually asks for.
    """
    state_file = str(tmp_path / "account_state.json")
    _write_json(tmp_path, "account_state.json", {"holding_bars": {"600000.SH": 10}})
    day1, day2 = "2024-05-10", "2024-05-13"

    w1 = _l222_weight("600000.SH", 600, as_of=day1)
    order_df = _l222_run(
        tmp_path,
        patched_weights,
        day1,
        {"600000.SH": w1, "601398.SH": 0.0, "BIL": 1.0 - w1},
        1000,
        state_file,
        extra=["--exit-confirm-bars", "1"],
        out_dir=tmp_path / day1,
    )

    row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert row["action"] == "SELL", f"expected a real trim SELL, got {row['action']}"
    assert int(row["current_shares"]) == 1000
    # The position did NOT go to zero: this is a partial de-risking, not an exit.
    assert int(row["delta_shares"]) == -400, f"expected -400 shares, got {row['delta_shares']}"
    assert int(row["target_shares"]) == 600
    assert float(row["target_weight"]) > 0.0

    st = _l222_state(state_file)
    assert st["last_exit_date"] == {}, (
        "a partial trim must NOT anchor the re-entry cooldown "
        f"(got {st['last_exit_date']})"
    )
    assert st["exit_bars_ago"] == {}, (
        "no exit anchor => nothing to resolve bars-since-exit for "
        f"(got {st['exit_bars_ago']})"
    )
    assert st["lifecycle_states"]["600000.SH"]["state"] != "COOLDOWN"

    # The day-1 archive really does hold the ambiguous shape this test is about --
    # otherwise the day-2 assertion below would be vacuous.
    archived = pd.read_csv(tmp_path / day1 / "live_trading_ticket.csv")
    arch_row = archived[archived["symbol"] == "600000.SH"].iloc[0]
    assert arch_row["action"] == "SELL"
    assert int(arch_row["target_shares"]) == 600

    # --- day 2: the archived partial trim must NOT be reconstructed as an exit ---
    w2 = _l222_weight("600000.SH", 600, as_of=day2)
    day2_df = _l222_run(
        tmp_path,
        patched_weights,
        day2,
        {"600000.SH": w2, "601398.SH": 0.0, "BIL": 1.0 - w2},
        600,
        state_file,
        extra=["--exit-confirm-bars", "1"],
        out_dir=tmp_path / day2,
    )
    day2_row = day2_df[day2_df["symbol"] == "600000.SH"].iloc[0]
    cooldown = day2_row["detail_reason"]["cooldown_status"]
    assert int(cooldown["bars_since_exit"]) == 999, (
        "the day-1 PARTIAL trim was reconstructed as an exit anchor: "
        f"bars_since_exit={cooldown['bars_since_exit']}"
    )
    assert bool(cooldown["in_cooldown"]) is False
    assert _l222_state(state_file)["last_exit_date"] == {}


def test_archived_partial_trim_never_anchors_cooldown(tmp_path, monkeypatch):
    """L2.22 (archive path): a `SELL` that still HOLDS shares must not anchor.

    An archived partial de-risking row is indistinguishable from a liquidation by
    `action` alone -- both are `SELL`. Only the recorded post-fill quantity
    (`target_shares`) separates them, so the archive reader must require zero.
    OLD IMPLEMENTATION: this anchored (the SELL-only verdict) -> `seen == [day1]`
    and `exit_bars == 0` instead of the "no known exit" fallback.
    """
    day1, as_of = "2024-05-09", "2024-05-13"
    _write_archive_ticket(
        tmp_path,
        day1,
        [{
            "symbol": "AAA",
            "action": "SELL",
            "current_shares": 1000,
            "target_shares": 600,   # 400 shares still held -> a trim, not an exit
            "target_weight": 0.28,
            "blocking_reason": "NONE",
        }],
    )

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    assert seen == [], f"a partial trim must not anchor, got {seen}"
    assert exit_bars["AAA"] == 999


def test_archived_partial_trim_never_shadows_an_older_full_exit(tmp_path, monkeypatch):
    """L2.22 (archive path): a newer partial trim must not move the anchor forward.

    The scan is newest-first, so a trim that "anchored" would hide the genuine
    older liquidation and shorten the elapsed cooldown to one day.
    """
    day0, day1, as_of = "2024-05-08", "2024-05-09", "2024-05-13"
    _write_archive_ticket(
        tmp_path, day0,
        [{"symbol": "AAA", "action": "SELL", "current_shares": 1000, "target_shares": 0,
          "target_weight": 0.0}],
    )
    _write_archive_ticket(
        tmp_path, day1,
        [{"symbol": "AAA", "action": "SELL", "current_shares": 500, "target_shares": 300,
          "target_weight": 0.14}],
    )

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    assert seen == [pd.Timestamp(day0).date()], (
        f"the FULL exit on {day0} must own the anchor, got {seen}"
    )
    assert exit_bars["AAA"] == 0


def test_archived_sell_without_share_columns_keeps_legacy_anchor(tmp_path, monkeypatch):
    """Legacy tolerance: an archive with no `target_shares` column still anchors.

    The column predates the heuristic, so this is a broken/partial-file tolerance
    path. Direction matters: an unverifiable row keeps the pre-L2.22 "SELL anchors"
    verdict -- silently dropping a genuine exit anchor would DISABLE a real
    cooldown, which is the riskier failure.
    """
    day0, as_of = "2024-05-08", "2024-05-13"
    _write_archive_ticket(
        tmp_path, day0,
        [{"symbol": "AAA", "action": "SELL", "target_weight": 0.0}],
    )

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    assert seen == [pd.Timestamp(day0).date()]
    assert exit_bars["AAA"] == 0


def test_full_liquidation_still_anchors_reentry_cooldown(tmp_path, patched_weights):
    """L2.22 case 2 (NO REGRESSION): hold 1000 -> sell ALL => cooldown RESETS.

    The ruling must not disable the cooldown's actual purpose. `SELL` + shares
    after == 0 anchors exactly as before, so the anti-whipsaw brake still works.
    """
    state_file = str(tmp_path / "account_state.json")
    _write_json(tmp_path, "account_state.json", {"holding_bars": {"600000.SH": 10}})

    order_df = _l222_run(
        tmp_path,
        patched_weights,
        "2024-05-10",
        {"600000.SH": 0.0, "601398.SH": 0.0, "BIL": 1.0},
        1000,
        state_file,
        extra=["--guard-latency-bars", "1"],
    )

    row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert row["action"] == "SELL"
    assert int(row["current_shares"]) == 1000
    assert int(row["target_shares"]) == 0  # the independent "position is gone" fact
    assert row["blocking_reason"] == "NONE"

    st = _l222_state(state_file)
    assert st["last_exit_date"] == {"600000.SH": "2024-05-10"}
    assert st["exit_bars_ago"] == {"600000.SH": 0}
    assert st["lifecycle_states"]["600000.SH"]["state"] == "COOLDOWN"


@pytest.mark.parametrize(
    "action, blocking_reason, shares_before, shares_after",
    [
        # Held symbol on a closed session: the freeze branch carries the current
        # weight, and for a flat symbol that is 0.0. NOT an exit.
        ("HOLD_FILTERED", "MARKET_CLOSED_FREEZE", 500, 0),
        # Flat symbol on a closed session -- nothing was evaluated at all.
        ("HOLD_FILTERED", "MARKET_CLOSED_FREEZE", 0, 0),
        # A gated row that happens to carry a zero target is not an exit either.
        ("HOLD_FILTERED", "MIN_ENTRY_THRESHOLD", 0, 0),
        ("HOLD_FILTERED", "EXIT_CONFIRMING", 500, 500),
        ("CANCEL_PENDING", "NONE", 0, 0),
    ],
    ids=["freeze-held", "freeze-flat", "min-entry", "exit-confirming", "cancel-pending"],
)
def test_zero_target_rows_never_anchor_reentry_cooldown(
    action, blocking_reason, shares_before, shares_after
):
    """L2.22 case 3: a freeze / gated / record row never anchors -- regardless of weight.

    The `freeze-held` case is the one with teeth: `(HOLD_FILTERED,
    MARKET_CLOSED_FREEZE, 500 shares -> 0)` satisfies the OLD inline anchor
    (`cur_sh > 0 and (act == "SELL" or tgt_sh == 0)`) purely through its
    `tgt_sh == 0` leg, i.e. it reverse-inferred "liquidated" from a zero target
    share count for a session in which NOTHING ran. It must be False.
    """
    from scripts.run_live_strategy import should_anchor_reentry_cooldown

    assert (
        should_anchor_reentry_cooldown(action, blocking_reason, shares_before, shares_after)
        is False
    ), f"({action}, {blocking_reason}) must never anchor a re-entry cooldown"


def test_market_closed_run_tickets_frozen_rows_without_anchoring_cooldown_l222(
    tmp_path, patched_weights, capsys
):
    """L2.22 case 3 (end-to-end): a closed session anchors nothing, held or flat.

    Same offline freeze fixture as L2.23's test, now with a HELD symbol too. The
    held row carries `target_shares == current_shares` (the freeze branch trades
    nothing), so the practical damage of the old code here was nil -- which is
    exactly why the row-level parametrization above, not this end-to-end run, is
    the one that fails on the old implementation. This still pins the contract at
    the ticket/account-state level: not one closed-session row may write
    `last_exit_date`.
    """
    from unittest.mock import patch

    state_file = str(tmp_path / "account_state.json")
    _write_json(tmp_path, "account_state.json", {"holding_bars": {"600000.SH": 10}})

    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601398.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings_freeze.json", {"600000.SH": 500})
    patched_weights({"600000.SH": 0.10, "601398.SH": 0.004, "BIL": 0.896})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        "2024-05-10",
        extra=["--current-holdings-file", holdings_file, "--allow-stale-prices"],
    )
    with patch.object(SyntheticDataProvider, "fetch_ohlcv", _truncate_every_bar()):
        order_df = live_main(args)
    capsys.readouterr()

    equity = order_df[order_df["symbol"] != "BIL"]
    assert set(equity["blocking_reason"]) == {"MARKET_CLOSED_FREEZE"}
    held_row = equity[equity["symbol"] == "600000.SH"].iloc[0]
    assert int(held_row["current_shares"]) == 500
    assert int(held_row["delta_shares"]) == 0  # frozen: no trade
    assert int(held_row["target_shares"]) == 500

    st = _l222_state(state_file)
    assert st["last_exit_date"] == {}, (
        f"a market-closed freeze must not anchor any exit (got {st['last_exit_date']})"
    )
    assert st["exit_bars_ago"] == {}


def test_repeated_partial_trims_anchor_only_on_the_final_full_exit(tmp_path, patched_weights):
    """L2.22 case 4: 1000 -> 800 -> 600 -> 0; ONLY the last trade anchors.

    Cumulative de-risking must not sneak an anchor in through the smaller trims
    either: the cooldown clock restarts on the day the position is finally gone,
    not on the last day the book was merely smaller. Each run writes its own
    date-named archive directory, so the following run reconstructs cooldowns from
    the real archived tickets: every partial-trim day must report the "no known
    exit" sentinel (`bars_since_exit == 999`), and only the final full exit may
    stamp `last_exit_date`.

    OLD IMPLEMENTATION: run 2 already reported `bars_since_exit == 1` (run 1's
    partial `SELL` row was reconstructed as an exit), so the cooldown restarted
    on the first trim.
    """
    state_file = str(tmp_path / "account_state.json")
    _write_json(tmp_path, "account_state.json", {"holding_bars": {"600000.SH": 10}})

    steps = [
        # (as_of, held before, target shares held after, expect anchor?)
        ("2024-05-10", 1000, 800, False),
        ("2024-05-13", 800, 600, False),
        ("2024-05-14", 600, 0, True),
    ]
    for as_of, held, target, anchors in steps:
        weight = _l222_weight("600000.SH", target, as_of=as_of)
        order_df = _l222_run(
            tmp_path,
            patched_weights,
            as_of,
            {"600000.SH": weight, "601398.SH": 0.0, "BIL": 1.0 - weight},
            held,
            state_file,
            extra=["--exit-confirm-bars", "1"],
            out_dir=tmp_path / as_of,
        )
        row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
        assert row["action"] == "SELL", f"{as_of}: expected SELL, got {row['action']}"
        assert int(row["current_shares"]) == held
        assert int(row["target_shares"]) == target

        # Pre-trade view: prior partial trims must not have been reconstructed as
        # exits (999 == "no known exit"; the cooldown is therefore not running).
        cooldown = row["detail_reason"]["cooldown_status"]
        assert int(cooldown["bars_since_exit"]) == 999, (
            f"{as_of}: an EARLIER partial trim anchored a cooldown, "
            f"bars_since_exit={cooldown['bars_since_exit']}"
        )
        if not anchors:
            # A trimmed-but-still-held asset must not be parked in COOLDOWN (the
            # final run legitimately enters it, having just liquidated).
            assert bool(cooldown["in_cooldown"]) is False

        st = _l222_state(state_file)
        if anchors:
            assert st["last_exit_date"] == {"600000.SH": as_of}, (
                f"{as_of}: the FINAL full exit must anchor, got {st['last_exit_date']}"
            )
            assert st["exit_bars_ago"] == {"600000.SH": 0}
        else:
            assert st["last_exit_date"] == {}, (
                f"{as_of}: the partial trim {held}->{target} must NOT anchor, "
                f"got {st['last_exit_date']}"
            )
            assert st["exit_bars_ago"] == {}, (
                f"{as_of}: a partial trim must leave no bars-since-exit anchor"
            )


# ---------------------------------------------------------------------------
# L2.23 / L2.24 -- market-closed freeze semantics + BlockingReason on the ticket
# ---------------------------------------------------------------------------


def _truncate_every_bar():
    """Patch helper: make EVERY symbol's series stop one session early.

    With no symbol holding an `as_of_date` bar, `has_market_bars` is False and the
    runner's `is_market_open` resolves to False -- i.e. this reproduces the
    non-trading-day path end-to-end (banner only, no early return). Prices are
    therefore stale by construction, which is what `--allow-stale-prices` exists
    for; the run must still produce a fully-formed ticket.
    """
    orig_fetch = SyntheticDataProvider.fetch_ohlcv

    def mock_fetch(self, sym, start, end, interval="1d"):
        df = orig_fetch(self, sym, start, end, interval)
        if len(df) >= 2:
            df = df.iloc[:-1]
        return df

    return mock_fetch


def test_market_closed_run_tickets_frozen_rows_without_anchoring_cooldown(
    tmp_path, patched_weights, capsys
):
    """L2.23 end-to-end: a closed session still archives the row -- as "not evaluated".

    Four defects this pins (all real, all verified by reading the prior code):

    1. the frozen row used to reach the archive at all (it does: the banner at
       `:1401` does not `return`, and NO_POSITION reclassification does not
       whitelist `HOLD_FILTERED`) -- so its WORDING is the only thing standing
       between "market was shut" and "the model decided to keep/flat this";
    2. the script-level min-entry relabel used to overwrite the freeze decision
       on a closed day with a SIGNAL-filter story for any symbol whose target sat
       below the threshold -- claiming a signal was evaluated when none was;
    3. the freeze row must not anchor the re-entry cooldown;
    4. Phase 3 must not render a flat frozen row as "⚪ HOLD ... 维持仓位 0.0%".
    """
    from unittest.mock import patch

    from scripts.run_live_strategy import main as live_main

    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    holdings_file = _write_json(tmp_path, "holdings.json", {"600000.SH": 5000, "BIL": 50000})
    state_file = str(tmp_path / "account_state.json")

    # 601857.SH's target (0.4%) is deliberately BELOW the 5% min-entry threshold:
    # on a closed day the freeze must still win the label (nothing was evaluated).
    patched_weights({"600000.SH": 0.10, "601857.SH": 0.004, "BIL": 0.896})
    args = _live_args(
        tmp_path,
        universe_file,
        state_file,
        _AS_OF_SECOND,
        extra=["--current-holdings-file", holdings_file, "--allow-stale-prices"],
    )

    with patch.object(SyntheticDataProvider, "fetch_ohlcv", _truncate_every_bar()):
        order_df = live_main(args)

    out = capsys.readouterr().out
    assert "MARKET CLOSED" in out  # the banner ran ...
    # ... and did NOT abort the run: every equity row survived into the ticket
    # (no NO_POSITION fold, no silent drop).
    equity = order_df[order_df["symbol"] != "BIL"]
    assert set(equity["symbol"]) == {"600000.SH", "601857.SH"}
    assert set(equity["action"]) == {"HOLD_FILTERED"}
    # (1)+(2): the exact label, for BOTH the held and the flat symbol.
    assert set(equity["blocking_reason"]) == {"MARKET_CLOSED_FREEZE"}
    flat_row = equity[equity["symbol"] == "601857.SH"].iloc[0]
    assert "未达最低建仓" not in flat_row["reason"]
    assert "未评估任何信号" in flat_row["reason"]
    # Freeze = carry the current weight, never a trade.
    held_row = equity[equity["symbol"] == "600000.SH"].iloc[0]
    assert float(held_row["target_weight"]) == pytest.approx(float(held_row["current_weight"]))
    assert int(held_row["delta_shares"]) == 0
    assert int(flat_row["delta_shares"]) == 0

    # (3) No cooldown anchor: nothing was sold, so `last_exit_date` stays empty and
    # the resolved "bars since exit" stays at the "no known exit" fallback.
    with open(state_file) as f:
        st = json.load(f)
    assert st["last_exit_date"] == {}
    # `resolve_exit_cooldown` is only consulted for symbols that DO have an
    # anchor, so an empty map here is the machine-readable proof that not a
    # single closed-session row armed a 2-trading-day cooldown.
    assert st["exit_bars_ago"] == {}

    # (4) Display: the frozen rows get their own "not evaluated" block, and a flat
    # frozen row is never printed as a maintained position.
    assert "PHASE 3a" in out
    assert "休市未评估" in out
    frozen_lines = [ln for ln in out.splitlines() if "601857.SH" in ln and "休市未评估" in ln]
    assert frozen_lines, "the flat frozen row must be printed with its own semantics"
    assert all("维持" not in ln for ln in frozen_lines)
    assert not any("⚪ HOLD" in ln and "601857.SH" in ln for ln in out.splitlines())

    # Archive on disk carries the structured verdict (trailing CSV column + JSON key).
    csv_df = pd.read_csv(tmp_path / "live_trading_ticket.csv")
    assert "blocking_reason" in csv_df.columns
    assert list(csv_df.columns)[-1] == "blocking_reason"
    assert set(csv_df[csv_df["symbol"] != "BIL"]["blocking_reason"]) == {"MARKET_CLOSED_FREEZE"}
    with open(tmp_path / "live_trading_ticket.json") as f:
        records = json.load(f)
    assert all("blocking_reason" in rec for rec in records)


def test_open_day_ticket_carries_blocking_reason_per_branch(tmp_path, patched_weights):
    """L2.24: an ordinary run labels each gated row with its own branch."""
    row = None
    order_df, _state_file, _u, _h = _run_pending_entry_day_one(tmp_path, patched_weights)
    row = order_df[order_df["symbol"] == "601857.SH"].iloc[0]

    # PENDING_ENTRY confirmation window (`position_lifecycle.py` branch), NOT a
    # market freeze and NOT a min-entry filter.
    assert row["action"] == "HOLD_FILTERED"
    assert row["blocking_reason"] == "PENDING_ENTRY_CONFIRM"
    # It is also mirrored inside the structured audit block, so a reader that only
    # keeps `detail_reason` sees the same verdict.
    assert row["detail_reason"]["blocking_reason"] == "PENDING_ENTRY_CONFIRM"

    csv_df = pd.read_csv(tmp_path / "live_trading_ticket.csv")
    assert list(csv_df.columns)[-1] == "blocking_reason"
    csv_row = csv_df[csv_df["symbol"] == "601857.SH"].iloc[0]
    assert csv_row["blocking_reason"] == "PENDING_ENTRY_CONFIRM"


def test_pre_l2_24_archive_without_blocking_reason_column_still_anchors(tmp_path, monkeypatch):
    """Legacy archive (no `blocking_reason` column) must read, and still anchor on SELL.

    A reader that indexed `row["blocking_reason"]` would raise `KeyError` on a
    pre-L2.24 ticket -- and because the scan site sits inside
    `try/except Exception: pass`, the failure would be SILENT: the symbol would
    simply lose its genuine anchor and the 2-day cooldown would not arm. This
    fixture mixes both formats in one archive to prove the tolerant read.
    """
    day0, day1, as_of = "2024-05-08", "2024-05-09", "2024-05-13"
    # Pre-L2.24 shape: 4 columns, no `blocking_reason`.
    _write_archive_ticket(
        tmp_path,
        day0,
        [{
            "symbol": "AAA",
            "action": "SELL",
            "target_weight": 0.0,
            "reason": "策略模型平仓信号持续 2 日确认生效",
        }],
    )
    # New shape: the freeze note carries an explicit label and a 0.0 target.
    _write_archive_ticket(
        tmp_path,
        day1,
        [{
            "symbol": "AAA",
            "action": "HOLD_FILTERED",
            "target_weight": 0.0,
            "reason": "非交易日/休市日：休市冻结，未评估任何信号",
            "blocking_reason": "MARKET_CLOSED_FREEZE",
        }],
    )

    exit_bars, seen = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], tmp_path / as_of, as_of, default_bars=999
    )

    # The legacy SELL still anchors (proves the missing column did not abort the read) ...
    assert seen == [pd.Timestamp(day0).date()], (
        f"a pre-L2.24 archive row must still anchor on SELL, got {seen}"
    )
    assert exit_bars["AAA"] == 0
    # ... and the explicit freeze label never promotes a HOLD_FILTERED row into an anchor.
    newer = tmp_path / "freeze_only"
    _write_archive_ticket(
        newer,
        day1,
        [{
            "symbol": "AAA",
            "action": "HOLD_FILTERED",
            "target_weight": 0.0,
            "blocking_reason": "MARKET_CLOSED_FREEZE",
        }],
    )
    exit_bars2, seen2 = _resolve_with_calendar_recorder(
        monkeypatch, ["AAA"], newer / as_of, as_of, default_bars=999
    )
    assert seen2 == [], f"a market-closed freeze row must not anchor, got {seen2}"
    assert exit_bars2["AAA"] == 999


# ---------------------------------------------------------------------------
# L2.25 -- consumers read `action` + `blocking_reason`, never `target_weight == 0`
# ---------------------------------------------------------------------------


def test_phase3_gated_row_renders_its_blocking_label_not_a_held_position(
    tmp_path, patched_weights, capsys
):
    """A gated row must not be painted as "⚪ HOLD 维持仓位" (display half of the bug).

    L2.23 removed the *frozen* rows from that wording. The other zero-target
    family is identical in shape: a PENDING_ENTRY confirmation row writes
    `target_weight = 0.0` having traded nothing, and a flat symbol then used to be
    printed as "维持仓位: 0.0%" -- i.e. "we still hold 0%". Phase 3 now renders the
    structured blocking label instead, and only claims "持仓不变" when shares are
    actually held.
    """
    order_df, _state, _u, _h = _run_pending_entry_day_one(tmp_path, patched_weights)
    out = capsys.readouterr().out

    row = order_df[order_df["symbol"] == "601857.SH"].iloc[0]
    assert row["action"] == "HOLD_FILTERED"
    assert float(row["target_weight"]) == pytest.approx(0.0)
    assert row["blocking_reason"] == "PENDING_ENTRY_CONFIRM"

    lines = out.splitlines()
    gated_lines = [ln for ln in lines if "601857.SH" in ln and "开仓信号确认中" in ln]
    assert gated_lines, "the gated row must be printed with its structured blocking label"
    for ln in gated_lines:
        assert "⚪ HOLD" not in ln
        assert "维持仓位" not in ln
        # Flat symbol: the honest wording is 未持仓, not "held at 0.0%".
        assert "未持仓" in ln
        assert "无交易指令" in ln


def test_detail_reason_is_liquidation_follows_action_not_target_weight(
    tmp_path, patched_weights, capsys
):
    """Audit attribution: `is_liquidation` == (action == "SELL"), for every row family.

    Pinned because the three families below all carry `target_weight == 0.0` yet
    only one of them actually sells. An auditor reading the weight instead of the
    verdict would count three liquidations where there is one (or, historically,
    arm a re-entry cooldown for positions that were never held).
    """
    from scripts.run_live_strategy import main as live_main

    # --- (a) gated, never traded: HOLD_FILTERED + target_weight 0.0 ------------
    day_one, state_file, universe_file, holdings_file = _run_pending_entry_day_one(
        tmp_path, patched_weights
    )
    capsys.readouterr()
    gated = day_one[day_one["symbol"] == "601857.SH"].iloc[0]
    assert float(gated["target_weight"]) == pytest.approx(0.0)
    assert gated["detail_reason"]["is_liquidation"] is False
    # JSON-safe Python bool (not a numpy.bool_ that would serialise as a string).
    assert type(gated["detail_reason"]["is_liquidation"]) is bool

    # --- (b) cancelled entry: CANCEL_PENDING + target_weight 0.0 --------------
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
    day_two = live_main(args)
    capsys.readouterr()
    cancelled = day_two[day_two["symbol"] == "601857.SH"].iloc[0]
    assert cancelled["action"] == "CANCEL_PENDING"
    assert float(cancelled["target_weight"]) == pytest.approx(0.0)
    assert cancelled["detail_reason"]["is_liquidation"] is False

    # --- (c) the ONE family that really sells --------------------------------
    sell_dir = tmp_path / "sell_case"
    sell_dir.mkdir()
    sell_universe = _write_universe(sell_dir, ["600000.SH", "BIL"])
    sell_holdings = _write_json(sell_dir, "holdings.json", {"600000.SH": 5000, "BIL": 50000})
    sell_state = str(sell_dir / "account_state.json")
    patched_weights({"600000.SH": 0.0, "BIL": 0.97})
    order_df = live_main(
        _live_args(
            sell_dir,
            sell_universe,
            sell_state,
            _AS_OF_SECOND,
            extra=[
                "--current-holdings-file", sell_holdings,
                "--exit-confirm-bars", "1",
            ],
        )
    )
    capsys.readouterr()
    sold = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert sold["action"] == "SELL"
    assert float(sold["target_weight"]) == pytest.approx(0.0)
    assert sold["detail_reason"]["is_liquidation"] is True
    # Same verdict on the persisted ticket. `detail_reason` is a dict inside a CSV
    # cell, so pandas writes its repr (single quotes) -- parse it as a literal.
    import ast as _ast

    csv_df = pd.read_csv(sell_dir / "live_trading_ticket.csv")
    detail = _ast.literal_eval(csv_df[csv_df["symbol"] == "600000.SH"].iloc[0]["detail_reason"])
    assert detail["is_liquidation"] is True


def test_live_linear_equity_scale_portfolio_deleverage(tmp_path, patched_weights, capsys):
    """L2.40: linear_equity_scale < 0.999 triggers is_portfolio_deleverage in Stage 2 runner.

    Verifies:
    1. Risky asset targets are scaled down by linear_equity_scale.
    2. Residual cash is swept to cash proxy (BIL).
    3. Partial trim bypasses inertia gate and tenure lock for held assets with held_bars >= 1.
    4. Order action is SELL with reason containing '组合级去杠杆风控减仓'.
    """
    from scripts.run_live_strategy import main as live_main

    del_dir = tmp_path / "live_deleverage"
    del_dir.mkdir()
    universe_file = _write_universe(del_dir, ["600000.SH", "BIL"])
    holdings_file = _write_json(del_dir, "holdings.json", {"600000.SH": 0.50, "BIL": 0.50})

    state_payload = {
        "linear_equity_scale": 0.95,
        "holding_bars": {"600000.SH": 1},
        "lifecycle_states": {
            "600000.SH": {
                "symbol": "600000.SH",
                "state": "HELD_GESTATION",
                "held_bars": 1,
                "current_weight": 0.50,
                "broker_weight": 0.50,
                "entry_price": 10.0,
                "peak_price": 10.0,
            }
        },
    }
    state_file = _write_json(del_dir, "account_state.json", state_payload)

    # Strategy model wants 50% in 600000.SH, 50% in BIL
    # linear_equity_scale=0.95 scales 600000.SH to 0.50 * 0.95 = 0.475, delta = -0.025 (< default inertia 0.05)
    # and held_bars=1 < min_holding_bars=2.
    # Normally both inertia (0.025 < 0.05) and tenure lock (1 < 2) would block partial trim.
    # Under is_portfolio_deleverage, both are bypassed, order emits SELL trim with reason containing '组合级去杠杆风控减仓'.
    patched_weights({"600000.SH": 0.50, "BIL": 0.50})

    order_df = live_main(
        _live_args(
            del_dir,
            universe_file,
            state_file,
            _AS_OF_SECOND,
            extra=[
                "--current-holdings-file", holdings_file,
                "--min-holding-bars", "2",
            ],
        )
    )
    capsys.readouterr()
    row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert row["action"] == "SELL"
    assert float(row["target_weight"]) == pytest.approx(0.475)
    assert "组合级去杠杆风控减仓" in str(row["reason"])

    bil_row = order_df[order_df["symbol"] == "BIL"].iloc[0]
    assert bil_row["action"] == "SWEEP_CASH"
    assert float(bil_row["target_weight"]) >= 0.525


def test_live_fast_recovery_override_tier3_parity(tmp_path, patched_weights, capsys):
    """L2.42: Stage 2 receiving CAUTION_RECOVERING (even with circuit_breaker_tier=TIER_3_HALT
    and freeze_remaining_bars > 0) does NOT liquidate 100% to 0.0, but scales risky targets
    to 30% (0.30) with is_portfolio_deleverage=True.
    """
    from scripts.run_live_strategy import main as live_main

    rec_dir = tmp_path / "live_recovery"
    rec_dir.mkdir()
    universe_file = _write_universe(rec_dir, ["600000.SH", "BIL"])
    holdings_file = _write_json(rec_dir, "holdings.json", {"600000.SH": 0.50, "BIL": 0.50})

    # Account state has TIER_3_HALT and freeze_remaining_bars=3, but gate_code=CAUTION_RECOVERING
    state_payload = {
        "current_nav": 100000.0,
        "peak_nav": 120000.0,
        "circuit_breaker_tier": "TIER_3_HALT",
        "freeze_remaining_bars": 3,
        "gate_code": "CAUTION_RECOVERING",
        "linear_equity_scale": 0.30,
        "lifecycle_states": {
            "600000.SH": {
                "symbol": "600000.SH",
                "state": "HELD_GESTATION",
                "held_bars": 1,
                "current_weight": 0.50,
                "broker_weight": 0.50,
                "entry_price": 10.0,
                "peak_price": 10.0,
            }
        },
    }
    state_file = _write_json(rec_dir, "account_state.json", state_payload)

    # Strategy model wants 50% in 600000.SH, 50% in BIL
    # Under CAUTION_RECOVERING, risky target scales to 0.50 * 0.30 = 0.15
    patched_weights({"600000.SH": 0.50, "BIL": 0.50})

    order_df = live_main(
        _live_args(
            rec_dir,
            universe_file,
            state_file,
            _AS_OF_SECOND,
            extra=[
                "--current-holdings-file", holdings_file,
                "--min-holding-bars", "2",
            ],
        )
    )
    capsys.readouterr()

    row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    # Must NOT be 0.0 liquidation; must be partial trim to 0.15
    assert row["action"] == "SELL"
    assert float(row["target_weight"]) == pytest.approx(0.15)
    assert "组合级去杠杆风控减仓" in str(row["reason"])

    bil_row = order_df[order_df["symbol"] == "BIL"].iloc[0]
    assert bil_row["action"] == "SWEEP_CASH"
    assert float(bil_row["target_weight"]) >= 0.80

    # Also verify fallback when linear_equity_scale was legacy 0.0 or None in state
    state_legacy = dict(state_payload)
    state_legacy["linear_equity_scale"] = 0.0
    state_legacy_file = _write_json(rec_dir, "account_state_legacy.json", state_legacy)

    order_df_legacy = live_main(
        _live_args(
            rec_dir,
            universe_file,
            state_legacy_file,
            _AS_OF_SECOND,
            extra=[
                "--current-holdings-file", holdings_file,
                "--min-holding-bars", "2",
            ],
        )
    )
    capsys.readouterr()

    row_legacy = order_df_legacy[order_df_legacy["symbol"] == "600000.SH"].iloc[0]
    assert row_legacy["action"] == "SELL"
    assert float(row_legacy["target_weight"]) == pytest.approx(0.15)


