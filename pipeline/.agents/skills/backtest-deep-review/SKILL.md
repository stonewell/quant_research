---
name: backtest-deep-review
description: >-
  Conducts white-box in-process forensic deep reviews of quantitative strategies and
  multi-strategy blends across narrow (N=20) and wide (N=100) universes. Instruments
  stage-by-stage weight waterfalls inside generate_weights, decomposes 18-fold causal
  walk-forward regime divergence, enforces the 6-Gate Exit Audit (Option B paired t-test
  and DSR, single-winner ERS, 0/25/50 bps cost stress, true Leave-One-Out fragility, MS5
  long-only/monotonicity dissection, and Rule 10 discriminating probes with UNADJUDICATED
  zero-delta detection), audits capital-tier A-share 100-share lot-rounding realism
  (100k vs 200k vs 1M RMB), and runs read-only counterfactual ablations. Use whenever
  asking to deep review a strategy on a new or wide universe, diagnose N=20 to N=100
  degradation, inspect waterfalls, or audit 200k lot-rounding and counterfactuals.
---

# Backtest Deep Review & White-Box Forensic Audit

This skill provides a repeatable, strictly read-only 6-phase methodology and reusable
Python toolkit for **in-process white-box strategy deep reviews** across narrow (`N=20`)
and wide (`N=100`) equity universes.

---

## When to Use This Skill vs `walkforward-audit`

| Skill | Primary Scope | Input Source |
| :--- | :--- | :--- |
| **`walkforward-audit`** | Post-hoc directory scan & cross-strategy ranking | Persisted `walkforward_summary.json` & `walkforward_rebalances.csv` on disk |
| **`backtest-deep-review`** | In-process white-box instrumentation, `N=20 -> N=100` degradation, 6-gate exit audit, capital-tier lot-rounding, & counterfactual ablations | Live `AllocationTemplate` subclasses, isolated DuckDB caches, & deep-review JSONs |

---

## Working Directory & Interpreter Convention

> [!IMPORTANT]
> Run every command below from the **repository root** (`quant/`) using `pipeline/.venv/bin/python3`
> (never system `python3`). All toolkit commands are 100% offline-safe and strictly read-only.

```bash
# 100% offline synthetic smoke demonstration exercising all 6 audit functions
pipeline/.venv/bin/python3 pipeline/.agents/skills/backtest-deep-review/scripts/deep_review_toolkit.py \
  --demo-synthetic

# Summarize a saved deep-review JSON (supports CFSB deep-review & MS28 schemas)
pipeline/.venv/bin/python3 pipeline/.agents/skills/backtest-deep-review/scripts/deep_review_toolkit.py \
  --summarize-json <PATH_TO_REVIEW_JSON>

# Audit all counterfactual arms for Rule 10 discriminating power (flags zero-delta arms)
pipeline/.venv/bin/python3 pipeline/.agents/skills/backtest-deep-review/scripts/deep_review_toolkit.py \
  --check-rule10 <PATH_TO_REVIEW_JSON>
```

---

## 6-Phase Deep-Review Workflow

```text
Phase 1: Read-Only Isolation, Data Precheck & Bit-Identity Anchor (max|dw| == 0.0)
   │
   ▼
Phase 2: Stage-by-Stage Exposure Waterfall & Sub-Strategy Dilution Audit (9 Stages)
   │
   ▼
Phase 3: Cross-Universe (N=20 vs N=100) & 18-Fold Causal Regime Decomposition
   │
   ▼
Phase 4: Six-Gate Quantitative Exit Audit & Walkforward Forensics (Rule 4 / 9 / 10)
   │
   ▼
Phase 5: Capital-Tier & A-Share 100-Share Lot-Rounding Realism (100k / 200k / 1M RMB)
   │
   ▼
Phase 6: Read-Only Counterfactual Matrix (CF0..CFk) & GO / NO-GO Closure Protocol
```

### Phase 1: Read-Only Isolation, Data Precheck & Bit-Identity Anchor

1. **Zero Production Edits**: Keep `git status --short` clean throughout research. Place
   all experiment scripts and JSON/Markdown outputs in the session scratch directory.
2. **Database Checksum Guard**: Record `md5sum` of protected DuckDB files
   (`data/cache.duckdb`, `data/cache_research_ms.duckdb`) before and after execution.
   Build any new wide-universe dataset in a separate isolated DuckDB file using a single
   homogeneous provider (eliminating cross-source adjust-factor collisions) and drop
   BaoStock suspended-trading bars (`Volume.isna()`) before `store_bars`.
3. **Calendar Preservation**: Use `backtester/run_backtest.py::_resolve_master_calendar`
   (`min_coverage=0.20`) so late-IPO stocks do not truncate the entire universe calendar.
4. **Bit-Identity Gate**: Verify `verify_rule10_weight_diff(w_instrumented, w_prod)` yields
   `max_abs_weight_diff == 0.0` before trusting any internal stage telemetry.

### Phase 2: Stage-by-Stage Exposure Waterfall & Sub-Strategy Dilution Audit

