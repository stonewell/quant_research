"""100% offline unit tests for the `backtest-deep-review` skill and toolkit.

Validates skill discovery/symlink invariants, frontmatter & markdown hygiene, all 6
reusable audit helpers in `scripts/deep_review_toolkit.py`, and its CLI entrypoints.
Zero real market data and zero network calls.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PIPELINE_ROOT.parent
_SKILL_DIR = _PIPELINE_ROOT / ".agents" / "skills" / "backtest-deep-review"
_TOOLKIT_SCRIPT = _SKILL_DIR / "scripts" / "deep_review_toolkit.py"


@pytest.fixture(scope="module")
def toolkit():
    """Load `deep_review_toolkit.py` by file path (dotted `.agents` dir is non-importable)."""
    spec = importlib.util.spec_from_file_location("deep_review_toolkit_under_test", _TOOLKIT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_yaml_frontmatter(md_text: str) -> dict[str, str]:
    """Parse YAML frontmatter block (`--- ... ---`) supporting folded `>-` scalars."""
    m = re.match(r"^---\n(.*?)\n---\n", md_text, re.DOTALL)
    assert m is not None, "SKILL.md must begin with YAML frontmatter delimited by ---"
    fm_block = m.group(1)

    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(fm_block)
        assert isinstance(parsed, dict)
        return {str(k): str(v).strip() for k, v in parsed.items()}
    except ImportError:
        result: dict[str, str] = {}
        current_key = None
        buf: list[str] = []
        for raw in fm_block.splitlines():
            if raw and not raw.startswith(" ") and ":" in raw:
                if current_key is not None:
                    result[current_key] = " ".join(buf).strip()
                k, v = raw.split(":", 1)
                current_key = k.strip()
                v_clean = v.strip()
                buf = [] if v_clean in (">", ">-", "|", "|-") else [v_clean]
            elif current_key is not None:
                buf.append(raw.strip())
        if current_key is not None:
            result[current_key] = " ".join(buf).strip()
        return result


def _find_long_prose_lines(md_path: Path, max_len: int = 400) -> list[tuple[int, int]]:
    """Return `(line_no, length)` for prose lines > max_len outside code fences and tables."""
    offenders: list[tuple[int, int]] = []
    in_fence = False
    for idx, line in enumerate(md_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("|"):
            continue
        if len(line) > max_len:
            offenders.append((idx, len(line)))
    return offenders


def test_skill_files_and_root_symlink_discovery():
    """Verify skill files, root symlink resolution, frontmatter, links, and prose line hygiene."""
    skill_md = _SKILL_DIR / "SKILL.md"
    checklist_md = _SKILL_DIR / "references" / "diagnostic_checklist.md"
    toolkit_py = _SKILL_DIR / "scripts" / "deep_review_toolkit.py"

    assert skill_md.is_file(), f"Missing {skill_md}"
    assert checklist_md.is_file(), f"Missing {checklist_md}"
    assert toolkit_py.is_file(), f"Missing {toolkit_py}"

    root_symlink_md = _REPO_ROOT / ".agents" / "skills" / "backtest-deep-review" / "SKILL.md"
    assert root_symlink_md.exists(), f"Root symlink discovery failed: {root_symlink_md}"
    assert os.path.realpath(root_symlink_md) == os.path.realpath(skill_md)

    skill_text = skill_md.read_text(encoding="utf-8")
    fm = _parse_yaml_frontmatter(skill_text)
    assert fm.get("name") == "backtest-deep-review"
    desc = fm.get("description", "")
    assert 0 < len(desc) < 1024, f"Unexpected description length: {len(desc)}"
    for required_phrase in ("deep review", "Rule 10", "lot-rounding", "waterfall"):
        assert required_phrase in desc, f"Frontmatter description missing {required_phrase!r}"

    # Verify all relative markdown links in SKILL.md resolve to existing paths.
    link_targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", skill_text)
    for target in link_targets:
        clean = target.split("#", 1)[0].strip()
        if not clean or clean.startswith(("http://", "https://", "file://", "mailto:")):
            continue
        resolved = (skill_md.parent / clean).resolve()
        assert resolved.exists(), f"Broken relative link in SKILL.md: {target!r} -> {resolved}"

    # Verify no single-line prose walls (> 400 chars outside tables/code fences).
    for doc_path in (skill_md, checklist_md):
        offenders = _find_long_prose_lines(doc_path, max_len=400)
        assert not offenders, f"Prose lines > 400 chars in {doc_path.name}: {offenders}"


def test_verify_rule10_weight_diff_adjudicated_vs_zero_delta_unadjudicated(toolkit):
    """Identical frames without probe movement must be flagged UNADJUDICATED_ZERO_DELTA."""
    dates = pd.bdate_range("2025-01-02", periods=5)
    cols = ["601288.SH", "300394.SZ", "600519.SH"]
    w_base = pd.DataFrame(0.10, index=dates, columns=cols)

    # 1. Identical frames + absent probe -> UNADJUDICATED_ZERO_DELTA
    res_zero = toolkit.verify_rule10_weight_diff(w_base.copy(), w_base)
    assert res_zero["adjudicated"] is False
    assert res_zero["verdict"] == "UNADJUDICATED_ZERO_DELTA"
    assert res_zero["n_changed_weight_cells"] == 0
    assert res_zero["first_divergence_date"] is None

    # 2. Identical frames + identical probe -> UNADJUDICATED_ZERO_DELTA
    res_same_probe = toolkit.verify_rule10_weight_diff(
        w_base.copy(),
        w_base,
        probe_name="canary_regime",
        probe_cand="defensive",
        probe_base="defensive",
    )
    assert res_same_probe["adjudicated"] is False
    assert res_same_probe["probe_differs"] is False
    assert res_same_probe["verdict"] == "UNADJUDICATED_ZERO_DELTA"

    # 3. Single modified weight cell -> ADJUDICATED with exact first_divergence_date & max diff
    w_cand = w_base.copy()
    w_cand.loc[dates[2], "300394.SZ"] = 0.275
    res_diff = toolkit.verify_rule10_weight_diff(w_cand, w_base)
    assert res_diff["adjudicated"] is True
    assert res_diff["verdict"] == "ADJUDICATED"
    assert res_diff["n_changed_weight_cells"] == 1
    assert res_diff["n_changed_rebalance_rows"] == 1
    assert res_diff["first_divergence_date"] == str(dates[2].date())
    assert res_diff["max_abs_weight_diff"] == pytest.approx(0.175, abs=1e-6)


def test_evaluate_option_b_gate_boundaries(toolkit):
    """Test all 4 simultaneous Option B conditions (delta, positive folds, paired_t, DSR)."""
    base = [0.50] * 18

    # Passing case: delta=+0.20, 15/18 positive folds, paired_t > 2.0, dsr=0.97
    cand_pass = [0.75] * 15 + [0.45] * 3
    res_pass = toolkit.evaluate_option_b_gate(cand_pass, base, dsr_val=0.97)
    assert res_pass["delta_sharpe_passed"] is True
    assert res_pass["positive_folds_passed"] is True
    assert res_pass["paired_t_passed"] is True
    assert res_pass["dsr_passed"] is True
    assert res_pass["option_b_passed"] is True

    # Fail criterion 1: mean_delta_sharpe < 0.15 (while 18/18 positive, high t, dsr >= 0.95)
    cand_low_delta = [0.60] * 18
    res_low_delta = toolkit.evaluate_option_b_gate(cand_low_delta, base, dsr_val=0.98)
    assert res_low_delta["delta_sharpe_passed"] is False
    assert res_low_delta["positive_folds_passed"] is True
    assert res_low_delta["paired_t_passed"] is True
    assert res_low_delta["dsr_passed"] is True
    assert res_low_delta["option_b_passed"] is False

    # Fail criterion 2: positive_folds = 12/18 (< 14) while delta >= 0.15, t >= 2.0, dsr >= 0.95
    cand_12_folds = [0.85] * 12 + [0.45] * 6
    res_12_folds = toolkit.evaluate_option_b_gate(cand_12_folds, base, dsr_val=0.98)
    assert res_12_folds["positive_folds"] == 12
    assert res_12_folds["delta_sharpe_passed"] is True
    assert res_12_folds["positive_folds_passed"] is False
    assert res_12_folds["paired_t_passed"] is True
    assert res_12_folds["dsr_passed"] is True
    assert res_12_folds["option_b_passed"] is False

    # Fail criterion 3: paired_t < 2.0 (14 positive folds, mean delta >= 0.15, high variance outlier)
    cand_low_t = [1.00] * 14 + [-0.40] * 4
    res_low_t = toolkit.evaluate_option_b_gate(cand_low_t, base, dsr_val=0.98)
    assert res_low_t["delta_sharpe_passed"] is True
    assert res_low_t["positive_folds_passed"] is True
    assert res_low_t["paired_t_passed"] is False
    assert res_low_t["dsr_passed"] is True
    assert res_low_t["option_b_passed"] is False

    # Fail criterion 4: DSR < 0.95 while all other 3 pass
    res_low_dsr = toolkit.evaluate_option_b_gate(cand_pass, base, dsr_val=0.94)
    assert res_low_dsr["delta_sharpe_passed"] is True
    assert res_low_dsr["positive_folds_passed"] is True
    assert res_low_dsr["paired_t_passed"] is True
    assert res_low_dsr["dsr_passed"] is False
    assert res_low_dsr["option_b_passed"] is False


def test_compute_cash_normalized_metrics_and_floor(toolkit):
    """Verify CFSB N=100 cash-normalized math and the 0.10 exposure floor safeguard."""
    res_cfsb = toolkit.compute_cash_normalized_metrics(
        raw_cagr=0.0619,
        raw_max_dd=0.0766,
        avg_invested_weight=0.2705,
    )
    assert res_cfsb["floor_applied"] is False
    assert res_cfsb["effective_denom"] == pytest.approx(0.2705, abs=1e-4)
    assert res_cfsb["norm_cagr"] == pytest.approx(round(0.0619 / 0.2705, 4), abs=1e-4)
    assert res_cfsb["norm_max_dd"] == pytest.approx(round(0.0766 / 0.2705, 4), abs=1e-4)
    assert res_cfsb["cash_drag_severity"] == "HEAVY_CASH_DRAG"

    # Below 0.10 exposure floor -> clamped to 0.10 and floor_applied is True
    res_floor = toolkit.compute_cash_normalized_metrics(
        raw_cagr=0.015,
        raw_max_dd=-0.025,
        avg_invested_weight=0.04,
        exposure_floor=0.10,
    )
    assert res_floor["floor_applied"] is True
    assert res_floor["effective_denom"] == pytest.approx(0.10, abs=1e-4)
    assert res_floor["norm_cagr"] == pytest.approx(0.15, abs=1e-4)
    assert res_floor["norm_max_dd"] == pytest.approx(0.25, abs=1e-4)
    assert res_floor["cash_drag_severity"] == "HEAVY_CASH_DRAG"


def test_audit_lot_rounding_realism_100k_vs_200k_vs_1m(toolkit):
    """Verify monotonic drop-rate ordering (100k > 200k > 1M) and exact < 1 lot counts."""
    dates = pd.bdate_range("2025-02-03", periods=2)
    # Uniform 5% weight across 5 A-shares at prices: 20, 60, 120, 220, 600 RMB
    # 1 lot (100 shares) costs: 2,000 / 6,000 / 12,000 / 22,000 / 60,000 RMB
    # Target notional per cell:
    # - 100k * 0.05 =  5,000 -> drops 60, 120, 220, 600 (4/5 = 80%)
    # - 200k * 0.05 = 10,000 -> drops 120, 220, 600 (3/5 = 60%)
    # -   1M * 0.05 = 50,000 -> drops 600 only (1/5 = 20%)
    syms = ["601288.SH", "600036.SH", "605499.SH", "688111.SH", "002371.SZ"]
    weights = pd.DataFrame(0.05, index=dates, columns=syms)
    prices = pd.DataFrame(
        {
            "601288.SH": [20.0, 20.0],
            "600036.SH": [60.0, 60.0],
            "605499.SH": [120.0, 120.0],
            "688111.SH": [220.0, 220.0],
            "002371.SZ": [600.0, 600.0],
        },
        index=dates,
    )

    r_100k = toolkit.audit_lot_rounding_realism(weights, prices, portfolio_value=100_000.0)
    r_200k = toolkit.audit_lot_rounding_realism(weights, prices, portfolio_value=200_000.0)
    r_1m = toolkit.audit_lot_rounding_realism(weights, prices, portfolio_value=1_000_000.0)

    assert r_100k["n_positive_target_cells"] == 10
    assert r_100k["n_dropped_below_1_lot"] == 8
    assert r_200k["n_dropped_below_1_lot"] == 6
    assert r_1m["n_dropped_below_1_lot"] == 2

    assert r_100k["drop_rate_pct"] == pytest.approx(80.0, abs=1e-2)
    assert r_200k["drop_rate_pct"] == pytest.approx(60.0, abs=1e-2)
    assert r_1m["drop_rate_pct"] == pytest.approx(20.0, abs=1e-2)
    assert r_100k["drop_rate_pct"] > r_200k["drop_rate_pct"] > r_1m["drop_rate_pct"]
    assert r_1m["worst_dropped_symbols"] == [{"symbol": "002371.SZ", "drops": 2}]


def test_summarize_stage_waterfall_pinpoints_entry_confirm_cliff(toolkit):
    """9-stage CFSB N=20 vs N=100 waterfall must pinpoint `7_post_entry_confirm`."""
    wf_n20 = {
        "1_post_ortho": 0.7200,
        "2_post_cb": 0.7050,
        "3_post_cyq": 0.6900,
        "4_post_bull_scale": 0.6850,
        "5_post_thrust": 0.6850,
        "6_post_vol_target": 0.6407,
        "7_post_entry_confirm": 0.5876,
        "8_post_sequencer": 0.5520,
        "9_delivered_bt": 0.5410,
    }
    wf_n100 = {
        "1_post_ortho": 0.7150,
        "2_post_cb": 0.7010,
        "3_post_cyq": 0.6880,
        "4_post_bull_scale": 0.6820,
        "5_post_thrust": 0.6820,
        "6_post_vol_target": 0.6388,
        "7_post_entry_confirm": 0.2802,
        "8_post_sequencer": 0.2740,
        "9_delivered_bt": 0.2705,
    }

    summary = toolkit.summarize_stage_waterfall(wf_n20, wf_n100, label_a="N=20", label_b="N=100")
    assert len(summary["stages"]) == 9
    assert summary["primary_bottleneck_stage"] == "7_post_entry_confirm"
    assert summary["primary_bottleneck_step_drop"] == pytest.approx(0.2802 - 0.6388, abs=1e-4)


def test_compute_superstar_and_group_attribution(toolkit):
    """Verify Core/Satellite breakdown, Top-K shares, LOO flip flag, and all 3 chronic-loser rules."""
    rows = [
        # Superstar winner (drives > 100% of net PnL -> removing Top-1 flips PnL negative)
        {"symbol": "300394.SZ", "net_pnl": 6000.0, "buys": 30000.0, "costs": 150.0, "trades": 8},
        {"symbol": "601288.SH", "net_pnl": 1000.0, "buys": 40000.0, "costs": 100.0, "trades": 6},
        {"symbol": "600900.SH", "net_pnl": 500.0, "buys": 25000.0, "costs": 60.0, "trades": 4},
        # Chronic loser #1: net_pnl < -1000
        {"symbol": "601766.SH", "net_pnl": -1500.0, "buys": 80000.0, "costs": 200.0, "trades": 12},
        # Chronic loser #2: roi_pct < -3.0% (net_pnl = -400 on 10,000 buys -> -4.0%)
        {"symbol": "000999.SZ", "net_pnl": -400.0, "buys": 10000.0, "costs": 50.0, "trades": 5},
        # Chronic loser #3: costs > 0.5 * |net_pnl| (costs=120 > 0.5*200=100)
        {"symbol": "601398.SH", "net_pnl": -200.0, "buys": 20000.0, "costs": 120.0, "trades": 18},
        # Benign small loser: triggers NONE of the 3 chronic-loser rules
        {"symbol": "600036.SH", "net_pnl": -100.0, "buys": 20000.0, "costs": 20.0, "trades": 2},
    ]
    group_map = {
        "300394.SZ": "Satellite",
        "601288.SH": "Core",
        "600900.SH": "Core",
        "601766.SH": "Core",
        "000999.SZ": "Core",
        "601398.SH": "Core",
        "600036.SH": "Core",
    }

    attr = toolkit.compute_superstar_and_group_attribution(
        rows, group_map=group_map, total_universe_size=10
    )
    # Total net PnL = 6000 + 1000 + 500 - 1500 - 400 - 200 - 100 = 5300
    assert attr["n_traded"] == 7
    assert attr["n_untraded"] == 3
    assert attr["total_net_pnl"] == pytest.approx(5300.0, abs=1e-2)
    assert attr["top1_symbol"] == "300394.SZ"
    assert attr["top1_share_pct"] == pytest.approx(round(100.0 * 6000.0 / 5300.0, 1), abs=1e-1)
    assert attr["top3_share_pct"] == pytest.approx(round(100.0 * 7500.0 / 5300.0, 1), abs=1e-1)
    assert attr["top5_share_pct"] == pytest.approx(round(100.0 * 7200.0 / 5300.0, 1), abs=1e-1)
    assert attr["loo_ex_top1_pnl"] == pytest.approx(-700.0, abs=1e-2)
    assert attr["loo_ex_top1_flips_negative"] is True

    chronic_syms = {r["symbol"] for r in attr["chronic_losers"]}
    assert chronic_syms == {"601766.SH", "000999.SZ", "601398.SH"}
    assert "600036.SH" not in chronic_syms

    assert set(attr["group_breakdown"].keys()) == {"Core", "Satellite"}
    assert attr["group_breakdown"]["Satellite"]["n_traded"] == 1
    assert attr["group_breakdown"]["Satellite"]["net_pnl"] == pytest.approx(6000.0, abs=1e-2)
    assert attr["group_breakdown"]["Core"]["n_traded"] == 6
    assert attr["group_breakdown"]["Core"]["net_pnl"] == pytest.approx(-700.0, abs=1e-2)


def test_cli_demo_synthetic_and_missing_file_error(toolkit, capsys, tmp_path):
    """Exercise CLI `--demo-synthetic`, missing-file error handling, and `--check-rule10`."""
    # 1. --demo-synthetic -> exit 0 + expected headers
    rc_demo = toolkit.main(["--demo-synthetic"])
    out_demo = capsys.readouterr()
    assert rc_demo == 0
    assert "# Backtest Deep-Review Synthetic Demo" in out_demo.out
    assert "UNADJUDICATED_ZERO_DELTA" in out_demo.out
    assert "7_post_entry_confirm" in out_demo.out

    # 2. --summarize-json on nonexistent path -> exit 1 + stderr error
    rc_missing = toolkit.main(["--summarize-json", "/nonexistent/file.json"])
    out_missing = capsys.readouterr()
    assert rc_missing == 1
    assert "not found" in out_missing.err.lower()

    # 3. --check-rule10 on synthetic JSON with both adjudicated and zero-delta arms
    synthetic_review = {
        "section5_counterfactuals": {
            "CF0": {"desc": "Baseline", "rule10_probe": {"n_changed_weight_cells": 0}},
            "CF1": {
                "desc": "Zero-delta arm",
                "rule10_probe": {
                    "n_changed_weight_cells": 0,
                    "n_changed_rebalance_rows": 0,
                    "max_abs_weight_diff": 0.0,
                },
                "specific_probe": "canary_defensive=100%",
            },
            "CF2": {
                "desc": "Active changed arm",
                "rule10_probe": {
                    "n_changed_weight_cells": 42,
                    "n_changed_rebalance_rows": 7,
                    "max_abs_weight_diff": 0.125,
                },
                "specific_probe": "first_div=2025-02-03",
            },
        }
    }
    review_json = tmp_path / "synthetic_review.json"
    review_json.write_text(json.dumps(synthetic_review), encoding="utf-8")

    rc_r10 = toolkit.main(["--check-rule10", str(review_json)])
    out_r10 = capsys.readouterr()
    assert rc_r10 == 0
    assert "| `CF1` | 0 | 0 | 0.0000 | **UNADJUDICATED_ZERO_DELTA** |" in out_r10.out
    assert "| `CF2` | 42 | 7 | 0.1250 | **ADJUDICATED** |" in out_r10.out
