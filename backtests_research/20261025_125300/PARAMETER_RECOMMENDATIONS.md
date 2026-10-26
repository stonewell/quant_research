# 策略参数变更建议（证据导向）

> **依据**：`FORENSIC_AUDIT.md`（法医审计）、`BEAR_WINDOW_OOS.md`（熊市样本外）、`ABLATION_AND_ANATOMY.md`（standard 模式 13 臂消融）、`ABLATION_WALKFORWARD.md`（walkforward 模式 13 臂复核）、`08_ablate_wf/*/_metrics.md`、**`09_param_grid/SUMMARY.md`（本批已执行的两个 18 折取值网格，三策略 × 6 臂）**，以及 `pipeline/research_strategy/rs/config.py` 与 `pipeline/research/blend/blend_base.py` 的实际机制。
> **日期**：2026-10-25
> **一句话结论**：**0 项可落地变更 / 3 项确定性否决**（`target_bull_exposure` 升 1.0、熔断阈值收紧、`regime_ic_ir_hybrid`「标准化」）**/ 2 项假设已证伪**（放宽·关闭 ATR 止损、收紧换手惯性闸）。**本文件不授权任何参数变更**：两个假设都已在 §4 的 CLI 上实际执行（18 折、6 臂 × 3 策略 = 18 个 run、18/18 VALID），执行结果是「无跨策略一致方向 + 每个"最优"只由 2–4 折支撑」⇒ 部署值维持 `min_weight_change=0.05` / `min_holding_bars=2` 与 `stop_atr_mult=4.5` / `stop_mode="atr_trailing"`。

---

## 1. 一页结论

| 排序 | 建议 | 类别 | 裁决 |
| --: | :--- | :--- | :--- |
| 1 | **保持 `target_bull_exposure = 0.80`，明确否决升到 1.0** | 否决项（无需变更） | 唯一「三策略 × 两模式」同向的确定性结论：升 1.0 只会确定性加深回撤；CAGR/Sharpe 两模式符号相反，不可主张收益侧 |
| 2 | **假设 A：放宽 / 关闭 ATR 跟踪止损**（`stop_atr_mult` 4.5 → 6.0 / 8.0 / `stop_mode="none"`） | 已执行，**证伪** | `09_param_grid` 18 折实测（`SUMMARY.md` §3）：`cdhb` 三臂 CAGR **全部低于**基线（−1.0027 / −0.4019 / −0.1590pp）且 MaxDD **全部变差**；`crb` 三臂 MaxDD 也全变差（+0.27 / +0.12 / +0.22pp）；`cfsb` 的 `atr_none` 虽 +3.3912pp，但 ΔSharpe 仅 +0.1963 = `fold_sharpe_std` 2.2836 的 **8.6%**。且 `6.0` / `8.0` 两臂的均值增益只有 +0.30 / +0.14pp、各自删单折即翻负（`SUMMARY.md` §5.1）⇒ 那两臂是**一折宽的噪声**，对单调性没有信息量（`clip(·, 3.5, 7.5)` 是否把 8.0 压到 7.5：本网格无 `atr_7_5` 臂，**无法判定**）⇒ **不作任何改动，`4.5 / atr_trailing` 维持** |
| 3 | **假设 B：收紧换手惯性闸**（`min_weight_change` 0.05↑ / `min_holding_bars` 2↑） | 已执行，**不成立（无一致最优档）** | 18 折实测（`SUMMARY.md` §4–§5）：三策略是**三个不同的峰**（`cdhb` 0.08/4、`crb` 0.08/4 且下一步即悬崖、`cfsb` 0.10/5），去掉 3 / 2 / 4 个折后分别变成 −0.6594 / −0.7204 / −0.0140pp；`0.15/10` 三策略全拒 ⇒ **参数不动，`0.05/2` 维持**，且「收紧买到收益」**不可断言** |
| 4 | **否决：收紧熔断阈值 / 「标准化」cfsb 权重模式 / 删除正交化·波动率目标·低贝塔惯性** | 否决项 | 熔断收紧方向反了（本就近乎惰性）；`regime_ic_ir_hybrid` 是 PINNED 部署态；其余落在噪声内或符号矛盾 |
| 5 | **结构性：周频 / 事件驱动调仓门控** | (c) 新代码，本轮不做 | 换手根因是子策略每 2–3 天重发 >5% 目标变动；参数闸只能缓解，根治需改重平衡调度逻辑 |