1. Precompute and cache sub-strategy sparse weight panels (`sub_daily`) and enriched
   structural features once per universe so counterfactual sweeps run in seconds.
2. Instrument per-bar risky exposure ($\sum w$), active count ($K$), median weight, and
   exact veto/kill counters across all 9 stages:
   `1_post_ortho` → `2_post_cb` → `3_post_cyq` → `4_post_bull_scale` → `5_post_thrust` →
   `6_post_vol_target` → `7_post_entry_confirm` → `8_post_sequencer` → `9_delivered_bt`.
3. Run `summarize_stage_waterfall` to isolate the `primary_bottleneck_stage` and test each
   sub-strategy with its internal inertia threshold set to `0.0` vs default to detect
   internal signal strangulation.

### Phase 3: Cross-Universe (`N=20` vs `N=100`) & 18-Fold Causal Regime Decomposition

1. Evaluate 18 strictly causal walk-forward folds (`win=126`, warmup slice `max(0, f_start - 252)`)
   aligned against benchmark (`000300.SH`) return, universe breadth (`% > SMA50`), active
   exposure, held count, and Core vs Satellite dispersion.
2. Pinpoint which macro regimes (defensive value bear markets vs broad stimulus surges)
   drive cross-universe Sharpe and CAGR divergence.

### Phase 4: Six-Gate Quantitative Exit Audit & Walkforward Forensics

Evaluate all 6 mandatory exit gates (exact formulas in
[`./references/diagnostic_checklist.md`](./references/diagnostic_checklist.md)):

1. **Gate 1 — Option B Paired 18-Fold Gate** (`evaluate_option_b_gate`):
   $\Delta\text{Sharpe} \ge +0.15$, $\ge 14/18$ positive folds, paired $t \ge 2.0$, and
   $\text{DSR}(M) \ge 0.95$ penalized by the exact number of arms tested ($M$).
2. **Gate 2 — Single-Winner ERS (`AGENTS.md` Rule 4)**: Call `run_ers_validation`
   **exactly once** on the winning arm (`n_random_search=50, seed=42`); require `trusted=True`.
3. **Gate 3 — Friction Bleed & 0 / 25 / 50 bps (`2×`) Cost Stress**: Quantify gross-to-net
   decay and verify positive $\Delta\text{Sharpe}$ under `2×` friction.
4. **Gate 4 — Stateful Per-Stock Attribution & True Re-Simulated LOO**
   (`compute_superstar_and_group_attribution`): Extract Core vs Satellite PnL, Top-1/3/5
   superstar skew, chronic losers (`net_pnl < -1,000 RMB`, `ROI < -3%`, or friction
   `> 50%` of loss), and re-simulated LOO dropping Top-5 winners one by one.
5. **Gate 5 — `MS5` Asymmetric Long-Only & Monotonicity Dissection**: Inspect
   `short_leg_illusion`, `long_ic_contribution_ratio`, `top_group_excess_return_ann`, and
   `quantile_monotonicity_spearman` (`monotonicity_gating_allowed=True` when $N \ge 30$).
6. **Gate 6 — Rule 10 Probes & Cash-Normalized Efficiency** (`compute_cash_normalized_metrics`):
   Report $\text{MaxDD}_{\text{norm}}$ and $\text{CAGR}_{\text{norm}}$ (floored at `0.10`
   exposure), two-sided annualized turnover against the `10.0x/yr` ceiling, and exact
   Rule 10 probes on both sides of every A/B comparison.

### Phase 5: Capital-Tier & A-Share 100-Share Lot-Rounding Realism Audit

Use `audit_lot_rounding_realism` across `100,000 RMB`, `200,000 RMB`, and `1,000,000 RMB`
capital tiers to measure `< 1 lot` dropped target slots (`0 < target_val < 100 * price`)
and `> 25%` relative weight distortion. Never evaluate a `200k` live universe solely at
`100k` backtest capital without measuring the lot-truncation gap.

### Phase 6: Counterfactual Ablations (`CF0..CFk`) & GO / NO-GO Closure Protocol

1. Run targeted read-only counterfactuals testing each suspected bottleneck independently
   at both `100k` and `200k` capital.
2. Run `verify_rule10_weight_diff` (or `--check-rule10`) on every counterfactual arm. Any
   arm with `n_changed_weight_cells == 0` and unchanged internal counters **must** be
   labelled `UNADJUDICATED_ZERO_DELTA` — never reported as "inert" or "rejected".
3. Cross-reference `docs/development_plan.md` and `docs/version.md` (`Rule 2`, `Rule 8`,
   `Rule 9`). Only propose production changes if all exit gates pass; otherwise close as
   a documented **NO-GO** negative result with zero production edits.

---

## Reference Material & Toolkit

- **Failure-Mode Catalog, Gate Formulas & Report Template**:
  [`./references/diagnostic_checklist.md`](./references/diagnostic_checklist.md)
- **Reusable Python Audit Module & CLI**:
  [`./scripts/deep_review_toolkit.py`](./scripts/deep_review_toolkit.py)
