[ [English](README.md) | 简体中文 ]

# `common` — 共享代码与数据 Schema

本工作区中每个项目（`backtester`、`instrument_selection`、`research_strategy`、`strategy_generator`）共享的代码：市场数据加载（`data.py`）、标的池解析（`universe.py`）、技术指标（`indicators.py`、`indicator_features.py`）、Hurst 指数（`hurst.py`）、性能指标（`metrics.py`）、组合配置回测器（`allocation_backtester.py`）及其模板（`allocation_templates.py`）、共享网格搜索 + 等效随机搜索 (ERS) 验证（`allocation_search.py`）、共享因子分类学（`factor_taxonomy.py`）、再平衡调度（`scheduling.py`）、合成数据测试生成器（`testing.py`）、每个 `run_*.py` 入口的共享 CLI 脚手架（`cli_utils.py`）、共享输出写入规范（`reporting.py`）、共享打乱置换/安慰剂零假设显著性检验原语（`significance.py`）以及共享图表生成（`plotting.py`）。

**本文件是本工作区中由 2 个或以上项目共享的每个 DataFrame/数据集结构的唯一权威来源。** 每个项目自身的 README 仅记录其真正独特的 Schema，对于任何共享内容均链接回此处——参阅本文件底部的交叉参考索引。如果某 Schema 定义已在下方记录，请勿在项目 README 中重复定义。

---

## 1. OHLCV DataFrame

通用价格数据结构。由 `data.py` 中的每个 `BaseDataProvider`（`YFinanceDataProvider`、`CSVFolderDataProvider`、`SyntheticDataProvider`、`CachedDataProvider`、`MarketDBDataProvider`、`FuyaoDataProvider`）及 `common/testing.py` 的合成生成器生成；被 `indicators.py`/`indicator_features.py` 中的每个指标函数及每个 `AllocationTemplate` 消费。

| | |
|---|---|
| **索引 (Index)** | `pd.DatetimeIndex`，升序，每个交易日一行 |
| **列 (Columns)** | `Open`、`High`、`Low`、`Close`（均为 `float`）——来自真实 `BaseDataProvider` 和 `SyntheticDataProvider` 的数据均包含 `Volume`（`float`），但来自 `common/testing.py` 的裸辅助函数（`make_ohlcv_from_closes`、`make_random_walk_df`、`make_oscillating_df`、`make_trending_pullback_df`、`make_ar1_ohlcv`）**不包含**该列——需要 `Volume` 的代码（例如 `common.indicators.obv`）在测试中必须使用基于 `SyntheticDataProvider` 的数据，而非裸辅助函数 |
| **不变性 (Invariant)** | 每行满足 `High >= Close >= Low` 且 `High >= Open >= Low`（并非每个合成生成器都单独强制约束，但真实数据和 `SyntheticDataProvider` 均满足） |

## 2. 标的池字典 (Universe dict)

`Dict[str, pd.DataFrame]` —— 标的代码 -> 该标的自身的 OHLCV DataFrame (§1)。传递给每个 `AllocationTemplate.generate_weights(universe, params)`、`run_allocation_backtest(universe, ...)` 及每个项目逐标的指标函数的标准结构。由 `common.data.load_universe`/`load_ohlcv`（循环）及 `common.universe.resolve_universe_from_args`（仅代码——解析为价格需通过 `load_universe`）生成。跨标的池的标的代码并不保证共享完全相同的 `DatetimeIndex`（上市历史不同），除非调用方明确对其进行对齐（例如 `backtester/run_backtest.py` 的 `_align_universe` 内连接）。

## 3. 目标权重 DataFrame（稀疏 Sparse）

每个 `AllocationTemplate.generate_weights(universe, params)` 的输出——参阅 `allocation_templates.py` 的模块 Docstring 获取完整设计说明，此处总结为数据契约：

