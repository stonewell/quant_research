[ [English](SCHEMAS.md) | 简体中文 ]

# `backtester` — 数据结构与 Schema

独立的 Schema 文档，与 `README_ZH.md`（涵盖安装/用法/CLI 参数）分开保存，避免 Schema 参考信息湮没在用法说明中。本项目消费在 `../common/README_ZH.md` (§1–4) 中记录的共享 **OHLCV DataFrame**、**标的池字典**、**目标权重 DataFrame** 和 **组合回测结果字典** 结构——请先参阅该文件。

## 输入：`--strategy-file`（`strategy.json` 文件）

Schema 归属于 `strategy_generator` 并由其记录——参阅 `../pipeline/strategy_generator/README_ZH.md` 的“数据结构与 Schema”章节了解完整字段列表，包括当 `template_name` 以 `pattern_` 开头时，本项目的 `_get_template()` 用于重建挖掘出的 `PatternBasedAllocationTemplate` 的 `pattern_spec` 块。仅 `template_name` 和 `params` 是严格必需的；其他所有内容均通过 `.get()` 读取。

同时支持：`research_strategy_spec` 块（字典或 `null`，默认 `null`），当 `research_strategy` 策略（`../pipeline/research_strategy/rs/strategy.py` 中的 17 个实现之一）赢得 `strategy_generator` 的搜索而非静态/挖掘模板时存在。恰好 2 个字段：`strategy_key`（字符串，来自 `pipeline/research_strategy/strategies_config.json` 的键，如 `"permanent_portfolio"`）与 `entry_data`（字典，即 `strategies_config.json[strategy_key]` 的精确原始条目）。`_get_template()` 在此块存在时进行读取，以通过 `research_strategy.rs.strategy.instantiate_strategy_from_config_entry(strategy_key, entry_data)` 重建确切的策略实例——这对于 `type: "class"` 和 `type: "natural_language"` 条目，以及对 `--mode standard` 和 `--mode walkforward`（在 Walk-Forward 窗口缓冲期间尊重复建实例的 `warmup_bars()`）均统一适用。

同时支持：`composite_spec` 块（字典或 `null`，默认 `null`），当 `strategy_generator` 的切面组合（`--no-compose-aspects` 可禁用；默认开启——参阅 `../pipeline/strategy_generator/README_ZH.md`）以跨两个不同来源模板的**混合**配对获胜而非单一完整模板时存在。始终包含 `track` 字段（`"allocation"` 或 `"timing"`），加上根据 track 类型决定的恰好 2 个额外字段：
- `track: "allocation"`（篮子模板，`common/strategy_aspects.py`）：`selection_key`（字符串，如 `"momentum_topn"`）+ `weighting_key`（字符串，如 `"inverse_vol"`）——在 `SELECTION_ASPECTS`/`WEIGHTING_ASPECTS` 中查找以构建 `CompositeAllocationTemplate`。
- `track: "timing"`（单资产模板，`../pipeline/research_strategy/rs/timing_aspects.py`）：`entry_key`（字符串，如 `"rsi_oversold_entry"`）+ `exit_key`（字符串，如 `"rsi_cross_exit"`）——在 `ENTRY_SIGNAL_ASPECTS`/`EXIT_RISK_ASPECTS` 中查找以构建 `CompositeTimingTemplate`。

`_get_template()` 还会将 strategy.json 顶层的 `params` 作为重建组合模板的 `default_params` 透传，因此 `--optimize` 的全新网格搜索会回退到实际调优后的数值，而非每个切面固定的硬编码默认值。`_load_strategy_file()` 校验 `track` 为两个允许值之一且存在对应的键对，否则抛出命名了缺失键的 `ValueError`。

同时支持：`fundamental_spec` 块（字典或 `null`，默认 `null`）——一个平凡标记，固定为 `{"source": "fundamental_screener"}`，当策略文件由独立的 `fundamental_screener` 项目生成时存在（参阅 `../pipeline/fundamental_screener/README_ZH.md`）。`_get_template()` 仅凭其存在重建无参数的 `FundamentalMarginOfSafetyStrategy`，所有实际行为均来自策略文件顶层的 `params`（已被加载）——该标记仅用于标识来源并触发正确的导入。

同时支持：`bnn_spec` 块（字典或 `null`，默认 `null`）——与 `fundamental_spec` 相同形式的标记，固定为 `{"source": "bnn_forecaster"}`，当策略文件由独立的 `bnn_forecaster` 项目生成时存在（参阅 `../ml/bnn_forecaster/README_ZH.md`）。重建 `bnn_spec` 策略**要求**使用 `bnn_forecaster` 独立的 `uv` 环境运行 `backtester`（其 `autobnn`/`jax` 依赖未安装在 `pipeline` 的 venv 中）——例如从仓库根目录运行 `ml/bnn_forecaster/.venv/Scripts/python.exe backtester/run_backtest.py --strategy-file ml/bnn_forecaster/results/bnn_strategy.json ...`。

