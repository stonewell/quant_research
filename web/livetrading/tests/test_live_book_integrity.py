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
"""

import json
import os
import sys

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from storage import (  # noqa: E402  (bare import: same module identity as server.py)
    StorageManager,
    has_stage2_artifact,
    instance_subdirs,
    prefer_instance_state_enabled,
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
