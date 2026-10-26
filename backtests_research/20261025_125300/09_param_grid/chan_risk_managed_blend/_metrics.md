# `chan_risk_managed_blend`（前缀 `crb`）参数网格审计 — ATR 止损宽度 × 惯性闸门松紧

> 本文件为 `09_param_grid` 中 **`chan_risk_managed_blend`** 单策略的自包含审计记录。
> 另外两个策略（`chan_four_state_blend` / `chan_dual_hybrid_blend`）的同名网格由并行 worker 负责，不在本文件范围。
> 本文件仅解释"值"的连续变化是否可判定，不改动任何策略代码、不调整任何默认参数。

---

## 1. 口径（run geometry & 基线复用）

### 1.1 精确 CLI

六臂使用**完全相同**的一条命令，只有 `--strategy-params` 不同；几何、窗口、现金收益率与 `08_ablate_wf` 逐字一致，否则所有 Δ 都会被混淆：

```bash
cd /home/stone/Work/github/quant
PY=pipeline/.venv/bin/python
DUMPS=pipeline/research_strategy/results/strategy_dumps
UNIV=backtests_research/20261025_125300/universe_20_stocks.txt
BT_ROOT=backtests_research/20261025_125300
s=chan_risk_managed_blend
out_dir="$BT_ROOT/09_param_grid/<grid>/<arm>/$s"
mkdir -p "$out_dir"
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
```

- **折叠几何**：`--window-years 0.25 --step-years 0.25` ⇒ **18 折**，`n_valid_folds = 18`，`actual_first_fold_start = 2022-01-04`，`actual_last_fold_end = 2026-09-07`（六臂与基线全部一致）。
- **数据**：本机 DuckDB（`data/cache.duckdb`），`--data-provider fuyao`，全离线，无网络、无 yfinance。
- **参数前缀**：全部 6 臂只用 `crb_` 前缀键。绝不使用裸名（`stop_atr_mult` / `min_weight_change` / `wq_alpha_id`），因为 `resolve_param` 的第 4 级回退是 `p[name]`、第 8 级是 `getattr(cfg, name)`，裸名会泄漏全局 `StrategyConfig` 值（2026-10-21 `wq_alpha_id=101` 事故的确切机制）。
- **`stop_mode` 取值**：合法域为 `"atr_trailing"` / `"fixed_pct"` / `"none"`（`config.py:574`、`config.py:1188-1199`）；`stop_atr_mult` 校验 `> 0`（`config.py:1190`）。因此"关闭"只能写 `"none"`，**不是** `0`、**不是** `"disabled"`。`blend_base.py:875` 以 `if stop_mode in ("atr_trailing", "fixed_pct")` 判定，`"none"` 确实跳过止损段。
- **策略源文件未变**：`pipeline/research_strategy/results/strategy_dumps/chan_risk_managed_blend_strategy.json` mtime 为 `2026-10-24 21:11`（早于基线运行 `2026-10-25 17:27`），与基线 `strategy_dump.json` 逐字段比对后唯一差异是后者多出 backtester 注入的 `cli_strategy_params_override` 字段（`backtester/run_backtest.py:1181`），**参数与版本完全一致**。基线 manifest 记录的 `strategy_dump_sha256 = 91b1e51c…` 是"注入后"副本的哈希，故与源文件现值不同，属预期而非漂移。

### 1.2 基线复用（无 baseline 重跑）

参照系为 `08_ablate_wf/baseline/chan_risk_managed_blend/`（18 折）。以下数字**逐一从 `walkforward_summary.json` 读出并确认**，与任务方口述一致：

| 指标 | 文件实际值 |
| :--- | :--- |
| `mean_sharpe_ratio` | **1.5369624775774695**（≈ 1.5370） |
| `mean_cagr` | **0.278799920100602**（= 27.880%） |
| `mean_max_drawdown` | **0.04153871466951203**（= 4.1539%） |
| `mean_calmar_ratio` | **10.635205526647102** |
| `fold_sharpe_std` | **2.1843636155619697**（≈ 2.1844） |
| `deflated_sharpe_ratio` | 3.192478030031899e-41（打印为 `0.000`） |
| `n_folds` / `n_valid_folds` | 18 / 18 |
| `actual_last_fold_end` | 2026-09-07 |
| `total_rebalances`（18 折求和） | **550** |