**类别定义**：(a) 研究配置层——只改 `strategies_config.json` 或 `--strategy-params`，**不**升版本、**不**改测试、**不**重 dump；(b) 类默认值改动——必须升该类 `version`、追加 `docs/version.md` 中文条目、同步两个 `test_*_versioning.py` 断言、跑 `--dump-strategies` 重 dump（AGENTS 规则 7）；(c) 需新代码，不属于参数调整。

---

## 2. 变更建议表

| 参数 | 当前值 | 建议 | 类别 | 证据 | 机制 | 预期方向 | 证伪实验 / 裁决指标 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `target_bull_exposure`（cfsb/crb/cdhb 三前缀） | 0.80 | **保持 0.80，不升 1.0** | 否决 | walkforward ΔMaxDD：cfsb +0.56pp、crb +1.25pp、cdhb +0.87pp；standard 三策略 MaxDD 全恶化 +3.61 / +2.80 / +1.77pp；cdhb 整段 11.8066% → 13.5727% | 0.8 敞口在 bull breadth 区间把约 20% 名义仓留在现金（1.5% 计息），等价压低满仓等效回撤 | 升 1.0 ⇒ MaxDD 确定性变深；收益侧两模式符号相反 | 无需重跑。若再确认：baseline vs `bull_full` 的 `mean_max_drawdown` 是否一致上升 |
| `stop_atr_mult` / `stop_mode` | 4.5 / `atr_trailing` | **维持 4.5 / `atr_trailing`；6.0 / 8.0 / `none` 三臂已测，全部不采纳** | (a) 已执行，结论**否决** | 18 折实测（`09_param_grid/atr/*`）：`cdhb` ΔCAGR **−1.0027 / −0.4019 / −0.1590pp（全部为负）**、ΔMaxDD **+0.1597 / +0.0089 / +0.2137pp（全部更差）**；`crb` ΔMaxDD 三臂全差（+0.27 / +0.12 / +0.22pp）；`cfsb` 唯一亮点 `atr_none` +3.3912pp（配对 t=2.22），但 ΔSharpe +0.1963 = 8.6% × `fold_sharpe_std`，且该臂需 **k=6** 折才能翻号 | 上一轮 `no_atrstop` 那句「关掉止损 6/6 格 CAGR 升」测的是**自适应乘数被钉死为常数 4.5×**，**不是**止损被移除（`ABLATION_AND_ANATOMY.md` §7.1 勘误）；真正移除（`stop_mode="none"`）在 `cdhb` 上符号相反。`6.0`/`8.0` 两臂的均值增益只有 +0.30 / +0.14pp、各自删单折即翻负 ⇒ **一折宽的噪声，对单调性没有信息量**；`clip(·, 3.5, 7.5)` 是否把 8.0 压到 7.5：本网格无 `atr_7_5` 臂，**无法判定**（`SUMMARY.md` §3.2） | **无方向可主张**（符号三策略不一致、量级全在噪声内） | 不信 DSR（§5 第 5 条）。若要重测"更紧 / 更松"，必须换 `{3.5, 4.0, 4.5}` **配合** `blend_enable_adaptive_stop_atr=false`，不要再用 6.0 / 8.0 |
| `min_weight_change` / `min_holding_bars` | 0.05 / 2 | **维持 0.05 / 2；0.08/4、0.10/5、0.15/10 三档已测，全部不采纳** | (a) 已执行，结论**否决** | 18 折实测（`09_param_grid/inertia/*`）：Σ调仓 **9/9 臂全降**（−28.3% ~ −55.3%）、Σ`total_turnover` **−28.4% ~ −54.7%**；但最优档三策略各异——`cdhb` 0.08/4 +4.9450pp、`crb` 0.08/4 +3.8488pp（下一档 0.10/5 净差 **−7.9763pp**）、`cfsb` 0.10/5 +5.7464pp——去掉 **3 / 2 / 4** 折后分别变成 **−0.6594 / −0.7204 / −0.0140pp**；`0.15/10` 三策略全拒（−3.4466 / −5.8074 / −1.2713pp）。关掉惯性的代价（`no_inertia`）：调仓次数（18 折 `total_rebalances` 之和）**535 → 970**（+81%）、ΔSharpe −0.184 / −0.199 / −0.139、CAGR −2.7~−3.7pp（standard 口径；walkforward 口径逐策略实测 **−2.3403 / −1.951 / −0.3370pp**） | 惯性与换手的关系是**单峰 + 悬崖**，不是"越紧越好"：`0.15/10` 比彻底拆闸更贵（`cdhb` −3.4466 vs −2.3403pp，`crb` −5.8074 vs −0.3370pp）。且三档收紧把折内 MaxDD **单调推深**（9/9 臂 ΔMaxDD 为正） | 换手侧**确定下降**（机械量、无噪声）；**收益侧不可断言** | 本批折上不再选点（同折内择点＝选择后拟合）。若坚持落地，须先换**未参与本轮挑选的新时间窗**复验 |
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
6. **把 `stop_atr_mult` 放宽到 6.0 / 8.0，或把 `stop_mode` 改成 `"none"`**：已执行、已证伪（`09_param_grid/SUMMARY.md` §3）。三点决定性理由：① `cdhb` 三臂 CAGR **全部低于**基线且 MaxDD **全部更差**（符号与假设相反）；② `crb` 三臂 MaxDD 也全部更差（+0.27 / +0.12 / +0.22pp），`cfsb` 的 `atr_none` +3.3912pp 只有 `fold_sharpe_std` 的 8.6%；③ `6.0`/`8.0` 两臂的均值增益只有 +0.30 / +0.14pp、各自删单折即翻负（`SUMMARY.md` §5.1）⇒ **那两臂是一折宽的噪声，对单调性没有信息量**；`clip(·, 3.5, 7.5)` 是否把 8.0 压缩到 7.5：本网格没有 `atr_7_5` 臂，**无法判定**——本批对"放宽"的证据力是缺的，对"关闭"的证据是反向的。维持 4.5 / `atr_trailing`。
7. **把换手闸收紧到 0.08/4、0.10/5 或 0.15/10**：已执行、结论否决（`SUMMARY.md` §4–§5）。**换手侧确定下降**（Σ调仓 −28.3% ~ −55.3%、Σ换手 −28.4% ~ −54.7%，9/9 臂全降），但**收益增量不可归因**：三个"最优"分别只需 3 / 2 / 4 折就能翻成负号，`crb` 的折中位数只有 +0.1841pp（9 上 9 下），`0.15/10` 在三策略上全部亏钱（−3.4466 / −5.8074 / −1.2713pp）。**「收紧不损收益」可辩护，「收紧买到收益」不可断言**——而前者不足以推翻 0.05/2。

