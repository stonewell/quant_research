[ [English](README.md) | 简体中文 ]

# 量化策略标的筛选工具 (`instrument_selection`)

一个计算量化可测试标准的筛选工具，用于评估股票或 ETF 是否为系统化策略的良好、可交易且具备分散化价值的候选标的——**故意与具体策略解耦（策略无关）**。这是一个筛选和研究工具，而非回测器：它不模拟交易，而是对标的特征进行刻画。

---

## 先筛选，后打分：软性打分之前的硬性门控

流动性和历史数据长度曾是综合评分中的软性排名输入，这可能导致极度缺乏流动性的标的因在其他维度（可预测性、分散化等）表现突出而被误选。经深度研究后，本项目引入了硬性门控：

- **指数提供商方法论**（如 MSCI 指数家族）：在进行任何因子倾斜、加权或优化之前，通过一组二元通过/失败（Pass/Fail）的可投资性筛选条件（最小规模、最小流动性、最小上市时长）明确界定合格标的池。
- **硬性门控设计**：`run_screener.py` 在计算原始指标后、构建相关性矩阵或打分之前，立即运行 `screening.screen_universe()`——未通过门控的标的会被彻底排除在相关性矩阵、`overall_selection_score` 及后续篮子选择方法之外。

实现的两大硬门控：
1. `min_avg_dollar_volume`：日均成交额下限（默认 5,000,000 美元）。
2. `min_history_years`：最少交易历史年限下限（默认 1.0 年）。

---

## 五大核心评估维度

1. **流动性 (Liquidity)**：评估实际回测价格的可执行性。包含日均成交额（`Close × Volume`）以及 **Corwin & Schultz (2012)** 基于高低价差估计买卖价差的高低价差估计器。
2. **波动率与下行风险 (Volatility and Downside Risk)**：包括实现波动率、下行实现波动率（Estrada 2000; Ang et al. 2006）、下行波动率比率、ATR% 和 ADX（趋势强度）。
3. **可预测性 (Predictability)**：
   - **Hurst 指数 (H)**：基于 R/S 分析评估序列的长记忆性（H < 0.5 均值回归，H = 0.5 随机游走，H > 0.5 趋势）。配合打乱置换显著性检验（`hurst_significance()`），排除伪序列影响。
   - **蜡烛图形态可预测性**：评估单/双/三 K 线反转形态在统计上是否具备高于基准漂移的预测能力，并通过安慰剂零假设进行检验。
   - **动量可预测性**：评估过去收益与未来收益之间的序列相关性，并通过 Bootstrap/打乱置换进行显著性检验。
4. **相关性与分散化 (Correlation and Diversification)**：计算两两相关性矩阵、Beta，使用层次聚类（距离度量 $d = \sqrt{2(1-\rho)}$）识别冗余标的，并评估应力状态下的相关性飙升。
5. **历史数据长度与基金质量 (History Length and Fund Quality)**：评估数据样本的可靠性（`history_years`）以及 ETF 费率和 AUM 规模（如果可用）。

---

## 综合评分 (Composite Score)

`overall_selection_score` 是上述维度的加权平均：

| 组件 | 权重 |
|---|---|
| `liquidity_score` | 0.30 |
| `vol_adequacy_score` | 0.20 |
| `momentum_score` | 0.10 |
| `predictability_score` | 0.07 |
| `candlestick_score` | 0.03 |
| `diversification_score` | 0.15 |
| `history_adequacy_score` | 0.10 |
| `etf_expense_score` | 0.025 (尽力而为) |
| `etf_aum_score` | 0.025 (尽力而为) |

---

## 从打分到最终资产组合选择 (`selection.py`)

1. **`select_cluster_representatives`**：层次聚类后，从每个聚类中挑选一个代表标的（默认挑选最低波动率标的）。
2. **`select_diversified_greedy`**：Max-Sum 分散化算法，在限定篮子大小 K 的情况下最大化个体得分与两两间分散度（距离）之和。
3. **`select_diversified_threshold_greedy`**：阈值门控贪心算法，按得分降序遍历，仅保留与已选标的相关性低于 `--max-cluster-correlation` 的标的，自动决定组合大小。
4. **`select_max_diversification_ratio`**：最大化 Choueifaty & Coignard (2008) 的分散化比率 ($DR$)。

---

## 项目结构

```
instrument_selection/
  selectorbot/
    config.py        SelectionConfig 配置
    data.py          数据加载包装器
    liquidity.py      日均成交额与 Corwin-Schultz 估计器
    volatility.py     波动率、ATR、ADX 计算
    persistence.py    Hurst 指数与打乱置换显著性检验
    candlestick.py    K 线形态检测与显著性检验
    momentum.py       动量序列相关性与显著性检验
    correlation.py    相关性矩阵、Beta、层次聚类
    screening.py       硬性门控筛选（流动性、上市年限）
    scoring.py        综合评分计算
    selection.py       篮子选择算法（聚类代表、贪心分散化等）
    plotting.py       热力图、谱系图、散点图生成
  run_screener.py      CLI 入口
  tests/               单元测试目录
  results/             结果输出目录
```

---

## 使用指南

### CLI 运行示例

```bash
# 从 pipeline/ 目录运行
# 默认标的池运行
uv run python instrument_selection/run_screener.py

# 指定标的池与基准
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT --benchmark SPY

# 使用阈值门控贪心算法选择组合
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM DIA EFA EEM GLD SLV USO TLT XLE XLF XLK XLV XLU \
  --select-method threshold --select-max-k 8
```

---

## 数据结构与 Schema

参阅 `../../common/README_ZH.md` (§1–2) 了解共享的 OHLCV DataFrame 和标的池字典。

### 主要输出文件
- `results/screening_report.csv`：通过硬筛选标的的指标与得分报告。
- `results/correlation_matrix.csv`：两两相关性矩阵。
- `results/screened_out.csv`：未通过硬筛选的标的及原因。
- `results/basket.json`：选定的资产组合文件（供 `strategy_generator` 和 `backtester` 消费）。
