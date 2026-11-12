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
4. `is_static_template()` itself is untouched: the set has 14 entries (`sleeve_allocator`
   added 2026-11-05 with the M2.2 core-satellite template) and four of them
   (`momentum`, `hrp`, `inv_vol`, `ew`) are aliases rather than `AllocationTemplate.name`
   values; the 13 pre-existing names stay byte-identical (an existing test pins them).
   A CHECKED strategy that pins no `blend_weighting_mode` still returns `verified=True` --
   being checked is not the same as being suspect.
"""

import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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
    "sleeve_allocator",
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


def _normalise_config_keys(keys: Any) -> frozenset:
    """Canonicalise an iterable of config top-level keys: `str(k).strip().lower()`.

    (P1.9 audit follow-up, 2026-10-24) One pure helper, two callers: the cached
    `_config_strategy_keys` loader and the verifier's already-loaded `cfg_data`. Before
    this, both built the same comprehension inline; a future edit to one side (or to the
    other) would have silently split the collision test in two.

    Deliberately NOT a cached accessor: the verifier must use the keys of the config it
    has ALREADY loaded (`config_path` may point at a synthesised file), so it takes the
    already-parsed `cfg_data` and normalises it here -- no extra read, and no chance of
    the per-path cache leaking a different file's keys into a live check.

    Args:
        keys: Any iterable of raw key objects (dict keys, list of strings, ...).

    Returns:
        frozenset of NORMALISED keys.
    """
    return frozenset(str(k).strip().lower() for k in keys)


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
            _normalise_config_keys(data.keys()) if isinstance(data, dict) else frozenset()
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
    1. it is a static basket template name (`is_static_template` -- 14 entries, the last four
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

    # (P1.9 audit follow-up, 2026-10-24) Strip the identity key, matching the normalisation
    # the static-exemption test already applies to every name. Two behaviour deltas, both
    # message/identity only and both invisible on real artifacts (every tracked dump name is
    # already stripped/lowercase -- the 58-dump verdict distribution is unchanged):
    #   1. a whitespace-padded name is now reported as `'X'`, not `'  X  '`; an all-whitespace
    #      name becomes the honest "cannot identify" instead of "strategy key '   ' is not
    #      defined";
    #   2. a padded name that equals a REAL config key (`'  dual_momentum  '`) now resolves to
    #      that entry instead of being refused as unknown -- which is the intended reading: the
    #      exemption test already treated it as the colliding name, so it is CHECKED (not
    #      exempt) and either compared against the pin or admitted by the no-pin branch.
    # Only a str is stripped -- a non-str identity (e.g. a numeric spec key) keeps its old
    # behaviour rather than raising.
    if isinstance(key, str):
        key = key.strip()

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
        _normalise_config_keys(cfg_data.keys()) if isinstance(cfg_data, dict) else frozenset()
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


# ---------------------------------------------------------------------------
# (LV7) Version-metadata parity -- READ-ONLY cross-check, fail closed
# ---------------------------------------------------------------------------
# Frozen contract: `docs/lv7_prior_state_contract.md` §5.
#
# Why this exists at all: `resolve_strategy_definition` computes
#     strat_version = getattr(template, "version", None) or strategy_def.get("strategy_version")
# i.e. the LIVE CLASS wins and the result is then stamped BACK over the dump's recorded
# value. An inconsistency ("the dump says 1.2.2, the running class is 1.3.0") therefore
# could never be observed: it was silently overwritten, and the ticket printed a version
# number that no persisted artifact agreed with. That is exactly how the 2026-11-02
# archive ended up carrying two coexisting version pairs (ticket 1.2.2 / 1.7.0 alongside
# a dump at 1.8.0). The check below runs BEFORE that overwrite, so "compare then take"
# replaces "take then pretend".
#
# Rules (do not soften any of them):
#   * Key ABSENT in the recorded carrier -> nothing was asserted -> no comparison. A dump
#     that does not claim a version cannot be lying about one; comparing it against the
#     live class would manufacture a failure for every legacy/config-synthesised carrier.
#   * Key PRESENT but empty (`null` / "" / whitespace) while the live class carries a
#     value -> raise. An explicitly recorded yet valueless field is a malformed record,
#     not "no claim". (`base_version: null` is the dumper's canonical spelling for a
#     non-blend strategy, so null == live None is EQUAL, not empty.)
#   * Both sides carry a value and they differ -> raise.
#   * Either side is unverifiable because the OTHER side has a value the recorded side
#     cannot confirm -> raise (e.g. recorded 1.7.0 vs live None).
#
# NEVER auto-heal: no function here writes a version back, and the
# `--allow-weighting-mode-override` escape hatch does NOT apply to the version axis
# (that flag only covers the pinned weighting mode).


def _normalise_version_value(value: Any) -> Optional[str]:
    """`None`/blank -> `None`; otherwise the stripped string form."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _assert_version_axis(
    *,
    container: Dict[str, Any],
    key: str,
    axis: str,
    live_value: Any,
    context: str,
) -> None:
    """Compare ONE recorded version field against the live class's own value."""
    if key not in container:
        # Nothing asserted on this axis -- see the rules above.
        return

    recorded = _normalise_version_value(container.get(key))
    live = _normalise_version_value(live_value)

    if recorded is None and live is None:
        # Canonical spelling for "this carrier/class has no such axis" (non-blend).
        return

    if recorded is None:
        raise LiveDumpPreflightError(
            f"版本元数据不一致（{axis}）：{context} 显式携带 `{key}` 但取值为空/不可解析，"
            f"而今日活类为 {live!r}。无法交叉校验的记录不得当作一致。"
            "该检查失败关闭，且不可被 --allow-weighting-mode-override 放行。"
        )

    if live is None:
        raise LiveDumpPreflightError(
            f"版本元数据不一致（{axis}）：{context} 记录 `{key}` = {recorded!r}，"
            f"但今日活类没有对应的 {axis}（None）。无法交叉校验的记录不得当作一致。"
            "该检查失败关闭，且不可被 --allow-weighting-mode-override 放行。"
        )

    if recorded != live:
        raise LiveDumpPreflightError(
            f"版本元数据不一致（{axis}）：{context} 记录 `{key}` = {recorded!r}，"
            f"而今日活类 {axis} = {live!r}。票据不得以旧引擎的版本号写出；"
            "本条**不会**自动改写任何版本字段（禁止重新盖章）——"
            "请重新生成 dump，或按 docs/lv7_prior_state_contract.md §5.4 由操作者显式处理。"
        )


