[ [English](README.md) | 简体中文 ]

# 量化交易策略研究 (`research_strategy`)

一个专门的子项目，实现并评估 43 种量化交易策略：从学术文献与从业者研究（*Journal of Finance*、*Journal of Portfolio Management*、SSRN、AllocateSmartly）中综合而成的战术资产配置 (TAA) 策略；单资产择时策略；Donchian 通道突破系统；现代热门静态/固定权重组合（永久组合、黄金蝴蝶、全天候、HFEA）；布林带通道突破与均值回归；残差动量与自适应快速扩张；涵盖 16 种策略的完整缠论及类似结构分析体系（缠中说禅：笔中枢移动、三类买卖点、MACD 背驰、多周期趋势共振、三买回抽、均值回归背驰、多阶段复合阶梯建仓、最优选择器元策略、风控混合策略、VAA 复合防御、中枢震荡监视器、斐波那契均线板块轮动，以及裸K形态回踩确认、成交量分布POC迁移与三浪斐波那契扩展）；宏观政体因子自适应复合引擎；以及采用核心-卫星配置架构的机构级多策略 Alpha 账簿 (`MultiStrategyAlphaBookStrategy`)。

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

### 策略 19：缠中说禅笔枢轴移动（MACD 版）(Chan Pivot Shift (MACD)，策略 18 的增量拷贝)

* **缠中说禅笔枢轴移动（MACD 版）** (`ChanPivotShiftMACDStrategy`, `chan_pivot_shift_macd`)：对策略 18 的近乎逐字拷贝，而非修改——`ChanPivotShiftStrategy`/`chan_structure.py` 保持原样不变。保留完全相同的基于笔的枢轴区间上移/下移买卖规则（有意不像下方策略 21 那样重建于线段之上），但将其披露性的笔斜率/长度"动量背驰代理"替换为基于 `common.indicators.macd` 的真实 MACD 柱面积背驰。除代理替换外的一处刻意扩展：做成**对称**结构——顶背驰卖出信号（新高但 MACD 动量减弱）与底背驰买入信号（新低但 MACD 动量减弱）并存，而原代理只会产生卖出信号，且只在上升笔上检测。实现见 `rs/chan_signals.py` 的 `compute_chan_pivot_macd_signals`。

### 策略 20：复利安全边际 (Compounder Margin of Safety)

* **复利安全边际** (`CompounderMarginOfSafetyStrategy`, `compounder_margin_of_safety`)：价格端代理版本的价值投资框架。真实基本面版本（包含真实 ROE/股息率/盈利增长）请参阅独立的 `fundamental_screener` 项目。

### 策略 21：缠论三类买卖点 (Chan Three-Type Buy/Sell Points，策略 18 的增量扩展)

* **缠论三类买卖点** (`ChanThreeTypeStrategy`, `chan_three_type`)：对策略 18 的**增量扩展**，而非修改——`ChanPivotShiftStrategy`/`chan_structure.py` 保持原样不变，本策略作为对缠中说禅理论更贴近正式分类法的独立实现与其并存。在 `chan_structure.py` 的笔之上新增两层结构：线段（对真实特征序列终止规则的一种披露性价格近似）与线段级别的中枢（直接复用 `chan_structure.build_pivots`，仅将输入从笔换成线段）。将策略 18 的背驰代理替换为基于 `common.indicators.macd`（此前未被本项目任何策略使用）的真实 MACD 柱面积背驰，并实现正式的一/二/三类买卖点分类法：第一类买卖点是经 MACD 背驰确认的中枢突破/跌破；第二类买卖点是第一类点之后未创新极值的回抽失败点；第三类买卖点是突破后回抽不破中枢边缘的确认点（无需背驰）。完整的披露性简化见 `rs/chan_signals.py`。

### 策略 22–30：高级缠论结构体系 (`rs/chan_advanced_strategies.py` 与 `rs/chan_lesson_strategies.py`)

