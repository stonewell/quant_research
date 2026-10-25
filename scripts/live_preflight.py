#!/usr/bin/env python3
"""Pre-flight consistency check between live strategy dumps and strategies_config.json.

Why this module exists
----------------------
(P1.8 / 2026-10-24) In live trading execution (Stage 1 `scripts/check_live_portfolio_health.py`,
Stage 2 `scripts/run_live_strategy.py`, and netted execution `scripts/run_live_multi_strategy.py`),
strategy parameters may be loaded from serialized dump files (*_strategy.json) or CLI arguments.
A silent drift between the strategy dump's weighting mode (e.g. blend_weighting_mode) and the
canonical pinned configuration in `pipeline/research_strategy/strategies_config.json` can cause
live execution to silently run an unauthorized execution engine (such as rolling_ic_ir instead of
the deployed regime_ic_ir_hybrid).

This module enforces a strict pre-flight gate:
1. Validates that the strategy is defined in `strategies_config.json`.
2. Validates that any pinned `blend_weighting_mode` matches both `params.blend_weighting_mode`
   and `research_strategy_spec.entry_data.parameters.blend_weighting_mode` in the strategy dump
   (a key MISSING from either container counts as a mismatch, never as "nothing to check").
3. Fails fast with `LiveDumpPreflightError` if a mismatch is detected, unless explicitly overridden
   with `--allow-weighting-mode-override`.

Scope of the override (owner decision 2026-10-24, option A): it bypasses ONLY (a) a pinned-value
mismatch and (b) a strategy key that is absent from `strategies_config.json` -- and (b) always
prints a loud one-time stderr warning saying the pinned-mode check did NOT run. It does NOT bypass
a malformed/non-object dump, an unidentifiable dump (no key at all), or a registered strategy whose
`params` carries no `blend_weighting_mode`.

Static-template exemption (P1.9 / 2026-10-24, second pass)
--------------------------------------------------------
Static basket allocation templates are not declared in `strategies_config.json`, so they have no
pin to verify and are exempt. That exemption used to be a bare name-membership test
(`is_static_template`) applied in THIS module and at all three live call sites -- but
`_STATIC_TEMPLATE_NAMES` shares one name with a real config key: `dual_momentum` is both a static
template name and the top-level key of the "Active Dual Momentum GTAA" research entry. A research
dump keyed `dual_momentum` therefore skipped the pinned-mode gate entirely -- harmless today (that
entry pins no `blend_weighting_mode`) but a silent bypass waiting for a pin.

The first pass (option C) exempted a static name when the dump "carried no
`research_strategy_spec`", tested with `bool(strategy_def.get("research_strategy_spec"))` and
applied as `exempt(key) or exempt(template_name)`. That was too weak in four ways, all measured:
a dump with the spec key DELETED, with `spec: {}`, or with a spec whose `strategy_key` was absent
still looked spec-less; a non-dict spec raised `AttributeError` instead of a diagnosable refusal;
and ANY static-looking `template_name` (e.g. `equal_weight`, `hrp`) shielded a spec-bearing
research dump -- even though `common/strategy_spec.py::get_template` gives
`research_strategy_spec` precedence over `template_name`, i.e. the RESEARCH engine ran while the
gate certified the dump as a static template.

The rule enforced now (second pass) is:

1. `research_strategy_spec` is parsed and TYPE-CHECKED once, before anything dereferences it.
   Key absent -> `spec = None`. Key present but not a JSON object -> `LiveDumpPreflightError`
   (never a traceback, never overridable: a malformed spec is indistinguishable from a stripped
   one, so treating it as "no spec" would re-open the exemption).
2. SPEC PRESENCE DECIDES IDENTITY, and a spec-bearing dump is NEVER static-exempt. Its identity
   is `spec["strategy_key"] or strategy_key or template_name`; `template_name` can no longer
   shield it because the exemption is not consulted at all.
3. A no-spec dump is exempt only when EVERY name it carries (`template_name`, `strategy_key`) is
   a static template name AND is NOT a key of `strategies_config.json`. Config membership wins:
   a no-spec dump whose name collides with a config key is CHECKED, not exempted -- the only safe
   reading, since a stripped spec is indistinguishable from the genuine static template. Concretely
   the genuine static `dual_momentum` template is UNREACHABLE through the live resolver anyway
   (`resolve_strategy_definition` matches the config key first and synthesises a
   `research_strategy_spec`), and if that entry were ever pinned, such a dump is now checked
   rather than trusted.
4. `is_static_template()` itself is untouched: the set has 13 entries and the last four
   (`momentum`, `hrp`, `inv_vol`, `ew`) are aliases rather than `AllocationTemplate.name`
   values; all 13 stay byte-identical (an existing test pins them). A CHECKED strategy that
   pins no `blend_weighting_mode` still returns `verified=True` -- being checked is not the
   same as being suspect.
"""

