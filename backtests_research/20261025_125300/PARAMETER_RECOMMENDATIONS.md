# 策略参数变更建议（证据导向）

> **依据**：`FORENSIC_AUDIT.md`（法医审计）、`BEAR_WINDOW_OOS.md`（熊市样本外）、`ABLATION_AND_ANATOMY.md`（standard 模式 13 臂消融）、`ABLATION_WALKFORWARD.md`（walkforward 模式 13 臂复核）、`08_ablate_wf/*/_metrics.md`，以及 `pipeline/research_strategy/rs/config.py` 与 `pipeline/research/blend/blend_base.py` 的实际机制。
> **日期**：2026-10-25
> **一句话结论**：本组证据支持 **0 项可直接落地的点值变更**、**1 项「维持现状」的确定性否决**（`target_bull_exposure` 保持 0.80）、以及 **2 项方向可信但量级未定的假设**（放宽 ATR 止损、收紧换手惯性闸）。其余看起来「可优化」的参数一律维持现状，理由逐条列于 §3。

---

## 1. 一页结论

| 排序 | 建议 | 类别 | 裁决 |
| --: | :--- | :--- | :--- |
| 1 | **保持 `target_bull_exposure = 0.80`，明确否决升到 1.0** | 否决项（无需变更） | 唯一「三策略 × 两模式」同向的确定性结论：升 1.0 只会确定性加深回撤；CAGR/Sharpe 两模式符号相反，不可主张收益侧 |
| 2 | **假设 A：放宽 / 关闭 ATR 跟踪止损**（`stop_atr_mult` 4.5 → 试 6.0 / 8.0 / `disabled`） | 实验期 (a)，采纳才升 (b) | 方向可信（6/6 格 CAGR 升、DD 持平或降），但量级全部落在噪声地板内 ⇒ 只给方向 |
| 3 | **假设 B：收紧换手惯性闸**（`min_weight_change` 0.05↑ / `min_holding_bars` 2↑） | 实验期 (a)，采纳才升 (b) | 惯性 on/off 是唯一双向一致的强结论，但只测过开关、从未测过收紧取值 ⇒ 纯外推假设 |
| 4 | **否决：收紧熔断阈值 / 「标准化」cfsb 权重模式 / 删除正交化·波动率目标·低贝塔惯性** | 否决项 | 熔断收紧方向反了（本就近乎惰性）；`regime_ic_ir_hybrid` 是 PINNED 部署态；其余落在噪声内或符号矛盾 |
| 5 | **结构性：周频 / 事件驱动调仓门控** | (c) 新代码，本轮不做 | 换手根因是子策略每 2–3 天重发 >5% 目标变动；参数闸只能缓解，根治需改重平衡调度逻辑 |

**类别定义**：(a) 研究配置层——只改 `strategies_config.json` 或 `--strategy-params`，**不**升版本、**不**改测试、**不**重 dump；(b) 类默认值改动——必须升该类 `version`、追加 `docs/version.md` 中文条目、同步两个 `test_*_versioning.py` 断言、跑 `--dump-strategies` 重 dump（AGENTS 规则 7）；(c) 需新代码，不属于参数调整。

---

## 2. 变更建议表

