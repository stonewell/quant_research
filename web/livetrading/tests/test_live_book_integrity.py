"""D6 / D7 / D8: the three 2026-10-29 "phantom book" defects in the live engine.

FORENSIC CONTEXT (what actually happened, and why these tests live here)
=======================================================================
The 10-29 archive contained a book the model had already abandoned:

* ``2026-10-27``'s ticket carried ``600584.SH`` / ``600362.SH`` as
  ``HOLD_FILTERED`` / ``COOLDOWN_BLOCKED`` with ``target_shares=100``;
* by ``2026-10-29`` those symbols were FLAT in the lifecycle machine, yet the
  operator's input book kept a 100-share residual each.  ``|target - current|``
  was ~2.49% of NAV, BELOW the 5% inertia threshold, so every round re-emitted
  ``HOLD`` with ``shares: 0``.  A position the model did not want could never be
  liquidated (D6);
* the model-side 20% single-name cap is applied to the RAW weight in
  ``blend_base.py``, but lot rounding happens afterwards, so the DELIVERED
  ``target_shares * price / portfolio_value`` reached 20.28% / 20.01% (D7);
* ``2026-10-29/holdings.json`` (and ``.../215823/holdings.json``) were
  byte-identical to ``2026-10-27/post_holdings.json``: the flat ``<date>/``
  directory was a Stage-1-ONLY seed (``summary.json`` + a pre-deploy
  ``account_state.json``), and because it sorts as its own ``os.walk`` row it
  could outrank ``<date>/<HHMMSS>/`` in the "newest state at-or-before" walk,
  resurrecting pre-trade state (D8).

The fixes are deliberately asymmetric -- DETECTION IS UNCONDITIONAL, the
ORDER-CHANGING HALF IS OPT-IN -- so this file pins BOTH halves for each defect:
the warn-only default must annotate without moving a share, the explicit flag
must actually clamp/sell, and the env kill-switch must win over the flag.

Most of D6/D7 is exercised through the module-level pure helpers in
``scripts/run_live_strategy.py`` (the same functions ``main()`` calls), D8 through
a real ``StorageManager`` over ``tmp_path``.  The package ``conftest.py`` already
re-points every ``LIVETRADING_*`` directory at the test's ``tmp_path``, so no test
can reach the production archive.

Two tests DO drive the real ``main()`` row loop (synthetic provider, pinned
as-of/start dates, every write inside ``tmp_path`` -- still 100% offline), because
the properties they pin are ORDERING properties of that loop that no pure helper
can witness:

* section 4 (2026-10-30 review, finding 1): the D6 guard must stay silent on a row
  the lifecycle machine already decided to liquidate -- the risk exits (hard stop /
  ATR trailing stop / Tier-3 emergency halt / confirmed normal exit) all flip the
  state to COOLDOWN *before* returning the SELL, so after the call
  ``state.is_holding()`` is False and the row is shape-identical to a residual lot;
* section 5 (2026-10-30 review, finding 2): ``raw_tgt_w`` is captured BEFORE
  ``step_position_lifecycle`` mutates the target and the D7 cap runs AFTER the
  purchasing-power rescale -- both single-line orderings that no pure-function
  test can pin (the comments there name the exact one-line change that re-breaks
  each assertion).

Section 6 adds the 2026-10-30 **seed-book** family: the web ``data/`` directory is
configuration only (``current_holdings.json`` / ``account_state.json`` are never
read and never written again), the run archive is the single store, and a deploy
whose seed book cannot be resolved from that archive REFUSES
(``LiveSeedBookMissingError``) before it writes a ``holdings.json`` or launches a
child process.  The incident it prevents: the global copy lagged the archive, so
``600584.SH`` / ``600362.SH`` came back as held after the model had exited them.
"""

import json
import logging
import os
import subprocess
import sys
import types

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from storage import (  # noqa: E402  (bare import: same module identity as server.py)
    _DEFAULT_ACCOUNT_STATE,
    LiveSeedBookMissingError,
    StorageManager,
    has_stage2_artifact,
    instance_subdirs,
    keys_match,
    prefer_instance_state_enabled,
    universe_key_from_path,
)

from execution_service import (  # noqa: E402
    CONFIRMED_EMPTY_BOOK_SOURCE,
    EXPLICIT_BOOK_SOURCE,
    EmptyBookConfirmationRequiredError,
    ExecutionService,
    book_is_empty,
)

from scripts.run_live_strategy import (  # noqa: E402
    DEFAULT_MAX_SINGLE_POSITION,
    MV_CAP_BADGE,
    STALE_LOT_LIQUIDATION_BADGE,
    STALE_LOT_REASON,
    apply_market_value_cap,
    assess_stale_input_lot,
    market_value_weight,
    mv_cap_enforcement_enabled,
    parse_args,
    stale_lot_liquidation_enabled,
)

_STRATEGY = "strat_x"
_UNIVERSE = "univ_y"
#: Incident-shaped constants: a 100-share lot at 25.35 is 20.28% of a 100k book.
_PRICE = 25.35
_PORTFOLIO = 100_000.0


# ---------------------------------------------------------------------------
# 0. CLI surface: every order-changing switch must be OFF by default
# ---------------------------------------------------------------------------


def test_new_order_changing_switches_default_to_off():
    """Predictability first: an un-updated invocation must behave exactly as before.

    ``--max-single-position`` (0.20) is inert on its own (warn-only); only
    ``--enforce-mv-cap`` / ``--liquidate-stale-input-lots`` can change a ticket.
    """
    args = parse_args([])
    assert args.max_single_position == pytest.approx(DEFAULT_MAX_SINGLE_POSITION)
    assert args.enforce_mv_cap is False
    assert args.liquidate_stale_input_lots is False


def test_flags_are_parsed_when_the_operator_asks(monkeypatch):
    monkeypatch.delenv("LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION", raising=False)
    monkeypatch.delenv("LIVETRADING_DISABLE_MV_CAP", raising=False)
    args = parse_args(
        [
            "--liquidate-stale-input-lots",
            "--enforce-mv-cap",
            "--max-single-position",
            "0.15",
        ]
    )
    assert args.liquidate_stale_input_lots is True
    assert args.enforce_mv_cap is True
    assert args.max_single_position == pytest.approx(0.15)
    assert stale_lot_liquidation_enabled(args.liquidate_stale_input_lots) is True
    assert mv_cap_enforcement_enabled(args.enforce_mv_cap) is True


# ---------------------------------------------------------------------------
# 1. D6 -- residual input lots the model no longer wants
# ---------------------------------------------------------------------------


def test_stale_input_lot_is_detected_and_warn_only_by_default():
    """INVARIANT: shares > 0 AND model target 0 AND not a holding => DETECTED.

    Detection must not move a single share: the row is annotated, ``action`` is
    None, and no target/delta is produced.
    """
    verdict = assess_stale_input_lot(
        current_shares=100,
        model_target=0.0,
        is_holding=False,
        lifecycle_action="HOLD",
        enforce=False,
        price=_PRICE,
    )
    assert verdict["detected"] is True
    assert verdict["enforced"] is False
    assert verdict["badge"] == STALE_LOT_LIQUIDATION_BADGE
    assert verdict["shares"] == 100
    assert verdict["action"] is None
    assert verdict["target_shares"] is None
    assert verdict["delta_shares"] is None
    assert STALE_LOT_LIQUIDATION_BADGE in verdict["reason"]
    assert "liquidate-stale-input-lots" in verdict["reason"]


def test_stale_input_lot_enforced_emits_a_full_sell():
    verdict = assess_stale_input_lot(
        current_shares=100,
        model_target=0.0,
        is_holding=False,
        lifecycle_action="HOLD",
        enforce=True,
        price=_PRICE,
    )
    assert verdict["detected"] is True
    assert verdict["enforced"] is True
    assert verdict["action"] == "SELL"
    assert verdict["target_shares"] == 0
    assert verdict["delta_shares"] == -100
    assert verdict["trade_value"] == pytest.approx(-100 * _PRICE)


@pytest.mark.parametrize(
    "current_shares, model_target, is_holding",
    [
        (0, 0.0, False),          # nothing held -- not a residual
        (100, 0.05, False),       # the model still wants it
        (100, 0.0249, False),     # the incident's 2.49% target: model DOES want it
        (100, 0.0, True),         # holding state (PENDING_ENTRY ...) is not "stale"
    ],
)
def test_non_stale_rows_are_never_flagged(current_shares, model_target, is_holding):
    verdict = assess_stale_input_lot(
        current_shares=current_shares,
        model_target=model_target,
        is_holding=is_holding,
        lifecycle_action="HOLD_FILTERED",
        enforce=True,
    )
    assert verdict["detected"] is False
    assert verdict["enforced"] is False
    assert verdict["action"] is None
    assert verdict["reason"] is None


def test_stale_lot_kill_switch_overrides_the_explicit_flag(monkeypatch):
    """The env kill-switch must win even when the operator passed the flag."""
    monkeypatch.setenv("LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION", "1")
    assert stale_lot_liquidation_enabled(True) is False
    # ... and the assessment itself is gate-free, so callers cannot bypass the gate
    # by calling the helper with a stale `enforce` value they computed earlier.
    assert assess_stale_input_lot(
        current_shares=100,
        model_target=0.0,
        is_holding=False,
        lifecycle_action="COOLDOWN_BLOCKED",
        enforce=True,
    )["enforced"] is True


def test_stale_lot_kill_switch_accepts_the_repo_spellings(monkeypatch):
    for value in ("1", "true", "yes", "on", "y", "t", "TRUE"):
        monkeypatch.setenv("LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION", value)
        assert stale_lot_liquidation_enabled(True) is False
    monkeypatch.setenv("LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION", "0")
    assert stale_lot_liquidation_enabled(True) is True


# ---------------------------------------------------------------------------
# 2. D7 -- the DELIVERED (post-lot-rounding) market-value weight
# ---------------------------------------------------------------------------


def _ticket_row(**overrides):
    row = {
        "symbol": "600584.SH",
        "price": _PRICE,
        "current_shares": 0,
        "target_shares": 0,
        "delta_shares": 0,
        "shares": 0,
        "trade_value": 0.0,
        "trade_value_rmb": 0.0,
        "current_weight": 0.0,
        "target_weight": 0.0,
        "delta_weight": 0.0,
        "reason": "",
        "summary_reason": "",
        "detail_reason": {},
    }
    row.update(overrides)
    return row


def _buy_row(target_shares=800, current_shares=0):
    return _ticket_row(
        current_shares=current_shares,
        target_shares=target_shares,
        delta_shares=target_shares - current_shares,
        shares=target_shares - current_shares,
        trade_value=(target_shares - current_shares) * _PRICE,
        target_weight=market_value_weight(target_shares, _PRICE, _PORTFOLIO),
    )


def test_delivered_weight_over_the_cap_is_flagged_warn_only():
    """20.28% is the incident's delivered weight; the default must NOT alter the order."""
    row = _buy_row(target_shares=800)
    out = apply_market_value_cap(row, portfolio_value=_PORTFOLIO, enforce=False)

    assert out["delta_shares"] == 800
    assert out["target_shares"] == 800
    assert out["target_weight"] == pytest.approx(0.2028)
    assert MV_CAP_BADGE in out["reason"]
    assert MV_CAP_BADGE in out["summary_reason"]
    assert "未启用 --enforce-mv-cap" in out["reason"]
    block = out["detail_reason"]["mv_cap"]
    assert block["breached"] is True
    assert block["clamped"] is False
    assert block["enforced"] is False
    assert block["market_value_weight"] == pytest.approx(0.2028)
    assert block["cap"] == pytest.approx(0.20)


def test_enforced_cap_clamps_a_buy_to_whole_lots():
    """800 shares (20.28%) -> 700 shares (17.745%) -- whole lots, so slightly UNDER."""
    row = _buy_row(target_shares=800)
    out = apply_market_value_cap(row, portfolio_value=_PORTFOLIO, enforce=True)

    assert out["target_shares"] == 700
    assert out["delta_shares"] == 700
    assert out["shares"] == 700
    assert out["trade_value"] == pytest.approx(700 * _PRICE)
    assert out["target_weight"] <= 0.20 + 1e-9
    assert out["target_weight"] == pytest.approx(0.17745)
    assert out["target_shares"] % 100 == 0
    block = out["detail_reason"]["mv_cap"]
    assert block["clamped"] is True
    assert block["enforced"] is True
    assert block["pre_clamp_market_value_weight"] == pytest.approx(0.2028)
    assert block["post_clamp_market_value_weight"] == pytest.approx(0.17745)
    assert block["clamped_delta_shares"] == 700
    assert "已按整手回收" in out["reason"]


def test_enforced_cap_keeps_the_increment_rule_for_an_existing_holder():
    """The clamp is on the TOTAL target, so an existing position's delta shrinks correctly."""
    # Already holds 600 shares (15.21%); a top-up to 800 would breach 20%.
    row = _buy_row(target_shares=800, current_shares=600)
    out = apply_market_value_cap(row, portfolio_value=_PORTFOLIO, enforce=True)
    assert out["target_shares"] == 700
    assert out["delta_shares"] == 100


def test_enforced_cap_never_sells_down_a_legacy_overweight_hold():
    """This guard is not an exit rule: a HOLD above the cap is annotated, never sold."""
    row = _ticket_row(
        current_shares=900,
        target_shares=900,
        delta_shares=0,
        shares=0,
        trade_value=0.0,
        target_weight=market_value_weight(900, _PRICE, _PORTFOLIO),
        reason="HOLD",
    )
    out = apply_market_value_cap(row, portfolio_value=_PORTFOLIO, enforce=True)
    assert out["target_shares"] == 900
    assert out["delta_shares"] == 0
    assert out["detail_reason"]["mv_cap"]["clamped"] is False
    assert MV_CAP_BADGE in out["reason"]
    assert "仅标注" in out["reason"]


def test_rows_within_the_cap_are_untouched_but_still_carry_the_measurement():
    row = _buy_row(target_shares=700)
    before_reason = row["reason"]
    out = apply_market_value_cap(row, portfolio_value=_PORTFOLIO, enforce=True)
    assert out["target_shares"] == 700
    assert out["reason"] == before_reason
    block = out["detail_reason"]["mv_cap"]
    assert block["breached"] is False
    assert block["clamped"] is False


def test_row_without_a_detail_reason_dict_does_not_crash():
    row = _buy_row(target_shares=800)
    row.pop("detail_reason")
    out = apply_market_value_cap(row, portfolio_value=_PORTFOLIO, enforce=True)
    assert out["target_shares"] == 700
    assert MV_CAP_BADGE in out["reason"]


def test_mv_cap_kill_switch_overrides_the_explicit_flag(monkeypatch):
    monkeypatch.setenv("LIVETRADING_DISABLE_MV_CAP", "1")
    assert mv_cap_enforcement_enabled(True) is False
    # The helper is gate-free -- the caller (main) must pass the gated flag through.
    row = _buy_row(target_shares=800)
    out = apply_market_value_cap(
        row,
        portfolio_value=_PORTFOLIO,
        enforce=mv_cap_enforcement_enabled(True),
    )
    assert out["delta_shares"] == 800
    assert out["detail_reason"]["mv_cap"]["breached"] is True
    monkeypatch.setenv("LIVETRADING_DISABLE_MV_CAP", "0")
    assert mv_cap_enforcement_enabled(True) is True


@pytest.mark.parametrize(
    "quantity, price, portfolio_value",
    [
        (0, 25.35, 100_000.0),
        (100, 0, 100_000.0),
        (100, 25.35, 0),
        (100, 25.35, -5),
        ("bad", 25.35, 100_000.0),
        (None, None, None),
    ],
)
def test_market_value_weight_returns_zero_for_unusable_inputs(
    quantity, price, portfolio_value
):
    assert market_value_weight(quantity, price, portfolio_value) == 0.0


def test_market_value_weight_matches_the_ticket_arithmetic():
    assert market_value_weight(800, _PRICE, _PORTFOLIO) == pytest.approx(0.2028)


