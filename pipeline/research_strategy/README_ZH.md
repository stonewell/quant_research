[ [English](README.md) | 简体中文 ]

# 量化交易策略研究 (`research_strategy`)

一个专门的子项目，实现并评估 20 种量化交易策略：5 种从学术文献与从业者研究（*Journal of Finance*、*Journal of Portfolio Management*、SSRN、AllocateSmartly）中综合而成的战术资产配置 (TAA) 策略；4 种单资产择时策略（由本工作区原有的 `rsi_strategy`、`swing_trend_strategy`、`grid_trading` 和 `ensemble_strategy` 整合而来）；2 种 Donchian 通道突破系统；4 种现代热门静态/固定权重组合（永久组合、黄金蝴蝶、全天候、HFEA）；在对”现代、热门、有效”策略的后续深度研究中新增的 2 种现代系统化 TAA 扩展（保护性资产配置 PAA、自适应资产配置 AAA，参阅下文”策略 12-17”了解该研究的发现与已披露的简化）；以及 2 种对缠中说禅价格结构的原创从零实现，后者是前者的增量扩展（参阅”策略 18-20”）。

---

## 1. 概述与策略目录

```
+-----------------------------------------------------------------------------------+
|                        research_strategy Side Project                             |
+-----------------------------------------------------------------------------------+
                                          |
          +-------------------------------+-------------------------------+
          |                               |                               |
          v                               v                               v
+-------------------+           +-------------------+           +-------------------+
|  策略 1:          |           |  策略 2:          |           |  策略 3:          |
|  主动双重动量 GTAA |           |  果断资产配置 BAA  |           |  波动率管理组合    |
|  (Antonacci /     |           |  (Wouter Keller   |           |  (Moreira & Muir  |
|  Faber)           |           |  2022 SSRN)       |           |  2017 J. Finance) |
+-------------------+           +-------------------+           +-------------------+
```

### 策略 1：主动双重动量 GTAA + 风险平价 (Active Dual Momentum GTAA + Risk Parity)
* **学术背景**：Gary Antonacci (2014, *Journal of Portfolio Management*, "Risk-Adjusted Momentum Strategies")；Meb Faber (2007, *Journal of Wealth Management*, "A Quantitative Approach to Tactical Asset Allocation")。
* **数学机制**：
  1. **绝对动量门控**：资产 $i$ 必须满足 $Close_i(t) > SMA_{200, i}(t)$ 且 $ROC_{126, i}(t) > 0$。未通过任一条件的资产将被取消资格，以避免严重回撤。
  2. **多周期相对动量排序**：通过 $Score_i(t) = 0.5 \cdot ROC_{63, i}(t) + 0.5 \cdot ROC_{126, i}(t)$ 对合格资产打分。选择前 $K=3$ 个资产。
  3. **逆波动率风险平价加权**：
     $$w_i = \frac{1/\sigma_{60, i}}{\sum_{j \in \text{Selected}} 1/\sigma_{60, j}} \cdot \left(\frac{M}{K}\right)$$
     其中 $M$ 为通过筛选的资产数量（$\le K$）。
  4. **防御性现金覆盖**：未分配的权重 $(1 - \sum w_i)$ 进入现金替代标的 (`BIL`)。

### 策略 2：Wouter Keller 的果断资产配置 (Bold Asset Allocation, BAA-G12)
* **学术背景**：Wouter J. Keller (2022, SSRN "Relative and Absolute Momentum in Times of Rising/Low Yields: Bold Asset Allocation")。
* **数学机制**：
  1. **金丝雀标的池市场动荡检测器**：`["SPY", "EEM", "EFA", "AGG"]`。
  2. **金丝雀触发条件**：若**任意**金丝雀资产的 12 个月 / 13 周动量为负（$Close < SMA_{200}$ 或 $ROC_{126} < 0$），则市场状态被标记为**动荡 (Turbulent)**；否则标记为**平静 (Calm)**。
  3. **标的池切换**：
     * **平静状态**：交易**进攻型标的池**（`SPY`, `QQQ`, `IWM`, `EFA`, `EEM`, `TLT`, `LQD`, `DBC`）。按 126 日 $ROC$ 等权重配置前 $K=3$ 个资产。
     * **动荡状态**：交易**防御型标的池**（`TIP`, `IEF`, `TLT`, `BIL`, `AGG`, `DBC`）。按 126 日 $ROC$ 等权重配置前 $K=3$ 个正动量的防御资产。任何未分配的插槽转入 `BIL` 现金替代标的。

