"""Offline regression suite for `scripts/check_live_portfolio_health.py` (P1.3).

P1.3 -- "Stage 1 auto-launch of Stage 2 must pass its CLI arguments explicitly".

Background (six-defect parity audit, Defect 6): `--auto-run-stage2` used to build
the Stage-2 argv inline, and that array omitted `--health-report-file`. The gate
report therefore only reached Stage 2 through its own implicit default
(`os.path.join(output_dir, "stage1_health_report.json")`), reconstructed INSIDE the
child from whatever `--output-dir` string it was handed. Any relative / unusual
output directory made the child re-resolve the path against ITS CWD, so Stage 2
could read a different file -- or no file at all -- while still printing a
successful Stage-1 gate. That is the exact "dangling argument" failure mode the
historical `--health-report-file` parameter had before T0.3 wired it up.

The fix: argv construction moved into `build_stage2_command()`, which
1. passes `--health-report-file` explicitly,
2. absolutizes every path-valued flag (anchored to the repo root, never the CWD),
3. keeps the file it names identical to the file `generate_reports()` wrote.

100% offline: no network, no real market data, and the child process is NEVER
spawned -- the argv is inspected (and re-parsed in-process by Stage 2's own
`parse_args`) exactly as `subprocess.run(cmd, check=True)` would receive it.
"""

import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script("check_live_portfolio_health", os.path.join("scripts", "check_live_portfolio_health.py"))


def _flag_value(argv, flag):
    """Returns the value that immediately follows `flag` in `argv` (argparse pairing)."""
    assert flag in argv, f"{flag} missing from argv: {argv}"
    return argv[argv.index(flag) + 1]


def _stage1_args(*cli_args):
    return _mod.parse_args(list(cli_args))


# ---------------------------------------------------------------------------
# 1. The explicit `--health-report-file` flag
# ---------------------------------------------------------------------------


def test_auto_run_stage2_argv_passes_explicit_absolute_health_report_file(tmp_path):
    """The auto-launched argv must name the gate report explicitly, as an absolute path.

    DISCRIMINATING: the pre-P1.3 inline argv had no `--health-report-file` entry at
    all, so this assertion fails on the old implementation (not a tautology).
    """
    out_dir = tmp_path / "stage1_run"
    args = _stage1_args("--output-dir", str(out_dir))

    cmd = _mod.build_stage2_command(args)

    report = _flag_value(cmd, "--health-report-file")
    assert report == os.path.join(str(out_dir), "stage1_health_report.json")
    assert os.path.isabs(report), f"health report path must be absolute, got {report!r}"

    # Every other path-valued flag is absolutized too, so no flag can be
    # re-resolved against the child's CWD.
    for flag in ("--output-dir", "--account-state-file", "--universe-file", "--strategy-file"):
        value = _flag_value(cmd, flag)
        assert os.path.isabs(value), f"{flag} must be absolute, got {value!r}"
    assert _flag_value(cmd, "--output-dir") == str(out_dir)
    assert os.path.dirname(report) == _flag_value(cmd, "--output-dir")


def test_auto_run_stage2_argv_names_the_file_generate_reports_actually_writes(tmp_path):
    """The forwarded path must be the same file Stage 1 just wrote.

    Otherwise the explicit flag would be worse than the implicit default: it would
    pin Stage 2 to a filename Stage 1 never produced.
    """
    out_dir = tmp_path / "stage1_run"
    args = _stage1_args("--output-dir", str(out_dir))

    dd_info = {
        "current_nav": 100000.0,
        "peak_nav": 105000.0,
        "drawdown_pct": -0.0476,
        "tier": "NORMAL",
        "tier_label": "Normal",
        "linear_equity_scale": 1.0,
        "tier1_counter": 0,
        "tier1_max": 15,
        "tier3_counter": 0,
        "freeze_remaining_bars": 0,
        "nav_10d_return": 0.012,
        "fast_recovery_nav": False,
    }
    macro_info = {
        "breadth": 0.55,
        "breadth_active": True,
        "thrust": 0.62,
        "thrust_active": True,
        "target_exposure": 0.80,
        "leaders": [],
    }
    vol_info = {
        "realized_vol_21d": 0.11,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }

    json_path, _md_path = _mod.generate_reports(
        output_dir=str(out_dir),
        as_of_date="2024-05-10",
        strategy_info={"strategy_name": "unit_test", "template_name": "equal_weight"},
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code="GO",
        gate_headline="GO",
        actions=["proceed"],
    )

    cmd = _mod.build_stage2_command(args)
    assert _flag_value(cmd, "--health-report-file") == os.path.abspath(json_path)
    assert os.path.exists(_flag_value(cmd, "--health-report-file"))