| | |
|---|---|
| **索引 (Index)** | `pd.DatetimeIndex` —— 与标的池自身的 OHLCV 数据相同的日历（或其内连接子集，例如 `common.allocation_templates.build_aggregate_curve` 的输出日历） |
| **列 (Columns)** | 标的池中的每个标的一列 |
| **数值 (Values)** | 目标组合权重（0.0–1.0）；一行数值的和应 `<= 1.0`（未分配的权重为闲置现金，收益率为 0% —— 参阅 `allocation_backtester.py`） |
| **稀疏性 (Sparsity，核心约束而非可选)** | 某个日期除实际再平衡日外，**每一行均为 `NaN`**，在实际再平衡日则保存新的目标权重——**即使该目标权重在数值上与上一次再平衡完全相同**（例如等权重每个周期重新计算出相同的 1/N）。回测器 (§4) 通过行的**存在性**而非数值是否变化来区分“再平衡至相同权重”与“未发生再平衡”。模板绝不能自行向前填充（forward-fill）其输出；只有回测器在内部进行该操作。 |

对于模板在特定再平衡日如何表示“数据不足尚无法计算目标”存在两种并存且均正确的规范（参阅 `common/allocation_templates.py` 了解各自的具体示例）：
- **基于排序的模板**（动量、均值回归、宽度门控）将该行初始化为全 `0.0`（明确、有意的全现金再平衡指令）。
- **基于协方差的模板**（HRP、最小方差、最大分散化）将该行保留为 `NaN`（根本不是再平衡——回测器将前一权重的持有状态向前漂移）。

该结构的 CSV 导出文件（例如 `pipeline/research_strategy/results/<strategy>_weights.csv`、`pipeline/strategy_generator/results/strategygen_allocation_weights.csv`、`backtester/results/backtest_weights.csv`）在写入前均应用 `.ffill().fillna(0.0)` ——即保存的 CSV 是**稠密 (DENSE)**、向前填充的每日权重序列，而非上述内存中的稀疏契约。

## 4. 组合回测结果字典 (Portfolio backtest result dict)

由 `common.allocation_backtester.run_allocation_backtest(universe, target_weights, ...)` 返回——这是 `backtester`、`research_strategy` 和 `strategy_generator` 共同使用的单个共享回测引擎。

| 键 (Key) | 类型 | 含义 |
|---|---|---|
| `equity_curve` | `pd.DataFrame`（1 列：`equity`），`DatetimeIndex` | 每日组合权益，起始值为 `initial_capital` |
| `actual_weights` | `pd.DataFrame`，`DatetimeIndex`，列 = 标的代码 | 实际持有的**稠密**每日权重（漂移后、再平衡后）——非稀疏 |
| `total_turnover` | `float` | 每次再平衡中绝对权重变化的总和 |
| `total_rebalances` | `int` | 包含实际再平衡指令的日期计数 |
| `total_return`、`cagr`、`max_drawdown`、`sharpe_ratio`、`calmar_ratio`、`win_rate`、`profit_factor` | `float` | 标准性能指标。`max_drawdown` 为**正数幅度**（例如 18% 回撤记为 `0.18`），符合 `common/metrics.py` 自身的规范。此处的 `win_rate`/`profit_factor` 根据**每日收益率序列**计算（`common.metrics.win_rate_from_returns`/`profit_factor_from_returns`）——与接收交易 DataFrame（`side`/`pnl` 列）的 `common.metrics.win_rate`/`profit_factor` **规范不同**；这两对同名函数不可互换，参阅 `common/metrics.py` 的 Docstring |

在空输入或退化输入上，仅返回 `{"equity_curve": pd.DataFrame(), "turnover": 0.0}`（注意：在此特定的空输入短路中键名为 `"turnover"`，而非 `"total_turnover"` ——调用方在依赖其他键之前应检查 `result["equity_curve"].empty`）。

## 5. 因子分类标签词汇表 (Factor taxonomy tag vocabulary)

