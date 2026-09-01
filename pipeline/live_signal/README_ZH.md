[ [English](README.md) | 简体中文 ]

# 时点买卖信号 (`live_signal`)

一个专门的 `pipeline/` 项目，回答其他流水线阶段都没有回答的实操问题：给定一个标的池和一个已经
生成的固定策略（`strategy.json`，即 `strategy_generator` 生成、`backtester` 消费的同一个文件），
**今天（或指定日期）应该做什么**？`backtester` 在一段历史日期范围内评估固定策略；本项目仅在
单一时间点评估它，并将结果转化为具体、可执行的买卖信号和再平衡指令。

本项目本身不做任何策略搜索——它只通过与 `backtester` 完全相同的重建函数，重建并重新运行一个
已经生成好的策略。

---

## 1. 功能说明

1. 将每个标的的 OHLCV 历史截断到 `<= --as-of-date`（默认今天）——这是保证输出时点正确性的唯一
   前提：该日期之后的数据永远不会被使用，无论数据提供商实际返回了什么。
2. 通过 `backtester.run_backtest` 自身的 `_get_template`/`_load_strategy_file`（本仓库中唯一统一
   处理全部 6 种 `strategy.json` 变体——普通静态模板、`pattern_spec`、`research_strategy_spec`、
   `composite_spec`、`fundamental_spec`、`bnn_spec`——的位置）重建策略。**耦合警告**：这两个函数是
   CLI 脚本中带下划线前缀的私有函数，并非公开 API；若 `backtester/run_backtest.py` 未来重构而未
   同步更新，此处的导入会静默失效。这里刻意选择复用而非重复实现约 110 行的六路重建逻辑。
3. 在截断后的数据上运行 `template.generate_weights(universe, params)`，提取策略的**当前目标权重**
   ——即在 `--as-of-date` 或之前最近一次真实再平衡行（稀疏权重契约，参阅 `common/README_ZH.md`
   §3）。
4. 将该目标与一个**参照**比较：
   - 若给出 `--current-holdings`/`--current-holdings-file`（您**实际**当前持仓的
     `{symbol: 权重占比}` JSON）——生成从真实持仓到策略目标的精确交易清单。
   - 否则使用策略自身上一次再平衡——无需额外输入即可自洽，但只反映策略推荐本身的变化，而非
     您实际持仓与之出现偏离后需要执行的交易。
5. 将每个标的分类为 `buy`（目标 > 参照）、`sell`（目标 < 参照）或 `hold`（不变，仍持有）——两侧
   都接近 0 的标的视为噪音而被剔除。报告按目标权重排序的前 `--top-n` 买入候选、完整买卖列表，
   以及完整的再平衡指令表。

## 2. 参数参考

标的池解析标志（`--universe`/`--universe-file`/`--universe-provider`/`--universe-kwargs`）与数据
提供商三件套（`--data-provider`/`--data-dir`/`--no-cache`/`--cache-ttl-days`）与其他项目共享——参阅
`common/README_ZH.md` 的交叉参考索引。

| 标志 | 类型 / 默认值 | 含义 |
|---|---|---|
| `--strategy-file` | 路径，**必填** | `strategy_generator` 导出的 `strategy.json` 路径 |
| `--as-of-date` | `YYYY-MM-DD`，默认今天 | 评估策略的时点日期。任意历史日期也可用（便于确定性测试/调试）——该日期之后的数据永不被使用。 |
| `--lookback-days` | 整数，默认 `800` | `--as-of-date` 之前加载的日历天数（约 2.2 年——足以覆盖现有所有模板的 `warmup_bars` 需求）。若看到历史不足的警告可调高此值。 |
| `--current-holdings` | JSON 字符串，默认无 | 您实际当前持仓的 `{symbol: 权重占比}`。省略（同时不传 `--current-holdings-file`）则改用策略自身上一次再平衡作为参照。 |
| `--current-holdings-file` | 路径，默认无 | 与 `--current-holdings` 相同结构，从文件读取。与 `--current-holdings` 互斥。 |
| `--top-n` | 整数，默认 `5` | 按目标权重排序，突出显示的买入候选数量。 |
| `--action-threshold` | 浮点数，默认 `1e-6` | 判定为买/卖（而非持有不变）所需的最小权重变化幅度。 |
| `--interval` | 字符串，默认 `"1d"` | 传递给数据提供商的 K 线周期。 |
| `--results-dir` | 路径，默认无 | 覆盖输出目录（默认 `live_signal/results/`）。 |
| `--cache-dir` | 路径，默认无 | 覆盖共享的工作区级 OHLCV 缓存目录。 |