### 策略 3：Moreira & Muir 波动率管理组合 (VolTiming)
* **学术背景**：Alan Moreira & Tyler Muir (2017, *Journal of Finance* 72(4):1611–1644, "Volatility-Managed Portfolios")。
* **数学机制**：
  1. 根据近期 20 日实现波动率 $\hat{\sigma}_{20, t-1}$ 的倒数动态缩放基准组合敞口 $f(t)$：
     $$f_{\text{managed}}(t) = \min\left(1.0, \frac{\text{Target Volatility}}{\hat{\sigma}_{20, t-1}}\right) \cdot f(t)$$
  2. 未分配的权重 $(1 - f_{\text{managed}}(t))$ 持有现金替代标的 (`BIL`)。
  3. 通过在市场波动率剧烈飙升时快速去杠杆，消除动量崩溃长尾风险 (Barroso & Santa-Clara 2015)。

### 策略 4：加速双重动量 (Accelerating Dual Momentum, ADM)
* **学术/从业者背景**：Chris Ludlow & Steve Hanly (2018, EngineeredPortfolio.com)，由 AllocateSmartly 独立跟踪。
* **标的池**：4 个 ETF -- `SPY`, `SCZ` 以及防御对 `TLT` / `TIP`。

### 策略 5：警惕资产配置 (Vigilant Asset Allocation, VAA-G4)
* **学术背景**：Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624)。
* **机制**：13612W 动量评分，在进攻型和防御型标的池之间二元切换。

### 策略 6–9：整合后的择时策略
* **RSI(2) 均值回归**：Connors 风格的短线 RSI 均值回归择时策略。
* **趋势回调波段**：趋势跟踪回调策略，在确立的上行趋势中逢低买入。
* **ATR 自适应网格**：具备趋势过滤和回撤止损的波动率缩放网格交易策略。
* **政体切换集成**：ADX 政体切换集成策略，结合趋势跟踪与 RSI 均值回归。

### 策略 10–11：海龟通道突破策略 (S1 & S2)
* **历史与学术背景**：Richard Dennis & William Eckhardt (1983 "Turtle Traders")，Richard Donchian (1960 "High-Low Channel Breakout")，Robert Carver (2023 "Systematic Trading")。
* **数学机制**：
  1. **Donchian 突破入场**：
     * **系统 1 (S1 - 20 日)**：当 $Close_i(t) > \max(High_i(t-20 \dots t-1))$ 时做多买入。
     * **系统 2 (S2 - 55 日)**：当 $Close_i(t) > \max(High_i(t-55 \dots t-1))$ 时做多买入。
  2. **趋势过滤器**：可选的 $Close_i(t) > SMA_{200, i}(t)$ 门控，防止在长期熊市中买入突破。
  3. **Donchian & $2N$ ATR 出场**：
     * **Donchian 低点出场**：当 $Close_i(t) < \min(Low_i(t-N_{\text{exit}} \dots t-1))$ 时出场（S1 为 10 日，S2 为 20 日）。
     * **$2N$ ATR 追踪止损**：当价格从入场以来的最高点下跌 $2 \times \text{ATR}_{20}$ 时出场。
  4. **逆 ATR 波动率仓位控制**：按与 $1 / (\text{ATR}_{20} / Close)$ 成正比归一化活跃突破标的的风险敞口。未分配资金默认进入现金替代标的 (`BIL`)。

### 策略 12–15：现代热门静态组合（新增于深度研究拓展）

* **永久组合 (Permanent Portfolio)**（Harry Browne，20 世纪 80 年代）：25% 美国股票 / 25% 长期国债 / 25% 现金 / 25% 黄金，每年再平衡。
* **黄金蝴蝶 (Golden Butterfly)**（Tyler / Portfolio Charts）：全市场股票 / 小型股 / 长期债券 / 短期债券 / 黄金各 20% ——相比永久组合增加了小型股倾斜并将固定收益按久期拆分。
* **全天候 (All Weather / "All Seasons")**（Tony Robbins *Money: Master the Game*）：30% 股票 / 40% 长期债券 / 15% 中期债券 / 7.5% 黄金 / 7.5% 商品。
* **HFEA — "Hedgefundie's Excellent Adventure"**（Bogleheads 论坛）：55% UPRO (3x 每日 S&P 500) / 45% TMF (3x 每日 20+年国债)，季度再平衡。

### 策略 16–17：现代系统化 TAA 扩展