结论：口述的四个基线数字（Sharpe 1.5370、CAGR 27.88%、MaxDD 4.15%、`fold_sharpe_std` 2.1844）**全部确认无误**。

### 1.3 `no_inertia` 惩罚 —— 由产物推导，不采信任何口述

来源：`08_ablate_wf/no_inertia/chan_risk_managed_blend/walkforward_summary.json`（同一几何、同一 18 折、同一 `--cash-sweep-rate 0.015`）。

| | baseline | `no_inertia` | Δ |
| :--- | ---: | ---: | ---: |
| `mean_sharpe_ratio` | 1.53696 | 1.33779 | **−0.19917** |
| `mean_cagr` | 27.8800% | 27.5430% | **−0.33701 pp** |
| `mean_max_drawdown` | 4.1539% | 4.6747% | +0.52080 pp（恶化） |
| `mean_calmar_ratio` | 10.6352 | 10.2711 | −0.36409 |
| `total_rebalances` | 550 | 1012 | **+462（×1.84）** |

**裁决性结论：两个候选值中 `−0.34pp` 正确，`−2.73pp` 错误。** 真实惩罚为 `mean_cagr` 的 **−0.3370pp**。

其推论必须明说：**在 walkforward 口径下，本策略的惯性闸门（`0.05` / `2`）本身只值约 0.34pp CAGR**。它确实"承重"（关闭后调仓次数几乎翻倍 550→1012，Sharpe −0.199，回撤 +0.52pp），但承重换来的年化收益极小。因此"收紧几乎无下行可保护"这一判断成立 —— 但见 §4，实测损害出现在**反方向**：过度收紧比彻底关闭贵一个数量级。

### 1.4 两条必要 caveat

1. **`mean_max_drawdown` 是 63 根 K 线（≈1 季）的折叠内回撤，不是全路径回撤。** 18 折均值不能相加、不能当作"策略历史最大回撤"。相邻折之间的复利叠加可能显著更深；本文件所有 DD 结论只能读作"折叠内风险"的相对比较。
2. **`--cash-sweep-rate 0.015` 与 `03_r2b_oos` 的 `0%` 口径不同。** 本网格只能与同样用 1.5% 的 `08_ablate_wf` 系列横比，**不可**与 `03_r2b_oos` 的数字直接对比。该 1.5% 现金收益率并非装饰：它抬高了空仓期的收益，对"降仓位/降暴露"的臂（`atr_none`、高惯性臂）尤其有利。

---

## 2. 逐臂强制校验（防"静默忽略 override"的假结果）

六臂全部通过。子策略类清单与基线**逐臂一致**，故没有任何一臂因 `wq_alpha_id` 裸名碰撞而被偷偷换掉第三子策略。

**基线子策略类清单**（`08_ablate_wf/baseline/chan_risk_managed_blend/strategy_dump.json` → `.version_info.sub_strategies`）：

```
chan_composite     -> ChanCompositeStrategy
chan_three_type    -> ChanThreeTypeStrategy
chan_vaa_compound  -> ChanVaaCompoundStrategy
```

| arm | dump `.params`（`stop_atr_mult`/`stop_mode`/`min_weight_change`/`min_holding_bars`） | 子策略类清单 | `ignoring unknown key(s)` | `Traceback` | `status`/`mode` | `cli_args.options.cash_sweep_rate` | `cli_args.options.strategy_params` |
| :--- | :--- | :--- | ---: | ---: | :--- | :--- | :--- |
| `atr_6_0` | `6.0` / `atr_trailing` / `0.05` / `2` | 与基线一致 | 0 | 0 | completed / walkforward | `0.015`（number） | `{"crb_stop_atr_mult": 6.0}` |
| `atr_8_0` | `8.0` / `atr_trailing` / `0.05` / `2` | 与基线一致 | 0 | 0 | completed / walkforward | `0.015`（number） | `{"crb_stop_atr_mult": 8.0}` |
| `atr_none` | `4.5` / **`none`** / `0.05` / `2` | 与基线一致 | 0 | 0 | completed / walkforward | `0.015`（number） | `{"crb_stop_mode": "none"}` |
| `inertia_008_4` | `4.5` / `atr_trailing` / `0.08` / `4` | 与基线一致 | 0 | 0 | completed / walkforward | `0.015`（number） | `{"crb_min_weight_change": 0.08, "crb_min_holding_bars": 4}` |
| `inertia_010_5` | `4.5` / `atr_trailing` / `0.10` / `5` | 与基线一致 | 0 | 0 | completed / walkforward | `0.015`（number） | `{"crb_min_weight_change": 0.1, "crb_min_holding_bars": 5}` |
| `inertia_015_10` | `4.5` / `atr_trailing` / `0.15` / `10` | 与基线一致 | 0 | 0 | completed / walkforward | `0.015`（number） | `{"crb_min_weight_change": 0.15, "crb_min_holding_bars": 10}` |