def test_default_cap_matches_the_model_side_cap():
    """The delivered cap must not silently drift away from blend_base's 20% intent."""
    assert DEFAULT_MAX_SINGLE_POSITION == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# 3. D8 -- a Stage-1-only flat seed must not shadow its session's instance dir
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_storage(tmp_path):
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _seed_flat(dir_path, date_str, *, account_state=None, stage2=False, summary=True):
    """Write a flat ``<date>/`` directory (Stage 1 always; Stage 2 only if asked)."""
    os.makedirs(dir_path, exist_ok=True)
    if summary:
        with open(os.path.join(dir_path, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": date_str,
                    "strategy_key": _STRATEGY,
                    "universe_key": _UNIVERSE,
                },
                f,
            )
    if account_state is not None:
        with open(
            os.path.join(dir_path, "account_state.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(account_state, f)
    if stage2:
        with open(
            os.path.join(dir_path, "live_trading_ticket.json"), "w", encoding="utf-8"
        ) as f:
            json.dump([], f)


def _seed_instance(base_dir, stamp, *, account_state, stage2=True):
    inst = os.path.join(base_dir, stamp)
    os.makedirs(inst, exist_ok=True)
    if stage2:
        with open(
            os.path.join(inst, "live_trading_ticket.json"), "w", encoding="utf-8"
        ) as f:
            json.dump([], f)
    if account_state is not None:
        with open(
            os.path.join(inst, "account_state.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(account_state, f)
    return inst


def _run_base(storage, date_str):
    return storage.get_run_dir(_STRATEGY, _UNIVERSE, date_str, prefer_existing=False)


_STALE_FLAT_STATE = {
    "lifecycle_states": {"600584.SH": "FLAT"},
    "entry_prices": {},
    "note": "PRE-DEPLOY STAGE-1 SEED",
}
_INSTANCE_STATE = {
    "lifecycle_states": {"600584.SH": "COOLDOWN"},
    "entry_prices": {"600584.SH": 25.35},
    "note": "REAL STAGE-2 ROUND",
}


def test_stage1_only_flat_seed_is_not_listed_as_a_run(temp_storage):
    """`summary.json` + a pre-deploy `account_state.json` is a SEED, not a round."""
    date_str = "2026-10-29"
    base = _run_base(temp_storage, date_str)
    _seed_flat(base, date_str, account_state=_STALE_FLAT_STATE)
    _seed_instance(base, "215823", account_state=_INSTANCE_STATE)

    runs = temp_storage.list_runs(
        strategy_key=_STRATEGY, universe_key=_UNIVERSE, require_both=True
    )
    assert [r["instance"] for r in runs] == ["215823"]


def test_stage1_only_flat_seed_does_not_win_the_state_resolution(temp_storage):
    """The incident: the flat seed's pre-deploy state was resurrected."""
    date_str = "2026-10-29"
    base = _run_base(temp_storage, date_str)
    _seed_flat(base, date_str, account_state=_STALE_FLAT_STATE)
    _seed_instance(base, "215823", account_state=_INSTANCE_STATE)

    state = temp_storage.latest_account_state_at_or_before(
        date_str, strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )
    assert state["note"] == "REAL STAGE-2 ROUND"
    assert state["lifecycle_states"]["600584.SH"] == "COOLDOWN"


def test_newest_instance_still_wins_among_several_instances(temp_storage):
    date_str = "2026-10-29"
    base = _run_base(temp_storage, date_str)
    _seed_flat(base, date_str, account_state=_STALE_FLAT_STATE)
    _seed_instance(base, "090000", account_state={"note": "MORNING"})
    _seed_instance(base, "215823", account_state={"note": "EVENING"})

    state = temp_storage.latest_account_state_at_or_before(
        date_str, strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )
    assert state["note"] == "EVENING"


def test_flat_dir_that_really_ran_stage2_is_preserved(temp_storage):
    """A first-of-session round legitimately lives in the flat dir and must stay listed."""
    date_str = "2026-10-29"
    base = _run_base(temp_storage, date_str)
    _seed_flat(
        base, date_str, account_state={"note": "FLAT ROUND"}, stage2=True
    )
    _seed_instance(base, "215823", account_state=_INSTANCE_STATE)

    runs = temp_storage.list_runs(
        strategy_key=_STRATEGY, universe_key=_UNIVERSE, require_both=True
    )
    assert [r["instance"] for r in runs] == ["215823", ""]


def test_legacy_flat_only_archive_is_untouched(temp_storage):
    """Pre-L2.62 layout: no instance subdirs at all -> no skip, no behaviour change."""
    date_str = "2026-10-26"
    base = _run_base(temp_storage, date_str)
    _seed_flat(base, date_str, account_state={"note": "LEGACY"})

    runs = temp_storage.list_runs(
        strategy_key=_STRATEGY, universe_key=_UNIVERSE, require_both=True
    )
    assert [r["instance"] for r in runs] == [""]
    state = temp_storage.latest_account_state_at_or_before(
        date_str, strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )
    assert state["note"] == "LEGACY"


def test_pending_stage2_handoff_without_instances_is_not_skipped(temp_storage):
    """The Stage-1 -> Stage-2 reuse window has no instance subdirs yet; the seed IS the run."""
    date_str = "2026-10-29"
    base = _run_base(temp_storage, date_str)
    _seed_flat(base, date_str, account_state={"note": "STAGE1 HOUR"})

    assert instance_subdirs(base) == []
    assert has_stage2_artifact(base) is False
    runs = temp_storage.list_runs(
        strategy_key=_STRATEGY, universe_key=_UNIVERSE, require_both=True
    )
    assert [r["instance"] for r in runs] == [""]


def test_gate_off_restores_the_flat_seed_listing(temp_storage, monkeypatch):
    """Documented grey-release escape hatch: pre-fix listing, verbatim."""
    date_str = "2026-10-29"
    base = _run_base(temp_storage, date_str)
    _seed_flat(base, date_str, account_state=_STALE_FLAT_STATE)
    _seed_instance(base, "215823", account_state=_INSTANCE_STATE)

    monkeypatch.setenv("LIVETRADING_PREFER_INSTANCE_STATE", "0")
    assert prefer_instance_state_enabled() is False
    runs = temp_storage.list_runs(
        strategy_key=_STRATEGY, universe_key=_UNIVERSE, require_both=True
    )
    assert [r["instance"] for r in runs] == ["215823", ""]

    monkeypatch.setenv("LIVETRADING_PREFER_INSTANCE_STATE", "1")
    assert prefer_instance_state_enabled() is True


def test_prefer_instance_state_is_on_by_default(monkeypatch):
    monkeypatch.delenv("LIVETRADING_PREFER_INSTANCE_STATE", raising=False)
    assert prefer_instance_state_enabled() is True


def test_has_stage2_artifact_ignores_the_pre_trade_holdings_book(tmp_path):
    """`holdings.json` is written BEFORE the child launches -- it proves nothing."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")
    (run_dir / "account_state.json").write_text("{}", encoding="utf-8")
    (run_dir / "holdings.json").write_text("{}", encoding="utf-8")
    assert has_stage2_artifact(str(run_dir)) is False

    (run_dir / "post_holdings.json").write_text("{}", encoding="utf-8")
    assert has_stage2_artifact(str(run_dir)) is True


def test_stage2_artifact_detection_accepts_csv_tickets(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "live_trading_ticket.csv").write_text("symbol\n", encoding="utf-8")
    assert has_stage2_artifact(str(run_dir)) is True


def test_has_stage2_artifact_is_false_for_missing_directories():
    assert has_stage2_artifact(None) is False
    assert has_stage2_artifact("/nonexistent/path/for/tests") is False


def test_instance_subdirs_excludes_the_flat_dir_and_non_stamp_names(tmp_path):
    base = tmp_path / "2026-10-29"
    for name in ("090000", "215823", "not_a_stamp", "09150"):
        (base / name).mkdir(parents=True)
    (base / "summary.json").write_text("{}", encoding="utf-8")
    assert instance_subdirs(str(base)) == ["090000", "215823"]
    assert instance_subdirs(str(base / "090000")) == []
    assert instance_subdirs(None) == []


# ---------------------------------------------------------------------------
# 4. (review finding 1) The D6 guard must never touch a lifecycle SELL
# ---------------------------------------------------------------------------


def test_stale_helper_refuses_a_lifecycle_liquidation():
    """DEFENSIVE INVARIANT: a liquidation row is out of scope, not "another residual".

    The risk exits in ``common/position_lifecycle.py`` flip the state to COOLDOWN
    *before* returning the SELL, so after the call ``is_holding()`` is False and
    ``current_shares > 0`` -- shape-identical to the 600584.SH residual lot.  The
    helper therefore rejects a liquidation loudly, so a future caller cannot
    silently mislabel a stop-loss / circuit-breaker exit as a residual cleanup.
    """
    with pytest.raises(ValueError) as excinfo:
        assess_stale_input_lot(
            current_shares=100,
            model_target=0.0,
            is_holding=False,
            lifecycle_action="SELL",
            enforce=True,
        )
    assert "liquidate" in str(excinfo.value)
    # ... and the same call on a HOLD row is the legitimate use.
    assert assess_stale_input_lot(
        current_shares=100,
        model_target=0.0,
        is_holding=False,
        lifecycle_action="HOLD",
        enforce=False,
    )["detected"] is True


# ---------------------------------------------------------------------------
# 5. Integration ordering: the real ``main()`` row loop
# ---------------------------------------------------------------------------

#: Pinned replay session.  Both dates are FIXED so the synthetic (deterministic)
#: provider yields the same bars every run -- the test never depends on today.
_PINNED_START = "2026-01-01"
_PINNED_AS_OF = "2026-10-22"


def _drive_live_ticket(
    tmp_path,
    *,
    strategy_args,
    symbols,
    holdings,
    account_state=None,
    extra_args=(),
    out_name="live_out",
):
    """Run the REAL ``main()`` row loop, fully offline, into ``tmp_path/<out_name>``.

    ``--data-provider synthetic`` + ``--no-cache`` + a pinned ``--as-of-date`` (a
    past session => ``run_mode=replay``, which skips the intraday run-window gate)
    is the same harness ``pipeline/tests/test_live_market_clock_wiring.py`` uses.
    ``--current-holdings`` is the operator's input book (integer shares => quantity
    mode).  The account state lands in the defaulted
    ``<output-dir>/account_state.json``, which is where ``main()`` reads the
    lifecycle states from.
    """
    from scripts.run_live_strategy import main as live_main

    out_dir = tmp_path / out_name
    out_dir.mkdir(parents=True, exist_ok=True)
    universe_file = tmp_path / "universe.txt"
    universe_file.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    if account_state is not None:
        (out_dir / "account_state.json").write_text(
            json.dumps(account_state, ensure_ascii=False), encoding="utf-8"
        )
    cmd = [
        "--universe-file",
        str(universe_file),
        "--output-dir",
        str(out_dir),
        "--start-date",
        _PINNED_START,
        "--as-of-date",
        _PINNED_AS_OF,
        "--data-provider",
        "synthetic",
        "--portfolio-value",
        str(int(_PORTFOLIO)),
        "--no-cache",
        "--current-holdings",
        json.dumps(holdings),
    ]
    return live_main(list(strategy_args) + cmd + list(extra_args))


def _rows_by_symbol(order_df):
    return {str(r["symbol"]): r for r in order_df.to_dict("records")}


def test_stale_guard_never_annotates_a_lifecycle_exit(tmp_path):
    """FINDING 1 REGRESSION: a genuine exit keeps its own reason, verbatim.

    Scenario: the account is in Tier-3 emergency halt, so the model target for every
    risky symbol is 0 and the lifecycle machine liquidates the HELD_ACTIVE position.
    That SELL leaves the state in COOLDOWN with ``current_shares > 0`` and a 0.0
    model target -- BEFORE the fix this row matched the D6 predicate and its reason
    was rewritten (warn-only: "…本行不下单" appended to a row that DOES place an
    order; enforced: prefixed with ``STALE_LOT_REASON | 原信号: <熔断原因>``, which
    made grepping the badge for residual-lot cleanups also return real risk events).

    Both modes are pinned: the annotation must be absent even when the operator DID
    pass ``--liquidate-stale-input-lots``.
    """
    symbol = "600000.SH"
    account_state = {
        "circuit_breaker_tier": "TIER_3_HALT",
        "freeze_remaining_bars": 3,
        "lifecycle_states": {
            symbol: {
                "symbol": symbol,
                "state": "HELD_ACTIVE",
                "held_bars": 10,
                "entry_price": 200.0,
                "peak_price": 200.0,
                "current_weight": 0.10,
            }
        },
    }
    for i, extra in enumerate(((), ("--liquidate-stale-input-lots",))):
        order_df = _drive_live_ticket(
            tmp_path,
            strategy_args=["--strategy", "equal_weight"],
            symbols=[symbol],
            holdings={symbol: 100},
            account_state=account_state,
            extra_args=extra,
            out_name=f"exit_{i}",
        )
        row = _rows_by_symbol(order_df)[symbol]
        assert row["action"] == "SELL"
        assert row["current_shares"] == 100
        # The lifecycle's own risk verdict survives untouched.
        assert row["reason"].startswith("触发账户熔断保护强制平仓")
        assert "原信号" not in row["reason"]
        assert STALE_LOT_LIQUIDATION_BADGE not in row["reason"]
        assert STALE_LOT_LIQUIDATION_BADGE not in row["summary_reason"]
        assert STALE_LOT_REASON not in row["reason"]
        assert "stale_input_lot" not in row["detail_reason"]
        assert row["detail_reason"]["blocking_reason"] == "NONE"


def test_row_loop_captures_target_before_lifecycle_and_caps_after_rescale(
    tmp_path, monkeypatch
):
    """FINDING 2: pin the two load-bearing call-site orderings end-to-end.

    ONE offline run of the real row loop.  Four A-share symbols are held, the model
    is ``dual_momentum`` with ``top_n_fraction=0.25`` (=> ``top_n = 1``), and
    ``--guard-latency-bars 1`` so the winning symbol actually BUYs today instead of
    stopping in PENDING_ENTRY.  That gives both shapes in one ticket:

    * a BUY whose delivered ``target_shares * price / NAV`` exceeds the 20% cap (D7);
    * three HOLD rows whose model target is 0 -> stale input lots (D6).

    ORDERING 1 (``if "600584.SH" in stale`` follows ``_inertia_shaped_step``): the
    wrapper below reproduces the inertia branch's write-back -- the machine rewrites
    ``decision.target_weight`` to ``state.current_weight`` (the residual's own
    weight) for one held-but-flat symbol.  Detection must still fire, because the
    call site feeds ``assess_stale_input_lot(model_target=raw_tgt_w)`` -- the value
    frozen at the TOP of the row body, before ``step_position_lifecycle``.
    RE-BREAKS IF: ``raw_tgt_w = tgt_w`` moves below the ``step_position_lifecycle(...)``
    call (or the call site passes ``model_target=tgt_w`` at the D6 block).  The
    rewritten 12%-ish weight would then read as "the model still wants it".

    ORDERING 2 (the ``market_value_weight == target_weight`` assertion): the D7 cap
    is evaluated AFTER the purchasing-power rescale, which re-rounds every BUY
    (``buy_scale`` here is well below 1: the 65.8% delivered weight is what survived
    the rescale).  RE-BREAKS IF: the ``apply_market_value_cap`` loop is moved ABOVE
    the purchasing-power block -- the recorded measurement would then be the
    pre-rescale quantity (~99% of NAV) while the row's ``target_weight`` is 65.8%.
    """
    import scripts.run_live_strategy as rls

    real_step = rls.step_position_lifecycle
    rewritten_symbol = "600584.SH"

    def _inertia_shaped_step(*args, **kwargs):
        """Real machine, but reproduce the inertia write-back for one symbol."""
        state = kwargs["state"] if "state" in kwargs else args[0]
        decision = real_step(*args, **kwargs)
        if (
            getattr(state, "symbol", None) == rewritten_symbol
            and decision.action == "HOLD"
            and abs(float(decision.target_weight or 0.0)) <= 1e-9
        ):
            decision.target_weight = float(state.current_weight)
        return decision

    monkeypatch.setattr(rls, "step_position_lifecycle", _inertia_shaped_step)

    symbols = ["600000.SH", "601857.SH", rewritten_symbol, "000001.SZ"]
    strategy_file = tmp_path / "dual_momentum_strategy.json"
    strategy_file.write_text(
        json.dumps(
            {
                "template_name": "dual_momentum",
                "params": {
                    "rebalance_freq_days": 21,
                    "mom_lookback": 63,
                    "top_n_fraction": 0.25,
                },
            }
        ),
        encoding="utf-8",
    )
    order_df = _drive_live_ticket(
        tmp_path,
        strategy_args=["--strategy-file", str(strategy_file)],
        symbols=symbols,
        holdings={s: 100 for s in symbols},
        # Every held symbol is FLAT in the lifecycle machine: the operator's book
        # still carries the lots, the machine says the model is out.  This is the
        # 600584.SH / 600362.SH shape of 2026-10-29.
        account_state={
            "lifecycle_states": {s: {"symbol": s, "state": "FLAT"} for s in symbols}
        },
        extra_args=["--guard-latency-bars", "1"],
    )
    rows = _rows_by_symbol(order_df)

    # -- D7: the cap measured the DELIVERED (post-rescale) quantity ------------
    breached = [
        r for r in rows.values()
        if (r["detail_reason"].get("mv_cap") or {}).get("breached")
    ]
    assert breached, "expected at least one delivered weight over the 20% cap"
    cap_row = max(breached, key=lambda r: r["detail_reason"]["mv_cap"]["market_value_weight"])
    assert cap_row["action"] == "BUY"
    assert MV_CAP_BADGE in cap_row["reason"]
    # ORDERING 2: same number the ticket actually delivers.  A cap sweep placed
    # before the purchasing-power rescale records the pre-rescale weight instead.
    assert cap_row["detail_reason"]["mv_cap"]["market_value_weight"] == pytest.approx(
        float(cap_row["target_weight"]), abs=1e-6
    )
    assert cap_row["target_weight"] > DEFAULT_MAX_SINGLE_POSITION

    # -- D6: stale lots are detected, and only on non-liquidating rows --------
    stale = {
        s: r for s, r in rows.items()
        if (r["detail_reason"].get("stale_input_lot") or {}).get("detected")
    }
    assert len(stale) >= 1
    for row in stale.values():
        assert row["action"] in ("HOLD", "HOLD_FILTERED", "COOLDOWN_BLOCKED")
        assert row["detail_reason"]["stale_input_lot"]["enforced"] is False
        assert STALE_LOT_LIQUIDATION_BADGE in row["reason"]
    # ORDERING 1: this one's post-lifecycle target was rewritten to its own weight.
    assert rewritten_symbol in stale
    assert stale[rewritten_symbol]["detail_reason"]["stale_input_lot"][
        "model_target_weight"
    ] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. (2026-10-30) Seed book: archive-only store, strict resolution, no global copy
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (the defect this whole section pins):
#   The book used to be persisted THREE times -- the web `data/current_holdings.json`,
#   the web `data/account_state.json`, and the per-run archive copies.  The globals were
#   read through a different resolution order than the archive, so a copy that lagged
#   (failed/partial run, restored archive, manual edit) could WIN the read and resurrect
#   lots that had already been sold: 2026-10-30, `600584.SH` / `600362.SH` came back as
#   held (a 100-share lot each) after the model had fully exited them, and the deploy
#   path sized the next ticket from them.
#   The fix is threefold and each half is pinned below:
#     1. the legacy globals are IGNORED (warned about once, never read, never written);
#     2. the DEPLOY path resolves its seed from the archive only and treats
#        `(None, "none")` as a REFUSAL (never as "assume flat");
#     3. `{}` from a real archive source stays a CONFIRMED all-cash book.
_LEGACY_GHOST_BOOK = {"600584.SH": 100, "600362.SH": 100}


def _storage_over(tmp_path):
    """A `StorageManager` over `tmp_path`'s (already created) sub-directories."""
    return StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _make_dirs(tmp_path, names=("data", "archive", "cache", "settings")):
    for name in names:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)


def test_legacy_global_current_holdings_is_ignored_and_named(tmp_path, caplog):
    """A pre-fix `data/current_holdings.json` must never become a position list.

    The warning is emitted ONCE at construction (`_warn_legacy_state_files`) and names
    the exact file, so an operator can quarantine it instead of editing a file that is
    no longer read.
    re-break: restore the `_load_json(os.path.join(self.data_dir, "current_holdings.json"),
    default={})` fallback at the end of `StorageManager.get_holdings`.
    """
    _make_dirs(tmp_path)
    legacy = tmp_path / "data" / "current_holdings.json"
    legacy.write_text(json.dumps(_LEGACY_GHOST_BOOK), encoding="utf-8")

    caplog.set_level(logging.WARNING)
    storage = _storage_over(tmp_path)
    # Every read shape the UI / deploy path could use: never the ghost lots.
    assert storage.get_holdings() == {}
    assert storage.get_holdings(date_str="2026-10-30") == {}
    assert (
        storage.get_holdings(
            date_str="2026-10-30", strategy_key=_STRATEGY, universe_key=_UNIVERSE
        )
        == {}
    )
    assert (
        storage.get_holdings(strategy_key=_STRATEGY, universe_key=_UNIVERSE) == {}
    )

    named = [
        rec.getMessage()
        for rec in caplog.records
        if "current_holdings.json" in rec.getMessage()
    ]
    assert named, "the legacy global file must be named in a WARNING at startup"
    assert any(str(legacy) in message for message in named), named


def test_legacy_global_account_state_is_ignored(tmp_path, caplog):
    """A pre-fix `data/account_state.json` must never supply the NAV baseline.

    A stale global here is worse than a stale book: `current_nav` / `peak_nav` drive the
    circuit-breaker tier, so resurrecting 999999 would mask every drawdown.
    re-break: restore the `_load_json(self.account_state_file, default=dict(...))`
    fallback at the end of `StorageManager.get_account_state`.
    """
    _make_dirs(tmp_path)
    legacy = tmp_path / "data" / "account_state.json"
    legacy.write_text(
        json.dumps({"as_of_date": "2026-10-30", "current_nav": 999999.0}),
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING)
    storage = _storage_over(tmp_path)

    state = storage.get_account_state()
    assert state == dict(_DEFAULT_ACCOUNT_STATE)
    assert state["current_nav"] == 100000.0
    assert state["current_nav"] != 999999.0
    assert (
        storage.get_account_state(date_str="2026-10-30") == dict(_DEFAULT_ACCOUNT_STATE)
    )
    assert (
        storage.get_account_state(
            date_str="2026-10-30", strategy_key=_STRATEGY, universe_key=_UNIVERSE
        )
        == dict(_DEFAULT_ACCOUNT_STATE)
    )

    named = [
        rec.getMessage()
        for rec in caplog.records
        if "account_state.json" in rec.getMessage()
    ]
    assert named, "the legacy global file must be named in a WARNING at startup"
    assert any(str(legacy) in message for message in named), named


def test_data_dir_is_never_written_by_any_save_path(temp_storage):
    """LOAD-BEARING invariant of the refactor: the web `data/` dir is config only.

    `save_holdings` / `save_account_state` / `save_daily_run` must write the run archive
    and nothing else.  A re-appearing global copy is the exact mechanism that let the
    2026-10-30 phantom lots resurrect, so this asserts the ABSENCE of the files, not just
    the presence of the archive copies.
    re-break: restore any `self._save_json(os.path.join(self.data_dir,
    "current_holdings.json"|"account_state.json"), ...)` write in `save_holdings`,
    `save_account_state` or `save_daily_run`.
    """
    data_dir = temp_storage.data_dir
    global_holdings = os.path.join(data_dir, "current_holdings.json")
    global_state = os.path.join(data_dir, "account_state.json")
    holdings = {"600584.SH": 100}
    state = {"as_of_date": "2026-09-25", "current_nav": 101000.0}

    temp_storage.save_holdings(
        holdings, date_str="2026-09-25", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )
    temp_storage.save_account_state(
        state, date_str="2026-09-25", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )
    run_dir = temp_storage.save_daily_run(
        date_str="2026-09-25",
        summary={"date": "2026-09-25"},
        holdings=holdings,
        account_state=state,
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
    )

    assert not os.path.exists(global_holdings), global_holdings
    assert not os.path.exists(global_state), global_state
    # ... while the run-dir copies (the ONLY store) really are there.
    assert os.path.isfile(os.path.join(run_dir, "holdings.json"))
    assert os.path.isfile(os.path.join(run_dir, "account_state.json"))
    # And the archive is readable back through its own keys.
    assert (
        temp_storage.get_holdings(
            date_str="2026-09-25", strategy_key=_STRATEGY, universe_key=_UNIVERSE
        )
        == holdings
    )
    assert (
        temp_storage.get_account_state(
            date_str="2026-09-25", strategy_key=_STRATEGY, universe_key=_UNIVERSE
        )["current_nav"]
        == 101000.0
    )


# -- `resolve_seed_holdings`: the deploy path's single seed resolver ----------------


def _seed_run_date(
    storage,
    date_str,
    *,
    post_holdings=None,
    holdings=None,
    instance=None,
    account_state=None,
):
    """Write one archived run dir (`summary.json` makes it a listed run)."""
    target = os.path.join(storage.archive_dir, _STRATEGY, _UNIVERSE, date_str)
    if instance:
        target = os.path.join(target, instance)
    os.makedirs(target, exist_ok=True)
    _write_json(
        os.path.join(target, "summary.json"),
        {"date": date_str, "strategy_key": _STRATEGY, "universe_key": _UNIVERSE},
    )
    if post_holdings is not None:
        _write_json(os.path.join(target, "post_holdings.json"), post_holdings)
    if holdings is not None:
        _write_json(os.path.join(target, "holdings.json"), holdings)
    if account_state is not None:
        _write_json(os.path.join(target, "account_state.json"), account_state)
    return target


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def test_resolve_seed_holdings_reports_unknown_on_an_empty_archive(temp_storage):
    """No archive book -> `(None, "none")`, i.e. UNKNOWN (the deploy must refuse).

    re-break: make `resolve_seed_holdings` end with `return {}, "none"` (or
    `return self.get_holdings(...), "none"`) -- an unknown book would then look like a
    confirmed flat one and the deploy would size every target as a fresh BUY.
    """
    seed, source = temp_storage.resolve_seed_holdings(
        "2026-10-30", _STRATEGY, _UNIVERSE
    )
    assert seed is None
    assert source == "none"


def test_resolve_seed_holdings_carries_the_previous_planned_post_book(temp_storage):
    """The last round's PLANNED `post_holdings.json` is the normal implicit seed.

    re-break: drop the `get_previous_post_holdings` leg from `resolve_seed_holdings`
    (a same-day rerun of an untouched session would then resolve to `"none"`).
    """
    _seed_run_date(temp_storage, "2026-10-29", post_holdings={"AAA.SH": 100})
    seed, source = temp_storage.resolve_seed_holdings(
        "2026-10-30", _STRATEGY, _UNIVERSE
    )
    assert seed == {"AAA.SH": 100}
    assert source == "previous_post_holdings"


def test_resolve_seed_holdings_distinguishes_confirmed_cash_from_unknown(temp_storage):
    """`{}` from a real source is CONFIRMED all-cash and must not be read as unknown.

    The previous round planned/liquidated a flat book; honouring it is what lets a
    fully-liquidated session deploy again.  `None` is what makes the deploy refuse.
    re-break: change the `if plan is not None:` guard in `resolve_seed_holdings` to
    `if plan:` -- a confirmed all-cash round would become `(None, "none")` and the next
    deploy would refuse (or, worse, be "fixed" by an empty-book workaround).
    """
    _seed_run_date(temp_storage, "2026-10-29", post_holdings={})
    seed, source = temp_storage.resolve_seed_holdings(
        "2026-10-30", _STRATEGY, _UNIVERSE
    )
    assert seed == {}
    assert seed is not None, "an empty planned book is a CONFIRMED flat book"
    assert source == "previous_post_holdings"


def test_operator_snapshot_wins_over_the_previous_post_book(temp_storage):
    """A same-session explicit snapshot beats yesterday's assumed fills.

    This is the "operator corrected the book" case: re-running Stage 2 must reconcile
    against what the operator says is held NOW, not against the previous round's plan.
    re-break: put the `get_previous_post_holdings` leg BEFORE the same-session snapshot
    walk in `resolve_seed_holdings`.
    """
    _seed_run_date(temp_storage, "2026-10-29", post_holdings={"AAA.SH": 100})
    _seed_run_date(temp_storage, "2026-10-30", holdings={"BBB.SH": 250})

    seed, source = temp_storage.resolve_seed_holdings(
        "2026-10-30", _STRATEGY, _UNIVERSE
    )
    assert seed == {"BBB.SH": 250}
    assert source == "operator_snapshot"


def test_newest_instance_snapshot_beats_the_flat_dir_snapshot(temp_storage):
    """Within one session the NEWEST `HHMMSS` instance's snapshot is the current book.

    (L2.62) The flat `<date>/` directory is the session's FIRST instance, so walking it
    last is what keeps a morning snapshot from shadowing the afternoon correction.
    re-break: stop `reversed(...)`-ing `list_instances_for_date` (or append `base_dir`
    first instead of last) in `resolve_seed_holdings`.
    """
    _seed_run_date(temp_storage, "2026-10-30", holdings={"FLAT.SH": 1})
    _seed_run_date(
        temp_storage, "2026-10-30", instance="090000", holdings={"NINE.SH": 9}
    )
    _seed_run_date(
        temp_storage, "2026-10-30", instance="130000", holdings={"THIRTEEN.SH": 13}
    )

    seed, source = temp_storage.resolve_seed_holdings(
        "2026-10-30", _STRATEGY, _UNIVERSE
    )
    assert seed == {"THIRTEEN.SH": 13}
    assert source == "operator_snapshot"


# -- deploy path: refuse before any write / launch, and label the seed source ------


def _dummy_strategy_file(tmp_path, name="seed_strat.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"template_name": "dummy", "params": {}}), encoding="utf-8"
    )
    return str(path)


def _patch_deploy_subprocess(monkeypatch):
    """Capture the child's argv and NEVER launch anything (offline by construction)."""
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _archived_summary(storage, argv):
    """`summary.json` of the run dir this deploy actually passed to the child."""
    run_dir = argv[argv.index("--output-dir") + 1]
    path = os.path.join(run_dir, "summary.json")
    assert os.path.isfile(path), path
    return storage._load_json(path, default={}) or {}


def test_deploy_refuses_an_unknown_seed_before_any_write_or_launch(
    temp_storage, tmp_path, monkeypatch
):
    """`(None, "none")` is a REFUSAL, and the refusal is ordered BEFORE every side effect.

    A `holdings.json` written before the refusal would be read as that round's PRE-trade
    input by the next run; a launched child would size orders from an empty book.  So the
    ordering itself is the invariant: no book file, no NAV breakdown, no subprocess.
    re-break: in `run_live_deploy`, move the implicit-seed resolution AFTER
    `save_holdings` / `_write_nav_breakdown`, or fall back to
    `self.storage.get_holdings(...)` instead of raising.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)

    with pytest.raises(LiveSeedBookMissingError):
        service.run_live_deploy(
            strategy_file=_dummy_strategy_file(tmp_path),
            custom_symbols=["600519.SH"],
            as_of_date="2026-10-30",
            portfolio_value=100000.0,
            strategy_key=_STRATEGY,
            universe_key=_UNIVERSE,
            nav_override=True,
            data_provider="synthetic",
        )

    assert calls == [], "a refused deploy must never launch the child process"

    base_dir = temp_storage.get_run_dir(
        _STRATEGY, _UNIVERSE, "2026-10-30", prefer_existing=True
    )
    # (fix 2) The run dir is NO LONGER allocated before the refusal: a refused round must
    # not leave an empty instance directory behind for the next reader to interpret.
    assert not os.path.exists(base_dir), "a refused deploy must not create the run dir"
    forbidden = []
    for root, _dirs, files in os.walk(temp_storage.archive_dir):
        forbidden.extend(
            os.path.join(root, name)
            for name in files
            if name in ("holdings.json", "nav_breakdown.json")
        )
    assert forbidden == [], forbidden


def test_deploy_opt_out_falls_back_and_labels_the_seed(
    temp_storage, tmp_path, monkeypatch, caplog
):
    """`LIVETRADING_STRICT_SEED_BOOK=0` is the documented, LOUD opt-out.

    It restores the pre-2026-10-30 `get_holdings()` resolution, logs a WARNING, and the
    archived summary labels the round `legacy_get_holdings_fallback` so a degraded deploy
    can never be mistaken for a strict one.  This is the ONLY place in the suite that
    exercises the knob: the strict path is asserted above, so the guard stays visible.
    re-break: hard-code `holdings_seed_source = seed_source` (or return early) so the
    opt-out branch never records `legacy_get_holdings_fallback`, or drop the WARNING.
    """
    monkeypatch.setenv("LIVETRADING_STRICT_SEED_BOOK", "0")
    calls = _patch_deploy_subprocess(monkeypatch)
    caplog.set_level(logging.WARNING)
    service = ExecutionService(temp_storage)

    res = service.run_live_deploy(
        strategy_file=_dummy_strategy_file(tmp_path),
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-30",
        portfolio_value=100000.0,
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
        nav_override=True,
        data_provider="synthetic",
    )

    assert res["status"] == "success"
    assert len(calls) == 1
    assert "LIVETRADING_STRICT_SEED_BOOK=0" in caplog.text
    assert "legacy_get_holdings_fallback" in caplog.text
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == "legacy_get_holdings_fallback"


def test_deploy_summary_labels_an_explicit_book_as_operator_input(
    temp_storage, tmp_path, monkeypatch
):
    """An explicit (form) book is `operator_input` -- the normal, unguarded arm.

    re-break: drop `holdings_seed_source` from `run_live_deploy`'s `round_fields` (the
    archive would no longer say which store sized the ticket).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)

    service.run_live_deploy(
        strategy_file=_dummy_strategy_file(tmp_path),
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        current_holdings={"600519.SH": 100},
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
        nav_override=True,
        data_provider="synthetic",
    )

    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == "operator_input"
    # The explicit arm must not have been re-labelled by the seed resolver.
    assert summary["holdings_seed_source"] != "previous_post_holdings"