`common/factor_taxonomy.py` 中的 `FACTOR_CATEGORIES: Dict[str, str]` —— `research_strategy`（`strategies_config.json` 的 `"factors"` 键）与 `common.allocation_templates`（`AllocationTemplate.factor_tags`，`List[str]` 字段）共同使用的单个共享词汇表，用于标记策略/模板所依赖的量化因子类别。有效标签：`absolute_momentum_trend`、`relative_momentum`、`volatility_targeting`、`mean_reversion`、`breadth`、`correlation_diversification`、`regime_trend_strength`、`static_fixed_weight`。参阅 `pipeline/research_strategy/README_ZH.md` 的“因子标签”章节与 `pipeline/strategy_generator/README_ZH.md` 的“消费 research_strategy 因子报告”章节了解该词汇表在实际中的使用方式（具体机制而非 Schema 定义驻留在那里）。

## 6. 指标特征菜单 (`name`, `lookback`) 对

`common/indicator_features.py` 中的 `DEFAULT_FEATURE_MENU: List[Tuple[str, int | Tuple[int,int,int]]]` —— 例如 `("rsi", 14)`、`("macd_hist", (12, 26, 9))`。`feature_label(name, lookback) -> str` 将一对转换为列安全的标签（例如 `"rsi_14"`、`"macd_hist_12_26_9"`）；`compute_feature(curve, name, lookback) -> pd.Series` 计算该特征。由 `pipeline/pattern_mining/pmine/pattern_mining.py` 的特征表使用（参阅该项目的 README）以及被 `common.allocation_templates.PatternBasedAllocationTemplate` 使用——有意由**同一个**调度函数支持两者，以便挖掘出的阈值在测试和后续交易中基于完全相同的计算进行。

## 7. 网格搜索 + 等效随机搜索 (ERS) 验证 (`allocation_search.py`)

共享的模板无关搜索/验证原语，从 `pipeline/strategy_generator/stratgen/generator.py` 抽取，以便 `backtester` 的 `--optimize` 标志可以使用完全相同的验证机制来调优**单个**已选模板的参数，而非维护第二个独立的代码副本。由 `strategy_generator` 的多模板搜索（每个候选模板调用一次 `grid_search_template`，对总体胜者调用一次 `run_ers_validation`）和 `backtester --optimize` 的单模板调优（`optimize_template`，将两者进行组合）共同使用。

每个函数接收一个 `ScoreFn = Callable[[object, dict], dict]` 回调函数 —— `score_fn(template, params) -> dict` —— 而非硬编码如何回测候选策略；返回的字典必须至少包含一个 `"sharpe_ratio"` 键（参阅 §4 的结果字典）。`strategy_generator` 提供基于单次 `run_allocation_backtest` 的打分器；`backtester` 提供基于 `run_standard`/`run_walkforward` 的打分器。`score_fn` 抛出的任何异常都会在内部被捕获，并降级为 `-inf` 夏普比率的降级试验，同时发出包含异常名称的 `RuntimeWarning`，因此坏候选策略绝不会导致整个搜索崩溃；如果**每个**试验（或每个 ERS 随机抽样）都以此方式失败，还会触发第二个更显眼的聚合 `RuntimeWarning`，因为这种模式通常意味着 `score_fn` 本身损坏，而非每个候选策略都真正不好。

公共成员：

| 成员 | 目的 |
|---|---|
| `grid_combinations(param_grid: dict) -> list` | 将 `{param_name: [values]}` 字典笛卡尔积展开为参数字典列表；空/假值的 `param_grid` 返回 `[{}]`（一次退化试验），绝不返回 `[]` |
| `random_weights(universe, rebalance_freq_days, rng) -> pd.DataFrame` | 为 ERS 零假设对比生成一个随机有效稀疏权重矩阵（符合 §3 的结构） |
| `RandomAllocationTemplate(rng)` | 哑 `AllocationTemplate` 子类（参阅 §3），纯粹用于 ERS 检查 —— `name="random_allocation"`，空的 `param_grid`/`factor_tags`，`generate_weights` 由 `random_weights` 支持 |
| `grid_search_template(template, score_fn) -> list` | 评估 `template.param_grid` 中每个组合上的 `score_fn`；返回 `[{"params", "result", "score"}, ...]`，每次试验一项 |
| `run_ers_validation(params, best_score, best_result, score_fn, *, n_random_search=200, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=None) -> dict` | 抽取 `n_random_search` 个随机权重组合并打分，返回 `{"ers_percentile", "ers_passed", "trusted"}` ——当所有随机试验均为非有限值时，`ers_percentile` 安全保底为 `0.0`（绝不为平凡的 `1.0`） |
| `optimize_template(universe, template, score_fn, **ers_kwargs) -> dict` | 便捷包装器：依次执行 `grid_search_template` 并在胜者上执行 `run_ers_validation`，用于单模板场景 |