每臂的 `strategy_dump.json` → `.cli_strategy_params_override` 也都精确回显了该臂的 override 对象（未覆盖的键保持默认），与 `run_manifest.json` 的 `cli_args.options.strategy_params` 相互印证。

**provenance commit `ae39020` 现场回归结论：正常。** 基线运行于 `8552c4b1`（`ae39020` 之前），其 manifest **没有** `cli_args` 键（只有 `cli_overrides`）；本批六臂在 `ae39020`（当前 HEAD）上重跑后，`cli_args.argv` / `cli_args.options` 均已落盘，且 `cash_sweep_rate` 是 **JSON number `0.015`** 而非字符串 —— 即 `0%` 与 `1.5%` 的混淆风险已被结构性消除。**未发现缺陷，无需修复。**

> 复现注记：首轮并行启动因本文档作者的 shell 引号处理缺陷（`xargs` 词分割吞掉 JSON 双引号）导致 5 个进程在 `json.loads` 处即失败（`rc=1`），未产生任何指标产物；发现后改用 `case` 构造 `extra` 重跑全部臂。上表与 §3/§4 的所有数字均来自修复后的最终一次干净运行（六个目录均为完整 8 文件集，无残留半成品）。

---

## 3. Grid A —— ATR 移动止损宽度

目录：`09_param_grid/atr/<arm>/chan_risk_managed_blend/`。`crb_stop_mode` 恒为 `atr_trailing`（`atr_none` 臂除外），`crb_stop_atr_mult` 由 4.5 起放宽。

| arm | `mean_sharpe_ratio` | `mean_cagr` | `mean_max_drawdown` | `mean_calmar_ratio` | `total_rebalances` | `deflated_sharpe_ratio` | ΔSharpe | ΔCAGR | ΔMaxDD | ΔCalmar |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **baseline**（4.5） | 1.5370 | 27.880% | 4.1539% | 10.6352 | 550 | 3.192e-41 | — | — | — | — |
| `atr_6_0` | 1.4679 | 27.372% | 4.4222% | 10.5772 | 550 | 1.771e-51 | −0.0691 | −0.51 pp | +0.27 pp | −0.0580 |
| `atr_8_0` | 1.6023 | 29.351% | 4.2756% | 10.7713 | 544 | 7.502e-42 | +0.0654 | +1.47 pp | +0.12 pp | +0.1360 |
| `atr_none` | 1.6509 | 29.850% | 4.3719% | 11.3091 | 520 | 1.034e-41 | +0.1140 | +1.97 pp | +0.22 pp | +0.6739 |

折内稳健性（对照基线逐折统计，共 18 折）：

| arm | 逐折 Sharpe 改善折数 | 逐折 CAGR 改善折数 | 逐折 MaxDD 改善折数 | `fold_sharpe_std` |
| :--- | ---: | ---: | ---: | ---: |
| `atr_6_0` | 9/18 | 10/18 | 9/18 | 2.2776 |
| `atr_8_0` | 7/18 | 8/18 | 9/18 | 2.2622 |
| `atr_none` | 9/18 | 9/18 | **6/18** | 2.3099 |

---

## 4. Grid B —— 惯性闸门收紧（`crb_adaptive_inertia` 保持 `True`，只调宽阈值）

目录：`09_param_grid/inertia/<arm>/chan_risk_managed_blend/`。基线为 `0.05` / `2`。