def test_deploy_summary_labels_the_previous_post_book_source(
    temp_storage, tmp_path, monkeypatch
):
    """An implicit seed from the archive records `previous_post_holdings`.

    This is the carry-forward path the 2026-10-30 incident ran through, so the archive
    must say so -- an auditor can tell a strict-sourced deploy from a degraded one.
    re-break: assign `holdings_seed_source = "legacy_get_holdings_fallback"` on the
    implicit arm regardless of `resolve_seed_holdings`' returned source.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)
    _seed_run_date(temp_storage, "2026-10-28", post_holdings={"600519.SH": 100})

    service.run_live_deploy(
        strategy_file=_dummy_strategy_file(tmp_path),
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
        nav_override=True,
        data_provider="synthetic",
    )

    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == "previous_post_holdings"


# ---------------------------------------------------------------------------
# 7. (fix 1) An empty table is never indistinguishable from "confirmed all-cash";
#    (fix 2) a refused deploy leaves no artifacts and consumes no Stage-1 hand-off
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (both fixes):
#   `static/js/app.js::getHoldingsFromTable()` returns `{}` for a holdings table with no
#   positive row -- a page whose table never rendered, an operator who cleared it, or an
#   untouched first deploy.  The explicit-book arm accepted that `{}` as a legitimate book,
#   and an empty book makes the engine treat every target weight as a fresh position: it
#   emits a SELL for every held symbol, i.e. a FULL LIQUIDATION.  That is the same failure
#   class as the 2026-10-30 duplicate-book incident (a wrong book produced real orders),
#   only "wrong" means "nothing" here.
#   The second half is ordering: the refusal used to be decided AFTER
#   `_resolve_stage2_run_dir` -- which POPS the Stage-1 hand-off and CREATES the instance
#   dir -- so a refused deploy left a half-built archive entry and ate the hand-off.


def _archive_tree(root):
    """Sorted `(relpath, sorted(entry names))` snapshot of an archive directory tree."""
    snapshot = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        snapshot.append((rel, tuple(sorted(dirnames)), tuple(sorted(filenames))))
    return sorted(snapshot)


def _stage1_handoff_dir(storage, date_str):
    """Create + return this session's Stage-1 dir with a real gate artifact (no summary).

    `stage1_health_report.json` alone makes `storage.is_run_dir()` true, so
    `resolve_seed_holdings` really walks the session -- and it carries NONE of the
    `STAGE1_EVIDENCE_SUMMARY_KEYS`, so the refusal under test cannot be confused with the
    (later, different) `Stage1HealthReportMissingError` gate.
    """
    base = storage.get_run_dir(_STRATEGY, _UNIVERSE, date_str, prefer_existing=False)
    os.makedirs(base, exist_ok=True)
    _write_json(
        os.path.join(base, "stage1_health_report.json"),
        {"gate_code": "PASS", "directive": "NORMAL"},
    )
    return base


def _deploy_kwargs(tmp_path, *, date_str="2026-10-30", **extra):
    kwargs = dict(
        strategy_file=_dummy_strategy_file(tmp_path),
        custom_symbols=["600519.SH"],
        as_of_date=date_str,
        portfolio_value=100000.0,
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
        nav_override=True,
        data_provider="synthetic",
    )
    kwargs.update(extra)
    return kwargs


def test_unconfirmed_empty_book_is_refused(temp_storage, tmp_path, monkeypatch):
    """`{}` + no confirmation => refusal naming the `confirm_empty_book` escape hatch.

    re-break: delete the `if not confirm_empty_book: raise` branch of
    `resolve_explicit_book_source` -- the empty explicit book would be accepted as a
    legitimate book and the engine would size a full liquidation from it.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)

    with pytest.raises(EmptyBookConfirmationRequiredError) as excinfo:
        service.run_live_deploy(**_deploy_kwargs(tmp_path, current_holdings={}))

    message = str(excinfo.value)
    # The refusal must name the field, or an API client cannot get past it.
    assert "confirm_empty_book" in message
    assert "全部空仓" in message
    # A sibling of the seed-book refusal, so existing handlers keep working.
    assert isinstance(excinfo.value, LiveSeedBookMissingError)
    assert calls == [], "a refused deploy must never launch the child process"