* **策略 22：缠论高级笔中枢移动 (MACD 版)** (`ChanPivotShiftMACDAdvStrategy`, `chan_pivot_shift_macd_adv`)：在策略 19 基础上引入大级别趋势过滤（200 日均线门控）与基于 ATR 的动态尾随风险缓冲区。
* **策略 23：缠论多周期趋势共振** (`ChanMTFTrendStrategy`, `chan_mtf_trend`)：跨日线与模拟更高周期分型笔结构进行趋势共识验证，仅在多周期趋势共振向上时参与突破。在 21 折前行测试中位列 Tier 1 Alpha 领导者（夏普 1.29，CAGR 11.27%）。
* **策略 24：缠论趋势第三类买点** (`ChanTrendThirdBuyStrategy`, `chan_trend_third_buy`)：严格实现第三类买点（三买）——捕获中枢向上突破后第一次次级别回抽且不跌回中枢上沿的强力趋势加速机会。Tier 1 Alpha 领导者（夏普 1.45，CAGR 15.18%）。
* **策略 25：缠论均值回归背驰** (`ChanMeanReversionDivergenceStrategy`, `chan_mean_reversion_divergence`)：针对价格偏离中枢中心过远（极端中枢偏离度）并在 MACD 柱线上呈现明确底背驰时执行逆势高胜率反弹操作。Tier 2 稳健防守（夏普 2.26，CAGR 4.27%，最大回撤 3.8%）。
* **策略 26：缠论多阶段阶梯复合** (`ChanCompositeStrategy`, `chan_composite`)：多阶段动态仓位构建体系，在一类底背驰（1/3 仓）、二类确认（1/3 仓）、三类突破（1/3 仓）顺势分批递增并附带跟踪止损。Tier 1 Alpha 领导者（夏普 1.53，CAGR 15.91%，最大回撤 5.71%，90.5% 正收益折数）。
* **策略 27：缠论最优选择器元策略** (`ChanBestSelectorStrategy`, `chan_best_selector`)：动态评估各标的的缠论结构形态与所处阶段，为每只资产自适应路由最优信号模式（突破、回调或背驰）。
* **策略 28：缠论中枢 MACD + VAA 复合策略** (`ChanVAACompoundStrategy`, `chan_vaa_compound`)：将缠论微观结构 Alpha 与 Keller & Keuning 的 VAA 13612W 金丝雀宏观风控结合，当宏观警报拉响时一键切换至避险防御资产（`BIL`、`IEF`）。Tier 1 Alpha 领导者（夏普 1.30，CAGR 14.25%，最大回撤 5.0%）。
* **策略 29：缠论中枢区间震荡监视器** (`ChanPivotOscillationStrategy`, `chan_pivot_oscillation`)：在结构性盘整中枢形成期间，在中枢下沿边界逢低做多、中枢上沿减仓止盈的网格化区间策略。
* **策略 30：缠论斐波那契均线板块强弱轮动** (`ChanFiboSectorStrengthStrategy`, `chan_fibo_sector_strength`)：利用 8/13/21/55/89 斐波那契均线束测算横截面板块动量强度，并以缠论中枢位确认突破质量。

### 策略 31–36：扩展 TAA、波动率与因子策略 (`rs/taa_strategies.py`, `rs/bollinger_strategy.py`, `rs/residual_momentum_strategy.py`, `rs/adaptive_fast_expansion_strategy.py`)

* **策略 31：混合资产配置 (HAA)** (`HybridAssetAllocation`, `hybrid_asset_allocation`)：Wouter Keller (2023, SSRN)。单金丝雀资产 (`TIP`) 驱动，在平静期持有前 4 种高动量进攻资产，在动荡期配置防御避险资产 (`IEF`, `BIL`)。
* **策略 32：防御资产配置 (DAA)** (`DefensiveAssetAllocation`, `defensive_asset_allocation`)：Wouter Keller & Jan Willem Keuning (2018, SSRN)。双金丝雀 (`VWO`, `BND`) 动态预警，在平静期分配至前 6 只进攻资产，在半崩盘或全崩盘状态下按 50%–100% 比例转入防御资产 (`IEF`, `LQD`, `BIL`)。
* **策略 33：布林带挤压突破（方法一）** (`BollingerBreakoutStrategy`, `bollinger_breakout`)：John Bollinger (2001, *Bollinger on Bollinger Bands*)。识别波动率极限压缩区间（带宽处于 126 日低位），在向上放量破轨时顺势做多。
* **策略 34：布林带均值回归（方法三）** (`BollingerMeanReversionStrategy`, `bollinger_mean_reversion`)：布林带 %b 摆动指标，在价格深跌下破下轨（%b < 0.05）且 RSI 超卖时入场博取回归 20 日中轨。
* **策略 35：残差动量策略** (`ResidualMomentumStrategy`, `residual_momentum`)：David Blitz, Juan Pang & Pim van Vliet (2013, *Journal of Empirical Finance*)。通过对基准 (`SPY`) 滚动 36 个月回归剥离市场 Beta 暴露，按 12 个月特质性残差收益与残差风险之比进行横截面排序配置。
* **策略 36：自适应快速扩张策略** (`AdaptiveFastExpansionStrategy`, `adaptive_fast_expansion`)：利用自适应动态 ATR 管道捕捉波动率政体跳变初期的动能爆发。

