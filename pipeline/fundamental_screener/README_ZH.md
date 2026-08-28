[ [English](README.md) | 简体中文 ]

# 真实基本面筛选器 (`fundamental_screener`)

改编自保守价值投资社区估值框架的真实基本面买入/卖出筛选器：仅持有具备持久护城河、高 ROE、派息复利能力的优质公司，且其预期收益率需高于广基指数基准的风险溢价，一旦该优势消退即触发卖出。

这是 `research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy`（纯价格代理版本）的**真实基本面姐妹项目**。本项目从 yfinance 获取真实的 ROE、股息率、盈利增长率和资产负债率——因此，与本工作区中的其他项目不同，**它始终需要访问真实网络**（`--data-provider` 标志仅控制基准标的自身的 OHLCV 历史，用于卖出触发比较器）。

**未接入 `run_pipeline.py`** ——其实时网络依赖与流水线其他阶段默认离线的约定不同。

---

## 功能与逻辑

给定标的池，排序：
- **前 N 个买入候选**：通过质量门控（ROE/股息/杠杆/盈利增长阈值）且预期收益率（`盈利增长率 + 股息率`）高于要求门槛（默认 12%）。
- **前 N 个卖出候选**：未通过质量门控，或其预期收益率已衰减至低于基准的年化收益率。

重叠处理：同一标的绝不会同时出现在买入和卖出列表中。卖出规则始终优先于买入规则（资本保全优先于收益信号）。

---

## 使用指南

```bash
# 从 pipeline/ 目录运行
# 筛选默认的蓝筹股组合 (KO, PG, JNJ, MSFT, COST, WMT, MCD, PEP)
uv run python fundamental_screener/run_fundamental_screener.py --data-provider synthetic

# 自定义标的池与真实基准历史
uv run python fundamental_screener/run_fundamental_screener.py \
  --universe KO PG JNJ MSFT COST WMT MCD PEP \
  --benchmark SPY --data-provider yfinance
```

---

## 输出产物

- `results/fundamental_screen_report.json`：包含买入/卖出候选清单及基本面指标的 JSON 报告。
- `results/fundamental_strategy.json`：兼容 `backtester` 的策略文件，可通过 `backtester/run_backtest.py --strategy-file pipeline/fundamental_screener/results/fundamental_strategy.json` 运行回测。
