"""Unit tests for live strategy dump pre-flight consistency checks (P1.8).

Validates:
1. Healthy strategy dump (a synthesised `chan_four_state_blend_strategy.json` written into
   `tmp_path`) passes silently without stdout/stderr output. NOTHING here opens the git-ignored
   serialized-dump directory: a clean clone has no dump file to read (see the `healthy_chan_dump`
   fixture docstring).
2. Mode mismatch in params.blend_weighting_mode fails fast with LiveDumpPreflightError containing:
   - mismatched mode
   - pinned mode
   - dump file path / display
   - config file path / display
3. Mode mismatch in research_strategy_spec.entry_data.parameters.blend_weighting_mode fails fast with LiveDumpPreflightError.
4. A MISSING blend_weighting_mode in the spec mirror (key deleted, or `research_strategy_spec`
   absent entirely) also fails fast -- a missing key is drift, not a pass (2026-10-24 fix).
5. Missing strategy key in strategies_config.json fails fast with LiveDumpPreflightError.
6. allow_override=True / --allow-weighting-mode-override allows (a) a pinned-mode mismatch and
   (b) an unregistered strategy key -- but NEVER silently: both paths emit a loud, deduplicated
   stderr warning, and a mismatch is reported as `verified=False, mismatch_overridden=True` so
   Stage 1 cannot publish it as a verified mode (2026-10-24 Step 0).
7. check_live_portfolio_health.py missing blend_weighting_mode on blend strategy raises LiveDumpPreflightError.
8. Static basket allocation templates (equal_weight, hrp, etc.) pass silently.
9. Single-asset timing / non-blend strategies without pinned mode pass silently.
10. End-to-end CLI integration in run_live_strategy.py, check_live_portfolio_health.py, and run_live_multi_strategy.py.
11. (P1.9, 2026-10-24, two passes + audit follow-up) The static-name COLLISION:
    `dual_momentum` is both a static template name and a `strategies_config.json` key. The
    first pass closed only the spec-present path; the second closes the four remaining shapes
    (stripped/empty spec, non-dict spec crash, `template_name` shields, blank `template_name`
    + config-key `strategy_key`) and pins all 13 shapes as a parametrised probe. Spec presence
    now decides identity: a spec-bearing dump is never static-exempt, and a no-spec dump is
    exempt only when every name it carries is a static name that owns no config key.
    (Audit follow-up, same day) shapes 3/4/5 now carry a NON-config static `template_name`
    (`equal_weight`) so only the spec-presence rule can reject them -- previously the
    config-membership rule alone rejected them and the spec branch was untested; shape 9 is
    no longer a copy of shape 1 but isolates caller-vs-spec identity precedence; and
    `test_config_key_normalisation_prevents_static_exemption` pins the
    `str(k).strip().lower()` config-key normalisation from `e2ed0e5`.
"""

import copy
import json
import os
import sys
from unittest.mock import patch
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.live_preflight import (
    LiveDumpPreflightError,
    is_static_template,
    is_static_template_exempt,
    verify_live_strategy_dump_preflight,
)
import scripts.live_preflight as live_preflight
import scripts.run_live_strategy as rls
import scripts.check_live_portfolio_health as clph
import scripts.run_live_multi_strategy as rlms


@pytest.fixture
def healthy_chan_dump(tmp_path):
    """Synthesise a healthy `chan_four_state_blend` dump inside `tmp_path`.

    WHY SYNTHESISED (clean-clone fix, 2026-10-24): the previous fixture opened a
    pre-existing `chan_four_state_blend_strategy.json` straight out of the git-ignored
    serialized-dump directory (which has zero tracked files). On a clean clone the file
    simply does not exist and 8 of the 12 tests errored with `FileNotFoundError` -- a test
    suite that only passed on the machine that happened to run `--dump-strategies`. The
    payload below carries exactly the facts the gate consumes (`template_name` /
    `strategy_key`, both `blend_weighting_mode` containers), so the tests no longer depend
    on any ignored artifact.

    The gate itself still reads the TRACKED `pipeline/research_strategy/strategies_config.json`
    for the pin -- that is deliberate (the pin is the thing under test), and the assertion
    at the end keeps this fixture from silently drifting away from it.

    Returns `(payload_dict, path_str)` so mutation tests can deepcopy and seed variants.
    """
    payload = {
        "template_name": "chan_four_state_blend",
        "strategy_key": "chan_four_state_blend",
        "params": {"blend_weighting_mode": "regime_ic_ir_hybrid"},
        "research_strategy_spec": {
            "strategy_key": "chan_four_state_blend",
            "entry_data": {
                # Enough to reconstruct the exact template instance via
                # `instantiate_strategy_from_config_entry` (the CLI-integration tests go
                # through `get_template`); NOT a copy of the 42-parameter live payload --
                # only the reconstruction identity plus the mirrored pin.
                "type": "class",
                "class_name": "ChanFourStateBlendStrategy",
                "parameters": {"blend_weighting_mode": "regime_ic_ir_hybrid"},
            },
        },
    }
    dump_path = tmp_path / "chan_four_state_blend_strategy.json"
    dump_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # The synthesised "healthy" value must equal the pin the gate will read from the
    # tracked config; otherwise `test_healthy_dump_passes_silently` would be checking
    # an invented pin instead of the deployed one.
    config_file = os.path.join(
        _REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json"
    )
    with open(config_file, "r", encoding="utf-8") as f:
        pinned = json.load(f)["chan_four_state_blend"]["parameters"]["blend_weighting_mode"]
    assert pinned == payload["params"]["blend_weighting_mode"]

    return payload, str(dump_path)


def test_is_static_template():
    assert is_static_template("equal_weight") is True
    assert is_static_template("hierarchical_risk_parity") is True
    assert is_static_template("hrp") is True
    assert is_static_template("dual_momentum") is True
    assert is_static_template("chan_four_state_blend") is False
    assert is_static_template("turtle_breakout") is False
    assert is_static_template(None) is False
    assert is_static_template("") is False


