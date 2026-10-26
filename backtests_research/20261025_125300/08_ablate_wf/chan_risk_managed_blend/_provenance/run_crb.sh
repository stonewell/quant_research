#!/usr/bin/env bash
# Walkforward ablation runner -- chan_risk_managed_blend (prefix crb) ONLY.
# Arm `extra` JSON copied VERBATIM from docs/backtest_research.md §8 run_arm, with pfx=crb.
# Scope: backtests_research/20261025_125300/08_ablate_wf/<arm>/chan_risk_managed_blend/
set -u
cd /home/stone/Work/github/quant

PY=pipeline/.venv/bin/python
DUMPS=pipeline/research_strategy/results/strategy_dumps
UNIV=backtests_research/20261025_125300/universe_20_stocks.txt
BT_ROOT=backtests_research/20261025_125300
s=chan_risk_managed_blend
pfx=crb
SCRATCH=/tmp/opencode/ablwf

mkdir -p "$SCRATCH"

run_arm () {
  local arm="$1"
  local extra
  case "$arm" in
    baseline)        extra='{}' ;;
    no_voltarget)    extra="{\"${pfx}_enable_vol_targeting\": false}" ;;
    no_orthogon)     extra='{"blend_enable_orthogonalization": false}' ;;
    no_structregime) extra='{"blend_enable_structural_regime": false}' ;;
    no_cyq)          extra='{"blend_enable_cyq_quality_gate": false}' ;;
    no_atrstop)      extra='{"blend_enable_adaptive_stop_atr": false}' ;;
    no_lowbeta)      extra='{"blend_enable_low_beta_inertia": false}' ;;
    no_sectorthrot)  extra='{"blend_max_assets_per_sector": 20}' ;;
    no_hysteresis)   extra='{"blend_enable_regime_hysteresis": false}' ;;
    no_thrust)       extra="{\"${pfx}_dynamic_cash_deployment\": false}" ;;
    no_inertia)      extra="{\"${pfx}_adaptive_inertia\": false, \"${pfx}_min_weight_change\": 0.0, \"${pfx}_min_holding_bars\": 0, \"${pfx}_entry_threshold\": 0.0}" ;;
    bull_full)       extra="{\"${pfx}_target_bull_exposure\": 1.0}" ;;
    no_circuit)      extra="{\"${pfx}_dd_reduce_thresh\": 10.0, \"${pfx}_dd_defensive_thresh\": 10.0, \"${pfx}_dd_stop_thresh\": 10.0}" ;;
    *) echo "unknown arm: $arm" >&2; return 1 ;;
  esac
  local out_dir="$BT_ROOT/08_ablate_wf/$arm/$s"
  mkdir -p "$out_dir"
  echo "=== ARM $arm :: extra=$extra ===" 
  echo "ARM=$arm EXTRA=$extra" >> "$SCRATCH/crb_extra.txt"
  $PY backtester/run_backtest.py \
    --strategy-file "$DUMPS/${s}_strategy.json" \
    --universe-file "$UNIV" \
    --start 2022-01-04 --end 2026-10-22 \
    --mode walkforward --window-years 0.25 --step-years 0.25 \
    --data-provider fuyao --min-shares 100 --china-trading \
    --baseline-symbol 000300.SH \
    --cash-sweep-rate 0.015 \
    --strategy-params "$extra" \
    --results-dir "$out_dir" --n-jobs 1 \
    2>&1 | tee "$out_dir/run.log"
  echo "=== DONE $arm rc=${PIPESTATUS[0]} ==="
}

for arm in "$@"; do
  run_arm "$arm"
done
