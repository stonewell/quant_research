[ [English](TODO_autobnn_future_uses.md) | 简体中文 ]

# TODO：本工作区中 AutoBNN 的其他潜在用途

**状态：搁置待未来重新评估。未实现任何内容，本文件未被任何 README 链接。** 实际交付的唯一 AutoBNN 集成是独立的 `ml/bnn_forecaster/` 项目（参阅其 README）。本文件记录了 AutoBNN 能力（贝叶斯神经网络生成校准后的中位数 + 置信区间预测）在本工作区其他地方的潜在应用点分析。

---

## 1. `instrument_selection` — 崭新的“可预测性”信号

现有 `selectorbot/persistence.py` (Hurst 指数) 与 `selectorbot/momentum.py` (动量有效性) 仅评估序列是否与随机游走存在统计差异，不生成前向预测。AutoBNN 的区间校准度可作为一种新的“BNN 可发现结构”评估信号。

## 2. 现有策略的仓位风控叠加 (`strategy_generator` / `research_strategy`)

将 AutoBNN 的置信区间宽度 (`ci_width`) 作为风险叠加层来缩放现有模板的仓位大小：置信区间越窄 -> 权重越大，置信区间越宽 -> 权重越小。

## 3. `pattern_mining` 交叉验证

AutoBNN 的变点检测算法（`ChangePoint`）与 `pattern_mining` 的拐点显著性检验属于完全独立的机制，两者结合可进行结构变点与拐点模式的交叉验证。

## 4. 因子分类学扩充

若未来引入上述功能，需要在 `common/factor_taxonomy.py` 中新增对应的因子类别标签（如 `"probabilistic_forecast"`）。
