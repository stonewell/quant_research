"""2026-11-16 -- persisting the PORTFOLIO vol-ceiling scope, and the version bump it forces.

WHY THIS FILE EXISTS
--------------------
The owner chose ablation arm B (`portfolio` scope) for the live vol ceiling. The switch already
lives in `scripts/run_live_strategy.py` (`cfsb_vol_ceiling_scope` in
`{stage1, portfolio}`, `stage1` default), so persisting the choice means two edits: the
per-strategy pin in `pipeline/research_strategy/strategies_config.json` (which reaches the live
runner through the re-dumped `strategy_dumps/chan_four_state_blend_strategy.json`) and the
`StrategyConfig` field that lets the key round-trip through `from_dict` instead of being
dropped as "unknown" (`AGENTS.md` §7's `wq_alpha_id` lesson).

Because that materially changes DELIVERED LIVE EXPOSURE, `ChanFourStateBlendStrategy.version`
moves `1.4.0 -> 1.5.0` (`BASE_VERSION` stays `2.0.0`). A version bump is exactly what produced
three archived deploy failures (`LiveDumpPreflightError` on 2026-11-06 / 11-09 `base_version`,
2026-11-12 `strategy_version`), so the transition itself is pinned here:

1. **The persisted pin is per-strategy.** Exactly one entry declares
   `cfsb_vol_ceiling_scope`, it is `chan_four_state_blend`, it says `portfolio`, and the
   overlay's own keys (`enabled` / `mode = cap`) are untouched. `StrategyConfig`'s DEFAULT
   stays `stage1`, so every other strategy and every legacy dump is unchanged.
2. **The dump the live run reads resolves to `portfolio` from `source="dump"`.** Built through
   `instantiate_strategy_from_config_entry` (the same constructor `--dump-strategies` uses)
   plus the runner's own resolver -- no second implementation of the resolution order.
3. **A `strategy_version` bump cannot cold-start the book.** `select_prior_state` gates on
   `base_version` ONLY; the carried snapshot keeps its own (older) `strategy_version` as
   lineage evidence and stays admissible. The `base_version` gate is **MAJOR-scoped**
   (`NV8`, owner decision A 2026-11-17): a patch/minor bump is the same engine era and is
   admissible, while a DIFFERENT MAJOR is still refused. That tolerance is what makes the
   forthcoming `BASE_VERSION` bump safe; it is why `BASE_VERSION` was left at `2.0.0` here.
4. **The round's own state must be re-stamped, and that write is load-bearing.** The seed
   copied from the previous snapshot carries the OLD version; `stamp_account_state_version`
   (Stage 1, from the round's dump triple) makes the parity gate pass. Without the stamp the
   gate refuses -- fail-closed, never auto-healed.
5. **Dump-vs-live parity, when the dump exists.** `assert_dump_version_parity` against the real
   dumped file: a stale dump (`1.4.0` against a `1.5.0` class) is the 2026-11-12 failure mode,
   so this asserts the re-dump actually happened on this machine. Skipped where the gitignored
   `results/` tree is absent (fresh clone / CI), never faked.

100% offline: temp dirs, JSON fixtures and in-memory dicts only.
"""

import json
import os
import sys

import pytest
from dataclasses import fields as dataclass_fields

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.live_preflight import (  # noqa: E402
    SKIP_BASE_VERSION_MISMATCH,
    LiveDumpPreflightError,
    assert_account_state_version_parity,
    assert_dump_version_parity,
    detect_state_version_clobber,
    select_prior_state,
    stamp_account_state_version,
    version_triple_from_strategy_def,
)
from scripts.run_live_strategy import (  # noqa: E402
    VOL_CEILING_SCOPE_PARAM,
    VOL_CEILING_SCOPE_PORTFOLIO,
    VOL_CEILING_SCOPE_STAGE1,
    resolve_vol_ceiling_scope,
)
from pipeline.research_strategy.rs.config import StrategyConfig  # noqa: E402
from pipeline.research_strategy.rs.strategy import (  # noqa: E402
    instantiate_strategy_from_config_entry,
)
from pipeline.research.blend.chan_four_state_blend import (  # noqa: E402
    ChanFourStateBlendStrategy,
)

STRATEGY_KEY = "chan_four_state_blend"
CONFIG_PATH = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json"
)
DUMP_PATH = os.path.join(
    _REPO_ROOT,
    "pipeline",
    "research_strategy",
    "results",
    "strategy_dumps",
    "chan_four_state_blend_strategy.json",
)


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _make_snapshot(root, run_date, *, strategy_version, base_version, status="SUCCESS"):
    """One flat `<root>/<date>/` archive instance, shaped the way the resolver reads it."""
    run_dir = os.path.join(str(root), run_date)
    os.makedirs(run_dir, exist_ok=True)
    _write_json(
        os.path.join(run_dir, "account_state.json"),
        {
            "as_of_date": run_date,
            "current_nav": 196789.34,
            "strategy_version": strategy_version,
            "base_version": base_version,
        },
    )
    _write_json(os.path.join(run_dir, "summary.json"), {"deploy_status": status})
    return run_dir


