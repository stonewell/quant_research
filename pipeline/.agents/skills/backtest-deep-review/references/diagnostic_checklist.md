# Backtest Deep-Review Diagnostic Checklist & Empirical Failure-Mode Catalog

This reference accompanies [`../SKILL.md`](../SKILL.md) and documents the exact 6-gate
exit formulas, Rule 10 zero-delta guardrails, 8 empirically verified quantitative failure
modes from `MS28` and `CFSB v1.8.0 (N=100)`, and the standard deep-review report template.

---

## 1. Six-Gate Exit Audit Thresholds & Formulas

| Gate | Metric / Formula | Pass Threshold | Toolkit Helper |
| :--- | :--- | :--- | :--- |
| **Gate 1 (Option B)** | Mean 18-fold $\Delta\text{SR}$, positive fold count, paired $t = \bar{d} / (s_d / \sqrt{K})$, $\text{DSR}(M)$ | $\Delta\text{SR} \ge +0.15$, $\ge 14/18$ folds, $t \ge 2.0$, $\text{DSR}(M) \ge 0.95$ | `evaluate_option_b_gate` |
| **Gate 2 (ERS)** | Single-winner Equivalent Random Search (`n_random_search=50, seed=42`, Rule 4) | `percentile >= 0.90` and `trusted == True` | `common.allocation_search.run_ers_validation` |
| **Gate 3 (2× Cost)** | Full & 18-fold backtest at `0 bps` / `25 bps` / `50 bps` round-trip | $\Delta\text{SR}_{2\times} > 0$, friction `< 30%` of net PnL | `run_allocation_backtest` |
| **Gate 4 (LOO & Skew)** | Stateful per-stock PnL, Core/Sat split, Top-1/3/5 skew, re-simulated LOO-K | Top-1 `< 35%`, Top-3 `< 60%`, LOO min SR positive | `compute_superstar_and_group_attribution` |
| **Gate 5 (`MS5`)** | `asymmetric_long_only_factor_summary` on composite & sub-families | `short_leg_illusion == False`, `top_exc > 0`, `mono >= 0.60` when $N \ge 30$ | `common.factor_analyzer` |
| **Gate 6 (Rule 10 & Cash)** | `n_changed_weight_cells`, internal probes, $\text{MaxDD}_{\text{norm}}$, $\text{CAGR}_{\text{norm}}$, 2-sided turnover | `adjudicated == True` (`ADJUDICATED`), turnover `<= 10.0x/yr` | `verify_rule10_weight_diff`, `compute_cash_normalized_metrics` |

### Rule 10 Zero-Delta Enforcement (`UNADJUDICATED_ZERO_DELTA`)

Per `AGENTS.md` Rule 10, a comparative arm with `n_changed_weight_cells == 0` and no
measured divergence in its target internal probe proves only that the experiment could
not observe the intervention. It **never** proves the mechanism is harmless or inert.
Always report such arms as `UNADJUDICATED_ZERO_DELTA` and diagnose the wiring trap.

---

## 2. Catalog of 8 Empirically Verified Failure Modes