def test_confirmed_empty_book_deploys_and_is_labelled(
    temp_storage, tmp_path, monkeypatch, caplog
):
    """`{}` + `confirm_empty_book=True` deploys, and the archive says it was confirmed.

    re-break: return `EXPLICIT_BOOK_SOURCE` unconditionally from
    `resolve_explicit_book_source` (a deliberately-confirmed all-cash round would be
    indistinguishable from an operator-entered position list in `summary.json`).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    caplog.set_level(logging.WARNING)
    service = ExecutionService(temp_storage)

    res = service.run_live_deploy(
        **_deploy_kwargs(tmp_path, current_holdings={}, confirm_empty_book=True)
    )

    assert res["status"] == "success"
    assert res["holdings"] == {}
    assert len(calls) == 1
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == CONFIRMED_EMPTY_BOOK_SOURCE
    assert summary["holdings_seed_source"] != EXPLICIT_BOOK_SOURCE
    # ... and it is never quiet about it: the whole-liquidation intent is logged loudly.
    assert "[empty-book]" in caplog.text


def test_all_zero_book_is_treated_as_empty(temp_storage, tmp_path, monkeypatch):
    """A table that still lists symbols at 0 shares is `{}`, not a position list.

    re-break: make `book_is_empty` `return len(holdings) == 0` -- an all-zero table would
    then be accepted as a real book and liquidate every position without confirmation.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)

    with pytest.raises(EmptyBookConfirmationRequiredError) as excinfo:
        service.run_live_deploy(
            **_deploy_kwargs(tmp_path, current_holdings={"600900.SH": 0})
        )
    assert "confirm_empty_book" in str(excinfo.value)
    assert calls == []

    # The predicate itself, including the "unknown is never zero" rule.
    assert book_is_empty({}) is True
    assert book_is_empty(None) is True
    assert book_is_empty({"600900.SH": 0}) is True
    assert book_is_empty({"600900.SH": 0, "600519.SH": "0"}) is True
    assert book_is_empty({"600900.SH": {"shares": 0}}) is True
    assert book_is_empty({"600900.SH": 100}) is False
    assert book_is_empty({"600900.SH": {"shares": 100}}) is False
    assert book_is_empty({"600900.SH": "abc"}) is False


@pytest.mark.parametrize("refusal", ["unknown_seed", "empty_book"])
@pytest.mark.parametrize("stage1_pending", [True, False])
def test_refused_deploy_leaves_the_archive_and_handoff_untouched(
    temp_storage, tmp_path, monkeypatch, refusal, stage1_pending
):
    """INVARIANT: a refusal is decided BEFORE the run dir exists and BEFORE the hand-off pop.

    Both refusal kinds x both hand-off states, because they fail differently:

    * `stage1_pending=True` -- the old code POPPED the Stage-1 entry (and swallowed it) even
      though no ticket was ever produced, so the next deploy lost the gate hand-off;
    * `stage1_pending=False` -- the old code CREATED the round's instance dir, i.e. an
      archive entry for a deploy that never happened.

    re-break: move the seed resolution (or the empty-book confirmation) back BELOW
    `_resolve_stage2_run_dir` / `os.makedirs(run_dir)` in `run_live_deploy`.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)
    val_date = "2026-10-30"

    if stage1_pending:
        handoff_dir = _stage1_handoff_dir(temp_storage, val_date)
        service._pending_stage1[service._instance_key(_STRATEGY, _UNIVERSE, val_date)] = (
            handoff_dir,
            frozenset(),
        )

    tree_before = _archive_tree(temp_storage.archive_dir)
    pending_before = dict(service._pending_stage1)

    extra = {"current_holdings": {}} if refusal == "empty_book" else {}
    expected = (
        EmptyBookConfirmationRequiredError
        if refusal == "empty_book"
        else LiveSeedBookMissingError
    )
    with pytest.raises(expected):
        service.run_live_deploy(**_deploy_kwargs(tmp_path, **extra))

    # 1. no child process
    assert calls == [], "a refused deploy must never launch the child process"
    # 2. the archive is byte-unchanged: no new run dir, no account_state.json,
    #    no holdings.json / nav_breakdown.json / summary.json
    assert _archive_tree(temp_storage.archive_dir) == tree_before
    written = [
        name
        for _root, _dirs, files in os.walk(temp_storage.archive_dir)
        for name in files
    ]
    for forbidden in ("summary.json", "account_state.json", "holdings.json", "nav_breakdown.json"):
        assert forbidden not in written, forbidden
    # 3. the Stage-1 hand-off entry was neither popped nor rewritten
    assert service._pending_stage1 == pending_before


def test_explicit_non_empty_book_needs_no_confirmation(temp_storage, tmp_path, monkeypatch):
    """The normal path is untouched: no dialog, no flag, `operator_input`, same book file.

    re-break: apply the empty-book gate to every book (a real deploy would be refused), or
    label this arm `CONFIRMED_EMPTY_BOOK_SOURCE` (the archive would misdescribe the round).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)

    res = service.run_live_deploy(
        **_deploy_kwargs(tmp_path, current_holdings={"600519.SH": 100})
    )

    assert res["status"] == "success"
    assert len(calls) == 1
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == EXPLICIT_BOOK_SOURCE == "operator_input"
    # The book the child was handed is exactly what the operator submitted.
    holdings_file = calls[0][calls[0].index("--current-holdings-file") + 1]
    assert temp_storage._load_json(holdings_file, default=None) == {"600519.SH": 100}



# ---------------------------------------------------------------------------
# 8. (2026-10-30) The ROUTE layer must forward `confirm_empty_book` (HTTP half)
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (fix 1, the silent-kwargs-drop half):
#   `RunDeployRequest.confirm_empty_book` and `MultiStrategyDeployRequest.confirm_empty_book`
#   are declared on the pydantic models, and
#   `ExecutionService.run_live_deploy` / `run_multi_strategy_netted_deploy` both accept and
#   honour them.  But BOTH routes call the service through an EXPLICIT keyword list, and
#   pydantic does not splat the rest of the payload onto that call: a model field that is
#   not re-listed at the call site is accepted, validated, and then SILENTLY DISCARDED,
#   with the service falling back to its `False` default.
#   Consequence: the escape hatch was unreachable over HTTP and from the UI -- a client
#   that legitimately submitted an intentional all-cash book (`confirm_empty_book: true`)
#   was still refused by `EmptyBookConfirmationRequiredError`.  The REFUSAL half worked
#   perfectly; only the "I really mean it" half was dead.
#   These tests drive the REAL routes over `TestClient` with `subprocess.run` captured
#   (`_patch_deploy_subprocess`), so no child process is ever launched and every write
#   lands in the hermetic `temp_storage` / `tmp_path` workspace.


def _route_client_and_app(storage):
    """The suite's existing route harness: real `create_app` + `TestClient`.

    Imported locally on purpose -- this section is APPENDED to a module whose top-of-file
    imports and fixtures are pinned by the tests above it, so no existing line is touched.
    """
    from fastapi.testclient import TestClient
    from server import create_app

    app = create_app(storage)
    return TestClient(app), app


def _record_service_kwargs(monkeypatch, app, method_name):
    """Record what the route hands the service, then run the real method.

    The route closes over the `ExecutionService` INSTANCE (`create_app` builds it), so an
    instance-level attribute is what the route's `execution_service.<method>(...)` lookup
    resolves -- `app.state.execution_service` is the same object.  This is what makes the
    "was the field forwarded at all?" question observable: a dropped field is simply ABSENT
    from the recorded kwargs (it does not arrive as `False`).
    """
    seen = []
    real = getattr(app.state.execution_service, method_name)

    def _spy(**kwargs):
        seen.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(app.state.execution_service, method_name, _spy)
    return seen


def _run_deploy_route_payload(tmp_path, **extra):
    payload = {
        "strategy_file": _dummy_strategy_file(tmp_path),
        "custom_symbols": ["600519.SH"],
        "as_of_date": "2026-10-30",
        "portfolio_value": 100000.0,
        "strategy_key": _STRATEGY,
        "universe_key": _UNIVERSE,
        "nav_override": True,
        "data_provider": "synthetic",
        # The empty holdings table, exactly as the UI submits it.
        "current_holdings": {},
    }
    payload.update(extra)
    return payload


def _multi_deploy_route_payload(**extra):
    payload = {
        "matrix_id": "tri_core_blend",
        "custom_symbols": ["600519.SH"],
        "as_of_date": "2026-10-30",
        "portfolio_value": 100000.0,
        "data_provider": "synthetic",
        "current_holdings": {},
    }
    payload.update(extra)
    return payload


def test_run_deploy_route_refuses_an_empty_book_without_confirmation(
    temp_storage, tmp_path, monkeypatch
):
    """`POST /api/run-deploy` with `{}` and no flag => the 500 refusal, no child launched.

    Also pins that the route FORWARDS the field (as the model's `False` default): a dropped
    kwarg is absent from the recorded call, not present-as-False.
    re-break: remove `confirm_empty_book=payload.confirm_empty_book,` from the
    `/api/run-deploy` call in `server.py` (`seen[0]["confirm_empty_book"]` then raises
    KeyError; the paired confirmed-book test below turns the refusal into a 500).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    client, app = _route_client_and_app(temp_storage)
    seen = _record_service_kwargs(monkeypatch, app, "run_live_deploy")

    res = client.post("/api/run-deploy", json=_run_deploy_route_payload(tmp_path))

    assert res.status_code == 500
    detail = res.json()["detail"]
    # The documented refusal shape: the exception text names the escape hatch.
    assert "confirm_empty_book" in detail
    assert "全部空仓" in detail
    # The flag reached the service (defaulted by the model, never dropped).
    assert seen, "the route must call the service"
    assert seen[0]["confirm_empty_book"] is False
    assert seen[0]["current_holdings"] == {}
    assert calls == [], "a refused deploy must never launch the child process"


def test_run_deploy_route_forwards_a_confirmed_empty_book(
    temp_storage, tmp_path, monkeypatch
):
    """`{}` + `confirm_empty_book: true` over HTTP => it DEPLOYS as a confirmed all-cash book.

    This is the end-to-end reachability proof for the escape hatch: with the field dropped
    by the route the very same request returns 500 instead of 200.
    re-break: remove `confirm_empty_book=payload.confirm_empty_book,` from the
    `/api/run-deploy` call in `server.py` (the service then sees its `False` default and
    refuses, so `res.status_code` becomes 500 and no summary is written).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    client, app = _route_client_and_app(temp_storage)
    seen = _record_service_kwargs(monkeypatch, app, "run_live_deploy")

    res = client.post(
        "/api/run-deploy",
        json=_run_deploy_route_payload(tmp_path, confirm_empty_book=True),
    )

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"
    assert seen and seen[0]["confirm_empty_book"] is True
    assert len(calls) == 1, "the confirmed deploy must launch exactly one child"
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == CONFIRMED_EMPTY_BOOK_SOURCE
    # The child really was handed the empty book, not a stale/ghost one.
    holdings_file = calls[0][calls[0].index("--current-holdings-file") + 1]
    assert temp_storage._load_json(holdings_file, default=None) == {}