### 策略 37：宏观政体因子自适应复合策略 (`rs/regime_factor_compound_strategy.py`)

* **宏观政体因子自适应复合策略** (`RegimeFactorCompoundStrategy`, `regime_factor_compound`)：基于经济增长、通胀与流动性利率指标识别宏观政体（增长/通胀/紧缩），按逆波动率风险平价加权在动量、套息、低波与防御因子间动态分配风险预算。

### 策略 38：机构级多策略 Alpha 账簿——最优核心-卫星蓝图 (`rs/multi_strategy_alpha_book.py`)

* **学术与从业者背景**：多策略 Pod 独立运行架构（Millennium、Point72、Citadel；Blitz 2024；Roncalli 2013 风险预算管理；Quant Memo 2026）。
* **核心-卫星架构矩阵 (`ms_pod_preset="core_satellite"`)**：
  - **核心 Pod 1：`ChanPivotShiftMACDStrategy`（30% 目标预算）**：基于结构笔中枢移动与 MACD 柱面积背驰确认的顶级结构 Alpha。21 折前行表现：夏普 1.75，CAGR 15.11%，最大回撤 4.39%，胜率 95.2%。
  - **核心 Pod 2：`ChanCompositeStrategy`（30% 目标预算）**：融合一/二/三类买点多阶段阶梯式建仓的结构复合 Alpha。21 折前行表现：夏普 1.53，CAGR 15.91%，最大回撤 5.71%，胜率 90.5%。
  - **卫星 Pod 3：`VigilantAssetAllocation`（20% 目标预算）**：战术防守与危机阿尔法，Keller 13612W 动量与金丝雀崩盘切换。在 Fold 10（2020 年疫情黑天鹅）期间取得 +54.9% 年化正收益。
  - **卫星 Pod 4：`AcceleratingDualMomentum`（20% 目标预算）**：多周期跨资产相对与绝对动量加速，与缠论 Pods 保持仅 0.35 的极低相关性。21 折前行表现：夏普 1.02，CAGR 16.78%。
  - *可选预设*：`alpha_leaders`（集中配置 Tier 1 缠论阿尔法 Pods）与 `all_regime`（包含永久组合的全天候配置）。
* **数学与机制革新**：
  1. **动态逆波动率风险预算**：按滚动的 63 日实现波动率倒数 ($1/\sigma$) 动态切分风险，设置 $[15\%, 35\%]$ 权重上下限与 $\alpha=0.5$ 指数平滑，防止单一资产或策略主导组合风险。
  2. **快速复苏型回撤阻尼正则化**：以平滑函数 $D_p = \max(0.20, 1.0 - \text{DD}_p / 0.15)$ 取代传统 80% 断崖式隔离熔断；一旦 Pod 的 10 日短期收益率回正（$R_{10d} > 0$），**立即清空回撤惩罚**，彻底杜绝底部踏空陷阱。
  3. **股票成长广度隔离宏观节流阀**：严格基于股票成长类资产（`SPY`, `QQQ`, `IWM`, `EFA`, `EEM`, `VNQ` > 200 日均线）测算市场广度，与美债熊市（如 2022–2024）彻底解耦；并配备 15 日短线广度推力阈值 (>0.65) 一键快速恢复 1.0 满仓。
  4. **Pod 原生稀疏执行模式 (`ms_execution_mode="pod_native_sparse"`)**：无损保留各 Pod 内部月度交易与盘中轮动节点，合并每日目标权重后应用严苛的稀疏权重契约（显式写入 `0.0`，闲置资金自动归入 `BIL`）。
