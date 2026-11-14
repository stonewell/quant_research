"""The 5m availability axis is SEPARATE from the run window (2026-11-12 requirement).

Facts pinned here:

* BaoStock's 5m bars for session T usually appear after ~20:00 market-local, while the
  DAILY bar is ready at close+grace (15:30 China). So "I may write a ticket" and "the 5m
  features are usable" are TWO clocks, and the second one must never move the first.
* `assess_intraday_5m_publication_state` returns exactly three states, built ON TOP of
  `assess_intraday_5m_freshness` (one owner for "do the bars reach the session"): a stale
  frame never becomes usable by waiting past 20:00 -- it becomes `missing`.
* Stage 1 records `pending_publication` + `intraday_5m_pending_publication` +
  `expected_after`, prints the greppable warning, and STILL emits tickets (the daily-bar
  decision is complete); the 5m-gate-dependent action is "rerun after 20:00".
* A strategy that does not enable 5m is untouched at 16:00: no block, no warning.

100% offline: synthetic frames, injected clocks, no network, no `data/runs/**`.
"""

import datetime as dt
import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common.intraday_features import (
    INTRADAY_5M_PUBLICATION_AFTER_LOCAL,
    INTRADAY_5M_PUBLICATION_AVAILABLE,
    INTRADAY_5M_PUBLICATION_CODE_MISSING,
    INTRADAY_5M_PUBLICATION_CODE_PENDING,
    INTRADAY_5M_PUBLICATION_MISSING,
    INTRADAY_5M_PUBLICATION_PENDING,
    assess_intraday_5m_freshness,
    assess_intraday_5m_publication_state,
)

_BEIJING = dt.timezone(dt.timedelta(hours=8))
AS_OF = "2026-11-12"  # Thursday
PREV = "2026-11-11"


def _load_script(module_name, relative_path):
    import importlib.util as _ilu

    path = os.path.join(_REPO_ROOT, relative_path)
    spec = _ilu.spec_from_file_location(module_name, path)
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HEALTH = _load_script(
    "check_live_portfolio_health_5m_publication",
    os.path.join("scripts", "check_live_portfolio_health.py"),
)


def _frame(last_ts, *, n=4):
    """A tiny but well-formed 5m frame ending at `last_ts` (bar CLOSE timestamps)."""
    idx = pd.date_range(end=pd.Timestamp(last_ts), periods=n, freq="5min")
    return pd.DataFrame(
        {
            "Open": 10.0,
            "High": 10.1,
            "Low": 9.9,
            "Close": 10.0,
            "Volume": 1000.0,
        },
        index=idx,
    )


def _stale_frame():
    """Last bar on the PREVIOUS session -- the frozen-cache / not-yet-published shape."""
    return {"AAA": _frame(f"{PREV} 14:55:00")}


def _fresh_frame():
    return {"AAA": _frame(f"{AS_OF} 14:55:00")}


def _at(hour, minute, day=None):
    return dt.datetime.fromisoformat(f"{day or AS_OF}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=_BEIJING
    )