# ---------------------------------------------------------------------------------------
# 1. The persisted pin (per-strategy, not a global default flip)
# ---------------------------------------------------------------------------------------


def test_scope_is_pinned_only_on_chan_four_state_blend():
    cfg = _load_config()
    declaring = {
        key: entry.get("parameters", {}).get(VOL_CEILING_SCOPE_PARAM)
        for key, entry in cfg.items()
        if isinstance(entry, dict) and VOL_CEILING_SCOPE_PARAM in entry.get("parameters", {})
    }
    assert declaring == {STRATEGY_KEY: VOL_CEILING_SCOPE_PORTFOLIO}


def test_overlay_enabled_and_cap_mode_are_untouched():
    params = _load_config()[STRATEGY_KEY]["parameters"]
    assert params["cfsb_portfolio_vol_overlay_enabled"] is True
    assert params["cfsb_portfolio_vol_overlay_mode"] == "cap"
    assert params["cfsb_portfolio_vol_ceiling_force_exits"] is True


def test_global_default_stays_stage1_and_round_trips_through_from_dict():
    # Every OTHER strategy -- and every legacy dump with no such key -- must be unchanged.
    assert StrategyConfig().cfsb_vol_ceiling_scope == VOL_CEILING_SCOPE_STAGE1
    pinned = _load_config()[STRATEGY_KEY]["parameters"]
    assert StrategyConfig.from_dict(pinned).cfsb_vol_ceiling_scope == VOL_CEILING_SCOPE_PORTFOLIO
    # The key must be a REAL dataclass field, not an "unknown key" that from_dict drops.
    assert "cfsb_vol_ceiling_scope" in {f.name for f in dataclass_fields(StrategyConfig)}


# ---------------------------------------------------------------------------------------
# 2. What the live run resolves out of the re-dumped file
# ---------------------------------------------------------------------------------------


def test_the_dumped_params_resolve_to_portfolio_from_the_dump():
    entry = _load_config()[STRATEGY_KEY]
    # Same constructor `--dump-strategies` uses: the dump's `params` block IS this
    # entry's `parameters` block.
    strat = instantiate_strategy_from_config_entry(STRATEGY_KEY, entry)
    params = dict(entry["parameters"])
    resolution = resolve_vol_ceiling_scope(STRATEGY_KEY, params)
    assert resolution["scope"] == VOL_CEILING_SCOPE_PORTFOLIO
    assert resolution["source"] == "dump"
    assert resolution["declared"] == VOL_CEILING_SCOPE_PORTFOLIO
    assert resolution["fallback_reason"] is None
    assert resolution["cli_override"] is False
    # The instance really is the class under audit, at the version the bump claims.
    assert isinstance(strat, ChanFourStateBlendStrategy)
    assert strat.version == ChanFourStateBlendStrategy.version


