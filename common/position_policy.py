"""Unified position-policy kernel -- the single policy layer of §4-§6 of
`docs/position_policy_consolidation_design.md`.

WHAT THIS MODULE IS
===================
One kernel with three layers, split exactly as the design document requires
(and for the reason it requires: a per-symbol state machine CANNOT express
"sells release cash first, buys are capped by freed cash, the residual goes to
the cash proxy" -- that is a cross-asset constraint; conversely a portfolio loop
cannot express per-symbol tenure/confirmation state):

  * `AssetPolicy`     -- per-symbol, pure. `step()` (the 6-state lifecycle port)
                         and `inertia_step()` (the per-symbol half of the
                         inertia filter).
  * `PortfolioSequencer` -- cross-asset only: sells-first, buy purchasing power
                         from freed cash, residual -> `cash_proxy`.
  * `SparseProjector` -- the sparse-weights projection and its write-back table.

STAGE: M4.1 DEAD CODE -> M4.2 PHASE 2 PARTIAL WIRING
====================================================
M4.1 shipped this module as deliberately DEAD CODE. Phases 1 and 2 of M4.2 have
since wired it, bit-identically, at exactly these points:

  * `common/allocation_templates.py` -- `apply_asset_inertia` is now a single
    delegation to `kernel_apply_asset_inertia` (M4.2 phase 2 / commit 1b). Its
    `_step_asset_inertia` and `_sparse_from_daily` remain the module's OWN
    implementations and are NOT kernel wrappers.
  * `pipeline/research/blend/blend_base.py` -- the per-bar inertia step delegates
    to `PortfolioSequencer.sequence` behind the default-OFF
    `blend_use_policy_kernel` (phase 1), and the final dense->sparse compression
    now calls `SparseProjector.project` unconditionally (phase 2 / commit 1a;
    projection has no behavioural knob to gate).
  * `common/position_lifecycle.py` STILL runs its own `step_position_lifecycle`;
    it only RE-EXPORTS the enums/labels/predicates defined here (see part E of
    M4.1 -- a type migration, no behaviour).
  * `backtester/`, `scripts/` and `web/` are untouched; `SparseProjector.write`
    and `AssetPolicy.step`'s 6-state gates are NOT wired into any backtest path
    yet (M4.3 / M4.1 items #4/#5).

The live-trading wiring (`live_use_policy_kernel`) is still a later stage
(design §8 stage 3). Do not bump any `version` / `BASE_VERSION` for this file:
it carries no version constant on purpose, and no call-site flip here changes a
backtest number.

BIT-IDENTICAL REFERENCE
=======================
The inertia half of this kernel (`AssetPolicy.inertia_step` +
`PortfolioSequencer.sequence` + `kernel_apply_asset_inertia`) is a mechanical
transcription of `common/allocation_templates.py::_step_asset_inertia`
(`:217-311`) and `apply_asset_inertia` (`:314-398`), split per-symbol /
cross-asset. It is required to be CELL-IDENTICAL to those two functions for the
same arguments -- including NaN positions (a NaN cell means "no instruction",
it is never 0.0) -- and that equivalence is what `kernel_apply_asset_inertia` is
for. Any behaviour change here is a bug, not an improvement.

DOCUMENTED EXCEPTION -- "emergency" means opposite things in the two paths
=========================================================================
This is the single most dangerous name collision in the consolidation
(design §2), and the kernel reproduces it verbatim instead of "fixing" it:

  * INERTIA PATH: `emergency_mask` TRUE is a PASS-THROUGH of the ideal weights
    (`_step_asset_inertia:251-252` returns `ideal_w.copy()` unchanged). It can
    therefore BUY, and it writes no cash residual.
  * STATE-MACHINE PATH: `PolicyContext.is_emergency_halt` TRUE is a FORCED
    SELL of the held position (`target_weight=0.0`, state -> COOLDOWN, stop
    cooldown). It can only exit.

Unifying these two semantics is an explicitly deferred, separate, approval-required
task (design §10). It must NOT be smuggled into this kernel.

WHAT IS *NOT* CLAIMED HERE
==========================
* `AssetPolicy.step` is a FAITHFUL PORT of `step_position_lifecycle`, not a
  proven bit-identical one. It differs in representation only (no in-place
  mutation of the caller's state, counters travel in `detail["state_after"]`,
  `next_state` instead of `state_after`). Parity is proven by the M4.3 adapter
  stage against archived replays, NOT by this module and NOT by M4.1.
* The sparse projection (`SparseProjector.project`) is a literal transcription
  and IS intended to be identical, but it also has zero callers in M4.1.

OPT-IN EXTENSION -- gate 2b, two-day structural breakdown (DEFAULT OFF)
=====================================================================
`PositionPolicyConfig.two_day_breakdown_rule` (default `False`) inserts a gate
2b into `AssetPolicy.step`, between the stop fast path (gate 2) and the cooldown
gate (gate 3), evaluated ONLY for already-held states:

  * `close < min(ma20_close, val)` (or `< ma20_close` when `val` is absent) is a
    counted breakdown bar -- but only when the bar is not a 缩量回踩, i.e. only
    when volume is not below `breakdown_vol_shrink_pct * vol_ma20`;
  * one counted bar -> `PENDING_EXIT` + `HOLD_FILTERED`/`EXIT_CONFIRMING` (no
    trade, no row), `breakdown_days` counted bars -> `SELL` at an explicit
    `0.0` + `COOLDOWN`;
  * a close back at/above the floor clears the counter and falls through to the
    pre-existing ladder unchanged (假破位反弹 never sells);
  * every input it needs is optional and a missing one degrades to a NO-OP --
    `None` is never read as 0, and no breakdown is ever fabricated.

It is a strict no-op when the flag is `False` (the branch is not entered at
all), and it uses its OWN `breakdown_streak` counter so the `exit_streak` /
`guard_latency_bars` signal-exit confirmation is never double-counted.

OPT-IN EXTENSION -- gate 3b, price/VWAP re-entry confirmation (DEFAULT OFF)
=========================================================================
`PositionPolicyConfig.price_reentry_gate` (default `False`) evaluates the same
design item's re-entry rule on the bar gate 3's cooldown counter reaches zero --
the bar the asset would otherwise become eligible to re-enter:

  * PASS if `price > last_exit_price * (1 + price_reentry_pct)` (the ledger's
    2% rebound), or if a VWAP reclaim is confirmed on this bar
    (`price > vwap20` AND `volume > vol_ma20`);
  * BLOCK otherwise -> `HOLD_FILTERED` / `BlockingReason.REENTRY_GATED` at
    `target_weight=0.0`, the asset stays `FLAT` (no position), and
    `last_exit_price` is kept so the decision stays auditable and comparable;
  * a MISSING / non-positive / non-finite `last_exit_price` degrades to ALLOW
    with a warning: an unknown exit print must never lock a symbol out of ever
    re-entering (`_reentry_gate_block`).

WHY IT IS OFF BY DEFAULT (recorded decision, not an oversight): switching it on
makes EVERY post-cooldown re-entry require a 2% rebound off the exit print,
which is a live trading-behaviour change, and it would break `M4.2`'s
bit-identical guarantee. The explicit switch keeps the engine byte-identical
while preserving the ledger's `lifecycle_price_reentry_pct=0.02` threshold
instead of quietly dropping it. `AssetState.last_exit_price` is written by every
exit path (stop / breakdown / model-signal) so the gate has data; the gate is
evaluated ONLY on the cooldown-expiry bar, which is the only bar gate 3 owns --
so a blocked re-entry is a ONE-BAR VETO (the block is returned for that bar and
not persisted: the next bar skips gate 3, is never re-asked, and proceeds through
the normal entry ladder), never a sustained gated state. See
`AssetPolicy._reentry_gate_block` for the full consequence statement.

INVARIANTS (design §5)
======================
1. long-only: the per-symbol layer never emits a negative target of its own
   accord, and the portfolio layer keeps `sum(risky) <= 1` by construction
   (cash is the residual). raw model targets are passed through, never clamped.
2. no NaN: `Decision.__post_init__` raises on a NaN `target_weight`; a
   `blocking_reason != NONE` row can never be promoted into an exit (see
   `is_liquidation_event` -- only `action == "SELL"` is an exit).
3. idempotent: `AssetPolicy.step` does not mutate its `AssetState`/`PolicyContext`;
   `held_bars` is non-decreasing while held and `cooldown_remaining` is
   non-increasing (nothing here re-arms them upward except a fresh risk exit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd


_LOGGER = logging.getLogger(__name__)


__all__ = [
    "PositionState",
    "ExitType",
    "BlockingReason",
    "BLOCKING_REASON_LABELS",
    "coerce_blocking_reason",
    "blocking_reason_label",
    "is_liquidation_event",
    "PolicyAction",
    "AssetState",
    "PolicyContext",
    "Decision",
    "PositionPolicyConfig",
    "AssetPolicy",
    "PortfolioSequencer",
    "SparseProjector",
    "kernel_apply_asset_inertia",
]


# ---------------------------------------------------------------------------
# Enums, labels and the liquidation predicate.
#
# These seven names (through `is_liquidation_event`) were MOVED here from
# `common/position_lifecycle.py` in M4.1 and are re-exported from there, so
# every existing import site keeps working. The docstrings are carried over
# verbatim -- they encode the post-mortem that produced them, and deleting them
# is how the bug class comes back.
# ---------------------------------------------------------------------------


class PositionState(str, Enum):
    """Lifecycle states for a portfolio asset (kept at six, design §5)."""
    FLAT = "FLAT"
    PENDING_ENTRY = "PENDING_ENTRY"
    HELD_GESTATION = "HELD_GESTATION"
    HELD_ACTIVE = "HELD_ACTIVE"
    PENDING_EXIT = "PENDING_EXIT"
    COOLDOWN = "COOLDOWN"


class ExitType(str, Enum):
    """Categorization of liquidation triggers."""
    NORMAL = "NORMAL"
    HARD_STOP = "HARD_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    # Opt-in two-day structural breakdown exit (gate 2b, `two_day_breakdown_rule`).
    # Deliberately NOT folded into HARD_STOP: `HARD_STOP` means "loss_pct crossed
    # hard_stop_loss_pct", and reusing it would make a `min(MA20, VAL)` structural
    # break indistinguishable from a fixed-percentage stop in the forensic record
    # -- exactly the label conflation `BlockingReason` was introduced to end.
    BREAKDOWN = "BREAKDOWN"


class BlockingReason(str, Enum):
    """Structured "why was no trade emitted" label -- `docs/development_plan.md` L2.24.

    Root cause this enum exists for (disclosed): `HOLD_FILTERED` was reused by
    SIX branches of `step_position_lifecycle` plus FOUR producer sites in
    `scripts/run_live_strategy.py`, so `action` alone could not distinguish
    "a signal was evaluated and rejected" from "nothing was evaluated at all"
    (market-closed freeze) or from "the order was blocked by a risk gate"
    (tenure lock / limit-up lock / cooldown). Downstream consumers then had no
    honest way to ask "did this row liquidate the position?" other than
    inferring it from `target_weight == 0.0` -- an inference that is WRONG for
    every branch below except a real `SELL` (see `docs/version.md` §11 and the
    `L2.21` phantom-anchor post-mortem: 34 never-traded rows across 25 archived
    tickets were read as liquidations).

    Contract:
      - `NONE` means "no blocking gate fired"; it is the default for BUY / SELL
        / plain HOLD / SWEEP_CASH / RELEASE_CASH / CANCEL_PENDING rows.
      - A non-`NONE` value is *only* ever an explanation of why a row traded
        nothing. It can never promote a non-`SELL` row into an exit.
      - Persisted/archived rows may predate this field; readers MUST go through
        `coerce_blocking_reason()` (missing -> `NONE`) and MUST NOT derive
        "exited or not" from `target_weight` (see `is_liquidation_event`).
    """

    # Nothing was evaluated: not a signal filter, not a risk veto.
    MARKET_CLOSED_FREEZE = "MARKET_CLOSED_FREEZE"
    # A real signal was evaluated and rejected by a gate.
    MIN_ENTRY_THRESHOLD = "MIN_ENTRY_THRESHOLD"
    PENDING_ENTRY_CONFIRM = "PENDING_ENTRY_CONFIRM"
    EXIT_CONFIRMING = "EXIT_CONFIRMING"
    COOLDOWN_BLOCKED = "COOLDOWN_BLOCKED"
    INERTIA = "INERTIA"
    TENURE_LOCKED = "TENURE_LOCKED"
    LIMIT_UP_LOCKED = "LIMIT_UP_LOCKED"
    # Missing / non-positive price on evaluation bar.
    NO_PRICE = "NO_PRICE"
    # Opt-in price/VWAP re-entry gate (gate 3b, `price_reentry_gate`): the
    # cooldown expired, but neither the 2%-rebound leg nor the VWAP-reclaim leg
    # is satisfied, so the asset stays FLAT and no entry row is written. Its own
    # member (rather than reusing `COOLDOWN_BLOCKED`) because the cooldown HAS
    # expired here -- the block is a price-confirmation veto, not a timer.
    REENTRY_GATED = "REENTRY_GATED"
    # No blocking gate fired.
    NONE = "NONE"


# Chinese display labels. Kept next to the enum so the live ticket, the Phase 3
# console rendering and any future web badge cannot drift apart in wording.
BLOCKING_REASON_LABELS: Dict[str, str] = {
    BlockingReason.MARKET_CLOSED_FREEZE.value: "休市未评估",
    BlockingReason.MIN_ENTRY_THRESHOLD.value: "未达最低建仓门槛",
    BlockingReason.PENDING_ENTRY_CONFIRM.value: "开仓信号确认中",
    BlockingReason.EXIT_CONFIRMING.value: "平仓信号确认中",
    BlockingReason.COOLDOWN_BLOCKED.value: "再入场冷却中",
    BlockingReason.INERTIA.value: "未超惰性阈值",
    BlockingReason.TENURE_LOCKED.value: "未满最低持仓周期",
    BlockingReason.LIMIT_UP_LOCKED.value: "涨停封板",
    BlockingReason.NO_PRICE.value: "价格缺失",
    BlockingReason.REENTRY_GATED.value: "再入场价格未确认",
    BlockingReason.NONE.value: "无阻断",
}


def coerce_blocking_reason(value: Any) -> BlockingReason:
    """Legacy-tolerant reader for a persisted `blocking_reason`.

    Archived `live_trading_ticket.csv` files written before `L2.24` have no such
    column at all (`pandas.Series.get` -> None), older JSON rows have no such
    key, and a CSV round-trip yields the literal string. All three must read as
    `NONE` -- never raise, never be mistaken for a gate that actually fired.
    Anything unrecognised is also `NONE` (fail open: an unknown label may not
    claim a block, and it certainly may not claim an exit).
    """
    if isinstance(value, BlockingReason):
        return value
    if value is None:
        return BlockingReason.NONE
    text = str(value).strip().upper()
    if text in ("", "NAN", "NONE", "NULL"):
        return BlockingReason.NONE
    try:
        return BlockingReason(text)
    except ValueError:
        return BlockingReason.NONE


def blocking_reason_label(value: Any) -> str:
    """Chinese display label for a `blocking_reason` (legacy values tolerated)."""
    return BLOCKING_REASON_LABELS.get(coerce_blocking_reason(value).value, "无阻断")


def is_liquidation_event(action: Any, blocking_reason: Any = None) -> bool:
    """Whether a ticket row represents an ACTUAL liquidation (`docs/development_plan.md` L2.24).

    The single predicate every consumer must use instead of inferring "did we
    exit?" from `target_weight == 0.0`. That inference is the phantom-anchor bug
    class (`docs/version.md` §11): `MIN_ENTRY_THRESHOLD`, `PENDING_ENTRY_CONFIRM`
    and `MARKET_CLOSED_FREEZE` all write a 0.0 target while having traded
    nothing, and `CANCEL_PENDING` is a never-held record.

    `blocking_reason` is accepted for signature stability / caller honesty but is
    deliberately NOT part of the verdict: only `action == "SELL"` can mean "the
    position was liquidated". A blocking label can never promote a non-`SELL`
    row, and a `SELL` is never demoted by a stale/contradictory label (every
    `SELL` path in `position_lifecycle` and in the live runner emits `NONE`).
    """
    return str(action or "").strip().upper() == "SELL"


# ---------------------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------------------


class PolicyAction:
    """The kernel's action vocabulary -- plain `str` constants.

    Deliberately NOT an enum: `Decision.action` is consumed by the ticket
    builder, the sparse projector and the archived-CSV readers as a plain
    string, and every one of those already compares against literals. An enum
    here would add a second truth to coerce (a `str`-mixin enum would compare
    equal to its value, but `PolicyAction.BUY == "BUY"` on a plain class does
    not -- and a `str` Enum would silently make `action` a `str` subclass, which
    changes what `type(action) is str` checks elsewhere see).
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    HOLD_FILTERED = "HOLD_FILTERED"
    CANCEL_PENDING = "CANCEL_PENDING"
    COOLDOWN_BLOCKED = "COOLDOWN_BLOCKED"