---

## 4. 已执行的取值扫描（两个网格均已跑完；结论：不改）

本节**不再是提案**：§4.1 的 CLI 已按原样执行（三策略 × 6 臂 = **18 个 run**，`18/18` `status=completed` / `mode=walkforward` / `n_folds=n_valid_folds=18` / `actual_last_fold_end=2026-09-07`，逐项 VERIFY 见 `09_param_grid/SUMMARY.md` §2.2），结果与裁决见 `09_param_grid/SUMMARY.md`。此处的 CLI 保留，是因为它**逐字记录了实际跑的是什么**。

### 4.1 统一 CLI（18 折，`0.25y / 0.25y`，`2022-01-04 → 2026-10-22`，`actual_last_fold_end = 2026-09-07`，尾部 27 根 bar 不进入任何折）

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

### 4.2 六个臂的 `--strategy-params`（`{pfx}` = `cdhb` / `cfsb` / `crb`，仅前缀不同）

| 网格 | arm | `--strategy-params` |
| :--- | :--- | :--- |
| A | `atr_6_0` | `{"${pfx}_stop_atr_mult": 6.0}` |
| A | `atr_8_0` | `{"${pfx}_stop_atr_mult": 8.0}` |
| A | `atr_none` | `{"${pfx}_stop_mode": "none"}` |
| B | `inertia_008_4` | `{"${pfx}_min_weight_change": 0.08, "${pfx}_min_holding_bars": 4}` |
| B | `inertia_010_5` | `{"${pfx}_min_weight_change": 0.10, "${pfx}_min_holding_bars": 5}` |
| B | `inertia_015_10` | `{"${pfx}_min_weight_change": 0.15, "${pfx}_min_holding_bars": 10}` |