`pattern_spec`、`research_strategy_spec`、`composite_spec`、`fundamental_spec` 与 `bnn_spec` **全部互斥**——获胜的 strategy.json 中上述五个块最多只有一个非 `null`（或者均为空，对于普通静态模板）。`_load_strategy_file()` 强制执行此约束：若 `strategy.json` 中有多个块非 `null`，则抛出指出冲突的 `ValueError`，而非静默让 `_get_template()` 的固定检查顺序忽略除一个之外的所有块。

## 输出

### `results/backtest_equity.csv` (`--mode standard`)

共享回测结果字典的 `equity_curve` (`../common/README_ZH.md` §4)，按原样写入：一列 `equity`，`DatetimeIndex`。

### `results/backtest_weights.csv` (`--mode standard`)

共享回测结果字典的 `actual_weights` (`../common/README_ZH.md` §4) ——实际持有的**稠密**每日权重（漂移后、再平衡后），标的池中每个标的一列。

### `results/walkforward_report.csv` (`--mode walkforward`)

每个滚动窗口一行。列：`start_date`、`end_date`（字符串，`YYYY-MM-DD`）、`sharpe_ratio`、`cagr`、`max_drawdown`、`calmar_ratio`、`win_rate`、`profit_factor`（均为 `float`，若模板生成了空权重或空权益曲线则为 `NaN`）、`total_turnover`（`float`，`NaN` 窗口上为 `0.0`）、`total_rebalances`（`int`，`NaN` 窗口上为 `0`）。

当设置了 `--baseline-symbol` 时，附加 5 个额外列：`baseline_sharpe_ratio`、`baseline_cagr`、`baseline_max_drawdown`、`baseline_calmar_ratio`（基准运行同名的逐窗口指标）与 `outperformance`（`cagr - baseline_cagr`）。这些列通过 **`(start_date, end_date)` 而非行位置** 连接到策略的窗口行上——策略与基准的窗口列表来自独立加载的日历和位置计算，因此不保证逐行对应。在 `(start_date, end_date)` 上没有匹配基准窗口的策略窗口将在所有 5 列中获得 `NaN`，而非被丢弃。

### `results/baseline_equity.csv` (`--mode standard`，仅当设置了 `--baseline-symbol` 时)

基准运行的权益曲线（结构与上述 `backtest_equity.csv` 相同）——即 `--baseline-symbol`/`--baseline-template`/`--baseline-params` 运行的 `run_standard` 的 `equity_curve`。仅在 `--mode standard` 下写入；在 `--mode walkforward` 下，基准纯粹通过 `walkforward_report.csv` 中的 `baseline_*`/`outperformance` 窗口列以及下方 `comparison_report.json` 中的 `mean_baseline_*` 字段体现（无单一基准权益曲线可保存——基准本身是逐窗口计算的）。

### `results/comparison_report.json`（两种模式，仅当设置了 `--baseline-symbol` 时）

字段集按模式不同：

**`--mode standard`:** `baseline_symbol` (字符串)、`baseline_template` (字符串)、`baseline_params` (字典)、`baseline_sharpe_ratio`、`baseline_cagr`、`baseline_max_drawdown`、`baseline_calmar_ratio`、`strategy_sharpe_ratio`、`strategy_cagr`（均为 `float`），加上对比字段：`overlap_bars`（`int`，策略与基准权益曲线之间的重叠 K 线数）、`alpha`（`float`，年化）、`beta`（`float`）、`tracking_error`（`float`，年化）、`information_ratio`（`float`）、`outperformance_cagr`（`float`，`strategy_cagr - baseline_cagr`）。当 `overlap_bars < 2` 时，`alpha`/`beta`/`tracking_error`/`information_ratio`/`outperformance_cagr` 为 `null`。

**`--mode walkforward`:** `baseline_symbol` (字符串)、`baseline_template` (字符串)、`baseline_params` (字典)、`mean_baseline_sharpe_ratio`、`mean_baseline_cagr`（合并后的 `walkforward_report.csv` 基准列的均值）、`mean_outperformance_cagr`（合并后的 `outperformance` 列的均值）、`baseline_calendar_mismatch` (布尔值)。策略与基准的窗口列表在 `(start_date, end_date)` 上连接而非行位置（参阅上述 `walkforward_report.csv`）；若两个窗口列表均非空但连接匹配到了 0 行（例如因为主 `--universe` 标的一比 `--baseline-symbol` 历史短，移动了每个窗口的对齐起始/结束日期），则每个 `baseline_*`/`outperformance` 数值都将静默为 `NaN`——在退化情况下 `baseline_calendar_mismatch` 为 `true`（且控制台会打印 `WARNING:`），以便报告自诊断而非填满未解释的空值。在正常情况下为 `false`，包括仅部分窗口未匹配时。

### `results/walkforward_summary.json` (`--mode walkforward`，始终写入)

