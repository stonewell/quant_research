"""Offline regression suite for the 2026-11-16 ticket audit fields.

THE INCIDENT UNDER TEST. The 2026-11-16 live run printed a 110-column
`RISK ALERT / VOL OVERLAY` banner -- "上限生效 模型权益 75.02% -> 39.57%" -- and then
issued NET BUYING of 14,193 RMB, taking the equity book from 25.1% to 32.3%. Both
statements are true (the ceiling binds the TARGET vector, while the live book sat
BELOW that ceiling, so closing the gap had to be done with buys), but the ticket said
nothing about the direction, and "cap binding" reads to an operator as "the book was
de-risked today". Four AUDIT-ONLY additions close that reporting hole:

1. ticket-level `equity_direction` -- `equity_before` / `equity_after` / `delta_pp` /
   `net_trade_value_rmb` / `direction` (ADD/REDUCE/FLAT at +/-0.5pp) / `vol_cap_binding` /
   `interpretation_note` (fires ONLY on vol-cap-binding + ADD);
2. `detail_reason["sector_throttle"]["throttled"]` -- was the hardcoded literal `False`;
   now a truthful inference plus an always-present `throttle_state`
   (`binding` / `not_binding` / `cannot_prove`);
3. `detail_reason["is_partial_trim"]` -- `action == "SELL" and target_weight > 0`, additive
   beside `is_liquidation` (whose `action == "SELL"` semantics are untouched);
4. per-row `lot_rounding.{delivered_shares, raw_shares, drag_rmb}` plus the ticket-level
   `lot_rounding_drag` block.

Two further 2026-11-16 additions are pinned here too:

5. `detail_reason["sector_throttle"]["sector_source"]` -- `"map"` / `"code_prefix_fallback"` /
   `"unknown"`: makes the silent `_get_symbol_sector` code-prefix fallback self-declaring
   (600900.SH / 000999.SZ are missing from `CHINA_EQUITY_SECTORS`, so the sector cap is inert
   for them), plus the ONE aggregate `[WARN/SECTOR MAP]` line that names those symbols and is
   silent otherwise. The mapping itself is NOT changed here.
6. the vol-ceiling caption's `…见票面 EQUITY DIRECTION。` pointer and the ticket's literal
   `EQUITY DIRECTION` tag line up: the literal is present in every direction-line variant, the
   caption is printed only for `stage1` + binding, and the direction block/print/attach are
   structurally unconditional in the same `main()` with no `sys.exit`/early `return` between
   them.

What is pinned here, in order:

* the ADD / REDUCE / FLAT thresholds behave strictly on both sides of +/-0.5pp, and the
  cash-proxy row is EXCLUDED from every sum (its `trade_value_rmb` mirrors the equity legs);
* the `interpretation_note` fires ONLY for (vol cap binding AND ADD), interpolates its
  numbers instead of hardcoding the 11-16 example, and the printed line carries the literal
  `EQUITY DIRECTION` tag the vol-ceiling caption cross-references;
* the throttle truth table, including `cannot_prove` for unusable inputs and
  `not_binding` for an incumbent in an already-full sector (the throttle only gates
  ADMISSION, and the real rule lives in `blend_base.py`, not in the live runner);
* `is_partial_trim` for a full exit vs. a trim, and that it does not disturb
  `is_liquidation`'s meaning;
* the lot-drag SIGN convention: a buy under-fill and a sell over-fill are BOTH negative
  (less equity deployed than the raw target asked for), a full liquidation is exactly 0,
  and the ticket level splits buys from sells and expresses the total as % NAV;
* a full 11-16-shaped regression: 25.1% -> 32.3% (+7.2pp, net buy 14,193), buy drag
  ~-3,383 against sell drag ~-1,913, total ~-5,281 RMB at ~-2.68% NAV.

100% offline: hand-written synthetic rows only. No network, no real market data, no file
writes, and no order/share/price field is produced or mutated by these helpers (they are
pure annotations).
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from scripts.run_live_strategy import (
    CHINA_EQUITY_SECTORS,
    EQUITY_DIRECTION_FLAT_BAND_PP,
    SECTOR_SOURCE_CODE_PREFIX_FALLBACK,
    SECTOR_SOURCE_MAP,
    SECTOR_SOURCE_UNKNOWN,
    VOL_CEILING_SCOPE_PORTFOLIO,
    VOL_CEILING_SCOPE_STAGE1,
    annotate_lot_rounding_drag,
    classify_sector_throttle,
    compute_equity_direction,
    format_equity_direction_line,
    format_sector_map_fallback_warning,
    format_vol_scope_alert_line,
    is_partial_trim,
    lot_rounding_drag_for_row,
    resolve_sector_source,
    signed_delivered_shares,
)

_CASH = "511880.SH"


# ---------------------------------------------------------------------------
# Synthetic row builders (no market data, no I/O)
# ---------------------------------------------------------------------------


def _row(
    symbol,
    *,
    action="BUY",
    price=10.0,
    current_weight=0.0,
    target_weight=0.0,
    target_weight_effective=None,
    trade_value_rmb=0.0,
    shares=0,
    raw_shares=None,
    with_lot_block=True,
):
    """One ticket-shaped row. `raw_shares` -> `detail_reason.lot_rounding`."""
    row = {
        "symbol": symbol,
        "action": action,
        "price": price,
        "current_weight": current_weight,
        "target_weight": target_weight,
        "target_weight_effective": target_weight_effective,
        "delta_shares": shares if action == "BUY" else -shares,
        "shares": abs(shares),
        "trade_value_rmb": trade_value_rmb,
        "detail_reason": {},
    }
    if with_lot_block:
        row["detail_reason"]["lot_rounding"] = {
            "target_shares_raw": float(raw_shares if raw_shares is not None else shares),
            "shares": abs(shares),
            "lot_size": 100,
        }
    return row


def _cash_row(current_weight=0.749, target_weight_effective=0.677, trade_value_rmb=-14193.0):
    """The cash-proxy leg: its `trade_value_rmb` MIRRORS the equity legs (sign flipped)."""
    return {
        "symbol": _CASH,
        "action": "CASH",
        "price": 1.0,
        "current_weight": current_weight,
        "target_weight": target_weight_effective,
        "target_weight_effective": target_weight_effective,
        "shares": 0,
        "delta_shares": 0,
        "trade_value_rmb": trade_value_rmb,
        "detail_reason": {
            "lot_rounding": {"target_shares_raw": 0.0, "shares": 0, "lot_size": 100}
        },
    }


def _nov16_rows(nav=197_052.0):
    """The 2026-11-16 book shape: 25.1% held -> 32.3% delivered, net +14,193 RMB.

    Two equity legs plus the cash mirror. Chosen so `Σ current_weight = 0.251` and
    `Σ target_weight_effective = 0.323` exactly, which is the archived before/after pair.
    """
    return [
        _row(
            "600660.SH",
            action="BUY",
            price=10.0,
            current_weight=0.200,
            target_weight=0.250,
            target_weight_effective=0.250,
            trade_value_rmb=9852.6,
            shares=662,
            raw_shares=1000.3,  # buy under-fill: (662 - 1000.3) * 10 = -3,383
        ),
        _row(
            "601919.SH",
            action="SELL",
            price=20.0,
            current_weight=0.051,
            target_weight=0.073,
            target_weight_effective=0.073,
            trade_value_rmb=4340.4,
            shares=1100,
            raw_shares=-1004.35,  # sell over-fill: (-1100 + 1004.35) * 20 = -1,913
        ),
        _cash_row(),
    ], nav


_VOL_OVERLAY_BINDING = {
    "applied": True,
    "binding": True,
    "model_equity_sum": 0.7502,
    "capped_equity_sum": 0.396,
    "vol_scaling_factor": 0.5278,
    "realized_vol_21d": 0.2431,
}


# ---------------------------------------------------------------------------
# 1. equity_direction
# ---------------------------------------------------------------------------


def test_equity_direction_nov16_shape():
    rows, _nav = _nov16_rows()
    block = compute_equity_direction(
        rows, cash_proxy=_CASH, vol_overlay=_VOL_OVERLAY_BINDING
    )
    assert block["equity_before"] == pytest.approx(0.251, abs=1e-9)
    assert block["equity_after"] == pytest.approx(0.323, abs=1e-9)
    assert block["delta_pp"] == pytest.approx(7.2, abs=1e-9)
    # The cash mirror is excluded, so the net is the EQUITY legs' +14,193, not 0.
    assert block["net_trade_value_rmb"] == pytest.approx(14193.0, abs=1e-6)
    assert block["direction"] == "ADD"
    assert block["vol_cap_binding"] is True


def test_equity_direction_excludes_cash_proxy():
    """Including the cash mirror would always net the book to zero."""
    rows, _nav = _nov16_rows()
    with_cash = compute_equity_direction(rows, cash_proxy=_CASH)
    assert with_cash["net_trade_value_rmb"] == pytest.approx(14193.0)
    # And without the cash proxy the weights would double-count the residual.
    assert with_cash["equity_after"] == pytest.approx(0.323)

    no_cash = compute_equity_direction(rows, cash_proxy=None)
    assert no_cash["equity_after"] == pytest.approx(0.323 + 0.677)
    assert no_cash["net_trade_value_rmb"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "before,after,expected",
    [
        (0.20, 0.206, "ADD"),      # +0.6pp -> ADD
        (0.20, 0.204, "FLAT"),     # +0.4pp -> inside the band
        (0.20, 0.205, "FLAT"),     # exactly +0.5pp -> FLAT (strict `>`)
        (0.20, 0.196, "FLAT"),     # -0.4pp -> inside the band
        (0.20, 0.195, "FLAT"),     # exactly -0.5pp -> FLAT (strict `<`)
        (0.20, 0.194, "REDUCE"),   # -0.6pp -> REDUCE
        (0.20, 0.193, "REDUCE"),   # -0.7pp -> REDUCE
        (0.30, 0.30, "FLAT"),      # no change
    ],
)
def test_equity_direction_thresholds(before, after, expected):
    rows = [
        _row(
            "600000.SH",
            current_weight=before,
            target_weight=after,
            target_weight_effective=after,
        )
    ]
    block = compute_equity_direction(rows, cash_proxy=_CASH)
    assert block["direction"] == expected


def test_equity_direction_thresholds_note_the_4bp_case_is_flat():
    """Guard against a floating-point surprise at the documented boundary."""
    rows = [
        _row(
            "600000.SH",
            current_weight=0.20,
            target_weight=0.204,
            target_weight_effective=0.204,
        )
    ]
    block = compute_equity_direction(rows, cash_proxy=_CASH)
    assert block["delta_pp"] == pytest.approx(0.4, abs=1e-6)
    assert block["direction"] == "FLAT"
    assert block["interpretation_note"] is None


def test_equity_direction_flat_band_constant_is_half_a_point():
    assert EQUITY_DIRECTION_FLAT_BAND_PP == 0.5


def test_equity_direction_reduce_does_not_fire_the_note():
    rows = [
        _row(
            "600000.SH",
            action="SELL",
            current_weight=0.32,
            target_weight=0.20,
            target_weight_effective=0.20,
            trade_value_rmb=-23000.0,
            shares=1000,
            raw_shares=-1000.0,
        )
    ]
    block = compute_equity_direction(
        rows, cash_proxy=_CASH, vol_overlay=_VOL_OVERLAY_BINDING
    )
    assert block["direction"] == "REDUCE"
    assert block["delta_pp"] == pytest.approx(-12.0)
    # The cap may bind while the book is being reduced -- that is NOT the confusing shape,
    # so the warning must stay silent.
    assert block["interpretation_note"] is None


def test_equity_direction_add_without_binding_cap_does_not_fire_the_note():
    rows, _nav = _nov16_rows()
    overlay = dict(_VOL_OVERLAY_BINDING, binding=False)
    block = compute_equity_direction(rows, cash_proxy=_CASH, vol_overlay=overlay)
    assert block["direction"] == "ADD"
    assert block["vol_cap_binding"] is False
    assert block["interpretation_note"] is None


def test_equity_direction_without_overlay_reports_none_not_false():
    """"never evaluated" is not "evaluated and did not bind"."""
    rows, _nav = _nov16_rows()
    block = compute_equity_direction(rows, cash_proxy=_CASH, vol_overlay=None)
    assert block["vol_cap_binding"] is None
    assert block["interpretation_note"] is None


def test_equity_direction_note_fires_with_interpolated_numbers():
    rows, _nav = _nov16_rows()
    block = compute_equity_direction(
        rows, cash_proxy=_CASH, vol_overlay=_VOL_OVERLAY_BINDING
    )
    note = block["interpretation_note"]
    assert note is not None
    # The numbers come from the data, not from the 11-16 example.
    assert "39.6%" in note  # capped target vector
    assert "25.1%" in note  # live book BEFORE today's orders
    assert "14,193" in note  # net buy
    assert "+7.2pp" in note
    # The point of the sentence: binding + adding is not a de-risk.
    assert "上限生效" in note and "今日加仓" in note
    assert "切勿读成今日在降风险" in note


def test_equity_direction_note_recomputes_on_a_different_day():
    """A cap of 50.0% over a 10.0% book must NOT echo the 11-16 percentages."""
    rows = [
        _row(
            "600000.SH",
            current_weight=0.10,
            target_weight=0.18,
            target_weight_effective=0.18,
            trade_value_rmb=15764.0,
        )
    ]
    overlay = {"binding": True, "capped_equity_sum": 0.50}
    block = compute_equity_direction(rows, cash_proxy=_CASH, vol_overlay=overlay)
    note = block["interpretation_note"]
    assert note is not None
    assert "50.0%" in note
    assert "10.0%" in note
    assert "39.6%" not in note
    assert "25.1%" not in note


def test_equity_direction_note_survives_a_missing_capped_sum():
    rows = [
        _row(
            "600000.SH",
            current_weight=0.10,
            target_weight=0.18,
            target_weight_effective=0.18,
        )
    ]
    block = compute_equity_direction(
        rows, cash_proxy=_CASH, vol_overlay={"binding": True}
    )
    assert block["interpretation_note"] is not None
    assert "上限值未记录" in block["interpretation_note"]


def test_equity_direction_falls_back_to_target_weight_when_effective_absent():
    """Rows seen before `reconcile_delivered_weight_budget` still report a direction."""
    rows = [
        {
            "symbol": "600000.SH",
            "action": "BUY",
            "current_weight": 0.10,
            "target_weight": 0.20,
            "target_weight_effective": None,
            "trade_value_rmb": 0.0,
        }
    ]
    block = compute_equity_direction(rows, cash_proxy=_CASH)
    assert block["equity_after"] == pytest.approx(0.20)
    assert block["delta_pp"] == pytest.approx(10.0)
    assert block["direction"] == "ADD"


def test_equity_direction_line_carries_the_grep_tag():
    rows, _nav = _nov16_rows()
    block = compute_equity_direction(
        rows, cash_proxy=_CASH, vol_overlay=_VOL_OVERLAY_BINDING
    )
    line = format_equity_direction_line(block)
    # The vol-ceiling caption tells the operator to read the ticket's EQUITY DIRECTION.
    assert "EQUITY DIRECTION" in line
    assert "权益方向" in line
    assert "加仓" in line
    assert "+7.2pp" in line
    assert "14,193" in line
    assert "25.1% -> 后 32.3%" in line


@pytest.mark.parametrize(
    "direction,label",
    [("ADD", "加仓"), ("REDUCE", "减仓"), ("FLAT", "持平")],
)
def test_equity_direction_line_labels(direction, label):
    line = format_equity_direction_line(
        {
            "direction": direction,
            "delta_pp": 1.0,
            "net_trade_value_rmb": 100.0,
            "equity_before": 0.2,
            "equity_after": 0.21,
        }
    )
    assert label in line
    assert "EQUITY DIRECTION" in line


def test_equity_direction_is_json_native():
    rows, _nav = _nov16_rows()
    block = compute_equity_direction(
        rows, cash_proxy=_CASH, vol_overlay=_VOL_OVERLAY_BINDING
    )
    # The ticket writer uses `json.dump(..., ensure_ascii=False)` with no `default=str`.
    import json

    json.dumps(block, ensure_ascii=False)
    assert isinstance(block["direction"], str)
    assert isinstance(block["delta_pp"], float)


# ---------------------------------------------------------------------------
# 2. sector_throttle truth table
# ---------------------------------------------------------------------------


def test_throttle_todays_inputs_are_not_binding():
    """2026-11-16 had 1 holding per sector against a cap of 3."""
    block = classify_sector_throttle(
        sector="煤炭",
        current_sector_holdings=1,
        max_allowed=3,
        already_held=False,
    )
    assert block["throttled"] is False
    assert block["throttle_state"] == "not_binding"
    assert "throttle_note" in block
    assert "throttled_reason" not in block
    # The note must name the layer that actually enforces the rule.
    assert "blend_base.py" in block["throttle_note"]


@pytest.mark.parametrize(
    "holdings,cap,already_held,throttled,state",
    [
        (1, 3, False, False, "not_binding"),   # free slots
        (2, 3, False, False, "not_binding"),   # one slot left
        (3, 3, False, True, "binding"),        # new entry into a full sector
        (4, 3, False, True, "binding"),        # already over the cap
        (3, 3, True, False, "not_binding"),    # incumbent: throttle gates admission only
        (1, 3, True, False, "not_binding"),    # incumbent in a sector with free slots
        (0, 1, False, False, "not_binding"),   # empty sector, cap 1
        (1, 1, False, True, "binding"),        # cap 1 already consumed
        (0, 0, False, False, "not_binding"),   # throttle disabled
        (5, 0, False, False, "not_binding"),   # disabled wins over the count
        (None, 3, False, False, "cannot_prove"),
        (3, None, False, False, "cannot_prove"),
        (None, None, False, False, "cannot_prove"),
        ("bad", 3, False, False, "cannot_prove"),
        (3, "bad", False, False, "cannot_prove"),
    ],
)
def test_throttle_truth_table(holdings, cap, already_held, throttled, state):
    block = classify_sector_throttle(
        sector="煤炭",
        current_sector_holdings=holdings,
        max_allowed=cap,
        already_held=already_held,
    )
    assert block["throttled"] is throttled
    assert block["throttle_state"] == state
    if throttled:
        assert block["throttled_reason"]
        assert "throttle_note" not in block
    else:
        assert block["throttle_note"]
        assert "throttled_reason" not in block


def test_throttle_binding_reason_points_at_the_weight_layer():
    block = classify_sector_throttle(
        sector="煤炭", current_sector_holdings=3, max_allowed=3, already_held=False
    )
    assert block["throttled"] is True
    assert "blend_base.py" in block["throttled_reason"]
    assert "不改任何权重" in block["throttled_reason"]


def test_throttle_block_keeps_the_existing_keys():
    """`common/tests/test_strategy_versioning.py` pins these four keys."""
    block = classify_sector_throttle(
        sector="银行", current_sector_holdings=1, max_allowed=3, already_held=True
    )
    for key in ("sector", "max_allowed", "current_sector_holdings", "throttled"):
        assert key in block
    assert block["sector"] == "银行"
    assert block["max_allowed"] == 3


def test_throttle_never_invents_true_on_missing_inputs():
    block = classify_sector_throttle(
        sector=None, current_sector_holdings=None, max_allowed=None, already_held=False
    )
    assert block["throttled"] is False
    assert block["throttle_state"] == "cannot_prove"


def test_throttle_is_json_native():
    import json

    for args in (
        dict(sector="煤炭", current_sector_holdings=3, max_allowed=3, already_held=False),
        dict(sector="煤炭", current_sector_holdings=1, max_allowed=3, already_held=True),
        dict(sector=None, current_sector_holdings=None, max_allowed=None, already_held=False),
    ):
        json.dumps(classify_sector_throttle(**args), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 2b. sector_source -- the map-vs-code-prefix provenance label
# ---------------------------------------------------------------------------
#
# `blend_base._get_symbol_sector` falls back to a CODE-PREFIX bucket (`sec_60`, `sec_00`)
# for any name missing from `CHINA_EQUITY_SECTORS`, which makes `max_assets_per_sector`
# throttling inert for that name (600900.SH was bought on 2026-11-16 that way). The mapping
# itself is NOT changed here (that is a `BASE_VERSION` bump and a `prior_state` cold start);
# the block merely DECLARES which branch produced `sector`, and the ticket prints ONE
# aggregate WARN line naming the affected symbols.


def test_sector_source_truth_table():
    assert resolve_sector_source("601225.SH") == SECTOR_SOURCE_MAP
    assert resolve_sector_source("601225.SH") == "map"
    # The two 2026-11-16 names missing from the table: utilities and pharma respectively,
    # both landing in a meaningless code-prefix bucket today.
    assert resolve_sector_source("600900.SH") == SECTOR_SOURCE_CODE_PREFIX_FALLBACK
    assert resolve_sector_source("000999.SZ") == SECTOR_SOURCE_CODE_PREFIX_FALLBACK
    assert resolve_sector_source("600900.SH") == "code_prefix_fallback"
    # Non-string / blank symbols never fabricate a fallback claim.
    assert resolve_sector_source(None) == SECTOR_SOURCE_UNKNOWN
    assert resolve_sector_source("") == SECTOR_SOURCE_UNKNOWN


def test_sector_source_labels_come_from_the_real_map():
    """The label is only honest if the table is the one `_get_symbol_sector` itself uses."""
    for symbol in CHINA_EQUITY_SECTORS:
        assert resolve_sector_source(symbol) == SECTOR_SOURCE_MAP
    for symbol in ("600900.SH", "000999.SZ"):
        assert symbol not in CHINA_EQUITY_SECTORS
        assert resolve_sector_source(symbol) == SECTOR_SOURCE_CODE_PREFIX_FALLBACK


def test_sector_source_is_unknown_when_the_map_is_missing():
    """A defensive import fallback must degrade the LABEL, never raise."""
    assert resolve_sector_source("601225.SH", sector_map=None) == SECTOR_SOURCE_UNKNOWN
    assert resolve_sector_source("600900.SH", sector_map=None) == SECTOR_SOURCE_UNKNOWN
    assert resolve_sector_source("600900.SH", sector_map=object()) == SECTOR_SOURCE_UNKNOWN
    # An empty mapping is a usable mapping: everything falls back.
    assert (
        resolve_sector_source("601225.SH", sector_map={})
        == SECTOR_SOURCE_CODE_PREFIX_FALLBACK
    )


def test_sector_source_survives_a_missing_module_mapping(monkeypatch):
    """The module-level `try/except ImportError` sets `CHINA_EQUITY_SECTORS = None`."""
    import scripts.run_live_strategy as mod

    monkeypatch.setattr(mod, "CHINA_EQUITY_SECTORS", None)
    assert mod.resolve_sector_source("601225.SH") == SECTOR_SOURCE_UNKNOWN
    block = mod.classify_sector_throttle(
        sector="sec_60",
        current_sector_holdings=1,
        max_allowed=3,
        already_held=False,
        symbol="600900.SH",
    )
    assert block["sector_source"] == SECTOR_SOURCE_UNKNOWN
    # And the aggregate warning stays silent: "unknown" is not a PROVEN fallback.
    assert mod.format_sector_map_fallback_warning(
        [{"symbol": "600900.SH", "detail_reason": {"sector_throttle": block}}]
    ) is None


def test_throttle_block_carries_sector_source_without_moving_other_keys():
    mapped = classify_sector_throttle(
        sector="coal",
        current_sector_holdings=3,
        max_allowed=3,
        already_held=False,
        symbol="601225.SH",
    )
    fallback = classify_sector_throttle(
        sector="sec_60",
        current_sector_holdings=3,
        max_allowed=3,
        already_held=False,
        symbol="600900.SH",
    )
    assert mapped["sector_source"] == SECTOR_SOURCE_MAP
    assert fallback["sector_source"] == SECTOR_SOURCE_CODE_PREFIX_FALLBACK
    # Provenance is an ADDITION: `sector` is the caller's own value (unchanged), and every
    # other pre-existing key/value is identical between the two calls.
    assert mapped["sector"] == "coal" and fallback["sector"] == "sec_60"
    for key in ("max_allowed", "current_sector_holdings", "throttled",
                "throttle_state", "throttled_reason"):
        assert mapped[key] == fallback[key]
    assert fallback["throttled"] is True


def test_sector_source_is_present_on_every_return_path():
    """Including `cannot_prove`, the disabled cap and the incumbent branches."""
    for args in (
        dict(sector="sec_60", current_sector_holdings=None, max_allowed=None, already_held=False),
        dict(sector="sec_60", current_sector_holdings=1, max_allowed=0, already_held=False),
        dict(sector="sec_60", current_sector_holdings=1, max_allowed=3, already_held=True),
        dict(sector="sec_60", current_sector_holdings=3, max_allowed=3, already_held=False),
        dict(sector="sec_60", current_sector_holdings=1, max_allowed=3, already_held=False),
    ):
        block = classify_sector_throttle(symbol="600900.SH", **args)
        assert block["sector_source"] == SECTOR_SOURCE_CODE_PREFIX_FALLBACK
        assert block["sector"] == "sec_60"


def _throttle_row(symbol, sector_source, **overrides):
    row = _row(symbol)
    row["detail_reason"]["sector_throttle"] = {
        "sector": "sec_60",
        "max_allowed": 3,
        "current_sector_holdings": 1,
        "throttled": False,
        "throttle_state": "not_binding",
        "sector_source": sector_source,
    }
    row["detail_reason"]["sector_throttle"].update(overrides)
    return row


def test_fallback_warning_names_every_fallback_row():
    line = format_sector_map_fallback_warning(
        [
            _throttle_row("600900.SH", SECTOR_SOURCE_CODE_PREFIX_FALLBACK),
            _throttle_row("000999.SZ", SECTOR_SOURCE_CODE_PREFIX_FALLBACK),
            _throttle_row("601225.SH", SECTOR_SOURCE_MAP),
        ]
    )
    assert line is not None
    assert "600900.SH" in line and "000999.SZ" in line
    assert "601225.SH" not in line
    # ONE line, stating that throttling is inert for these names.
    assert "\n" not in line
    assert "空转" in line
    assert SECTOR_SOURCE_CODE_PREFIX_FALLBACK in line


def test_fallback_warning_is_silent_when_every_row_is_mapped():
    assert format_sector_map_fallback_warning(
        [
            _throttle_row("601225.SH", SECTOR_SOURCE_MAP),
            _throttle_row("600660.SH", SECTOR_SOURCE_MAP),
        ]
    ) is None
    assert format_sector_map_fallback_warning([]) is None
    assert format_sector_map_fallback_warning(None) is None


def test_fallback_warning_ignores_rows_without_the_block():
    """No `detail_reason`, no `sector_throttle`, garbage values: never raise, never warn."""
    assert format_sector_map_fallback_warning(
        [
            {"symbol": "600900.SH"},
            {"symbol": "000999.SZ", "detail_reason": None},
            {"symbol": "600584.SH", "detail_reason": {"sector_throttle": "bad"}},
            "not-a-row",
        ]
    ) is None


def test_fallback_warning_deduplicates_and_survives_unknown():
    line = format_sector_map_fallback_warning(
        [
            _throttle_row("600900.SH", SECTOR_SOURCE_CODE_PREFIX_FALLBACK),
            _throttle_row("600900.SH", SECTOR_SOURCE_CODE_PREFIX_FALLBACK),
            _throttle_row("000999.SZ", SECTOR_SOURCE_UNKNOWN),
        ]
    )
    assert line is not None
    assert line.count("600900.SH") == 1
    assert "000999.SZ" not in line


def test_fallback_warning_does_not_mutate_rows_or_order_fields():
    import json

    rows = [
        _throttle_row("600900.SH", SECTOR_SOURCE_CODE_PREFIX_FALLBACK),
        _throttle_row("601225.SH", SECTOR_SOURCE_MAP),
    ]
    before = [json.loads(json.dumps(r)) for r in rows]
    format_sector_map_fallback_warning(rows)
    assert rows == before


# ---------------------------------------------------------------------------
# 3. is_partial_trim
# ---------------------------------------------------------------------------


def test_partial_trim_marks_the_nov16_trims():
    # 600660.SH -2.3pp and 601919.SH -2.2pp keep the names.
    assert is_partial_trim("SELL", 0.115) is True
    assert is_partial_trim("SELL", 0.073) is True


def test_partial_trim_excludes_the_full_exit():
    # 600362.SH's target was 0 -> a full liquidation, not a trim.
    assert is_partial_trim("SELL", 0.0) is False
    assert is_partial_trim("SELL", None) is False
    assert is_partial_trim("SELL", float("nan")) is False


@pytest.mark.parametrize("action", ["BUY", "HOLD", "HOLD_FILTERED", "CANCEL_PENDING", "CASH", "", None])
def test_partial_trim_requires_a_sell(action):
    assert is_partial_trim(action, 0.1) is False


def test_partial_trim_is_case_and_whitespace_tolerant():
    assert is_partial_trim(" sell ", 0.05) is True


def test_partial_trim_does_not_touch_is_liquidation():
    """`is_liquidation` stays `action == "SELL"` -- the two keys answer different things."""
    from common.position_lifecycle import is_liquidation_event
    from scripts.run_live_strategy import BlockingReason

    # A SELL that keeps 7.3% is still, by the frozen contract, a `SELL` event.
    assert bool(is_liquidation_event("SELL", BlockingReason.NONE)) is True
    assert is_partial_trim("SELL", 0.073) is True
    # A 0.0 target with no trade is NOT a partial trim, and its liquidation verdict is
    # decided by `action`, never by `target_weight`.
    assert is_partial_trim("HOLD", 0.0) is False


# ---------------------------------------------------------------------------
# 4. lot-rounding drag
# ---------------------------------------------------------------------------


def test_buy_under_fill_is_negative():
    """Asked 1,000.3 shares, delivered 662 whole-lot shares -> less equity deployed."""
    row = _row("600660.SH", action="BUY", price=10.0, shares=662, raw_shares=1000.3)
    drag = lot_rounding_drag_for_row(row)
    assert drag["delivered_shares"] == 662
    assert drag["raw_shares"] == pytest.approx(1000.3)
    assert drag["drag_rmb"] == pytest.approx(-3383.0, abs=1e-6)


def test_sell_over_fill_is_negative_too():
    """Asked to sell 1,004.35 shares, the `ceil` lot delivered 1,100 -> same sign."""
    row = _row("601919.SH", action="SELL", price=20.0, shares=1100, raw_shares=-1004.35)
    drag = lot_rounding_drag_for_row(row)
    assert drag["delivered_shares"] == -1100
    assert drag["raw_shares"] == pytest.approx(-1004.35)
    assert drag["drag_rmb"] == pytest.approx(-1913.0, abs=1e-6)


def test_buy_over_fill_is_positive():
    row = _row("600000.SH", action="BUY", price=10.0, shares=200, raw_shares=150.0)
    drag = lot_rounding_drag_for_row(row)
    assert drag["drag_rmb"] == pytest.approx(500.0)


def test_exact_fill_has_no_drag():
    row = _row("600000.SH", action="BUY", price=10.0, shares=100, raw_shares=100.0)
    assert lot_rounding_drag_for_row(row)["drag_rmb"] == pytest.approx(0.0)


def test_full_liquidation_has_no_drag():
    """`raw_shares == -current_shares` on a liquidation, so the gap is exactly zero."""
    row = _row("600362.SH", action="SELL", price=30.0, shares=500, raw_shares=-500.0)
    drag = lot_rounding_drag_for_row(row)
    assert drag["drag_rmb"] == pytest.approx(0.0)
    assert drag["delivered_shares"] == -500


def test_signed_delivered_shares():
    assert signed_delivered_shares({"action": "BUY", "shares": 100}) == 100
    assert signed_delivered_shares({"action": "SELL", "shares": 100}) == -100
    assert signed_delivered_shares({"action": "HOLD", "shares": 0}) == 0
    assert signed_delivered_shares({"action": "CASH", "shares": 5}) == 0
    assert signed_delivered_shares({"action": "BUY", "shares": None}) == 0


def test_row_without_lot_block_returns_none():
    row = _row("600000.SH", with_lot_block=False)
    assert lot_rounding_drag_for_row(row) is None


def test_annotate_writes_per_row_fields_additively():
    rows, _nav = _nov16_rows()
    before = [dict(r) for r in rows]
    annotate_lot_rounding_drag(rows, cash_proxy=_CASH, portfolio_value=_nav)

    block = rows[0]["detail_reason"]["lot_rounding"]
    assert block["delivered_shares"] == 662
    assert block["raw_shares"] == pytest.approx(1000.3)
    assert block["drag_rmb"] == pytest.approx(-3383.0, abs=1e-6)
    # The pre-existing keys are untouched.
    assert block["lot_size"] == 100
    assert block["shares"] == 662

    # No order field moved.
    for original, annotated in zip(before, rows):
        for key in ("action", "price", "current_weight", "target_weight", "shares",
                    "delta_shares", "trade_value_rmb", "target_weight_effective"):
            assert annotated[key] == original[key]


def test_annotate_ticket_totals_nov16():
    rows, nav = _nov16_rows()
    block = annotate_lot_rounding_drag(rows, cash_proxy=_CASH, portfolio_value=nav)
    assert block["buy_drag_rmb"] == pytest.approx(-3383.0, abs=1e-6)
    assert block["sell_drag_rmb"] == pytest.approx(-1913.0, abs=1e-6)
    # The reference archive rounds these to "about -5,281"; the legs sum exactly.
    assert block["drag_rmb"] == pytest.approx(-5296.0, abs=1e-6)
    assert block["drag_rmb"] == pytest.approx(
        block["buy_drag_rmb"] + block["sell_drag_rmb"], abs=1e-9
    )
    assert block["drag_rmb"] == pytest.approx(-5281.0, abs=20.0)
    assert block["drag_pct_nav"] == pytest.approx(-2.68, abs=0.02)
    assert block["rows_measured"] == 2  # the cash proxy is excluded
    assert "note" in block


def test_annotate_drag_pct_nav_uses_nav():
    rows = [_row("600000.SH", action="BUY", price=10.0, shares=100, raw_shares=200.0)]
    block = annotate_lot_rounding_drag(rows, cash_proxy=_CASH, portfolio_value=100_000.0)
    assert block["drag_rmb"] == pytest.approx(-1000.0)
    assert block["drag_pct_nav"] == pytest.approx(-1.0)


def test_annotate_without_nav_reports_none_pct():
    rows = [_row("600000.SH", action="BUY", price=10.0, shares=100, raw_shares=200.0)]
    block = annotate_lot_rounding_drag(rows, cash_proxy=_CASH, portfolio_value=None)
    assert block["drag_pct_nav"] is None
    assert block["drag_rmb"] == pytest.approx(-1000.0)


def test_annotate_with_no_drag_is_quiet():
    rows = [_row("600000.SH", action="BUY", price=10.0, shares=100, raw_shares=100.0)]
    block = annotate_lot_rounding_drag(rows, cash_proxy=_CASH, portfolio_value=100_000.0)
    assert block["drag_rmb"] == 0.0
    assert block["drag_pct_nav"] == 0.0
    assert "note" not in block


def test_annotate_handles_empty_input():
    assert annotate_lot_rounding_drag([], cash_proxy=_CASH, portfolio_value=1.0) == {
        "drag_rmb": 0.0,
        "buy_drag_rmb": 0.0,
        "sell_drag_rmb": 0.0,
        "drag_pct_nav": 0.0,
        "rows_measured": 0,
    }


def test_annotate_is_json_native():
    import json

    rows, nav = _nov16_rows()
    block = annotate_lot_rounding_drag(rows, cash_proxy=_CASH, portfolio_value=nav)
    json.dumps(block, ensure_ascii=False)
    json.dumps(rows, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 5. The two render-site cross-references (vol-ceiling caption <-> EQUITY DIRECTION)
# ---------------------------------------------------------------------------
#
# Two independent changes touched the rendering layer on 2026-11-16:
#   * the vol-ceiling work added a SCOPE caption ending with
#     `…当前仓位变动方向见票面 EQUITY DIRECTION。`;
#   * the ticket-audit work prints the equity direction under the literal tag
#     `EQUITY DIRECTION`.
# The caption is emitted ONLY for a BINDING `stage1` ceiling. These tests pin the contract
# from both ends: the literal exists in every direction-line variant, the caption's pointer
# exists, and the one place that prints the caption is structurally incapable of running
# without the direction line later in the same `main()` (no `sys.exit`, no early `return`).

_CAPTION_POINTER = "当前仓位变动方向见票面 EQUITY DIRECTION"

#: `format_vol_scope_alert_line` emits this caption only for `stage1` + `binding`.
_BINDING_STAGE1_OVERLAY = {
    "applied": True,
    "scope": VOL_CEILING_SCOPE_STAGE1,
    "scope_applied": VOL_CEILING_SCOPE_STAGE1,
    "binding": True,
    "model_equity_sum": 0.7502,
    "capped_equity_sum": 0.396,
    "vol_scaling_factor": 0.5278,
    "realized_vol_21d": 0.2431,
}


def test_caption_and_direction_line_share_the_literal():
    caption = format_vol_scope_alert_line(_BINDING_STAGE1_OVERLAY, None)
    assert caption is not None
    assert _CAPTION_POINTER in caption
    line = format_equity_direction_line(
        compute_equity_direction(_nov16_rows()[0], cash_proxy=_CASH)
    )
    assert "EQUITY DIRECTION" in caption
    assert "EQUITY DIRECTION" in line


@pytest.mark.parametrize("direction", ["ADD", "REDUCE", "FLAT", "UNKNOWN"])
@pytest.mark.parametrize("with_note", [True, False])
def test_every_direction_line_variant_carries_the_literal(direction, with_note):
    """ADD / REDUCE / FLAT (and an unmapped verdict), with and without the note."""
    note = "上限绑定…切勿读成今日在降风险。" if with_note else None
    block = {
        "equity_before": 0.251,
        "equity_after": 0.323,
        "delta_pp": 7.2,
        "net_trade_value_rmb": 14193.0,
        "direction": direction,
        "vol_cap_binding": True if with_note else None,
        "interpretation_note": note,
    }
    line = format_equity_direction_line(block)
    assert "EQUITY DIRECTION" in line
    assert "权益方向" in line


def test_caption_is_only_emitted_for_stage1_and_binding():
    """The caption's exact gate -- the same condition the direction line always satisfies."""
    assert format_vol_scope_alert_line(None, None) is None
    assert format_vol_scope_alert_line({}, None) is None
    assert format_vol_scope_alert_line(
        dict(_BINDING_STAGE1_OVERLAY, binding=False), None
    ) is None
    # Portfolio scope prints its own banner and needs no scope caption.
    assert format_vol_scope_alert_line(
        dict(
            _BINDING_STAGE1_OVERLAY,
            scope=VOL_CEILING_SCOPE_PORTFOLIO,
            scope_applied=VOL_CEILING_SCOPE_PORTFOLIO,
        ),
        None,
    ) is None
    # A scope that is not spelled `stage1` must not print the pointer either.
    for stale in ("portfolio", "", "STAGE1-ish"):
        assert (
            format_vol_scope_alert_line(
                dict(_BINDING_STAGE1_OVERLAY, scope_applied=stale), None
            )
            is None
        )
    missing_scope = dict(_BINDING_STAGE1_OVERLAY)
    missing_scope.pop("scope_applied")
    assert format_vol_scope_alert_line(missing_scope, None) is None
    # The one case that prints: stage1 + binding.
    assert format_vol_scope_alert_line(_BINDING_STAGE1_OVERLAY, None) is not None