import json
import os
import sys
from typing import Any, Dict, NamedTuple, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The canonical pinned configuration. Same file the verifier reads when no
# `config_path` override is supplied -- one owner, so a test that asks
# "which keys exist?" cannot silently disagree with the gate.
_DEFAULT_CONFIG_PATH = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json"
)

_STATIC_TEMPLATE_NAMES = {
    "equal_weight",
    "inverse_volatility",
    "cross_sectional_momentum",
    "hierarchical_risk_parity",
    "dual_momentum",
    "max_diversification",
    "mean_reversion",
    "minimum_variance",
    "breadth_gated_momentum",
    "momentum",
    "hrp",
    "inv_vol",
    "ew",
}


class LiveDumpPreflightError(ValueError):
    """Raised when a live strategy dump file's weighting mode or configuration
    deviates from strategies_config.json."""
    pass


class PreflightVerdict(NamedTuple):
    """Outcome of a pre-flight check, in enough detail for callers to stay honest.

    A single bool was not enough (owner review 2026-10-24): "the pinned value was
    compared and agreed" and "a registered pin was REJECTED but the override let it
    through" are both non-fatal, and collapsing them makes the Stage 1 artifact claim
    a mode was verified when the configuration explicitly disagreed with it.

    Attributes:
        verified: True when the pinned `blend_weighting_mode` was actually compared and
            agreed -- or when there was no pin to compare against at all (static
            template, non-blend strategy). False means nothing in this run confirmed
            the mode against the pinned configuration.
        mismatch_overridden: True only for a REGISTERED strategy whose dump disagreed
            with the pinned value and was let through by
            `--allow-weighting-mode-override`. `weighting_mode` then still holds the
            dump's real mode -- but it is demonstrably NOT the authorised one.
    """
    verified: bool
    mismatch_overridden: bool = False


# (Owner decision 2026-10-24, option A) `--allow-weighting-mode-override` also exempts a
# strategy key that has NO entry in strategies_config.json -- because otherwise a custom
# `--strategy-file` handed to a live script had NO escape at all. The exemption is NOT
# silent: `_warn_unregistered_override` names the key, the config file, the fact that the
# pinned-value check did not run, and that this is an exploration run. Deduplicated per
# (key, config) so a multi-strategy run prints at most one warning per unregistered key.
_WARNED_UNREGISTERED: set = set()


def _warn_unregistered_override(key: str, dump_display: str, cfg_file: str) -> None:
    """Loud, one-time stderr warning for an unregistered strategy let through by override."""
    dedupe_key = (str(key), str(cfg_file))
    if dedupe_key in _WARNED_UNREGISTERED:
        return
    _WARNED_UNREGISTERED.add(dedupe_key)
    sys.stderr.write(
        "!!! WARNING: UNREGISTERED STRATEGY PASSED BY --allow-weighting-mode-override !!!\n"
        f"Strategy key '{key}' (dump '{dump_display}') is NOT present in config file "
        f"'{cfg_file}'.\n"
        "Pinned-weighting-mode verification did NOT happen for this strategy: there is no "
        "config entry to verify against, so its blend_weighting_mode was accepted as-is.\n"
        "This is an EXPLORATION / 探索 run only. Do NOT archive its output as a live ticket. "
        "Register the strategy key in strategies_config.json for production use.\n"
    )
    sys.stderr.flush()


# (Owner authorisation 2026-10-24, Step 0) Separate from the unregistered path: here the
# strategy IS registered and the pinned mode IS known -- we simply disagree with it and
# the operator overrode the refusal. Leaving this silent (or reporting `verified: true`)
# made stage1_health_report.json read as "the pin approved rolling_ic_ir", which is the
# opposite of what happened. Deduplicated per (key, config, dump mode) so a multi-strategy
# run prints at most one warning per distinct disagreement.
_WARNED_MISMATCH: set = set()