def test_healthy_dump_passes_silently(healthy_chan_dump, capsys):
    strategy_def, dump_path = healthy_chan_dump
    verify_live_strategy_dump_preflight(strategy_def, strategy_file=dump_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_mode_mismatch_in_params_raises_with_all_four_strings(healthy_chan_dump):
    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(bad_def, strategy_file=dump_path)

    err_msg = str(exc_info.value)
    # 1. Mismatched mode
    assert "rolling_ic_ir" in err_msg
    # 2. Pinned mode
    assert "regime_ic_ir_hybrid" in err_msg
    # 3. Dump file path/display
    assert "chan_four_state_blend_strategy.json" in err_msg
    # 4. Config file path/display
    assert "strategies_config.json" in err_msg


def test_mode_mismatch_in_spec_raises_with_all_four_strings(healthy_chan_dump):
    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    # params is regime_ic_ir_hybrid, but spec entry_data has rolling_ic_ir
    bad_def["params"]["blend_weighting_mode"] = "regime_ic_ir_hybrid"
    bad_def["research_strategy_spec"]["entry_data"]["parameters"]["blend_weighting_mode"] = "rolling_ic_ir"

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(bad_def, strategy_file=dump_path)

    err_msg = str(exc_info.value)
    assert "rolling_ic_ir" in err_msg
    assert "regime_ic_ir_hybrid" in err_msg
    assert "chan_four_state_blend_strategy.json" in err_msg
    assert "strategies_config.json" in err_msg


def test_missing_strategy_key_in_config_fails_fast():
    missing_key_def = {
        "template_name": "mystery_unregistered_alpha",
        "params": {"blend_weighting_mode": "rolling_ic_ir"},
    }
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            missing_key_def,
            strategy_key="mystery_unregistered_alpha",
            strategy_file="mystery_unregistered_alpha_strategy.json",
        )

    err_msg = str(exc_info.value)
    assert "mystery_unregistered_alpha" in err_msg
    assert "mystery_unregistered_alpha_strategy.json" in err_msg
    assert "strategies_config.json" in err_msg
    # The only escape that exists today is the override flag -- the message must name it
    # (owner decision 2026-10-24, option A) so operators do not go hunting elsewhere.
    assert "--allow-weighting-mode-override" in err_msg


# ---------------------------------------------------------------------------
# FIX 2: the spec mirror is not optional. A MISSING key is drift, not a pass.
# ---------------------------------------------------------------------------

def test_spec_mirror_missing_weights_mode_raises(healthy_chan_dump):
    """Deleting only the spec-mirror key (params stays correct) must NOT pass silently.

    The pre-fix guard was `spec_mode is not None and spec_mode != pinned_mode`, so a dump
    whose mirror had been stripped/never written sailed through -- exactly the "half a
    door" the P1.8 acceptance criteria forbid.
    """
    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    del bad_def["research_strategy_spec"]["entry_data"]["parameters"]["blend_weighting_mode"]

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(bad_def, strategy_file=dump_path)

    err_msg = str(exc_info.value)
    assert "research_strategy_spec.entry_data.parameters" in err_msg
    assert "missing" in err_msg
    assert "regime_ic_ir_hybrid" in err_msg
    assert "chan_four_state_blend_strategy.json" in err_msg
    assert "strategies_config.json" in err_msg


def test_research_strategy_spec_absent_entirely_raises(healthy_chan_dump):
    """A dump with no `research_strategy_spec` at all must also fail (mirror missing)."""
    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    del bad_def["research_strategy_spec"]

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(bad_def, strategy_file=dump_path)

    err_msg = str(exc_info.value)
    assert "research_strategy_spec.entry_data.parameters" in err_msg
    assert "missing" in err_msg
    assert "chan_four_state_blend_strategy.json" in err_msg
    assert "strategies_config.json" in err_msg


# ---------------------------------------------------------------------------
# Owner decision 2026-10-24 (option A): --allow-weighting-mode-override also exempts an
# UNREGISTERED strategy key, loudly. It must not widen anything else.
# ---------------------------------------------------------------------------

def test_unregistered_key_without_override_raises_naming_key_and_config():
    unregistered = {
        "template_name": "custom_exploratory_alpha",
        "params": {"blend_weighting_mode": "rolling_ic_ir"},
    }
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            unregistered,
            strategy_key="custom_exploratory_alpha",
            strategy_file="custom_exploratory_alpha_strategy.json",
        )

    err_msg = str(exc_info.value)
    assert "custom_exploratory_alpha" in err_msg
    assert "strategies_config.json" in err_msg
    assert "--allow-weighting-mode-override" in err_msg


def test_unregistered_key_with_override_passes_with_loud_warning(monkeypatch, capsys):
    """Override lets an unregistered key through -- but never silently."""
    # The warning is deduplicated per (key, config) process-wide; reset so this test does
    # not depend on whether another test already tripped the same key.
    monkeypatch.setattr(live_preflight, "_WARNED_UNREGISTERED", set())

    unregistered = {
        "template_name": "custom_exploratory_alpha",
        "params": {"blend_weighting_mode": "rolling_ic_ir"},
    }
    verify_live_strategy_dump_preflight(
        unregistered,
        strategy_key="custom_exploratory_alpha",
        strategy_file="custom_exploratory_alpha_strategy.json",
        allow_override=True,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    # (i) the strategy key, (ii) the config file, (iii) the exploration phrase,
    # (iv) the fact that pinned-mode verification did NOT happen.
    assert "custom_exploratory_alpha" in err
    assert "strategies_config.json" in err
    assert "EXPLORATION" in err and "探索" in err
    assert "did NOT happen" in err
    assert "WARNING" in err


def test_malformed_dump_json_still_raises_with_override(tmp_path):
    """Unreadable JSON is not an "explore another mode" situation -- override cannot help."""
    broken = tmp_path / "custom_exploratory_alpha_strategy.json"
    broken.write_text("{ this is not json ", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        rls.parse_args([
            "--strategy-file", str(broken),
            "--allow-weighting-mode-override",
        ])


def test_non_object_dump_still_raises_with_override():
    """A dump that is not a JSON object cannot be reasoned about; override must not pass it."""
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            ["not", "a", "dict"],  # type: ignore[arg-type]
            strategy_file="custom_exploratory_alpha_strategy.json",
            allow_override=True,
        )
    assert "not a JSON object" in str(exc_info.value)
    assert "cannot be overridden" in str(exc_info.value)


def test_unidentifiable_dump_still_raises_with_override():
    """No strategy_key / spec key / template_name -> nothing to look up, stay fatal."""
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            {"params": {"blend_weighting_mode": "rolling_ic_ir"}},
            strategy_file="anonymous_strategy.json",
            allow_override=True,
        )
    err_msg = str(exc_info.value)
    assert "cannot identify" in err_msg
    assert "anonymous_strategy.json" in err_msg
    assert "cannot be overridden" in err_msg


