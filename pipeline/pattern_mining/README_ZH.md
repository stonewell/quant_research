[ [English](README.md) | 简体中文 ]

# 拐点指标模式挖掘 (`pattern_mining`)

一个专门的流水线阶段，用于从标的池的聚合组合价格历史中挖掘在重大拐点前具备统计显著性的技术指标模式，并导出持久化、独立的 `pattern_report.json` 供 `strategy_generator` 的 `--pattern-report` 标志消费。该模块从 `strategy_generator` 原有的进程内挖掘逻辑中提取出来，确保挖掘结果可跨多次策略生成运行/参数扫面复用。

---

## 1. 核心功能

给定标的池（例如由 `instrument_selection` 导出）：

1. 构建等权重聚合组合曲线（`common.allocation_templates.build_aggregate_curve`）。
2. 通过百分比 Zigzag 过滤器检测组合曲线的重大顶点和底点（`pmine/turning_points.py`）。
3. 测试技术指标菜单（RSI、SMA 相对位置、ROC、ATR%、ADX、布林带 %B、随机指标 %K、MACD 柱状图、CCI、威廉指标 %R）在拐点前 `--pattern-lag-bars`（默认 20 个交易日）的读数是否与随机日期的读数存在显著差异，采用经过 Bonferroni 矫正的打乱置换零假设显著性检验。
4. 将每个测试的 (特征, 事件类型) 组合结果（无论显著与否）写入 `results/pattern_report.json`。

`strategy_generator` 加载该文件后，将显著的发现转换为候选模板（`PatternBasedAllocationTemplate`），并将其与静态模板一同送入网格搜索与等效随机搜索（ERS）流水线中。

---

## 2. 滞后阶数 (Lag) 的重要性

在 Zigzag 确认的拐点**当交易日**（lag=0）读取指标几乎是循环论证/套套逻辑（Tautology）。在拐点前 `--pattern-lag-bars` 天读取指标提出了一个可操作的问题：“在该反转发生之前，该指标是否已经呈现出异常特征，且该特征在实时交易中是可以观察并据以采取行动的”。

---

## 3. 使用指南

```bash
# 从 pipeline/ 目录运行
uv run python pattern_mining/run_pattern_mining.py \
  --universe-file instrument_selection/results/basket.json --data-provider synthetic
```

### 主要 CLI 参数

| 标志 | 类型 / 默认值 | 含义 |
|---|---|---|
| `--universe` / `-u` | 字符串列表 | 显式标的代码列表 |
| `--universe-file` | 路径 | 从文件加载标的池 |
| `--pattern-min-swing-pct` | 浮点数，默认 `0.05` | 确认拐点的最小 Zigzag 波幅 (0.05 = 5%) |
| `--pattern-lag-bars` | 整数，默认 `20` | 在拐点前多少个交易日读取指标 |
| `--data-provider` | 字符串，默认 `"yfinance"` | 数据提供商 (`synthetic`, `yfinance`, `csv`) |

---

## 4. 输出文件：`results/pattern_report.json`

包含 `run_context`、`status`（`"ok"`, `"insufficient_data"`, `"insufficient_turning_points"`）以及 `findings` 列表。

每个 `findings` 条目包含：
- `feature`：指标名称（如 `"rsi"`, `"adx"`）
- `event_type`：`"peak"` 或 `"trough"`
- `observed_stat`：拐点前的观察均值
- `null_mean`：随机日期的零假设均值
- `p_value` 与 `adjusted_alpha`（Bonferroni 矫正）
- `significant`：是否显著 (`bool`)
- `threshold`：观察到的中位数，将作为挖掘出的阈值

---

## 5. 项目结构

```
pattern_mining/
  pmine/
    __init__.py
    turning_points.py    百分比 Zigzag 顶底检测器
    pattern_mining.py    拐点检测 -> 特征计算 -> 显著性检验 -> 模版生成
  run_pattern_mining.py  CLI 入口点，导出 results/pattern_report.json
  tests/                 单元测试目录
  results/               输出目录
```

---

## 6. 测试

```bash
uv run pytest pattern_mining/tests -v
```
基于合成数据对 Zigzag 拐点检测器、显著性检验（正向控制与负向控制）及 JSON 序列化 round-trip 进行完整测试。