def _warn_mismatch_override(
    key: str,
    dump_mode: Any,
    pinned_mode: str,
    reason: str,
    dump_display: str,
    cfg_file: str,
) -> None:
    """Loud, one-time stderr warning for a pinned-mode mismatch let through by override."""
    dedupe_key = (str(key), str(cfg_file), str(dump_mode))
    if dedupe_key in _WARNED_MISMATCH:
        return
    _WARNED_MISMATCH.add(dedupe_key)
    sys.stderr.write(
        "!!! WARNING: PINNED WEIGHTING MODE MISMATCH PASSED BY --allow-weighting-mode-override !!!\n"
        f"Strategy key '{key}' (dump '{dump_display}'): dump value='{dump_mode}' vs "
        f"pinned value='{pinned_mode}' from '{cfg_file}'.\n"
        f"Reason: {reason}.\n"
        "The pinned-weighting-mode verification FAILED and was overridden: the mode above is "
        "the dump's value, NOT the value configured for live deployment.\n"
        "This is an EXPLORATION / 探索 run only. Do NOT archive its output as a live ticket.\n"
    )
    sys.stderr.flush()


def is_static_template(name: Optional[str]) -> bool:
    """Return True if name corresponds to a static basket allocation template."""
    if not name:
        return False
    return name.strip().lower() in _STATIC_TEMPLATE_NAMES


# Lazy config-key cache, keyed by the RESOLVED absolute path. Why per path instead of a
# single global: tests point the gate at synthesised `tmp_path` configs, and a
# one-slot cache would hand a tmp config's keys to the next real-config call (or vice
# versa) depending on call order -- a test-order-dependent gate. One read per distinct
# file per process is all the gate needs.
_CONFIG_KEYS_CACHE: Dict[str, frozenset] = {}


def _config_strategy_keys(cfg_file: Optional[str] = None) -> frozenset:
    """Return the set of top-level strategy keys declared in `strategies_config.json`.

    (P1.9 / 2026-10-24) This is the "is this name a research strategy?" half of the
    static-template exemption rule. The pre-P1.9 gate decided exemption from the bare
    `_STATIC_TEMPLATE_NAMES` membership test, which made `dual_momentum` -- simultaneously
    a static template name and a top-level key of the "Active Dual Momentum GTAA" entry --
    exempt from the pinned-weighting-mode check even when the dump identified itself as a
    research strategy. Config membership is the authoritative signal that a name is
    owned by `strategies_config.json`, so the exemption has to consult this set.

    Read lazily and cached per resolved path: the verifier prefers the key set from the
    config it has ALREADY loaded, and only falls back here (structural/inspection use).

    (P1.9 nit / 2026-10-24) The returned set is NORMALISED (`str(k).strip().lower()`), the
    same normalisation `is_static_template()` applies to the name. A raw key set made the
    collision test asymmetric: `is_static_template_exempt` canonicalises the name but the
    set was raw, so a config key written `Dual_Momentum` (or padded with whitespace) missed
    the membership test and silently flipped the colliding static name back to EXEMPT --
    re-opening the very bypass this module exists to close. Normalising here means the two
    halves of the test cannot disagree. No live impact today: every tracked key is already
    lowercase, so the normalised and raw sets are identical on real artifacts.

    Args:
        cfg_file: Optional path to the configuration file; defaults to the tracked
            `pipeline/research_strategy/strategies_config.json`.

    Returns:
        frozenset of NORMALISED top-level keys (empty for a non-object / empty JSON document).
    """
    resolved = os.path.abspath(cfg_file or _DEFAULT_CONFIG_PATH)
    cached = _CONFIG_KEYS_CACHE.get(resolved)
    if cached is None:
        with open(resolved, "r", encoding="utf-8") as f:
            data = json.load(f)
        cached = (
            frozenset(str(k).strip().lower() for k in data.keys())
            if isinstance(data, dict)
            else frozenset()
        )
        _CONFIG_KEYS_CACHE[resolved] = cached
    return cached