def test_multi_deploy_route_refuses_an_empty_matrix_book_without_confirmation(
    temp_storage, monkeypatch
):
    """`POST /api/deploy/multi` with `{}` and no flag => the 500 refusal, no child launched.

    re-break: remove `confirm_empty_book=payload.confirm_empty_book,` from the
    `/api/deploy/multi` call in `server.py` (`seen[0]["confirm_empty_book"]` then raises
    KeyError; the paired confirmed-matrix test below turns the refusal into a 500).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    client, app = _route_client_and_app(temp_storage)
    seen = _record_service_kwargs(monkeypatch, app, "run_multi_strategy_netted_deploy")

    res = client.post("/api/deploy/multi", json=_multi_deploy_route_payload())

    assert res.status_code == 500
    detail = res.json()["detail"]
    assert "confirm_empty_book" in detail
    assert "全部空仓" in detail
    assert seen, "the route must call the service"
    assert seen[0]["confirm_empty_book"] is False
    assert seen[0]["current_holdings"] == {}
    assert calls == [], "a refused netted deploy must never launch the child process"


def test_multi_deploy_route_forwards_a_confirmed_empty_matrix_book(
    temp_storage, monkeypatch
):
    """`{}` + `confirm_empty_book: true` over HTTP => the netted deploy runs all-cash.

    The matrix archive's `summary.json` must carry `operator_confirmed_empty_book`, which is
    the only record of "this netted round started from a deliberately empty book".
    re-break: remove `confirm_empty_book=payload.confirm_empty_book,` from the
    `/api/deploy/multi` call in `server.py` (the service sees `False`, refuses, and the
    response is a 500 with no summary to read).
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    client, app = _route_client_and_app(temp_storage)
    seen = _record_service_kwargs(monkeypatch, app, "run_multi_strategy_netted_deploy")

    res = client.post(
        "/api/deploy/multi",
        json=_multi_deploy_route_payload(confirm_empty_book=True),
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert seen and seen[0]["confirm_empty_book"] is True
    assert body["summary"]["holdings_seed_source"] == CONFIRMED_EMPTY_BOOK_SOURCE
    assert len(calls) == 1, "the confirmed netted deploy must launch exactly one child"
    # The empty matrix book is represented as the cash proxy, never as positions.
    holdings_file = calls[0][calls[0].index("--current-holdings-file") + 1]
    assert temp_storage._load_json(holdings_file, default=None) == {
        "BIL": 100000.0
    }


# ---------------------------------------------------------------------------
# 9. (2026-10-30 review, MINOR) An all-unparsable book IS an empty engine book
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (the hole this section closes):
#   `book_is_empty` deliberately judges emptiness per VALUE -- "unknown is never zero" -- so
#   `{"600900.SH": "abc"}` reads as "not empty".  The explicit-book arm then labelled it
#   `operator_input` and never asked for confirmation.  But the engine does not size from the
#   raw payload: `_shares_book` DROPS every row it cannot parse, so that same book becomes
#   `{}` downstream and the child sizes a FULL LIQUIDATION (a SELL for every held symbol) with
#   no `operator_confirmed_empty_book` label and no refusal -- exactly the failure the
#   empty-book gate exists to prevent.  The gate therefore has to judge emptiness by what the
#   ENGINE will size from, not only by per-value provability.
#   The 2026-10-30 `600584.SH` / `600362.SH` incident is why this class is live-money risk: a
#   book that does not describe the account produced real orders on symbols already exited.
#   NON-REGRESSION PINNED BELOW: a fractional WEIGHT-mode book (`{"600900.SH": 0.05}`, the
#   `is_weight_mode` arm of `scripts/run_live_strategy.py`) coerces to `{"600900.SH": 0}` --
#   a NON-empty dict -- and must stay an accepted `operator_input`.  That is why the gate tests
#   DICT emptiness of `_shares_book(...)`, never `book_is_empty(_shares_book(...))`.

_UNPARSABLE_BOOK = {"600900.SH": "abc"}


def test_all_unparsable_book_is_gated_like_an_empty_one(
    temp_storage, tmp_path, monkeypatch
):
    """`{"600900.SH": "abc"}` collapses to `{}` in the engine => same gate as `{}`.

    The module must refuse BEFORE any side effect: an accepted booking here would hand the
    child an empty book and the ticket would sell every held symbol.
    re-break: drop the `and _shares_book(holdings)` conjunct from
    `resolve_explicit_book_source` -- the unparsable book returns `operator_input`, the gate is
    never consulted, and the engine sizes a full liquidation from `{}`.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)

    with pytest.raises(EmptyBookConfirmationRequiredError) as excinfo:
        service.run_live_deploy(
            **_deploy_kwargs(tmp_path, current_holdings=dict(_UNPARSABLE_BOOK))
        )

    # The refusal is the ordinary empty-book one, naming the escape hatch.
    assert "confirm_empty_book" in str(excinfo.value)
    assert isinstance(excinfo.value, LiveSeedBookMissingError)
    assert calls == [], "a refused deploy must never launch the child process"
    # ... and it left no book file behind: on this path the run dir is not even allocated.
    written = [
        name
        for _root, _dirs, files in os.walk(temp_storage.archive_dir)
        for name in files
    ]
    assert "holdings.json" not in written, written


def test_confirmed_all_unparsable_book_is_labelled_as_confirmed_empty(
    temp_storage, tmp_path, monkeypatch, caplog
):
    """The same book + `confirm_empty_book=True` deploys as CONFIRMED all-cash, never `operator_input`.

    Only the LABEL changes with the confirmation: an archived `operator_input` would tell an
    auditor the round was sized from a real position list, which this book never was.
    re-break: make `resolve_explicit_book_source` return `EXPLICIT_BOOK_SOURCE` for an
    engine-empty book (i.e. keep the pre-fix `if not book_is_empty(holdings)` early return) --
    the archived summary would then say `operator_input` for a full liquidation.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    caplog.set_level(logging.WARNING)
    service = ExecutionService(temp_storage)

    res = service.run_live_deploy(
        **_deploy_kwargs(
            tmp_path, current_holdings=dict(_UNPARSABLE_BOOK), confirm_empty_book=True
        )
    )

    assert res["status"] == "success"
    assert len(calls) == 1
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == CONFIRMED_EMPTY_BOOK_SOURCE
    assert summary["holdings_seed_source"] != EXPLICIT_BOOK_SOURCE
    # A confirmed all-cash round is never quiet about the liquidation it implies.
    assert "[empty-book]" in caplog.text


def test_mixed_book_with_one_unparsable_row_is_still_operator_input(
    temp_storage, tmp_path, monkeypatch
):
    """One garbage row does NOT make the book empty: the engine still holds `601728.SH`.

    This is the anti-over-refusal half.  `_shares_book` drops only the row it cannot parse, so
    the child sizes a real position -- there is no full liquidation to gate.
    re-break: test `book_is_empty(holdings)` alone (pre-fix) is fine here, but gating on
    `len(_shares_book(holdings)) != len(holdings)` -- or on any "all rows must parse" rule --
    would refuse a legitimate book that merely carries one stale/typo'd row.
    """
    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)
    book = {"600900.SH": "abc", "601728.SH": 5800}

    res = service.run_live_deploy(**_deploy_kwargs(tmp_path, current_holdings=book))

    assert res["status"] == "success"
    assert len(calls) == 1
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == EXPLICIT_BOOK_SOURCE == "operator_input"
    # The child really was handed the untouched payload (the engine drops the bad row itself).
    holdings_file = calls[0][calls[0].index("--current-holdings-file") + 1]
    assert temp_storage._load_json(holdings_file, default=None) == book


def test_fractional_weight_book_is_not_mistaken_for_an_empty_one(
    temp_storage, tmp_path, monkeypatch
):
    """WEIGHT MODE NON-REGRESSION: `{"600900.SH": 0.05}` is a real book, never a liquidation.

    `is_weight_mode` (`scripts/run_live_strategy.py`) accepts fractional weights, and
    `_shares_book` coerces `0.05 -> 0` shares -- a NON-empty dict holding a zero-share row the
    child re-derives from the weight.  Judging emptiness by `book_is_empty(_shares_book(...))`
    would call that "empty" and refuse (or confirm-gate) a perfectly legitimate weight book.
    re-break: change the gate to `if not book_is_empty(holdings) and not
    book_is_empty(_shares_book(holdings)):` -- this book then raises
    `EmptyBookConfirmationRequiredError` even though it is an ordinary weight-mode payload.
    """
    from execution_service import _shares_book

    monkeypatch.delenv("LIVETRADING_STRICT_SEED_BOOK", raising=False)
    calls = _patch_deploy_subprocess(monkeypatch)
    service = ExecutionService(temp_storage)
    book = {"600900.SH": 0.05}

    # The coercion that makes this arm delicate: a NON-empty dict of ZERO shares.
    assert _shares_book(book) == {"600900.SH": 0}

    res = service.run_live_deploy(**_deploy_kwargs(tmp_path, current_holdings=book))

    assert res["status"] == "success"
    assert len(calls) == 1
    summary = _archived_summary(temp_storage, calls[0])
    assert summary["holdings_seed_source"] == EXPLICIT_BOOK_SOURCE == "operator_input"
    holdings_file = calls[0][calls[0].index("--current-holdings-file") + 1]
    assert temp_storage._load_json(holdings_file, default=None) == book


@pytest.mark.parametrize(
    "holdings, expected",
    [
        ({}, True),
        (None, True),
        ({"600900.SH": 0}, True),
        ({"600900.SH": "0"}, True),
        ({"600900.SH": None}, True),
        ({"600900.SH": {"shares": 0}}, True),
        ({"600900.SH": 100}, False),
        ({"600900.SH": "abc"}, False),  # unknown is never zero -- the gate adds the engine test
    ],
)
def test_book_is_empty_semantics_are_unchanged(holdings, expected):
    """The `book_is_empty` predicate itself is NOT redefined by the engine-book gate.

    It must keep meaning "no PROVABLE position", so a future refactor cannot fold the engine's
    row-dropping into it (which would make an unparsable row read as "all cash").
    re-break: redefine `book_is_empty` as `not _shares_book(holdings)` (unparsable => empty) or
    as `len(holdings) == 0` (all-zero table => a real book).
    """
    assert book_is_empty(holdings) is expected


# ---------------------------------------------------------------------------
# 10. DISPLAY book (25.9.11): a completed Stage-2 round shows its POST book
#
# The web UI painted a historical date's PRE-trade INPUT (`holdings.json`) instead
# of the round's post book, so `2026-10-29` displayed the ghost `600584.SH: 100` /
# `600362.SH: 100` lots that were sold and filled on 2026-10-26 -- although the same
# dir's `post_holdings.json` already held the corrected 5-symbol book.
#
# One rule, one place: `StorageManager._display_book_for_run_dir`.  A dir that
# carries a Stage-2 artifact (`has_stage2_artifact`) DISPLAYS its planned post book
# and exposes the raw submitted book separately as `pre_trade_holdings`; a dir with
# no Stage-2 artifact still displays its operator snapshot (unchanged).  DISPLAY
# ONLY -- `resolve_seed_holdings` never calls the helper (pinned by test 5 below).
# ---------------------------------------------------------------------------

#: The real `2026-10-29/215823/holdings.json` shape (ghost lots deliberately preserved).
_GHOST_BOOK = {
    "600900.SH": 800,
    "600660.SH": 400,
    "000999.SZ": 600,
    "600584.SH": 100,  # sold + filled 2026-10-26; still in the PRE-trade input
    "600362.SH": 100,  # same
    "601728.SH": 5800,
}
#: The same dir's corrected `post_holdings.json` (5 symbols, ghost lots gone).
_CORRECTED_BOOK = {
    "601728.SH": 5800,
    "601919.SH": 2400,
    "000999.SZ": 600,
    "600660.SH": 400,
    "600900.SH": 800,
}


def test_get_run_displays_the_post_book_and_keeps_the_ghost_as_pre_trade(temp_storage):
    """`get_run()["holdings"]` is the 5-symbol POST book; the ghost survives as `pre_trade_holdings`.

    re-break: revert `get_run` to read `holdings.json` directly into `bundle["holdings"]`
    (the pre-25.9.11 code) -- the UI then paints `600584.SH` / `600362.SH` again.
    """
    _seed_run_date(
        temp_storage,
        "2026-10-29",
        holdings=dict(_GHOST_BOOK),
        post_holdings=dict(_CORRECTED_BOOK),
    )

    bundle = temp_storage.get_run(
        "2026-10-29", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )

    assert bundle is not None
    assert bundle["holdings"] == _CORRECTED_BOOK, "the displayed book must be the plan"
    assert "600584.SH" not in bundle["holdings"]
    assert "600362.SH" not in bundle["holdings"]
    assert bundle["holdings_source"] == "post_trade_book"
    # The evidence is NOT hidden: the raw submitted book is reachable under its own key.
    assert bundle["pre_trade_holdings"] == _GHOST_BOOK
    # `post_holdings` is byte-for-byte unchanged from before this fix.
    assert bundle["post_holdings"] == _CORRECTED_BOOK


def test_get_holdings_does_not_return_the_ghost_book_for_a_completed_round(temp_storage):
    """`get_holdings()` (the `/api/holdings` outlet) also returns the post book, not the ghost.

    re-break: revert the `get_holdings` step-1 change back to
    `_load_snapshot_holdings(...)` -- `GET /api/holdings` then serves the ghost book.
    """
    _seed_run_date(
        temp_storage,
        "2026-10-29",
        holdings=dict(_GHOST_BOOK),
        post_holdings=dict(_CORRECTED_BOOK),
    )

    book = temp_storage.get_holdings(
        "2026-10-29", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )

    assert book == _CORRECTED_BOOK
    assert "600584.SH" not in book
    assert "600362.SH" not in book


def test_stage2_dir_without_a_resolvable_plan_labelled_falls_back_to_the_snapshot(temp_storage):
    """Artifact but NO plan: show the raw snapshot LABELLED, never an empty book.

    An old ticket carrying only `current_shares` (no `target_shares`) and no
    `post_holdings.json` has no resolvable plan.  Blanking the display to `{}` would be
    worse than showing a book explicitly labelled as the pre-trade one.
    re-break: return `{}` / `None` from the helper's `pre_trade_fallback` arm instead of
    falling back to `_load_snapshot_holdings(run_dir)`.
    """
    target = _seed_run_date(temp_storage, "2026-10-29", holdings=dict(_GHOST_BOOK))
    # A Stage-2 artifact with no `target_shares` -> `post_holdings_from_ticket` returns None.
    _write_json(
        os.path.join(target, "live_trading_ticket.json"),
        [{"symbol": "600584.SH", "action": "SELL", "current_shares": 100}],
    )

    bundle = temp_storage.get_run(
        "2026-10-29", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )

    assert bundle is not None
    assert bundle["holdings"] == _GHOST_BOOK, "must not silently blank the display"
    assert bundle["holdings"] != {}
    assert bundle["holdings_source"] == "pre_trade_fallback"
    # Identical to the displayed book -> deliberately NOT duplicated under its own key.
    assert "pre_trade_holdings" not in bundle


def test_dir_without_a_stage2_artifact_displays_its_operator_snapshot(temp_storage):
    """An operator-only snapshot dir is untouched: `holdings` is the snapshot, source `snapshot`.

    re-break: apply the post-book rule unconditionally (drop the `has_stage2_artifact`
    guard) -- an operator snapshot would then be mislabelled / overridden.
    """
    _seed_run_date(temp_storage, "2026-10-29", holdings={"AAA.SH": 300})

    bundle = temp_storage.get_run(
        "2026-10-29", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )

    assert bundle is not None
    assert bundle["holdings"] == {"AAA.SH": 300}
    assert bundle["holdings_source"] == "snapshot"
    assert "pre_trade_holdings" not in bundle


def test_display_book_rule_does_not_change_seed_resolution(temp_storage):
    """Seed-path neutrality: `resolve_seed_holdings` ignores the display helper.

    For the case-1 dir it still returns the POST book with source `operator_snapshot`
    (its own precedence, reached independently of `_display_book_for_run_dir`), so order
    sizing cannot move because of a display fix.
    re-break: make `_display_book_for_run_dir` feed `resolve_seed_holdings` -- e.g. have the
    seed's plan leg return the helper's label -- `source` becomes `"post_trade_book"` and the
    deploy-path contract drifts (verified red).
    """
    _seed_run_date(
        temp_storage,
        "2026-10-29",
        holdings=dict(_GHOST_BOOK),
        post_holdings=dict(_CORRECTED_BOOK),
    )

    seed, source = temp_storage.resolve_seed_holdings(
        "2026-10-29", _STRATEGY, _UNIVERSE
    )

    assert seed == _CORRECTED_BOOK
    assert source == "operator_snapshot"


def test_multi_netted_dir_displays_its_netted_plan_not_the_ghost_snapshot(temp_storage):
    """A MULTI-STRATEGY netted dir obeys the same display rule (25.9.11b).

    `get_run` reaches those dirs through `get_multi_netting_run_dir` (a matrix id is
    passed as `strategy_key`).  Such a dir writes `netted_trading_ticket.json` and a
    pre-trade `holdings.json`, and has NO `post_holdings.json` and NO
    `live_trading_ticket.*` -- so a helper that only tried the single-strategy ticket
    name would find "artifact present, plan unresolvable" and display the PRE-trade
    seed: exactly the ghost-class defect this whole section removes.

    re-break: pass only `("live_trading_ticket",)` to `_planned_book_from_run_dir` inside
    `_display_book_for_run_dir` -- `holdings_source` becomes `pre_trade_fallback` and
    `holdings` is the ghost snapshot again (verified red).
    """
    matrix_id = "m1"
    netted_dir = temp_storage.get_multi_netting_run_dir(matrix_id, "2026-10-29")
    os.makedirs(netted_dir, exist_ok=True)
    _write_json(
        os.path.join(netted_dir, "netted_trading_ticket.json"),
        [
            {"symbol": "601728.SH", "action": "BUY", "target_shares": 5800},
            {"symbol": "601919.SH", "action": "BUY", "target_shares": 2400},
            {"symbol": "600900.SH", "action": "HOLD", "target_shares": 800},
        ],
    )
    # Pre-trade seed carrying a lot the model already exited.
    _write_json(
        os.path.join(netted_dir, "holdings.json"),
        {"601728.SH": 5800, "600584.SH": 100},
    )

    bundle = temp_storage.get_run("2026-10-29", strategy_key=matrix_id)

    assert bundle is not None
    assert bundle["holdings_source"] == "post_trade_book"
    assert bundle["holdings"] == {
        "601728.SH": 5800,
        "601919.SH": 2400,
        "600900.SH": 800,
    }
    assert "600584.SH" not in bundle["holdings"]
    # Still not hidden: the submitted seed is reachable under its own key.
    assert bundle["pre_trade_holdings"] == {"601728.SH": 5800, "600584.SH": 100}


