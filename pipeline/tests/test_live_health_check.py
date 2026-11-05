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


# ---------------------------------------------------------------------------
# 6. 2026-11-04 post-run audit: the 20% single-stock cap disclosure
#
# The archived `as_of = 2026-11-04` run delivered 601919.SH at 21.5077% of NAV with
# `breached: true, enforced: false, clamped: false` in the Stage-2 ticket, and the
# SAME run's Stage-1 report asserted both "Enforced across all regimes" (table) and
# "Strictly maintain 20% single-stock ceiling across all assets." (actions). Those
# sentences are now DERIVED from Stage 2's own enforcement predicate, so:
#   * enforcement OFF  -> the report may not claim enforcement anywhere;
#   * enforcement ON   -> the report must say so (no stale "not clamped" text);
#   * the kill-switch case must name the override instead of the flag.
# Nothing here exercises the clamp itself -- the clamp's behaviour is pinned by
# web/livetrading/tests/test_live_book_integrity.py, and this suite must not change it.
# ---------------------------------------------------------------------------

_STAGE2 = _load_script("run_live_strategy", os.path.join("scripts", "run_live_strategy.py"))

_FORBIDDEN_OLD_CLAIMS = (
    "Enforced across all regimes",
    "Strictly maintain 20% single-stock ceiling",
    "single-stock cap strictly applied",
)