| arm | `mean_sharpe_ratio` | `mean_cagr` | `mean_max_drawdown` | `mean_calmar_ratio` | `total_rebalances` | `deflated_sharpe_ratio` | ΔSharpe | ΔCAGR | ΔMaxDD | ΔCalmar | Δ调仓（占比） |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **baseline**（0.05/2） | 1.5370 | 27.880% | 4.1539% | 10.6352 | 550 | 3.192e-41 | — | — | — | — | — |
| `inertia_008_4` | 1.5429 | 31.729% | 4.9199% | 9.7641 | **343** | 2.312e-32 | +0.0059 | **+3.85 pp** | +0.77 pp | −0.8711 | **−207（−37.6%）** |
| `inertia_010_5` | 1.2147 | 23.752% | 5.1881% | 7.5096 | **281** | 4.010e-46 | −0.3223 | **−4.13 pp** | +1.03 pp | −3.1256 | **−269（−48.9%）** |
| `inertia_015_10` | 1.4182 | 22.073% | 4.8563% | 7.9900 | **249** | 1.451e-49 | −0.1188 | **−5.81 pp** | +0.70 pp | −2.6452 | **−301（−54.7%）** |

折内稳健性：

| arm | 逐折 Sharpe 改善折数 | 逐折 CAGR 改善折数 | 逐折 MaxDD 改善折数 | `fold_sharpe_std` |
| :--- | ---: | ---: | ---: | ---: |
| `inertia_008_4` | 10/18 | 9/18 | **3/18** | 2.0271 |
| `inertia_010_5` | 9/18 | 8/18 | **2/18** | 1.9384 |
| `inertia_015_10` | 9/18 | 7/18 | **4/18** | 2.1964 |

对照 `no_inertia` 惩罚 `−0.3370pp`（§1.3）：`inertia_010_5` 的 CAGR 损失是它的 **12.2 倍**，`inertia_015_10` 是 **17.2 倍**。

---

## 5. 裁决

### 5.1 假设 A（ATR 移动止损过紧）—— **不成立（在其被陈述的形式上）**

判定条件：CAGR **与** MaxDD 同时沿假设方向单调移动，**且**改善幅度超过 `fold_sharpe_std = 2.1844`。

- **CAGR 单调性：成立。** 沿放宽方向 `6.0 → 8.0 → none` 为 27.372% → 29.351% → 29.850%，单调递增。
- **MaxDD 单调性：不成立。** 4.4222% → 4.2756% → 4.3719%，在 `8.0` 处出现谷值后回升，非单调；更关键的是**三臂全部劣于基线 4.1539%**（+0.27 / +0.12 / +0.22 pp）。原假设"关闭止损后回撤没有恶化"在 `crb` 上**被证伪** —— 逐折看 `atr_none` 只有 6/18 折回撤改善。
- **幅度对噪声：完全没有超出。** ΔSharpe 最大 +0.1140，仅为 `fold_sharpe_std`（2.1844）的 **5.2%**；即使改用均值标准误 `2.1844/√18 = 0.515`，仍然只有 0.22 个标准误。逐折 CAGR 改善折数 8–10/18，Sharpe 改善 7–9/18，都是纯抛硬币。

⇒ 结论：**落在折间噪声之内，判定为"未证实"，而非"已证实"。** 不采纳"放宽/关闭 ATR 止损"作为改进；`atr_none` 的 +1.97pp CAGR 同时换来 +0.22pp 折叠内回撤与 −30 次调仓，且风险调整后（Sharpe）差异不可与噪声分离。

### 5.2 假设 B（惯性闸门可收紧）—— **仅在 `0.08/4` 这一点上成立，其余两点被否**

判定条件：`total_rebalances` 显著下降，**且** CAGR 损失远低于 `no_inertia` 惩罚 `−0.3370pp`。

| arm | 调仓是否显著下降 | CAGR 相对惩罚 | 判定 |
| :--- | :--- | :--- | :--- |
| `inertia_008_4` | 是（−37.6%） | **+3.85pp（是增益，不是损失）**，远低于 −0.337pp | **通过** |
| `inertia_010_5` | 是（−48.9%） | −4.13pp = 惩罚的 **12.2×** | **拒绝** |
| `inertia_015_10` | 是（−54.7%） | −5.81pp = 惩罚的 **17.2×** | **拒绝** |

**必须同时说清的三件事（否则"通过"会被误读）：**