始终写入，独立于 `--baseline-symbol`。字段：`mean_sharpe_ratio`、`mean_cagr`、`mean_max_drawdown`、`mean_calmar_ratio`（所有窗口上的均值，`float`）、`n_folds`（`int`，总窗口数）、`n_valid_folds`（`int`，具有非 `NaN` `sharpe_ratio` 的窗口数）、`fold_sharpe_std`（`float`，有效窗口夏普比率的样本标准差 `ddof=1`）、`deflated_sharpe_ratio`（`float`，Bailey & Lopez de Prado 的平减夏普比率 Deflated Sharpe Ratio，将每个窗口视为 `n_valid_folds` 个独立试验之一）。当 `n_valid_folds < 2` 时，`fold_sharpe_std` 与 `deflated_sharpe_ratio` 均为 `null`（标准差/DSR 无法从少于 2 个窗口计算）。

### `results/optimize_report.json`（仅当设置了 `--optimize` 时，始终写入 --成功或失败）

由共享的 `common/allocation_search.py` 网格搜索 + 等效随机搜索（ERS）机制（`optimize_template()`）写入，即 `strategy_generator` 在内部使用的相同机制，在此应用于在当前运行的标的池/模式上重新调优**已被选择的**模板的 `param_grid`，而非静默信任策略文件的原始参数。字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `status` | `"success"` \| `"failed"` | 当且仅当获胜的网格搜索组合通过 ERS 验证（`trusted`）时为 `"success"`；否则为 `"failed"` |
| `reason` | 字符串或 `null` | 成功时为 `null`。失败时：ERS 百分位消息（`ers_passed` 为 `False`）或 `total_rebalances` 对比 `--min-rebalances-for-trust` 消息（`ers_passed` 为 `True` 但胜者再平衡不够频繁无法被信任） |
| `original_params` | 字典 | 策略文件的原始 `params`，未修改 |
| `original_result` | 字典 | 对 `original_params` 打分一次的 score_fn 结果，用于对比。结构因模式而异 --参阅下方 |
| `best_params` | 字典 | 获胜网格搜索组合的参数（当 `template.param_grid` 为空时与 `original_params` 相同，例如源自 `research_strategy_spec` 的模板） |
| `best_result` | 字典 | 对 `best_params` 打分的 score_fn 结果。与 `original_result` 具有相同的逐模式结构 |
| `ers_percentile` | 浮点数 | `best_params` 相比于等效随机搜索池的夏普百分位排名（`0.0`-`1.0`） |
| `ers_passed` | 布尔值 | `ers_percentile >= --ers-percentile-threshold` 是否成立 |
| `trusted` | 布尔值 | `ers_passed AND best_result["total_rebalances"] >= --min-rebalances-for-trust` --这实际上关口控制了最终回测是使用 `best_params` 还是回退到 `original_params` |
| `n_trials` | 整数 | 打分的（模板，参数）组合总数：网格组合 + `--n-random-search` |
| `improvement` | 字典 | `{"sharpe_ratio": best - original, "cagr": best - original}`（`best_result`/`original_result` 的 `sharpe_ratio`/`cagr` 键） |

`original_result`/`best_result` 的结构因模式而异：

- **`--mode standard`:** 扁平的 `run_standard()` 结果字典（`common/README_ZH.md` §4 结构）-- `sharpe_ratio`、`cagr`、`max_drawdown`、`calmar_ratio`、`win_rate`、`profit_factor`、`total_turnover`、`total_rebalances`，加上 `equity_curve`/`actual_weights`（DataFrame，在 JSON 中呈现为其 `str()` 形式，因为 DataFrame 不能原生 JSON 序列化 --对于实际权益曲线/权重，请改用 `backtest_equity.csv`/`backtest_weights.csv`，它们始终反映最终运行实际使用的参数：`best_params` 或 `original_params` 回退）。
- **`--mode walkforward`:** `sharpe_ratio`、`cagr`、`max_drawdown`、`calmar_ratio`（各自为该指标有限值窗口的均值，`float`；若每个窗口均为非有限值则 `sharpe_ratio` 为 `-inf`，在此情况下其他三个为 `NaN`）、`total_rebalances`/`total_turnover`（跨窗口求和）以及 `folds`（`run_walkforward()` 返回的完整逐窗口列表，与 `walkforward_report.csv` 的行结构相同）。因此在 `--mode walkforward` 下 `improvement.cagr`（`best_result` 的平均窗口 `cagr` 减去 `original_result`）也是实数，而非始终为 `null`。

无论 `status` 为何，反映在 `backtest_equity.csv`/`backtest_weights.csv`/`walkforward_report.csv`/本文档记录的其他所有内容中的回测，在成功时对应 `best_params`，在失败时对应 `original_params` -- `--optimize` 绝不会不输出结果，即使调优未通过验证。该结果直接复用了上述 `best_result`/`original_result`（`score_fn(template, params)` 已在网格搜索/原始打分期间计算），而非二次多余地调用 `run_standard`/`run_walkforward`。

### `results/equity_curve.png` (`--mode standard`，除非使用了 `--no-plots`)

策略权益曲线的 PNG 折线图（参阅 `common/plotting.py` 的 `plot_equity_curve`）。当设置了 `--baseline-symbol` 时，同一图表上会叠加第二条虚线表示基准权益曲线。在 `--mode walkforward` 下在任何标志组合下均不生成。