def _dd_info_for_report():
    return {
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


def _macro_info_for_report(thrust_active=True):
    return {
        "breadth": 0.55,
        "breadth_active": True,
        "thrust": 0.62,
        "thrust_active": thrust_active,
        "thrust_thresh": 0.60,
        "breadth_thresh": 0.30,
        "target_exposure": 0.80,
        "leaders": [],
    }


def _vol_info_for_report():
    return {
        "realized_vol_21d": 0.11,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }


def _render_report(tmp_path, *, cap_state, thrust_active=True):
    """Drives `resolve_gate_directive` + `generate_reports` exactly as `main()` does."""
    import json

    dd_info = _dd_info_for_report()
    macro_info = _macro_info_for_report(thrust_active=thrust_active)
    vol_info = _vol_info_for_report()
    gate_code, gate_headline, actions = _mod.resolve_gate_directive(
        dd_info, macro_info, vol_info, None, single_stock_cap=cap_state
    )
    json_path, md_path = _mod.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-11-04",
        strategy_info={"strategy_name": "unit_test", "template_name": "equal_weight"},
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code=gate_code,
        gate_headline=gate_headline,
        actions=actions,
        single_stock_cap=cap_state,
    )
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    with open(json_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    return md, report, actions


def test_cap_wording_is_derived_from_stage2_predicate_and_constant():
    """The cap value and the `enforced` verdict come from Stage 2, not from prose.

    DISCRIMINATING: re-spelling either literal in the health script (the original
    defect) or changing `mv_cap_enforcement_enabled`'s semantics turns this red.
    """
    state = _mod.single_stock_cap_state()

    assert state["cap"] == pytest.approx(_STAGE2.DEFAULT_MAX_SINGLE_POSITION)
    assert _STAGE2.DEFAULT_MAX_SINGLE_POSITION == pytest.approx(0.20)
    assert state["enforce_flag"] == "--enforce-mv-cap"
    assert state["kill_switch_env"] == "LIVETRADING_DISABLE_MV_CAP"
    # Stage 1 never clamps, so its default is the warn-only reading -- but it is the
    # PREDICATE's reading, not a literal in the report generator.
    assert state["enforced"] is _STAGE2.mv_cap_enforcement_enabled(False) is False
    assert state["enforced"] is _STAGE2.mv_cap_enforcement_enabled(None) is False


def test_enforcement_off_report_makes_no_enforcement_claim(tmp_path, monkeypatch):
    """Enforcement off (the deployed configuration): disclose, never promise.

    This is the exact 2026-11-04 contradiction -- a report that disclosed an
    unenforced 21.5077% breach while also claiming blanket enforcement.
    """
    monkeypatch.delenv("LIVETRADING_DISABLE_MV_CAP", raising=False)
    cap_state = _mod.single_stock_cap_state(False)
    assert cap_state["enforced"] is False

    md, report, actions = _render_report(tmp_path, cap_state=cap_state)

    for claim in _FORBIDDEN_OLD_CLAIMS:
        assert claim not in md, f"stale absolute cap claim survived in markdown: {claim!r}"
        assert claim not in " ".join(actions), f"stale absolute cap claim survived in actions: {claim!r}"
        assert claim not in " ".join(report["trader_actions"]), claim

    # The positive disclosure: measured and disclosed, explicitly NOT clamped, and the
    # config that disables the clamp is named.
    assert "**Single-Stock Cap**" in md
    assert "NOT clamped" in md
    assert "--enforce-mv-cap" in md
    assert "LIVETRADING_DISABLE_MV_CAP" in md
    assert "Measured & disclosed" in md
    assert "detail_reason.mv_cap" in md

    # JSON carries the same derived action string (one `actions` list feeds both files).
    json_cap_actions = [a for a in report["trader_actions"] if "single-stock ceiling" in a]
    assert json_cap_actions, report["trader_actions"]
    assert "NOT clamped" in json_cap_actions[0]
    assert "--enforce-mv-cap" in json_cap_actions[0]
    # ... and the JSON must not contain the Markdown table's claim either.
    assert "across all regimes" not in " ".join(report["trader_actions"])


def test_enforcement_on_report_switches_to_enforcing_form(tmp_path, monkeypatch):
    """Companion case: if the clamp is ever switched on, the report says so.

    Both halves matter -- a report frozen on "not clamped" would be the same bug
    mirrored. The flip is driven by `mv_cap_enforcement_enabled`, i.e. by the thing
    that actually decides whether orders are clamped.
    """
    monkeypatch.delenv("LIVETRADING_DISABLE_MV_CAP", raising=False)
    cap_state = _mod.single_stock_cap_state(True)
    assert cap_state["enforced"] is True
    assert cap_state["overridden_by_kill_switch"] is False

    md, report, _actions = _render_report(tmp_path, cap_state=cap_state)

    assert "Enforced" in md
    assert "NOT clamped" not in md
    assert "clamps any BUY leg" in md
    assert not any("NOT clamped" in a for a in report["trader_actions"])


def test_kill_switch_override_is_named_not_misreported(tmp_path, monkeypatch):
    """Flag passed but kill-switch set: still not clamped, and the reason is named.

    The naive fix would print the "flag is off" reason while the operator passed it,
    which is the same class of lie in a smaller font.
    """
    monkeypatch.setenv("LIVETRADING_DISABLE_MV_CAP", "1")
    cap_state = _mod.single_stock_cap_state(True)

    assert cap_state["enforced"] is False
    assert cap_state["overridden_by_kill_switch"] is True

    md, _report, _actions = _render_report(tmp_path, cap_state=cap_state)

    assert "NOT clamped" in md
    assert "overrides it" in md
    assert "LIVETRADING_DISABLE_MV_CAP" in md
    assert "Enforced across all regimes" not in md


def test_conservative_regime_uses_the_same_derived_cap_action():
    """The third action site (conservative/selective branch) must not keep a stale claim.

    The audit quoted only the thrust branch's "Strictly maintain ..." line; the
    conservative branch carried its own absolute ("Standard 20% single-stock cap
    strictly applied."), so a half-fix would have left the same lie reachable from a
    different regime. (The breadth-only branch in between never mentioned the cap at
    all -- it made no claim, so it stays byte-identical.)
    """
    cap_state = _mod.single_stock_cap_state(False)
    dd_info = _dd_info_for_report()
    vol_info = _vol_info_for_report()

    conservative_macro = _macro_info_for_report(thrust_active=False)
    conservative_macro["breadth_active"] = False
    _code, _headline, actions = _mod.resolve_gate_directive(
        dd_info, conservative_macro, vol_info, None,
        single_stock_cap=cap_state,
    )
    joined = " ".join(actions)
    for claim in _FORBIDDEN_OLD_CLAIMS:
        assert claim not in joined, claim
    assert "NOT clamped" in joined
    assert "--enforce-mv-cap" in joined
    assert "Standard unscaled allocation" in joined  # the branch really was exercised

    # Both cap-bearing branches emit the identical derived sentence: one source of truth.
    _code2, _hl2, thrust_actions = _mod.resolve_gate_directive(
        _dd_info_for_report(), _macro_info_for_report(thrust_active=True), vol_info, None,
        single_stock_cap=cap_state,
    )
    cap_actions = [a for a in thrust_actions if "single-stock ceiling" in a]
    assert cap_actions and cap_actions[0] in joined


def test_gate_decision_and_numbers_are_untouched_by_the_cap_state():
    """Zero-behaviour-change guard: the cap state must not move a gate or a number.

    The disclosure rewrite is formatting only -- feeding an enforcing state must leave
    the gate code, headline and every numeric action text byte-identical.
    """
    off = _mod.single_stock_cap_state(False)
    on = _mod.single_stock_cap_state(True)  # kill-switch unset in this process
    if on["enforced"] is not True:  # pragma: no cover - env-dependent, documented
        pytest.skip("LIVETRADING_DISABLE_MV_CAP is set in this environment")

    for thrust in (True, False):
        code_off, head_off, actions_off = _mod.resolve_gate_directive(
            _dd_info_for_report(), _macro_info_for_report(thrust), _vol_info_for_report(), None,
            single_stock_cap=off,
        )
        code_on, head_on, actions_on = _mod.resolve_gate_directive(
            _dd_info_for_report(), _macro_info_for_report(thrust), _vol_info_for_report(), None,
            single_stock_cap=on,
        )
        assert (code_off, head_off) == (code_on, head_on)
        # Only the cap sentence may differ; the gate and every number stay put.
        cap_mask_off = [a for a in actions_off if "single-stock" not in a.lower()]
        cap_mask_on = [a for a in actions_on if "single-stock" not in a.lower()]
        assert cap_mask_off == cap_mask_on