**`stop_mode` 的合法值只有三个：`"atr_trailing"` / `"fixed_pct"` / `"none"`**（`pipeline/research_strategy/rs/config.py:574`、`:1188-1199`），`stop_atr_mult` 被校验 `> 0`（`:1190`）。**已更正**：旧稿在此处写过一个**不在合法集内**的字符串当"关闭"值；本轮 18 个 run 的实测确认只有 `"none"` 能真正跳过止损段（`blend_base.py:875` 的守卫 `if stop_mode in ("atr_trailing", "fixed_pct")`），写非法字符串或 `0` 会在校验处直接报错，**不是**一种可用的"关闭"写法。

### 4.3 裁决指标

`mean_sharpe_ratio`、`mean_max_drawdown`、`total_rebalances`、`fold_sharpe_std`，外加**逐折 Δ 的符号翻转所需折数 k**（`09_param_grid/SUMMARY.md` §5）。**不得用 DSR**——18 折下 `n_trials = n_folds = 18`，`08_ablate_wf` 的去膨胀门槛 sr₀ ≈ 4.05–4.10、39 个 run 全部落在 ≤ 1.377e-19；本批网格 sr₀ ≈ **3.45–4.32**、均值 Sharpe 1.40–1.76，同样全部饱和在 `1e-56 ~ 1e-21`，无分辨力。

### 4.4 重 dump 提醒

假设 A/B 若最终采纳为**类默认值**（改 `rs/config.py` 或策略类），才触发 (b) 类义务。纯 CLI / `strategies_config.json` 实验不升版本。**本批结论是不采纳，故 `version` / `BASE_VERSION` / `strategy_dumps` 全部不动。**

---

## 5. 风险与陷阱

