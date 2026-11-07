"""Unified Position Lifecycle State Machine (UPLSM).

Formalizes the lifecycle of every portfolio asset into a deterministic,
observable state machine preventing whipsaws, locking T+1 gestation tenures,
enforcing asymmetric re-entry cooldowns, and filtering micro-rebalance churn
across both backtesting and live execution engines.

M4.3 -- THIS MODULE IS AN ADAPTER OVER `common.position_policy`
==============================================================
`step_position_lifecycle` keeps its public signature and its
`LifecycleDecision` return type, and stays the ONLY entry point the live runner
and the parity harness call. It delegates ONE step to the kernel's
`AssetPolicy.step` and translates the kernel's `Decision` back into a
`LifecycleDecision`:

  * `Decision.action` / `target_weight` / `reason` map 1:1 (the action
    vocabulary is shared, see `position_policy.PolicyAction`);
  * `Decision.blocking_reason` becomes `LifecycleDecision.blocking_reason` (the
    telemetry field the ticket column is read from), and -- ONLY for the two
    "nothing was evaluated" reasons (`MARKET_CLOSED_FREEZE` / `NO_PRICE`) -- is
    also mirrored into `detail_reason["blocking_reason"]`, which is the shape
    those two branches have always had (and only those);
  * `Decision.detail["state_after"]` (a full JSON-safe snapshot: counters,
    tenures, cooldowns, prices) is written back onto the caller's
    `AssetLifecycleState` so the in-place-mutation contract of this function is
    preserved for callers that read `state` after the call;
  * `fast_path_triggered` / `cancelled_entry_streak` are recovered from the
    kernel detail, which is where the kernel records them.

THE LEGACY BODY IS GONE (M4.4 stage D3, `BASE_VERSION 2.0.0`)
-------------------------------------------------------------
There is exactly ONE engine: the kernel. Stage D2 flipped the shipping default
to ON on the strength of evidence, not faith -- the multi-seed ON-vs-legacy
differential in `pipeline/tests/test_policy_parity.py` (frame cells, per-record
decision fields, and the persisted state trajectory) plus the archived live
tickets -- and stage D3 deleted the legacy 6-state body that D2 kept as an
opt-out. Two consequences, both deliberate:

  * `live_use_policy_kernel` survives as an INERT LEGACY KEY. A persisted live
    config / strategy JSON that still carries `false` does NOT select a deleted
    path: it logs a loud warning naming the flag and the version that removed the
    legacy body, and the kernel runs anyway. Raising instead would fail a live
    run closed over a key that no longer means anything -- a bigger behaviour
    change than the removal itself.
  * `enable_entry_fast_path` / `enable_exit_fast_path` have NO kernel
    equivalent: the kernel's risk/entry fast paths are the policy, not a toggle.
    They are now a HARD PRECONDITION (see `_require_kernel_step_supported`): a
    config that disables either is rejected with an exception naming the key,
    because silently running the always-on fast path would turn a disabled stop
    into a live stop.

CONTEXT PLUMBING
----------------
The adapter fills `PolicyContext` from the arguments this signature carries:
`price`, `volume`, `ma20_volume` (also the fallback for `vol_ma20`), the
`ma20_close` / `vwap20` / `val` anchor keywords, `market_breadth`, `atr`,
`market`, the three boolean flags.

Stage C closed the gap this section used to disclose: `ma20_close`, `vwap20` and
`val` had NO source anywhere, so the two OPT-IN M1.1 gates (2b `two_day_breakdown_rule`
/ 3b `price_reentry_gate`) were inert in EVERY engine -- backtest and live alike.
They are now supplied by `derive_lifecycle_context` (+ `lifecycle_context_at`),
ONE causal derivation shared by `simulate_position_lifecycle` and
`scripts/run_live_strategy.py`, so the backtest and the live ticket cannot
disagree on whether a bar broke structure or reclaimed its VWAP. `val` is only
ever passed through when the universe frame actually carries a `VAL`-like column
(`VAL_COLUMNS`); it is never fabricated. Both flags remain OFF by default, so the
plumbing moves no number on its own.
"""

from __future__ import annotations

import copy
import logging
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd


_LOGGER = logging.getLogger(__name__)

#: M4.4 stage D3 -- `live_use_policy_kernel=False` is an inert legacy key. The
#: warning it triggers is emitted ONCE per process (the flag is a per-run config
#: decision, so repeating it on every one of the ~40k per-asset steps of a
#: backtest would bury the signal it exists to give).
_REMOVED_KERNEL_FLAG_WARNED = False


def _warn_removed_kernel_flag_once() -> None:
    """Say, once and loudly, that `live_use_policy_kernel=False` no longer selects anything.

    Same contract as D1's `blend_use_policy_kernel` handling: the key survives so
    persisted configs keep loading, but it cannot route to a deleted body. Never
    raise -- failing a live run closed over a key whose meaning was removed would
    be a bigger behaviour change than the removal itself.
    """
    global _REMOVED_KERNEL_FLAG_WARNED
    if _REMOVED_KERNEL_FLAG_WARNED:
        return
    _REMOVED_KERNEL_FLAG_WARNED = True
    _LOGGER.warning(
        "live_use_policy_kernel=False was configured but the legacy "
        "position-lifecycle state machine was removed in BASE_VERSION 2.0.0 "
        "(M4.4 stage D3): running the position-policy kernel instead. Drop the flag "
        "from params/config to silence this warning."
    )


# ---------------------------------------------------------------------------
# M4.1 (type migration, no behaviour): these seven names now live in
# `common/position_policy.py` -- the single policy kernel of
# `docs/position_policy_consolidation_design.md` §4-§6. They are RE-EXPORTED
# here (a one-way dependency: `position_lifecycle` -> `position_policy`;
# `position_policy` must never import this module, which is why the kernel owns
# no live-runner code) so that EVERY existing import site keeps working
# unchanged:
#
#   * `common/tests/test_position_lifecycle.py`, `backtester/`, `pipeline/`
#     (`parity_harness.py`, `test_blend_track_c.py`),
#   * `scripts/run_live_strategy.py` (imports `BlockingReason`,
#     `coerce_blocking_reason`, `is_liquidation_event`, ...),
#   * `web/livetrading/` (`BLOCKING_REASON_LABELS`),
#
# all of which do `from common.position_lifecycle import ...` and must keep
# working without an edit. Re-exporting (instead of keeping a duplicate copy of
# each definition) is what makes the type single-source: a `BlockingReason` built
# by this module, by the kernel, or by a re-exported import is the SAME enum
# member, so `is` / `==` / dict lookups cannot diverge.
# ---------------------------------------------------------------------------
from common.position_policy import (  # noqa: E402  (re-export, not a new use)
    BLOCKING_REASON_LABELS,
    AssetPolicy,
    AssetState,
    BlockingReason,
    Decision,
    ExitType,
    PolicyContext,
    PositionPolicyConfig,
    PositionState,
    blocking_reason_label,
    coerce_blocking_reason,
    is_liquidation_event,
)