def test_a_non_allowlisted_strategy_declaring_the_same_key_is_unaffected():
    params = dict(_load_config()[STRATEGY_KEY]["parameters"])
    resolution = resolve_vol_ceiling_scope("turtle_breakout", params)
    assert resolution["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert resolution["source"] == "default"
    assert resolution["allowlisted"] is False


# ---------------------------------------------------------------------------------------
# 3. A `strategy_version` bump cannot cold-start the book
# ---------------------------------------------------------------------------------------


def _previous_release_version() -> str:
    """A version that is guaranteed to differ from the live class's own.

    Derived from the live value rather than hard-coded, so this pin survives the NEXT bump:
    the invariant under test is "a `strategy_version` change is not an admissibility axis",
    not any particular pair of numbers.
    """
    major, minor, patch = (int(p) for p in ChanFourStateBlendStrategy.version.split("."))
    return f"{major}.{minor - 1}.{patch}" if minor > 0 else f"{major}.0.{patch + 1}"


def test_prior_state_admissibility_ignores_strategy_version(tmp_path):
    live = ChanFourStateBlendStrategy()
    old_version = _previous_release_version()
    assert old_version != live.version
    _make_snapshot(
        tmp_path,
        "2026-11-16",
        strategy_version=old_version,
        base_version=live.base_version,
    )

    selection = select_prior_state(
        str(tmp_path), "2026-11-17", expected_base_version=live.base_version
    )

    assert selection.state is not None, selection.log_line
    assert selection.source["run_date"] == "2026-11-16"
    # The snapshot keeps the OLD triple: it is lineage evidence, not something to re-stamp.
    assert selection.state["strategy_version"] == old_version
    assert selection.state["base_version"] == live.base_version
    assert selection.source["skipped"] == []


def test_a_base_version_bump_cold_starts_the_book_only_across_a_major(tmp_path):
    """The `base_version` axis is MAJOR-scoped (`NV8`), not exact-string.

    A patch/minor bump is the same engine era, so the previous snapshot stays admissible and
    the book does NOT cold-start (owner decision A, 2026-11-17: make the tolerance land
    BEFORE the forthcoming `BASE_VERSION` bump). A DIFFERENT MAJOR is still refused -- that
    is the half of the guard `BASE_VERSION` 2.0.0 -> 3.0.0 would trip, and it is pinned here
    so nobody can widen the tolerance into "any bump is fine".
    """
    live = ChanFourStateBlendStrategy()
    major, minor, patch = (int(p) for p in live.base_version.split("."))
    same_root = tmp_path / "same_major"
    cross_root = tmp_path / "cross_major"
    same_root.mkdir()
    cross_root.mkdir()

    # Same major: MINOR and PATCH bumps both stay admissible, in both directions.
    for bumped in (
        f"{major}.{minor + 1}.{patch}",
        f"{major}.{minor}.{patch + 1}",
    ):
        _make_snapshot(
            same_root,
            "2026-11-16",
            strategy_version=live.version,
            base_version=bumped,
            status="SUCCESS",
        )
        allowed = select_prior_state(
            str(same_root), "2026-11-17", expected_base_version=bumped
        )
        assert allowed.state is not None, allowed.log_line  # same string -> admissible
        upgraded = select_prior_state(
            str(same_root), "2026-11-17", expected_base_version=live.base_version
        )
        assert upgraded.state is not None, upgraded.log_line  # same major -> still admissible

    # Different major: refused, with the pre-existing mismatch reason.
    _make_snapshot(
        cross_root,
        "2026-11-16",
        strategy_version=live.version,
        base_version=f"{major + 1}.0.0",
        status="SUCCESS",
    )
    refused = select_prior_state(
        str(cross_root), "2026-11-17", expected_base_version=live.base_version
    )
    assert refused.state is None
    assert refused.source is None
    reasons = [c.reason for c in refused.candidates]
    assert reasons == [SKIP_BASE_VERSION_MISMATCH]


# ---------------------------------------------------------------------------------------
# 4. The re-stamp is load-bearing (and fail-closed without it)
# ---------------------------------------------------------------------------------------


def _round_triple():
    return version_triple_from_strategy_def(
        {
            "strategy_version": ChanFourStateBlendStrategy.version,
            "base_version": ChanFourStateBlendStrategy().base_version,
            "version_info": {"version": ChanFourStateBlendStrategy.version},
        }
    )


def test_seeded_old_version_state_is_refused_until_the_round_stamps_its_own():
    old_version = _previous_release_version()
    seeded = {
        "as_of_date": "2026-11-17",
        "current_nav": 196789.34,
        "strategy_version": old_version,
        "base_version": ChanFourStateBlendStrategy().base_version,
    }

    # A state claiming TODAY with yesterday's engine version is the 2026-11-12 refusal.
    with pytest.raises(LiveDumpPreflightError):
        assert_account_state_version_parity(
            seeded,
            ChanFourStateBlendStrategy.version,
            ChanFourStateBlendStrategy().base_version,
            state_display="seeded copy",
        )
    assert detect_state_version_clobber(
        seeded,
        ChanFourStateBlendStrategy.version,
        ChanFourStateBlendStrategy().base_version,
        as_of_date="2026-11-17",
    )

    # Stage 1's stamp (from the round's DUMP triple, never the carried state) clears it.
    stamp_account_state_version(seeded, _round_triple())
    assert seeded["strategy_version"] == ChanFourStateBlendStrategy.version
    assert_account_state_version_parity(
        seeded,
        ChanFourStateBlendStrategy.version,
        ChanFourStateBlendStrategy().base_version,
        state_display="stamped",
    )
    assert (
        detect_state_version_clobber(
            seeded,
            ChanFourStateBlendStrategy.version,
            ChanFourStateBlendStrategy().base_version,
            as_of_date="2026-11-17",
        )
        is None
    )


def test_stamping_never_touches_the_carried_snapshot_block():
    old_version = _previous_release_version()
    state = {
        "as_of_date": "2026-11-17",
        "prior_state": {
            "as_of_date": "2026-11-16",
            "strategy_version": old_version,
            "base_version": ChanFourStateBlendStrategy().base_version,
        },
    }
    stamp_account_state_version(state, _round_triple())
    assert state["prior_state"]["strategy_version"] == old_version
    assert state["strategy_version"] == ChanFourStateBlendStrategy.version


# ---------------------------------------------------------------------------------------
# 5. Dump-vs-live parity on this machine (skipped when the gitignored dumps are absent)
# ---------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists(DUMP_PATH),
    reason="strategy_dumps/ is a generated (gitignored) artifact; run --dump-strategies",
)
def test_real_dump_agrees_with_the_live_class_and_carries_the_pin():
    with open(DUMP_PATH, "r", encoding="utf-8") as f:
        dump = json.load(f)
    live = ChanFourStateBlendStrategy()
    # The preflight's own read-only gate -- the exact comparison Stage 2 runs.
    assert_dump_version_parity(
        dump,
        live.version,
        live.base_version,
        dump_display=DUMP_PATH,
    )
    assert dump["params"][VOL_CEILING_SCOPE_PARAM] == VOL_CEILING_SCOPE_PORTFOLIO
