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
    strategy_def, _ = healthy_chan_dump
    bad_def = copy.deepcopy(strategy_def)
    bad_def["params"]["blend_weighting_mode"] = "rolling_ic_ir"

    strat_file = tmp_path / "chan_four_state_blend_strategy.json"
    with open(strat_file, "w", encoding="utf-8") as f:
        json.dump(bad_def, f)

    # 1. Parsing without override raises LiveDumpPreflightError
    with pytest.raises(LiveDumpPreflightError) as exc_info:
        rls.parse_args(["--strategy-file", str(strat_file)])
    assert "rolling_ic_ir" in str(exc_info.value)
    assert "regime_ic_ir_hybrid" in str(exc_info.value)

    # 2. Parsing with --allow-weighting-mode-override succeeds
    parsed = rls.parse_args([
        "--strategy-file", str(strat_file),
        "--allow-weighting-mode-override",
    ])
    assert parsed.allow_weighting_mode_override is True
    assert parsed._strategy_def["params"]["blend_weighting_mode"] == "rolling_ic_ir"


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