# ---------------------------------------------------------------------------
# 2. Working-directory independence
# ---------------------------------------------------------------------------


def test_auto_run_stage2_argv_is_cwd_invariant(tmp_path, monkeypatch):
    """Changing the CWD between two builds must not change a single argv element.

    A relative `--output-dir` that did not exist at parse time (Stage 1 itself
    creates it) stays relative through `parse_args`; the old code forwarded exactly
    that relative string, leaving the child to resolve it against its own CWD.
    Anchoring to the repo root makes the argv CWD-independent by construction.
    """
    args = _stage1_args("--output-dir", os.path.join("reports", "cwd_invariance_run"))
    assert not os.path.isabs(args.output_dir), "fixture must exercise a relative output-dir"

    baseline = _mod.build_stage2_command(args)

    monkeypatch.chdir(tmp_path)
    after_chdir = _mod.build_stage2_command(args)
    assert after_chdir == baseline

    monkeypatch.chdir(_REPO_ROOT)
    after_chdir_back = _mod.build_stage2_command(args)
    assert after_chdir_back == baseline

    report = _flag_value(baseline, "--health-report-file")
    assert os.path.isabs(report)
    # Anchored to the repo root (this script's documented base for relative
    # paths), NOT to the CWD pytest happened to run from.
    assert report == os.path.join(_REPO_ROOT, "reports", "cwd_invariance_run", "stage1_health_report.json")


# ---------------------------------------------------------------------------
# 3. Stage-2 still understands the argv it is handed
# ---------------------------------------------------------------------------


def test_auto_run_stage2_argv_round_trips_through_stage2_parser(tmp_path):
    """Stage 2's own parser must resolve `--health-report-file` back to the same path.

    Guards the cross-script contract: renaming/removing the flag in
    `scripts/run_live_strategy.py` would otherwise only surface on a live run.
    """
    stage2 = _load_script("run_live_strategy", os.path.join("scripts", "run_live_strategy.py"))

    out_dir = tmp_path / "stage1_run"
    cmd = _mod.build_stage2_command(_stage1_args("--output-dir", str(out_dir)))
    expected = _flag_value(cmd, "--health-report-file")

    # cmd = [python, stage2_script, <real CLI flags>...]
    parsed = stage2.parse_args(cmd[2:])
    assert parsed.health_report_file == expected
    assert parsed.output_dir == str(out_dir)


def test_build_stage2_command_rejects_unresolved_paths():
    """An unresolved path must fail loudly BEFORE a live child process is spawned."""
    args = _stage1_args("--output-dir", "/tmp/p13_unresolved_guard")
    args.account_state_file = None
    with pytest.raises(ValueError, match="account-state-file"):
        _mod.build_stage2_command(args)


# ---------------------------------------------------------------------------
# 4. Writer/reader agreement on the DEFAULT output directory
# ---------------------------------------------------------------------------


