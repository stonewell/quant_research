[ [English](README.md) | 简体中文 ]

# `backtester`

独立的命令行（CLI）工具，评估已生成且固定的资产配置策略（由 `strategy_generator` 导出的 `strategy.json`）在资产组合上的表现——支持在全时间段上评估一次（`--mode standard`），或在滚动时间窗口上检查其一致性且无需重新优化（`--mode walkforward`）。该项目自身故意**不进行**任何策略搜索；它仅重新运行 `strategy_generator` 和 `research_strategy` 也在使用的共享 `common.allocation_backtester.run_allocation_backtest` 引擎。

数据结构与 Schema（`strategy.json` 输入、3 个 CSV 输出以及该项目消费的共享 OHLCV / 标的池 / 目标权重 / 结果字典结构）均在 `SCHEMAS_ZH.md` 中记录，此处不再重复。

## 环境安装与配置

本项目与工作区的其余项目共享单个 `uv` 管理的环境。从仓库根目录（上一级）：

```bash
uv sync
```

## 使用指南

### 参数参考

标的池解析标志（`--universe`/`--universe-file`/`--universe-provider`/`--universe-kwargs`）与其他 3 个项目共享——参阅 `common/README_ZH.md` 的交叉参考索引。与任何其他项目的 CLI 不同，本项目的 CLI **不会向 `resolve_universe_from_args` 传递默认标的池**——必须指定 3 个标的池标志之一；若全部省略将抛出 `ValueError("No universe symbols provided or resolved...")`。

| 标志 (Flag) | 类型 / 默认值 | 含义 |
|---|---|---|
| `--strategy-file` | 路径，**必填** | 由 `strategy_generator` 导出的 `strategy.json` 路径（Schema 在 `SCHEMAS_ZH.md` 中） |
| `--universe` / `-u` | 空格分隔的代码，默认值：无 | 用于回测策略的显式标的代码列表（**无保底默认值**——参阅上方） |
| `--universe-file` | 路径，默认值：无 | 从文件中加载标的代码 |
| `--universe-provider` | 字符串，默认值：无 | 从已注册的提供商解析标的池，而非静态列表 |
| `--universe-kwargs` | JSON 字符串，默认值：无 | 传递给 `--universe-provider` 的额外参数（JSON 对象字符串） |
| `--start` | `YYYY-MM-DD`，默认值 `"2015-01-01"` | 历史起始日期 |
| `--end` | `YYYY-MM-DD`，默认值 `"2024-12-31"` | 历史结束日期 |
| `--interval` | 字符串，默认值 `"1d"` | 传递给数据提供商的 K 线周期 |
| `--mode` | `standard` \| `walkforward`，默认值 `"standard"` | `standard` 在全日期范围内评估一次；`walkforward` 在滚动折叠窗口上重新评估**相同**的固定参数，无需重新优化 |
| `--window-years` | 浮点数，默认值 `1.0` | Walkforward 窗口长度（年）（仅用于 `--mode walkforward`） |
| `--step-years` | 浮点数，默认值 `0.5` | Walkforward 窗口步长（年）（仅用于 `--mode walkforward`） |
| `--initial-capital` | 浮点数，默认值 `100000.0` | 初始组合资金 |
| `--commission-pct` | 浮点数，默认值 `0.0005` | 每笔交易的佣金占交易名义价值的比例 |
| `--slippage-pct` | 浮点数，默认值 `0.0005` | 每笔交易的滑点占交易名义价值的比例 |
| `--baseline-symbol` | 字符串，默认值：无 | 可选的单一参考标的（如 `SPY`），用于与策略对比。默认关闭——除非设置此项，否则不会运行对比代码 |
| `--baseline-template` | 字符串，默认值 `"equal_weight"` | 静态配置模板（`ALLOCATION_TEMPLATES` 中的 9 个之一——无 `pattern_*` 模板），用于将 `--baseline-symbol` 转换为基准权益曲线 |
| `--baseline-params` | JSON 字符串，默认值：无 | `--baseline-template` 的参数（JSON 对象字符串，默认值：该模板的第一个 `param_grid` 组合） |
| `--optimize` | 标志，默认关闭 | 在当前标的池上网格搜索已加载策略的 `template.param_grid`（通过您选择的相同 `--mode` 进行打分），并在运行最终回测之前使用等效随机搜索（ERS）验证胜者（通过共享的 `common/allocation_search.py`）。若胜者未通过 ERS 验证，则回退到策略文件的**原始**参数——绝不会静默不产生输出。始终写入 `results/optimize_report.json`（参阅 `SCHEMAS_ZH.md`） |
| `--n-random-search` | 整数，默认值 `200` | 用于验证 `--optimize` 胜利组合的等效随机搜索池大小 |
| `--ers-percentile-threshold` | 浮点数，默认值 `0.90` | 胜利组合必须超越随机组合池多高的百分位才能被信任 |
| `--min-rebalances-for-trust` | 整数，默认值 `4` | 胜利组合在被信任之前必须达到的最小 `total_rebalances` 次数，即使其通过了 ERS 百分位 |
| `--data-provider` | 字符串，默认值 `"yfinance"` | `yfinance`、`csv`、`synthetic` 或自定义模块规范 |
| `--data-dir` | 路径，默认值：无 | `csv` 数据提供商的文件夹路径 |
| `--no-cache` | 标志，默认关闭（已缓存） | 禁用本地 CSV 缓存已获取的数据 |
| `--results-dir` | 路径，默认值：无 | 覆盖 `backtest_equity.csv`/`backtest_weights.csv`/`walkforward_report.csv` 的写入位置（默认导出到 `backtester/results/`） |
| `--cache-dir` | 路径，默认值：无 | 覆盖 OHLCV CSV 缓存目录（默认使用共享的工作区目录 `<repo_root>/data/` ——参阅 `common/README_ZH.md` 的“共享 OHLCV 缓存目录”章节） |
| `--cache-ttl-days` | 浮点数天数，默认值：无 | 重新获取早于 N 天的缓存 OHLCV 文件，而非永久信任 |
| `--no-plots` | 标志，默认关闭（生成图表） | 跳过 normally 在 `--mode standard` 下生成的 `equity_curve.png` 图表 |