### FM-1: `1/K` Sub-Strategy Dilution vs Fixed `5%` `entry_thresh`
- **Code Site**: `pipeline/research/blend/blend_base.py:831-832, 1844, 1884`
- **Empirical Evidence (`CFSB v1.8.0`, `N=20` vs `N=100`)**:
  - `chan_three_type` (`45%` blend weight) averaged $K=33.6$ active stocks at `N=100`
    (median weight `2.44%`, only `2.9% >= 5%` vs `99.4%` at `N=20`).
  - Through Stage 6 (`6_post_vol_target`), `N=100` exposure matched `N=20` (`63.88%` vs
    `64.07%`). At Stage 7 (`7_post_entry_confirm`), `target_w < 0.05` killed **`19,107`
    diluted signals** (`33.30%/day` equity weight destroyed vs `1.02%/day` at `N=20`),
    collapsing exposure to `28.02%` (`27.05%` delivered, `72.95%` idle cash).
  - Stripping cash drag revealed strong underlying alpha: Cash-Normalized CAGR was
    **`22.88%`** (matching `N=20`'s `23.42%`), while Cash-Normalized MaxDD rose to
    **`28.33%`** (vs `20.28%` at `N=20`).

### FM-2: Sub-Strategy Internal Inertia Strangle
- **Code Sites**: `pipeline/research_strategy/rs/strategy.py:885` (`chan3_min_weight_change=0.02`),
  `pipeline/research_strategy/rs/chan_advanced_strategies.py:1607` (`chan_vaa_min_weight_change=0.02`),
  `pipeline/research_strategy/rs/chan_advanced_strategies.py:1812` (`chan_fse_min_weight_change=0.04`).
- **Empirical Evidence**: Applying fixed `2%–4%` internal inertia *after* $1/K$ scaling
  freezes sub-strategy updates at `N=100`. Zeroing internal inertia released `+7,043`
  non-zero cells in `chan_three_type` and `+42,542` non-zero cells in `chan_vaa_compound`.

### FM-3: Why Naive Threshold Lowering Fails at `N=100` (`CF2` / `CF3` Lesson)
- **Empirical Evidence (`CFSB v1.8.0`, 200k RMB)**:
  - Lowering blend gates to `entry_threshold=0.02` / `min_weight_change=0.025` (`CF2`,
    `15,454` changed cells) lifted `post_sequencer` exposure `30.3% -> 52.5%`, yet
    delivered 200k exposure **fell to `24.4%`** and 200k Sharpe dropped `0.8759 -> 0.8417`
    (`CAGR 6.55% -> 5.40%`).
  - Combining unstrangled `1%` sub-strategy inertia + `CF2` (`CF3`, `17,130` cells) pushed
    `post_sequencer` to `53.6%` but delivered 200k exposure fell to `22.1%` and Sharpe
    dropped to **`0.7346`** (`CAGR 4.19%`).
- **Architectural Takeaway**: Admitting 15–30 tiny `2%–3%` positions triggers massive
  A-share 100-share lot truncation and outer `5%` lifecycle rejection. Wide universes
  require **Top-$K$ cross-sectional concentration prior to `blend_base`**, not lower gates.

### FM-4: Outer Backtester Lifecycle Gate Value Verification (`CF4` Lesson)
- **Empirical Evidence (`CFSB v1.8.0`, `N=100`)**:
  - Disabling `run_allocation_backtest(enable_position_lifecycle=False)` (`CF4`) dropped
    200k Sharpe from **`0.8759 -> 0.6552`** (`CAGR 6.55% -> 4.68%`, `MaxDD 8.13% -> 8.76%`,
    turnover `40.48 -> 44.28`).
- **Takeaway**: Never assume a second lifecycle layer is redundant drag without running an
  isolated counterfactual — on `N=100`, it provided material whipsaw protection.

### FM-5: `CHINA_EQUITY_SECTORS` 21-Stock Hardcoded Map & 85/100 Prefix Collapse
- **Code Site**: `pipeline/research/blend/blend_base.py:83-106, 1697-1760`
- **Mechanism**: Only 15/100 stocks in `core_satellite_200k_stocks.txt` appear in the
  hardcoded 21-stock dict. The remaining 85 stocks collapse into 4 code-prefix buckets
  (`sec_60`: 48 stocks, `sec_00`: 25, `sec_30`: 6, `sec_68`: 6), generating **`1,674`**
  sector-cap blocks under `max_assets_per_sector=2` (vs `211` at `N=20`).

### FM-6: `DEFAULT_VAA_OFFENSIVE` US-Ticker Mismatch on China A-Shares
- **Code Site**: `pipeline/research_strategy/rs/chan_advanced_strategies.py:1491-1515`
- **Mechanism**: `offensive_universe = p.get("chan_vaa_offensive_universe", cfg.chan_vaa_offensive_universe)`
  defaults to `["SPY", "QQQ", "EFA", "EEM"]`, yielding `offensive_symbols = []` on A-shares
  and locking `ChanVaaCompoundStrategy` permanently into `"defensive"` mode (`chan_scaled * 0.5`
  capped at `0.30`).

### FM-7: `MS28` Cross-Sectional OLS Neutralization & `N=100` Monotonicity Reversal
- **Empirical Evidence (`MS28`, `N=100`, `2022-01-05..2026-11-20`)**:
  - Activating `MS1` `cs_neutralize_ols` in `mode="full"` (`n_sectors=13, k_regressors=14`
    on `1405/1405` active bars, `2,599` changed cells) did **not** repair `chip` or `micro`:
    `chip` worsened `IC_IR -1.0480 -> -1.9981` (`top_exc -16.40% -> -33.42%`, `mono=-0.90`),
    `micro` stayed `short_leg_illusion=True` (`long_ic_ratio=-1.0000`), and full 4-family
    `MS7` degraded `18f SR 0.6573 (B0) -> 0.5006 (B1)` (Full SR `0.4035 -> 0.0289`).

### FM-8: Late-IPO NaN Whole-Basket Starvation & Rule 10 Wiring Traps
1. **Late-IPO Starvation (`M1.2`)**: `CrossSectionalMomentumAllocation` produced `0.0000`
   Sharpe (0 trades) on `N=100` when 5 post-2021 IPO stocks (`688041.SH`, `600938.SH`,
   `600941.SH`, `601728.SH`, `605499.SH`) introduced pre-IPO `NaN`s.
2. **Rule 10 Wiring Trap (a) — Closure Binding (`CF1`)**: Monkey-patching
   `blend_base_mod._get_symbol_sector` *after* copying `blend_base_mod.__dict__` into an
   `exec` namespace leaves the compiled function bound to the original helper (`cells=0`
   → `UNADJUDICATED_ZERO_DELTA`). Always pass a mutable dispatch or patch before `exec`.
3. **Rule 10 Wiring Trap (b) — VAA Canary All-Positive Rule (`CF5`)**:
   `chan_advanced_strategies.py:1491` reads `p.get("chan_vaa_offensive_universe", ...)`,
   and `L1511` forces `"defensive"` whenever `len(off_scores) < len(offensive_symbols)` OR
   *any single* offensive symbol has `13612W <= 0`. Passing all 100 A-shares into a VAA
   canary guarantees `>= 1` negative score on 100% of dates (`cells=0` →
   `UNADJUDICATED_ZERO_DELTA`). A valid A-share VAA counterfactual must pass a compact
   3–5 index/ETF canary basket, not all 100 stocks.

---

## 3. Standard Deep-Review Report Template

Every deep-review Markdown report written to scratch should follow this 5-section layout:

1. **Header**: Timestamp, total runtime, bit-identity `max|dw|`, and protected DB MD5s.
2. **Section 1 — Stage-by-Stage Exposure Waterfall**: Sub-strategy signal density table +
   9-stage exposure waterfall (`N=20` vs `N=100`) + exact gate kill counts.
3. **Section 2 — 18-Fold Causal Regime Breakdown**: Per-fold dates, benchmark return,
   universe breadth, `N=20` vs `N=100` causal Sharpe, $\Delta\text{SR}$, CAGR, MDD, exposure.
4. **Section 3 — Six-Gate & `walkforward-audit` Forensics**: 11-row scorecard, Core vs
   Satellite breakdown, Top-5 Winners / Worst-10 Bleeders, LOO-K, 0/25/50 bps cost stress.
5. **Section 4 — Capital-Tier Lot-Rounding Audit**: `100k` vs `200k` vs `1M RMB` metrics,
   `< 1 lot` drop rate, and `> 25%` distortion rate.
6. **Section 5 — Counterfactual Matrix (`CF0..CFk`) & Verdict**: `100k` / `200k` results,
   Rule 10 probes (`ADJUDICATED` vs `UNADJUDICATED_ZERO_DELTA`), and GO / NO-GO decision.