| 参数 | 当前值 | 建议 | 类别 | 证据 | 机制 | 预期方向 | 证伪实验 / 裁决指标 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `target_bull_exposure`（cfsb/crb/cdhb 三前缀） | 0.80 | **保持 0.80，不升 1.0** | 否决 | walkforward ΔMaxDD：cfsb +0.56pp、crb +1.25pp、cdhb +0.87pp；standard 三策略 MaxDD 全恶化 +3.61 / +2.80 / +1.77pp；cdhb 整段 11.8066% → 13.5727% | 0.8 敞口在 bull breadth 区间把约 20% 名义仓留在现金（1.5% 计息），等价压低满仓等效回撤 | 升 1.0 ⇒ MaxDD 确定性变深；收益侧两模式符号相反 | 无需重跑。若再确认：baseline vs `bull_full` 的 `mean_max_drawdown` 是否一致上升 |
| `stop_atr_mult` / `stop_mode` | 4.5 / `atr_trailing` | **假设**：试 6.0 / 8.0 / `disabled` | (a) 仅实验 | 关掉止损后 CAGR **6/6 格全升**：standard +2.52 / +0.98 / +1.01pp；walkforward cfsb +0.23、crb +2.08、cdhb +0.62pp（crb ΔSharpe +0.125） | `atr_trailing` 在震荡里反复触发止损，只削收益、既没换来更低 DD 也没降低摩擦 | 放宽 ⇒ CAGR↑、DD 不升；**量级 <±3pp / ±0.15，不得给点值** | 18 折 walkforward，`stop_atr_mult ∈ {4.5, 6.0, 8.0, disabled}`，看 `mean_sharpe` / `mean_max_drawdown` 是否单调改善且越出 `fold_sharpe_std`（≈2.2–2.4） |
| `min_weight_change` / `min_holding_bars` | 0.05 / 2 | **假设**：0.08/4、0.10/5、0.15/10 网格 | (a) 仅实验 | 关掉惯性（`no_inertia`）后调仓次数 +82~97%（cfsb 442 → 839）、ΔSharpe −0.184 / −0.199 / −0.139、CAGR −2.7~−3.7pp；当前每 2.2–2.6 个交易日调一次仓 | 子策略每 2–3 天重发 >5% 目标变动；惯性闸把重平衡次数减半（唯一双向一致的强结论）。**收紧取值从未被测过** | 调仓次数↓、摩擦↓；但摩擦仅占净利 5–9%（二阶项），CAGR 方向未定（过紧会吃 alpha） | 18 折对比 `{0.05/2, 0.08/4, 0.10/5, 0.15/10}`，看 `total_rebalances` 降且 `mean_cagr` / `mean_sharpe` 不显著变差；反证门槛：收紧造成的 CAGR 损失必须远小于 `no_inertia` 的 −2.7~−3.7pp |
| `dd_reduce_thresh` / `dd_defensive_thresh` / `dd_stop_thresh` | 0.10 / 0.15 / 0.20 | **保持，否决收紧** | 否决 | 折级触发率仅 1/18（cfsb）、0/18（crb、cdhb）；standard 模式下 `no_circuit` 1161/1161 bar 完全 ≡ | `fast_recovery = thrust_active or r_10d > 0 or breadth ≥ 0.60`（`blend_base.py:988`）几乎恒真 ⇒ 熔断被旁路；cdhb 的 `elastic_drawdown_reset`（`:622`）直接重置峰净值，整体覆盖熔断 | 收紧阈值 ⇒ 触发更少，与目标方向相反 | 无需。若坚持，须先证明 `no_circuit` 折级触发率 > 0 |
| `cfsb.blend_weighting_mode` | `regime_ic_ir_hybrid` | **保持，否决「标准化」** | 否决 | AGENTS.md 记录的 2026-10-22 部署态例外；断言 `test_cfsb_deployed_regime_mode_and_inertia_ratio_config` | 部署态专用模式。曾有一次 `--dump-strategies` 静默把它切回 `rolling_ic_ir`，同时使 regime 分支防御（桶 delta-clamp / thrust 滞后）惰性 | — | 无需 |
| 正交化 / 波动率目标 / 低贝塔惯性 | 现默认 | **保持** | 否决 | `no_voltarget` ΔSharpe 全在噪声地板内（≤0.091 < 0.15）；`no_orthogon` 三策略符号矛盾（crb −0.1935 → +0.2452，cdhb +0.1230 → −0.0568）；`no_lowbeta` 在 standard 下实质惰性（相对差 1.6e-4）而 walkforward 下非 ≡ | 三者均不可主张：或噪声内、或反向矛盾、或标准模式下的 ≡ 来自相互抵消而非惰性 | — | 无需 |

---

## 3. 明确不建议改的项（逐条给「为什么证据不支持」）

1. **把 `target_bull_exposure` 升到 1.0**：三策略两模式 MaxDD 全升（walkforward +0.56 / +1.25 / +0.87pp；standard +3.61 / +2.80 / +1.77pp），是整批消融里唯一在回撤侧三策略同向的结论；但收益侧（CAGR/Sharpe）两模式符号相反 ⇒ **「抬高回撤」可信、「抬高收益」不可信**。维持 0.80。
2. **收紧熔断阈值**：`no_circuit` 折级触发 0~1/18，说明熔断本就几乎不触发；收紧只会让它更不触发。方向与目标相反。
3. **「标准化」cfsb 的 `blend_weighting_mode`**：Pinned 部署态，历史上一次 `--dump-strategies` 就静默切回 `rolling_ic_ir` 并使 regime 防御惰性（`docs/version.md` §8）。严禁改动。
4. **删除波动率目标 / 正交化 / 低贝塔惯性**：`no_voltarget` 全落在噪声地板内、`no_orthogon` 符号矛盾、`no_lowbeta` 在 standard 下实质惰性。**「测不到正贡献」≠「应当删除」**。
5. **把 standard 模式的 `≡` / ≈ 当作「模块无用」**：已由两个反例证伪——`no_lowbeta` 在 cdhb 上 standard ≡ 但 walkforward 非 ≡（492 对齐、15 处不匹配、`max|Δ| = 792.52`）；`no_circuit` 在 cfsb 上 standard ≡ 但 walkforward 非 ≡（442 对齐、20 个日期不同、全部落在折 4）。标准模式的相等可能来自**相互抵消**，不构成「可安全移除」的证据。

---

## 4. 要先补的实验（改任何参数之前）

当前证据只支持方向、不给点值；换手闸更是只测过 on/off。**改默认值之前必须先做取值扫描。**

统一 CLI（18 折，`0.25y / 0.25y`，`2022-01-04 → 2026-10-22`，`actual_last_fold_end = 2026-09-07`，尾部 27 根 bar 不进入任何折）：