def test_default_output_dir_is_one_shared_resolution_for_both_stages():
    """Stage 1 and Stage 2 must default to ONE directory -- by construction, not by luck.

    The coupling is bidirectional and invisible in either file alone:
      * Stage 1 WRITES `stage1_health_report.json` into its `--output-dir` and reads
        `account_state.json` from it;
      * Stage 2 READS that report back out of ITS `--output-dir` and rewrites the state.
    Two literals that merely happen to match is exactly how "health check writes here,
    tickets read there" splits silently -- and until 2026-10-23 both literals pointed
    into `docs/ruleset/`, a hand-authored documentation tree. This pins the single
    resolver (`scripts/live_paths.py`): re-spelling the path OR the strategy key in
    either script turns this red.
    """
    from scripts import live_paths

    stage2 = _load_script("run_live_strategy", os.path.join("scripts", "run_live_strategy.py"))

    expected = live_paths.default_live_output_dir(live_paths.DEFAULT_STRATEGY_KEY)

    # Stage 1 has no --strategy of its own: it must name the SAME key by importing it.
    stage1_args = _stage1_args()
    # Stage 2 binds the default to the resolved strategy key.
    stage2_args = stage2.parse_args(["--strategy", live_paths.DEFAULT_STRATEGY_KEY])

    assert stage1_args.output_dir == expected
    assert stage2_args.output_dir == expected

    # The report Stage 1 writes is the file Stage 2 reads back: same directory, same name.
    assert os.path.dirname(stage2_args.health_report_file) == stage1_args.output_dir
    assert os.path.basename(stage2_args.health_report_file) == _mod.STAGE1_HEALTH_REPORT_JSON

    # The account state that both sides own is one file, not one per stage.
    assert stage1_args.account_state_file == stage2_args.account_state_file
    assert stage1_args.account_state_file == os.path.join(expected, "account_state.json")

    # The whole point of the change: no default path may be inside `docs/` (let alone
    # `docs/ruleset/`), so neither stage can create that tree by default any more.
    docs_root = os.path.join(_REPO_ROOT, "docs")
    for path in (stage1_args.output_dir, stage2_args.output_dir, expected):
        assert os.path.relpath(os.path.abspath(path), docs_root).startswith(".."), path
        assert "ruleset" not in os.path.normpath(path).split(os.sep), path

    # A moved `scripts/live_paths.py` would silently retarget every default above.
    assert live_paths.REPO_ROOT == _REPO_ROOT


# ---------------------------------------------------------------------------
# 5. L2.42: Tier-3 Fast Recovery Override Parity in Stage 1
# ---------------------------------------------------------------------------


def test_resolve_gate_directive_fast_recovery_downgrades_tier3_to_tier2_defensive():
    """L2.42: When fast_recovery_nav or thrust_active is True during TIER_3_HALT or freeze,
    resolve_gate_directive must return CAUTION_RECOVERING, synchronize dd_info['tier'] to
    TIER_2_DEFENSIVE, and set dd_info['linear_equity_scale'] to 0.30.
    """
    resolve_gate_directive = _mod.resolve_gate_directive

    vol_info = {"vol_elevated": False}

    # Case 1: TIER_3_HALT with fast_recovery_nav=True
    dd_info_1 = {
        "tier": "TIER_3_HALT",
        "tier_label": "🔴 TIER 3 (Emergency Halt / 100% Cash)",
        "linear_equity_scale": 0.0,
        "fast_recovery_nav": True,
        "freeze_remaining_bars": 0,
    }
    macro_info_1 = {"thrust_active": False, "breadth_active": False}
    code1, headline1, actions1 = resolve_gate_directive(dd_info_1, macro_info_1, vol_info)

    assert code1 == "CAUTION_RECOVERING"
    assert "TIER 3 DOWNGRADED VIA FAST RECOVERY OVERRIDE" in headline1
    assert dd_info_1["tier"] == "TIER_2_DEFENSIVE"
    assert dd_info_1["linear_equity_scale"] == pytest.approx(0.30)
    assert "Defensive Mode / Fast Recovery Override" in dd_info_1["tier_label"]

    # Case 2: In freeze period (freeze_remaining_bars > 0) with thrust_active=True
    dd_info_2 = {
        "tier": "TIER_3_HALT",
        "tier_label": "🔴 TIER 3 (Emergency Halt / 100% Cash)",
        "linear_equity_scale": 0.0,
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 3,
    }
    macro_info_2 = {"thrust_active": True, "thrust": 0.65, "thrust_thresh": 0.60, "target_exposure": 0.80}
    code2, headline2, actions2 = resolve_gate_directive(dd_info_2, macro_info_2, vol_info)

    assert code2 == "CAUTION_RECOVERING"
    assert dd_info_2["tier"] == "TIER_2_DEFENSIVE"
    assert dd_info_2["linear_equity_scale"] == pytest.approx(0.30)

    # Case 3: Counter-check without fast recovery -> stays NO_GO_EMERGENCY
    dd_info_3 = {
        "tier": "TIER_3_HALT",
        "tier_label": "🔴 TIER 3 (Emergency Halt / 100% Cash)",
        "linear_equity_scale": 0.0,
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 3,
    }
    macro_info_3 = {"thrust_active": False, "breadth_active": False}
    code3, headline3, actions3 = resolve_gate_directive(dd_info_3, macro_info_3, vol_info)

    assert code3 == "NO_GO_EMERGENCY"
    assert dd_info_3["tier"] == "TIER_3_HALT"
    assert dd_info_3["linear_equity_scale"] == pytest.approx(0.0)