def _main_function_ast():
    import ast
    import inspect

    import scripts.run_live_strategy as mod

    return ast.parse(inspect.getsource(mod.main))


def _parent_map(tree):
    import ast

    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _main_node(tree):
    import ast

    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")


def _find_calls(tree, name):
    import ast

    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def test_equity_direction_is_printed_unconditionally_in_main():
    """`compute` / `print` / payload attach are all direct children of `main`'s body."""
    import ast

    tree = _main_function_ast()
    main = _main_node(tree)
    parents = _parent_map(tree)

    computes = _find_calls(main, "compute_equity_direction")
    assert len(computes) == 1
    compute_stmt = parents[computes[0]]
    assert parents[compute_stmt] is main  # `equity_direction_block = compute(...)`

    prints = [
        stmt
        for stmt in main.body
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and "format_equity_direction_line" in ast.dump(stmt.value)
    ]
    assert len(prints) == 1
    assert parents[prints[0]] is main

    attach = [
        stmt
        for stmt in main.body
        if isinstance(stmt, ast.Assign)
        and any(
            isinstance(t, ast.Subscript)
            and isinstance(t.value, ast.Name)
            and t.value.id == "ticket_payload"
            and "equity_direction" in ast.dump(t.slice)
            for t in stmt.targets
        )
    ]
    assert len(attach) == 1