# ---------------------------------------------------------------------------
# 1. 16:00 + 5m missing -> pending_publication, previous-day bars still unusable
# ---------------------------------------------------------------------------
def test_1600_with_missing_5m_is_pending_publication_and_never_uses_yesterday():
    state = assess_intraday_5m_publication_state(
        _stale_frame(), as_of=AS_OF, market="china", now=_at(16, 0)
    )

    assert state["status"] == INTRADAY_5M_PUBLICATION_PENDING == "pending_publication"
    assert state["available"] is False
    assert state["expected_after"] == "20:00"
    assert state["degraded_reason_code"] == "intraday_5m_pending_publication"
    assert state["expected_after"] == INTRADAY_5M_PUBLICATION_AFTER_LOCAL

    # The freshness judgement is NOT weakened: the previous session's bars are still stale,
    # which is exactly why the features stay unusable instead of being silently reused.
    assert state["stale"] is True
    assert state["latest_available_ts"].startswith(PREV)
    assert state["freshness"]["stale"] is True

    # ... and Stage 1 turns that into the documented machine-readable record + warning.
    info = {
        "intraday_features_available": False,
        "symbols_5m_available": 1,
        "symbols_vwap_measured": 0,
        "unavailable_reason": "5m 当日数据尚未发布 …",
    }
    block = _HEALTH.classify_intraday_5m_status(
        {"rows_5m": 0, "intraday_data_last_bar_date": PREV},
        n_5m_symbols_nonempty=1,
        features_measured=0,
        requested=True,
        freshness=state["freshness"],
        publication=state,
        now=dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc),
    )
    assert block["status"] == "pending_publication"
    assert block["degraded"] is True
    assert block["degraded_reason_code"] == "intraday_5m_pending_publication"
    assert block["expected_after"] == "20:00"
    # `enabled` keeps the web contract: real rows on the as-of session -- there are none.
    assert block["enabled"] is False

    code, _headline, actions = _HEALTH.resolve_gate_directive(
        _dd_info(), _macro_info(), _vol_info(), {**info, **block}
    )
    # NOT a refusal: the daily gate verdict is untouched, the 5m gate is SKIPPED/UNKNOWN.
    assert code in ("GO", "CAUTION_DAMPING", "CAUTION_DEFENSIVE", "CAUTION_RECOVERING")
    joined = " ".join(actions)
    assert "SKIPPED / UNKNOWN" in joined
    assert "建议 20:00 后重跑" in joined


# ---------------------------------------------------------------------------
# 2. 20:30 + published -> available
# ---------------------------------------------------------------------------
def test_2030_with_published_5m_is_available():
    state = assess_intraday_5m_publication_state(
        _fresh_frame(), as_of=AS_OF, market="china", now=_at(20, 30)
    )

    assert state["status"] == INTRADAY_5M_PUBLICATION_AVAILABLE == "available"
    assert state["available"] is True
    assert state["degraded_reason_code"] is None
    assert state["stale"] is False
    assert state["latest_available_ts"].startswith(f"{AS_OF}T14:55")
    # The deadline the freshness owner computed is the as-of session's last full bar.
    assert state["freshness"]["expected_last_session"] == AS_OF


# ---------------------------------------------------------------------------
# 3. 20:30 + still missing -> missing, with the existing stale-window code
# ---------------------------------------------------------------------------
def test_2030_still_missing_is_the_stale_window_code():
    state = assess_intraday_5m_publication_state(
        _stale_frame(), as_of=AS_OF, market="china", now=_at(20, 30)
    )

    assert state["status"] == INTRADAY_5M_PUBLICATION_MISSING == "missing"
    assert state["available"] is False
    assert state["degraded_reason_code"] == "intraday_5m_stale_window"
    # One spelling across the two modules (Stage 1's degraded path and this owner).
    assert state["degraded_reason_code"] == INTRADAY_5M_PUBLICATION_CODE_MISSING
    assert state["degraded_reason_code"] == _HEALTH.INTRADAY_5M_CODE_STALE
    assert (
        INTRADAY_5M_PUBLICATION_CODE_PENDING == _HEALTH.INTRADAY_5M_CODE_PENDING
        == "intraday_5m_pending_publication"
    )

    block = _HEALTH.classify_intraday_5m_status(
        {"rows_5m": 0, "intraday_data_last_bar_date": PREV},
        n_5m_symbols_nonempty=1,
        features_measured=0,
        requested=True,
        freshness=state["freshness"],
        publication=state,
        now=dt.datetime(2026, 11, 12, 12, 30, tzinfo=dt.timezone.utc),
    )
    # A post-publication absence stays a STALE CACHE, not "pending".
    assert block["status"] == "degraded_stale_intraday_cache"
    assert block["degraded_reason_code"] == "intraday_5m_stale_window"