1. **换手原先不在打分 rubric 里（2026-10-25 起改为"有上限、仍不逐项扣分"）**：`FORENSIC_AUDIT.md` 的 Safety Grade 不直接扣换手分（摩擦仅 MINIMAL 档，换手只在 FLAG 列标注）。所以「收紧换手闸」不会体现在评级上，只能靠 `total_rebalances` / Σ换手 / 摩擦率单独验证——别期待它「升级评级」。**（2026-10-25 更新：评分上限已裁定为双边 10.0×/年 = 单边 5.0×，见 `SUMMARY.md` §9 第 3 条；按该上限，本批 9 个 ATR 臂与三策略基线全部超线，只有 `inertia_010_5` / `inertia_015_10` 六格在限内。）**
2. **`{pfx}_` vs `blend_` 前缀陷阱**：`resolve_param`（`blend_base.py:214`）的解析链是 `{pfx}_{name}` → `alt_prefix` → `blend_{name}` → 裸 `p[name]` → `getattr(cfg, name)`。写覆盖必须用**策略前缀键**；写成 `blend_*` 会被更靠前的前缀键遮蔽或漏进全局默认；写成与全局 `StrategyConfig` 同名的裸键则是 `wq_alpha_id` 那次事故的根因。
3. **cash-sweep 0.015 vs 0% 不可混比**：本批全部按 `--cash-sweep-rate 0.015` 计息，而 `03_r2b_oos` 是 **0%** 现金收益。任何新实验必须复现 `0.015`，否则空仓 30%–39% 的现金计息差会系统性偏移 CAGR。
4. **per-fold MaxDD ≠ full-path MaxDD**：walkforward 的 `mean_max_drawdown`（4.15%–5.41%）是 **63 根 bar 的折内**平均值；同窗口 standard 整段回撤是 **11.8066%**（cdhb，1161 bar）。讨论「4% / 6.6% / 11.81%」时必须先对齐口径，不可相减。
5. **DSR 已饱和、不可作裁决指标**：`08_ablate_wf` 39/39 全部 `≤ 1.377e-19`，本批 18/18 落在 `1e-56 ~ 1e-21`。请用 `mean_sharpe` / `mean_max_drawdown` / `total_rebalances` 与 `fold_sharpe_std`。
6. **噪声地板必须守住**：同一策略仅换评估窗就产生 ±10pp CAGR / ±0.3–0.5 Sharpe 的漂移；故 `|ΔCAGR| < 3pp 且 |ΔSharpe| < 0.15` 的符号一律不可主张。假设 A 的 6/6 格增益全部落在此区间内（且其主体已被 §7 更正），这正是它只能算「方向」的原因。
7. **6.6% 风险预算的来源**：6.6% 是把 cdhb `03_r2b_oos` 的 4.0% 按 39.2% 常态现金垫归一化后的「满仓等效」值；同窗口 standard 整段实测 11.8066% 已高于 6.6%。按 6.6% 配资金前，必须先决定「是否相信现金垫永续」——`bull_full` 的 DD 加深方向支持「现金垫确实压回撤」，但**给不出 6.6% 这个点估计**。本批 18 个臂的 `mean_max_drawdown` 全部落在 4.15%–5.41%，同样是折内 63 bar 口径，**既不能支持也不能反驳 6.6%**。
8. **`walkforward_summary.json` 里的 `baseline_cagr` / `baseline_sharpe_ratio` / `baseline_max_drawdown` 不是策略基线，是冻结的 `000300.SH` 等权基准列**。`comparison_report.json` 写明 `baseline_symbol = 000300.SH`、`baseline_template = equal_weight`、`baseline_params.rebalance_freq_days = 5`、`mean_baseline_cagr = 0.06016900475744777`（≈6.0169%）；实测 `08_ablate_wf` 的 39 个叶 + `09_param_grid` 的 18 个 run = **57 个 leaf × 18 折，该列只有 1 条不同序列**。它与 `08_ablate_wf/baseline/<strategy>/` 的逐折 `cagr` 相差**逐折最大 147.52pp（cdhb）/ 192.13pp（cfsb）/ 162.26pp（crb）**、均值层面相差 **19.61 / 24.13 / 21.86pp**。⇒ **任何 Δ 都必须对 `08_ablate_wf/baseline/<strategy>/walkforward_summary.json` 计算**，拿臂内 `baseline_*` 当基线会把「相对大盘的超额」误读成「相对策略基线的改善」。
9. **留一折（LOO）在本批是伪检验**：去掉单折后的均值 = (18·m − d_i)/17，只有 `|d_i| > 18·m` 才可能翻号；本批所有"均值为正"的臂最大单折都远小于 18×均值（cdhb 38.09 < 89.01、crb 47.85 < 69.28、cfsb 40.19 < 103.43），**"18/18 次 LOO 全为正"是构造上必然，不是稳健性证据**。反过来，单折 `|ΔCAGR|` 就超过全样本均值的折数在 cdhb / crb 上分别是 **11/18** 与 **12/18**——一折摆动比整个"增益"还大。判据请用 **k-fold removal**（`SUMMARY.md` §5）。

---

## 6. 口径与基线（供复算）

walkforward 基线（18 折，`2022-01-04 → 2026-09-07`，cash-sweep 1.5%）：