def test_registered_strategy_missing_params_mode_still_raises_with_override(healthy_chan_dump):
    """Registered key + no params.blend_weighting_mode: there is no value to override."""
    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"].pop("blend_weighting_mode", None)

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            bad_def,
            strategy_file=dump_path,
            allow_override=True,
        )

    err_msg = str(exc_info.value)
    assert "params.blend_weighting_mode" in err_msg
    assert "cannot be passed by --allow-weighting-mode-override" in err_msg
    assert "chan_four_state_blend_strategy.json" in err_msg
    assert "strategies_config.json" in err_msg


def test_allow_override_bypasses_mismatch_with_loud_warning(healthy_chan_dump, monkeypatch, capsys):
    """(Step 0) Override passes a REGISTERED mismatch -- but never silently, and never as "verified".

    Before this fix the same call returned a bare `True` and wrote "verified" next to a mode
    the pin had explicitly rejected. Now the verdict separates the two facts and a warning
    names both values.
    """
    monkeypatch.setattr(live_preflight, "_WARNED_MISMATCH", set())

    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    verdict = verify_live_strategy_dump_preflight(
        bad_def,
        strategy_file=dump_path,
        allow_override=True,
    )

    assert verdict.verified is False
    assert verdict.mismatch_overridden is True

    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert "WARNING" in err
    # Both sides of the disagreement are named.
    assert "rolling_ic_ir" in err
    assert "regime_ic_ir_hybrid" in err
    assert "chan_four_state_blend" in err
    assert "strategies_config.json" in err
    assert "EXPLORATION" in err and "探索" in err
    assert "NOT the value configured for live deployment" in err


def test_mismatch_override_warning_is_deduplicated(healthy_chan_dump, monkeypatch, capsys):
    """A multi-strategy run must not print one warning per rebalance/strategy repeat."""
    monkeypatch.setattr(live_preflight, "_WARNED_MISMATCH", set())

    strategy_def, dump_path = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    for _ in range(3):
        verify_live_strategy_dump_preflight(bad_def, strategy_file=dump_path, allow_override=True)

    assert capsys.readouterr().err.count("WARNING: PINNED WEIGHTING MODE MISMATCH") == 1


def test_healthy_verdict_reports_verified(healthy_chan_dump):
    """The verified path must still say verified=True / mismatch_overridden=False."""
    strategy_def, dump_path = healthy_chan_dump
    verdict = verify_live_strategy_dump_preflight(strategy_def, strategy_file=dump_path)
    assert verdict.verified is True
    assert verdict.mismatch_overridden is False


def test_unregistered_verdict_is_not_marked_as_mismatch(healthy_chan_dump, monkeypatch):
    """An unregistered key is unverified, but nothing was mismatch-overridden."""
    monkeypatch.setattr(live_preflight, "_WARNED_UNREGISTERED", set())
    verdict = verify_live_strategy_dump_preflight(
        {"template_name": "custom_exploratory_alpha", "params": {}},
        strategy_key="custom_exploratory_alpha",
        strategy_file="custom_exploratory_alpha_strategy.json",
        allow_override=True,
    )
    assert verdict.verified is False
    assert verdict.mismatch_overridden is False


def test_static_template_passes_silently(capsys):
    static_def = {
        "template_name": "equal_weight",
        "params": {},
    }
    verify_live_strategy_dump_preflight(static_def, strategy_key="equal_weight")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# P1.9 (2026-10-24, TWO passes): closing the static-name exemptions.
#
# `_STATIC_TEMPLATE_NAMES` and `strategies_config.json` share exactly one name:
# `dual_momentum` (static template / "Active Dual Momentum GTAA" research entry). The original
# bypass was a bare name test applied before the config was even loaded. The FIRST fix (spec
# presence via `bool(...)` + `exempt(key) or exempt(template_name)`) closed only the
# spec-bearing path and left four shapes open -- measured, and pinned below as a 13-shape probe:
#
#   * spec key DELETED / `spec: {}` / spec without `strategy_key` -> looked "spec-less" -> PASSED;
#   * a non-dict spec -> `AttributeError` (crash, not a refusal);
#   * a drifting research dump for a pinned key whose `template_name` was `equal_weight` /
#     `hrp` / any other non-config static name -> PASSED, even though `common/strategy_spec.py::
#     get_template` gives `research_strategy_spec` precedence (the research engine ran);
#   * `template_name: ""` + `strategy_key: "dual_momentum"` + drift -> PASSED.
#
# The rule now: spec presence decides identity and a spec-bearing dump is NEVER static-exempt; a
# no-spec dump is exempt only when EVERY name it carries is a static name that owns no config key.
#
# Nothing below reads a git-ignored dump or mutates the tracked config: every pinned config
# is synthesised inside `tmp_path`.
# ---------------------------------------------------------------------------

def _write_collision_config(tmp_path, pinned_mode):
    """Synthesise a one-key config that pins `dual_momentum` to `pinned_mode`."""
    config_file = tmp_path / "strategies_config.json"
    config_file.write_text(
        json.dumps(
            {
                "dual_momentum": {
                    "name": "Synthetic Active Dual Momentum GTAA",
                    "type": "natural_language",
                    "parameters": {"blend_weighting_mode": pinned_mode},
                }
            }
        ),
        encoding="utf-8",
    )
    return str(config_file)