* **21 折滚动前行回测（2015–2026，10.5 年）实证表现**：
  - 平均夏普比率：**1.15**（升级前 0.82，+40.2%）
  - 平均年化 CAGR：**12.49%**（升级前 4.71%，+165.1%）
  - 极端最差折最大回撤：**13.95%**（升级前 18.04%，尾部风险下降 22.7%）
  - Calmar 比率：**3.15**（升级前 2.53）
  - 正收益折胜率：**85.71%**（18/21 折盈利，升级前 66.67%）
  - 战胜 SPY 基准概率：**42.86%**（升级前 19.05%）

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
pipeline/research_strategy/
├── rs/
│   ├── __init__.py
│   ├── config.py                         # StrategyConfig & load_strategies_config()
│   ├── nl_parser.py                      # 自然语言描述 -> ParsedStrategySpec
│   ├── chan_structure.py                 # 独立缠论结构检测器（分型/笔/枢轴）
│   ├── chan_signals.py                   # 增量扩展：线段、真实 MACD 背驰、一/二/三类买卖点
│   ├── chan_advanced_strategies.py       # 高级缠论策略集（多周期共振、三买回抽、均值回归、阶梯复合、风控混合、VAA 复合、最优选择）
│   ├── chan_similar_strategies.py        # 缠论类似模型（裸K形态回踩确认、成交量分布POC迁移、三浪斐波那契扩展）
│   ├── chan_lesson_strategies.py         # 缠论教程拓展策略（中枢震荡监视器、斐波那契均线板块轮动）
│   ├── taa_strategies.py                 # 拓展战术资产配置策略（PAA、AAA、HAA、DAA）
│   ├── bollinger_strategy.py             # 布林带突破（方法一）与均值回归（方法三）
│   ├── residual_momentum_strategy.py     # 剥离 Beta 的特质性残差动量
│   ├── adaptive_fast_expansion_strategy.py # 自适应快速波动率扩张冲冲策略
│   ├── regime_factor_compound_strategy.py # 宏观政体因子自适应复合引擎
│   ├── multi_strategy_alpha_book.py      # 机构级多策略 Alpha 账簿（最优核心-卫星蓝图）
│   ├── timing_aspects.py                 # 单资产择时模板的入场 x 出场/风控要素分解
│   └── strategy.py                       # NaturalLanguageStrategy 引擎与策略实现
├── strategies_config.json                # 包含 43 个策略和参数的中央 JSON 配置
├── run_research_strategy.py              # 动态加载策略配置的 CLI 运行器
├── dashboard.py                          # 终端 ASCII 报告查看器
├── tests/
│   ├── test_nl_parser.py                 # 自然语言解析器的离线单元测试
│   ├── test_chan_structure.py            # 缠论结构检测器的离线单元测试
│   ├── test_chan_signals.py              # 线段/MACD 背驰/三类买卖点的离线单元测试
│   ├── test_chan_advanced_strategies.py  # 高级缠论策略的离线单元测试
│   ├── test_chan_similar_strategies.py   # 缠论类似策略的离线单元测试
│   ├── test_chan_lesson_strategies.py    # 缠论教程策略的离线单元测试
│   ├── test_timing_aspects.py            # 入场 x 出场要素组合的离线单元测试
│   ├── test_strategy.py                  # 策略实现与配置加载的离线单元测试
│   ├── test_bollinger_strategy.py        # 布林带策略的离线单元测试
│   ├── test_regime_factor_compound_strategy.py # 宏观政体策略离线单元测试
│   ├── test_adaptive_fast_expansion_strategy.py # 自适应扩张策略离线单元测试
│   ├── test_novel_alpha_strategies.py    # 新型阿尔法策略离线单元测试
│   └── test_multi_strategy_alpha_book.py # 多策略 Alpha 账簿离线单元测试
└── README_ZH.md                          # 策略公式、引用与指南
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

# 将所有已配置的策略导出为可直接被 backtester 使用的 strategy.json (无需加载市场数据)
uv run python research_strategy/run_research_strategy.py --dump-strategies
```

---

## 6. 数据结构与 Schema

参阅 `../../common/README_ZH.md` (§1–4) 了解共享的 OHLCV DataFrame、标的池字典、目标权重 DataFrame 和组合回测结果字典格式。

### 输出产物
- `results/research_strategy_report.json`：逐策略的性能指标报告。
- `results/top_strategies_summary.json`：按夏普比率排名的前 N 策略榜单。
- `results/<strategy>_weights.csv`：各策略每日持仓稠密权重 CSV。
- `results/factor_summary.json`：按因子标签聚合的性能摘要。