def assert_dump_version_parity(
    strategy_def: Dict[str, Any],
    live_strategy_version: Any,
    live_base_version: Any = None,
    *,
    dump_display: str = "",
) -> None:
    """Raise `LiveDumpPreflightError` when the persisted dump's versions disagree with the live class.

    Read-only: nothing in `strategy_def` is mutated. Call this BEFORE
    `resolve_strategy_definition` stamps the live class's values over the recorded ones.
    """
    context = f"策略 dump `{dump_display or '<未命名 dump>'}`"
    _assert_version_axis(
        container=strategy_def,
        key="strategy_version",
        axis="strategy_version",
        live_value=live_strategy_version,
        context=context,
    )
    _assert_version_axis(
        container=strategy_def,
        key="base_version",
        axis="base_version",
        live_value=live_base_version,
        context=context,
    )


def assert_account_state_version_parity(
    account_state: Dict[str, Any],
    live_strategy_version: Any,
    live_base_version: Any = None,
    *,
    state_display: str = "",
) -> None:
    """Raise when the carried-forward `account_state.json` was written by a different engine version.

    Read-only. Called twice per run (before the ticket exists, and again immediately
    before persisting) because the state dict is heavily mutated in between.
    """
    if not isinstance(account_state, dict) or not account_state:
        return
    context = f"账户状态 `{state_display or '<未命名 account_state>'}`"
    _assert_version_axis(
        container=account_state,
        key="strategy_version",
        axis="strategy_version",
        live_value=live_strategy_version,
        context=context,
    )
    _assert_version_axis(
        container=account_state,
        key="base_version",
        axis="base_version",
        live_value=live_base_version,
        context=context,
    )