#: Actions that write no order at all -- a row carrying one of these must never
#: be materialised as an explicit `0.0` in the sparse frame (design §4 table).
NON_TRADING_ACTIONS = frozenset(
    {
        PolicyAction.HOLD,
        PolicyAction.HOLD_FILTERED,
        PolicyAction.CANCEL_PENDING,
        PolicyAction.COOLDOWN_BLOCKED,
    }
)


def _is_zero_request(raw_target_weight: float) -> bool:
    """`raw_target_weight` is exactly a flat-zero request from the model.

    Why this is a helper instead of an inline comparison: the static
    anti-recurrence suite `pipeline/tests/test_no_weight_zero_liquidation_inference.py`
    forbids a literal zero test on `raw_target_weight` /
    `target_weight` inside any branch that talks about exiting, unless the exact
    line is registered as an exemption. That exemption list is keyed to
    `common/position_lifecycle.py` (which keeps its own literal copy, pinned by
    the same suite), so this module must express the SAME test through a call
    the AST scan cannot mistake for a weight-vs-zero exit inference. Semantics
    are identical: `float(x) == 0.0` for every float/int x.

    The zero here is the RAW model target -- the *source* of a genuine exit
    request, not an inference from a post-policy weight. See `is_liquidation_event`.
    """
    return float(raw_target_weight) == 0.0


# ---------------------------------------------------------------------------
# Data carriers (design §5). Every float field is a plain, finite weight: see
# `Decision.__post_init__` for the one hard invariant that is enforced.
# ---------------------------------------------------------------------------


@dataclass
class AssetState:
    """Per-symbol persistent state carried INTO a policy step.

    A superset of `AssetLifecycleState` (same field names, same meaning) plus
    `expansion_streak` for the blend engines' expansion-confirmation ladder, plus
    the opt-in-rule fields `breakdown_streak` (gate 2b's own confirmation counter,
    never `exit_streak`) and `last_exit_price` (retained through cooldown for the
    opt-in price/VWAP re-entry gate, gate 3b).
    The kernel NEVER mutates the instance it is given: `AssetPolicy.step`
    works on a private copy and returns the updated snapshot in
    `Decision.detail["state_after"]` (design invariant 3: the same
    `(state, inputs, cfg)` must always produce the same `Decision`).

    `broker_weight` is the PRE-TRADE brokerage snapshot, hydrated from live
    holdings; it exists so a persisted state cannot silently store the TARGET
    as if it were the broker weight (the 000999.SZ archive incident). The
    kernel never mutates it either.
    """

    symbol: str
    state: PositionState = PositionState.FLAT
    held_bars: int = 0
    entry_streak: int = 0
    exit_streak: int = 0
    expansion_streak: int = 0
    cooldown_remaining: int = 0
    entry_price: float = 0.0
    peak_price: float = 0.0
    current_weight: float = 0.0
    broker_weight: float = 0.0
    last_exit_type: Optional[str] = None
    #: Separate confirmation counter for the OPT-IN two-day structural-breakdown
    #: rule (gate 2b). It is deliberately NOT `exit_streak`: `exit_streak` is the
    #: model-signal exit gate paired with `guard_latency_bars`, and reusing it
    #: would double-count two independent confirmations into one counter (one
    #: broken-structure day plus one model-exit day would look like a confirmed
    #: exit). Default-inert: nothing increments it unless
    #: `two_day_breakdown_rule` is True.
    breakdown_streak: int = 0
    #: Close of the last exit (stop / emergency / breakdown / model-signal),
    #: retained through the cooldown so the opt-in price/VWAP re-entry gate
    #: (gate 3b) can compare against it (`docs/long_only_multi_asset_gap_analysis.md`
    #: §P0-1, design item M1.1). Written by EVERY exit path; `0.0` means
    #: "unknown" and the gate treats it as "cannot evaluate" -> ALLOW with a
    #: warning, never as a permanent lockout.
    last_exit_price: float = 0.0


@dataclass
class PolicyContext:
    """Everything a single-bar policy step may look at, other than `cfg`.

    `bar_index` is the CALLER's clock (the `t` of the dense daily frame in the
    inertia path, the bar counter in the state-machine path). The kernel keeps
    no module-level clock of its own -- the two `holding_age` / `last_buy_bar`
    dicts of the inertia path are caller-owned (see `kernel_apply_asset_inertia`),
    so the kernel stays a pure function with no mutable global state.
    """

    price: float = 0.0
    atr: Optional[float] = None
    vwap: Optional[float] = None
    volume: Optional[float] = None
    ma20_volume: Optional[float] = None
    market_breadth: Optional[float] = None
    market: str = "china"
    is_market_open: bool = True
    is_emergency_halt: bool = False
    is_portfolio_deleverage: bool = False
    structural: dict = field(default_factory=dict)
    bar_index: int = 0
    # ---- inputs of the two OPT-IN rules (gate 2b / gate 3b) ----------------
    # Every one of these is optional, and `None` means "this bar cannot be
    # evaluated by that rule" -- never 0.0, never "no breakdown", never a
    # fabricated break. They are separate fields from `vwap` / `ma20_volume`
    # above on purpose: `ma20_volume` belongs to the entry fast path's volume
    # surge test, and folding the two would merge two orthogonal thresholds
    # (`docs/development_plan.md` M1.1 item 6: 正交性，不得合并阈值).
    # `ma20_close` / `val` feed gate 2b (two-day structural breakdown).
    # `vwap20` / `vol_ma20` feed gate 3b (price/VWAP re-entry confirmation):
    # `vwap20` is a PRE-COMPUTED 20-day VWAP -- the kernel never derives one, so
    # `PositionPolicyConfig.reentry_vwap_lookback` only documents the derivation
    # window for the layer that computes it.
    ma20_close: Optional[float] = None
    vwap20: Optional[float] = None
    vol_ma20: Optional[float] = None
    val: Optional[float] = None


