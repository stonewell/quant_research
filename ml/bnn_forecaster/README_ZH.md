[ [English](README.md) | 简体中文 ]

# 贝叶斯神经网络预测器 (`bnn_forecaster`)

基于 [AutoBNN](https://research.google/blog/autobnn-probabilistic-time-series-forecasting-with-compositional-bayesian-neural-networks/) (Google Research) 的概率价格预测模块——组合式贝叶斯神经网络，生成校准后的中位数预测及置信区间，由可解释组件（趋势、周期、变点）构建而成。

这是本工作区中第三个“战胜基准”的策略项目，与 `research_strategy` 中的复利安全边际策略（纯技术代理）和 `fundamental_screener`（真实 ROE/股息/盈利增长）并列。

**未接入 `run_pipeline.py`**。

---

## 为何该项目拥有独立的 `uv` 环境

AutoBNN 需要 JAX + TensorFlow Probability (TFP)，依赖栈较重且对版本极其敏感（例如 `numpy>=1.24,<2.1`，`jax<0.4.30`）。为避免污染或破坏主工作区的环境，`bnn_forecaster` 拥有**独立的 `uv` 项目**（位于 `ml/bnn_forecaster/`）。

```bash
cd ml/bnn_forecaster
uv sync                          # 一次性安装，创建 ml/bnn_forecaster/.venv
uv run python run_bnn_forecaster.py --data-provider synthetic
```

若要回测生成的 `bnn_strategy.json`，请使用**本项目独立的虚拟环境**运行 `backtester`：

```bash
# 从仓库根目录运行
ml/bnn_forecaster/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file ml/bnn_forecaster/results/bnn_strategy.json --universe KO PG SPY BIL
```

---

## 功能简介

给定标的池，在历史 OHLCV 价格上拟合 BNN，并排序出：
- **前 N 个买入候选**：预测收益率高于门槛且置信区间足够狭窄。
- **前 N 个卖出候选**：预测收益率衰减至低于基准，或置信度恶化。

无网络依赖——拟合完全基于 OHLCV 历史数据。

---

## ⚠️ 校准未经实证验证

本项目实现了端到端的运行机制，但未经金融收益率序列的精确概率校准。在实际使用 `--required-return`/`--max-ci-width` 门控前，请先观察真实运行输出的 `ci_width` 并调整相应阈值。

---

## 使用指南与示例

```bash
# 从 ml/bnn_forecaster 内部运行
uv run python run_bnn_forecaster.py --data-provider synthetic
uv run python run_bnn_forecaster.py --universe KO PG SPY BIL --data-provider yfinance
```

### 输出文件
- `results/bnn_forecast_report.json`：预测报告与买卖信号列表。
- `results/bnn_strategy.json`：供 `backtester` 消费的策略规范文件。