def _write_unpinned_config(tmp_path):
    """Synthesise a config that declares NO static-template name at all."""
    config_file = tmp_path / "strategies_config.json"
    config_file.write_text(json.dumps({"some_other_strategy": {}}), encoding="utf-8")
    return str(config_file)


def _write_identity_precedence_config(tmp_path):
    """Two-key config for the caller-vs-spec identity precedence shape (probe shape 9).

    `dual_momentum` is the key a CALLER would pass and is deliberately left UNPINNED, while
    `chan_four_state_blend` (the key the dump's spec declares) IS pinned. If identity ever
    fell back to the caller's key, shape 9 would hit the no-pin branch and return
    `verified=True` silently; only spec-first identity reaches the pinned comparison.
    """
    config_file = tmp_path / "strategies_config.json"
    config_file.write_text(
        json.dumps(
            {
                "dual_momentum": {
                    "name": "Synthetic Active Dual Momentum GTAA",
                    "parameters": {"rebalance_freq_days": 21},
                },
                "chan_four_state_blend": {
                    "name": "Synthetic pin for identity precedence",
                    "parameters": {"blend_weighting_mode": "regime_ic_ir_hybrid"},
                },
            }
        ),
        encoding="utf-8",
    )
    return str(config_file)


def _colliding_dual_momentum_dump(mode):
    """A RESEARCH dump for the config key `dual_momentum` (identifies itself via the spec)."""
    return {
        "template_name": "dual_momentum",
        "strategy_key": "dual_momentum",
        "params": {"blend_weighting_mode": mode},
        "research_strategy_spec": {
            "strategy_key": "dual_momentum",
            "entry_data": {
                "type": "class",
                "parameters": {"blend_weighting_mode": mode},
            },
        },
    }


def _without_spec(dump):
    """The same dump with `research_strategy_spec` stripped -- the `spec` key fully deleted."""
    return {k: v for k, v in dump.items() if k != "research_strategy_spec"}


def _nonconfig_static_dump(mode, **extra):
    """A dump whose visible names are all static templates that own NO config key.

    (P1.9 audit follow-up, 2026-10-24) `template_name` is `equal_weight` -- a static
    template name that is not a `strategies_config.json` key -- so the static-exemption
    test WOULD exempt this dump if it were read as spec-less. Every shape built from this
    base therefore proves the SPEC branch really runs: mutate the verifier so the
    empty/partial spec counts as "no spec" and the shape flips from `LiveDumpPreflightError`
    to a silent `verified=True`.

    Before this fix, probe shapes 3/4/5 used `template_name == strategy_key ==
    "dual_momentum"`, i.e. a config key, so the config-membership rule alone rejected them
    and they never reached the "empty/partial spec is not the same as no spec" branch --
    a claim no test could turn red.
    """
    return {
        "template_name": "equal_weight",
        "params": {"blend_weighting_mode": mode},
        **extra,
    }