# ---------------------------------------------------------------------------
# (D2) `prior_state` selection -- newest SUCCESSFUL, version-consistent snapshot
# ---------------------------------------------------------------------------
# WHY THIS LIVES HERE.  `prior_state` is supposed to be "the previous session's CONFIRMED
# snapshot" (`docs/lv7_prior_state_contract.md` §1).  The 2026-11-09 archive
# (`2026-11-09/204956/account_state.json`) carried `prior_state.as_of_date = 2026-11-05`
# while a *successful* 2026-11-06 snapshot existed at `2026-11-06/185016/` -- so
# `nav_history_10d` never received 11-06's 199,480.90, the 10-day recovery measure fell to
# `INSUFFICIENT_HISTORY`, and the "-5.23%" printed on 11-09 was really a two-session move.
# The sibling `2026-11-06/184826/` was a FAILED deploy (`nav=186,476.57`), and the
# 2026-11-08 instance was an orphan copy of the 11-05 book.
#
# THE RULE IMPLEMENTED BELOW (and the reason it is a *resolver*, not a "walk to the previous
# dated directory"):
#   1. Every candidate is a directory that actually holds an `account_state.json`
#      (`<date>/` or `<date>/<HHMMSS>/`).  There is NO implicit "date-root-level file"
#      fallback: a date whose only snapshots live in instance subdirectories is a normal
#      candidate, not a reason to jump to an older date (that implicit fallback is exactly
#      what skipped 11-06/185016).
#   2. A candidate is INADMISSIBLE when its run verdict is a recorded non-SUCCESS
#      (`deploy_status` wins, else `health_status`), when the state's OWN `as_of_date` is
#      not the directory's date (the orphan signature: an 11-05 book sitting in an 11-08
#      directory), or when its `base_version` disagrees with today's live engine.
#   3. Among admissible candidates the winner is the NEWEST date, and within one date the
#      NEWEST instance.  A failed newest instance falls back to the next-newest instance of
#      the SAME date before any older date is considered.
#   4. Every rejection is recorded with a machine-readable reason, so a FAILED snapshot can
#      never be skipped silently.
#
# READ-ONLY with respect to the archive: nothing here writes, repairs or re-stamps a
# snapshot.  `apply_prior_state_selection` only mutates the in-memory state dict that the
# caller is about to persist for TODAY's round.

PRIOR_STATE_LOG_PREFIX = "[PRIOR STATE]"

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTANCE_DIR_RE = re.compile(r"^\d{6}$")

#: Machine-readable skip reasons (`prior_state_source["skipped"][*]["reason"]`).
SKIP_STATUS_NOT_SUCCESS = "run_status_not_success"
SKIP_AS_OF_MISMATCH = "as_of_date_does_not_match_the_run_date"
SKIP_AS_OF_MISSING = "as_of_date_missing"
SKIP_BASE_VERSION_MISMATCH = "base_version_mismatch"
SKIP_BASE_VERSION_MISSING = "base_version_missing"
SKIP_UNREADABLE = "account_state_unreadable_or_empty"


class PriorStateCandidate(NamedTuple):
    """One snapshot directory considered as a `prior_state` source."""

    path: str
    run_date: str
    instance: str
    run_status: Optional[str]
    status_source: Optional[str]
    base_version: Optional[str]
    as_of: Optional[str]
    eligible: bool
    reason: str

    def as_skip_record(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "run_date": self.run_date,
            "instance": self.instance,
            "run_status": self.run_status,
            "base_version": self.base_version,
            "as_of": self.as_of,
            "reason": self.reason,
        }


class PriorStateSelection(NamedTuple):
    """Outcome of one `prior_state` resolution (`state=None` -> nothing admissible)."""

    state: Optional[Dict[str, Any]]
    source: Optional[Dict[str, Any]]
    candidates: Tuple[PriorStateCandidate, ...]
    log_line: str