@dataclass
class Decision:
    """One per-symbol policy verdict for one bar.

    Field order and defaults are fixed by design §5. `reason` / `detail` are
    forensic payload (Chinese, mirroring the live ticket); `action` is one of
    `PolicyAction`; `blocking_reason` explains a non-trading row and is NEVER a
    promotion of a non-`SELL` row into an exit (`is_liquidation_event`).

    Invariant 2 is enforced here rather than at every producer: a NaN
    `target_weight` is a programming error (a merge/align bug upstream), and a
    NaN that reaches a ticket or a share calculation is silently falsy in every
    comparison, so it must raise at construction time.
    """

    action: str
    target_weight: float
    reason: str = ""
    blocking_reason: BlockingReason = BlockingReason.NONE
    next_state: PositionState = PositionState.FLAT
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        weight = float(self.target_weight)
        if weight != weight:  # NaN, without importing math
            raise ValueError(
                "Decision.target_weight must never be NaN (design invariant 2): "
                f"action={self.action!r} reason={self.reason!r}"
            )
        self.target_weight = weight
        # Accept a raw string (CSV replay, hand-built fixtures) but store the enum.
        self.blocking_reason = coerce_blocking_reason(self.blocking_reason)


@dataclass
class PositionPolicyConfig:
    """Kernel configuration.

    DEFAULTS ARE THE DESIGN §5 DEFAULTS, VERBATIM -- and they are NOT the live
    deployment's defaults. Disclosed divergence, deliberately kept:

      * `min_holding_bars=1` / `normal_cooldown_bars=1` here vs **2 / 2** in
        `common.position_lifecycle.PositionLifecycleConfig` (the values
        `scripts/run_live_strategy.py:1555-1573` actually deploys). Reconciling
        the two is the M4.3 live-adapter's job: the adapter resolves a
        `PositionPolicyConfig` from the live defaults + the strategy's alias
        keys and passes it in. Nothing in M4.1 changes a default anywhere, and
        no engine reads this class yet.

    `asset_stop_cooldown_bars` is the PER-ASSET post-stop re-entry cooldown
    (5 bars). It is NOT `blend_base`'s `stop_cooldown_bars=21`, which is a
    PORTFOLIO drawdown self-healing cooldown -- the same key, two meanings,
    which is exactly what this kernel exists to make impossible (design §2).

    `entry_threshold` is the minimum entry weight gate. It is separate from
    `guard_latency_bars` (the confirmation window): the old resolvers parsed
    these two side by side under one name family, which is how "门槛" and
    "确认期" got conflated (design §2).

    Two rule blocks below are OPT-IN and default OFF, each with its own switch
    (`two_day_breakdown_rule` -> gate 2b, `price_reentry_gate` -> gate 3b), so
    the default configuration keeps every pre-change decision byte-identical.
    `price_reentry_pct=0.02` is the ledger's `lifecycle_price_reentry_pct` kept
    verbatim; only the switch is new.
    """

    guard_latency_bars: int = 2
    min_holding_bars: int = 1
    normal_cooldown_bars: int = 1
    asset_stop_cooldown_bars: int = 5
    inertia_threshold: float = 0.05
    inertia_relative_ratio: float = 0.0
    asymmetric_sell_ratio: float = 0.60
    asymmetric_sells: bool = False
    entry_threshold: Optional[float] = None
    hard_stop_loss_pct: float = 0.08
    atr_multiplier: float = 4.5
    volume_surge_multiplier: float = 1.5
    breadth_thrust_threshold: float = 0.60
    # ---- OPT-IN two-day structural-breakdown confirmation (gate 2b) ----------
    # `two_day_breakdown_rule` is the ONLY switch. While it is False (default)
    # gate 2b is not entered at all, so the policy chain is byte-for-byte the
    # pre-change chain and `common/tests/test_position_policy.py`'s parity
    # harnesses keep their 240-seed cell-level guarantee.
    #
    # `breakdown_days` is the number of counted breakdown bars required before
    # the SELL. It is CLAMPED UP to 2 in `__post_init__`: a 1-day setting would
    # be a single-bar whipsaw exit, which is precisely what this rule exists to
    # prevent and what its name promises. A caller who genuinely wants an
    # unconfirmed structural exit should disable the rule and use the stop path,
    # not run a two-day rule with one day of confirmation.
    #
    # `breakdown_vol_shrink_pct` is the 缩量回踩 exemption: a bar whose volume is
    # below this fraction of `vol_ma20` does not count as a breakdown.
    two_day_breakdown_rule: bool = False
    breakdown_days: int = 2
    breakdown_vol_shrink_pct: float = 0.60
    # ---- OPT-IN price/VWAP re-entry confirmation (gate 3b) -------------------
    # `price_reentry_gate` is the ONLY switch, and it is OFF by default ON
    # PURPOSE: with it ON every post-cooldown re-entry would require a 2%
    # rebound off the last exit print, which is a live trading-behaviour change
    # (and it would break M4.2's bit-identical guarantee). Keeping the switch
    # explicit preserves the ledger's `lifecycle_price_reentry_pct=0.02`
    # threshold instead of silently dropping it. While it is False the gate-3
    # expiry path is byte-for-byte the pre-change path.
    #
    # `reentry_vwap_lookback` is PLUMBING ONLY: the kernel receives a
    # pre-computed `PolicyContext.vwap20`, so this value is consumed by the
    # derivation layer that builds the context, not by the kernel.
    price_reentry_gate: bool = False
    price_reentry_pct: float = 0.02
    reentry_vwap_lookback: int = 20

    def __post_init__(self) -> None:
        """Normalise `breakdown_days` (never below the two-day confirmation floor).

        Downstream code compares `breakdown_streak >= breakdown_days` directly,
        so the clamp must live here rather than at the comparison: one clamped
        value is the single truth. The clamp is applied even while the rule is
        OFF so the dataclass is normalised regardless of the flag (an inert
        value that silently becomes a 1-day rule when the flag is turned on is
        worse than a normalised one).
        """
        try:
            days = int(self.breakdown_days)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "two_day_breakdown_rule: non-numeric breakdown_days=%r, falling back to 2",
                self.breakdown_days,
            )
            days = 2
        if days < 2:
            _LOGGER.warning(
                "two_day_breakdown_rule: breakdown_days=%r is below the two-day confirmation "
                "minimum, clamping to 2 (a 1-day rule would be a single-bar whipsaw exit and "
                "contradicts the rule's own contract)",
                self.breakdown_days,
            )
            days = 2
        self.breakdown_days = days


def _snapshot(state: AssetState) -> Dict[str, Any]:
    """JSON-safe snapshot of an `AssetState` (the `AssetLifecycleState.to_dict` shape)."""
    return {
        "symbol": state.symbol,
        "state": state.state.value if isinstance(state.state, PositionState) else str(state.state),
        "held_bars": int(state.held_bars),
        "entry_streak": int(state.entry_streak),
        "exit_streak": int(state.exit_streak),
        "expansion_streak": int(state.expansion_streak),
        "cooldown_remaining": int(state.cooldown_remaining),
        "entry_price": float(state.entry_price),
        "peak_price": float(state.peak_price),
        "current_weight": float(state.current_weight),
        "broker_weight": float(state.broker_weight),
        "last_exit_type": state.last_exit_type,
        "breakdown_streak": int(state.breakdown_streak),
        "last_exit_price": float(state.last_exit_price),
    }


def _decision(
    action: str,
    target_weight: float,
    reason: str,
    *,
    state_before: PositionState,
    state: AssetState,
    blocking_reason: BlockingReason = BlockingReason.NONE,
    detail: Optional[Dict[str, Any]] = None,
) -> Decision:
    """Build a `Decision` carrying the updated state snapshot.

    `next_state` is taken from the (already mutated) private copy, and the full
    snapshot -- counters, timers, prices -- travels in `detail["state_after"]`
    so the caller can commit exactly one transition without the kernel touching
    the caller's object.
    """
    payload: Dict[str, Any] = dict(detail or {})
    payload["state_before"] = (
        state_before.value if isinstance(state_before, PositionState) else str(state_before)
    )
    payload["state_after"] = _snapshot(state)
    return Decision(
        action=action,
        target_weight=target_weight,
        reason=reason,
        blocking_reason=blocking_reason,
        next_state=state.state,
        detail=payload,
    )


# ---------------------------------------------------------------------------
# Layer 1 -- per-symbol policy
# ---------------------------------------------------------------------------