@dataclass
class PositionLifecycleConfig:
    """Configuration parameters for asset position lifecycles.

    The DEFAULTS below are the ones `scripts/run_live_strategy.py:1555-1573`
    actually deploys (`min_holding_bars=2`, `normal_cooldown_bars=2`,
    `inertia_relative_ratio=0.0`, ...). They were once inconsistent here
    (`min_holding_bars=1`, `normal_cooldown_bars=1`), which meant a bare
    `PositionLifecycleConfig()` modelled a DIFFERENT, looser state machine than
    the live ticket -- a 1-bar tenure lock and a 1-bar re-entry cooldown instead
    of the deployed 2/2. `min_holding_bars` was the odd one out (the field
    default said 1 while both resolvers said 2); F3 aligned it.
    """
    guard_latency_bars: int = 2
    min_holding_bars: int = 2
    normal_cooldown_bars: int = 2
    stop_cooldown_bars: int = 5
    inertia_threshold: float = 0.05
    # (2026-10-22 incident) Relative trim-side inertia ratio. When > 0.0, a
    # SELL-direction partial trim must clear BOTH the absolute
    # `inertia_threshold` AND a proportional fraction of the current weight
    # (`ratio * current_weight`) before it is allowed. Prevents micro-trims
    # (e.g. a -0.0082pp trim on a 17-bar incumbent) from being emitted as real
    # SELL orders that merely round to a handful of shares. 0.0 disables the
    # proportional hurdle and preserves the legacy absolute-only gate.
    inertia_relative_ratio: float = 0.0
    min_entry_threshold: Optional[float] = None
    hard_stop_loss_pct: float = 0.08
    atr_multiplier: float = 4.5
    volume_surge_multiplier: float = 1.5
    breadth_thrust_threshold: float = 0.60
    # M4.4 stage D3: these two toggles are a HARD PRECONDITION, not a switch.
    # The kernel's fast paths are the policy (a risk exit must never be
    # toggle-able away), so `False` raises -- see
    # `_require_kernel_step_supported`. Kept as fields because they are part of
    # the persisted config schema and the CLI surface.
    enable_entry_fast_path: bool = True
    enable_exit_fast_path: bool = True

    # ---- M4.3 live adapter ---------------------------------------------------
    # `live_use_policy_kernel` is an INERT LEGACY KEY since M4.4 stage D3: the
    # legacy body it used to select (an explicit `False`) was DELETED in
    # `BASE_VERSION 2.0.0`, so only the kernel remains and `False` merely logs a
    # warning (see `step_position_lifecycle`). The field is kept -- not removed --
    # because persisted `account_state.json` files and strategy JSONs still carry
    # it, and a resolver that dropped the key would be a silent schema change.
    live_use_policy_kernel: bool = True
    # The six M1.1 opt-in kernel knobs, MIRRORED here so the kernel config can be
    # resolved through ONE path (`resolve_position_policy_config` maps a
    # resolved `PositionLifecycleConfig` onto the kernel's config). Defaults are
    # the kernel's own: both rules OFF, so an unflagged config maps onto a
    # `PositionPolicyConfig()` whose opt-in gates are inert. They are declared on
    # BOTH classes on purpose -- the live runner carries a
    # `PositionLifecycleConfig`, while the kernel consumes a
    # `PositionPolicyConfig`; duplicating the defaults is what keeps a bare
    # instance of either class semantically identical.
    two_day_breakdown_rule: bool = False
    breakdown_days: int = 2
    breakdown_vol_shrink_pct: float = 0.60
    price_reentry_gate: bool = False
    price_reentry_pct: float = 0.02
    reentry_vwap_lookback: int = 20

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class AssetLifecycleState:
    """Persistent state representation for a single asset's lifecycle."""
    symbol: str
    state: PositionState = PositionState.FLAT
    held_bars: int = 0
    entry_streak: int = 0
    exit_streak: int = 0
    cooldown_remaining: int = 0
    entry_price: float = 0.0
    peak_price: float = 0.0
    current_weight: float = 0.0
    # (2026-10-22 incident) Pre-trade brokerage weight snapshot, hydrated from
    # the actual holdings (cur_w) by run_live_strategy.py before
    # step_position_lifecycle mutates `current_weight` to the post-rebalance
    # target. Persisting `broker_weight` alongside `current_weight` prevents
    # account_state.json from silently storing the TARGET as if it were the
    # broker weight (000999.SZ persisted 0.06731049 while the broker held
    # 0.13934551). The lifecycle machine itself never mutates this field.
    broker_weight: float = 0.0
    last_exit_type: Optional[str] = None
    # ---- M4.3: state fields the policy kernel needs to persist ----------------
    # `expansion_streak` mirrors `AssetState.expansion_streak` (the blend
    # engines' expansion-confirmation ladder). `step_position_lifecycle` never
    # reads or writes it; it exists so a hydrating caller can carry the full
    # kernel state without a second side-car dict, and so the cold-start rule can
    # zero it explicitly.
    expansion_streak: int = 0
    #: Gate 2b's OWN breakdown confirmation counter (`two_day_breakdown_rule`).
    #: Written by the kernel and carried here so a two-day structural break can
    #: span two live runs. Default-inert: nothing increments it unless the rule
    #: is ON.
    breakdown_streak: int = 0
    #: Close of the last exit, retained through the cooldown for gate 3b
    #: (`price_reentry_gate`). `0.0` = unknown -> the gate ALLOWS (never a
    #: permanent lockout). Written by every kernel exit path.
    last_exit_price: float = 0.0
    # ---- M4.3: cold-start provenance, persisted so it stays auditable --------
    #: True when this state was CREATED by live hydration for a symbol with no
    #: persisted lifecycle state (i.e. no history to resume). Counters start at
    #: zero and the state is restricted to {FLAT, HELD_ACTIVE, COOLDOWN} --
    #: `PENDING_*` / `HELD_GESTATION` are never fabricated for a cold start.
    cold_start: bool = False
    #: True when `entry_price` was filled from the current `price` because no
    #: recorded cost basis existed. Carried so a stop-loss computed off an
    #: approximated basis is never mistaken for one computed off a real fill.
    entry_price_approx: bool = False

    def is_holding(self) -> bool:
        """Returns True if the asset is currently held in any position state."""
        return self.state in (
            PositionState.HELD_GESTATION,
            PositionState.HELD_ACTIVE,
            PositionState.PENDING_EXIT,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes lifecycle state to a JSON-safe dictionary."""
        return {
            "symbol": self.symbol,
            "state": self.state.value if isinstance(self.state, PositionState) else str(self.state),
            "held_bars": self.held_bars,
            "entry_streak": self.entry_streak,
            "exit_streak": self.exit_streak,
            "cooldown_remaining": self.cooldown_remaining,
            "entry_price": float(self.entry_price),
            "peak_price": float(self.peak_price),
            "current_weight": float(self.current_weight),
            "broker_weight": float(self.broker_weight),
            "last_exit_type": self.last_exit_type,
            "expansion_streak": int(self.expansion_streak),
            "breakdown_streak": int(self.breakdown_streak),
            "last_exit_price": float(self.last_exit_price),
            "cold_start": bool(self.cold_start),
            "entry_price_approx": bool(self.entry_price_approx),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AssetLifecycleState":
        """Restores an AssetLifecycleState instance from a dictionary."""
        raw_state = d.get("state", PositionState.FLAT.value)
        state_enum = PositionState(raw_state) if isinstance(raw_state, str) else raw_state
        return cls(
            symbol=d["symbol"],
            state=state_enum,
            held_bars=int(d.get("held_bars", 0)),
            entry_streak=int(d.get("entry_streak", 0)),
            exit_streak=int(d.get("exit_streak", 0)),
            cooldown_remaining=int(d.get("cooldown_remaining", 0)),
            entry_price=float(d.get("entry_price", 0.0)),
            peak_price=float(d.get("peak_price", 0.0)),
            current_weight=float(d.get("current_weight", 0.0)),
            broker_weight=float(d.get("broker_weight", 0.0)),
            last_exit_type=d.get("last_exit_type"),
            # (M4.3) Additive with defaults, so every archived
            # `account_state.json` written before this change still loads (a
            # missing key -> the same inert value a legacy state carried).
            expansion_streak=int(d.get("expansion_streak", 0) or 0),
            breakdown_streak=int(d.get("breakdown_streak", 0) or 0),
            last_exit_price=float(d.get("last_exit_price", 0.0) or 0.0),
            cold_start=bool(d.get("cold_start", False)),
            entry_price_approx=bool(d.get("entry_price_approx", False)),
        )


@dataclass
class LifecycleDecision:
    """Decision output generated by a single lifecycle step transition."""
    action: str  # "BUY", "SELL", "HOLD", "HOLD_FILTERED", "COOLDOWN_BLOCKED", "CANCEL_PENDING"
    target_weight: float
    reason: str
    detail_reason: Dict[str, Any] = field(default_factory=dict)
    state_before: PositionState = PositionState.FLAT
    state_after: PositionState = PositionState.FLAT
    fast_path_triggered: Optional[str] = None
    # (2026-10-22 incident, Fix 3) Pre-reset entry-confirmation streak, populated
    # ONLY by the CANCEL_PENDING transition. A PENDING_ENTRY asset whose signal
    # disappears has its `entry_streak` zeroed (correct: the confirmation clock
    # must restart from scratch if the signal returns), but the VALUE it had
    # reached is the single most diagnostic number in the 601857.SH post-mortem
    # ("was it 1/2, or 2/2 and therefore a lost confirmed entry?"). Carrying it
    # on the decision object lets the ticket record the lost progress instead of
    # erasing it. Appended with a default so every existing constructor keeps
    # working unchanged.
    cancelled_entry_streak: int = 0
    # (L2.24) Structured "why was nothing traded" label. Appended with a default so
    # every existing constructor keeps working unchanged. See `BlockingReason`:
    # this is the ONLY honest way downstream code may distinguish "a signal was
    # evaluated and gated" from "nothing was evaluated at all", instead of
    # inferring a liquidation from `target_weight == 0.0`.
    blocking_reason: BlockingReason = BlockingReason.NONE

    def to_dict(self) -> Dict[str, Any]:
        """Serializes decision to a JSON-safe dictionary."""
        return {
            "action": self.action,
            "target_weight": float(self.target_weight),
            "reason": self.reason,
            "detail_reason": self.detail_reason,
            "state_before": self.state_before.value if isinstance(self.state_before, PositionState) else str(self.state_before),
            "state_after": self.state_after.value if isinstance(self.state_after, PositionState) else str(self.state_after),
            "fast_path_triggered": self.fast_path_triggered,
            "cancelled_entry_streak": int(self.cancelled_entry_streak),
            "blocking_reason": coerce_blocking_reason(self.blocking_reason).value,
        }


# ---------------------------------------------------------------------------
# M4.3 adapter: legacy config -> kernel config, and kernel `Decision` ->
# `LifecycleDecision`. The two helpers below are the ONLY translation surface;
# nothing else in this module may re-spell a kernel field mapping.
# ---------------------------------------------------------------------------


def position_policy_config_from_lifecycle(
    cfg: PositionLifecycleConfig,
) -> PositionPolicyConfig:
    """Map a LIVE-RESOLVED `PositionLifecycleConfig` onto the kernel's config.

    This is the single field mapping. `resolve_position_policy_config()` (the
    public bridge) and the in-adapter step both go through it, so a caller that
    resolves a config for inspection can never be inspecting a DIFFERENT config
    than the one `step_position_lifecycle` actually runs.

    DELIBERATE, DOCUMENTED DIVERGENCES -- read before "tidying" any of them:
      * `stop_cooldown_bars` -> `asset_stop_cooldown_bars`. The legacy field name
        is the per-ASSET post-stop cooldown; it is NOT `blend_base`'s
        portfolio-level `stop_cooldown_bars=21` (design §2: same key, two
        meanings -- that ambiguity is why the kernel renamed it).
      * `min_entry_threshold` -> `entry_threshold`. The legacy `None` is kept as
        `None` (kernel: `None` means "fall back to `inertia_threshold`", which is
        exactly what the legacy body does). The live runner resolves `0.05`
        instead; that divergence lives in the live driver, not here, and must
        NOT be silently adopted by this mapper (see
        `resolve_position_lifecycle_config`'s docstring).
      * The two fast-path toggles (`enable_entry_fast_path` /
        `enable_exit_fast_path`) have NO kernel field: the kernel's risk and
        entry fast paths are always on. They are a HARD PRECONDITION
        (`_require_kernel_step_supported` rejects `False` with a `ValueError`),
        so no mapping is invented for them here.
      * `asymmetric_sells` / `asymmetric_sell_ratio` are INERTIA-path fields
        (`AssetPolicy.inertia_step`). The state-machine ladder (`AssetPolicy.step`)
        never reads them, so they keep the kernel defaults; mapping the legacy
        values would create a second, unread truth.
      * Every other field is copied EXPLICITLY, including the ones whose kernel
        default differs (`min_holding_bars=1`, `normal_cooldown_bars=1`). The
        kernel class defaults are the design §5 defaults, deliberately NOT the
        deployed live ones (design §5 disclosure), so a mapping that "left them
        at the default" would silently relax the live machine to a 1-bar tenure
        lock and a 1-bar cooldown.
    """
    return PositionPolicyConfig(
        # --- ladder thresholds (kernel default != deployed live default) -------
        guard_latency_bars=int(cfg.guard_latency_bars),
        min_holding_bars=int(cfg.min_holding_bars),
        normal_cooldown_bars=int(cfg.normal_cooldown_bars),
        asset_stop_cooldown_bars=int(cfg.stop_cooldown_bars),
        # --- inertia ----------------------------------------------------------
        inertia_threshold=float(cfg.inertia_threshold),
        inertia_relative_ratio=float(cfg.inertia_relative_ratio),
        # --- entry / risk thresholds -----------------------------------------
        entry_threshold=(
            None if cfg.min_entry_threshold is None else float(cfg.min_entry_threshold)
        ),
        hard_stop_loss_pct=float(cfg.hard_stop_loss_pct),
        atr_multiplier=float(cfg.atr_multiplier),
        volume_surge_multiplier=float(cfg.volume_surge_multiplier),
        breadth_thrust_threshold=float(cfg.breadth_thrust_threshold),
        # --- M1.1 opt-in gates (both default OFF; inert while OFF) ------------
        two_day_breakdown_rule=bool(cfg.two_day_breakdown_rule),
        breakdown_days=int(cfg.breakdown_days),
        breakdown_vol_shrink_pct=float(cfg.breakdown_vol_shrink_pct),
        price_reentry_gate=bool(cfg.price_reentry_gate),
        price_reentry_pct=float(cfg.price_reentry_pct),
        reentry_vwap_lookback=int(cfg.reentry_vwap_lookback),
    )


def _require_kernel_step_supported(cfg: PositionLifecycleConfig) -> None:
    """Hard precondition: the config must be expressible in the kernel.

    The kernel's entry and risk-exit fast paths are unconditional (they ARE the
    policy, not a toggle). A config that DISABLES either asks for a DIFFERENT
    policy -- a stop-loss bypassed by a toggle -- which the kernel cannot and must
    not silently reproduce.

    M4.4 stage D3 turned this from a silent fall-through (return `False` -> run
    the legacy body) into an ERROR, because the legacy body no longer exists: a
    `False` here would otherwise reach a deleted code path. Raising is correct
    precisely because NO shipped config sets either key false -- the only False
    settings in the tree are tests that pin this rejection. If a real config ever
    arrives with one of these disabled, it is a genuine, unsupported policy and
    the operator must be told, not silently upgraded to an always-on stop.
    """
    disabled = [
        key
        for key in ("enable_entry_fast_path", "enable_exit_fast_path")
        if not bool(getattr(cfg, key))
    ]
    if disabled:
        raise ValueError(
            f"{' / '.join(disabled)}=False is not supported: the legacy "
            "position-lifecycle state machine was removed in BASE_VERSION 2.0.0 "
            "(M4.4 stage D3), and the policy kernel's entry/risk fast paths are "
            "unconditional -- a disabled fast path is a different policy, not a "
            "configuration of this one. Remove the key(s) from the config/params "
            "to run the kernel."
        )


def _kernel_asset_state(state: AssetLifecycleState) -> AssetState:
    """Snapshot the legacy state into the kernel's (immutable-input) carrier."""
    return AssetState(
        symbol=state.symbol,
        state=state.state,
        held_bars=int(state.held_bars),
        entry_streak=int(state.entry_streak),
        exit_streak=int(state.exit_streak),
        expansion_streak=int(state.expansion_streak),
        cooldown_remaining=int(state.cooldown_remaining),
        entry_price=float(state.entry_price),
        peak_price=float(state.peak_price),
        current_weight=float(state.current_weight),
        broker_weight=float(state.broker_weight),
        last_exit_type=state.last_exit_type,
        breakdown_streak=int(state.breakdown_streak),
        last_exit_price=float(state.last_exit_price),
    )


def _apply_kernel_state(state: AssetLifecycleState, snapshot: Dict[str, Any]) -> None:
    """Write the kernel's `detail["state_after"]` back onto the caller's state.

    WHY THIS EXISTS: the legacy `step_position_lifecycle` mutates its `state`
    argument in place, and every caller depends on that (`run_live_strategy`
    reads `state.is_holding()`, `state.held_bars`, `state.cooldown_remaining`
    and finally persists `state.to_dict()`). The kernel deliberately does NOT
    mutate its input (design invariant 3), so the adapter must re-establish the
    contract -- here, in exactly one place.
    """
    raw_state = snapshot.get("state", state.state)
    state.state = PositionState(raw_state) if isinstance(raw_state, str) else raw_state
    state.held_bars = int(snapshot.get("held_bars", state.held_bars))
    state.entry_streak = int(snapshot.get("entry_streak", state.entry_streak))
    state.exit_streak = int(snapshot.get("exit_streak", state.exit_streak))
    state.expansion_streak = int(snapshot.get("expansion_streak", state.expansion_streak))
    state.cooldown_remaining = int(
        snapshot.get("cooldown_remaining", state.cooldown_remaining)
    )
    state.entry_price = float(snapshot.get("entry_price", state.entry_price))
    state.peak_price = float(snapshot.get("peak_price", state.peak_price))
    state.current_weight = float(snapshot.get("current_weight", state.current_weight))
    # The kernel never mutates `broker_weight`; the write-through keeps the two
    # representations equal without inventing a value.
    state.broker_weight = float(snapshot.get("broker_weight", state.broker_weight))
    state.last_exit_type = snapshot.get("last_exit_type", state.last_exit_type)
    state.breakdown_streak = int(snapshot.get("breakdown_streak", state.breakdown_streak))
    state.last_exit_price = float(snapshot.get("last_exit_price", state.last_exit_price))


def _decision_to_lifecycle(
    decision: Decision,
    state: AssetLifecycleState,
    state_before: PositionState,
) -> LifecycleDecision:
    """Translate the kernel's `Decision` into the legacy `LifecycleDecision`.

    Field-by-field, all of it explicit:
      * `action` / `target_weight` / `reason` are shared vocabulary, copied;
      * `detail["state_after"]` is applied to `state` BEFORE `state_after` is
        read off it, so the returned decision and the returned state agree;
      * `blocking_reason` is both the typed field AND -- for the two "nothing was
        evaluated" reasons only -- mirrored into `detail_reason`, which is the
        shape those two branches have always used and which several readers
        (`web/`, the ticket's `lifecycle_decision` block) consult;
      * `fast_path_triggered` and `cancelled_entry_streak` are lifted out of the
        kernel detail because `LifecycleDecision` carries them as fields.
    """
    detail = dict(decision.detail or {})
    _apply_kernel_state(state, detail.get("state_after") or {})
    # `blocking_reason` is mirrored into the detail ONLY for the two "nothing was
    # evaluated" branches (`MARKET_CLOSED_FREEZE` / `NO_PRICE`), which is exactly
    # where the legacy body has always put it (see this module's docstring). Adding
    # it everywhere would make the ON path's `detail_reason` carry a key the OFF
    # path's does not -- a difference with no reader and no justification.
    typed_reason = coerce_blocking_reason(decision.blocking_reason)
    if typed_reason in (BlockingReason.MARKET_CLOSED_FREEZE, BlockingReason.NO_PRICE):
        detail.setdefault("blocking_reason", typed_reason.value)
    try:
        cancelled_streak = int(detail.get("cancelled_entry_streak", 0) or 0)
    except (TypeError, ValueError):
        cancelled_streak = 0
    return LifecycleDecision(
        action=decision.action,
        target_weight=decision.target_weight,
        reason=decision.reason,
        detail_reason=detail,
        state_before=state_before,
        state_after=state.state,
        fast_path_triggered=detail.get("fast_path_triggered"),
        cancelled_entry_streak=cancelled_streak,
        blocking_reason=decision.blocking_reason,
    )


def _kernel_policy_context(
    *,
    current_price: float,
    current_volume: Optional[float],
    ma20_volume: Optional[float],
    market_breadth: Optional[float],
    atr: Optional[float],
    market: str,
    is_market_open: bool,
    is_emergency_halt: bool,
    is_portfolio_deleverage: bool,
    ma20_close: Optional[float] = None,
    vwap20: Optional[float] = None,
    vol_ma20: Optional[float] = None,
    val: Optional[float] = None,
) -> PolicyContext:
    """Fill `PolicyContext` from the arguments `step_position_lifecycle` carries.

    `price` / `ma20_volume` / `atr` / `market_breadth` / the flags are passed
    through. `ma20_close` / `vwap20` / `vol_ma20` / `val` are the two OPT-IN
    M1.1 gates' anchors; since Stage C they are real inputs threaded down from
    `derive_lifecycle_context`, and a caller that does not supply them still gets
    `None`, which both gates treat as "this bar cannot be evaluated" (a strict
    no-op, never 0.0). Inventing any of them HERE would be fabricating the very
    anchor the gates exist to test -- which is why the derivation is one layer up
    and shared with the backtest path.

    `vol_ma20` FALLS BACK to `ma20_volume` when the caller supplies no separate
    volume mean: the entry fast path's 20-bar mean volume IS the correct input
    for gate 2b's 缩量 exemption and gate 3b's expansion leg, and handing one of
    them `None` while giving the other a value would disable a rule for a reason
    that does not exist. The two fields stay SEPARATE inside `PolicyContext`
    (their thresholds are orthogonal and must not be merged) -- they merely share
    one source when only one source exists.
    """
    return PolicyContext(
        price=float(current_price),
        atr=atr,
        volume=current_volume,
        # `ma20_volume` feeds gate 5's volume-surge test; `vol_ma20` feeds gates
        # 2b/3b. They are separate FIELDS with separate thresholds (orthogonality
        # is deliberate); `vol_ma20` falls back to the entry path's mean only when
        # the caller supplied no separate series (Stage C threads one down).
        ma20_volume=ma20_volume,
        vol_ma20=ma20_volume if vol_ma20 is None else vol_ma20,
        market_breadth=market_breadth,
        market=market,
        is_market_open=bool(is_market_open),
        is_emergency_halt=bool(is_emergency_halt),
        is_portfolio_deleverage=bool(is_portfolio_deleverage),
        # --- the two OPT-IN gates' anchors (Stage C plumbing; `None` = no-op) --
        ma20_close=ma20_close,
        vwap20=vwap20,
        val=val,
    )


def cold_start_lifecycle_state(
    symbol: str,
    *,
    is_held: bool,
    held_bars: int,
    bars_since_exit: int,
    reentry_cooldown_bars: int,
    entry_price: float = 0.0,
    price: float = 0.0,
    current_weight: float = 0.0,
) -> AssetLifecycleState:
    """Build the state for a symbol that has NO persisted lifecycle history.

    This is the M4.3 cold-start rule, extracted so it is testable without a live
    run (and so `run_live_strategy` cannot quietly grow a second variant of it):

      * **counters are ZERO** -- `entry_streak`, `exit_streak`,
        `breakdown_streak`, `expansion_streak`. The machine has never observed this
        symbol, so any non-zero value here would be history it did not earn.
      * **state is restricted to `{HELD_ACTIVE, COOLDOWN, FLAT}`** -- the
        pre-M4.4-D3 hydration branch USED TO fabricate `HELD_GESTATION` (from a
        broker holding-bar guess) and `PENDING_ENTRY` (from a model signal
        streak). Those are states the machine was never observed in, and handing
        them to the kernel would credit tenure/confirmation on its very first
        step. That branch is gone; this rule is the only cold start.
      * `held_bars` is NOT zeroed on the held branch: it comes from the account
        ledger (`holding_bars`), i.e. it is broker fact rather than a lifecycle
        counter.
      * **`entry_price` falls back to `price`** when no cost basis is recorded --
        a stop-loss needs an anchor -- and then `entry_price_approx=True` records
        that the anchor is a guess, so a stop computed off it is never read as one
        computed off a real fill.
      * `cold_start=True` and `last_exit_price=0.0` (unknown -> gate 3b ALLOWS;
        a fabricated exit price would be a permanent lockout).
    """
    if is_held:
        state_enum = PositionState.HELD_ACTIVE
    elif bars_since_exit < reentry_cooldown_bars:
        state_enum = PositionState.COOLDOWN
    else:
        state_enum = PositionState.FLAT
    cooldown_rem = (
        max(0, reentry_cooldown_bars - bars_since_exit)
        if state_enum == PositionState.COOLDOWN
        else 0
    )

    resolved_entry_price = float(entry_price or 0.0)
    entry_price_approx = False
    if is_held and resolved_entry_price <= 0 and price > 0:
        resolved_entry_price = float(price)
        entry_price_approx = True
    peak_price = (
        max(resolved_entry_price, float(price)) if is_held and price > 0 else resolved_entry_price
    )
    return AssetLifecycleState(
        symbol=symbol,
        state=state_enum,
        held_bars=int(held_bars),
        entry_streak=0,
        exit_streak=0,
        cooldown_remaining=cooldown_rem,
        entry_price=resolved_entry_price,
        peak_price=peak_price,
        current_weight=float(current_weight),
        broker_weight=float(current_weight),
        breakdown_streak=0,
        expansion_streak=0,
        last_exit_price=0.0,
        cold_start=True,
        entry_price_approx=entry_price_approx,
    )


def step_position_lifecycle(
    state: AssetLifecycleState,
    raw_target_weight: float,
    current_price: float,
    cfg: PositionLifecycleConfig,
    market: str = "china",
    current_volume: Optional[float] = None,
    ma20_volume: Optional[float] = None,
    market_breadth: Optional[float] = None,
    atr: Optional[float] = None,
    is_emergency_halt: bool = False,
    is_market_open: bool = True,
    is_portfolio_deleverage: bool = False,
    ma20_close: Optional[float] = None,
    vwap20: Optional[float] = None,
    vol_ma20: Optional[float] = None,
    val: Optional[float] = None,
) -> LifecycleDecision:
    """Transitions an asset's position lifecycle state for a single bar.

    Parameters:
        state: The persistent asset lifecycle state (mutated in place).
        raw_target_weight: Unfiltered target weight from the strategy model.
        current_price: Latest asset market price.
        cfg: State machine configuration rules.
        market: Target exchange market ("china" enforces T+1 gestation locks).
        current_volume: Current bar volume (for volume breakout fast path).
        ma20_volume: 20-bar moving average volume.
        market_breadth: Macro breadth percentage [0.0, 1.0].
        atr: Average True Range for dynamic trailing stop checks.
        is_emergency_halt: Macro circuit breaker emergency halt flag.
        is_market_open: Whether current bar is an open trading day. If False,
            all lifecycle counters, gestation timers, and cooldowns remain frozen.
        is_portfolio_deleverage: Portfolio-level drawdown deleveraging active.
            When True and trimming an existing holding (0.0 <= raw_target < current_weight),
            bypasses asset-level inertia threshold and tenure lock to prevent risk-control
            dilution.
        ma20_close: 20-bar simple mean of close -- gate 2b's structural floor
            (with `val`, when present). `None` (the default) means "no anchor on
            this bar", which makes gate 2b a strict no-op; it is NEVER read as
            0.0. Derive it with `derive_lifecycle_context`, never by hand.
        vwap20: 20-bar volume-weighted average price -- gate 3b's reclaim leg.
            `None` = cannot evaluate (never 0.0).
        vol_ma20: 20-bar mean volume used by gates 2b (缩量 exemption) and 3b
            (expansion leg). `None` falls back to `ma20_volume`, the same 20-bar
            mean volume the entry fast path uses.
        val: Value-Area-Low level (`common/structural_factors` `VAL` column) for
            gate 2b's `min(MA20, VAL)` floor. Only ever a pass-through from the
            caller's own frame -- there is no derivation here, and `None` simply
            leaves the floor at MA20 alone.

    Returns:
        LifecycleDecision containing actionable target weight, trade action,
        and forensic reasoning.

    M4.4 D3: the policy kernel is the ONLY engine -- there is no engine switch
    left to flip. It delegates one step to
    `common.position_policy.AssetPolicy.step` and translates the decision back;
    the signature, the in-place mutation of `state` and the `LifecycleDecision`
    return type are unchanged. See the module docstring for the translation
    contract, and `_require_kernel_step_supported` for the one config shape that
    is rejected outright (`enable_*_fast_path=False`).

    Stage C: the four anchor keywords above are the plumbing the two OPT-IN M1.1
    gates need. They are inert while both gates are False; `simulate_` builders
    and the live runner both fill them from `derive_lifecycle_context`, so the
    backtest and the live ticket cannot disagree on a trigger.
    """
    state_before = state.state

    # M4.4 stage D3: the kernel is the ONLY engine. `live_use_policy_kernel` is
    # an INERT LEGACY KEY -- a persisted config pinning it to False must not select
    # a path that no longer exists, so it warns (loudly, naming the version) and
    # the kernel runs anyway. Raising would fail a live run closed over a key whose
    # meaning was removed; that is a bigger behaviour change than the removal.
    if bool(getattr(cfg, "live_use_policy_kernel", True)) is False:
        _warn_removed_kernel_flag_once()
    _require_kernel_step_supported(cfg)
    # The kernel is pure and does NOT mutate `state`; `_decision_to_lifecycle`
    # writes `detail["state_after"]` back so this function keeps its documented
    # in-place mutation contract.
    kernel_decision = AssetPolicy().step(
        _kernel_asset_state(state),
        float(raw_target_weight),
        _kernel_policy_context(
            current_price=current_price,
            current_volume=current_volume,
            ma20_volume=ma20_volume,
            market_breadth=market_breadth,
            atr=atr,
            ma20_close=ma20_close,
            vwap20=vwap20,
            vol_ma20=vol_ma20,
            val=val,
            market=market,
            is_market_open=is_market_open,
            is_emergency_halt=is_emergency_halt,
            is_portfolio_deleverage=is_portfolio_deleverage,
        ),
        position_policy_config_from_lifecycle(cfg),
    )
    return _decision_to_lifecycle(kernel_decision, state, state_before)


def _live_param_resolvers() -> Tuple[Any, Any, Any, Any]:
    """The LIVE runner's parameter resolvers, imported -- never re-implemented.

    Root cause this helper exists for (F2): `resolve_position_lifecycle_config`
    used to read ONLY `lifecycle_<field>` keys, while the live runner reads the
    STRATEGY's own spellings (`guard_latency_bars`, `min_entry_bars`,
    `reentry_cooldown_bars`, `min_hold_bars`, `cfsb_inertia_relative_ratio`, the
    `min_weight_change` family, ...). A strategy carrying any of those therefore
    resolved to the DEFAULTS in a backtest -- the backtest silently modelled a
    different state machine than the one actually deployed, with no warning.

    The alias resolution delegates to `scripts/run_live_strategy`'s own
    functions (`resolve_blend_param`, `resolve_min_entry_bars`,
    `resolve_min_holding_bars`, `resolve_trade_inertia_threshold`) instead of
    copying their precedence rules, so the two engines cannot drift: e.g. the
    `_min_holding_bars` / `_min_hold_bars` suffix compatibility (`L2.39`) and the
    `cfsb_/crb_/ccsb_/cfssb_/cdhb_/blend_` prefix scan are picked up here for
    free, and a future fix to the live precedence applies to both.

    Imported LAZILY on purpose: `scripts.run_live_strategy` imports this module
    at module scope (`PositionLifecycleConfig`, `step_position_lifecycle`,
    `BlockingReason`, ...), so a module-level import here would be circular.
    `scripts.run_live_strategy` also repairs `sys.path` (repo root + `pipeline/`)
    on import, so the call works from any cwd.
    """
    from scripts.run_live_strategy import (
        resolve_blend_param,
        resolve_min_entry_bars,
        resolve_min_holding_bars,
        resolve_trade_inertia_threshold,
    )

    return (
        resolve_blend_param,
        resolve_min_entry_bars,
        resolve_min_holding_bars,
        resolve_trade_inertia_threshold,
    )


def resolve_position_lifecycle_config(
    params: Optional[Dict[str, Any]] = None,
    args: Any = None,
) -> PositionLifecycleConfig:
    """Resolves PositionLifecycleConfig by merging strategy parameters and CLI arguments.

    Defaults (ALIGNED WITH THE LIVE DEPLOYMENT, `scripts/run_live_strategy.py:1538-1573`):
        guard_latency_bars: 2
        min_holding_bars: 2
        normal_cooldown_bars: 2
        stop_cooldown_bars: 5
        inertia_threshold: 0.05
        inertia_relative_ratio: 0.0
        min_entry_threshold: None
        hard_stop_loss_pct: 0.08
        atr_multiplier: 4.5
        volume_surge_multiplier: 1.5
        breadth_thrust_threshold: 0.60
        enable_entry_fast_path: True
        enable_exit_fast_path: True
        live_use_policy_kernel: True    (M4.3 key; INERT legacy since M4.4 D3)
        two_day_breakdown_rule: False   (M1.1 gate 2b, kernel-only)
        breakdown_days: 2
        breakdown_vol_shrink_pct: 0.60
        price_reentry_gate: False       (M1.1 gate 3b, kernel-only)
        price_reentry_pct: 0.02
        reentry_vwap_lookback: 20

    The two defaults that used to DISAGREE with live (and are fixed here, F1)
    were `normal_cooldown_bars=1` (live: 2 -- `--reentry-cooldown-bars` default)
    and `inertia_relative_ratio=0.50` (live: 0.0, i.e. DISABLED everywhere except
    `chan_four_state_blend`, which opts in through
    `cfsb_inertia_relative_ratio`). A 0.50 default silently halved every trim
    below half the incumbent weight in the backtest only.

    `min_entry_threshold` is the ONE field that deliberately stays `None` here
    while the live ticket sets `0.05`: live derives it as
    `args.min_entry_threshold or resolve_blend_param(params, "entry_threshold")
    or <inertia threshold>` (line 1539), i.e. the raw entry gate inherits the
    turnover-inertia threshold. Adopting that here would silently TIGHTEN entry
    gating for every backtest that never set the field (0.0 < w < 0.05 would
    stop entering), which is a separate behavioural decision, not a
    default-alignment fix. Pass `min_entry_threshold` explicitly (params
    `lifecycle_min_entry_threshold` / CLI `--lifecycle-min-entry-threshold`) to
    opt in.

    KEY RESOLUTION (F2) -- first hit wins, per field:
      1. `args.lifecycle_<field>` (backtester CLI, `run_backtest.py:214-258`)
      2. `params["lifecycle_<field>"]` (explicit, unambiguous)
      3. live-runner ALIAS on the CLI namespace (`args.reentry_cooldown_bars`,
         `args.guard_latency_bars`, `args.min_entry_bars`, ...)
      4. the same alias in `params` (`reentry_cooldown_bars`,
         `min_hold_bars`, `min_entry_bars`, `cfsb_inertia_relative_ratio`,
         `min_weight_change`, ...)
      5. the live default

    Alias table (at least; the live resolvers cover far more spellings):
        normal_cooldown_bars  <- reentry_cooldown_bars
        stop_cooldown_bars    <- stop_cooldown_bars (bare) / max(5, normal)
        min_holding_bars      <- min_hold_bars (+ *_min_holding_bars suffix)
        guard_latency_bars    <- min_entry_bars (+ *_min_entry_bars suffix)
        inertia_threshold     <- min_weight_change / *_min_weight_change
        inertia_relative_ratio<- cfsb_inertia_relative_ratio (only when present)

    M4.3 (kernel bridge): the last seven fields are the inert legacy key
    `live_use_policy_kernel` (a persisted `false` only logs: the path it used to
    select was deleted in BASE_VERSION 2.0.0 / M4.4 D3) plus the six M1.1 knobs,
    mirroring `PositionPolicyConfig`. The knob defaults are the KERNEL's (both
    gates OFF), so a strategy that never opted in resolves to a semantically
    inert kernel config. `resolve_position_policy_config()` is
    the public bridge that applies `position_policy_config_from_lifecycle` to
    this function's result -- use it instead of re-deriving the mapping at a call
    site.
    """
    p = params or {}
    (
        resolve_blend_param,
        resolve_min_entry_bars,
        resolve_min_holding_bars,
        resolve_trade_inertia_threshold,
    ) = _live_param_resolvers()

    def _get(field_name: str, default: Any, *aliases: str) -> Any:
        key = f"lifecycle_{field_name}"
        if args is not None:
            val = getattr(args, key, None)
            if val is not None:
                return val
        if p:
            val = p.get(key)
            if val is not None:
                return val
        if args is not None:
            for alias in aliases:
                val = getattr(args, alias, None)
                if val is not None:
                    return val
        if p:
            for alias in aliases:
                val = p.get(alias)
                if val is not None:
                    return val
        return default

    def _live_guard_latency_bars() -> int:
        """`scripts/run_live_strategy.py:1541-1547`, verbatim.

        NOTE the live chain is gated on three literal keys
        (`min_entry_bars` / `cfsb_min_entry_bars` / `cfssb_min_entry_bars`)
        before `resolve_min_entry_bars` (which itself scans any
        `*_min_entry_bars` suffix) is invoked; the same three-key gate is kept
        here so a params dict resolves to the SAME guard in both engines.
        """
        guard = 2
        if args is not None and getattr(args, "guard_latency_bars", None) is not None:
            guard = int(getattr(args, "guard_latency_bars"))
        if args is not None and getattr(args, "min_entry_bars", None) is not None:
            return resolve_min_entry_bars(p, args.min_entry_bars, default=guard)
        if p.get("guard_latency_bars") is not None:
            return max(1, int(p["guard_latency_bars"]))
        if any(k in p for k in ("min_entry_bars", "cfsb_min_entry_bars", "cfssb_min_entry_bars")):
            return resolve_min_entry_bars(p, None, default=guard)
        return guard

    guard_latency_bars = max(1, int(_get("guard_latency_bars", _live_guard_latency_bars())))
    # `resolve_min_holding_bars` IS the live resolution (including the
    # `min_hold_bars` / `*_min_hold_bars` suffix compatibility, L2.39), so no
    # alias list is layered on top of it.
    min_holding_bars = max(
        0,
        int(
            _get(
                "min_holding_bars",
                resolve_min_holding_bars(
                    p,
                    None if args is None else getattr(args, "min_holding_bars", None),
                    default=2,
                ),
            )
        ),
    )
    normal_cooldown_bars = max(
        0, int(_get("normal_cooldown_bars", 2, "reentry_cooldown_bars"))
    )
    # Live: `stop_cooldown_bars=max(5, reentry_cooldown_bars)` (line 1559) -- the
    # stop-loss cooling period can never be shorter than the voluntary one.
    # An explicit `lifecycle_stop_cooldown_bars` / bare `stop_cooldown_bars`
    # still wins over that floor.
    stop_cooldown_bars = max(
        0,
        int(
            _get(
                "stop_cooldown_bars",
                max(5, normal_cooldown_bars),
                "stop_cooldown_bars",
            )
        ),
    )
    # Live: `resolve_trade_inertia_threshold(params, default=0.05)` (line 1538),
    # which owns the `min_weight_change` / `*_min_weight_change` /
    # `*_min_weight_pct` spellings.
    inertia_threshold = float(
        _get("inertia_threshold", resolve_trade_inertia_threshold(p, default=0.05))
    )
    # Live: `resolve_blend_param(params, "inertia_relative_ratio", 0.0, float)`
    # (line 1565) -- 0.0 unless the strategy explicitly carries the key under
    # one of the blend prefixes (`cfsb_inertia_relative_ratio` for
    # chan_four_state_blend) or bare.
    inertia_relative_ratio = float(
        _get(
            "inertia_relative_ratio",
            resolve_blend_param(p, "inertia_relative_ratio", 0.0, float),
        )
    )
    min_entry_thresh = _get("min_entry_threshold", None)
    min_entry_threshold = float(min_entry_thresh) if min_entry_thresh is not None else None
    hard_stop_loss_pct = float(_get("hard_stop_loss_pct", 0.08))
    atr_multiplier = float(_get("atr_multiplier", 4.5))
    volume_surge_multiplier = float(_get("volume_surge_multiplier", 1.5))
    breadth_thrust_threshold = float(_get("breadth_thrust_threshold", 0.60))
    enable_entry_fast_path = bool(_get("enable_entry_fast_path", True))
    enable_exit_fast_path = bool(_get("enable_exit_fast_path", True))

    # --- M4.3: the master switch + the six M1.1 opt-in kernel knobs -----------
    # `live_use_policy_kernel` is resolved like every other field so a strategy
    # JSON can pin it per-strategy (`live_use_policy_kernel` /
    # `lifecycle_live_use_policy_kernel`) and a CLI can override it
    # (`--live-use-policy-kernel` -> `args.lifecycle_live_use_policy_kernel`).
    # It defaults to True, and since M4.4 stage D3 it is INERT: the legacy body an
    # explicit `False` used to select was deleted in BASE_VERSION 2.0.0, so a
    # pinned `false` only warns (`_warn_removed_kernel_flag_once`) and the kernel
    # runs. Kept as a resolved field so old configs keep round-tripping.
    live_use_policy_kernel = bool(_get("live_use_policy_kernel", True, "live_use_policy_kernel"))
    # The six knobs keep the KERNEL's own defaults (both rules OFF), so the
    # resolved config is semantically identical to a bare `PositionPolicyConfig`
    # whenever a strategy has not opted in. `two_day_breakdown_rule` and
    # `price_reentry_gate` are gates 2b / 3b in `common/position_policy.py`.
    # Each `_get(field, default, field)`: the alias repeats the field name on
    # purpose, so BOTH spellings resolve (`lifecycle_price_reentry_pct` from the
    # backtester CLI namespace, bare `price_reentry_pct` from a strategy JSON)
    # while `lifecycle_*` keeps precedence.
    two_day_breakdown_rule = bool(_get("two_day_breakdown_rule", False, "two_day_breakdown_rule"))
    breakdown_days = int(_get("breakdown_days", 2, "breakdown_days"))
    breakdown_vol_shrink_pct = float(
        _get("breakdown_vol_shrink_pct", 0.60, "breakdown_vol_shrink_pct")
    )
    price_reentry_gate = bool(_get("price_reentry_gate", False, "price_reentry_gate"))
    price_reentry_pct = float(_get("price_reentry_pct", 0.02, "price_reentry_pct"))
    reentry_vwap_lookback = int(_get("reentry_vwap_lookback", 20, "reentry_vwap_lookback"))

    return PositionLifecycleConfig(
        guard_latency_bars=guard_latency_bars,
        min_holding_bars=min_holding_bars,
        normal_cooldown_bars=normal_cooldown_bars,
        stop_cooldown_bars=stop_cooldown_bars,
        inertia_threshold=inertia_threshold,
        inertia_relative_ratio=inertia_relative_ratio,
        min_entry_threshold=min_entry_threshold,
        hard_stop_loss_pct=hard_stop_loss_pct,
        atr_multiplier=atr_multiplier,
        volume_surge_multiplier=volume_surge_multiplier,
        breadth_thrust_threshold=breadth_thrust_threshold,
        enable_entry_fast_path=enable_entry_fast_path,
        enable_exit_fast_path=enable_exit_fast_path,
        live_use_policy_kernel=live_use_policy_kernel,
        two_day_breakdown_rule=two_day_breakdown_rule,
        breakdown_days=breakdown_days,
        breakdown_vol_shrink_pct=breakdown_vol_shrink_pct,
        price_reentry_gate=price_reentry_gate,
        price_reentry_pct=price_reentry_pct,
        reentry_vwap_lookback=reentry_vwap_lookback,
    )


def resolve_position_policy_config(
    params: Optional[Dict[str, Any]] = None,
    args: Any = None,
) -> PositionPolicyConfig:
    """Resolve the KERNEL config the live runner would execute, from params+args.

    This is `resolve_position_lifecycle_config` (the legacy resolver, with its
    documented `min_entry_threshold=None` divergence INTACT) followed by the
    single `position_policy_config_from_lifecycle` mapping. It exists so the
    live runner has ONE production call site that names the kernel config
    explicitly instead of re-deriving fields, and so "which config does the
    kernel actually see?" is answerable without re-implementing the mapping.

    The six M1.1 knobs are carried through to the kernel; deliberately NOT
    carried: `asymmetric_sells` / `asymmetric_sell_ratio` (inertia-path only --
    `AssetPolicy.step` never reads them) and the two fast-path toggles (no kernel
    field; see `_require_kernel_step_supported`).

        >>> resolve_position_policy_config({}, None).price_reentry_gate
        False
        >>> resolve_position_policy_config({}, None).two_day_breakdown_rule
        False
    """
    return position_policy_config_from_lifecycle(
        resolve_position_lifecycle_config(params, args)
    )


# ---------------------------------------------------------------------------
# M1.1 / Stage C -- the ONE derivation of the two opt-in gates' bar anchors
# ---------------------------------------------------------------------------
#
# WHY THIS LIVES HERE, AND WHY IT IS ONE FUNCTION
# -----------------------------------------------
# `AssetPolicy.step`'s two M1.1 gates need four pre-computed anchors per bar:
#   gate 2b (`two_day_breakdown_rule`): `ma20_close` + optional `val`
#   gate 3b (`price_reentry_gate`):      `vwap20` + `vol_ma20`
# The kernel deliberately does NOT derive them (`PolicyContext`'s docstring:
# "`vwap20` is a PRE-COMPUTED 20-day VWAP -- the kernel never derives one"), so
# SOMETHING must. Before this stage, NOTHING did: the backtest simulator and the
# live adapter both passed `None`, so both gates were inert in every engine.
#
# The danger of a second derivation is the whole point of this stage: a 20-bar
# mean computed over a different window (or a VWAP over a different volume
# series) makes the backtest and the live ticket disagree about whether a bar
# was a structural break -- silently, because both would still report "a gate
# fired". So there is exactly ONE implementation (`derive_lifecycle_context`) and
# exactly ONE reader (`lifecycle_context_at`), used by:
#   * `simulate_position_lifecycle` below (the backtest path the backtester,
#     `pipeline/parity_harness.py` and the parity suite all drive), and
#   * `scripts/run_live_strategy.py` (the live ticket path).
#
# CAUSALITY (no look-ahead): every output is a TRAILING window that ends on the
# bar being labelled -- `rolling(n).mean()` / `rolling(n).sum()` over that bar
# and earlier. Nothing is centred, shifted back, or forward-filled from a later
# bar, so mutating future bars cannot change an earlier bar's row (proven by
# `pipeline/tests/test_lifecycle_context_derivation.py`).

#: Column names accepted as a "VAL" (Value Area Low) level, in priority order.
#: Today exactly ONE producer exists: `common.structural_factors.
#: enrich_universe_with_structural_features` attaches a rolling Volume-Profile
#: `VAL` (default 60-bar lookback, `compute_rolling_poc_series`), and it is
#: attached only by `backtester/run_backtest.py` and the blend base -- no data
#: provider emits it, so a plain OHLCV frame carries none. The aliases exist
#: because a caller may rename the column in its own panel. What is NEVER done is
#: inventing a level: no column -> `None` -> gate 2b degrades to its documented
#: MA20-only floor (`_two_day_breakdown_step`).
VAL_COLUMNS: Tuple[str, ...] = ("VAL", "value_area_low", "VALUE_AREA_LOW")

#: The four columns `derive_lifecycle_context` returns, in `PolicyContext` order.
LIFECYCLE_CONTEXT_COLUMNS: Tuple[str, ...] = ("ma20_close", "vwap20", "vol_ma20", "val")


def derive_lifecycle_context(
    close: Optional[pd.Series],
    volume: Optional[pd.Series] = None,
    *,
    val: Optional[pd.Series] = None,
    vol_ma20: Optional[pd.Series] = None,
    ma20_window: int = 20,
    vwap_lookback: int = 20,
) -> pd.DataFrame:
    """Per-bar anchors for the two OPT-IN M1.1 gates -- strictly causal, never fabricated.

    Returns a frame indexed exactly like `close` with the four
    `LIFECYCLE_CONTEXT_COLUMNS`. A NaN cell means "this bar cannot be evaluated
    by that rule"; readers (`lifecycle_context_at`) turn it into `None`, and the
    kernel treats `None` as a strict no-op -- NEVER as 0.0 (a fabricated 0 floor
    would make every close a "breakdown", a fabricated 0 VWAP would make every
    price a reclaim).

    THE FOUR OUTPUTS
    ----------------
    * `ma20_close` -- `close.rolling(20, min_periods=20).mean()`.
      **min_periods is the FULL window, deliberately.** A 3-bar mean printed as
      "MA20" sits almost on top of the price, so nearly any dip would clear it and
      gate 2b would confirm a "structural break" on a symbol that has no 20-day
      structure yet. Insufficient history therefore yields NaN -> `None` -> the
      gate is a no-op during warmup, which is the contract its own docstring
      states ("missing data degrades to a NO-OP, never to a fabricated break").
    * `vwap20` -- rolling volume-weighted average price over `vwap_lookback`
      bars (default 20 = `PositionPolicyConfig.reentry_vwap_lookback`):
      `sum(close * volume) / sum(volume)`. Same FULL-window `min_periods`, plus
      an explicit `sum(volume) > 0` guard, so a window containing a zero/NaN
      volume bar (or a fully missing `volume` series) yields NaN -> `None`. A
      VWAP built over a partial window is not the 20-day VWAP gate 3b means.
    * `vol_ma20` -- 20-bar mean volume. **REUSED, not re-derived, whenever the
      caller already has the series** (`vol_ma20=` is honoured verbatim): both
      call sites already resolve a 20-bar mean volume for the ENTRY fast path's
      `ma20_volume`, and handing the gates a second, slightly different volume
      mean would be exactly the drift this helper exists to prevent. When it is
      absent it is computed with the same `min_periods=1` trailing mean both
      call sites already use -- that partial-window warmup is pre-existing
      deployed behaviour for `ma20_volume` and is left alone; it cannot leak into
      the gates anyway because both gates already require a full-window
      `ma20_close` / `vwap20` anchor before they evaluate at all.
    * `val` -- passed through from the caller's frame, or all-NaN. THERE IS NO
      DERIVATION HERE ON PURPOSE: `VAL` is a Volume-Profile level produced by
      `common.structural_factors`, and fabricating one (e.g. a rolling low) would
      silently change the floor `min(MA20, VAL)` computes. See `VAL_COLUMNS`.

    `close=None` returns an empty frame with the right columns (a caller with no
    frame at all); every other missing input degrades per-column, never
    per-row-block, so one absent input cannot disable the others.
    """
    if close is None:
        return pd.DataFrame(columns=list(LIFECYCLE_CONTEXT_COLUMNS), dtype=float)

    closes = pd.to_numeric(pd.Series(close), errors="coerce").astype(float)
    window = max(int(ma20_window), 1)
    lookback = max(int(vwap_lookback), 1)

    # 1. MA20 of close -- full trailing window only (see docstring).
    ma20_close = closes.rolling(window, min_periods=window).mean()

    # 2. Rolling VWAP -- `sum(close*volume)/sum(volume)` over the same full window.
    #    `min_periods == lookback` on BOTH sums means fewer than `lookback` usable
    #    observations (a NaN volume bar among them) yields NaN rather than a VWAP
    #    that quietly skipped half its window.
    if volume is not None:
        vols = pd.to_numeric(
            pd.Series(volume).reindex(closes.index), errors="coerce"
        ).astype(float)
        denom = vols.rolling(lookback, min_periods=lookback).sum()
        numer = (closes * vols).rolling(lookback, min_periods=lookback).sum()
        # A zero total volume has no price: 0/0 is NaN, never a division blow-up.
        vwap20 = numer / denom.where(denom > 0.0)
    else:
        vols = None
        vwap20 = pd.Series(np.nan, index=closes.index, dtype=float)

    # 3. vol_ma20 -- REUSE the caller's series when it has one (see docstring).
    if vol_ma20 is not None:
        vol_mean = pd.to_numeric(
            pd.Series(vol_ma20).reindex(closes.index), errors="coerce"
        ).astype(float)
    elif vols is not None:
        vol_mean = vols.rolling(window, min_periods=1).mean()
    else:
        vol_mean = pd.Series(np.nan, index=closes.index, dtype=float)

    # 4. VAL -- pass-through only (never derived, never fabricated).
    if val is not None:
        val_level = pd.to_numeric(
            pd.Series(val).reindex(closes.index), errors="coerce"
        ).astype(float)
    else:
        val_level = pd.Series(np.nan, index=closes.index, dtype=float)

    context = pd.DataFrame(
        {
            "ma20_close": ma20_close,
            "vwap20": vwap20,
            "vol_ma20": vol_mean,
            "val": val_level,
        },
        index=closes.index,
        dtype=float,
    )
    # Infinities are not anchors either: report them as "cannot evaluate" here so
    # no downstream caller has to remember to test `np.isfinite` before handing a
    # value to the kernel (the kernel tests it too -- belt and suspenders).
    return context.replace([np.inf, -np.inf], np.nan)


def lifecycle_context_at(
    context: Optional[pd.DataFrame],
    bar: Any = None,
) -> Dict[str, Optional[float]]:
    """One bar's anchors as the four `Optional[float]` values `PolicyContext` takes.

    THE single reader of `derive_lifecycle_context`'s frame, shared by the
    backtest simulator and the live runner, so a "missing cell" is interpreted
    identically in both: missing bar, missing/duplicate column, NaN, inf, or a
    non-positive level all decode to `None`. `None` is never 0.0 -- see
    `derive_lifecycle_context` for why a fabricated zero is a fabricated signal.

    `bar=None` reads the LAST row, which is what the live runner wants (its
    universe is already truncated to the as-of session by `as_of_universe`).
    """
    missing: Dict[str, Optional[float]] = {name: None for name in LIFECYCLE_CONTEXT_COLUMNS}
    if context is None or context.empty:
        return missing
    try:
        row = context.iloc[-1] if bar is None else context.loc[bar]
    except (KeyError, IndexError):
        return missing
    if isinstance(row, pd.DataFrame):
        # A duplicated index label: refuse to guess which bar is meant.
        return missing
    values: Dict[str, Optional[float]] = {}
    for name in LIFECYCLE_CONTEXT_COLUMNS:
        try:
            numeric = float(row.get(name))
        except (TypeError, ValueError):
            values[name] = None
            continue
        # Every one of the four is a PRICE/VOLUME LEVEL the kernel only uses when
        # > 0 (a floor, a VWAP, a volume mean). Non-positive -> unusable -> None.
        values[name] = numeric if (np.isfinite(numeric) and numeric > 0.0) else None
    return values


def derive_lifecycle_context_from_frame(
    frame: Optional[pd.DataFrame],
    *,
    ma20_window: int = 20,
    vwap_lookback: int = 20,
) -> pd.DataFrame:
    """`derive_lifecycle_context` straight from an OHLCV(+VAL) frame.

    A thin convenience so the live runner (which holds `{symbol: DataFrame}`) and
    any caller holding a frame cannot disagree about WHICH columns are the
    inputs: `Close` is required, `Volume` and the `VAL_COLUMNS` are optional and
    simply absent -> `None`. No column is ever synthesised from another (a
    `Close`-derived "VAL" would be a fabricated level).
    """
    if frame is None or len(frame) == 0 or "Close" not in getattr(frame, "columns", ()):
        return derive_lifecycle_context(None)
    return derive_lifecycle_context(
        close=frame["Close"],
        volume=frame["Volume"] if "Volume" in frame.columns else None,
        val=_val_series_from_frame(frame),
        ma20_window=ma20_window,
        vwap_lookback=vwap_lookback,
    )


def _val_series_from_frame(frame: Optional[pd.DataFrame]) -> Optional[pd.Series]:
    """The frame's VAL-like level series, or `None` when it carries none.

    Only a real `VAL_COLUMNS` member is accepted; nothing is derived. Kept as one
    helper (rather than an inline `frame.get(...)` at each call site) so the
    backtest simulator, the live runner and the frame convenience above cannot
    pick different columns.
    """
    if frame is None:
        return None
    for column in VAL_COLUMNS:
        if column in getattr(frame, "columns", ()):
            return frame[column]
    return None


def _reentry_vwap_lookback(cfg: Any) -> int:
    """`cfg.reentry_vwap_lookback`, defaulted/guarded (never a 0-bar window).

    `PositionPolicyConfig.reentry_vwap_lookback` documents this window but the
    kernel never consumes it -- the layer that DERIVES `vwap20` does, which is
    this one. A duck-typed cfg without the field gets the documented default.
    """
    try:
        lookback = int(getattr(cfg, "reentry_vwap_lookback", 20) or 20)
    except (TypeError, ValueError):
        lookback = 20
    return max(lookback, 1)


def simulate_position_lifecycle(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    cfg: Optional[PositionLifecycleConfig] = None,
    market: str = "china",
    cash_proxy: Optional[str] = None,
    initial_states: Optional[Dict[str, Union[AssetLifecycleState, Dict[str, Any]]]] = None,
    current_volumes: Optional[pd.DataFrame] = None,
    ma20_volumes: Optional[pd.DataFrame] = None,
    market_breadths: Optional[pd.Series] = None,
    atrs: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """Pure causal simulation of position lifecycle state machine across time.

    Enforces entry latency confirmation, T+1 tenure locks, asymmetric cooldowns,
    and trailing / hard stop-loss risk exits.

    Adheres strictly to the SPARSE WEIGHTS CONTRACT:
    - On active rebalance days (where at least one asset triggers BUY or SELL):
      target weights are written for all active assets. Closed/exited assets are
      explicitly written as 0.0 (never left as NaN, preventing backtester ffill leaks).
    - If NO asset triggers a trade on date t (all HOLD/HOLD_FILTERED/COOLDOWN_BLOCKED):
      the entire row is left as all-NaN so the backtester does not rebalance.
    - If cash_proxy is provided, residual cash weight `max(0.0, 1.0 - sum(risky_w))`
      is assigned to cash_proxy on active rebalance days.

    CASH-PROXY DISCLOSURE (F5, accounting geometry is deliberately unchanged):
    the cash leg is EXCLUDED from `risky_symbols`, so it never enters the state
    machine -- it has no PENDING_ENTRY confirmation, no tenure lock, no cooldown
    and no stop-loss. It is therefore also not a "position" that can be
    de-risked by this function. What it IS, is an ordinary symbol to the
    backtester that consumes these weights: its rebalances still pay commission,
    slippage and the stamp/levy legs exactly like a risky asset, and its
    in-and-out turnover counts toward the reported turnover figure. In other
    words the idle-cash sleeve is lifecycle-exempt but NOT cost-exempt.
    Auto cash parking (the `max(0.0, 1.0 - sum(risky_w))` residual) only happens
    when `cash_proxy` is a column of `target_weights`; a caller that passes a
    proxy outside the universe gets a warning (see below) and no parking.

    Returns:
        (filtered_sparse_weights, lifecycle_records)

    M1.1 / Stage C: every bar's `PolicyContext` anchors (`ma20_close`, `vwap20`,
    `vol_ma20`, `val`) are derived here -- once per symbol, causally, by
    `derive_lifecycle_context` -- and handed to `step_position_lifecycle`. That is
    the ONLY backtest-path source of those anchors, and it is the same helper the
    live runner calls, so the two engines cannot disagree on a trigger. The
    anchors are inert while the two gates are OFF (their defaults); they move a
    number only for a cfg that opts in.
    """
    if cfg is None:
        cfg = resolve_position_lifecycle_config()

    if target_weights.empty:
        return target_weights.copy(), []

    # Identify symbols
    symbols = list(target_weights.columns)
    if cash_proxy is not None and cash_proxy not in symbols:
        # (F5) This used to be silent: the residual-cash parking below appended
        # a brand-new all-NaN column, which a caller that indexes columns by
        # POSITION (rather than by name) would either drop or misalign -- the
        # weights would then look like an un-invested book while the state
        # machine reported it had run. Warn ONCE per call instead of guessing:
        # the proxy has to be a real member of the universe for the sleeve to
        # be lifecycle-exempt *and* cost-bearing in the same ledger.
        warnings.warn(
            f"simulate_position_lifecycle: cash_proxy={cash_proxy!r} is not a column of "
            "target_weights, so automatic cash parking into it is inert (the residual is "
            "left to the caller's own cash handling). Pass the proxy as a universe/target "
            "column to have the idle-cash sleeve lifecycle-exempt but cost-bearing.",
            RuntimeWarning,
            stacklevel=2,
        )
    risky_symbols = [s for s in symbols if cash_proxy is None or s != cash_proxy]

    # Pre-extract price, volume, and ATR series
    closes: Dict[str, pd.Series] = {}
    vols: Dict[str, pd.Series] = {}
    ma20_v: Dict[str, pd.Series] = {}
    atr_map: Dict[str, pd.Series] = {}
    #: M1.1 / Stage C: per-symbol frame of the two OPT-IN gates' causal anchors
    #: (`derive_lifecycle_context`). Built HERE, once per symbol, from the SAME
    #: series the loop reads, and read back per bar through `lifecycle_context_at`
    #: -- which is the identical code the live runner executes, so the backtest
    #: and the live ticket cannot disagree on whether a bar broke structure.
    lifecycle_ctx: Dict[str, pd.DataFrame] = {}

    for sym in risky_symbols:
        df = universe.get(sym)
        if df is not None and not df.empty:
            if "Close" in df.columns:
                closes[sym] = pd.to_numeric(df["Close"], errors="coerce")
            if current_volumes is not None and sym in current_volumes.columns:
                vols[sym] = current_volumes[sym]
            elif "Volume" in df.columns:
                vols[sym] = pd.to_numeric(df["Volume"], errors="coerce")
            if ma20_volumes is not None and sym in ma20_volumes.columns:
                ma20_v[sym] = ma20_volumes[sym]
            elif sym in vols:
                ma20_v[sym] = vols[sym].rolling(20, min_periods=1).mean()
            if atrs is not None and sym in atrs.columns:
                atr_map[sym] = atrs[sym]
            elif "High" in df.columns and "Low" in df.columns and "Close" in df.columns:
                try:
                    from common.indicators import atr
                    atr_map[sym] = atr(df, 14)
                except Exception:
                    pass
            if sym in closes:
                # Stage C: the two OPT-IN gates' anchors, derived from the SAME
                # close/volume series the loop below reads. `ma20_v` is REUSED
                # (not recomputed) as `vol_ma20` so gates 2b/3b and the entry fast
                # path can never look at two different volume means.
                lifecycle_ctx[sym] = derive_lifecycle_context(
                    close=closes[sym],
                    volume=vols.get(sym),
                    vol_ma20=ma20_v.get(sym),
                    val=_val_series_from_frame(df),
                    vwap_lookback=_reentry_vwap_lookback(cfg),
                )

    # Initialize states
    states: Dict[str, AssetLifecycleState] = {}
    if initial_states:
        for sym, st in initial_states.items():
            if isinstance(st, AssetLifecycleState):
                states[sym] = AssetLifecycleState.from_dict(st.to_dict())
            elif isinstance(st, dict):
                states[sym] = AssetLifecycleState.from_dict(st)
    for sym in risky_symbols:
        if sym not in states:
            states[sym] = AssetLifecycleState(symbol=sym)

    # Raw model targets carried forward (ffill) across dates
    raw_targets = target_weights.ffill().fillna(0.0)

    # Initialize output sparse weights with NaNs
    filtered_weights = pd.DataFrame(
        np.nan,
        index=target_weights.index,
        columns=target_weights.columns,
        dtype=float,
    )
    if cash_proxy is not None and cash_proxy not in filtered_weights.columns:
        filtered_weights[cash_proxy] = np.nan

    lifecycle_records: List[Dict[str, Any]] = []

    for t in target_weights.index:
        decisions: Dict[str, LifecycleDecision] = {}
        for sym in risky_symbols:
            st = states[sym]
            tgt_w = float(raw_targets.loc[t, sym]) if (t in raw_targets.index and sym in raw_targets.columns) else 0.0

            # Resolve price
            p_series = closes.get(sym)
            if p_series is not None and t in p_series.index:
                p_val = p_series.loc[t]
                if isinstance(p_val, pd.Series):
                    p_val = p_val.iloc[-1]
                price = float(p_val) if pd.notna(p_val) and p_val > 0 else 0.0
            else:
                price = 0.0
            # M4.4 stage D3: NO_PRICE is NOT hand-built here any more. This used
            # to construct a `LifecycleDecision` inline, i.e. a SECOND
            # implementation of the no-price rule that could drift from the
            # kernel's gate 1b. The price (possibly 0.0) is now handed to
            # `step_position_lifecycle` below, which returns the kernel's
            # NO_PRICE decision -- same action / target_weight / reason /
            # `detail_reason` shape as before (the kernel's gate is an exact port
            # of the deleted branch), but from ONE implementation.
            cur_v = None
            if sym in vols and t in vols[sym].index:
                v_val = vols[sym].loc[t]
                if isinstance(v_val, pd.Series):
                    v_val = v_val.iloc[-1]
                cur_v = float(v_val) if pd.notna(v_val) and v_val > 0 else None

            ma_v = None
            if sym in ma20_v and t in ma20_v[sym].index:
                mv_val = ma20_v[sym].loc[t]
                if isinstance(mv_val, pd.Series):
                    mv_val = mv_val.iloc[-1]
                ma_v = float(mv_val) if pd.notna(mv_val) and mv_val > 0 else None

            sym_atr = None
            if sym in atr_map and t in atr_map[sym].index:
                atr_val = atr_map[sym].loc[t]
                if isinstance(atr_val, pd.Series):
                    atr_val = atr_val.iloc[-1]
                sym_atr = float(atr_val) if pd.notna(atr_val) and atr_val > 0 else None

            breadth_val = None
            if market_breadths is not None and t in market_breadths.index:
                b_val = market_breadths.loc[t]
                if isinstance(b_val, pd.Series):
                    b_val = b_val.iloc[-1]
                breadth_val = float(b_val) if pd.notna(b_val) else None

            # Stage C: the two OPT-IN M1.1 gates' anchors for THIS bar, read
            # through the same `lifecycle_context_at` the live runner uses. A
            # missing bar / warmup NaN decodes to `None` (a strict no-op), never
            # to 0.0 -- a fabricated floor or VWAP would fabricate a signal.
            ctx_values = lifecycle_context_at(lifecycle_ctx.get(sym), t)

            # Resolve market per asset
            asset_market = market
            if market == "auto":
                if sym.endswith(".SH") or sym.endswith(".SZ") or (sym.isdigit() and len(sym) == 6):
                    asset_market = "china"
                elif sym.endswith(".HK") or (sym.isdigit() and len(sym) in (4, 5)):
                    asset_market = "hk"
                else:
                    asset_market = "us"

            dec = step_position_lifecycle(
                state=st,
                raw_target_weight=tgt_w,
                current_price=price,
                cfg=cfg,
                market=asset_market,
                current_volume=cur_v,
                ma20_volume=ma_v,
                market_breadth=breadth_val,
                atr=sym_atr,
                ma20_close=ctx_values["ma20_close"],
                vwap20=ctx_values["vwap20"],
                vol_ma20=ctx_values["vol_ma20"],
                val=ctx_values["val"],
            )
            decisions[sym] = dec

            rec = dec.to_dict()
            rec["date"] = str(t)
            rec["symbol"] = sym
            lifecycle_records.append(rec)

        # Check if any asset triggered an actionable trade (BUY or SELL)
        any_trade = any(d.action in ("BUY", "SELL") for d in decisions.values())

        if any_trade:
            # Active rebalance day: write target weights for all active assets
            sum_risky = 0.0
            for sym in risky_symbols:
                st = states[sym]
                if st.is_holding():
                    w = float(st.current_weight)
                else:
                    # Closed / flat assets MUST write explicit 0.0
                    w = 0.0
                filtered_weights.loc[t, sym] = w
                sum_risky += w

            # If cash proxy is provided, assign residual cash
            if cash_proxy is not None:
                cash_w = max(0.0, 1.0 - sum_risky)
                filtered_weights.loc[t, cash_proxy] = cash_w

    return filtered_weights, lifecycle_records