def test_get_run_and_get_holdings_agree_on_the_instance_walk(temp_storage):
    """One instance walk, both outlets (25.9.11c).

    The newest instance carries NO book; an EARLIER instance of the same session
    completed Stage 2 and also holds the pre-trade seed.  `/api/runs/...` used to read
    only `holdings.json` on that walk, so it painted the ghost while `/api/holdings`
    (already on the shared helper) painted the plan -- the two screens disagreed about
    the same session.

    re-break: put `self._load_snapshot_holdings(instance_dir)` back into `get_run`'s
    instance walk -- `bundle["holdings"]` becomes `_GHOST_BOOK` again while
    `get_holdings` keeps returning `_CORRECTED_BOOK` (verified red).
    """
    _seed_run_date(
        temp_storage,
        "2026-10-29",
        holdings=dict(_GHOST_BOOK),
        post_holdings=dict(_CORRECTED_BOOK),
    )
    # Newest instance of the same session: a later round that carries no book at all.
    _seed_run_date(temp_storage, "2026-10-29", instance="120000")

    bundle = temp_storage.get_run(
        "2026-10-29", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )
    book = temp_storage.get_holdings(
        "2026-10-29", strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )

    assert bundle is not None
    assert bundle["holdings"] == _CORRECTED_BOOK
    assert "600584.SH" not in bundle["holdings"]
    assert book == _CORRECTED_BOOK
    assert bundle["holdings"] == book, "the two outlets must not disagree"
    # `holdings_source` still describes the PRIMARY (newest) dir's rule, which here has
    # no book at all -- documented: it can read `snapshot` while the VALUE was carried
    # forward from an earlier instance.
    assert bundle["holdings_source"] == "snapshot"


# ---------------------------------------------------------------------------
# 11. (P0) ONE owner for the path -> universe_key rule
#
# `resolve_keys` (what a deploy WRITES under) and `discover_universes` (what the UI
# LETS THE OPERATOR PICK) each derived that key with their own spelling of the same
# rule.  They agreed only because both baked in the root's name -- one as the literal
# set ("universe", "docs", "."), the other as `basename(self.universe_dir)`.  A probe
# over all 29 presets under `docs/universe/**` plus both `custom_universes.json`
# entries found ZERO divergence with the shipped layout; the split is LATENT and
# appears as soon as `universe_dir` points anywhere else (a per-deployment folder, a
# renamed tree), where the UI would advertise `<folder>_<stem>` while every archive
# write and lookup used the bare `<stem>`.  One universe, two keys, no run able to
# match.  `universe_key_from_path` is now the single implementation and both sites
# call it, so the divergence cannot be reintroduced by editing one side only.
#
# The refactor is pinned two ways: the shipped presets must keep the exact key they
# had (the first test -- a no-op proof, not a new rule), and a file sitting directly in
# a root whose name is NOT "universe" must collapse to the bare stem on both paths
# (the second).  Neither test can be satisfied by the old pair of spellings.
# ---------------------------------------------------------------------------


def _storage_with_universe(tmp_path, universe_dir):
    """A `tmp_path` StorageManager whose UNIVERSE root is the caller's choice.

    Every writable directory stays inside ``tmp_path`` (the package ``conftest``
    would do this anyway; passing them explicitly keeps the intent local).
    """
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
        universe_dir=str(universe_dir),
    )


def _preset_files(universe_dir):
    """Every ``.txt`` under ``universe_dir``, in `os.walk` order -- exactly what
    ``discover_universes`` walks (symlinks to FILES included, symlinked dirs not)."""
    return sorted(
        os.path.join(root, f)
        for root, _dirs, files in os.walk(universe_dir)
        for f in files
        if f.endswith(".txt")
    )


#: Three shipped presets whose key is pinned to its exact historical value, one per
#: shaping rule the derivation applies (all values observed BEFORE the P0 refactor):
#: a plain ASCII folder prefix, a CJK folder whose `A股` lowercases and whose `(...)`
#: collapses to `_`, and a SYMLINKED file (the link and the file it points at are two
#: distinct universes -- `china/core_satellite_22_stocks.txt` vs the CJK-folder copy).
_ANCHORED_PRESET_KEYS = {
    "china/20_etf.txt": "china_20_etf",
    "china/50只A股高流动性优质股票精选/11_stocks.txt": "50只a股高流动性优质股票精选_11_stocks",
    "china/core_satellite_22_stocks.txt": "china_core_satellite_22_stocks",
}


def test_universe_key_from_path_matches_discover_for_every_preset(tmp_path):
    """Every shipped preset keeps the key the UI has always shown it under.

    This is the NO-OP half of the P0 dedup: `resolve_keys` and
    `discover_universes` used to disagree structurally, so routing both through
    `universe_key_from_path` is only safe if not one of the 29 real files moved.
    The probe that motivated the pass found 0 mismatches; this test keeps it that
    way permanently, on the real tree, read-only.  Two assertions per file: the
    two code paths agree (a one-sided edit cannot slip through), and the anchored
    samples still produce their historical spelling (the keys are unchanged).

    re-break: make `discover_universes` derive the key WITHOUT the folder prefix
    (`universe_key = os.path.splitext(f)[0]` -- dropping the helper call on that
    side) -- every one of the 29 nested presets mismatches (verified red: the loop
    stops on the first, `china/14_etf_top3_pruned.txt` reporting
    `14_etf_top3_pruned` where `china_14_etf_top3_pruned` is expected).  Stemming
    the helper from the bare FILENAME instead of `splitext(...)[0]` trips the
    anchored sample below (`china_20_etf_txt`, verified red).  NOTE: reintroducing
    the historical hardcoded `("universe", "docs", ".")` parent set into the helper
    is measurably INERT on these 29 files -- none sits in the universe root, their
    parents are `china` / `hongkong` / `us` / `<CJK dir names>` -- so that half of
    the predicate is pinned by the root-level test below, not here.
    """
    docs_universe = os.path.join(_REPO_ROOT, "docs", "universe")
    if not os.path.isdir(docs_universe):
        pytest.skip(f"no preset universe tree at {docs_universe} (CI layout?)")

    storage = _storage_with_universe(tmp_path, docs_universe)
    discovered = {
        u["path"]: u["universe_key"] for u in storage.discover_universes() if u.get("path")
    }
    presets = _preset_files(docs_universe)
    assert presets, "docs/universe must expose at least one preset for this to mean anything"
    assert all(p in discovered for p in presets), "discover_universes skipped a file it walks"

    for path in presets:
        rel = os.path.relpath(path, docs_universe)
        assert universe_key_from_path(path, docs_universe) == discovered[path], (
            f"resolved key diverges from the advertised one for {rel}"
        )
        if rel in _ANCHORED_PRESET_KEYS:
            assert discovered[path] == _ANCHORED_PRESET_KEYS[rel], (
                f"the key for {rel} moved; every archived round under it would be orphaned"
            )


def test_resolve_keys_and_discover_agree_for_docs_level_file(tmp_path):
    """A file DIRECTLY in the universe root collapses to the bare stem -- both paths.

    The root is deliberately named ``pool``, not ``universe``/``docs``: that is
    the whole point of the P0 pass.  The historical pair of predicates agreed
    here only because the shipped root happens to be called ``docs/universe``.
    A nested file of the SAME name must still be disambiguated by its folder, so
    the assertion cannot be satisfied by "always use the bare stem" either.

    re-break: replace the `universe_key_from_path` call in `resolve_keys` with the
    naive `f"{parent}_{stem}"` -- it returns `pool_flat_pool` while
    `discover_universes` (still on the helper) shows `flat_pool`, so the
    bare-stem expectation fails (verified red).  Putting the hardcoded
    `("universe", "docs", ".")` set back into the helper fails the same two
    assertions for the same reason (verified red).
    """
    universe_root = tmp_path / "pool"
    (universe_root / "nested").mkdir(parents=True)
    for path in (universe_root / "flat_pool.txt", universe_root / "nested" / "flat_pool.txt"):
        path.write_text("600000.SH\n601398.SH\n", encoding="utf-8")

    storage = _storage_with_universe(tmp_path, universe_root)
    discovered = {
        u["path"]: u["universe_key"] for u in storage.discover_universes() if u.get("path")
    }
    root_file = str(universe_root / "flat_pool.txt")
    nested_file = str(universe_root / "nested" / "flat_pool.txt")

    # Root-level: the folder IS the universe root, so it is not a prefix.
    assert universe_key_from_path(root_file, str(universe_root)) == "flat_pool"
    assert storage.resolve_keys("", universe_file=root_file)[1] == "flat_pool"
    assert discovered[root_file] == "flat_pool"

    # One level down: the folder is a real group name and stays a prefix.
    assert universe_key_from_path(nested_file, str(universe_root)) == "nested_flat_pool"
    assert storage.resolve_keys("", universe_file=nested_file)[1] == "nested_flat_pool"
    assert discovered[nested_file] == "nested_flat_pool"


def test_legacy_suffix_dir_still_resolves(temp_storage):
    """The legacy `<name>_custom` DIRECTORY convention still answers to `custom_<name>`.

    15 archived rounds live under `chan_four_state_blend/astock_202609_20_custom/`
    (a suffix-style directory name) while every UI selection asks for
    `custom_astock_202609_20` (prefix style).  Nothing in the P0 dedup may narrow
    that bridge: it is reached only through `get_universe_key_variants` /
    `keys_match` / `find_existing_run_base_dir`, all untouched by the refactor --
    this test is the guard that keeps them untouched.

    The two rounds are deliberately shaped differently, because they are bridged
    by different halves of the alias:

    * `2026-10-20` has a `summary.json` recording the PREFIX key, like every real
      round in that directory, so the listing matches verbatim and only PATH
      resolution needs the alias;
    * `2026-10-19` predates that and has no summary at all, so its key is derived
      from the DIRECTORY name (`astock_202609_20_custom`) and only
      `normalize_universe_key`'s `_custom` arm can match it.

    re-break: drop the `k.endswith("_custom")` arm from `normalize_universe_key`
    -- the summary-less `2026-10-19` round disappears from the selection's listing
    (and the `2026-10-20` one is merely rescued by its summary; verified red:
    listing becomes `["2026-10-20"]`).  Dropping the `_custom` suffix arm from
    `get_universe_key_variants` alone is measurably INERT here (the normalisation
    still bridges it -- verified) but it is what makes
    `find_existing_run_base_dir` work at all: remove it TOGETHER with the
    normalisation arm and that lookup returns `None` (verified red).
    """
    legacy_dir = os.path.join(
        temp_storage.archive_dir, "chan_four_state_blend", "astock_202609_20_custom"
    )

    # A real round: summary carries the prefix-style selection key.
    dated = os.path.join(legacy_dir, "2026-10-20")
    os.makedirs(dated)
    with open(os.path.join(dated, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "date": "2026-10-20",
                "strategy_key": "chan_four_state_blend",
                "universe_key": "custom_astock_202609_20",
            },
            f,
        )
    with open(os.path.join(dated, "live_trading_ticket.json"), "w", encoding="utf-8") as f:
        json.dump([], f)

    # An older round with no summary: its key comes from the directory name.
    older = os.path.join(legacy_dir, "2026-10-19")
    os.makedirs(older)
    with open(os.path.join(older, "live_trading_ticket.json"), "w", encoding="utf-8") as f:
        json.dump([], f)

    runs = temp_storage.list_runs(
        strategy_key="chan_four_state_blend",
        universe_key="custom_astock_202609_20",
        require_both=True,
    )
    assert sorted(r["date"] for r in runs) == ["2026-10-19", "2026-10-20"]

    assert (
        temp_storage.find_existing_run_base_dir(
            "chan_four_state_blend", "custom_astock_202609_20", "2026-10-20"
        )
        == dated
    )
    bundle = temp_storage.get_run(
        "2026-10-20",
        strategy_key="chan_four_state_blend",
        universe_key="custom_astock_202609_20",
    )
    assert bundle is not None
    assert bundle["universe_key"] == "custom_astock_202609_20"


# ---------------------------------------------------------------------------
# 12. (P1) The dropdown must report the symbols the RUNNER will load
#
# `discover_universes` used to build each preset's symbol list from the FIRST
# whitespace token of each line (`s.replace(",", " ").split()[0].upper()`), while
# the runner -- `FileUniverseProvider.get_symbols` in `common/universe.py`, what
# `--universe <file>` actually loads -- takes EVERY token of every line.  So the
# dropdown label "[N只标的]", the editable symbol box and the count the operator
# judges the selection by were describing a universe the run would not use:
# `china/20_etf.txt` advertised 1 symbol and ran 20, `china/global_etf.txt` 1 vs 29,
# `47_stock_without_ai_outliers.txt` 1 vs 47, `31_stock_without_losers.txt` 1 vs 31.
#
# The fix is not a better copy of the parser: `FileUniverseProvider` is imported and
# asked, so the list has ONE owner.  These tests are what keep it that way -- they
# compare the dropdown against the provider itself for every shipped preset, so a
# future local re-spelling cannot pass by agreeing with a fixture.
#
# For PRESETS the textarea is display-only: `static/js/app.js` sends
# `custom_symbols: null` unless the selected option's `dataset.type === "custom"`
# (`getExecutionParams`, ~line 1729), and `execution_service._resolve_universe`
# prefers a located `universe_file` anyway.  Fixing the list therefore cannot move a
# share -- it corrects what the operator is shown.
# ---------------------------------------------------------------------------


def _runner_symbols(path):
    """The list the RUNNER builds, straight from `common/universe.py`.

    Test-only import of the real parser (never a re-statement of it), so these
    assertions measure the UI against the thing that actually trades.
    """
    from common.universe import FileUniverseProvider  # noqa: E402  (repo root on sys.path above)

    return FileUniverseProvider(path).get_symbols()


def test_discovered_symbols_match_file_universe_provider_for_every_preset(tmp_path):
    """Every preset's advertised symbol list IS the runner's symbol list.

    One rule, one owner: `discover_universes` asks `FileUniverseProvider`, and this
    test asks it too, so the two can only agree.  `count` must remain
    `len(symbols)` for the "[N只标的]" label.

    re-break: restore the first-token read in `discover_universes`
    (`s.replace(",", " ").split()[0].upper()` per line) -- the four single-line
    presets fail: `china/20_etf.txt` reports 1 symbol where the provider returns 20,
    plus `china/global_etf.txt` (29), `47_stock_without_ai_outliers.txt` (47) and
    `31_stock_without_losers.txt` (31) (verified red).
    """
    docs_universe = os.path.join(_REPO_ROOT, "docs", "universe")
    if not os.path.isdir(docs_universe):
        pytest.skip(f"no preset universe tree at {docs_universe} (CI layout?)")

    storage = _storage_with_universe(tmp_path, docs_universe)
    presets = [u for u in storage.discover_universes() if u.get("type") == "preset"]
    assert presets, "docs/universe must expose at least one preset"

    for entry in presets:
        assert entry["symbols"] == _runner_symbols(entry["path"]), (
            f"{os.path.relpath(entry['path'], docs_universe)}: the dropdown would "
            f"describe {len(entry['symbols'])} symbols, the runner loads "
            f"{len(_runner_symbols(entry['path']))}"
        )
        assert entry["count"] == len(entry["symbols"])


def test_preset_symbol_count_is_not_one_when_the_file_has_many(tmp_path):
    """The exact incident, shrunk: twenty tickers on ONE line are twenty symbols.

    Deliberately not a preset from `docs/`: the regression is about the parsing
    rule, and this file makes the expected count its own documentation.  The line
    is space-separated and mixes a comma to exercise the provider's separator set.

    re-break: same first-token mutation in `discover_universes` -- the count
    collapses to 1 and the list to `["600000.SH"]` (verified red).
    """
    symbols = [f"60{i:04d}.SH" for i in range(20)]
    line = " ".join(symbols[:10]) + ", " + " ".join(symbols[10:])
    universe_root = tmp_path / "pool"
    universe_root.mkdir()
    (universe_root / "twenty_on_one_line.txt").write_text(line + "\n", encoding="utf-8")

    storage = _storage_with_universe(tmp_path, universe_root)
    entry = next(
        u for u in storage.discover_universes() if u.get("filename") == "twenty_on_one_line.txt"
    )

    assert entry["count"] == 20
    assert entry["symbols"] == symbols