### 示例命令（真实市场数据）

```bash
# 标准模式：对新资产组合上的已生成策略进行全历史评估（从仓库根目录运行）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL --mode standard

# 显式指定日期范围和 K 线周期
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA --start 2018-01-01 --end 2024-12-31 --interval 1d

# 从文件加载标的池（如 instrument_selection 生成的组合）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe-file pipeline/instrument_selection/results/basket.json --mode standard

# Walkforward 模式：使用默认的 1 年窗口 / 0.5 年步长进行滚动窗口一致性检查
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL GLD TLT --mode walkforward

# 自定义窗口/步长大小的 Walkforward 模式
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL GLD TLT --mode walkforward --window-years 2 --step-years 1

# 自定义交易成本假设和初始资金
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL --initial-capital 250000 --commission-pct 0.001 --slippage-pct 0.001

# 在新组合上重新运行基于挖掘模式的策略（带有 pattern_spec 块的 strategy.json）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT IEF --mode standard

# 在新组合上重新运行源自 research_strategy 的策略（带有 research_strategy_spec 块的 strategy.json，即在 strategy_generator 搜索中获胜的 research_strategy 策略）；在 --mode standard 与 --mode walkforward 下运行机制完全相同
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY TLT BIL GLD --mode standard
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY TLT BIL GLD --mode walkforward

# 在新组合上重新运行切面组合的混合策略（带有 composite_spec 块的 strategy.json，即某模板的选择/入场切面与另一不同模板的权重/离场切面的获胜组合；参阅 strategy_generator 的 --no-compose-aspects）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ IWM EFA EEM GLD TLT --mode standard

# 重新运行源自 fundamental_screener 的策略（带有 fundamental_spec 块的 strategy.json）--参阅 pipeline/fundamental_screener/README_ZH.md 了解 bnn_strategy.json 是如何生成的
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/fundamental_screener/results/fundamental_strategy.json \
  --universe KO PG SPY BIL --mode standard

# 重新运行源自 bnn_forecaster 的策略（带有 bnn_spec 块的 strategy.json）--必须使用 bnn_forecaster 独立的 venv，而非 pipeline 的（参阅 ml/bnn_forecaster/README_ZH.md）
ml/bnn_forecaster/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file ml/bnn_forecaster/results/bnn_strategy.json \
  --universe KO PG SPY BIL --mode standard

# 自定义结果/缓存目录，不本地缓存获取的数据
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ TLT GLD --no-cache \
  --results-dir /tmp/backtest_results --cache-dir /tmp/backtest_cache

# CSV 文件夹提供商（您已下载的离线真实数据）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ TLT GLD --data-provider csv --data-dir /path/to/ohlcv_csvs

# 仅离线/合成数据（无网络调用）--本工作区常设的测试规范
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic

# 带基准标的对比的标准模式（合成数据 --无网络调用）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic \
  --baseline-symbol SPY --baseline-template equal_weight

# 带基准对比和自定义基准参数的 Walkforward 模式（合成数据）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode walkforward \
  --baseline-symbol SPY --baseline-params '{"rebalance_freq_days": 21}'

# --optimize：在当前标的池/模式上重新调优已加载策略的参数，并在运行最终回测前对胜者进行 ERS 验证（合成数据）
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode standard \
  --optimize --n-random-search 200 --ers-percentile-threshold 0.90

# --mode walkforward 下的 --optimize：每个候选策略通过其在 walkforward 报告的相同滚动窗口上的平均折叠夏普比率进行打分
uv run python backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode walkforward \
  --optimize --n-random-search 100 --ers-percentile-threshold 0.90 --min-rebalances-for-trust 4
```

输出落地在 `results/`（或指定的 `--results-dir`）中：`backtest_equity.csv` 和 `backtest_weights.csv`（`--mode standard`），或 `walkforward_report.csv`（`--mode walkforward`）——参阅 `SCHEMAS_ZH.md` 了解列级细节。`--mode walkforward` 始终会写入 `walkforward_summary.json`（平均折叠指标加平减夏普比率 Deflated Sharpe Ratio）。当设置了 `--baseline-symbol` 时：在两种模式下均会写入 `baseline_equity.csv` 和 `comparison_report.json`，且 `walkforward_report.csv` 增加 5 个额外 `baseline_*`/`outperformance` 列。在 `--mode standard` 中，除非指定了 `--no-plots`，否则还会写入 `equity_curve.png`（如果设置了 `--baseline-symbol`，则为策略 vs 基准的双线图）。当设置了 `--optimize` 时，始终会写入 `results/optimize_report.json`（无论成功或失败）——参阅 `SCHEMAS_ZH.md`——且在被信任的获胜情况下，最终回测（以及上述每个其他输出）均反映调优后的 `best_params`，而非策略文件的原始参数。