def is_static_template_exempt(name: Optional[str], config_keys: frozenset) -> bool:
    """Return True if this SINGLE name may skip the pinned-weighting-mode pre-flight.

    (P1.9 second pass / 2026-10-24) The predicate is deliberately one name wide and has no
    "has a research spec" argument: whether an exemption applies AT ALL is decided by spec
    presence in `verify_live_strategy_dump_preflight` (a spec-bearing dump is never exempt),
    so that conflation cannot creep back in here.

    A name is exempt iff:
    1. it is a static basket template name (`is_static_template` -- 13 entries, the last four
       of which, `momentum` / `hrp` / `inv_vol` / `ew`, are aliases rather than
       `AllocationTemplate.name` values; unchanged), AND
    2. it is NOT a top-level key of `strategies_config.json`.

    Why config membership wins (#2): `dual_momentum` is simultaneously a genuine static
    template name and a real config key. Exempting it on the name alone left the pinned-mode
    gate with a hole -- first for spec-bearing dumps (P1.9 first pass), then for dumps whose
    spec had been stripped/emptied (`{}`, deleted key, missing `strategy_key`). A stripped spec
    is indistinguishable from the genuine static payload, so the safe reading is:
    a colliding name is CHECKED. The genuine static `dual_momentum` template cannot reach the
    live scripts anyway -- `resolve_strategy_definition` matches the config key first and
    synthesises a `research_strategy_spec` -- and if that entry were ever given a
    `blend_weighting_mode`, this predicate now refuses to trust it.

    Args:
        name: A `template_name` or `strategy_key` from the dump.
        config_keys: Top-level keys of the configuration being consulted.

    Returns:
        True only for "static template name that owns no config entry".
    """
    if not is_static_template(name):
        return False
    # Same normalisation as `is_static_template`, so the config lookup cannot disagree
    # with the name test (e.g. " Dual_Momentum ").
    canonical = name.strip().lower()  # type: ignore[union-attr]
    return canonical not in config_keys