def test_header_row_in_50_stock_is_reported_by_both_paths(tmp_path):
    """`50_stock.txt` carries a CSV header row, and BOTH paths report it -- by design.

    `证券代码` is not a ticker; it is the first line of the FILE.  This is a
    universe-file DATA defect, not a parser defect: `FileUniverseProvider` has no
    header rule (it skips only blank and `#` lines), so the run itself has always
    loaded the header as a "symbol", and the dropdown now tells the truth about
    that.  Fixing it means editing `docs/universe/china/50只A股高流动性优质股票精选/
    50_stock.txt` (delete the header line) -- a data change this pass must NOT make,
    because it would change what the next run loads.

    re-break: add header-skipping to ONLY the UI side (e.g. drop a first line equal
    to `证券代码` inside `discover_universes`) -- the dropdown then loses one entry
    the runner keeps, so UI and runner diverge again and this test fails (verified
    red).  Do not "fix" it in the shared parser either: that WOULD change run output.
    """
    rel = os.path.join("china", "50只A股高流动性优质股票精选", "50_stock.txt")
    path = os.path.join(_REPO_ROOT, "docs", "universe", rel)
    if not os.path.exists(path):
        pytest.skip(f"no {rel} in this layout")

    storage = _storage_with_universe(tmp_path, os.path.join(_REPO_ROOT, "docs", "universe"))
    entry = next(u for u in storage.discover_universes() if u.get("path") == path)
    runner = _runner_symbols(path)

    assert entry["symbols"] == runner
    assert entry["count"] == len(runner)
    # The data defect, recorded verbatim and in position: the header is symbol #1.
    assert entry["symbols"][0] == "证券代码"
    assert runner[0] == "证券代码"


# ---------------------------------------------------------------------------
# 13. (P2) A round's reported universe key must not depend on `summary.json`
#
# FORENSIC CONTEXT.  `<ARCH>/chan_four_state_blend/astock_202609_20_custom` holds 15
# dated rounds.  Three of them (`2026-09-27`, `2026-09-28`, `2026-09-29`) carry no
# `summary.json`; the other twelve record `universe_key = "custom_astock_202609_20"`.
# Two claims were made about the three:
#
#   (i)  "they are invisible to every selection" -- a probe assertion.  FALSE, and
#        the data says so: `list_runs(strategy=chan_four_state_blend,
#        universe=custom_astock_202609_20)` returns all 15.  The probe compared the
#        ECHOED `run["universe_key"]` string to the selection instead of asking the
#        listing; `keys_match` (storage.py:2932-2941) normalises `custom_X` and
#        `X_custom` to the same key, so the summary-less three always answered.
#   (ii) a sibling test (`test_legacy_suffix_dir_still_resolves`) says a summary-less
#        sibling IS reachable through the alias layer.  TRUE -- and it is the whole
#        truth: reachability never depended on the summary.
#
# What WAS real: the three reported `astock_202609_20_custom` while their twelve
# siblings reported `custom_astock_202609_20`.  One physical directory, two keys,
# decided by whether a file happens to exist -- so any consumer that compares the
# echoed key as a string sees two sessions where there is one.  `list_runs` now
# reports the summary-less round under the SELECTION spelling (see
# `selection_spelling_for_universe_dir`), which is a move inside one alias class and
# therefore cannot change which selection a round answers to.
# ---------------------------------------------------------------------------

_P2_STRAT = "chan_four_state_blend"
_P2_UNIV_DIR = "astock_202609_20_custom"
_P2_UNIV_SELECTION = "custom_astock_202609_20"


def _seed_round(root, date_str, *, summary_universe_key=None):
    """One dated round dir; a summary only when `summary_universe_key` is given."""
    dated = os.path.join(root, _P2_STRAT, _P2_UNIV_DIR, date_str)
    os.makedirs(dated, exist_ok=True)
    with open(os.path.join(dated, "live_trading_ticket.json"), "w", encoding="utf-8") as f:
        json.dump([], f)
    if summary_universe_key is not None:
        with open(os.path.join(dated, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": date_str,
                    "strategy_key": _P2_STRAT,
                    "universe_key": summary_universe_key,
                },
                f,
            )
    return dated


def test_dates_without_summary_are_listed_under_the_selection_that_owns_them(temp_storage):
    """A summary-less round answers to the universe selection, in BOTH spellings.

    The shape is the real archive's: one physical universe directory whose dates
    disagree about whether they have a `summary.json`.  Both directions are checked,
    because they are bridged by different halves of the alias layer: the twelve
    summary-keyed dates only answer to the DIRECTORY spelling through
    `normalize_universe_key`, and a round keyed by the directory name only answers to
    the SELECTION spelling through it too.

    re-break: compare raw keys in the listing filter (`r_univ_key == universe_key`
    instead of `keys_match(...)`, storage.py:2934/2939) -- the summary-less date
    disappears from the selection spelling AND the summary-keyed date disappears from
    the directory spelling (verified red).  Removing the dir-name fallback instead
    drops the summary-less date at the `require_both` guard (2930), also red.
    """
    _seed_round(temp_storage.archive_dir, "2026-10-20", summary_universe_key=_P2_UNIV_SELECTION)
    _seed_round(temp_storage.archive_dir, "2026-10-19")

    def dates_for(universe_key):
        runs = temp_storage.list_runs(
            strategy_key=_P2_STRAT, universe_key=universe_key, require_both=True
        )
        return sorted(r["date"] for r in runs)

    assert dates_for(_P2_UNIV_SELECTION) == ["2026-10-19", "2026-10-20"]
    # Reverse direction: the physical DIRECTORY spelling must find both as well.
    assert dates_for(_P2_UNIV_DIR) == ["2026-10-19", "2026-10-20"]

    # `latest_only` must not collapse two different dates into one, now that both
    # report the same key: the dedupe key includes the date.
    latest = temp_storage.list_runs(
        strategy_key=_P2_STRAT,
        universe_key=_P2_UNIV_SELECTION,
        require_both=True,
        latest_only=True,
    )
    assert sorted(r["date"] for r in latest) == ["2026-10-19", "2026-10-20"]


def test_a_dates_key_does_not_depend_on_summary_presence(temp_storage):
    """Flipping `summary.json` in and out of one date dir must not move its key.

    This is the defect stated as an invariant: the same physical directory, the same
    date, the same round -- only the presence of a sidecar file changes.  The key the
    listing reports may not follow it, because "which universe is this round under"
    is a property of the archive layout, not of whether a writer got as far as
    emitting `summary.json`.

    re-break: revert `selection_spelling_for_universe_dir` to return the directory
    name unchanged (`X_custom` instead of `custom_X`) -- the summary-less pass
    reports `astock_202609_20_custom` and this assert fires (verified red).
    Deleting the dir-name fallback (storage.py:2897-2902) is the harsher mutation and
    also red: the round reports `""` and, with `require_both`, is dropped entirely.
    """
    dated = _seed_round(temp_storage.archive_dir, "2026-10-19")

    def key_for():
        runs = temp_storage.list_runs(strategy_key=_P2_STRAT)
        return {r["date"]: r["universe_key"] for r in runs}["2026-10-19"]

    without_summary = key_for()
    with open(os.path.join(dated, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "date": "2026-10-19",
                "strategy_key": _P2_STRAT,
                "universe_key": _P2_UNIV_SELECTION,
            },
            f,
        )
    with_summary = key_for()

    assert without_summary == with_summary == _P2_UNIV_SELECTION


def test_real_archive_three_legacy_dates_are_reachable(temp_storage):
    """The three summary-less real rounds: listed, and under the selection that owns them.

    Read-only against the production archive (skipped when it is not mounted).  The
    `StorageManager` is built against `temp_storage`'s dirs and the archive is then
    re-pointed by ATTRIBUTE, so not even `makedirs(exist_ok=True)` runs against the
    real tree.

    Records the resolution of the conflicting claims in section 13's header: the
    probe's "invisible" reading was an artefact of comparing the echoed key string;
    the sibling test's reading -- reachable through the alias layer -- is what the
    data supports.

    re-break: delete the dir-name fallback in `list_runs` -- the three summary-less
    rounds report `""` and `require_both` drops them, so `2026-09-27 is invisible to
    its own selection` (verified red).  Note that raw `==` in the listing filter does
    NOT re-break this test once the fallback re-spells into the selection spelling --
    it re-breaks test 1's reverse direction instead, which is why both are pinned.
    """
    archive = "/home/stone/Work/stock/simulate/202609/runs"
    if not os.path.isdir(archive):
        pytest.skip(f"no production archive at {archive}")

    temp_storage.archive_dir = archive  # attribute only: nothing is created on disk
    runs = temp_storage.list_runs(
        strategy_key=_P2_STRAT, universe_key=_P2_UNIV_SELECTION, require_both=True
    )
    dates = sorted(r["date"] for r in runs)

    for expected in ("2026-09-27", "2026-09-28", "2026-09-29"):
        assert expected in dates, f"{expected} is invisible to its own selection"
    assert len(dates) == 15, f"the session has 15 rounds, the listing returned {len(dates)}"

    # Every echoed key is inside the selection's alias class, whichever file exists.
    assert all(keys_match(_P2_UNIV_SELECTION, r["universe_key"]) for r in runs)


# ---------------------------------------------------------------------------
# 14. (P3) A scoped read must not be handed an unrelated legacy flat round
#
# FORENSIC CONTEXT.  `get_run` ended with an unconditional last resort:
#
#     legacy = os.path.join(self.archive_dir, date_str)
#     if is_run_dir(legacy):
#         run_dir = legacy
#
# It was written for the legacy flat layout (`<archive>/<date>/`, the archive's
# pre-`<strategy>/<universe>/` shape) and the UNKEYED route, which legitimately
# addresses a date without a scope.  But it ran for SCOPED reads too, so any
# strategy/universe pair asking for a date that only exists as a flat round was
# handed that round -- somebody else's ticket, echoed back with the REQUEST's
# `strategy_key` / `universe_key` (`chosen.get("strategy_key") or strategy_key`
# in the bundle).  Measured on the real archive before the fix:
#
#   get_run("2026-09-29", "chan_four_state_blend", "custom_astock_202609_20_new")
#       -> path <archive>/2026-09-29, strategy_key "chan_four_state_blend"
#          (the round is the accelerating_dual_momentum one)
#   get_run("2026-09-29", "chan_four_state_blend", "china_20_etf")
#       -> the same foreign round; `get_run_csv` leaked it identically.
#
# So a wrong selection produced another strategy's data, mislabelled, which is
# worse than an empty view.  The fallback is now UNSCOPED-ONLY, and nothing
# legitimate is lost by that: a scoped request whose keys match a legacy round is
# already answered by the `list_runs` search that precedes the fallback -- that
# search applies the same recorded/derived-key rule (`resolve_keys` +
# `keys_match`) the listing uses, so the history row for 2026-09-29 stays openable
# through the keyed route while `chan_four_state_blend/...` gets its 404.
# ---------------------------------------------------------------------------

_P3_DATE = "2026-09-29"
_P3_OTHER_STRAT = "accelerating_dual_momentum"
_P3_OTHER_UNIV = "2026_09_29_resolved_universe"


def _seed_legacy_flat_round(root, date_str, summary_extra):
    """One legacy flat round: `<root>/<date>/` with a summary and a Stage-2 ticket.

    Writes BOTH ticket files: `get_run` reads the JSON, `get_run_csv` the CSV, and the
    leak this section pins happened on both routes.
    """
    dated = os.path.join(root, date_str)
    os.makedirs(dated, exist_ok=True)
    with open(os.path.join(dated, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date_str, **summary_extra}, f)
    with open(os.path.join(dated, "live_trading_ticket.json"), "w", encoding="utf-8") as f:
        json.dump([{"action": "BUY", "symbol": "AAA", "shares": 1}], f)
    with open(os.path.join(dated, "live_trading_ticket.csv"), "w", encoding="utf-8") as f:
        f.write("action,symbol,shares\nBUY,AAA,1\n")
    return dated


def test_scoped_get_run_does_not_return_a_foreign_legacy_round(temp_storage):
    """A scoped miss is a miss -- never `<archive>/<date>`'s unrelated round.

    Shaped like the real archive: the only run on the date is a legacy flat round
    belonging to `accelerating_dual_momentum`.  A read scoped to a different pair must
    return nothing (the route then 404s), not that round relabelled with the request's
    keys.  Both directions of the leak are pinned: a custom-universe pair and a preset
    pair, on `get_run` and on `get_run_csv` (the CSV route had the same fallback).

    re-break: restore the unconditional fallback (`if is_run_dir(legacy): run_dir =
    legacy` without the `not strategy_key and not universe_key` guard, storage.py:3059 /
    3235) -- the flat round comes back echoed as `strategy_key='chan_four_state_blend'`
    (verified red).
    """
    flat = _seed_legacy_flat_round(
        temp_storage.archive_dir,
        _P3_DATE,
        {"strategy_key": _P3_OTHER_STRAT, "universe_key": _P3_OTHER_UNIV},
    )

    for strat, univ in (
        (_P2_STRAT, _P2_UNIV_SELECTION),  # custom universe
        (_P2_STRAT, "china_20_etf"),  # preset universe
    ):
        bundle = temp_storage.get_run(_P3_DATE, strategy_key=strat, universe_key=univ)
        assert bundle is None, f"scoped miss handed the {_P3_OTHER_STRAT} round"
        assert (bundle or {}).get("path") != flat
        assert temp_storage.get_run_csv(_P3_DATE, strategy_key=strat, universe_key=univ) is None


def test_unscoped_get_run_still_returns_the_legacy_round(temp_storage):
    """The legacy round stays viewable without a scope -- the fallback's real job.

    The unkeyed route (`GET /api/runs/{date_str}`, server.py:451) and pre-`<strategy>/
    <universe>/` browsing address a date with no keys at all; that path must be
    byte-identical to before the guard.  `None` and `""` both mean "unscoped" here,
    matching `find_existing_run_base_dir`'s own test (storage.py:2651-2656).

    re-break: refuse flat rounds to UNSCOPED reads too (an early `return None` when no
    scope was claimed and the resolved dir sits directly under the archive root), on top
    of the same refusal in `find_existing_run_base_dir` -- both reads return None
    (verified red).  That is deliberately the whole family: a keyless date read reaches
    the flat round by THREE independent paths (`find_existing_run_base_dir`'s unscoped
    branch 2651-2656, the `list_runs` search, and `get_run`'s own fallback), so deleting
    the fallback alone -- the exact change this pass made scoped-only -- cannot break
    this test, which is itself the proof that the fallback was never load-bearing for
    the unkeyed route.
    """
    flat = _seed_legacy_flat_round(
        temp_storage.archive_dir,
        _P3_DATE,
        {"strategy_key": _P3_OTHER_STRAT, "universe_key": _P3_OTHER_UNIV},
    )

    for strat, univ in ((None, None), ("", "")):
        bundle = temp_storage.get_run(_P3_DATE, strategy_key=strat, universe_key=univ)
        assert bundle is not None, f"unscoped read lost the legacy round for ({strat!r}, {univ!r})"
        assert os.path.normpath(bundle["path"]) == os.path.normpath(flat)
        assert temp_storage.get_run_csv(_P3_DATE, strategy_key=strat, universe_key=univ) is not None


def test_scoped_get_run_returns_a_legacy_round_whose_summary_matches(temp_storage):
    """The guard is a MATCH test, not a blanket ban on scoped legacy reads.

    A flat round whose summary records the requested keys IS that scope, so it must be
    returned -- and by `keys_match`, not raw equality, which is why the request also
    works with the directory-style alias `astock_202609_20_custom` for the custom key.

    re-break: refuse scoped legacy reads outright -- `if not run_dir and (strategy_key
    or universe_key): return None` before the `list_runs` search.  That is the
    over-correction this test guards against (the key rule lives in the search, not in
    the fallback); it goes red here and in the derived-key test while the
    foreign-round test stays green (verified red).
    """
    flat = _seed_legacy_flat_round(
        temp_storage.archive_dir,
        _P3_DATE,
        {"strategy_key": _P2_STRAT, "universe_key": _P2_UNIV_SELECTION},
    )

    for univ in (_P2_UNIV_SELECTION, _P2_UNIV_DIR):  # alias spellings of one key
        bundle = temp_storage.get_run(_P3_DATE, strategy_key=_P2_STRAT, universe_key=univ)
        assert bundle is not None, f"matching legacy round refused for universe {univ!r}"
        assert os.path.normpath(bundle["path"]) == os.path.normpath(flat)
        assert temp_storage.get_run_csv(_P3_DATE, strategy_key=_P2_STRAT, universe_key=univ)