注意：与 `research_strategy`（`--data-provider` 默认 `synthetic`）不同，本项目默认 `yfinance`——
与 `backtester` 一致，因为本工具的目的正是针对真实价格给出可执行的实时信号。以下每条测试和文档
示例仍会显式传入 `--data-provider synthetic`，符合本工作区的离线测试策略——默认值只影响交互式的
人工运行。

## 3. 示例命令

```bash
# 从 pipeline/ 目录运行（离线/合成数据——本工作区的标准测试惯例）

# 0. 需要先有一个 strategy.json（如果您已经有来自 strategy_generator/research_strategy/
#    fundamental_screener/bnn_forecaster 中任意一个的文件，可跳过此步——均可直接使用）：
uv run python strategy_generator/run_strategygen.py \
  --universe SPY QQQ BIL --data-provider synthetic --mode generate

# 未提供持仓 -- 与策略自身上一次再平衡比较
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ BIL --data-provider synthetic --as-of-date 2024-06-01

# 持仓感知模式 -- 从真实持仓到策略目标的精确交易清单
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ BIL --data-provider synthetic --as-of-date 2024-06-01 \
  --current-holdings '{"BIL": 1.0}'

# 从文件加载标的池（如 instrument_selection 生成的组合）
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe-file instrument_selection/results/basket.json --data-provider synthetic
```

```bash
# 真实市场数据 -- 针对真实价格回答"今天该做什么"
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT --data-provider yfinance \
  --current-holdings-file my_portfolio.json
```

`bnn_spec` 来源的 `strategy.json` 需要 `bnn_forecaster` 自身独立的虚拟环境（与 `backtester` 相同的
注意事项）：`ml/bnn_forecaster/.venv/Scripts/python.exe live_signal/run_live_signal.py ...`。

## 4. 输出

终端：运行上下文（请求的 as-of 日期 vs 实际使用的信号日期、参照来源）、策略的
`explain_weights()` 说明文字、前 N 个买入候选、完整买卖列表，以及每个标的的权重变化量。

`results/live_signal_report.json`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | 字符串 | `"ok"` 或 `"no_signal"`（`--as-of-date` 或之前未发生再平衡——历史/预热不足，非错误） |
| `run_context` | 对象 | `as_of_date`、`signal_date`（实际使用的再平衡日期）、`template_name`、`universe`、`reference_source` |
| `current_target_weights` | 对象 | 策略每个标的的当前目标权重 |
| `reference_weights` | 对象 | 比较基准（持仓或策略上一次再平衡） |
| `buy_signal` / `sell_signal` | 数组 | 再平衡指令表中 `action` 为 `"buy"`/`"sell"` 的行 |
| `top_n_buys` | 数组 | `target_weight` 最大的 `--top-n` 条买入记录 |
| `rebalance_instruction` | 数组 | 每个非噪音标的：`symbol`、`target_weight`、`reference_weight`、`delta`、`is_new_position`、`action` |

`results/live_signal_instruction.csv` —— 与 `rebalance_instruction` 相同的表格，便于电子表格/交易台使用。

## 5. 目录结构

```
pipeline/live_signal/
├── lsig/
│   ├── __init__.py
│   └── signal.py            # 纯逻辑：时点截断、再平衡指令/差值计算、Top-N 买入
├── run_live_signal.py       # CLI：参数、标的池/数据加载、策略重建、编排、输出
├── tests/
│   └── test_signal.py       # 离线单元测试（纯逻辑 + 合成数据 CLI 测试）
└── README_ZH.md
```

## 6. 测试

100% 离线，符合本工作区的标准策略——每个 CLI 级测试都显式传入 `--data-provider synthetic`。

```bash
uv run pytest live_signal/tests -v
```