def _nested_dict_get(container: Any, *keys: str) -> Any:
    """Walk `keys` through nested dicts; return None at the first absent key or non-dict.

    Used for dump-container reads (`params`, `research_strategy_spec.entry_data.parameters`)
    so that a malformed nested container degrades to "key missing" (a diagnosable drift)
    instead of raising `AttributeError` out of the pre-flight gate.
    """
    cur = container
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def verify_live_strategy_dump_preflight(
    strategy_def: Dict[str, Any],
    strategy_key: Optional[str] = None,
    strategy_file: Optional[str] = None,
    config_path: Optional[str] = None,
    allow_override: bool = False,
) -> PreflightVerdict:
    """Verify consistency between a strategy definition/dump and strategies_config.json.

    Args:
        strategy_def: Loaded strategy definition dictionary.
        strategy_key: Strategy key if known (e.g. 'chan_four_state_blend').
        strategy_file: Optional path or description of strategy dump file.
        config_path: Optional override path for strategies_config.json.
        allow_override: If True, bypasses (a) a pinned weighting-mode MISMATCH and
            (b) a strategy key absent from strategies_config.json (with a loud
            one-time stderr warning). It never bypasses an unreadable/non-object
            dump, an unidentifiable dump (no key/template_name), or a strategy that
            IS registered but has no `params.blend_weighting_mode` at all.

    Raises:
        LiveDumpPreflightError: If the dump is malformed (non-object dump, non-object
            `research_strategy_spec`), unidentifiable, its key is missing from the config
            (without the override), or `blend_weighting_mode` does not match the pinned
            configuration. None of the first three are overridable.

    Returns:
        PreflightVerdict: `verified=True` when a pinned value was compared and agreed (or
        there was no pin to compare, e.g. a genuine static template / non-blend strategy).
        For the two override paths `verified=False`, with `mismatch_overridden` telling them
        apart: True for a registered pin that disagreed and was overridden (nothing
        authorised the dump's mode), False for a key absent from `strategies_config.json`
        (nothing could be compared at all). Callers that persist `weighting_mode` into a
        report must publish these facts instead of implying the mode was verified.
    """
    if not isinstance(strategy_def, dict):
        # An unreadable/malformed dump is never an "explore something else" situation --
        # there is nothing to reason about. `--allow-weighting-mode-override` must not
        # turn it into a pass.
        raise LiveDumpPreflightError(
            f"Live strategy dump pre-flight check failed: strategy definition for dump "
            f"'{strategy_file or 'strategy.json'}' is {type(strategy_def).__name__}, not a "
            f"JSON object. A malformed dump cannot be overridden by "
            f"--allow-weighting-mode-override."
        )

    # (P1.9 second pass / 2026-10-24) The spec container is parsed and TYPE-CHECKED exactly
    # once, here, BEFORE anything dereferences it. Key absent -> no spec. Key present but not
    # a JSON object -> a diagnosable refusal, NEVER a traceback and never overridable: an
    # unreadable spec is indistinguishable from a stripped one, so treating it as "no spec"
    # would let the very exemption this module exists to close reappear.
    spec_raw = strategy_def.get("research_strategy_spec")
    if "research_strategy_spec" in strategy_def and not isinstance(spec_raw, dict):
        raise LiveDumpPreflightError(
            f"Live strategy dump pre-flight check failed: 'research_strategy_spec' in dump "
            f"'{strategy_file or strategy_def.get('strategy_file') or 'strategy.json'}' is "
            f"{type(spec_raw).__name__}, not a JSON object. A malformed research_strategy_spec "
            f"cannot be overridden by --allow-weighting-mode-override: it cannot be told apart "
            f"from a stripped/empty spec, so accepting it as 'no spec' would silently exempt a "
            f"research dump from the pinned-weighting-mode check."
        )
    spec = spec_raw if isinstance(spec_raw, dict) else None
    spec_present = spec is not None
    template_name = strategy_def.get("template_name")

    if spec_present:
        # Spec presence decides IDENTITY and a spec-bearing dump is never static-exempt, so
        # `template_name` cannot shield it (the first pass's `or` let `template_name:
        # equal_weight` / `hrp` certify a research dump whose research engine actually ran --
        # `common/strategy_spec.py::get_template` gives research_strategy_spec precedence).
        key = spec.get("strategy_key") or strategy_key or template_name
    else:
        key = strategy_key or template_name

    # Determine dump file display name for diagnostic error messages
    if strategy_file and str(strategy_file).endswith(".json"):
        dump_file_display = str(strategy_file)
    elif strategy_def.get("strategy_file"):
        dump_file_display = str(strategy_def["strategy_file"])
    elif key:
        dump_file_display = f"{key}_strategy.json"
    else:
        dump_file_display = "strategy.json"

    cfg_file = config_path or _DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_file):
        raise LiveDumpPreflightError(
            f"Configuration file not found at: '{cfg_file}' while checking dump '{dump_file_display}'."
        )

    with open(cfg_file, "r", encoding="utf-8") as f:
        cfg_data = json.load(f)

    if not key:
        # Unidentifiable dump: no strategy_key / research_strategy_spec.strategy_key /
        # template_name. There is no config entry to look up and no honest way to warn
        # "we skipped verification for X" -- so this stays fatal under the override too.
        raise LiveDumpPreflightError(
            f"Live strategy dump pre-flight check failed: cannot identify the strategy in dump "
            f"'{dump_file_display}' -- it carries no 'strategy_key', no "
            f"'research_strategy_spec.strategy_key' and no 'template_name'. An unidentifiable "
            f"dump cannot be overridden by --allow-weighting-mode-override."
        )

    # (P1.9 second pass / 2026-10-24) Static-template exemption, decided AFTER the config
    # load (config membership is one of its two inputs) and ONLY for spec-less dumps:
    #
    #   * spec present  -> NEVER exempt, whatever the names say (the research engine runs);
    #   * spec absent   -> exempt only when EVERY name the dump carries is a static template
    #                      name that owns no config key. Using `all()` (not `any()`) is the
    #                      safe direction: a dump mixing a static-looking name with a
    #                      config-key name is ambiguous, so it gets CHECKED.
    #
    # The key set is reused from the config already in memory -- one read, one owner. It is
    # NORMALISED exactly like the name test so the two halves of the collision test cannot
    # disagree (`Dual_Momentum` / padded keys would otherwise look absent and re-exempt).
    # NOTE: this normalised set is used ONLY for the static-name collision test. The
    # registered-strategy lookups below (`key not in cfg_data` / `cfg_data[key]`) keep the
    # RAW key, because they ask "is this exact strategy registered?", not "does this name
    # collide with a static template name".
    config_keys = (
        frozenset(str(k).strip().lower() for k in cfg_data.keys())
        if isinstance(cfg_data, dict)
        else frozenset()
    )
    if not spec_present:
        absent_spec_names = [n for n in (template_name, strategy_key) if n]
        if absent_spec_names and all(
            is_static_template_exempt(n, config_keys) for n in absent_spec_names
        ):
            return PreflightVerdict(verified=True)

    if key not in cfg_data:
        if not allow_override:
            raise LiveDumpPreflightError(
                f"Live strategy dump pre-flight check failed: strategy key '{key}' from dump "
                f"'{dump_file_display}' is not defined in configuration file '{cfg_file}'. "
                f"Escape that exists today: re-run with --allow-weighting-mode-override "
                f"(EXPLORATION / 探索 only -- the pinned blend_weighting_mode will NOT be "
                f"verified and a loud warning is printed), or register the strategy key in "
                f"'{os.path.basename(cfg_file)}' so the pin can be checked."
            )
        # (Owner decision 2026-10-24, option A) The override now also covers an
        # unregistered key -- but loudly, never silently. Verification of the pinned
        # mode is impossible without a config entry, so we say exactly that.
        _warn_unregistered_override(key, dump_file_display, cfg_file)
        # Nothing was verified -- and nothing was even mismatch-overridden (there is no
        # pin to disagree with). The caller must not publish the dump's mode as fact.
        return PreflightVerdict(verified=False, mismatch_overridden=False)

    pinned_mode = cfg_data[key].get("parameters", {}).get("blend_weighting_mode")
    if pinned_mode is None:
        # Strategy does not pin a blend_weighting_mode (e.g. non-blend research strategy)
        return PreflightVerdict(verified=True)

    # (P1.9 second pass) Both containers are read from the ALREADY-VALIDATED spec and via a
    # dict-safe walk: a malformed spec/entry_data must never surface as `AttributeError`
    # (rule: the spec type is validated before anything dereferences it, including this
    # comparison and including the case where a decisive `params` mismatch already exists).
    dump_mode = _nested_dict_get(strategy_def.get("params"), "blend_weighting_mode")
    spec_mode = (
        _nested_dict_get(spec, "entry_data", "parameters", "blend_weighting_mode")
        if spec_present
        else None
    )

    # A registered strategy whose dump has NO `params.blend_weighting_mode` is not a
    # "mode we disagree about" -- there is no value to override. This stays fatal even
    # under --allow-weighting-mode-override (owner decision 2026-10-24, option A, scope
    # limited to mismatch + missing-config-entry). The spec-mirror key, by contrast, is
    # handled as ordinary drift below (overridable, matching the params-mismatch path).
    if dump_mode is None:
        raise LiveDumpPreflightError(
            f"Live strategy dump pre-flight consistency check failed for strategy '{key}': "
            f"dump '{dump_file_display}' has no params.blend_weighting_mode at all, while "
            f"'{cfg_file}' pins blend_weighting_mode='{pinned_mode}'. A registered strategy "
            f"with the params key absent cannot be passed by --allow-weighting-mode-override "
            f"(there is no requested value to override); add the key or fix the dump."
        )

    # Both containers must carry the pinned value. A MISSING spec-mirror key is a
    # mismatch, not a pass: the original `spec_mode is not None` gate meant a dump with a
    # correct `params` but a stripped mirror passed the gate silently -- i.e. half the
    # door stayed open (P1.8 acceptance: "two containers, any drift = inconsistent").
    def _container_mismatch(container: str, actual: Any) -> str:
        if actual is None:
            return (
                f"dump container '{container}' is missing blend_weighting_mode "
                f"(expected pinned blend_weighting_mode='{pinned_mode}'); "
                f"a missing mirror key is treated as drift, not as 'nothing to check'"
            )
        return (
            f"dump {container}='{actual}' does not match "
            f"pinned blend_weighting_mode='{pinned_mode}'"
        )

    mismatch_reason = None
    if dump_mode != pinned_mode:
        mismatch_reason = _container_mismatch("params.blend_weighting_mode", dump_mode)
    elif spec_mode != pinned_mode:
        mismatch_reason = _container_mismatch(
            "research_strategy_spec.entry_data.parameters.blend_weighting_mode", spec_mode
        )

    if mismatch_reason:
        if not allow_override:
            raise LiveDumpPreflightError(
                f"Live strategy dump pre-flight consistency check failed for strategy '{key}': "
                f"{mismatch_reason}. "
                f"Dump file: '{dump_file_display}', Config file: '{cfg_file}'. "
                f"To override this safety gate, specify --allow-weighting-mode-override."
            )
        # (Step 0) Registered pin REJECTED the dump's mode and we are letting it through
        # anyway. Say so out loud, and never let the Stage 1 report call it "verified".
        _warn_mismatch_override(
            key=key,
            dump_mode=dump_mode,
            pinned_mode=pinned_mode,
            reason=mismatch_reason,
            dump_display=dump_file_display,
            cfg_file=cfg_file,
        )
        return PreflightVerdict(verified=False, mismatch_overridden=True)

    # Pinned value compared and agreed.
    return PreflightVerdict(verified=True)