# ---------------------------------------------------------------------------
# 4. No 5m enabled at 16:00 -> completely unaffected
# ---------------------------------------------------------------------------
def _load_nav_parity_helpers():
    """Reuse the existing hermetic Stage-1 harness (`test_nav_source_parity._run`)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_nav_source_parity.py")
    spec = importlib.util.spec_from_file_location("nav_source_parity_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage1_reports_pending_publication_end_to_end(tmp_path, monkeypatch, capsys):
    """The STATE reaches `stage1_health_report.json::intraday_5m_metrics` (not just a log)."""
    helpers = _load_nav_parity_helpers()
    import common.cli_utils as cli_utils

    frozen = dt.datetime(2026, 11, 12, 21, 30, tzinfo=dt.timezone.utc)  # 16:30 ET
    real_market_now = cli_utils.market_now

    def _frozen(market, now=None, holidays=None, publication_grace_minutes=None):
        return real_market_now(
            market,
            now=now if now is not None else frozen,
            holidays=holidays,
            publication_grace_minutes=publication_grace_minutes,
        )

    monkeypatch.setattr(cli_utils, "market_now", _frozen)
    monkeypatch.setattr(helpers, "AS_OF", "2026-11-12")

    real_patch = helpers._patch_5m

    def _patch(monkeypatch, loader=None, enricher=None):
        real_patch(monkeypatch, loader=loader, enricher=enricher)
        # Same instant as the frozen clock: before the 20:00 5m publication hour.
        monkeypatch.setattr(helpers._mod, "INTRADAY_5M_NOW_OVERRIDE", frozen)

    monkeypatch.setattr(helpers, "_patch_5m", _patch)

    def _stale_loader(symbols, start=None, end=None, data_provider=None, cutoff_time="14:00"):
        return {s: _frame("2026-11-11 14:55:00") for s in symbols}

    run = helpers._run(
        tmp_path,
        monkeypatch,
        "pending_publication_e2e",
        nav_effective=helpers.FORM_NAV,
        form_nav=helpers.FORM_NAV,
        enable_5m=True,
        loader=_stale_loader,
        enricher=helpers._no_features,
    )
    out = capsys.readouterr().out
    block = run["reported_intraday"]

    assert (
        "[WARN] 5m 当日数据尚未发布（BaoStock 通常 20:00 后可用）；本次运行将不使用 5m 特征"
        in out
    )
    assert block["status"] == "pending_publication"
    assert block["degraded"] is True
    assert block["degraded_reason_code"] == "intraday_5m_pending_publication"
    assert block["expected_after"] == "20:00"
    assert block["available"] is False
    assert block["intraday_features_available"] is False
    # The previous session's bars were NOT reused as if they were today's.
    assert block["stale"] is True
    assert block["latest_available_ts"].startswith("2026-11-11")
    assert "5m 当日数据尚未发布" in block["unavailable_reason"]


def test_strategy_without_5m_is_unaffected_at_1600(tmp_path, monkeypatch, capsys):
    helpers = _load_nav_parity_helpers()
    run = helpers._run(
        tmp_path,
        monkeypatch,
        "no_5m_at_1600",
        nav_effective=helpers.FORM_NAV,
        form_nav=helpers.FORM_NAV,
        enable_5m=False,  # the axis is not even evaluated
    )
    out = capsys.readouterr().out

    # No publication warning, no degraded 5m state: the key exists (frozen schema) but is
    # None, i.e. the whole axis was never evaluated.
    assert "5m 当日数据尚未发布" not in out
    assert "[WARN] 5m" not in out
    assert run["report"]["intraday_5m_metrics"] is None
    assert run["intraday"] is None
    # The daily gate still ran and still produced its own verdict.
    assert run["report"]["gate_code"]


# ---------------------------------------------------------------------------
# 5. ONE spelling across three modules (`R7b` F2 -- identity, not copy-paste)
# ---------------------------------------------------------------------------
def test_status_vocabulary_is_identical_across_the_three_modules():
    """`pending_publication` / `evaluated` / `available` are THREE modules' shared strings.

    They are declared as literals in `common/intraday_features.py`,
    `scripts/check_live_portfolio_health.py` and `scripts/run_live_strategy.py` (no shared
    import: Stage 1 keeps lazy imports and Stage 2 owns its gate vocabulary). These are
    IDENTITY assertions -- not new runtime machinery -- so renaming either constant in one
    module turns this test red instead of silently splitting the vocabulary.
    """
    from scripts import run_live_strategy as stage2

    # "pending_publication": publication owner -> Stage-1 status -> Stage-2 gate state.
    assert (
        INTRADAY_5M_PUBLICATION_PENDING
        == _HEALTH.INTRADAY_5M_STATUS_PENDING
        == stage2.INTRADAY_GATE_PENDING_PUBLICATION
        == "pending_publication"
    )
    # The reason code that travels beside it, in all three modules.
    assert (
        INTRADAY_5M_PUBLICATION_CODE_PENDING
        == _HEALTH.INTRADAY_5M_CODE_PENDING
        == stage2.INTRADAY_GATE_REASON_PENDING
        == "intraday_5m_pending_publication"
    )
    # "evaluated" (status axis) and "available" (publication axis) both stay in Stage-2's
    # measured set, and both spellings still match their owners.
    assert _HEALTH.INTRADAY_5M_STATUS_EVALUATED == "evaluated"
    assert INTRADAY_5M_PUBLICATION_AVAILABLE == "available"
    assert stage2._5M_MEASURED_STATUSES == frozenset(
        {_HEALTH.INTRADAY_5M_STATUS_EVALUATED, INTRADAY_5M_PUBLICATION_AVAILABLE}
    )
    assert stage2._5M_PENDING_STATUSES == frozenset({INTRADAY_5M_PUBLICATION_PENDING})

    # "missing": the publication owner's third state and the freshness owner's stale
    # reason both reach Stage 1's `--publication` help text; Stage 1 spells the stale
    # status with its own longer token, so only the publication string is shared.
    assert INTRADAY_5M_PUBLICATION_MISSING == "missing"
    assert INTRADAY_5M_PUBLICATION_MISSING not in stage2._5M_MEASURED_STATUSES
    assert INTRADAY_5M_PUBLICATION_MISSING not in stage2._5M_PENDING_STATUSES

    # Every Stage-2 token that is neither measured nor pending freezes (`R7`).
    for frozen_token in (
        INTRADAY_5M_PUBLICATION_MISSING,
        _HEALTH.INTRADAY_5M_STATUS_STALE,
        _HEALTH.INTRADAY_5M_STATUS_UNSUPPORTED,
        _HEALTH.INTRADAY_5M_STATUS_UNMEASURED,
    ):
        assert frozen_token not in stage2._5M_MEASURED_STATUSES
        assert frozen_token not in stage2._5M_PENDING_STATUSES


# ---------------------------------------------------------------------------
# Minimal directive inputs (mirrors `test_execution_session_labeling._dashboard_kwargs`)
# ---------------------------------------------------------------------------
def _dd_info():
    return {
        "current_nav": 100000.0,
        "peak_nav": 100000.0,
        "drawdown_pct": 0.0,
        "tier": "NORMAL",
        "tier_label": "⚪ NORMAL",
        "linear_equity_scale": 1.0,
        "freeze_remaining_bars": 0,
        "fast_recovery_nav": False,
        "nav_10d_status": "insufficient_history",
    }


def _macro_info():
    return {
        "breadth": 0.5,
        "breadth_thresh": 0.3,
        "thrust": 0.5,
        "thrust_thresh": 0.6,
        "target_exposure": 0.5,
        "tracked_symbols_count": 2,
        "leaders": [],
        "breadth_active": True,
        "thrust_active": False,
    }


def _vol_info():
    return {
        "vol_elevated": False,
        "realized_vol_21d": 0.1,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
    }