def test_scoped_get_run_matches_a_keyless_legacy_round_by_its_derived_keys(temp_storage):
    """The real 2026-09-29 shape: no keys recorded, only `strategy_file`/`universe_file`.

    That round's summary predates key recording, but `list_runs` still shows it under
    `accelerating_dual_momentum` / `2026_09_29_resolved_universe` (the `resolve_keys`
    legacy branch) -- and the UI opens history rows through the SCOPED route only.  A
    strict "recorded keys or nothing" guard would make a listed round unopenable, so the
    guard asks the same `resolve_keys` owner for the round's keys.  A foreign pair is
    still refused.

    re-break: same over-correction as the previous test -- `if not run_dir and
    (strategy_key or universe_key): return None` before the `list_runs` search, i.e.
    refusing every scoped legacy read instead of asking `resolve_keys` what the round
    is (verified red).  The derivation itself has ONE owner (`list_runs`'s legacy
    branch); this test pins that the keyed route reaches it.
    """
    flat = _seed_legacy_flat_round(
        temp_storage.archive_dir,
        _P3_DATE,
        {
            "strategy_file": f"/legacy/{_P3_OTHER_STRAT}_strategy.json",
            "universe_file": f"/legacy/{_P3_DATE}/resolved_universe.txt",
        },
    )

    bundle = temp_storage.get_run(
        _P3_DATE, strategy_key=_P3_OTHER_STRAT, universe_key=_P3_OTHER_UNIV
    )
    assert bundle is not None, "a listed legacy round became unopenable through the keyed route"
    assert os.path.normpath(bundle["path"]) == os.path.normpath(flat)
    assert temp_storage.get_run_csv(
        _P3_DATE, strategy_key=_P3_OTHER_STRAT, universe_key=_P3_OTHER_UNIV
    )

    # Still not a free pass: a different scope gets nothing.
    assert (
        temp_storage.get_run(_P3_DATE, strategy_key=_P2_STRAT, universe_key=_P2_UNIV_SELECTION)
        is None
    )


# ---------------------------------------------------------------------------
# 15. (P4) A multi-strategy (matrix) round must record the universe it ran on
#
# FORENSIC CONTEXT.  `MultiStrategyDeployRequest` had `universe_file` and
# `custom_symbols` but no `universe_key`, and `run_multi_strategy_netted_deploy`
# therefore wrote a matrix `summary.json` without one.  A matrix run's directory is
# `<archive>/<date>/multi_netted_<matrix>`, i.e. it has no universe directory of its
# own, so `list_runs` could only label such a round with the literal `multi_matrix`
# -- a key NO selection has.  Measured on the real archive: both matrix rounds
# (`2026-10-13` / `2026-10-15` multi_netted_tri_core_blend) answered
# `list_runs(universe_key="multi_matrix")` and nothing else; asking for the real
# selection (`custom_astock_202609_20`, `china_20_etf`) returned 0.
#
# The fix carries the selection as METADATA: the request model and the UI now send
# `universe_key`/`universe_name`, the service records them verbatim, and `list_runs`
# prefers a recorded key while keeping `multi_matrix` for rounds already archived
# without one.  Universe RESOLUTION is untouched: `_resolve_universe` still takes
# `universe_file` (1) then `custom_symbols` (2), so these fields can never change
# which universe file or symbol set a deploy loads -- test 1 asserts exactly that.
# ---------------------------------------------------------------------------

import execution_service as _execution_service  # noqa: E402  (module seam for monkeypatch)
from models import MultiStrategyDeployRequest  # noqa: E402

_P4_DATE = "2026-10-20"
_P4_MATRIX = "tri_core_blend"
_P4_UNIV_KEY = "custom_astock_202609_20_new"
_P4_UNIV_NAME = "★ astock_202609_20 (Custom)"


def _matrix_dir(storage, date_str, matrix_id=_P4_MATRIX):
    return os.path.join(storage.archive_dir, date_str, f"multi_netted_{matrix_id}")


def _seed_matrix_round(storage, date_str, summary_extra=None, matrix_id=_P4_MATRIX):
    """One matrix round: `<archive>/<date>/multi_netted_<matrix>/summary.json`."""
    d = _matrix_dir(storage, date_str, matrix_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "date": date_str,
                "matrix_id": matrix_id,
                "deploy_status": "SUCCESS",
                **(summary_extra or {}),
            },
            f,
        )
    return d


def test_multi_deploy_summary_records_universe_key(temp_storage, monkeypatch):
    """The matrix deploy WRITES the selection into its summary -- and loads the same universe.

    Drives the real `run_multi_strategy_netted_deploy` (validation, universe resolution,
    run-dir allocation, argv assembly, summary merge) with only the child process faked,
    because the subject is what the service persists, not what the child computes.  This
    is the first test to exercise the matrix deploy path end-to-end: every existing
    matrix test stops at the captured argv or at the model.

    The second half is the safety property: `universe_key` is metadata.  The universe the
    child is told to load must still be the one materialised from `custom_symbols`
    (`resolved_universe.txt`), unchanged by the label.

    (P4b) End-to-end proof of the argv fix as well: the argv handed to the faked child must
    be accepted by the REAL `run_live_multi_strategy.py` parser (a fake child cannot notice
    a flag argparse would reject -- the explicit `parse_args` below is what catches it), the
    round must not be recorded `FAILED` when that child succeeds, and the intraday block must
    say truthfully that the matrix path requested no 5m features.

    re-break: drop `universe_key`/`universe_name` from the matrix summary dict in
    `run_multi_strategy_netted_deploy` -- both summary asserts fail (verified red).
    re-break (P4b, argv): re-add `cmd.append("--enable-intraday-5m")` to the matrix argv --
    the `parse_args` call below raises `SystemExit` naming the flag (verified red).
    re-break (P4b, status): make `deploy_status` unconditionally `"FAILED"` -- the status
    assert fails (verified red).
    re-break (P4b, honesty): set `block["requested"] = True` in `_apply_intraday_observability`
    -- the intraday assert fails on a claim the matrix path cannot honour (verified red).
    """
    calls = []

    def fake_run(cmd, **kwargs):  # the child is the only thing faked
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_execution_service.subprocess, "run", fake_run)

    service = ExecutionService(temp_storage)
    service.run_multi_strategy_netted_deploy(
        matrix_id=_P4_MATRIX,
        strategies=[{"strategy_key": "chan_four_state_shield_blend", "capital_weight": 1.0}],
        custom_symbols=["600519.SH", "000001.SZ"],
        as_of_date=_P4_DATE,
        portfolio_value=100_000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        universe_key=_P4_UNIV_KEY,
        universe_name=_P4_UNIV_NAME,
    )

    run_dir = _matrix_dir(temp_storage, _P4_DATE)
    with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["universe_key"] == _P4_UNIV_KEY
    assert summary["universe_name"] == _P4_UNIV_NAME
    # Metadata only: the child still loads the file materialised from `custom_symbols`.
    assert len(calls) == 1
    univ_arg = calls[0][calls[0].index("--universe-file") + 1]
    assert os.path.normpath(univ_arg) == os.path.normpath(os.path.join(run_dir, "resolved_universe.txt"))
    with open(univ_arg, encoding="utf-8") as f:
        assert "600519.SH" in f.read()

    # (P4b) The argv is a real contract with the child's own argparse: a faked child would
    # happily "succeed" on a flag the real script rejects, so parse it for real here.
    parser = _script_parser("scripts.run_live_multi_strategy")
    parser.parse_args(calls[0][2:])  # raises SystemExit if any flag is unknown
    # ... and a child that exits 0 must not be recorded as a failed round.
    assert summary["deploy_status"] != "FAILED", summary.get("deploy_stderr")
    assert summary["deploy_returncode"] == 0
    # The matrix runner has no 5m capability at all, so the summary must not claim one.
    intraday = summary["observability"]["intraday_5m"]
    assert intraday["requested"] is False
    assert intraday["enabled"] is False
    assert intraday["source"] == "none"


def test_matrix_round_is_listable_under_its_recorded_universe(temp_storage):
    """A matrix round answers the selection it records, not only `multi_matrix`.

    This is the defect's user-visible half: before the field existed, no selection could
    match a matrix round at all (the real archive's two rounds only answer
    `multi_matrix`).  The echoed key must be the recorded one.

    re-break: in `list_runs`, stop preferring `summary_rep.get("universe_key")` and always
    assign `multi_matrix` to a `multi_netted_*` dir -- the listing comes back empty and
    the echoed key is `multi_matrix` (verified red).
    """
    _seed_matrix_round(
        temp_storage,
        "2026-10-13",
        {"universe_key": _P4_UNIV_KEY, "universe_name": _P4_UNIV_NAME},
    )

    runs = temp_storage.list_runs(
        strategy_key=_P4_MATRIX, universe_key=_P4_UNIV_KEY, require_both=True
    )
    assert [r["date"] for r in runs] == ["2026-10-13"]
    assert runs[0]["universe_key"] == _P4_UNIV_KEY
    assert runs[0]["universe_name"] == _P4_UNIV_NAME


def test_matrix_round_without_a_recorded_key_still_lists_as_multi_matrix(temp_storage):
    """Backward compatibility: rounds archived before the field keep working verbatim.

    The two real rounds (`2026-10-13`, `2026-10-15`) have no `universe_key`, and an old
    client sends none, so the literal `multi_matrix` must remain the fallback -- it is how
    those rounds are addressed today.

    re-break: replace the `multi_matrix` fallback with `None`/`""` -- the round is listed
    with an empty universe key and disappears from `multi_matrix` (verified red).
    """
    _seed_matrix_round(temp_storage, "2026-10-15")  # old-style: no universe fields

    runs = temp_storage.list_runs(
        strategy_key=_P4_MATRIX, universe_key="multi_matrix", require_both=True
    )
    assert [r["date"] for r in runs] == ["2026-10-15"]
    assert runs[0]["universe_key"] == "multi_matrix"


def test_multi_deploy_request_accepts_universe_key():
    """The request model carries the fields and stays optional for old clients.

    A payload without them must validate exactly as before (`None`, not `""`), otherwise
    every existing API client and the archived-round path regress together.

    re-break: make `universe_key` required on `MultiStrategyDeployRequest` -- the
    field-less payload raises `ValidationError` (verified red).
    """
    req = MultiStrategyDeployRequest(
        matrix_id=_P4_MATRIX, universe_key=_P4_UNIV_KEY, universe_name=_P4_UNIV_NAME
    )
    assert req.universe_key == _P4_UNIV_KEY
    assert req.universe_name == _P4_UNIV_NAME
    assert req.model_dump()["universe_key"] == _P4_UNIV_KEY

    old_style = MultiStrategyDeployRequest(matrix_id=_P4_MATRIX)
    assert old_style.universe_key is None
    assert old_style.universe_name is None


# ---------------------------------------------------------------------------
# 16. (P4b) argv CONTRACT: every flag the service emits must exist in the script
#
# FORENSIC CONTEXT (measured, not remembered).  `c8559e6` (2026-10-20) added
# `cmd.append("--enable-intraday-5m")` to the MATRIX argv in
# `ExecutionService.run_multi_strategy_netted_deploy` and nothing else; the same commit
# never touched `scripts/run_live_multi_strategy.py`, and no commit on any branch has
# ever defined that flag there (`git log --all -S'enable_intraday_5m' --
# scripts/run_live_multi_strategy.py` -> empty).  The consequence, reproduced live:
# exit code 2 in ~0.83 s, `error: unrecognized arguments: --enable-intraday-5m`,
# `deploy_status: FAILED`, `trades_count: 0` -- the runner died before reading a bar.
# The two matrix rounds in the real archive (`2026-10-13`, `2026-10-15`) are SUCCESS
# WITH 6 and 8 trades precisely because they predate `c8559e6`.
#
# The class of defect is "the service invents a flag the child's argparse does not
# have", and nothing but the child's own parser can answer it.  These two tests
# therefore compare the argv the SERVICE builds against the parser the SCRIPT builds,
# in both directions.
# ---------------------------------------------------------------------------

#: One faked child that records its argv and exits 0 -- the subject is the argv, never
#: the runner's output parsing (that is `test_multi_deploy_summary_records_universe_key`).
def _capture_argv(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_execution_service.subprocess, "run", fake_run)
    return calls


def _script_parser(module_name):
    """The script's REAL `argparse.ArgumentParser`, captured without editing the script.

    Neither runner exposes a `build_parser()`: each constructs its parser inside
    `parse_args()` and returns `parser.parse_args()` from it.  Two ways to get at the
    parser from a test exist -- refactor the entrypoint (a `scripts/*` behaviour-adjacent
    edit, and `--help` would have to stay byte-equivalent) or observe the constructor.
    This observes it: `ArgumentParser.__init__` is spied for exactly one `parse_args()`
    call, and that call still runs to completion, so a module that normalises its
    Namespace afterwards (as `run_live_strategy.parse_args()` does with
    `min_entry_bars` / `min_entry_confirmation`) behaves exactly as in production.
    `sys.argv` is reduced to the program name so the capture depends on nothing but the
    module itself.

    `_actions` is private argparse API, used here deliberately: it is the only way to
    enumerate option strings without parsing a sentinel argv, and a rename would fail the
    test loudly rather than silently pass it.
    """
    import argparse
    import importlib

    module = importlib.import_module(module_name)
    saved_argv = sys.argv
    captured = []
    orig_init = argparse.ArgumentParser.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        captured.append(self)

    argparse.ArgumentParser.__init__ = spy_init
    sys.argv = ["<argv-contract-capture>"]
    try:
        module.parse_args()
    finally:
        argparse.ArgumentParser.__init__ = orig_init
        sys.argv = saved_argv
    assert captured, f"{module_name} constructed no ArgumentParser in parse_args()"
    return captured[-1]


def _accepted_options(parser):
    return {s for action in parser._actions for s in action.option_strings}


def _required_options(parser):
    """Options argparse itself insists on (`required=True`). Everything else is defaulted."""
    return [a.option_strings[0] for a in parser._actions if getattr(a, "required", False)]


def _emitted_options(argv):
    """The option strings in a service-built argv.

    A leading dash is a sufficient discriminator for these two builders: every path they
    pass is absolute, every numeric value is non-negative, and the only values with a dash
    are none.  (If that ever changes, this returns a "flag" the contract test would wrongly
    report -- so the test would fail loudly, never pass silently.)
    """
    return sorted({t for t in argv if t.startswith("-")})


def _assert_argv_contract(parser, argv, script_name):
    """Symmetric difference: nothing invented, nothing required-but-missing."""
    accepted = _accepted_options(parser)
    emitted = _emitted_options(argv)
    unknown = [f for f in emitted if f not in accepted]
    assert not unknown, (
        f"{script_name} would reject {unknown}: the service emits flags its argparse "
        f"does not define. This is the 2026-10-20 `--enable-intraday-5m` defect."
    )
    missing = [f for f in _required_options(parser) if f not in emitted]
    assert not missing, (
        f"{script_name} requires {missing} but the service never passes them."
    )
    # The strongest form: hand the whole argv to the real parser. A faked subprocess
    # cannot notice a flag argparse would reject; this can.
    parser.parse_args(argv[2:])


def test_multi_deploy_argv_is_accepted_by_the_script_parser(temp_storage, monkeypatch):
    """The matrix deploy's argv is a contract with `run_live_multi_strategy.py`.

    Every flag the service emits must be one the script defines; an operator who runs the
    printed command by hand must not meet a different argparse than the service does.

    re-break: re-add `cmd.append("--enable-intraday-5m")` to the matrix argv in
    `run_multi_strategy_netted_deploy` -- this test fails naming `--enable-intraday-5m`
    (verified red), while the single-path test below stays green: they are different
    scripts, which is exactly why both exist.
    """
    calls = _capture_argv(monkeypatch)
    service = ExecutionService(temp_storage)
    service.run_multi_strategy_netted_deploy(
        matrix_id=_P4_MATRIX,
        strategies=[{"strategy_key": "chan_four_state_shield_blend", "capital_weight": 1.0}],
        custom_symbols=["600519.SH", "000001.SZ"],
        as_of_date=_P4_DATE,
        portfolio_value=100_000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
    )
    assert len(calls) == 1
    _assert_argv_contract(
        _script_parser("scripts.run_live_multi_strategy"), calls[0], "run_live_multi_strategy.py"
    )


def test_single_deploy_argv_is_accepted_by_the_script_parser(temp_storage, monkeypatch):
    """REPORTING ONLY: the single-strategy argv is currently clean -- keep it visible.

    The symmetric difference for `run_live_deploy` -> `run_live_strategy.py` is empty today
    (that script really does define `--enable-intraday-5m`, `--guard-latency-bars`, ...),
    so this is a plain green assertion, not an `xfail`.  If the single path ever grows the
    same defect, this test names the flag -- and fixing order generation is a separate
    decision, deliberately not taken here.

    re-break: append a flag the script does not define (e.g. `cmd.append("--not-a-flag")`)
    to the Stage-2 argv -- this test fails naming it (verified red).
    """
    calls = _capture_argv(monkeypatch)
    strat_file = os.path.join(temp_storage.data_dir, "p4b_single_contract_strat.json")
    os.makedirs(temp_storage.data_dir, exist_ok=True)
    temp_storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    service = ExecutionService(temp_storage)
    service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        portfolio_value=100_000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        nav_override=True,
    )
    assert len(calls) == 1
    _assert_argv_contract(
        _script_parser("scripts.run_live_strategy"), calls[0], "run_live_strategy.py"
    )
