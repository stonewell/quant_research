[ [English](README.md) | 简体中文 ]

# 自动化组合策略生成器 (`strategy_generator`)

一个为整篮子标的资产同时生成具体、参数化的资产配置交易策略的工具。

---

## 1. 当前功能与搜索空间

在 9 个静态组合配置模板中进行搜索——全部定义在共享的 `common/allocation_templates.py` 中：

1. `EqualWeightAllocation`（1/N 算法）
2. `InverseVolatilityAllocation`（逆波动率风险平价）
3. `CrossSectionalMomentumAllocation`（截面动量）
4. `HierarchicalRiskParityAllocation`（层次风险平价 HRP）
5. `DualMomentumAllocation`（双重动量）
6. `MaxDiversificationAllocation`（最大分散化配置）
7. `MeanReversionAllocation`（截面 RSI 均值回归）
8. `MinimumVarianceAllocation`（最小方差配置，二次规划求解）
9. `BreadthGatedMomentumAllocation`（基于市场宽度的崩溃保护动量配置）

搜索过程网格搜索每个模板的参数集，通过共享回测器（`common/allocation_backtester.py`）打分，并挑选夏普比率最高的组合。此外，默认支持不同模板切面（选择 vs 加权，入场 vs 离场）的**切面重组 (Aspect Composition)** 混合搜索（可通过 `--no-compose-aspects` 禁用）。

胜者将通过**等效随机搜索 (ERS)** 验证，确认其表现战胜同规模的随机配置组合池。

---

## 2. 扩展候选输入

- **消费因子研究报告 (`--factor-report`)**：加载 `research_strategy` 导出的 `factor_summary.json`。在最高夏普比率候选策略出现微小平局（夏普比率差异在 `--factor-tiebreak-epsilon` 内）时作为平局决胜依据。
- **消费模式挖掘报告 (`--pattern-report`)**：加载 `pattern_mining` 导出的 `pattern_report.json`，将其显著性特征转换为候选策略参与竞争。
- **引入研究策略 (`--research-strategy`)**：引入 `research_strategy` 的 20 种具体策略实现作为额外候选模板。

---

## 3. 核心机制：网格搜索 + 等效随机搜索 (ERS)

- **受限参数网格**：每个模板仅暴露 2-4 个自由参数，避免过拟合与数据误掘（Data-Snooping）。
- **等效随机搜索 (ERS)**：生成 200 个（默认）随机权重资产组合进行回测，胜者必须超越设定百分位（如 90%）且达到最小再平衡次数，才会被标记为 `trusted: true`。

---

## 4. 使用指南

```bash
# 从 pipeline/ 目录运行
# 为标的池生成策略
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate

# 配合因子报告与模式挖掘报告运行
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL \
  --factor-report research_strategy/results/factor_summary.json \
  --pattern-report pattern_mining/results/pattern_report.json
```

---

## 5. 输出文件：`results/strategy.json`

输出的策略规范文件（由 `backtester` 消费）：

- `template_name`：模板名称（或切面组合混合名称）。
- `params`：获胜的超参数组合。
- `sharpe_ratio`, `cagr`, `max_drawdown`, `calmar_ratio` 等表现指标。
- `trusted` 与 `ers_passed`：ERS 验证状态。
- `pattern_spec`, `research_strategy_spec`, `composite_spec`：重构复杂策略所需的扩展规范块（互斥）。

数据结构参阅 `../../common/README_ZH.md` (§1–6)。