```bash
pipeline/.venv/bin/python backtester/run_backtest.py \
  --strategy-file <dump.json> \
  --universe-file backtests_research/20261025_125300/universe_20_stocks.txt \
  --start 2022-01-04 --end 2026-10-22 \
  --mode walkforward --window-years 0.25 --step-years 0.25 \
  --data-provider fuyao --min-shares 100 --china-trading \
  --cash-sweep-rate 0.015 --baseline-symbol 000300.SH \
  --results-dir <new_results_dir>
```

- **假设 A 网格**（前缀按策略替换 `cfsb_` / `crb_` / `cdhb_`）：
  `--strategy-params '{"cfsb_stop_atr_mult": 6.0}'`、`{"cfsb_stop_atr_mult": 8.0}`、`{"cfsb_stop_mode": "disabled"}`。
- **假设 B 网格**：`{"cfsb_min_weight_change": 0.08, "cfsb_min_holding_bars": 4}`；`{0.10, 5}`；`{0.15, 10}`。

**裁决指标**：`mean_sharpe`、`mean_max_drawdown`、`total_rebalances`、`fold_sharpe_std`。**不得用 DSR**——18 折下 `n_trials = n_folds`，去膨胀门槛 sr₀ ≈ 4.05–4.10，39 个 run 全部落在 ≤ 1.377e-19，无分辨力。

**重 dump 提醒**：假设 A/B 若最终采纳为**类默认值**（改 `rs/config.py` 或策略类），才触发 (b) 类义务。纯 CLI / `strategies_config.json` 实验不升版本。

---

## 5. 风险与陷阱

1. **换手不在打分 rubric 里**：`FORENSIC_AUDIT.md` 的 Safety Grade 不直接扣换手分（摩擦仅 MINIMAL 档，换手只在 FLAG 列标注）。所以「收紧换手闸」不会体现在评级上，只能靠 `total_rebalances` / Σ换手 / 摩擦率单独验证——别期待它「升级评级」。
2. **`{pfx}_` vs `blend_` 前缀陷阱**：`resolve_param`（`blend_base.py:214`）的解析链是 `{pfx}_{name}` → `alt_prefix` → `blend_{name}` → 裸 `p[name]` → `getattr(cfg, name)`。写覆盖必须用**策略前缀键**；写成 `blend_*` 会被更靠前的前缀键遮蔽或漏进全局默认；写成与全局 `StrategyConfig` 同名的裸键则是 `wq_alpha_id` 那次事故的根因。
3. **cash-sweep 0.015 vs 0% 不可混比**：本批全部按 `--cash-sweep-rate 0.015` 计息，而 `03_r2b_oos` 是 **0%** 现金收益。任何新实验必须复现 `0.015`，否则空仓 30%–39% 的现金计息差会系统性偏移 CAGR。
4. **per-fold MaxDD ≠ full-path MaxDD**：walkforward 的 `mean_max_drawdown`（4.15%–4.69%）是 **63 根 bar 的折内**平均值；同窗口 standard 整段回撤是 **11.8066%**（cdhb，1161 bar）。讨论「4% / 6.6% / 11.81%」时必须先对齐口径，不可相减。
5. **DSR 已饱和、不可作裁决指标**：本网格 39/39 全部 `≤ 1.377e-19`。请用 `mean_sharpe` / `mean_max_drawdown` / `total_rebalances` 与 `fold_sharpe_std`。
6. **噪声地板必须守住**：同一策略仅换评估窗就产生 ±10pp CAGR / ±0.3–0.5 Sharpe 的漂移；故 `|ΔCAGR| < 3pp 且 |ΔSharpe| < 0.15` 的符号一律不可主张。假设 A 的 6/6 格增益全部落在此区间内——这正是它只能算「方向」的原因。
7. **6.6% 风险预算的来源**：6.6% 是把 cdhb `03_r2b_oos` 的 4.0% 按 39.2% 常态现金垫归一化后的「满仓等效」值；同窗口 standard 整段实测 11.8066% 已高于 6.6%。按 6.6% 配资金前，必须先决定「是否相信现金垫永续」——`bull_full` 的 DD 加深方向支持「现金垫确实压回撤」，但**给不出 6.6% 这个点估计**。

---

## 6. 口径与基线（供复算）

walkforward 基线（18 折，`2022-01-04 → 2026-09-07`，cash-sweep 1.5%）：

| 策略 | Sharpe | CAGR | MaxDD（折内均值） | Calmar | `fold_sharpe_std` | 去膨胀门槛 sr₀ |
| :--- | --: | --: | --: | --: | --: | --: |
| `chan_four_state_blend` | 1.4329 | 30.15% | 4.69% | 10.9135 | 2.2836 | 4.2336 |
| `chan_risk_managed_blend` | 1.5370 | 27.88% | 4.15% | 10.6352 | 2.1844 | 4.0495 |
| `chan_dual_hybrid_blend` | 1.4250 | 25.63% | 4.22% | 10.2096 | 2.2141 | 4.1046 |

同窗口 standard 整段回撤参考：cdhb **11.8066%**（`06_ablate/baseline/chan_dual_hybrid_blend`，1161 bar）。
cfsb 的 standard `blend_weighting_mode` 为 `regime_ic_ir_hybrid`（部署态，见 §3 第 3 条）。