* **保护性资产配置 — PAA** (Wouter J. Keller & Jan Willem Keuning, 2016, SSRN #2759734)。
* **自适应资产配置 — AAA** (Butler, Philbrick, Gordillo & Varadi, 2012, SSRN #2328254)。

### 策略 18：缠中说禅笔枢轴移动 (Chan Pivot Shift)

* **缠中说禅笔枢轴移动** (`ChanPivotShiftStrategy`, `chan_pivot_shift`)：对缠论价格结构的从零实现（`rs/chan_structure.py`）。合并包含关系、检测顶/底分型、连成笔、将重叠笔组包含为枢轴。当新枢轴区间整体高于上一枢轴且形成确认回调低点时做多。

### 策略 19：复利安全边际 (Compounder Margin of Safety)

* **复利安全边际** (`CompounderMarginOfSafetyStrategy`, `compounder_margin_of_safety`)：价格端代理版本的价值投资框架。真实基本面版本（包含真实 ROE/股息率/盈利增长）请参阅独立的 `fundamental_screener` 项目。

### 策略 20：缠论三类买卖点 (Chan Three-Type Buy/Sell Points，策略 18 的增量扩展)

* **缠论三类买卖点** (`ChanThreeTypeStrategy`, `chan_three_type`)：对策略 18 的**增量扩展**，而非修改——`ChanPivotShiftStrategy`/`chan_structure.py` 保持原样不变，本策略作为对缠中说禅理论更贴近正式分类法的独立实现与其并存。在 `chan_structure.py` 的笔之上新增两层结构：线段（对真实特征序列终止规则的一种披露性价格近似）与线段级别的中枢（直接复用 `chan_structure.build_pivots`，仅将输入从笔换成线段）。将策略 18 的背驰代理替换为基于 `common.indicators.macd`（此前未被本项目任何策略使用）的真实 MACD 柱面积背驰，并实现正式的一/二/三类买卖点分类法：第一类买卖点是经 MACD 背驰确认的中枢突破/跌破；第二类买卖点是第一类点之后未创新极值的回抽失败点；第三类买卖点是突破后回抽不破中枢边缘的确认点（无需背驰）。完整的披露性简化见 `rs/chan_signals.py`。

---

## 2. JSON 策略配置 (`strategies_config.json`)

所有策略参数、描述及自然语言定义均在 `research_strategy/strategies_config.json` 中统一定义。

---

## 2b. 因子标签与 `factor_summary.json`

`strategies_config.json` 中的每条配置均可带有 `"factors"` 列表，用于标注该策略所依赖的量化因子类别。标签词汇表通过 `common/factor_taxonomy.py` 的 `FACTOR_CATEGORIES` 共享。

每次运行 `run_research_strategy.py` 后，`results/factor_summary.json` 会按这些标签聚合已运行策略的回测性能。`strategy_generator` 可通过 `--factor-report` 加载该报告并在策略性能接近时进行平局决胜。

---

## 3. 严格离线测试策略

**不获取也不需要任何实时市场数据。**
所有 CLI 运行和单元测试均基于通过几何布朗运动和因子漂移模型生成的合成多资产 OHLCV 数据（`common/testing.py`）严格离线执行。

---

## 4. 目录结构

```
apps/quant/research_strategy/
├── rs/
│   ├── __init__.py
│   ├── config.py              # StrategyConfig & load_strategies_config()
│   ├── nl_parser.py           # 自然语言描述 -> ParsedStrategySpec
│   ├── chan_structure.py      # 独立缠论结构检测器（分型/笔/枢轴）
│   ├── chan_signals.py        # 增量扩展：线段、真实 MACD 背驰、一/二/三类买卖点
│   ├── timing_aspects.py      # 单资产择时模板的入场 x 出场/风控要素分解
│   └── strategy.py            # NaturalLanguageStrategy 引擎与策略实现
├── strategies_config.json     # 所有策略和参数的中央 JSON 配置
├── run_research_strategy.py   # 动态加载策略配置的 CLI 运行器
├── dashboard.py               # 终端 ASCII 报告查看器
├── tests/
│   ├── test_nl_parser.py      # 自然语言解析器的离线单元测试
│   ├── test_chan_structure.py # 缠论结构检测器的离线单元测试
│   ├── test_chan_signals.py   # 线段/MACD 背驰/三类买卖点的离线单元测试
│   ├── test_timing_aspects.py # 入场 x 出场要素组合的离线单元测试
│   └── test_strategy.py       # 所有策略和配置加载的离线单元测试
└── README_ZH.md               # 策略公式、引用与指南
```

---

## 5. 使用指南

本项目与 `pipeline/` 项目组的其余项目共享单个 `uv` 管理的环境。从 `pipeline/`（上一级）运行一次 `uv sync`，然后：

### 运行单元测试
```powershell
uv run pytest research_strategy/tests -v
```

### CLI 运行示例
```powershell
# 运行所有策略
uv run python research_strategy/run_research_strategy.py --strategy all

# 运行单个策略
uv run python research_strategy/run_research_strategy.py --strategy dual_momentum

# 使用真实市场数据 (yfinance)
uv run python research_strategy/run_research_strategy.py --strategy all --data-provider yfinance --no-cache
```

---

## 6. 数据结构与 Schema

参阅 `../../common/README_ZH.md` (§1–4) 了解共享的 OHLCV DataFrame、标的池字典、目标权重 DataFrame 和组合回测结果字典格式。

### 输出产物
- `results/research_strategy_report.json`：逐策略的性能指标报告。
- `results/top_strategies_summary.json`：按夏普比率排名的前 N 策略榜单。
- `results/<strategy>_weights.csv`：各策略每日持仓稠密权重 CSV。
- `results/factor_summary.json`：按因子标签聚合的性能摘要。