def test_caption_site_is_conditional_and_cannot_precede_an_early_exit():
    """Structural guarantee: the caption only runs behind the binding banner's guards, the
    direction line does not, and `main` cannot terminate between the two (no `return`, no
    `raise`, no `sys.exit`)."""
    import ast

    tree = _main_function_ast()
    main = _main_node(tree)
    parents = _parent_map(tree)

    captions = _find_calls(main, "format_vol_scope_alert_line")
    assert len(captions) == 1
    caption = captions[0]
    caption_lineno = caption.lineno

    # Walk up from the call to `main`: the capture is an assignment under `if`s only.
    enclosing = []
    cur = parents[caption]
    while cur is not None and cur is not main:
        enclosing.append(cur)
        cur = parents.get(cur)
    assert isinstance(enclosing[0], ast.Assign)  # `_scope_line = format_vol_scope_alert_line(...)`
    assert all(isinstance(n, (ast.Assign, ast.If)) for n in enclosing)
    # ... and the banner guard it lives behind is the BINDING one.
    guarding_ifs = [n for n in enclosing if isinstance(n, ast.If)]
    assert guarding_ifs
    assert "binding" in ast.dump(guarding_ifs[0].test)

    prints = [
        stmt
        for stmt in main.body
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and "format_equity_direction_line" in ast.dump(stmt.value)
    ]
    assert len(prints) == 1
    direction_lineno = prints[0].lineno
    assert caption_lineno < direction_lineno

    def _is_terminator(stmt):
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and "sys.exit" in ast.dump(stmt.value)
        )

    between = [
        stmt for stmt in main.body if caption_lineno < stmt.lineno < direction_lineno
    ]
    assert between, "expected top-level statements between the caption and the direction line"
    assert not any(_is_terminator(stmt) for stmt in between)