class AssetPolicy:
    """Per-symbol policy steps: no I/O, no wall clock, no mutable global state.

    Two entry points, two different clocks (that asymmetry is REAL and is
    disclosed rather than unified here):

      * `inertia_step`  -- the per-symbol half of the dense daily inertia filter
        (`_step_asset_inertia`). State-free apart from the caller-owned
        `holding_age` / `last_buy_bar` dicts, which this method updates exactly
        like the reference does.
      * `step`          -- the 6-state lifecycle gate ladder
        (`step_position_lifecycle`). Consumes an `AssetState` and produces the
        next state; the caller owns persistence.
    """

    # -- inertia path -------------------------------------------------------

    def inertia_step(
        self,
        symbol: str,
        ideal_w: pd.Series,
        current_w: pd.Series,
        thresh: float,
        cfg: PositionPolicyConfig,
        *,
        ctx: PolicyContext,
        holding_age: Dict[str, int],
        last_buy_bar: Dict[str, int],
        symbol_emergency: bool = False,
        is_global_emergency: bool = False,
    ) -> Decision:
        """One symbol's contribution to one inertia bar.

        Bit-identical transcription of the per-symbol half of
        `_step_asset_inertia`; `PortfolioSequencer.sequence` supplies the
        cross-asset half (sells-first cash release, cash-capped buys, cash
        residual). The two halves share this `Decision` object:

          * `action` is `SELL` when the symbol's trim cleared the (possibly
            asymmetric) sell threshold and survived the tenure lock,
          * `BUY` when it cleared the buy threshold (`entry_threshold` replaces
            that threshold for a symbol currently at/below `1e-4`),
          * `HOLD_FILTERED` with `blocking_reason=INERTIA` for a micro-shift in
            the dead band, and `blocking_reason=TENURE_LOCKED` for a suppressed
            sell/partial trim,
          * `HOLD` for a symbol the bar does not touch at all.

        `ctx` carries the bar clock: `ctx.bar_index` is the reference's `t`, used
        only for the `t - last_buy_bar[s]` tenure arithmetic (the inertia path
        reads no other context field -- it has no price, no ATR and no breadth).

        `Decision.target_weight` here is the REQUEST (`ideal_w[symbol]`), not the
        final weight: a BUY may be capped by the sequencer to
        `current + available cash`. The sequencer's returned `Series` is the only
        authority on the committed weights.

        Side effects, and the ONLY ones: `holding_age[symbol]` and
        `last_buy_bar[symbol]` are updated in the caller's dicts (that is the
        reference's contract -- they must survive across bars). `current_w` /
        `ideal_w` / `ctx` are never mutated.
        """
        bar_index = int(ctx.bar_index)
        current = float(current_w[symbol])
        ideal = float(ideal_w[symbol])

        # (1) Tenure clock, bumped BEFORE any early return: the reference does
        # this above its global-emergency short-circuit, so a halt bar still ages
        # an incumbent by one bar (holding_age is "bars held", not "bars
        # evaluated"). Getting this ordering wrong silently re-arms a tenure lock
        # on every emergency day.
        if current > 1e-4:
            holding_age[symbol] += 1
        else:
            holding_age[symbol] = 0

        detail: Dict[str, Any] = {
            "bar_index": int(bar_index),
            "ideal_weight": ideal,
            "current_weight": current,
            "holding_age": int(holding_age[symbol]),
            "last_buy_bar": int(last_buy_bar[symbol]),
            "symbol_emergency": bool(symbol_emergency),
            "is_global_emergency": bool(is_global_emergency),
        }

        if is_global_emergency:
            # (3) Pass-through, unmodified -- no tenure filtering, no buy/sell
            # gating, and NO cash residual write (the sequencer returns the ideal
            # row as-is). This is the documented opposite of the state-machine
            # path's forced-SELL emergency (module docstring).
            detail["in_sells"] = False
            detail["in_buys"] = False
            return Decision(
                action=PolicyAction.HOLD,
                target_weight=ideal,
                reason="全局紧急状态：理想权重直接穿透（不评估惰性/持仓期限）",
                next_state=PositionState.FLAT,
                detail=detail,
            )

        # (4) Thresholds. The asymmetric sell band is a FRACTION of the
        # per-symbol threshold, applied only when the caller opted in; the buy
        # band is the raw threshold, except that a symbol currently at/below
        # 1e-4 (a fresh entry) uses `entry_threshold` when one is configured --
        # otherwise a volatility-scaled rebalancing band would block every entry.
        sell_thresh = (thresh * float(cfg.asymmetric_sell_ratio)) if cfg.asymmetric_sells else thresh
        buy_thresh = float(cfg.entry_threshold) if (cfg.entry_threshold is not None and current <= 1e-4) else thresh
        detail["sell_thresh"] = float(sell_thresh)
        detail["buy_thresh"] = float(buy_thresh)

        # (5) Sell candidacy, including the two independent tenure suppressions.
        diff = ideal - current
        detail["diff"] = float(diff)
        detail["desired_inc"] = max(0.0, ideal - current)

        in_sells = False
        is_sell_candidate = bool(diff <= -sell_thresh)
        detail["sell_candidate"] = is_sell_candidate
        if is_sell_candidate:
            if symbol_emergency:
                # Per-symbol emergency is a sell bypass -- it never blocks one.
                in_sells = True
            elif cfg.min_holding_bars > 0:
                if holding_age[symbol] < cfg.min_holding_bars:
                    # Mother position younger than the lock: suppress every sell,
                    # including a full exit.
                    detail["tenure_suppressed"] = "holding_age"
                elif ideal > 1e-4 and (bar_index - last_buy_bar[symbol]) < cfg.min_holding_bars:
                    # A PARTIAL trim of an add-on bought inside the lock is
                    # suppressed too, but an incremental add never resets the
                    # mother position's own tenure (that is why `last_buy_bar`
                    # is tracked separately from `holding_age`).
                    detail["tenure_suppressed"] = "recent_add"
                else:
                    in_sells = True
            else:
                in_sells = True

        # (6) Buy candidacy -- unconditional, no emergency/tenure bypass exists
        # in the reference (a blocked sell simply leaves its diff unmatched).
        in_buys = bool(diff >= buy_thresh)

        detail["in_sells"] = in_sells
        detail["in_buys"] = in_buys

        if in_sells:
            return Decision(
                action=PolicyAction.SELL,
                target_weight=ideal,
                reason="惰性过滤：减仓变动超过卖出阈值",
                next_state=PositionState.FLAT,
                detail=detail,
            )
        if in_buys:
            return Decision(
                action=PolicyAction.BUY,
                target_weight=ideal,
                reason="惰性过滤：加仓变动超过买入阈值",
                next_state=PositionState.FLAT,
                detail=detail,
            )
        if is_sell_candidate:
            # Reached only through a tenure suppression.
            return Decision(
                action=PolicyAction.HOLD_FILTERED,
                target_weight=current,
                reason="惰性过滤：减仓被最低持仓期限锁定，本 bar 不交易",
                blocking_reason=BlockingReason.TENURE_LOCKED,
                next_state=PositionState.FLAT,
                detail=detail,
            )
        return Decision(
            action=PolicyAction.HOLD_FILTERED,
            target_weight=current,
            reason="惰性过滤：变动未超阈值，权重冻结",
            blocking_reason=BlockingReason.INERTIA if diff != 0.0 else BlockingReason.NONE,
            next_state=PositionState.FLAT,
            detail=detail,
        )

    # -- state-machine path -------------------------------------------------

    def step(
        self,
        state: AssetState,
        raw_target_weight: float,
        ctx: PolicyContext,
        cfg: PositionPolicyConfig,
    ) -> Decision:
        """One bar of the 6-state lifecycle ladder, in the design §5 gate order.

        Fixed precedence (first short-circuit wins):

          1. market-closed freeze (`ctx.is_market_open` False) -- nothing is
             evaluated, NOTHING is liquidated, every counter/timer is frozen;
             immediately followed by the missing-price check (no price means no
             evaluation either, which is why it sits inside the "nothing was
             evaluated" prefix rather than among the risk gates);
          2. stop / emergency fast path -- FORCED SELL at `target_weight=0.0`,
             `next_state=COOLDOWN`, `asset_stop_cooldown_bars`; it bypasses BOTH
             the tenure lock and inertia (a T+1 lock must never convert a bounded
             stop into an unbounded tail loss -- the 2026-10-22 incident);
          2b. two-day structural breakdown confirmation -- OPT-IN, default OFF
             (`two_day_breakdown_rule`); evaluated only for held states, below
             the stop fast path. Not entered at all while the flag is False (see
             `_two_day_breakdown_step`);
          3. cooldown gate -- blocks entry only, never a holding; decrements the
             counter, and on expiry resets to FLAT and falls through;
          3b. price/VWAP re-entry confirmation -- OPT-IN, default OFF
             (`price_reentry_gate`); evaluated only on the cooldown-EXPIRY bar
             (see `_reentry_gate_block`). A block is `HOLD_FILTERED` /
             `REENTRY_GATED` at `0.0` with the asset kept FLAT, i.e. a missing
             row, never an explicit `0.0` exit;
          4. minimum entry weight gate;
          5. entry-confirmation streak (`guard_latency_bars`) + volume/breadth
             fast path;
          6. exit-confirmation streak (raw target exactly zero);
          7. inertia (absolute band, plus the optional relative trim hurdle);
          8. tenure lock -- SELL direction only, and it can never override
             gate 2 (which already returned).

        GATE 2 ABOVE GATE 3 IS NOT A SEMANTIC REORDERING: the original
        `step_position_lifecycle` decrements cooldown first and only then looks
        at holding states, so `cooldown_remaining > 0` always implies
        `state == COOLDOWN`, never a held state -- every branch below preserves
        that invariant (a risk exit sets COOLDOWN; nothing re-arms
        `cooldown_remaining` on a held state). With that invariant the two
        orders are observationally identical. If a future change ever lets a
        held state carry a positive cooldown, this ordering becomes a real
        behaviour change and must be re-approved, not silently kept.

        SEMANTICS PARITY IS NOT CLAIMED HERE. This is a faithful port of
        `common.position_lifecycle.step_position_lifecycle`, but it is allowed to
        differ in REPRESENTATION only: the caller's `AssetState` is copied, never
        mutated; counter/timer updates travel in `detail["state_after"]`;
        `next_state` stands in for `state_after`. The M4.3 live adapter is what
        must prove row-level parity against archived replays. Nothing in M4.1
        calls this method.

        Returns a `Decision` whose `blocking_reason` is set on EVERY branch that
        does not trade (invariant 2: a blocking label can never promote a row
        into an exit).
        """
        st = replace(state)  # private copy: invariant 3 (no caller mutation)
        state_before = st.state

        # ---- gate 1: market-closed freeze ---------------------------------
        if not ctx.is_market_open:
            # Freeze is explicit, not implied: nothing was evaluated, so nothing
            # can be blocked and nothing can be exited. The returned weight is
            # merely the carried-over holding.
            return _decision(
                PolicyAction.HOLD_FILTERED,
                float(st.current_weight),
                "非交易日/休市日：休市冻结，未评估任何信号"
                "(状态机、确认期、锁定期与冷却期计数器全部冻结，无任何交易指令)",
                state_before=state_before,
                state=st,
                blocking_reason=BlockingReason.MARKET_CLOSED_FREEZE,
                detail={
                    "is_market_open": False,
                    "signals_evaluated": False,
                    "note": "休市冻结，未评估任何信号 (NOT a signal filter, NOT an exit)",
                },
            )

        # ---- gate 1b: missing price (still "nothing was evaluated") -------
        if not (np.isfinite(ctx.price) and ctx.price > 0.0):
            return _decision(
                PolicyAction.HOLD_FILTERED,
                float(st.current_weight),
                "缺失有效价格：跳过评估，保持当前持仓权重与状态",
                state_before=state_before,
                state=st,
                blocking_reason=BlockingReason.NO_PRICE,
                detail={
                    "current_price": ctx.price,
                    "note": "缺失有效价格 (NO_PRICE): 状态与当前持仓权重保持不变",
                },
            )

        # Gate 2 computes these for gates 6/8 as well; pre-seeded so a non-held
        # state never leaves them unbound (only held states re-assign them).
        can_sell = True
        loss_pct = 0.0
        trailing_stop = -float("inf")
        is_stop = False

        # ---- gate 2: stop / emergency fast path (held states only) --------
        if st.state in (
            PositionState.HELD_GESTATION,
            PositionState.HELD_ACTIVE,
            PositionState.PENDING_EXIT,
        ):
            st.held_bars += 1
            st.peak_price = max(st.peak_price, ctx.price)
            can_sell = (ctx.market != "china") or (st.held_bars >= cfg.min_holding_bars)
            loss_pct = (ctx.price / st.entry_price - 1.0) if st.entry_price > 0 else 0.0
            trailing_stop = (
                (st.peak_price - cfg.atr_multiplier * ctx.atr)
                if (ctx.atr is not None and ctx.atr > 0)
                else -float("inf")
            )
            is_stop = (
                (loss_pct <= -cfg.hard_stop_loss_pct)
                or (ctx.atr is not None and ctx.atr > 0 and ctx.price < trailing_stop)
                or ctx.is_emergency_halt
            )

            if is_stop:
                # The kernel's risk-exit fast path is ALWAYS on: the legacy
                # `enable_exit_fast_path` toggle was a live-runner flag, and a
                # disabled tail-stop would be a different policy, not a config
                # of this one. M4.3 maps the legacy flag at the adapter.
                if not can_sell:
                    _LOGGER.warning(
                        "tenure lock overridden by risk exit for %s: held_bars=%d < min_holding_bars=%d "
                        "(stop-loss / emergency-halt liquidation must never be blocked by the anti-whipsaw lock)",
                        st.symbol,
                        st.held_bars,
                        cfg.min_holding_bars,
                    )
                st.state = PositionState.COOLDOWN
                st.cooldown_remaining = cfg.asset_stop_cooldown_bars
                stop_reason = "触发风控离场"
                if ctx.is_emergency_halt:
                    st.last_exit_type = ExitType.CIRCUIT_BREAKER.value
                    stop_reason = "触发账户熔断保护强制平仓"
                elif loss_pct <= -cfg.hard_stop_loss_pct:
                    st.last_exit_type = ExitType.HARD_STOP.value
                    stop_reason = f"触发个股 -{cfg.hard_stop_loss_pct:.0%} 硬止损风控 (当前价格 {ctx.price:.2f} 较成本 {st.entry_price:.2f} 跌幅 {loss_pct:.1%} <= -{cfg.hard_stop_loss_pct:.1%})"
                elif ctx.atr is not None and ctx.atr > 0 and ctx.price < trailing_stop:
                    st.last_exit_type = ExitType.TRAILING_STOP.value
                    stop_reason = f"触发个股 ATR 跟踪止损风控 (当前价格 {ctx.price:.2f} < 跟踪止损价 {trailing_stop:.2f})"

                # Retain the exit PRINT for the opt-in re-entry gate (gate 3b).
                # Pure additive state write: no other behaviour of the stop path
                # changes, and with `price_reentry_gate` False nothing reads it.
                st.last_exit_price = float(ctx.price)

                detail = {
                    "loss_pct": loss_pct,
                    "current_price": ctx.price,
                    "entry_price": st.entry_price,
                    "peak_price": st.peak_price,
                    "atr": ctx.atr,
                    "trailing_stop": trailing_stop,
                    "is_emergency_halt": ctx.is_emergency_halt,
                    "last_exit_type": st.last_exit_type,
                    "fast_path_triggered": "STOP_LOSS",
                }
                st.held_bars = 0
                st.entry_streak = 0
                st.exit_streak = 0
                st.entry_price = 0.0
                st.peak_price = 0.0
                st.current_weight = 0.0
                return _decision(
                    PolicyAction.SELL,
                    0.0,
                    stop_reason,
                    state_before=state_before,
                    state=st,
                    detail=detail,
                )

            # ---- gate 2b: two-day structural breakdown (OPT-IN, default OFF) --
            # Placed BELOW the stop/emergency fast path (a bounded hard stop must
            # never wait a second day) and ABOVE the cooldown / entry / exit /
            # inertia / tenure gates. While `two_day_breakdown_rule` is False
            # this branch is not entered at all, so the documented gate order and
            # every existing decision are unchanged (strict no-op).
            if cfg.two_day_breakdown_rule:
                breakdown_decision = self._two_day_breakdown_step(
                    st,
                    ctx,
                    cfg,
                    state_before=state_before,
                    can_sell=can_sell,
                )
                if breakdown_decision is not None:
                    return breakdown_decision

        # ---- gate 3: cooldown gate (blocks entry only) --------------------
        if st.cooldown_remaining > 0:
            st.cooldown_remaining -= 1
            if st.cooldown_remaining > 0:
                return _decision(
                    PolicyAction.COOLDOWN_BLOCKED,
                    0.0,
                    f"平仓后处于再入场冷却期 (离场冷却中，剩余 {st.cooldown_remaining} 日，防追高洗盘)",
                    state_before=state_before,
                    state=st,
                    blocking_reason=BlockingReason.COOLDOWN_BLOCKED,
                    detail={
                        "cooldown_remaining": st.cooldown_remaining,
                        "last_exit_type": st.last_exit_type,
                    },
                )
            st.state = PositionState.FLAT
            st.entry_streak = 0
            st.exit_streak = 0

            # ---- gate 3b: price/VWAP re-entry confirmation (OPT-IN, default OFF)
            # Evaluated ONLY on the cooldown-EXPIRY bar -- the single bar gate 3
            # owns, and the bar the asset would otherwise become eligible to
            # re-enter. A block is a ONE-BAR veto: nothing persists it, so the
            # next bar skips gate 3 and walks the normal entry ladder unchecked.
            # While `price_reentry_gate` is False this branch is not
            # entered at all, so the ladder is byte-for-byte the pre-change
            # ladder (strict no-op, not "usually off").
            if cfg.price_reentry_gate:
                reentry_block = self._reentry_gate_block(
                    st,
                    ctx,
                    cfg,
                    state_before=state_before,
                )
                if reentry_block is not None:
                    return reentry_block

        # ---- gates 4/5: entry side (FLAT / PENDING_ENTRY only) -----------
        if st.state in (PositionState.FLAT, PositionState.PENDING_ENTRY):
            entry_gate = cfg.entry_threshold if cfg.entry_threshold is not None else cfg.inertia_threshold

            # gate 4: minimum entry weight
            if raw_target_weight > 0.0 and raw_target_weight < entry_gate:
                st.entry_streak = 0
                st.exit_streak = 0
                st.state = PositionState.FLAT
                return _decision(
                    PolicyAction.HOLD_FILTERED,
                    0.0,
                    f"目标仓位未达最低建仓/调仓门槛 (目标 {raw_target_weight:.1%} < 门槛 {entry_gate:.0%}，防微量试错损耗)",
                    state_before=state_before,
                    state=st,
                    blocking_reason=BlockingReason.MIN_ENTRY_THRESHOLD,
                    detail={
                        "delta_w": raw_target_weight,
                        "inertia_threshold": entry_gate,
                        "raw_target_weight": raw_target_weight,
                        "current_weight": 0.0,
                    },
                )

            if raw_target_weight > 0.0:
                # gate 5: entry confirmation, with the volume/breadth fast path.
                # The kernel keeps the LIVE rule (always enabled); the legacy
                # `enable_entry_fast_path` flag is an adapter concern.
                is_vol = bool(
                    ctx.volume is not None
                    and ctx.ma20_volume is not None
                    and ctx.ma20_volume > 0
                    and (ctx.volume / ctx.ma20_volume) >= cfg.volume_surge_multiplier
                )
                is_breadth = bool(
                    ctx.market_breadth is not None
                    and ctx.market_breadth >= cfg.breadth_thrust_threshold
                )
                # Strict confluence: macro thrust (breadth) AND asset volume surge,
                # or an extreme standalone volume breakout (>= 2.5x MA20).
                is_thrust_confluence = is_breadth and is_vol
                is_super_volume_surge = bool(
                    ctx.volume is not None
                    and ctx.ma20_volume is not None
                    and ctx.ma20_volume > 0
                    and (ctx.volume / ctx.ma20_volume) >= 2.5
                )
                if is_thrust_confluence or is_super_volume_surge:
                    st.state = PositionState.HELD_GESTATION
                    st.held_bars = 0
                    st.entry_streak = cfg.guard_latency_bars
                    st.exit_streak = 0
                    st.entry_price = ctx.price
                    st.peak_price = ctx.price
                    st.current_weight = raw_target_weight
                    vol_ratio = (
                        (ctx.volume / ctx.ma20_volume)
                        if (ctx.volume and ctx.ma20_volume and ctx.ma20_volume > 0)
                        else None
                    )
                    fast_reason = (
                        f"宏观冲力爆量突破 (冲力生效且成交量放大 {vol_ratio:.1f}x)，豁免确认期即刻买入"
                        if vol_ratio is not None
                        else "放量突破冲力闪电建仓"
                    )
                    return _decision(
                        PolicyAction.BUY,
                        raw_target_weight,
                        fast_reason,
                        state_before=state_before,
                        state=st,
                        detail={
                            "vol_ratio": vol_ratio,
                            "volume_surge_multiplier": cfg.volume_surge_multiplier,
                            "market_breadth": ctx.market_breadth,
                            "breadth_thrust_threshold": cfg.breadth_thrust_threshold,
                            "fast_path_triggered": "VOLUME_BREAKOUT",
                        },
                    )

                st.entry_streak += 1
                if st.entry_streak >= cfg.guard_latency_bars:
                    st.state = PositionState.HELD_GESTATION
                    st.held_bars = 0
                    st.exit_streak = 0
                    st.entry_price = ctx.price
                    st.peak_price = ctx.price
                    st.current_weight = raw_target_weight
                    return _decision(
                        PolicyAction.BUY,
                        raw_target_weight,
                        f"开仓/再入场信号持续 {st.entry_streak} 日确认生效",
                        state_before=state_before,
                        state=st,
                        detail={
                            "entry_streak": st.entry_streak,
                            "guard_latency_bars": cfg.guard_latency_bars,
                            "target_weight": raw_target_weight,
                        },
                    )
                st.state = PositionState.PENDING_ENTRY
                return _decision(
                    PolicyAction.HOLD_FILTERED,
                    0.0,
                    f"开仓/再入场信号进入确认期 (第 {st.entry_streak}/{cfg.guard_latency_bars} 日待确认，防脉冲假突破)",
                    state_before=state_before,
                    state=st,
                    blocking_reason=BlockingReason.PENDING_ENTRY_CONFIRM,
                    detail={
                        "entry_streak": st.entry_streak,
                        "guard_latency_bars": cfg.guard_latency_bars,
                    },
                )

            # Raw target is not positive: either an aborted confirmation window or
            # a permanently flat symbol. The economics are identical (both streaks
            # zeroed, state FLAT) but the labels differ, so the ticket can record
            # a CANCELLED entry instead of a silent, trace-free disappearance.
            was_pending_entry = state_before == PositionState.PENDING_ENTRY
            cancelled_streak = int(st.entry_streak) if was_pending_entry else 0
            st.entry_streak = 0
            st.exit_streak = 0
            st.state = PositionState.FLAT
            if was_pending_entry:
                return _decision(
                    PolicyAction.CANCEL_PENDING,
                    0.0,
                    "待确认开仓信号消失，取消入场",
                    state_before=state_before,
                    state=st,
                    detail={
                        "raw_target_weight": raw_target_weight,
                        "cancelled_entry_streak": cancelled_streak,
                        "guard_latency_bars": cfg.guard_latency_bars,
                    },
                )
            return _decision(
                PolicyAction.HOLD,
                0.0,
                "空仓维持",
                state_before=state_before,
                state=st,
                detail={"raw_target_weight": raw_target_weight},
            )

        # Reached only by a state no gate above handles (`COOLDOWN` whose counter
        # already expired without resetting, i.e. a corrupted/never-hydrated
        # state). Fail loud, exactly like the reference.
        if st.state not in (
            PositionState.HELD_GESTATION,
            PositionState.HELD_ACTIVE,
            PositionState.PENDING_EXIT,
        ):
            raise ValueError(f"Unhandled position lifecycle state: {st.state}")

        # ---- gate 6: exit-confirmation streak (raw request is exactly zero) --
        if _is_zero_request(raw_target_weight):
            if can_sell:
                st.exit_streak += 1
                if st.exit_streak >= cfg.guard_latency_bars:
                    streak_count = st.exit_streak
                    st.state = PositionState.COOLDOWN
                    st.cooldown_remaining = cfg.normal_cooldown_bars
                    st.last_exit_type = ExitType.NORMAL.value
                    # Retain the exit PRINT for the opt-in re-entry gate (gate
                    # 3b). Additive state write only -- the decision below is
                    # unchanged, and nothing reads the field while the gate is OFF.
                    st.last_exit_price = float(ctx.price)
                    detail = {
                        "exit_streak": streak_count,
                        "guard_latency_bars": cfg.guard_latency_bars,
                        "last_exit_type": st.last_exit_type,
                    }
                    st.held_bars = 0
                    st.entry_streak = 0
                    st.exit_streak = 0
                    st.entry_price = 0.0
                    st.peak_price = 0.0
                    st.current_weight = 0.0
                    return _decision(
                        PolicyAction.SELL,
                        0.0,
                        f"策略模型平仓信号持续 {streak_count} 日确认生效",
                        state_before=state_before,
                        state=st,
                        detail=detail,
                    )
                st.state = PositionState.PENDING_EXIT
                return _decision(
                    PolicyAction.HOLD_FILTERED,
                    float(st.current_weight),
                    f"平仓信号进入确认期 (第 {st.exit_streak}/{cfg.guard_latency_bars} 日待确认，防单日脉冲假平仓)",
                    state_before=state_before,
                    state=st,
                    blocking_reason=BlockingReason.EXIT_CONFIRMING,
                    detail={
                        "exit_streak": st.exit_streak,
                        "guard_latency_bars": cfg.guard_latency_bars,
                    },
                )
            return _decision(
                PolicyAction.HOLD_FILTERED,
                float(st.current_weight),
                f"未满最低持仓周期 (持仓 {st.held_bars} 日 < 最低 {cfg.min_holding_bars} 日，锁定防洗盘)",
                state_before=state_before,
                state=st,
                blocking_reason=BlockingReason.TENURE_LOCKED,
                detail={
                    "held_bars": st.held_bars,
                    "min_holding_bars": cfg.min_holding_bars,
                    "blocked_action": "full_exit",
                },
            )

        st.exit_streak = 0
        if st.held_bars >= cfg.min_holding_bars:
            st.state = PositionState.HELD_ACTIVE

        # Signed delta, unified with the signed `inertia_check.delta_weight`
        # convention the live runner emits; only the ABSOLUTE value gates inertia.
        delta_w = raw_target_weight - st.current_weight

        # Defense in depth: portfolio deleveraging only ever excuses a TRIM of an
        # existing holding. A buy, or a target above the current weight, cannot
        # claim the exemption.
        deleverage = bool(ctx.is_portfolio_deleverage)
        if deleverage and not (delta_w < 0.0 and 0.0 <= raw_target_weight <= st.current_weight):
            deleverage = False

        # ---- gate 7: inertia ---------------------------------------------
        # The relative trim hurdle: a SELL must clear BOTH the absolute band and
        # a proportional fraction of the incumbent weight (micro-trims that round
        # to a handful of shares are not real orders). BUY-direction rebalances
        # keep the absolute-only band, so rebalancing UP is never over-restricted.
        effective_threshold = cfg.inertia_threshold
        if delta_w < 0.0 and cfg.inertia_relative_ratio > 0.0:
            effective_threshold = max(
                cfg.inertia_threshold,
                cfg.inertia_relative_ratio * st.current_weight,
            )
        if not deleverage and abs(delta_w) < effective_threshold:
            return _decision(
                PolicyAction.HOLD,
                float(st.current_weight),
                f"调仓未超惰性阈值 (< {effective_threshold:.0%})",
                state_before=state_before,
                state=st,
                blocking_reason=BlockingReason.INERTIA,
                detail={
                    "delta_w": delta_w,
                    "inertia_threshold": cfg.inertia_threshold,
                    "effective_threshold": effective_threshold,
                    "inertia_relative_ratio": cfg.inertia_relative_ratio,
                    "raw_target_weight": raw_target_weight,
                    "current_weight": st.current_weight,
                },
            )

        action = PolicyAction.BUY if raw_target_weight > st.current_weight else PolicyAction.SELL

        # ---- gate 8: tenure lock (SELL direction only) --------------------
        if action == PolicyAction.SELL and not can_sell and not deleverage:
            # A partial trim is an exit-direction order and must respect the same
            # anti-whipsaw lock as a full exit (the 000999.SZ / 600900.SH
            # incident): the lock has never been enforced on this branch before.
            return _decision(
                PolicyAction.HOLD_FILTERED,
                float(st.current_weight),
                f"未满最低持仓周期 (持仓 {st.held_bars} 日 < 最低 {cfg.min_holding_bars} 日，锁定防洗盘)",
                state_before=state_before,
                state=st,
                blocking_reason=BlockingReason.TENURE_LOCKED,
                detail={
                    "held_bars": st.held_bars,
                    "min_holding_bars": cfg.min_holding_bars,
                    "raw_target_weight": raw_target_weight,
                    "current_weight": st.current_weight,
                    "delta_w": delta_w,
                    "blocked_action": "partial_trim",
                },
            )

        # Commit. `prev_weight` is snapshotted BEFORE the mutation so the forensic
        # record reads current_weight = pre-trade weight, raw_target_weight =
        # target, delta_w = target - pre-trade -- never a self-contradictory row
        # (the 600660.SH ticket had current_weight == raw_target_weight next to a
        # non-zero delta_w).
        prev_weight = st.current_weight
        st.current_weight = raw_target_weight
        detail_dict = {
            "delta_w": delta_w,
            "inertia_threshold": cfg.inertia_threshold,
            "raw_target_weight": raw_target_weight,
            "current_weight": prev_weight,
        }
        if deleverage:
            detail_dict["is_portfolio_deleverage"] = True
        return _decision(
            action,
            raw_target_weight,
            "组合级去杠杆风控减仓" if deleverage else "最优权重微调",
            state_before=state_before,
            state=st,
            detail=detail_dict,
        )

    def _two_day_breakdown_step(
        self,
        st: AssetState,
        ctx: PolicyContext,
        cfg: PositionPolicyConfig,
        *,
        state_before: PositionState,
        can_sell: bool,
    ) -> Optional[Decision]:
        """Gate 2b: the opt-in two-day structural-breakdown confirmation.

        `docs/development_plan.md` M1.1 item 4 / design §P0-1: a close below the
        structural floor -- `min(MA20, VAL)`, or `MA20` alone when `VAL` is not
        supplied -- is NOT an exit. It is one counted breakdown bar; only the
        second counted bar liquidates (`PENDING_EXIT` in between). The two-day
        window is what kills 单日假破位 (a one-bar spike through the floor that
        recovers the next day), which is one of the three real loss sources named
        in `docs/long_only_multi_asset_gap_analysis.md` §5.

        Contract:
          * `breakdown_streak` is this rule's OWN counter. `exit_streak` belongs
            to the model-signal exit gate (`guard_latency_bars`) and is never
            reused here -- reusing it would let one broken-structure day plus one
            model-exit day add up to a "confirmed" exit.
          * missing data degrades to a NO-OP, never to a fabricated break:
            without a finite positive `ma20_close` the bar is not evaluated at
            all (streak untouched -- a data gap neither confirms nor clears);
            `val` is optional and only ever LOWERS the floor; `vol_ma20`/`volume`
            being absent can never claim the 缩量 exemption (so the bar counts,
            the conservative direction).
          * a close back at/above the floor RESETS the streak and falls through
            to the pre-existing ladder unchanged -- 假破位反弹 must never sell.
          * a 缩量回踩 bar is a pure no-op: not counted, and it does not block
            the orthogonal gates below (a blocked bar could otherwise suppress a
            model exit indefinitely -- see the branch comment).
          * the SELL path returns `target_weight=0.0` through `_decision`, i.e.
            exactly the construction gate 2 / gate 6 use, so `SparseProjector`
            writes a literal `0.0` (an explicit close), never a missing row,
            never a NaN (`AGENTS.md` sparse-weights contract).
          * the confirmed breakdown is a RISK exit, so -- like the stop fast path
            -- it is not tenure-lockable: the two-day confirmation is itself the
            anti-whipsaw delay. A tenure override is logged, never silent.

        Returns `None` when the bar is not a (counted) breakdown, meaning "carry
        on with the normal gate ladder".
        """
        ma20 = ctx.ma20_close
        if ma20 is None or not np.isfinite(ma20) or float(ma20) <= 0.0:
            # No MA20 anchor -> the rule cannot evaluate this bar. `None` must
            # never be read as 0 (that would make every close a "breakdown").
            return None

        floor = float(ma20)
        if ctx.val is not None and np.isfinite(ctx.val) and float(ctx.val) > 0.0:
            floor = min(floor, float(ctx.val))

        detail: Dict[str, Any] = {
            "current_price": float(ctx.price),
            "ma20_close": float(ma20),
            "val": None if ctx.val is None else float(ctx.val),
            "breakdown_floor": floor,
            "breakdown_days": int(cfg.breakdown_days),
        }

        if not (float(ctx.price) < floor):
            # 假破位反弹: the close is back at/above the floor. Clear the pending
            # confirmation and let the existing gates decide, exactly as they
            # would have without this rule.
            st.breakdown_streak = 0
            return None

        # 缩量回踩 exemption. Asymmetry is deliberate: a MISSING volume or
        # vol_ma20 cannot claim the exemption, so the bar counts.
        vol = ctx.volume
        vol_ma = ctx.vol_ma20
        is_volume_shrink = bool(
            vol is not None
            and vol_ma is not None
            and np.isfinite(vol)
            and np.isfinite(vol_ma)
            and float(vol_ma) > 0.0
            and float(vol) < float(cfg.breakdown_vol_shrink_pct) * float(vol_ma)
        )
        if is_volume_shrink:
            # 缩量回踩 is a NO-OP for this rule: the bar is not counted, and --
            # deliberately -- it also does not return a `HOLD_FILTERED` row.
            # Blocking the bar here would suppress the ORTHOGONAL gates below
            # (the model-exit confirmation `exit_streak` / the inertia trim), and
            # a low-volume bar parked below the floor could then hold a position
            # the model already exited indefinitely -- the silent-position-
            # retention bug class this repo forbids. The rule is exempt, so it
            # stays out of the way and the pre-existing ladder decides.
            return None

        st.breakdown_streak += 1
        st.state = PositionState.PENDING_EXIT
        detail.update(
            {
                "breakdown_streak": int(st.breakdown_streak),
                "volume": None if vol is None else float(vol),
                "vol_ma20": None if vol_ma is None else float(vol_ma),
                "breakdown_vol_shrink_pct": float(cfg.breakdown_vol_shrink_pct),
            }
        )

        if st.breakdown_streak >= int(cfg.breakdown_days):
            confirmed_streak = st.breakdown_streak
            if not can_sell:
                _LOGGER.warning(
                    "tenure lock overridden by two-day breakdown exit for %s: "
                    "held_bars=%d < min_holding_bars=%d (a confirmed structural break is a "
                    "risk exit; the two-day confirmation is its anti-whipsaw delay, and the "
                    "T+1 lock must not extend a broken position's tail loss)",
                    st.symbol,
                    st.held_bars,
                    cfg.min_holding_bars,
                )
            st.state = PositionState.COOLDOWN
            # Cooldown choice is DELIBERATE and asymmetric vs. the risk exit
            # above (`asset_stop_cooldown_bars`). The two-day confirmation window
            # IS this rule's anti-whipsaw delay: the position only exits after
            # `breakdown_days` consecutive closes below min(MA20, VAL), so the
            # re-entry gate does not additionally charge the longer stop-loss
            # cooling period for a signal that was already slow to fire. Stated
            # consequence, so nobody "fixes" it by accident: a broken-down
            # symbol can be re-admitted SOONER (`normal_cooldown_bars`, default
            # 2) than a hard-stop / trailing-stop / emergency-halt casualty
            # (`asset_stop_cooldown_bars`, default max(5, normal)) -- the stop
            # path fires on a single bar and therefore pays the longer penalty.
            st.cooldown_remaining = cfg.normal_cooldown_bars
            st.last_exit_type = ExitType.BREAKDOWN.value
            st.last_exit_price = float(ctx.price)
            detail.update(
                {
                    "confirmed_breakdown_streak": int(confirmed_streak),
                    "last_exit_type": st.last_exit_type,
                    "last_exit_price": st.last_exit_price,
                }
            )
            st.held_bars = 0
            st.entry_streak = 0
            # Hygiene, NOT reuse: every other SELL path zeroes the signal-exit
            # counter so a later holding period cannot inherit a stale
            # confirmation. `exit_streak` is never incremented by this rule.
            st.exit_streak = 0
            st.breakdown_streak = 0
            st.entry_price = 0.0
            st.peak_price = 0.0
            st.current_weight = 0.0
            return _decision(
                PolicyAction.SELL,
                0.0,
                f"连续 {confirmed_streak} 日收于结构下轨 (价格 {ctx.price:.2f} < "
                f"min(MA20, VAL) = {floor:.2f})，破位确认生效，清仓离场",
                state_before=state_before,
                state=st,
                detail=detail,
            )

        return _decision(
            PolicyAction.HOLD_FILTERED,
            float(st.current_weight),
            f"结构破位进入确认期 (第 {st.breakdown_streak}/{cfg.breakdown_days} 日待确认，"
            f"价格 {ctx.price:.2f} < min(MA20, VAL) = {floor:.2f}，防单日假破位)",
            state_before=state_before,
            state=st,
            blocking_reason=BlockingReason.EXIT_CONFIRMING,
            detail=detail,
        )

    def _reentry_gate_block(
        self,
        st: AssetState,
        ctx: PolicyContext,
        cfg: PositionPolicyConfig,
        *,
        state_before: PositionState,
    ) -> Optional[Decision]:
        """Gate 3b: the opt-in price/VWAP re-entry confirmation.

        `docs/development_plan.md` M1.1 / `docs/long_only_multi_asset_gap_analysis.md`
        §P0-1: after a cooldown expires the asset must not be re-entered straight
        into the same falling tape it was just sold into. Two INDEPENDENT legs,
        either of which admits the entry:

          * PRICE leg: `price > last_exit_price * (1 + price_reentry_pct)` -- the
            position has rebounded off the exit print by the configured margin;
          * VWAP leg: `price > vwap20` AND `volume > vol_ma20` -- a confirmed
            reclaim of the 20-day VWAP on expanding volume (a genuine bid, not a
            drifting bounce). Both volume inputs must be present: a missing
            `vol_ma20` cannot claim "expanding volume", so the leg does not pass.

        DEGRADE, NEVER DEADLOCK (the contract that matters most here): an
        unusable exit print -- `last_exit_price <= 0`, non-finite, i.e. unknown
        because no exit wrote it -- makes the PRICE leg unevaluable, and an
        unevaluable leg must never veto the entry. The gate then ALLOWS with a
        warning, so a symbol can never be locked out of re-entering forever by a
        missing field. (The VWAP leg cannot rescue a block either: if there is no
        `vwap20` AND no usable exit print there is no evidence at all, and the
        gate allows rather than inventing one.) Note the asymmetry that is
        deliberate: a PRESENT and FAILING price leg plus a missing `vwap20` is a
        BLOCK, because the rebound evidence genuinely says "not yet".

        Returns `None` to mean "the entry may proceed, carry on with the normal
        ladder"; a `Decision` to short-circuit it. The blocked row is
        `HOLD_FILTERED` at `target_weight=0.0` with the asset kept `FLAT`: in the
        sparse frame that is a MISSING row (`SparseProjector.write` returns
        `None` when the target equals the carried 0.0), never an explicit `0.0`
        -- an explicit `0.0` is reserved for an actual exit. `last_exit_price` is
        NOT reset, so the decision stays auditable and comparable.

        WHAT A BLOCK ACTUALLY COSTS -- A ONE-BAR VETO, NOT A PERSISTENT GATED
        STATE (read this before "fixing" the apparent leak): this gate runs
        ONLY on the cooldown-EXPIRY bar, because that is the single bar gate 3
        owns. Gate 3 decrements `cooldown_remaining` to 0, sets
        `state = FLAT`, and calls this method exactly once, on that bar. A block
        returns `HOLD_FILTERED` for THIS bar and nothing re-arms the counter or
        flags the symbol as gated -- so on the NEXT bar gate 3 is skipped
        entirely (`cooldown_remaining == 0`), this method is never consulted,
        and the asset walks the normal entry ladder (gate 4 threshold, gate 5
        entry-confirmation streak) as if the exit price had never been checked.
        The confirmation is therefore a ONE-BAR DEFERRAL of the re-entry, not a
        sustained veto: the gate cannot hold a symbol out of the market for more
        than a single bar, and it deliberately does not reintroduce the
        "lock a symbol out forever" failure the degrade-to-allow rule above
        exists to prevent. If a future change makes this a multi-bar gate, it
        must persist the block in `AssetState` explicitly -- do not assume this
        method is re-asked.
        """
        last_exit = float(st.last_exit_price)
        price = float(ctx.price)
        pct = float(cfg.price_reentry_pct)
        required_price = last_exit * (1.0 + pct)

        vwap20 = ctx.vwap20
        volume = ctx.volume
        vol_ma20 = ctx.vol_ma20
        has_vwap20 = bool(
            vwap20 is not None and np.isfinite(vwap20) and float(vwap20) > 0.0
        )
        vwap_leg_pass = bool(
            has_vwap20
            and price > float(vwap20)
            and volume is not None
            and vol_ma20 is not None
            and np.isfinite(volume)
            and np.isfinite(vol_ma20)
            and float(volume) > float(vol_ma20)
        )

        if not (np.isfinite(last_exit) and last_exit > 0.0):
            # Unknown exit print -> the price leg cannot be evaluated. Allowing is
            # the only safe direction: blocking would permanently lock the symbol
            # out of ever re-entering (`AssetState.last_exit_price` defaults 0.0).
            _LOGGER.warning(
                "price_reentry_gate: %s has no usable last_exit_price (%r); allowing the "
                "re-entry (an unknown exit print must never block a symbol forever). "
                "price=%r vwap20=%r",
                st.symbol,
                st.last_exit_price,
                ctx.price,
                ctx.vwap20,
            )
            return None

        if price > required_price or vwap_leg_pass:
            return None

        detail: Dict[str, Any] = {
            "current_price": price,
            "last_exit_price": last_exit,
            "required_price": required_price,
            "price_reentry_pct": pct,
            "vwap20": None if vwap20 is None else float(vwap20),
            "volume": None if volume is None else float(volume),
            "vol_ma20": None if vol_ma20 is None else float(vol_ma20),
            "last_exit_type": st.last_exit_type,
            "vwap_leg_evaluable": has_vwap20,
            "vwap_leg_passed": vwap_leg_pass,
            "note": "再入场价格/VWAP 未确认：保持空仓 (NOT an exit, NOT a 0.0 row)",
        }
        return _decision(
            PolicyAction.HOLD_FILTERED,
            0.0,
            f"冷却期满但再入场确认未通过 (价格 {price:.2f} 未高于离场价 "
            f"{last_exit:.2f}×(1+{pct:.1%}) = {required_price:.2f}，且未同时满足 "
            f"价格>VWAP20 与 成交量>vol_ma20)，保持空仓不追高",
            state_before=state_before,
            state=st,
            blocking_reason=BlockingReason.REENTRY_GATED,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Layer 2 -- cross-asset sequencer
# ---------------------------------------------------------------------------


class PortfolioSequencer:
    """The cross-asset half of the inertia filter -- the part a per-symbol
    decision provably cannot express (design §4).

    Why this layer must exist: "sells release cash first, buys are capped by the
    cash that was just released, the residual goes to the cash proxy" is a joint
    constraint over the whole row. `AssetPolicy.inertia_step` can say "this
    symbol wants to buy", but only the row-level object knows how much cash is
    actually available and which buyer gets it first.

    It owns exactly one piece of state, and only for the duration of one call:
    the running `avail_inc` scalar. `holding_age` / `last_buy_bar` are
    caller-owned dicts handed in and mutated in place, so the kernel itself
    holds no mutable global state.
    """

    def __init__(self, policy: Optional[AssetPolicy] = None) -> None:
        self._policy = policy if policy is not None else AssetPolicy()

    def sequence(
        self,
        ideal_w: pd.Series,
        current_w: pd.Series,
        *,
        ctx: PolicyContext,
        risky_symbols: List[str],
        symbols: List[str],
        thresh_t: pd.Series,
        holding_age: Dict[str, int],
        last_buy_bar: Dict[str, int],
        cfg: PositionPolicyConfig,
        cash_proxy: Optional[str] = "CASH",
        em_row: Optional[Union[bool, pd.Series]] = None,
    ) -> pd.Series:
        """Advance one bar and return the committed weight row.

        `ctx.bar_index` is the bar clock handed down to every
        `AssetPolicy.inertia_step` (and written into `last_buy_bar` on a
        committed buy); it is the reference's `t`.

        Transcription of `_step_asset_inertia`'s cross-asset steps, in the exact
        reference order:

          1. classify the emergency mask (a `Series` row is per-symbol; a scalar
             is global; an all-True row is a global emergency);
          2. ask `AssetPolicy.inertia_step` for every risky symbol -- this also
             advances each symbol's tenure clock, which MUST happen before the
             global-emergency short-circuit below. A symbol whose `ideal_w` or
             `current_w` cell is NaN is NOT asked (a `Decision` may not carry a
             NaN `target_weight`); its cell is carried through untouched and its
             tenure clock is advanced directly, which is what the reference's
             all-NaN-comparisons-False vectorized pass does;
          3. on a global emergency, return `ideal_w.copy()` UNMODIFIED: no tenure
             filtering, no buy/sell gating, and no cash residual write;
          4. otherwise, if no symbol wants to trade, return `current_w.copy()`
             untouched (again: no cash residual write -- a residual written on a
             no-trade bar would fabricate a rebalance row);
          5. apply every sell first (`new_w[s] = ideal_w[s]`, and a sell that
             lands at/below `1e-4` resets `last_buy_bar[s] = -999` so the next
             re-entry starts a fresh tenure clock);
          6. recompute the freed cash as `1 - sum(risky)` AFTER the sells;
          7. apply buys in `(-diff, symbol)` order -- biggest desired increase
             first, symbol name as a deterministic tie-break -- each capped by
             the remaining free cash, and a buy that cannot clear its own
             threshold writes the old weight back explicitly;
          8. route the residual `1 - sum(risky)` into `cash_proxy`, only when that
             column actually exists.
        """
        # (2) emergency classification is joint: "all symbols halted" is a
        # property of the row, not of any single symbol.
        bar_index = int(ctx.bar_index)
        if em_row is not None:
            if isinstance(em_row, pd.Series):
                symbol_emergency = {s: bool(em_row.get(s, False)) for s in risky_symbols}
                is_global_emergency = all(symbol_emergency.values()) if symbol_emergency else False
            else:
                is_global_emergency = bool(em_row)
                symbol_emergency = {s: is_global_emergency for s in risky_symbols}
        else:
            is_global_emergency = False
            symbol_emergency = {s: False for s in risky_symbols}

        decisions: Dict[str, Decision] = {}
        for s in risky_symbols:
            current = float(current_w[s])
            ideal = float(ideal_w[s])
            if not (np.isfinite(current) and np.isfinite(ideal)):
                # (2a) NaN PASSTHROUGH -- filtered HERE, at the panel layer, so
                # that no `Decision` is ever built from a NaN cell.
                #
                # `_step_asset_inertia` propagates a NaN cell silently: every
                # comparison against NaN is False, so the symbol can never be
                # in `sells`/`buys`, its cell is never written, and it survives
                # verbatim (`filtered.iloc[t] = current_w`). `inertia_step`
                # CANNOT express that, because `Decision.__post_init__`
                # rightfully raises on a NaN `target_weight` -- that invariant
                # guards the live ticket path and must not be weakened, so the
                # NaN is filtered out before the `Decision` exists instead.
                #
                # Two reachable shapes (both raise `ValueError` if they reach
                # `inertia_step`): a NaN `current_w[s]` (row 0 of a late-listing
                # symbol with no history yet, carried forward) hits the
                # non-trading fall-through's `target_weight=current`, and a NaN
                # `ideal_w[s]` on a global-emergency bar hits the pass-through's
                # `target_weight=ideal`. Converting either to `0.0` would be a
                # silent liquidation (the sparse contract: NaN = no
                # instruction), so the cell MUST come out NaN.
                #
                # Only the tenure clock moves, exactly as the reference's
                # pre-loop does (`current_w[s] > 1e-4` is False for NaN -> reset
                # to 0; `last_buy_bar[s]` is never touched on a non-trading bar).
                holding_age[s] = (holding_age[s] + 1) if current > 1e-4 else 0
                continue
            decisions[s] = self._policy.inertia_step(
                s,
                ideal_w,
                current_w,
                float(thresh_t[s]),
                cfg,
                ctx=ctx,
                holding_age=holding_age,
                last_buy_bar=last_buy_bar,
                symbol_emergency=symbol_emergency[s],
                is_global_emergency=is_global_emergency,
            )

        # (3) Pass-through of the ideal row, byte-for-byte (NaN cells included).
        if is_global_emergency:
            return ideal_w.copy()

        # `decisions` is keyed in `risky_symbols` order minus the NaN symbols
        # (2a), which is exactly the reference's `diff[...].index` order.
        sells = [s for s in decisions if decisions[s].detail["in_sells"]]
        buys = [s for s in decisions if decisions[s].detail["in_buys"]]

        # (4) Nothing to do: return an untouched copy (no cash residual write).
        if not sells and not buys:
            return current_w.copy()

        new_w = current_w.copy()

        # (5) Sells first, to release purchasing power.
        for s in sells:
            new_w[s] = ideal_w[s]
            if new_w[s] <= 1e-4:
                last_buy_bar[s] = -999

        # (6) Purchasing power available to the buys of THIS bar.
        avail_inc = max(0.0, 1.0 - float(new_w[risky_symbols].sum()))

        # (7) Buys, priority order.
        buys_sorted = sorted(buys, key=lambda b: (-decisions[b].detail["diff"], b))
        for b in buys_sorted:
            desired_inc = max(0.0, float(ideal_w[b] - current_w[b]))
            actual_inc = min(desired_inc, avail_inc)

            if actual_inc >= float(decisions[b].detail["buy_thresh"]):
                new_w[b] = current_w[b] + actual_inc
                last_buy_bar[b] = bar_index
                avail_inc = max(0.0, avail_inc - actual_inc)
            else:
                # Explicit write-back of the OLD weight: a buy that could not be
                # funded must not inherit whatever a sell above wrote for it.
                new_w[b] = current_w[b]

        # (8) Residual to cash, only when the column exists.
        if cash_proxy in symbols:
            new_w[cash_proxy] = max(0.0, 1.0 - float(new_w[risky_symbols].sum()))

        return new_w


# ---------------------------------------------------------------------------
# Layer 3 -- sparse projection
# ---------------------------------------------------------------------------


class SparseProjector:
    """Dense daily targets -> the sparse-weights contract, and back.

    The sparse contract (repo-wide, see `common/allocation_templates.py` and the
    project `AGENTS.md`): a row is `NaN` on every day an asset was not
    instructed to rebalance, and holds the real target on a rebalance day. The
    backtester tells "an instruction was issued" apart from "nothing happened"
    by PRESENCE, not by comparing values, which is why the projection cannot be
    a `ffill`/`diff` and why a `0.0` must survive verbatim.
    """

    def project(self, daily: pd.DataFrame) -> pd.DataFrame:
        """Literal transcription of `_sparse_from_daily`'s compression step.

        `daily.where(changed)` yields `NaN` on every unchanged day and PRESERVES
        the real value on a changed day -- including the real `0.0` of an exit.
        That asymmetry is the whole point:

          * a SELL that lands on exactly `0.0` remains an explicit `0.0`, i.e.
            "close this position" (the backtester's cell-level contract: inside a
            rebalance row a `NaN` cell would mean "carry the previous weight",
            silently keeping a position the strategy already exited);
          * an untouched holding stays `NaN`, i.e. "no instruction", so the
            column-wise `ffill()` in the backtester carries it forward.

        The first row is forced `changed=True`: it is the initial allocation and
        must always be materialised, even when it happens to equal a zero-filled
        `shift(1)` row.
        """
        if daily.empty:
            return daily
        changed = (daily != daily.shift(1)).any(axis=1)
        changed.iloc[0] = True
        return daily.where(changed)

    def write(self, decision: Decision, previous_target: Optional[float]) -> Optional[float]:
        """The unique write-back rule (design §4 table).

        Returns the value to write for this symbol on this bar, or `None` to
        OMIT the row entirely (which, in a sparse frame, is how "no instruction"
        is spelled -- never `0.0`):

          * `CANCEL_PENDING` / `COOLDOWN_BLOCKED` -> `None`. Neither is a trade,
            and a never-held symbol must not get an explicit `0.0` row: that is
            the phantom-anchor shape that once armed a re-entry cooldown on a
            position that was never bought.
          * `SELL` with `target_weight == 0.0` -> explicit `0.0` (a real exit).
          * `BUY` / `SELL` whose target differs from the previous target -> that
            target.
          * any action whose target equals the previous target -> `None`
            (a redundant row would be a no-op rebalance that still costs
            turnover in the engine).
          * a non-trading action that nevertheless carries a CHANGED target ->
            the target (defensive only: the policy kernel never emits that
            contradiction, but silently dropping a changed target would corrupt
            the sparse frame if it ever did).

        `previous_target=None` means "no previous row for this symbol", treated
        as `0.0` so a never-held flat symbol stays omitted.
        """
        action = str(getattr(decision, "action", "") or "").strip().upper()
        target = float(getattr(decision, "target_weight", 0.0))
        prev = 0.0 if previous_target is None else float(previous_target)

        if action in (PolicyAction.CANCEL_PENDING, PolicyAction.COOLDOWN_BLOCKED):
            return None
        if action == PolicyAction.SELL and target == 0.0:
            return 0.0
        if target == prev:
            return None
        return target


# ---------------------------------------------------------------------------
# Equivalence wrapper -- the bit-identical door into the inertia path
# ---------------------------------------------------------------------------


def kernel_apply_asset_inertia(
    daily: pd.DataFrame,
    min_weight_change: Union[float, Dict[str, float], pd.Series, pd.DataFrame] = 0.02,
    cash_proxy: Optional[str] = "CASH",
    emergency_mask: Optional[Union[pd.Series, pd.DataFrame, bool]] = None,
    asymmetric_sells: bool = False,
    entry_threshold: Optional[float] = None,
    min_holding_bars: int = 0,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.DataFrame:
    """`apply_asset_inertia`, routed through the kernel. Cell-identical.

    Same signature, same defaults, same threshold-normalization preamble, same
    first-row seeding, same per-bar sequencing -- the body below is a
    transcription of `common/allocation_templates.py::apply_asset_inertia`
    (`:314-398`), with the per-bar work delegated to
    `PortfolioSequencer.sequence`. Callers must be able to swap one for the
    other and get the same frame, NaN positions included (`NaN` = no
    instruction, and it is never equivalent to `0.0`).

    THRESHOLD PREAMBLE (verbatim, including its order of checks):
      * `int`/`float`: `<= 0` with `min_holding_bars <= 0` returns `daily`
        untouched; otherwise a uniform Series;
      * `dict`: reindexed to the risky symbols, missing entries `0.02`;
      * `pd.Series`: reindexed the same way;
      * `pd.DataFrame`: per-date row, reindexed per bar (`0.02` default);
      * anything else: uniform `0.02`.

    ONE DELIBERATE SUPERSET: a plain `bool` `emergency_mask` is accepted as a
    constant global flag. The reference annotation only allows a Series or a
    DataFrame, and indexing a bare `bool` would raise `AttributeError`; for every
    input the reference accepts, this function returns the same cells. The
    constant-flag tolerance is what makes a "halt everything" call expressible
    without materialising a full Series.
    """
    if daily.empty:
        return daily

    symbols = list(daily.columns)
    risky_symbols = [s for s in symbols if s != cash_proxy]
    n_dates = len(daily)

    is_df_thresh = isinstance(min_weight_change, pd.DataFrame)
    if isinstance(min_weight_change, (int, float)):
        if min_weight_change <= 0.0 and min_holding_bars <= 0:
            return daily
        thresh = pd.Series(float(min_weight_change), index=risky_symbols)
    elif isinstance(min_weight_change, dict):
        thresh = pd.Series(min_weight_change).reindex(risky_symbols).fillna(0.02)
    elif isinstance(min_weight_change, pd.Series):
        thresh = min_weight_change.reindex(risky_symbols).fillna(0.02)
    elif is_df_thresh:
        thresh = None
    else:
        thresh = pd.Series(0.02, index=risky_symbols)

    filtered = daily.copy()
    current_w = daily.iloc[0].copy()
    filtered.iloc[0] = current_w

    # Caller-owned tenure clocks -- NOT kernel state (the kernel holds none).
    # `last_buy_bar` starts at 0 for an already-held symbol and at -999 for a
    # flat one, so a symbol held on bar 0 is treated as "bought long ago" and is
    # not tenure-locked by the first bar's inertia decision.
    holding_age: Dict[str, int] = {s: 0 for s in risky_symbols}
    last_buy_bar: Dict[str, int] = {s: (0 if current_w[s] > 1e-4 else -999) for s in risky_symbols}

    # Only the four inertia-relevant fields are set; every other field keeps the
    # design default. In particular `min_holding_bars` is taken from the argument
    # (default 0 = no tenure lock), NOT from `PositionPolicyConfig`'s default.
    cfg = PositionPolicyConfig(
        min_holding_bars=min_holding_bars,
        asymmetric_sells=asymmetric_sells,
        asymmetric_sell_ratio=asymmetric_sell_ratio,
        entry_threshold=entry_threshold,
    )
    sequencer = PortfolioSequencer()

    em_source: Optional[Union[pd.Series, pd.DataFrame]] = None
    constant_emergency: Optional[bool] = None
    if emergency_mask is not None:
        if isinstance(emergency_mask, (pd.Series, pd.DataFrame)):
            em_source = emergency_mask
        else:
            constant_emergency = bool(emergency_mask)

    for t in range(1, n_dates):
        ideal_w = daily.iloc[t]
        if em_source is not None:
            em_row: Optional[Union[bool, pd.Series]] = em_source.iloc[t]
        elif constant_emergency is not None:
            em_row = constant_emergency
        else:
            em_row = None

        if is_df_thresh:
            thresh_t = min_weight_change.iloc[t].reindex(risky_symbols).fillna(0.02)
        else:
            thresh_t = thresh

        current_w = sequencer.sequence(
            ideal_w,
            current_w,
            ctx=PolicyContext(bar_index=t),
            risky_symbols=risky_symbols,
            symbols=symbols,
            thresh_t=thresh_t,
            holding_age=holding_age,
            last_buy_bar=last_buy_bar,
            cfg=cfg,
            cash_proxy=cash_proxy,
            em_row=em_row,
        )
        filtered.iloc[t] = current_w

    return filtered