| 策略 | Sharpe | CAGR | MaxDD（折内均值） | Calmar | `fold_sharpe_std` | 去膨胀门槛 sr₀ | Σrebalances |
| :--- | --: | --: | --: | --: | --: | --: | --: |
| `chan_four_state_blend` | 1.4329 | 30.15% | 4.69% | 10.9135 | 2.2836 | 4.2336 | 535 |
| `chan_risk_managed_blend` | 1.5370 | 27.88% | 4.15% | 10.6352 | 2.1844 | 4.0495 | 550 |
| `chan_dual_hybrid_blend` | 1.4250 | 25.63% | 4.22% | 10.2096 | 2.2141 | 4.1046 | 628 |

同窗口 standard 整段回撤参考：cdhb **11.8066%**（`06_ablate/baseline/chan_dual_hybrid_blend`，1161 bar）。
cfsb 的 standard `blend_weighting_mode` 为 `regime_ic_ir_hybrid`（部署态，见 §3 第 3 条）。
本表与 `09_param_grid/SUMMARY.md` §3/§4 的基线逐位一致（未取整值：`cdhb` 1.4249700144569886 / 0.25627364561917376 / 0.04215945583509294；`cfsb` 1.4328704609192824 / 0.30151320606816717 / 0.04687693080691772；`crb` 1.5369624775774695 / 0.278799920100602 / 0.04153871466951203）。

---

## 7. 方法学结论：一次「臂标签被当成语义」的误读

本批最值得留下来的不是任何 pp 数字，而是**一个流程缺陷**：

- **发生过什么**：上一轮把 `08_ablate_wf/no_atrstop` 的 +2.52 / +0.98 / +1.01pp 读成「关掉 ATR 止损后三策略 CAGR 全部上升」，并据此提出「放宽 / 关闭止损」的假设（旧稿 §1 第 2 行、§2 第 2 行）。但该臂的唯一 override 是 `{"blend_enable_adaptive_stop_atr": false}`，**它从未触碰 `stop_mode`**——`stop_mode` 仍是 `atr_trailing`、`stop_atr_mult` 仍是 4.5，止损照旧生效；它关掉的是 `med_vol/s_vol` 的自适应放大/收缩，使 `eff_stop_mult` 从 `clip(·, 3.5, 7.5)` 退回常数 4.5（`blend_base.py:891-896`）。
- **为什么是"标签当语义"而不是"数字错"**：`docs/backtest_research.md` §8 的 `no_atrstop` 行与其后「语义边界披露」**早已写明**——关闭后「回退至固定 4.5x ATR 追踪止损，**并不是**彻底关闭底层 ATR 止损机制本身（彻底关闭需设置 `"${pfx}_stop_mode": "none"`）」。本批报告 §1.3 声明臂定义**逐字取自该来源**，却在解释结果时**没有回读自己引用的定义**。数字全对，语义全错。
- **代价有多大**：一句错标把研究方向整体带偏——真正的「关闭」臂（`atr_none`）在 `cdhb` 上是 **−0.1590pp / MaxDD +0.2137pp（更差）**，与旧稿预测**符号相反**；而在 `crb`/`cfsb` 上是 +1.9697 / +3.3912pp（`SUMMARY.md` §6），**三策略符号不一致**。也就是说，旧稿不只是"量级不准"，它把一个**策略依赖**的效应写成了**跨策略单调**的规律。
- **防复发规则（写进本文件，后续沿用）**：
  1. 引用任何消融臂前，必须回读该臂的 `run_manifest.json.cli_overrides` **与** `strategy_dump.json.params` 两份落地证据，而不是只读臂名；
  2. 臂名里的 `no_*` 前缀**不携带语义承诺**——凡断言"某机制被移除"，须给出"该机制入口条件未满足"的代码行（如 `blend_base.py:875` 的 `stop_mode` 守卫）；
  3. 同一组数字在被**两个不同主体**（如"自适应乘数"与"止损")解释过之后，必须重跑**字面语义**的臂再下结论，不得在旧数字上叠加新解读。
- **范围声明**：本节只修"主体与语义"，不改任何数字；`ABLATION_AND_ANATOMY.md` §7.1 的勘误与本批 `09_param_grid/SUMMARY.md` §6 互为完整口径（勘误的反向证据仅限 `cdhb`，三策略口径见 `SUMMARY.md` §6）。