1. **`−0.34pp` 这个真实惩罚直接改变了叙事。** 闸门在 walkforward 里只值 0.34pp CAGR，所以"收紧有下行风险需要防范"这个前提几乎不成立 —— 但也正因如此，**"收紧"的实测代价（−4 ~ −6pp）反而是"彻底关闭"代价的 12–17 倍**。损害的方向是反的：过紧远比关掉更贵。把闸门描述成"承重且单调可紧"是错的；真实曲面在基线 `0.05/2` 附近**单峰**，右侧一步就是悬崖。
2. **`inertia_008_4` 的通过不是风险调整后的胜利。** ΔSharpe 仅 +0.0059（≈ 0），`mean_calmar_ratio` 从 10.6352 降到 9.7641（−0.87），`mean_max_drawdown` 恶化 +0.77pp，且**逐折只有 3/18 折回撤改善**。CAGR +3.85pp 与"调仓次数 −37.6%"同时发生，机制上更像"更少调仓 ⇒ 仓位更集中/持有更久 ⇒ 波动与回撤同步放大"的杠杆效应，而非选股/择时 alpha 提升。它是一项**换手经济性**的收益，不是风险调整收益的改善。
3. **单点通过不构成稳健性。** `0.08/4` 相邻的 `0.10/5` 立刻崩掉 4.13pp，说明 `0.08/4` 无安全边际，属于局部脊线上的孤立点，不宜直接写入实盘默认。

### 5.3 关于"承重"的再表述

`no_inertia` 证据支持"闸门有效但价值很小"：关闭后调仓 550→1012（×1.84）、Sharpe −0.199、折叠内回撤 +0.52pp、CAGR −0.34pp。所以闸门应当**保留在基线值**；但它不值得被收紧 —— 收紧不是"提高门槛换取更少噪声"，而是在一个已经接近最优点、且价值仅 0.34pp 的旋钮上向悬崖一侧移动。

---

## 6. 禁用指标声明

`deflated_sharpe_ratio` 在本口径下**已饱和，不能用于给臂排序**。

- 代码事实：walkforward 模式下 `n_trials` 直接取 `n_valid_folds`（`backtester/run_backtest.py:1786`：`n_trials=n_valid_folds`），本批全部为 **`n_trials = n_folds = 18`**。
- 运行日志原样：`Deflated Sharpe Ratio: 0.000 (n_trials=18, fold Sharpe std=…)`，**基线本身也是 `0.000`**。
- 表内原始值（`atr_6_0` 1.771e-51 … `inertia_008_4` 2.312e-32）全部在 `1e-32 ~ 1e-51` 量级，实质为 0，且**大小顺序与 Sharpe 顺序无关**（例如 `atr_6_0` 的 DSR 比 `atr_8_0` 小 9 个数量级，尽管两者 Sharpe 只差 0.13）。这是因为 `fold_sharpe_std ≈ 2.0–2.3` 极大，`expected_max_sharpe(18, ~2.2)` 远超任何观测 Sharpe。

⇒ 因此：`n_trials` 被几何固定为 18、而非真实搜索次数，DSR 在此只反映"折间 Sharpe 离散度"，**本文件所有排序均不使用 DSR**，仅按 `mean_sharpe_ratio` / `mean_cagr` / `mean_max_drawdown` / `total_rebalances` 与逐折胜率判断。

---

## 7. 结论

- **假设 A（ATR 止损过紧）：证伪。** 决定数字：三臂折叠内回撤全部劣于基线（4.4222% / 4.2756% / 4.3719% vs 4.1539%），MaxDD 非单调，且最大 ΔSharpe 仅 **+0.1140**，为 `fold_sharpe_std = 2.1844` 的 5.2%（0.22 个均值标准误）—— 幅度完全在折间噪声内。
- **假设 B（惯性闸门可收紧）：仅在 `crb_min_weight_change=0.08` / `crb_min_holding_bars=4` 上成立（且是换手经济性而非风险调整收益），其余两点证伪。** 决定数字：`0.08/4` 调仓 550→343（−37.6%）而 CAGR **+3.85pp**；但 `0.10/5` 与 `0.15/10` 的 CAGR 为 **−4.13pp / −5.81pp**，分别是真实 `no_inertia` 惩罚（**−0.3370pp**）的 **12.2× / 17.2×**。
- **闸门是否"承重"：是，但只值 0.34pp。** 决定数字：关闭后调仓 ×1.84、Sharpe −0.199、折叠内回撤 +0.52pp、CAGR −0.3370pp —— 故保留基线 `0.05/2`，不做任何收紧；过紧的代价是关闭的 12–17 倍，方向性证据反对收紧。