# (label, payload, strategy_key, config kind, expected error substrings)
# Shapes 1-6, 9-12 of the probe all MUST raise; 7-8 are the silent-pass shapes below.
# Shapes 3/4/5 carry a NON-config static `template_name` on purpose (see
# `_nonconfig_static_dump`): only the spec-presence rule can reject them.
_PROBE_RAISES = [
    (
        "1-full-spec-drift",
        _colliding_dual_momentum_dump("rolling_ic_ir"),
        None,
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
    (
        "2-spec-key-deleted-drift",
        _without_spec(_colliding_dual_momentum_dump("rolling_ic_ir")),
        None,
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
    (
        # A spec-bearing dump must NEVER be static-exempt, even when its only visible name
        # is a non-config static template. Mutation that turns this red:
        # `spec_present = spec is not None` -> `spec_present = bool(spec)` (the empty dict
        # then counts as "no spec", the dump is exempted on `equal_weight`, silent pass).
        "3-empty-spec-dict-non-config-name-drift",
        _nonconfig_static_dump("rolling_ic_ir", research_strategy_spec={}),
        None,
        "pinned",
        ("equal_weight", "is not defined in configuration file"),
    ),
    (
        # Partial spec with no `entry_data`: identity still comes from the spec. Mutation
        # that turns this red: `spec_present = spec is not None` ->
        # `spec_present = spec is not None and "entry_data" in spec`.
        "4-spec-with-only-strategy-key-non-config-name",
        _nonconfig_static_dump(
            "rolling_ic_ir", research_strategy_spec={"strategy_key": "dual_momentum"}
        ),
        None,
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
    (
        # Partial spec whose `entry_data` is present but EMPTY. Mutation that turns this
        # red (a weaker presence test than shape 4's): `spec_present = spec is not None` ->
        # `spec_present = bool(spec.get("entry_data"))`.
        "5-spec-with-empty-entry-data-non-config-name",
        _nonconfig_static_dump(
            "rolling_ic_ir",
            research_strategy_spec={"strategy_key": "dual_momentum", "entry_data": {}},
        ),
        None,
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
    (
        "6a-spec-is-a-string",
        {**_colliding_dual_momentum_dump("rolling_ic_ir"), "research_strategy_spec": "x"},
        None,
        "pinned",
        ("research_strategy_spec", "cannot be overridden"),
    ),
    (
        "6b-spec-is-a-list",
        {**_colliding_dual_momentum_dump("rolling_ic_ir"), "research_strategy_spec": []},
        None,
        "pinned",
        ("research_strategy_spec", "cannot be overridden"),
    ),
    (
        # (P1.9 audit follow-up) This shape used to be byte-for-byte identical to shape 1.
        # Now it isolates CALLER-VS-SPEC identity precedence: the caller passes the
        # (unpinned) `dual_momentum` key while the spec declares `chan_four_state_blend`
        # (pinned). Mutation that turns this red: swap the fallback order to
        # `key = strategy_key or spec.get("strategy_key") or template_name` -- the dump then
        # resolves to the unpinned caller key, hits the no-pin branch and passes silently.
        "9-caller-key-vs-spec-key-precedence",
        {
            "template_name": "equal_weight",
            "strategy_key": "dual_momentum",
            "params": {"blend_weighting_mode": "rolling_ic_ir"},
            "research_strategy_spec": {
                "strategy_key": "chan_four_state_blend",
                "entry_data": {
                    "parameters": {"blend_weighting_mode": "rolling_ic_ir"}
                },
            },
        },
        "dual_momentum",
        "two_key",
        (
            "for strategy 'chan_four_state_blend'",
            "rolling_ic_ir",
            "regime_ic_ir_hybrid",
        ),
    ),
    (
        "10-blank-template-name-plus-config-key",
        _without_spec({**_colliding_dual_momentum_dump("rolling_ic_ir"), "template_name": ""}),
        "dual_momentum",
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
    (
        "extra-equal-weight-template-name-shield",
        {**_colliding_dual_momentum_dump("rolling_ic_ir"), "template_name": "equal_weight"},
        "dual_momentum",
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
    (
        "extra-hrp-template-name-shield",
        {**_colliding_dual_momentum_dump("rolling_ic_ir"), "template_name": "hrp"},
        "dual_momentum",
        "pinned",
        ("rolling_ic_ir", "regime_ic_ir_hybrid"),
    ),
]


@pytest.mark.parametrize(
    "payload,strategy_key,cfg_kind,expected",
    [(p, k, c, e) for _label, p, k, c, e in _PROBE_RAISES],
    ids=[label for label, *_rest in _PROBE_RAISES],
)
def test_probe_shapes_that_must_raise(tmp_path, payload, strategy_key, cfg_kind, expected):
    """(P1.9 second pass) 11 shapes: a pinned drift must be refused, never exempted.

    Every shape here PASSED `verified=True` (or crashed with `AttributeError`) before the
    second pass. `pytest.raises(LiveDumpPreflightError)` also asserts the failure is a
    diagnosable refusal rather than a traceback.
    """
    config_file = (
        _write_collision_config(tmp_path, "regime_ic_ir_hybrid")
        if cfg_kind == "pinned"
        else _write_identity_precedence_config(tmp_path)
        if cfg_kind == "two_key"
        else _write_unpinned_config(tmp_path)
    )
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            payload,
            strategy_key=strategy_key,
            strategy_file=str(tmp_path / "probe_strategy.json"),
            config_path=config_file,
        )
    err_msg = str(exc_info.value)
    for needle in expected:
        assert needle in err_msg


@pytest.mark.parametrize(
    "payload,cfg_kind",
    [
        ({"template_name": "dual_momentum", "params": {}}, "unpinned"),
        ({"template_name": "equal_weight", "params": {}}, "pinned"),
    ],
    ids=["7-genuine-static-dual-momentum-unpinned", "8-equal-weight-no-spec"],
)
def test_probe_shapes_that_must_pass_silently(tmp_path, capsys, payload, cfg_kind):
    """(P1.9 second pass) 2 shapes: genuine static templates still pass with zero noise."""
    config_file = (
        _write_collision_config(tmp_path, "regime_ic_ir_hybrid")
        if cfg_kind == "pinned"
        else _write_unpinned_config(tmp_path)
    )
    verdict = verify_live_strategy_dump_preflight(
        payload,
        strategy_file=str(tmp_path / "static_strategy.json"),
        config_path=config_file,
    )
    assert verdict.verified is True
    assert verdict.mismatch_overridden is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_colliding_static_name_with_pinned_mode_runs_preflight_and_catches_drift(tmp_path):
    """(P1.9) A research dump keyed `dual_momentum` must NOT be exempt: drift raises.

    Pre-fix this call returned `PreflightVerdict(verified=True)` from the early
    `is_static_template("dual_momentum")` return -- before the config was read -- so a pin
    of `regime_ic_ir_hybrid` could be violated silently.
    """
    config_file = _write_collision_config(tmp_path, "regime_ic_ir_hybrid")
    dump = _colliding_dual_momentum_dump("rolling_ic_ir")

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            dump,
            strategy_file=str(tmp_path / "dual_momentum_strategy.json"),
            config_path=config_file,
        )

    err_msg = str(exc_info.value)
    assert "rolling_ic_ir" in err_msg
    assert "regime_ic_ir_hybrid" in err_msg
    assert "dual_momentum_strategy.json" in err_msg
    assert "strategies_config.json" in err_msg


def test_colliding_static_name_healthy_pinned_value_passes_silently(tmp_path, capsys):
    """(P1.9) Same collision, agreeing value: verified, not overridden, no output."""
    config_file = _write_collision_config(tmp_path, "regime_ic_ir_hybrid")
    dump = _colliding_dual_momentum_dump("regime_ic_ir_hybrid")

    verdict = verify_live_strategy_dump_preflight(
        dump,
        strategy_file=str(tmp_path / "dual_momentum_strategy.json"),
        config_path=config_file,
    )

    assert verdict.verified is True
    assert verdict.mismatch_overridden is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_genuine_static_dual_momentum_without_spec_still_passes(tmp_path, capsys):
    """(P1.9 second pass) A spec-less static `dual_momentum` dump still passes -- but WHY differs.

    (i) Against the TRACKED config `dual_momentum` IS a config key, so the dump is now
    CHECKED rather than exempt; it passes only because that entry pins no
    `blend_weighting_mode` (the no-pin branch returns `verified=True`). This is the honest
    reading the second pass enforces: a stripped spec is indistinguishable from the genuine
    static payload, so a colliding name is checked, never trusted.
    (ii) Against a config that does not declare the name at all (the ordinary static case) it
    is genuinely exempt.
    """
    genuine = {"template_name": "dual_momentum", "params": {}}

    # (i) tracked config: checked (not exempt), but there is no pin to disagree with.
    verdict = verify_live_strategy_dump_preflight(genuine, strategy_key="dual_momentum")
    assert verdict.verified is True
    assert verdict.mismatch_overridden is False

    # (ii) a config without the key at all -> genuinely exempt, same observable result.
    verdict = verify_live_strategy_dump_preflight(
        genuine,
        strategy_key="dual_momentum",
        config_path=_write_unpinned_config(tmp_path),
    )
    assert verdict.verified is True
    assert verdict.mismatch_overridden is False

    # Genuine static templates produce no diagnostics at all.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_no_static_name_is_exempt_when_shadowed_by_config_key(tmp_path):
    """(P1.9 second pass) Structural: enumerate the 13 static names against the REAL config.

    The invariant is now unconditional: a static name that owns a config key is NEVER exempt
    (spec or no spec) -- the first pass let it be exempt when the spec looked absent, which is
    exactly the stripped-spec hole. A static name with no config entry stays exempt. The
    collision set itself is pinned to exactly `{"dual_momentum"}` so a future config/static
    name collision cannot slip in unnoticed.
    """
    config_keys = live_preflight._config_strategy_keys()
    assert "dual_momentum" in config_keys  # sanity: the loader read the real file

    collisions = {name for name in live_preflight._STATIC_TEMPLATE_NAMES if name in config_keys}
    assert collisions == {"dual_momentum"}

    for name in live_preflight._STATIC_TEMPLATE_NAMES:
        if name in config_keys:
            # Config membership wins, unconditionally: never exempt, with or without a spec.
            assert is_static_template_exempt(name, config_keys) is False
        else:
            # No config entry -> nothing to pin, exempt.
            assert is_static_template_exempt(name, config_keys) is True

    # The 4 aliases are NOT config keys, so they stay exempt; and a non-static name is never
    # exempt even when it owns a config entry.
    for alias in ("momentum", "hrp", "inv_vol", "ew"):
        assert is_static_template_exempt(alias, config_keys) is True
    assert is_static_template_exempt("chan_four_state_blend", config_keys) is False
    assert is_static_template_exempt(None, config_keys) is False
    # Normalisation must agree with `is_static_template` (same strip().lower()).
    assert is_static_template_exempt(" Equal_Weight ", config_keys) is True
    assert is_static_template_exempt(" DUAL_MOMENTUM ", config_keys) is False

    # Spec presence is decided at the verifier, not in the predicate: a spec-bearing dump is
    # checked even when every one of its names is a non-config static name.
    pinned = _write_collision_config(tmp_path, "regime_ic_ir_hybrid")
    shielded = {
        "template_name": "equal_weight",
        "params": {"blend_weighting_mode": "rolling_ic_ir"},
        "research_strategy_spec": {
            "strategy_key": "dual_momentum",
            "entry_data": {"parameters": {"blend_weighting_mode": "rolling_ic_ir"}},
        },
    }
    with pytest.raises(LiveDumpPreflightError):
        verify_live_strategy_dump_preflight(
            shielded,
            strategy_file=str(tmp_path / "shielded_strategy.json"),
            config_path=pinned,
        )

    # The loader is lazy-cached PER RESOLVED PATH: a synthesised config's key set must not
    # leak into the real one (that would make the gate test-order dependent).
    synth = tmp_path / "other_config.json"
    synth.write_text(json.dumps({"only_here": {}}), encoding="utf-8")
    assert live_preflight._config_strategy_keys(str(synth)) == frozenset({"only_here"})
    assert "only_here" not in live_preflight._config_strategy_keys()
    assert live_preflight._config_strategy_keys() == config_keys


def test_config_key_normalisation_prevents_static_exemption(tmp_path):
    """(P1.9 audit follow-up, 2026-10-24) Mixed-case / padded config keys must not re-exempt.

    Commit `e2ed0e5` normalised the config-key set with `str(k).strip().lower()`, but no test
    pinned it: reverting the normalisation turned nothing red. This is that test.

    Mutation that turns this red: `_normalise_config_keys` ->
    `frozenset(str(k) for k in keys)`. Then (1) the loader returns the raw `"Dual_Momentum"`
    instead of `"dual_momentum"`, and (2) the spec-less `dual_momentum` dump no longer
    matches the config set, so the static exemption fires and the verifier returns
    `verified=True` instead of raising -- i.e. the exact silent bypass P1.9 closed.
    """
    config_file = tmp_path / "mixed_case_config.json"
    config_file.write_text(
        json.dumps(
            {"Dual_Momentum": {"parameters": {"blend_weighting_mode": "regime_ic_ir_hybrid"}}}
        ),
        encoding="utf-8",
    )

    # 1. The loader hands back the NORMALISED key, not the raw spelling.
    keys = live_preflight._config_strategy_keys(str(config_file))
    assert keys == frozenset({"dual_momentum"})

    # 2. The verifier therefore refuses to exempt a `dual_momentum` dump against this config:
    #    a config key is CHECKED, whatever case/whitespace it was written in. (The raise is
    #    the unregistered/raw-lookup refusal, not a pin comparison -- the raw lookup is
    #    deliberately case-sensitive; the point here is only "not exempt".)
    spec_less = {
        "template_name": "dual_momentum",
        "params": {"blend_weighting_mode": "rolling_ic_ir"},
    }
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            spec_less,
            strategy_file=str(tmp_path / "dual_momentum_strategy.json"),
            config_path=str(config_file),
        )
    assert "dual_momentum" in str(exc_info.value)

    # 3. The predicate agrees with the loader for the raw spelling (config membership wins).
    assert is_static_template_exempt("dual_momentum", keys) is False
    assert is_static_template_exempt(" Dual_Momentum ", keys) is False


def test_identity_key_is_stripped_before_config_lookup(tmp_path):
    """(P1.9 audit follow-up, 2026-10-24) The identity key is stripped, like every other name.

    Mutations that turn this red:
    * delete the `if isinstance(key, str): key = key.strip()` block -> case 1 reverts to the
      raw `'  custom_alpha  '` spelling and case 3 is refused as "not defined" instead of
      being compared against the pin.
    """
    # 1. Padded non-static name: the message names the stripped key, not the padded spelling.
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            {"template_name": "  custom_alpha  ", "params": {}},
            strategy_file="custom_strategy.json",
        )
    err = str(exc_info.value)
    assert "'custom_alpha'" in err
    assert "'  custom_alpha  '" not in err

    # 2. All-whitespace name: "cannot identify" (nothing to look up), not "not defined".
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            {"template_name": "   ", "params": {}}, strategy_file="anonymous_strategy.json"
        )
    assert "cannot identify" in str(exc_info.value)

    # 3. Padded CONFIG KEY: resolves to that entry and is still NOT static-exempt, so the
    #    pinned value is really compared (drift raises with both values).
    config_file = _write_collision_config(tmp_path, "regime_ic_ir_hybrid")
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        verify_live_strategy_dump_preflight(
            {
                "template_name": "  dual_momentum  ",
                "params": {"blend_weighting_mode": "rolling_ic_ir"},
            },
            strategy_file=str(tmp_path / "dual_momentum_strategy.json"),
            config_path=config_file,
        )
    err = str(exc_info.value)
    assert "rolling_ic_ir" in err
    assert "regime_ic_ir_hybrid" in err


def test_strategy_without_pinned_mode_passes_silently(capsys):
    turtle_def = {
        "template_name": "turtle_breakout",
        "params": {"turtle_entry_breakout_days": 20},
    }
    verify_live_strategy_dump_preflight(turtle_def, strategy_key="turtle_breakout")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_live_strategy_cli_preflight_integration(tmp_path, healthy_chan_dump):
    """(P1.10 / 2026-10-24) `parse_args()` is free of the preflight; `main()` still fails fast.

    INVERTED from the P1.8 version, which asserted that `parse_args()` itself raised: the
    pre-flight gate moved into `main()` so argument parsing no longer performs a disk-backed
    validation with a fail-fast side effect. Both halves are asserted here on the SAME argv --
    parsing must not raise, and `main()` must.
    """
    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    with open(strat_file, "w", encoding="utf-8") as f:
        json.dump(bad_def, f)

    # 1. Parsing the drift dump no longer raises: the strategy definition is still resolved
    #    (that binding is deliberate and unchanged), but the gate does not run here.
    parsed = rls.parse_args(["--strategy-file", str(strat_file)])
    assert parsed._strategy_def["params"]["blend_weighting_mode"] == "rolling_ic_ir"
    assert parsed._strategy_key == "chan_four_state_blend"

    # 2. `main()` with the same argv still fails fast, naming both values.
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        rls.main(["--strategy-file", str(strat_file), "--data-provider", "synthetic"])
    assert "rolling_ic_ir" in str(exc_info.value)
    assert "regime_ic_ir_hybrid" in str(exc_info.value)

    # 3. `--allow-weighting-mode-override` still parses and is still bound (semantics intact).
    parsed = rls.parse_args([
        "--strategy-file", str(strat_file),
        "--allow-weighting-mode-override",
    ])
    assert parsed.allow_weighting_mode_override is True
    assert parsed._strategy_def["params"]["blend_weighting_mode"] == "rolling_ic_ir"


def test_main_fails_preflight_before_any_ticket_written(tmp_path, healthy_chan_dump):
    """(P1.10) The gate runs BEFORE every write: no ticket artifact, no output directory.

    A drifting `chan_four_state_blend` dump (synthesised here from the same shape as the
    tracked config entry -- no git-ignored dump is read) must abort `main()` with the gate's
    refusal, and the output directory must not even exist afterwards. This is the structural
    half of "fail before ticket": the preflight call must stay above `os.makedirs(...)`.
    """
    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    strat_file.write_text(json.dumps(bad_def), encoding="utf-8")
    out_dir = tmp_path / "out"

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        rls.main([
            "--strategy-file", str(strat_file),
            "--output-dir", str(out_dir),
            "--data-provider", "synthetic",
        ])

    err = str(exc_info.value)
    assert "rolling_ic_ir" in err
    assert "regime_ic_ir_hybrid" in err

    # Nothing was written, and the run never even reached `os.makedirs`.
    assert not out_dir.exists()
    for name in (
        "live_trading_ticket.csv",
        "live_trading_ticket.json",
        "post_holdings.json",
        "account_state.json",
    ):
        assert not (out_dir / name).exists()


def test_main_override_still_bypasses_same_drift(
    tmp_path, healthy_chan_dump, monkeypatch, capsys
):
    """(P1.10) `--allow-weighting-mode-override` still reaches the verifier and gets past it.

    Spy variant: the spy runs the REAL verifier (so this proves the gate actually passes, not
    merely that the flag was parsed), records its kwargs, then aborts the run with a sentinel
    before any data load -- keeping the test offline and fast without a broad `except`.
    """
    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    strat_file.write_text(json.dumps(bad_def), encoding="utf-8")
    out_dir = tmp_path / "out"

    real_verifier = rls.verify_live_strategy_dump_preflight
    seen = []

    class _StopAfterGate(Exception):
        pass

    def _spy(**kwargs):
        verdict = real_verifier(**kwargs)  # the REAL gate must let this dump through
        seen.append((kwargs, verdict))
        raise _StopAfterGate

    monkeypatch.setattr(live_preflight, "_WARNED_MISMATCH", set())
    monkeypatch.setattr(rls, "verify_live_strategy_dump_preflight", _spy)

    with pytest.raises(_StopAfterGate):
        rls.main([
            "--strategy-file", str(strat_file),
            "--output-dir", str(out_dir),
            "--data-provider", "synthetic",
            "--allow-weighting-mode-override",
        ])

    assert len(seen) == 1
    kwargs, verdict = seen[0]
    assert kwargs["allow_override"] is True
    assert kwargs["strategy_key"] == "chan_four_state_blend"
    # The override path stays honest: not "verified", but explicitly mismatch-overridden.
    assert verdict.verified is False
    assert verdict.mismatch_overridden is True

    err = capsys.readouterr().err
    assert "WARNING: PINNED WEIGHTING MODE MISMATCH" in err
    assert "rolling_ic_ir" in err and "regime_ic_ir_hybrid" in err

    # The sentinel fired before any write, in both the plain and the override path.
    assert not out_dir.exists()


def test_check_live_portfolio_health_cli_preflight_integration(tmp_path, healthy_chan_dump):
    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    with open(strat_file, "w", encoding="utf-8") as f:
        json.dump(bad_def, f)

    universe_file = tmp_path / "universe.txt"
    universe_file.write_text("SPY\nQQQ\nBIL\n")

    # 1. check_live_portfolio_health.main() raises LiveDumpPreflightError on mismatch
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        clph.main([
            "--strategy-file", str(strat_file),
            "--universe-file", str(universe_file),
            "--as-of-date", "2026-04-10",
            "--data-provider", "synthetic",
            "--no-save-state",
            "--output-dir", str(tmp_path / "out"),
        ])
    assert "rolling_ic_ir" in str(exc_info.value)
    assert "regime_ic_ir_hybrid" in str(exc_info.value)

    # 2. Forwarding in build_stage2_command
    parsed = clph.parse_args([
        "--strategy-file", str(strat_file),
        "--universe-file", str(universe_file),
        "--output-dir", str(tmp_path / "out"),
        "--allow-weighting-mode-override",
    ])
    assert parsed.allow_weighting_mode_override is True
    cmd = clph.build_stage2_command(parsed)
    assert "--allow-weighting-mode-override" in cmd


def _run_stage1(tmp_path, strat_file, extra_args=()):
    """Run Stage 1 end-to-end (synthetic data, tmp_path) and return the JSON report."""
    universe_file = tmp_path / "universe.txt"
    universe_file.write_text("SPY\nQQQ\nBIL\n")
    out_dir = tmp_path / "out"
    clph.main([
        "--strategy-file", str(strat_file),
        "--universe-file", str(universe_file),
        "--as-of-date", "2026-04-10",
        "--data-provider", "synthetic",
        "--no-save-state",
        "--output-dir", str(out_dir),
        *extra_args,
    ])
    return json.loads((out_dir / "stage1_health_report.json").read_text(encoding="utf-8"))


def test_unregistered_override_records_unverified_mode_not_a_fabricated_value(tmp_path, capsys):
    """(Owner decision A follow-up) The artifact must not lie about an unverified mode.

    Pre-fix, this path wrote `"weighting_mode": "rolling_ic_ir"` into
    stage1_health_report.json -- a plausible value that no config pin had ever confirmed.
    """
    unregistered = {
        "template_name": "custom_exploratory_alpha",
        "params": {"blend_weighting_mode": "rolling_ic_ir"},
    }
    strat_file = tmp_path / "custom_exploratory_alpha_strategy.json"
    strat_file.write_text(json.dumps(unregistered), encoding="utf-8")

    report = _run_stage1(tmp_path, strat_file, ("--allow-weighting-mode-override",))

    info = report["strategy_info"]
    assert info["weighting_mode"] is None
    assert info["weighting_mode_verified"] is False
    assert info["weighting_mode_mismatch_overridden"] is False
    assert "rolling_ic_ir" not in json.dumps(info)

    captured = capsys.readouterr()
    # The loud warning still fires on this exploration path.
    assert "custom_exploratory_alpha" in captured.err
    assert "EXPLORATION" in captured.err
    assert "did NOT happen" in captured.err


def test_registered_healthy_path_records_verified_mode(tmp_path, healthy_chan_dump):
    """Registered path stays byte-identical in spirit: real mode + verified flag true."""
    strategy_def, _ = healthy_chan_dump
    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    strat_file.write_text(json.dumps(strategy_def), encoding="utf-8")

    report = _run_stage1(tmp_path, strat_file)

    info = report["strategy_info"]
    assert info["weighting_mode"] == "regime_ic_ir_hybrid"
    assert info["weighting_mode_verified"] is True
    assert info["weighting_mode_mismatch_overridden"] is False


def test_registered_mismatch_override_records_unverified_and_warns(
    tmp_path, healthy_chan_dump, monkeypatch, capsys
):
    """(Step 0) The artifact must not call an overridden mismatch "verified".

    Pre-fix, this exact path wrote `weighting_mode="rolling_ic_ir"` **with**
    `weighting_mode_verified=True` and printed no warning -- reading as "the pin approved
    rolling_ic_ir", which is the opposite of what the pin said.
    """
    monkeypatch.setattr(live_preflight, "_WARNED_MISMATCH", set())

    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"
    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    strat_file.write_text(json.dumps(bad_def), encoding="utf-8")

    report = _run_stage1(tmp_path, strat_file, ("--allow-weighting-mode-override",))

    info = report["strategy_info"]
    # The dump's real mode is kept (owner instruction: do not blank it) ...
    assert info["weighting_mode"] == "rolling_ic_ir"
    # ... but it is emphatically not presented as verified.
    assert info["weighting_mode_verified"] is False
    assert info["weighting_mode_mismatch_overridden"] is True

    err = capsys.readouterr().err
    assert "WARNING: PINNED WEIGHTING MODE MISMATCH" in err
    assert "rolling_ic_ir" in err and "regime_ic_ir_hybrid" in err

    # The console/Markdown surfaces label it too, instead of printing a bare mode.
    assert "[UNVERIFIED: pinned-mode mismatch overridden]" in clph._weighting_mode_display(info)


def test_check_live_portfolio_health_missing_blend_weighting_mode_raises(tmp_path, healthy_chan_dump):
    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"].pop("blend_weighting_mode", None)
    if "research_strategy_spec" in bad_def:
        bad_def["research_strategy_spec"]["entry_data"]["parameters"].pop("blend_weighting_mode", None)

    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    with open(strat_file, "w", encoding="utf-8") as f:
        json.dump(bad_def, f)

    universe_file = tmp_path / "universe.txt"
    universe_file.write_text("SPY\nQQQ\nBIL\n")

    with pytest.raises(LiveDumpPreflightError) as exc_info:
        clph.main([
            "--strategy-file", str(strat_file),
            "--universe-file", str(universe_file),
            "--as-of-date", "2026-04-10",
            "--data-provider", "synthetic",
            "--no-save-state",
            "--output-dir", str(tmp_path / "out"),
        ])
    assert "blend_weighting_mode" in str(exc_info.value)


def test_run_live_multi_strategy_override_flag():
    with patch("sys.argv", ["run_live_multi_strategy.py", "--allow-weighting-mode-override"]):
        parsed = rlms.parse_args()
        assert parsed.allow_weighting_mode_override is True