## 8. 共享图表生成 (`plotting.py`)

`plot_equity_curve(equity: pd.Series, results_dir: str, *, baseline: Optional[pd.Series] = None, strategy_label="Strategy", baseline_label="Baseline", title="Equity Curve", filename="equity_curve.png") -> str` —— 单线权益图（若提供了 `baseline`，则为双线图，例如策略旁边绘制买入持有基准）。`equity`/`baseline` 是普通的 `pd.Series`（日期索引 -> 组合价值）—— 传入 `result["equity_curve"]["equity"]` (§4)，而非原始 `run_allocation_backtest()` 字典。保存于 `results_dir` 下（若缺失则创建）并返回保存的绝对路径。由 `backtester/run_backtest.py` 与 `pipeline/strategy_generator/run_strategygen.py` 使用，用以生成伴随每次运行报告的权益曲线图表，使图表样式/行为在两者之间保持一致，而非按项目重复实现。

## 9. 共享 OHLCV 缓存目录

每个项目的 `run_*.py` 均通过 `common.cli_utils.shared_data_dir()` 解析其 `--cache-dir`/`DATA_DIR` 默认值，无论哪个项目调用它，它总是解析为单个 `<repo_root>/data/` 目录 —— 一个项目获取的标的/周期/日期范围数据会被其他所有项目复用，无需按项目分别下载/缓存。

`common.data.CachedDataProvider` 将每个缓存文件命名为 `{ProviderClassName}_{symbol}_{interval}_{start}_{end}.csv`（例如 `YFinanceDataProvider_SPY_1d_2015-01-01_2024-12-31.csv`）。提供商类前缀至关重要，而非装饰性的：`research_strategy` 默认使用 `--data-provider synthetic`，而其他 3 个项目默认使用 `--data-provider yfinance`，因此若没有前缀，两个从不同提供商针对该共享目录请求“相同”标的/周期/日期范围的项目会静默读回彼此（错误提供商）的缓存数据。

缓存条目默认永不过期（`cache_max_age_days=None`，`CachedDataProvider` 的构造函数默认值）——每个 `run_*.py` 的 `--cache-ttl-days` 标志（通过 `add_data_provider_cli_args` 添加，因此其名称/行为在所有 4 个项目及 `run_pipeline.py` 的透传中完全一致）可选择重新获取早于 N 天的缓存文件。

---

## 交叉参考索引

| 项目 | 使用本文件中的内容 | 在本地记录的内容（参阅该项目自身的 README） |
|---|---|---|
| `backtester` | §1–4, 7–8 | `strategy.json` 消费（Schema 由 `strategy_generator` 所有）、`backtest_equity.csv`/`backtest_weights.csv`/`walkforward_report.csv`（参阅 `backtester/SCHEMAS_ZH.md`） |
| `instrument_selection` | §1–2 (输入 Universe/OHLCV) | `screening_report.csv`、`correlation_matrix.csv`、`screened_out.csv`、`basket.json` |
| `research_strategy` | §1–5 | `research_strategy_report.json`、`factor_summary.json`、`strategies_config.json` 条目 Schema |
| `pattern_mining` | §1–2, 6 | `pattern_report.json`（Schema 在此归属）、拐点/特征表/发现成果 DataFrames |
| `strategy_generator` | §1–8 | `strategy.json`（Schema 在此归属）、`GeneratedStrategySpec` |