def _load_json_object(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def snapshot_run_status(run_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """`(status, source_key)` for one run dir from its `summary.json`, or `(None, None)`.

    `deploy_status` wins over `health_status` because a round that never produced a Stage-2
    ticket has no post-trade book to carry forward: `2026-11-06/184826` is
    `health_status=SUCCESS, deploy_status=FAILED` and its NAV (186,476.57) is a Stage-1
    placeholder, not a settlement.  A directory with no readable summary keeps `(None, None)`
    -- "no verdict recorded" (legacy archive), never "failed".
    """
    summary = _load_json_object(os.path.join(str(run_dir), "summary.json"))
    if not summary:
        return None, None
    for key in ("deploy_status", "health_status"):
        value = summary.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().upper(), key
    return None, None


def _instance_sort_key(run_dir: str) -> str:
    """Chronological key for one instance dir: summary `timestamp` first, name as tiebreak."""
    summary = _load_json_object(os.path.join(str(run_dir), "summary.json"))
    stamp = ""
    if summary:
        stamp = str(summary.get("timestamp") or summary.get("date") or "")
    return f"{stamp}|{os.path.basename(str(run_dir))}"


def derive_session_root(output_dir: Optional[str]) -> Optional[str]:
    """`<...>/<strategy>/<universe>` for a run dir, or None when the shape is not an archive.

    The live archive layout is `<root>/<strategy>/<universe>/<YYYY-MM-DD>[/<HHMMSS>]/`
    (`web/livetrading/storage.py::allocate_run_instance_dir`).  Anything else -- notably the
    CLI default `scripts/results/live/<strategy_key>` -- returns None so the resolver simply
    does not run; it never guesses a parent and scans unrelated directories.
    """
    if not output_dir:
        return None
    path = os.path.abspath(str(output_dir))
    if _INSTANCE_DIR_RE.match(os.path.basename(path)):
        path = os.path.dirname(path)
    if not _DATE_DIR_RE.match(os.path.basename(path)):
        return None
    parent = os.path.dirname(path)
    return parent or None


def list_prior_state_candidates(
    session_root: Optional[str],
    as_of_date: str,
    *,
    expected_base_version: Any = None,
) -> List[PriorStateCandidate]:
    """All snapshot dirs strictly BEFORE `as_of_date`, NEWEST date first, newest instance first.

    "Strictly before" is the `prior_state` contract (previous session's confirmed snapshot);
    today's own earlier instances are the same session and are covered by the carried state
    itself, not by this resolver.
    """
    if not session_root or not os.path.isdir(str(session_root)) or not as_of_date:
        return []
    expected = _normalise_version_value(expected_base_version)
    date_dirs = sorted(
        (
            name
            for name in os.listdir(str(session_root))
            if _DATE_DIR_RE.match(name) and name < str(as_of_date)
        ),
        reverse=True,
    )
    out: List[PriorStateCandidate] = []
    for run_date in date_dirs:
        date_dir = os.path.join(str(session_root), run_date)
        if not os.path.isdir(date_dir):
            continue
        instance_dirs: List[str] = []
        if os.path.exists(os.path.join(date_dir, "account_state.json")):
            instance_dirs.append(date_dir)
        instance_dirs.extend(
            sorted(
                (
                    os.path.join(date_dir, name)
                    for name in os.listdir(date_dir)
                    if _INSTANCE_DIR_RE.match(name)
                    and os.path.isdir(os.path.join(date_dir, name))
                ),
                key=_instance_sort_key,
                reverse=True,
            )
        )
        for run_dir in instance_dirs:
            state_path = os.path.join(run_dir, "account_state.json")
            if not os.path.exists(state_path):
                continue
            state = _load_json_object(state_path)
            instance_label = "flat" if run_dir == date_dir else os.path.basename(run_dir)
            if not state:
                out.append(
                    PriorStateCandidate(
                        path=state_path,
                        run_date=run_date,
                        instance=instance_label,
                        run_status=None,
                        status_source=None,
                        base_version=None,
                        as_of=None,
                        eligible=False,
                        reason=SKIP_UNREADABLE,
                    )
                )
                continue
            status, status_source = snapshot_run_status(run_dir)
            base_version = _normalise_version_value(state.get("base_version"))
            as_of = _normalise_version_value(state.get("as_of_date"))
            eligible, reason = True, "eligible"
            if status is not None and status != "SUCCESS":
                eligible, reason = False, SKIP_STATUS_NOT_SUCCESS
            elif as_of is None:
                eligible, reason = False, SKIP_AS_OF_MISSING
            elif as_of != run_date:
                eligible, reason = False, SKIP_AS_OF_MISMATCH
            elif expected is not None and base_version != expected:
                eligible = False
                reason = (
                    SKIP_BASE_VERSION_MISMATCH if base_version else SKIP_BASE_VERSION_MISSING
                )
            out.append(
                PriorStateCandidate(
                    path=state_path,
                    run_date=run_date,
                    instance=instance_label,
                    run_status=status,
                    status_source=status_source,
                    base_version=base_version,
                    as_of=as_of,
                    eligible=eligible,
                    reason=reason,
                )
            )
    return out


def select_prior_state(
    session_root: Optional[str],
    as_of_date: str,
    *,
    expected_base_version: Any = None,
) -> PriorStateSelection:
    """Resolve the newest admissible `prior_state` snapshot (see the section header)."""
    candidates = tuple(
        list_prior_state_candidates(
            session_root, as_of_date, expected_base_version=expected_base_version
        )
    )
    chosen = next((c for c in candidates if c.eligible), None)
    if chosen is None:
        reason = (
            "no_candidate_directories_under_session_root"
            if not candidates
            else "no_admissible_candidate"
        )
        return PriorStateSelection(
            state=None,
            source=None,
            candidates=candidates,
            log_line=(
                f"{PRIOR_STATE_LOG_PREFIX} unresolved as_of={as_of_date} "
                f"session_root={session_root or '<none>'} reason={reason} "
                f"considered={len(candidates)} -- 保留既有 prior_state 不变"
            ),
        )
    state = _load_json_object(chosen.path)
    skipped = [c.as_skip_record() for c in candidates if not c.eligible]
    source = {
        "path": chosen.path,
        "as_of": chosen.as_of,
        "run_date": chosen.run_date,
        "instance": chosen.instance,
        "run_status": chosen.run_status,
        "status_source": chosen.status_source,
        "base_version": chosen.base_version,
        "selection_reason": "newest_successful_snapshot_at_or_before_as_of",
        "skipped": skipped,
    }
    # The console/summary line names the first few rejections; the FULL list (with reasons)
    # is in `prior_state_source["skipped"]` so nothing is silently dropped, while a long
    # carry-forward chain cannot bloat `deploy_stdout` into unreadability.
    shown = skipped[:6]
    skip_text = ", ".join(
        f"{s['run_date']}/{s['instance']}:{s['reason']}" for s in shown
    )
    if len(skipped) > len(shown):
        skip_text += f", +{len(skipped) - len(shown)} more (see prior_state_source.skipped)"
    return PriorStateSelection(
        state=state,
        source=source,
        candidates=candidates,
        log_line=(
            f"{PRIOR_STATE_LOG_PREFIX} selected path={chosen.path} as_of={chosen.as_of} "
            f"run_date={chosen.run_date} instance={chosen.instance} "
            f"run_status={chosen.run_status or '<no verdict>'} "
            f"({chosen.status_source or 'legacy'}) base_version={chosen.base_version} "
            f"reason={source['selection_reason']} skipped={len(skipped)}"
            + (f" skips=[{skip_text}]" if skipped else "")
        ),
    )


def _same_snapshot(left: Any, right: Any) -> bool:
    """`True` when two state dicts describe the same snapshot (date + NAV, exact)."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if str(left.get("as_of_date") or "") != str(right.get("as_of_date") or ""):
        return False
    try:
        return float(left.get("current_nav")) == float(right.get("current_nav"))
    except (TypeError, ValueError):
        return False


def apply_prior_state_selection(
    account_state: Dict[str, Any], selection: PriorStateSelection
) -> Optional[str]:
    """Install the resolved snapshot as `prior_state` and record where it came from.

    Returns the log line to print (also meant for `summary.json`), or None when this
    resolver had nothing to say.  `account_state` is mutated in place; when no admissible
    candidate exists the carried `prior_state` is left EXACTLY as it was (an unresolved
    selection must never destroy state).
    """
    if not isinstance(account_state, dict):
        return None
    if selection.source is None:
        # Unresolved: report it, but never destroy the carried block.
        return selection.log_line
    carried = account_state.get("prior_state")
    if _same_snapshot(carried, selection.state):
        action = "kept"
        reason = "carried_prior_state_is_the_newest_admissible_snapshot"
    else:
        action = "replaced"
        reason = "carried_prior_state_skipped_a_newer_admissible_snapshot"
        if not isinstance(carried, dict):
            reason = "carried_prior_state_missing"
        account_state["prior_state"] = copy.deepcopy(selection.state)
    source = dict(selection.source)
    source["selection_reason"] = reason
    source["carried_prior_state_as_of"] = (
        str(carried.get("as_of_date")) if isinstance(carried, dict) and carried.get("as_of_date") else None
    )
    account_state["prior_state_source"] = source
    return (
        f"{selection.log_line} action={action} carried_as_of={source['carried_prior_state_as_of']} "
        f"reason={reason}"
    )
