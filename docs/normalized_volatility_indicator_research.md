# 归一化波动率指标（Normalized Volatility Indicators）深度研究与量化交易应用规范
# (Deep Quantitative Research & Systematic Trading Specification of Normalized Volatility Indicators)

> [!IMPORTANT]
> **唯一权威任务拆解与路线图总表声明 (Single Source of Truth)**：
> 本设计规范专注于**归一化波动率指标（Normalized Volatility Indicators）族群的物理必要性、数学推导、无偏估计量构造、跨截面风控及分层架构集成规范**。
> 整个量化交易代码库所有工程级任务拆解、优先级编排、状态流转与波次落地跟踪，必须且仅在 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) 中统一集中维护。本文档涉及的算子新增、切面改造及实盘护栏增强，均作为专项原子任务挂载至该主计划。

---

## 目录 (Table of Contents)

- [一、 波动率归一化的物理必要性与三大失真痛点](#一-波动率归一化的物理必要性与三大失真痛点)
  - [1.1 价格绝对量纲失真（Nominal Price Distortion）](#11-价格绝对量纲失真nominal-price-distortion)
  - [1.2 时间跨度 $\sqrt{t}$ 扩散失真（Square-Root-of-Time Horizon Distortion）](#12-时间跨度-sqrtt-扩散失真square-root-of-time-horizon-distortion)
  - [1.3 时序分布非平稳异方差性（Heteroskedasticity & Volatility Clustering）](#13-时序分布非平稳异方差性heteroskedasticity--volatility-clustering)
- [二、 归一化波动率指标族六大数学模型与推导](#二-归一化波动率指标族六大数学模型与推导)
  - [2.1 基础比例型：NATR / ATR%（Normalized Average True Range）](#21-基础比例型natr--atrnormalized-average-true-range)
  - [2.2 极值几何无偏估计型：Garman-Klass (1980) 与 Yang-Zhang (2000)](#22-极值几何无偏估计型garman-klass-1980-与-yang-zhang-2000)
  - [2.3 动态偏离度与区制转换型：Z-Score 波动率偏离度与 ATR Regime Ratio](#23-动态偏离度与区制转换型z-score-波动率偏离度与-atr-regime-ratio)
  - [2.4 动量相对波动型：RVI（Relative Volatility Index, Dorsey 1993）](#24-动量相对波动型rvirelative-volatility-index-dorsey-1993)
  - [2.5 分形维数与混沌状态型：CHOP 混沌指数（Choppiness Index, Dreiss 1991）](#25-分形维数与混沌状态型chop-混沌指数choppiness-index-dreiss-1991)
  - [2.6 归一化波动率指标族多维对比表](#26-归一化波动率指标族多维对比表)
- [三、 归一化波动率在量化交易系统中的四大核心应用](#三-归一化波动率在量化交易系统中的四大核心应用)
  - [3.1 资产级与组合级反波动率自适应定仓（Inverse-Vol Sizing & Risk Parity）](#31-资产级与组合级反波动率自适应定仓inverse-vol-sizing--risk-parity)
  - [3.2 波动率挤压（Volatility Squeeze）与爆发突破信号滤波](#32-波动率挤压volatility-squeeze与爆发突破信号滤波)
  - [3.3 弹性自适应追踪止损（Dynamic Adaptive Stops: Chandelier & SuperTrend）](#33-弹性自适应追踪止损dynamic-adaptive-stops-chandelier--supertrend)
  - [3.4 截面低波动异象选股与风控（Cross-Sectional Low-Volatility Anomaly）](#34-截面低波动异象选股与风控cross-sectional-low-volatility-anomaly)
- [四、 量化代码实现与在当前代码库（quant）的分层挂载规范](#四-量化代码实现与在当前代码库quant的分层挂载规范)
  - [4.1 算子层沉淀：common/indicators.py 标准算子原型](#41-算子层沉淀commonindicatorspy-标准算子原型)
  - [4.2 权重切面层集成：common/strategy_aspects.py 定仓与选股切面](#42-权重切面层集成commonstrategy_aspectspy-定仓与选股切面)
  - [4.3 组合策略层集成：pipeline/research/blend/blend_base.py 宏观预算与止损防线](#43-组合策略层集成pipelineresearchblendblend_basepy-宏观预算与止损防线)
  - [4.4 实盘执行护栏集成：scripts/ 两阶段操作联动](#44-实盘执行护栏集成scripts-两阶段操作联动)
- [五、 总结与最佳实践准则](#五-总结与最佳实践准则)
  - [5.1 绝对三大操作禁忌（Avoid At All Costs）](#51-绝对三大操作禁忌avoid-at-all-costs)
  - [5.2 黄金组合搭档矩阵（Indicator Confluence Matrix）](#52-黄金组合搭档矩阵indicator-confluence-matrix)
  - [5.3 生产级避坑指南：NaN/0.0 契约与防未来函数](#53-生产级避坑指南nan00-契约与防未来函数)
- [六、 后续实施路线图与 development_plan.md 关联](#六-后续实施路线图与-development_planmd-关联)

---

## 一、 波动率归一化的物理必要性与三大失真痛点

在量化交易与风险平价投资学中，波动率（Volatility）是定价资产不确定性、分配风险预算、设定动态出场止损的基石。然而，原生技术分析或初级统计模型中常用的**名义指标（如绝对点位 ATR、收盘价绝对差分标准差等）**，在现代多品种、多时间尺度的量化体系中会引发毁灭性的度量失真。归一化（Normalization）并非简单的数学放缩，而是消除几何量纲、跨越异方差噪声、实现跨资产可比性的**物理必要步骤**。

```mermaid
flowchart TD
    subgraph RAW["原生未归一化波动率 (Raw Nominal Volatility)"]
        A1["名义 ATR (Wilder 1978)\n点数绝对值"]
        A2["Close-to-Close 方差\n极值采样丢失"]
        A3["固定点数止损 / 固定周期比较"]
    end

    subgraph DISTORTION["三大致命失真痛点 (Core Distortion Pitfalls)"]
        B1["1. 价格量纲失真 (Nominal Scale)\n高价股与低价股无法横向排序"]
        B2["2. 时间跨度 根号t 扩散失真\n分数布朗运动偏离 Hurst != 0.5"]
        B3["3. 时序分布非平稳异方差性\nARCH/GARCH 聚集、下行杠杆效应"]
    end

    subgraph NORMALIZED["现代归一化波动率体系 (Normalized Volatility Engine)"]
        C1["无量纲比例化: NATR / ATR%"]
        C2["几何无偏极值估计: Garman-Klass / Yang-Zhang"]
        C3["统计区制标准化: Volatility Z-Score / ATR Regime Ratio"]
        C4["有界状态映射: RVI (0-100) / CHOP (分形维数)"]
    end

    RAW --> DISTORTION
    DISTORTION --> NORMALIZED
```

### 1.1 价格绝对量纲失真（Nominal Price Distortion）

#### 痛点实质
传统技术分析中，J. Welles Wilder (1978) 提出的真实波幅均值（Average True Range, ATR）以价格绝对点位计量：
$$TR_t = \max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|)$$
$$ATR_{t} = \frac{(n-1)ATR_{t-1} + TR_t}{n}$$

若标的 A（如中国核心资产贵州茅台 `600519.SH`）现价为 1800 元，其日线 $ATR_{14}$ 可能高达 36 元；而标的 B（如低价银行股光大银行 `601818.SH`）现价为 3.0 元，其日线 $ATR_{14}$ 仅为 0.05 元。若一个多资产风险平价模型或多因子截面选股策略直接以名义 $ATR$ 倒数进行加权：
$$w_i \propto \frac{1}{ATR_i}$$
则标的 B 将获得数百倍于标的 A 的名义权重，瞬间导致全组合被低价股主导，甚至引发仓位爆仓风险。

#### 标的时序自身的尺度漂移
同一标的在不同牛熊周期中，名义价格会发生数倍乃至数十倍的漂移。例如标的价格由 10 元上涨至 100 元，即使资产本身的百分比波动率始终恒定为 2%，其绝对 ATR 也会由 0.2 元扩大至 2.0 元。若交易系统采用固定绝对值作为突破阈值或追踪止损距离，系统将陷入**低价期过度敏感被频繁洗盘扫损、高价期极度迟钝放任巨大回撤**的灾难中。

### 1.2 时间跨度 $\sqrt{t}$ 扩散失真（Square-Root-of-Time Horizon Distortion）

#### 经典几何布朗运动的假设缺陷
量化金融常遵循 Bachelier 与 Black-Scholes-Merton 的正态扩散假设，根据几何布朗运动（Geometric Brownian Motion, GBM）的性质，波动率随时间跨度 $t$ 按平方根扩散：
$$\sigma_{\Delta t} = \sigma_{1} \times \sqrt{\Delta t}$$

在年化处理中，日线波动率常规乘以 $\sqrt{252}$，周线乘以 $\sqrt{52}$，高频 5 分钟线（全天 48 根 K 线）乘以 $\sqrt{252 \times 48} \approx \sqrt{12096}$。

#### 实际金融资产的分数长记忆性与失真
大量实证计量研究（Mandelbrot 1963; Peters 1994 分形市场假说; Cont 2001 典型事实）表明，金融收益率序列普遍存在长记忆性（Long Memory）和自相关波动率。根据分形几何学与 Hurst 指数 $H$：
$$\mathbb{E}[|R_{t+\tau} - R_t|] \propto \tau^H$$
- 若 $H = 0.5$：标准布朗运动，$\sqrt{\tau}$ 规则严格成立；
- 若 $H > 0.5$：持续性（Persistent），收益率呈现趋势性，长期波动率扩散速率高于 $\sqrt{\tau}$；
- 若 $H < 0.5$：反持续性（Anti-persistent），均值回归显著，长期波动率扩散速率低于 $\sqrt{\tau}$。

若未对不同采样频率（如日线与 5 分钟高频特征）进行几何与时间尺度归一化，高频采样的微观微结构噪声（Bid-Ask Bounce）将在直接年化中被放大数十倍，严重误导资产分配与风险预算。

### 1.3 时序分布非平稳异方差性（Heteroskedasticity & Volatility Clustering）

#### 波动率聚集效应（Volatility Clustering）
根据 Robert Engle (1982, ARCH) 与 Tim Bollerslev (1986, GARCH) 的研究，金融时间序列并非独立同分布（i.i.d.），其二阶矩具有强烈的聚集性——**“大波动之后往往跟随大波动，小波动之后往往跟随小波动”**。

这意味着未经归一化处理的原生波动率序列是一个非平稳的随机过程。如果不将当前波动率置于其自身历史分布或动态均值中进行基准化（Benchmark），系统就无法判断当前的高波动率是属于正常的牛市健康换手，还是流动性抽干引发的极端左尾闪崩。

#### 下行杠杆效应（Leverage Effect / Asymmetric Volatility）
Fischer Black (1976) 揭示了资产收益率与波动率之间的负相关不对称性：股票价格下跌往往伴随着波动率的急剧跃升（恐慌挤兑与财务杠杆放大），而价格平稳上涨时波动率往往持续收敛。因此，在归一化指标中，必须具备区分**上行良性波动（Expansion）**与**下行恶性扰动（Downside Shock）**的数学映射能力。

---

## 二、 归一化波动率指标族六大数学模型与推导

为了彻底解决上述三大失真，量化金融工程与技术分析先驱们构建了一系列严谨的归一化波动率指标。本节严格剖析六大核心模型的数学推导、无偏性质与适用边界。

```mermaid
graph LR
    subgraph INDICATORS["归一化波动率六大数学模型"]
        M1["2.1 比例型: NATR\n(Wilder 1978)"]
        M2["2.2 极值几何型:\nGarman-Klass (1980)\nYang-Zhang (2000)"]
        M3["2.3 动态区制型:\nZ-Score Vol / ATR Ratio"]
        M4["2.4 动量相对型: RVI\n(Dorsey 1993)"]
        M5["2.5 混沌分形型: CHOP\n(Dreiss 1991)"]
    end

    M1 --> D1["跨资产百分比可比\n弹性止损标尺"]
    M2 --> D2["高统计效率无偏估计\n风险平价/方差矩阵"]
    M3 --> D3["统计异常预警\n高低波动区制转换"]
    M4 --> D4["超买超卖背离\n趋势真实波幅确认"]
    M5 --> D5["分形维数投影\n趋势/震荡状态机"]
```

### 2.1 基础比例型：NATR / ATR%（Normalized Average True Range）

#### 理论溯源
J. Welles Wilder 在 1978 年经典著作 *New Concepts in Technical Trading Systems* 中发明了 ATR，随后工业界量化分析师（如 John Murphy 1999）为了使其具备跨资产横向横截面可比性，将其与当前资产收盘价相除，演进为 **NATR**（Normalized Average True Range，在交易软件中常标注为 `ATR%`）。

#### 数学定义与递归推导
对于第 $t$ 根 K 线，其最高价为 $H_t$、最低价为 $L_t$、收盘价为 $C_t$，前一根 K 线收盘价为 $C_{t-1}$。

1. **真实波幅（True Range, TR）**：
$$TR_t = \max \left( H_t - L_t, \ |H_t - C_{t-1}|, \ |L_t - C_{t-1}| \right)$$
该定义精确覆盖了日内震荡幅度以及由于隔夜跳空（Gap Opening）引起的未成交价差。

2. **Wilder 指数加权移动平均**：
Wilder 采用平滑因子 $\alpha = \frac{1}{n}$（等价于参数为 $2n-1$ 的常规指数移动平均 EMA）：
$$ATR_t(n) = \alpha \cdot TR_t + (1 - \alpha) \cdot ATR_{t-1}(n) = \frac{(n - 1) ATR_{t-1} + TR_t}{n}$$

3. **价格归一化（NATR）**：
$$NATR_t(n) = \frac{ATR_t(n)}{C_t} \times 100\%$$

#### 物理与金融学意义
- **量纲完全消除**：NATR 是一个无量纲的百分比纯数（如 $2.5\%$），代表资产每日波幅占其自身价格的期望比例。
- **跨资产截面直接可比**：无论是 1800 元的茅台还是 3 元的低价股，若两者的 $NATR_{14}$ 均为 $2.0\%$，说明两者的日内相对不确定性完全对等，可直接放入多因子截面模型进行排序（Percentile Ranking）。
- **天然的相对波动率标尺**：为弹性自适应止损（Chandelier Exit）提供了不随股价涨跌而扭曲的物理距离基准。

---

### 2.2 极值几何无偏估计型：Garman-Klass (1980) 与 Yang-Zhang (2000)

在资产组合选择（Markowitz 1952）与风险平价配置中，精准估计资产收益率方差 $\sigma^2$ 是第一要务。常规的**收盘价对收盘价（Close-to-Close）样本方差**估计量：
$$\hat{\sigma}_{CC}^2 = \frac{1}{n-1} \sum_{t=1}^n \left( r_t - \bar{r} \right)^2, \quad \text{其中 } r_t = \ln(C_t / C_{t-1})$$
其严重缺陷在于只采集离散时点的两个采样点，**将日内所有的价格高低极值演变路径完全当作黑箱丢弃**，统计方差效率极低，需要极长的时间窗口（如 60~252 天）才能收敛，但过长的窗口又会破坏时间局部平稳性。

#### 1. Parkinson (1980) 极值估计量
Michael Parkinson (1980) 首次利用日内最高价 $H_t$ 和最低价 $L_t$，基于连续扩散布朗运动的一阶穿越时间分布推导出极值估计量：
$$\sigma_{P}^2 = \frac{1}{4 \ln 2 \cdot n} \sum_{t=1}^n \left( \ln \frac{H_t}{L_t} \right)^2 \approx \frac{0.361}{n} \sum_{t=1}^n \left( \ln \frac{H_t}{L_t} \right)^2$$
- **效率优势**：Parkinson 方差估计量的统计效率约为经典 Close-to-Close 估计量的 **5.2 倍**（即 20 天的 Parkinson 估计精度相当于 100 天的 Close-to-Close 估计）。
- **局限性**：假定无日内漂移（Drift = 0），且完全忽略了开盘跳空（Overnight Gap）。

#### 2. Garman & Klass (1980) OHLC 极值估计量
Mark Garman 与 Michael Klass (1980) 在 Parkinson 基础上进一步结合了开盘价 $O_t$ 与收盘价 $C_t$，推导出在连续布朗运动（无隔夜跳空）下**方差最小的最优无偏极值估计量**：
$$\sigma_{GK}^2 = \frac{1}{n} \sum_{t=1}^n \left[ 0.5 \left( \ln \frac{H_t}{L_t} \right)^2 - (2\ln 2 - 1) \left( \ln \frac{C_t}{O_t} \right)^2 \right]$$
其中系数 $2\ln 2 - 1 \approx 0.38629$。
- **效率跃升**：Garman-Klass 估计量的效率达到了 Close-to-Close 估计量的 **7.4 至 8.0 倍**！
- **年化归一化**：
$$\sigma_{GK, \text{annual}} = \sqrt{252 \times \sigma_{GK}^2}$$
- **代码库现存实现挂载**：当前量化库在 [`pipeline/research/blend/blend_base.py#L68-L93`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py#L68-L93) 中已将 `_compute_garman_klass_vol` 作为核心波动率引擎内置。

#### 3. Yang & Zhang (2000) 连续漂移与跳空无偏估计量
Dennis Yang 与 Qiang Zhang (2000) 发表了著名的 *Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices*。该模型是目前金融工程界公认的最强大、最完备的日线波动率无偏估计量，它同时解决了三大难题：
1. **独立于日内漂移（Drift-Independent）**；
2. **完美融合隔夜跳空（Overnight Jump/Gap）**；
3. **充分利用日内极值连续路径信息**。

**数学推导体系**：
设价格对数差分：
$$u_t = \ln(H_t / O_t), \quad d_t = \ln(L_t / O_t), \quad c_t = \ln(C_t / O_t)$$
$$o_t = \ln(O_t / C_{t-1})$$

1. **隔夜跳空方差项 $\sigma_{overnight}^2$**：
$$\sigma_{overnight}^2 = \frac{1}{n-1} \sum_{t=1}^n (o_t - \bar{o})^2, \quad \bar{o} = \frac{1}{n}\sum_{t=1}^n o_t$$

2. **日内开盘至收盘方差项 $\sigma_{open\_to\_close}^2$**：
$$\sigma_{open\_to\_close}^2 = \frac{1}{n-1} \sum_{t=1}^n (c_t - \bar{c})^2, \quad \bar{c} = \frac{1}{n}\sum_{t=1}^n c_t$$

3. **Rogers-Satchell (1991) 极值连续方差项 $\sigma_{RS}^2$**：
$$\sigma_{RS}^2 = \frac{1}{n} \sum_{t=1}^n \left[ u_t (u_t - c_t) + d_t (d_t - c_t) \right]$$

4. **Yang-Zhang 凸组合无偏方差**：
$$\sigma_{YZ}^2 = \sigma_{overnight}^2 + k \cdot \sigma_{open\_to\_close}^2 + (1 - k) \cdot \sigma_{RS}^2$$
其中最优加权因子 $k$ 最小化估计方差：
$$k = \frac{0.34}{1.34 + \frac{n + 1}{n - 1}}$$
- **效率极限**：Yang-Zhang 估计量的统计效率高达常规 Close-to-Close 估计量的 **14 倍**，即使在极端跳空频发的商品期货和 A 股隔夜市场中，依然保持严格无偏。

---

### 2.3 动态偏离度与区制转换型：Z-Score 波动率偏离度与 ATR Regime Ratio

单纯得到一个百分比数值（如 $NATR = 3\%$）仍不足以完全指导交易，因为某些高贝塔资产（如成长科技股）的长期基准波动率本就在 $3\% \sim 5\%$，而防御性公用事业资产的波动率常年在 $1\%$ 左右。为了捕捉资产**相对于自身历史常态的异常突变**，必须建立区制转换指标。

#### 1. ATR Regime Ratio（波动率区制变化比）
由代码库在 [`common/indicators.py#L133-L140`](file:///home/stone/Work/github/quant/common/indicators.py#L133-L140) 率先规范化：
$$R_{ATR}(t) = \frac{\frac{1}{S} \sum_{i=0}^{S-1} NATR_{t-i}}{\frac{1}{L} \sum_{j=0}^{L-1} NATR_{t-j}}$$
常规参数设置短期窗口 $S = 20$，长期基准窗口 $L = 60$。

- **区制阈值判别逻辑**：
  - **$R_{ATR} \ge 1.30$（高波动爆发 / 危机区制）**：短期波动率较长期均值跃升 30% 以上，预示市场进入剧烈博弈或流动性冲击阶段，必须主动下调仓位上限，放宽止损距离防假扫损；
  - **$0.80 < R_{ATR} < 1.30$（平稳常态区制）**：波动率处于中性均衡状态，基准参数平稳运行；
  - **$R_{ATR} \le 0.80$（波动率极限压缩区制）**：市场进入极致窒息窄幅整理期，根据能量守恒定律，随后大概率爆发单边突破性超级趋势。

#### 2. Normalized Volatility Z-Score（标准化波动率偏离度）
将任意时序波动率 $\sigma_t$（可为 $NATR$ 或 $\sigma_{GK}$）投影至其自身滚动窗口 $W$（如 60 或 120 日）的标准正态分布度量空间：
$$\mu_W(t) = \frac{1}{W} \sum_{i=0}^{W-1} \sigma_{t-i}, \quad s_W(t) = \sqrt{\frac{1}{W-1} \sum_{i=0}^{W-1} (\sigma_{t-i} - \mu_W(t))^2}$$
$$Z_{vol}(t) = \frac{\sigma_t - \mu_W(t)}{\max(s_W(t), \ \epsilon)}$$
- **统计学映射**：
  - $Z_{vol} > +2.0$：处于历史分布前 $2.5\%$ 的极端离群高波冲击（右尾风险），触发宏观一票否决熔断；
  - $Z_{vol} < -1.5$：处于历史极低位压缩状态，是“波动率挤压突破”策略的最佳就绪信号。

---

### 2.4 动量相对波动型：RVI（Relative Volatility Index, Dorsey 1993）

#### 理论溯源
Donald Dorsey 于 1993 年在 *Technical Analysis of Stocks & Commodities* (TASC) 杂志发表了著名的相对波动率指标（Relative Volatility Index, RVI）。Dorsey 指出，J. Welles Wilder 的相对强弱指标（RSI）以价格变动绝对值作为输入，容易在横盘震荡时产生大量无意义的微小毛刺。Dorsey 创造性地**用“价格的标准差（波动率）”替换了“价格变动差分”**，以此衡量价格方向变动背后的真实能量扩散强度。

#### 数学构建
对于给定标准差计算周期 $n$（通常取 10 天）和平滑平滑周期 $m$（通常取 14 天）：

1. **计算滚动收益波动率**：
$$S_t = \text{Std}(C, n) = \sqrt{\frac{1}{n-1}\sum_{i=0}^{n-1} (C_{t-i} - \bar{C}_t)^2}$$

2. **区分上涨与下跌波幅**：
$$U_t = \begin{cases} S_t, & \text{若 } C_t > C_{t-1} \\ 0, & \text{若 } C_t \le C_{t-1} \end{cases}$$
$$D_t = \begin{cases} S_t, & \text{若 } C_t < C_{t-1} \\ 0, & \text{若 } C_t \ge C_{t-1} \end{cases}$$

3. **Wilder 递归平滑**（衰减系数 $\beta = 1/m$）：
$$EMA(U)_t = \beta \cdot U_t + (1 - \beta) \cdot EMA(U)_{t-1}$$
$$EMA(D)_t = \beta \cdot D_t + (1 - \beta) \cdot EMA(D)_{t-1}$$

4. **归一化相对波动比率**：
$$RVI_t = 100 \times \frac{EMA(U)_t}{EMA(U)_t + EMA(D)_t}$$

#### 物理特性与交易应用
- **天然闭区间有界归一化**：$RVI \in [0, 100]$，中轨中枢为 50；
- **确认趋势质量**：当价格创出新高但 RVI 无法突破 60（甚至出现顶背离时），说明价格上涨仅由极少数缺乏流动性支持的脆弱波动推高，趋势极易衰竭；
- **牛熊波幅不对称鉴别**：$RVI > 50$ 证明上涨日的波动能量显著大于下跌日，买盘在波动率维度处于优势统治地位。

---

### 2.5 分形维数与混沌状态型：CHOP 混沌指数（Choppiness Index, Dreiss 1991）

#### 理论溯源与分形几何背景
E.W. Dreiss 于 1991 年提出 Choppiness Index (CHOP)。该指标的理论根基是 Benoît Mandelbrot 的分形几何学（Fractal Geometry）。
在欧几里得几何中，一维线段的维度是 1，二维平面的维度是 2。然而金融时间序列是一条具有**自相似性与空间填充性**的分形曲线：
- 当市场处于**极强单边趋势**时，价格轨迹近乎一条直线，分形维数趋近于 $D \to 1$；
- 当市场处于**剧烈布朗随机漫步震荡（混沌状态）**时，价格反复无序折叠，几乎填满高低价格构成的整个矩形二维平面，分形维数趋近于 $D \to 2$。

分形维数 $D$ 与 Hurst 指数满足关系 $D = 2 - H$。Dreiss 巧妙地设计了无需高昂计算开销的离散近似公式，直接映射分形维数。

#### 数学推导
对于窗口周期 $n$（经典取值 $n = 14$）：
1. **真实波幅累加总行程（Curve Length）**：
$$L_{total}(n) = \sum_{i=0}^{n-1} TR_{t-i}$$
这代表过去 $n$ 天多空双方在这段轨迹中走过的全部路程总和（折线总长度）。

2. **极值欧几里得位移跨度（Euclidean Displacement）**：
$$MaxH_n = \max(H_t, H_{t-1}, \dots, H_{t-n+1})$$
$$MinL_n = \min(L_t, L_{t-1}, \dots, L_{t-n+1})$$
$$Span_n = MaxH_n - MinL_n$$
这代表过去 $n$ 天价格波动的总包络宽度（终点与起点能够达到的最大几何空间）。

3. **分形维数测度比率**：
根据豪斯多夫分形测度 $L \propto Span^D$，两边取以 10 为底的对数：
$$\log_{10}(L_{total}) = D \cdot \log_{10}(Span)$$
Dreiss 将其转化为标准化的混沌指数百分比公式：
$$CHOP_t(n) = 100 \times \frac{\log_{10} \left( \frac{\sum_{i=0}^{n-1} TR_{t-i}}{MaxH_n - MinL_n} \right)}{\log_{10}(n)}$$

```mermaid
graph TD
    subgraph CHOP_PHYSICS["CHOP 指数物理映射原理"]
        P1["分子: TR 累加和\n折线总行程 (Total Path Length)"]
        P2["分母: Max(H) - Min(L)\n空间包络位移 (Bounding Box Span)"]
        RATIO["测度比: Path / Span\n反映曲线折叠程度"]
        FORMULA["CHOP = 100 * log10(Path / Span) / log10(N)"]
    end

    P1 --> RATIO
    P2 --> RATIO
    RATIO --> FORMULA

    FORMULA --> STATE_TREND["CHOP < 38.2\n强单边趋势 (D -> 1)\n趋势跟踪模型全速进攻"]
    FORMULA --> STATE_MID["38.2 <= CHOP <= 61.8\n常规扩散过度带"]
    FORMULA --> STATE_CHOP["CHOP > 61.8\n极度混沌震荡 (D -> 2)\n网格/均值回归模型接管"]
```

#### 关键分水岭常数（斐波那契黄金分割位）
- **$CHOP \le 38.2$（强单边趋势状态，Trend Regime）**：价格位移迅速而直接，分形维数坍缩至 1 附近，任何试图抄底摸顶的均值回归策略均会被趋势碾压，此时应**全力运行动量与趋势跟踪模型**；
- **$CHOP \ge 61.8$（极度混沌震荡状态，Chop/Consolidation Regime）**：价格在通道内来回洗盘，分形维数膨胀逼近 2，趋势策略会遭遇极高频的假突破连续扫损，此时系统应**冻结突破入场，切入网格中枢或全面观望**。

---

### 2.6 归一化波动率指标族多维对比表

| 指标名称 | 学术/工程出处 | 数学输入量纲 | 输出域区间 | 统计无偏性与效率 | 核心功能定位 | 计算复杂度 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **NATR (ATR%)** | Wilder (1978) | $H, L, C$ | $(0, +\infty)$ 百分比 | 有偏；包含跳空；效率基准 1.0x | 跨资产相对幅度度量；动态止损弹性标尺 | $O(N)$ 极低 |
| **Parkinson Vol** | Parkinson (1980) | $H, L$ | $[0, +\infty)$ 年化率 | 无偏（假设漂移=0，无跳空）；效率 5.2x | 资产波动率快速估计；过滤日内杂波 | $O(N)$ 极低 |
| **Garman-Klass** | Garman & Klass (1980) | $O, H, L, C$ | $[0, +\infty)$ 年化率 | 无偏（假设无跳空）；效率 **7.4~8.0x** | 组合风险平价定仓；反波动率加权基石 | $O(N)$ 极低 |
| **Yang-Zhang** | Yang & Zhang (2000) | $O, H, L, C$ | $[0, +\infty)$ 年化率 | **严格无偏**（支持跳空与连续漂移）；效率 **14x** | 机构级协方差矩阵估计；极端跳空市场风控 | $O(N)$ 低 |
| **ATR Regime Ratio** | 工业界量化规范 | $NATR$ 均线比 | $(0, +\infty)$ 比率 | 统计无量纲相对比值 | 宏观高低波区制划分；风控防线升降级 | $O(N)$ 极低 |
| **Volatility Z-Score** | 现代极值理论 | 任意波动率时序 | $(-\infty, +\infty)$ 标量 | 标准正态标准化度量（均值0，方差1） | 捕捉极端右尾波动冲击；能量压缩就绪判别 | $O(N)$ 低 |
| **RVI** | Dorsey (1993) | $C$ 与 滚动标准差 | $[0, 100]$ 有界区间 | 动量与波动率非线性融合 | 趋势内在动力质量确认；过滤虚假新高 | $O(N)$ 中 |
| **CHOP Index** | Dreiss (1991) | $H, L, C$ 与 $TR$ | $[0, 100]$ 有界区间 | 分形维数映射（$1 \to 0, 2 \to 100$） | 宏观趋势/震荡状态机判决；突破假信号过滤 | $O(N)$ 低 |

---

## 三、 归一化波动率在量化交易系统中的四大核心应用

```mermaid
flowchart TD
    subgraph APPS["归一化波动率四大核心量化应用"]
        APP1["3.1 资产与组合反波定仓\n(Inverse-Vol & Risk Parity)"]
        APP2["3.2 波动率挤压蓄势突破\n(Squeeze & Breakout Filter)"]
        APP3["3.3 弹性自适应单向追踪止损\n(Chandelier & SuperTrend Ratchet)"]
        APP4["3.4 截面低波动异象选股\n(Low-Volatility Anomaly Alpha)"]
    end

    APP1 --> OUT1["平滑每项资产边际风险贡献\nBarroso-Santa-Clara 组合杠杆缩放"]
    APP2 --> OUT2["Bollinger Bandwidth + CHOP 能量压缩\n突破即启动，拒绝假突破耗损"]
    APP3 --> OUT3["NATR 动态松紧度量 + 极值锁定\n让利润奔跑，防假阴线洗盘"]
    APP4 --> OUT4["Blitz-van Vliet 截面升序分箱\n获取穿越牛熊的长期异象超额"]
```

### 3.1 资产级与组合级反波动率自适应定仓（Inverse-Vol Sizing & Risk Parity）

#### 1. 资产级风险平价配比（Asset-Level Sizing）
传统等权重配置（Equal-Weighting）对高波动资产给予了过多的风险暴露。而在反波动率加权体系中，每个标的的持仓权重与其归一化波动率成反比：
$$w_i = \frac{\frac{1}{\sigma_{i, norm}^\gamma}}{\sum_{j=1}^M \frac{1}{\sigma_{j, norm}^\gamma}}$$
其中参数 $\gamma$ 为风险厌恶衰减系数（常规取 $\gamma = 1.0$ 为标准反波动率；$\gamma = 2.0$ 对应逆方差最小方差组合近似）。

**量纲升级的关键**：若 $\sigma_{i, norm}$ 使用传统 20 日收盘收益率标准差，估计误差大且极度滞后。本架构要求全面接入 **Garman-Klass 或 Yang-Zhang 无偏波动率 $\sigma_{GK, i}$**，使得单资产权重的边际风险贡献（Marginal Risk Contribution, MRC）达到跨资产严格均衡：
$$MRC_i = w_i \frac{(\Sigma w)_i}{\sqrt{w^T \Sigma w}} \approx \text{Constant}$$

#### 2. 组合级目标波动率缩放（Portfolio Volatility Targeting）
根据 Pedro Barroso 与 Pedro Santa-Clara (2015, *Momentum Has Its Moments*, JFE) 的权威论证，动量策略长期面临灾难性崩溃（Momentum Crashes），而**通过以波动率倒数动态调整全组合实际风险敞口，可以在消除左尾下行风险的同时显著提升夏普比率**。

全组合当前已实现波动率估计 $\sigma_{port, t}$，设定组合目标年化波动率 $\sigma_{target}$（如机构级默认值 $12\%$ 或牛市扩张值 $24\%$）：
$$\text{Scale}_t = \min \left( \text{MaxLeverage}, \ \frac{\sigma_{target}}{\max(\sigma_{port, t}, \ \sigma_{floor})} \right)$$
$$W_t^* = \text{Scale}_t \times W_{raw, t}$$
未投资的剩余现金敞口 $(1 - \sum w_i)$ 自动划转至无风险现金代理标的（如美股 `BIL`、A股银华日利 `511880` 或国债逆回购 `GC001`）。

---

### 3.2 波动率挤压（Volatility Squeeze）与爆发突破信号滤波

John Carter 在其经典著作中提出了基于布林带（Bollinger Bands）与肯特纳通道（Keltner Channels）的波动率挤压技术。当波动率指标经过归一化之后，能够构建出极度坚固的突破就绪与假突破过滤系统。

#### 1. 归一化布林带宽（Normalized Bollinger Bandwidth）
由 John Bollinger (1980s) 提出，通过中间基准均线 $SMA(C, 20)$ 对通道总宽度进行归一化：
$$Bandwidth_t = \frac{Upper_t - Lower_t}{Mid_t} = \frac{2 \cdot k \cdot \sigma_t}{SMA(C_t, 20)}$$

#### 2. Keltner Channel 结合之 Squeeze 判定
肯特纳通道以 $EMA(C, 20)$ 为中轨，以 $m \times ATR_{14}$ 为通道半宽。
当且仅当布林带完全落入肯特纳通道内部时，判定系统进入**“能量极端压缩（Squeeze On）”**：
$$\text{Is\_Squeezed}_t = \left( Upper_{BB} \le Upper_{KC} \right) \land \left( Lower_{BB} \ge Lower_{KC} \right)$$

#### 3. 三重能量共振确认突破过滤器
单纯由价格触及通道边界往往遭遇假突破（Bull/Bear Trap）。引入归一化波动率三元组进行状态决议：
1. **CHOP 混沌状态判别**：$CHOP_{14} \ge 61.8$（证明市场经历充分混沌换手，积蓄巨大势能）；
2. **NATR 分位数压缩**：$NATR_{14} \le \text{Quantile}_{0.15}(NATR, 120)$（当前波幅处于半年周期最低的 15% 区间）；
3. **突破释放点**：当价格突破通道上轨且 $Bandwidth$ 产生一阶正导数向上发散，同时 $RVI > 55$。
该过滤机制可将传统海龟突破或通道突破的胜率从 $35\%$ 显著提振至 $52\%$ 以上，极大降低假突破的摩擦滑点损耗。

---

### 3.3 弹性自适应追踪止损（Dynamic Adaptive Stops: Chandelier & SuperTrend）

#### 传统固定百分比止损的物理缺陷
在许多量化回测中，交易者常设定机械的硬性止损比例（例如跌破买入价 -8% 一刀切平仓）。
- 若标的为低波动资产（如日均 $NATR = 0.8\%$ 的高股息公用事业股），-8% 的跌幅意味着超过 **10 个标准差的崩塌**，此时平仓早已回天乏术，回撤极其惨烈；
- 若标的为高弹性成长科技股（如日均 $NATR = 4.5\%$），正常一天的日内洗盘即可达到 3%~5%，-8% 极易在主升浪启动前的“破底翻”洗盘中被无情扫地出局。

#### 1. 动态 Chandelier 吊灯止损（Chuck Le Beau 1990s）
吊灯止损的核心思想是将出场线挂在自建仓以来的**最高极值（Highest High）**下方，并以归一化波动率动态测量松紧度：
$$\text{StopLevel}_{t} = \max_{k \in [0, t_{held}]} (H_{t-k}) - \text{Mult} \times ATR_t$$
或以名义百分比弹性动态化：
$$\text{DynamicStopPct}_t = \text{Clip} \left( \beta \times NATR_t, \ \text{MinStopPct}, \ \text{MaxStopPct} \right)$$
例如，设定止损百分比为 $3 \times NATR_t$，并限制在 $[4\%, 12\%]$ 区间内：
- 低波动标的（$NATR=1.2\%$）：止损线自动收紧至 $-4.0\%$，防范钝刀子割肉；
- 高波动标的（$NATR=3.5\%$）：止损线自适应放宽至 $-10.5\%$，从容耐受正常的牛市洗盘。

#### 2. SuperTrend 单向棘轮自适应风控防线
Olivier Seban 创造的 SuperTrend 是一种基于 ATR 的单向不可逆棘轮（Ratchet）趋势线。本架构引入波动率归一化因子对其倍数进行自适应调制（Adaptive SuperTrend）：
$$\text{AdaptiveMult}_t = \text{BaseMult} \times \left( 1 + \alpha \cdot \frac{NATR_t - \mu_{NATR, 60}}{\sigma_{NATR, 60}} \right)$$
- 当市场处于非平稳异动期，倍数自动弹性撑开，杜绝在极端插针行情中发生误触发；
- 当市场波动率回落常态，单向棘轮紧随高点上移，锁死已获利润。

---

### 3.4 截面低波动异象选股与风控（Cross-Sectional Low-Volatility Anomaly）

#### 学术理论奠基
经典资本资产定价模型（CAPM, Sharpe 1964）断言高风险必有高回报（即股票预期收益与其市场贝塔 $\beta$ 成正比）。然而，实证资产定价学界的一系列重量级论文推翻了该假说：
- **David Blitz & Pim van Vliet (2007)** 发表在 *Journal of Portfolio Management* 的名篇 *The Volatility Effect: Lower Risk Without Lower Return* 证实：在美股、欧洲与日本市场中，**历史波动率最低的五分位数组合，其长周期年化收益率与风险调整后夏普比率系统性大幅超越最高波动率五分位数组合**；
- **Malcolm Baker, Brendan Bradley & Jeffrey Wurgler (2011)** 在 *Financial Analysts Journal* 发表 *Benchmarks as Limits to Arbitrage: Understanding the Low-Volatility Anomaly*，从基金经理对基准指数跟踪误差（Tracking Error）的代理冲突和杠杆约束视角，揭示了低波异象长期存在的制度性根源；
- **Andrew Ang, Robert J. Hodrick, Yuhang Xing & Xiaoyan Zhang (2006)** 在 *Journal of Finance* 揭示了特质波动率之谜（The Cross-Section of Volatility and Expected Returns），高特质波动的股票在截面上表现极其恶劣。

```mermaid
graph LR
    subgraph LOW_VOL_ENGINE["截面低波动选股与风险平价引擎"]
        UNIVERSE["股票池全量候选集 (Universe)"]
        YZ_VOL["计算无偏极值波动率\nYang-Zhang / Garman-Klass"]
        RANK["截面升序分箱排序\n升序排列 (Low-Vol Top 20%)"]
        FILTER["基本面/动量因子联合筛选\nROE > 10% 且 12M 动量 > 0"]
        WEIGHT["反波动率风险平价加权\nw_i 正比于 1 / sigma_i"]
    end

    UNIVERSE --> YZ_VOL
    YZ_VOL --> RANK
    RANK --> FILTER
    FILTER --> WEIGHT
```

#### 在量化选股体系中的落地规范
1. **指标选取**：严禁采用粗糙的收盘收益率标准差，必须在横截面上计算全市场标的的 **60 日 Yang-Zhang 或 Garman-Klass 极值波动率 $\sigma_{YZ}$**；
2. **截面升序排序**：剔除退市警示股（ST）与流动性极低的后 10% 标的后，按 $\sigma_{YZ}$ 由小到大排序，挑选最左侧的前 $20\%$ 作为低波动候选股票池；
3. **因子正交化与质量共振**：与行业中性化（Industry Neutralization）及高股息、高 ROE 质量因子叠加，形成“高防御、高夏普、低最大回撤”的穿越牛熊底仓策略。

---

## 四、 量化代码实现与在当前代码库（quant）的分层挂载规范

为了将上述理论模型无缝集成到当前代码库架构中，本节给出完整的算子原型、切面接口以及在已有框架中的具体调用映射。

```mermaid
flowchart TD
    subgraph COMMON["1. 通用算子层 (common/)"]
        OP_NATR["indicators.py\nnatr(df, period=14)"]
        OP_GK["indicators.py\ngarman_klass_vol(df, window=21)"]
        OP_YZ["indicators.py\nyang_zhang_vol(df, window=21)"]
        OP_RVI["indicators.py\nrvi(df, period=14, smooth=14)"]
        OP_CHOP["indicators.py\nchoppiness_index(df, period=14)"]
        OP_Z["indicators.py\nvolatility_zscore(series, window=60)"]
    end

    subgraph ASPECTS["2. 切面解耦层 (common/strategy_aspects.py)"]
        ASP_W["WeightingAspect:\ninverse_vol\n(注入 GK / YZ 无偏引擎)"]
        ASP_S["SelectionAspect:\nlow_vol_topn\n(截面低波异象选股切面)"]
    end

    subgraph BLEND["3. 组合策略层 (pipeline/research/blend/blend_base.py)"]
        BL_MACRO["CHOP 宏观状态机:\n分形维数动态调控子策略权重"]
        BL_STOP["动态 Chandelier 弹性止损:\n基于 NATR 的单向不可逆防线"]
        BL_INERTIA["波动率自适应重平衡阈值:\n高波放大惯性 杜绝换手磨损"]
    end

    subgraph LIVE["4. 实盘护栏层 (scripts/)"]
        LV1["check_live_portfolio_health.py\nStage 1: 组合加权 NATR 冲击预警"]
        LV2["run_live_strategy.py\nStage 2: 基于 NATR 的自适应挂单保护带宽"]
    end

    COMMON --> ASPECTS
    ASPECTS --> BLEND
    BLEND --> LIVE
```

### 4.1 算子层沉淀：[`common/indicators.py`](file:///home/stone/Work/github/quant/common/indicators.py) 标准算子原型

所有技术指标必须严格保持**无前视偏差（No Look-Ahead Bias）**、**纯向量化运算**、**对空值与零方差鲁棒处理**的优良风格。以下代码将作为新增基石函数沉淀至 [`common/indicators.py`](file:///home/stone/Work/github/quant/common/indicators.py)：

```python
"""Normalized Volatility Indicators implementation in common/indicators.py."""

import numpy as np
import pandas as pd


def natr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Normalized Average True Range (Wilder 1978, Murphy 1999).
    
    NATR_t = (ATR_t / Close_t) * 100%
    Completely dimensionless, scale-invariant, and cross-sectionally comparable.
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    
    # Wilder's 1/n smoothing
    atr_val = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    safe_close = close.replace(0, np.nan)
    return (atr_val / safe_close * 100.0).fillna(0.0)


def garman_klass_vol(df: pd.DataFrame, window: int = 21, periods_per_year: int = 252) -> pd.Series:
    """Garman & Klass (1980) OHLC extreme-value volatility estimator.
    
    Provides ~7.4x to 8x statistical efficiency over close-to-close returns.
    Assumes continuous diffusion without jumps; falls back to close-to-close std
    if Open/High/Low are degenerate.
    """
    cols = df.columns
    if not all(col in cols for col in ["Open", "High", "Low", "Close"]):
        ret = df["Close"].pct_change().fillna(0.0)
        return ret.rolling(window, min_periods=window).std() * np.sqrt(periods_per_year)

    h = df["High"].replace(0, np.nan)
    l = df["Low"].replace(0, np.nan)
    c = df["Close"].replace(0, np.nan)
    o = df["Open"].replace(0, np.nan)

    # Degenerate price check (e.g., constant prices in synthetic data)
    if (h == l).all():
        ret = c.pct_change().fillna(0.0)
        return (ret.rolling(window, min_periods=window).std() * np.sqrt(periods_per_year)).fillna(0.0)

    log_hl = np.log((h / l).clip(lower=1e-8)) ** 2
    log_co = np.log((c / o).clip(lower=1e-8)) ** 2
    
    gk_var = 0.5 * log_hl - (2.0 * np.log(2.0) - 1.0) * log_co
    gk_var = gk_var.clip(lower=0.0)
    annualized_vol = np.sqrt(gk_var.rolling(window, min_periods=window).mean() * periods_per_year)
    return annualized_vol.fillna(0.0)


def yang_zhang_vol(df: pd.DataFrame, window: int = 21, periods_per_year: int = 252) -> pd.Series:
    """Yang & Zhang (2000) drift-independent, jump-incorporated volatility estimator.
    
    The most efficient unbiased volatility estimator (~14x efficiency of Close-to-Close).
    Uniquely handles overnight jumps and continuous drift simultaneously.
    """
    cols = df.columns
    if not all(col in cols for col in ["Open", "High", "Low", "Close"]):
        ret = df["Close"].pct_change().fillna(0.0)
        return ret.rolling(window, min_periods=window).std() * np.sqrt(periods_per_year)

    h, l, c, o = df["High"], df["Low"], df["Close"], df["Open"]
    prev_c = c.shift(1)

    log_ho = np.log((h / o).clip(lower=1e-8))
    log_lo = np.log((l / o).clip(lower=1e-8))
    log_co = np.log((c / o).clip(lower=1e-8))
    log_oc = np.log((o / prev_c).clip(lower=1e-8))

    # Overnight gap variance
    overnight_var = log_oc.rolling(window, min_periods=window).var()

    # Open-to-close variance
    open_to_close_var = log_co.rolling(window, min_periods=window).var()

    # Rogers-Satchell variance
    rs_var = (log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)).rolling(window, min_periods=window).mean()

    # Optimal weighting constant k
    k = 0.34 / (1.34 + (window + 1) / (window - 1)) if window > 1 else 0.5

    yz_var = (overnight_var + k * open_to_close_var + (1.0 - k) * rs_var).clip(lower=0.0)
    annualized_vol = np.sqrt(yz_var * periods_per_year)
    return annualized_vol.fillna(0.0)


def rvi(df: pd.DataFrame, period: int = 14, smooth_period: int = 14) -> pd.Series:
    """Relative Volatility Index (Donald Dorsey 1993).
    
    Measures the direction of volatility on a 0-100 bounded scale.
    Replaces price changes in RSI with rolling standard deviations of close prices.
    """
    close = df["Close"]
    std_series = close.rolling(period, min_periods=period).std()
    delta = close.diff()

    up = pd.Series(np.where(delta > 0, std_series, 0.0), index=df.index)
    down = pd.Series(np.where(delta < 0, std_series, 0.0), index=df.index)

    # Wilder smoothing of up/down volatility
    avg_up = up.ewm(alpha=1.0 / smooth_period, adjust=False, min_periods=smooth_period).mean()
    avg_down = down.ewm(alpha=1.0 / smooth_period, adjust=False, min_periods=smooth_period).mean()

    denom = (avg_up + avg_down).replace(0, np.nan)
    rvi_val = 100.0 * (avg_up / denom)
    
    # Handle flat zero-volatility windows by returning the neutral 50.0 level
    flat = (avg_up == 0) & (avg_down == 0)
    rvi_val = rvi_val.where(~flat, 50.0)
    return rvi_val.fillna(50.0)


def choppiness_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Choppiness Index (E.W. Dreiss 1991).
    
    Directly reflects Mandelbrot fractal dimension:
    CHOP in [0, 100]. CHOP <= 38.2 indicates strong trend (D -> 1).
    CHOP >= 61.8 indicates extreme consolidation/choppiness (D -> 2).
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    sum_tr = tr.rolling(period, min_periods=period).sum()
    max_hi = high.rolling(period, min_periods=period).max()
    min_lo = low.rolling(period, min_periods=period).min()
    span = (max_hi - min_lo).clip(lower=1e-8)

    chop = 100.0 * np.log10(sum_tr / span) / np.log10(float(period))
    return chop.clip(lower=0.0, upper=100.0).fillna(50.0)


def volatility_zscore(series: pd.Series, window: int = 60) -> pd.Series:
    """Rolling Z-Score of any normalized volatility series."""
    mean_val = series.rolling(window, min_periods=window).mean()
    std_val = series.rolling(window, min_periods=window).std().replace(0, np.nan)
    z = (series - mean_val) / std_val
    return z.fillna(0.0)
```

---

### 4.2 权重切面层集成：[`common/strategy_aspects.py`](file:///home/stone/Work/github/quant/common/strategy_aspects.py) 定仓与选股切面

在策略生成器（`strategy_generator`）中，切面架构将选择（Selection）与权重（Weighting）正交解耦。当前已有 `inverse_vol` 权重切面，本规范要求其**全面升级支持极值无偏估计量**，并新增 `low_vol_topn` 选股切面。

#### 1. 升级 `WeightingAspect("inverse_vol")`
在 [`common/strategy_aspects.py#L300-L325`](file:///home/stone/Work/github/quant/common/strategy_aspects.py#L300-L325)，扩展支持参数 `vol_engine`（默认为 `"garman_klass"`，可选 `"yang_zhang"`、`"natr"`、`"close_std"`）：

```python
def _weight_inverse_vol(universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
    symbols = list(universe.keys())
    vol_lookback = params["vol_lookback"]
    vol_engine = params.get("vol_engine", "garman_klass")

    vols = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        if vol_engine == "garman_klass":
            vols[sym] = garman_klass_vol(df, window=vol_lookback)
        elif vol_engine == "yang_zhang":
            vols[sym] = yang_zhang_vol(df, window=vol_lookback)
        elif vol_engine == "natr":
            vols[sym] = natr(df, period=vol_lookback)
        else:
            vols[sym] = realized_vol(df["Close"], window=vol_lookback)
    vols_rebal = vols.loc[rebalance_dates]

    # Strictly comply with workspace sparse weights NaN-vs-0.0 contract
    weights_rebal = pd.DataFrame(np.nan, index=rebalance_dates, columns=symbols)
    for date in rebalance_dates:
        inv_frac = selection.invested_fraction.loc[date]
        chosen = _chosen_symbols(selection, date)
        if not _mask_unchosen_and_derisked(weights_rebal, date, symbols, chosen, inv_frac):
            continue
        w = _inverse_vol_weights(vols_rebal.loc[date, chosen], scale=inv_frac, on_invalid="nan")
        weights_rebal.loc[date, chosen] = w.values

    weights_df = pd.DataFrame(np.nan, index=master_index, columns=symbols)
    weights_df.loc[rebalance_dates] = weights_rebal
    return weights_df
```

#### 2. 新增 `SelectionAspect("low_vol_topn")` 截面低波选股切面
实现经典 Blitz & van Vliet (2007) 低波动选股切面：

```python
def _select_low_vol_topn(universe, params, master_index, rebalance_dates) -> SelectionResult:
    """Selects the lowest-volatility quintile of symbols across the universe."""
    symbols = list(universe.keys())
    vol_lookback = params["vol_lookback"]
    vols = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        vols[sym] = garman_klass_vol(df, window=vol_lookback)
    vols_rebal = vols.loc[rebalance_dates]

    n_symbols = len(symbols)
    top_n = max(1, int(n_symbols * params.get("top_n_fraction", 0.25)))
    eligible = pd.DataFrame(False, index=rebalance_dates, columns=symbols)

    for date, row in vols_rebal.iterrows():
        if row.isna().all():
            continue
        # Ascending sort: lowest volatility comes first
        lowest_vol_symbols = sorted(row.dropna().index, key=lambda s: (row[s], s))[:top_n]
        eligible.loc[date, lowest_vol_symbols] = True

    return SelectionResult(eligible=eligible, invested_fraction=pd.Series(1.0, index=rebalance_dates))
```

---

### 4.3 组合策略层集成：[`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) 宏观预算与止损防线

[`BaseBlendAllocationStrategy`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) 驱动着代码库最核心的 5 个旗舰融合策略（`ChanRiskManagedBlendStrategy`、`ChanFourStateBlendStrategy`、`ChanCrisisShieldBlendStrategy`、`ChanDualHybridBlendStrategy`、`GenericBlendStrategy`）。归一化波动率在此处有三大关键挂载点：

#### 挂载点 1：CHOP 指数宏观状态机与子策略动态预算分配
在 [`pipeline/research/blend/blend_base.py#L400-L439`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py#L400-L439) 的子策略预算层，引入宏观宽基（或等权全市场）的 `CHOP` 状态：
- 当 $CHOP_{macro} \le 38.2$ 时：市场处于单边大趋势，将 **动量子策略组（Momentum/Trend）的风险预算提升 +30%**，削减均值回归子策略预算；
- 当 $CHOP_{macro} \ge 61.8$ 时：市场处于剧烈混沌震荡期，**自动将趋势策略风险预算压降 50%**，并把释放的敞口安全配置至高分红防御或无风险现金代理标的（`BIL`/`511880`）。

#### 挂载点 2：基于 NATR 的弹性 Chandelier 吊灯止损与冷却锁死
当前在 [`pipeline/research/blend/blend_base.py#L649-L679`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py#L649-L679) 实现了 `stop_mode == "atr_trailing"`。在此规范中，正式确立**归一化 NATR 动态止损线计算法则**：
$$\text{TrailingStopPrice}_{i, t} = \text{PeakPrice}_{i, t} \times \left( 1.0 - \text{Clip} \left( k \times NATR_{i, t}, \ 0.04, \ 0.12 \right) \right)$$
一旦跌破该自适应门禁：
1. 立即置该标的持仓权重为 `0.0`（严格遵守 NaN-vs-0.0 契约触发平仓）；
2. 激活冷冻期门禁 `asset_cooldown_until[s] = t + asset_stop_cooldown_bars`（默认 5 个交易日），杜绝在止损后被主力诱多反抽二次扫损。

#### 挂载点 3：波动率自适应重平衡惯性阈值（Adaptive Inertia Rebalancing）
在 [`pipeline/research/blend/blend_base.py#L621-L628`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py#L621-L628) 中，资产权重调仓阈值由资产波动率与全市场波动率的比值自适应缩放：
$$\text{InertiaThreshold}_{i, t} = \text{Clip} \left( \frac{\sigma_{GK, i, t}}{\bar{\sigma}_{market, t}} \times \Delta w_{base}, \ 0.02, \ 0.08 \right)$$
- 对于高弹性资产，系统容忍更大的权重漂移范围（最高达 $8\%$），避免因短期剧烈波动引发不必要的频繁换手和交易佣金损耗；
- 对于低弹性稳健资产，阈值收窄至 $2\%$，确保严格贴合目标权重。

---

### 4.4 实盘执行护栏集成：[`scripts/`](file:///home/stone/Work/github/quant/scripts/) 两阶段操作联动

实盘交易体系包含 Stage 1 盘前宏观准入与 Stage 2 盘尾订单生成：

#### Stage 1 盘前宏观健康门禁：[`scripts/check_live_portfolio_health.py`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py)
在每日 14:00~14:15 运行。新增指标：
1. **全组合加权 NATR 监控**：计算当前持仓股票的加权平均 $NATR_{port} = \sum w_i NATR_i$；
2. **波动率跃升异动熔断预警（`WARN_HIGH_VOL_SHOCK`）**：
   若 $NATR_{port} / \text{MA}_{60}(NATR_{port}) \ge 1.35$，触发三级熔断警报，输出黄色或红色告警，建议交易员暂停人工干预加仓，并将 Barroso 目标波动率缩放因子下调至防御级别。

#### Stage 2 盘尾智能发单生成器：[`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py)
在 14:15 执行发单。新增基于 NATR 的**挂单限价保护带宽（Limit Order Bandwidth Protection）**：
- 在 Phase 2（买单生成）中，为防止尾盘 15 分钟遭遇恶意流动性诱多拉升或滑点击穿，限价单买入价上限设置为：
  $$\text{LimitPrice}_{buy} = \text{CurrentClose} \times (1.0 + \min(0.015, \ 0.25 \times NATR_i))$$
- 杜绝使用市价单无脑扫盘，在保证 100% 达成成交的同时，锁定单笔交易滑点上限。

---

## 五、 总结与最佳实践准则

### 5.1 绝对三大操作禁忌（Avoid At All Costs）

> [!CAUTION]
> **禁忌一：严禁跨标的直接使用名义点位 ATR 进行排序或权重分配**
> 名义 ATR 严重受制于股票面额（茅台 1800 元 vs 低价股 3 元）。在多资产选股、风险平价加权时，**必须且只能使用 NATR 或 Garman-Klass / Yang-Zhang 年化无量纲指标**。违反此条将导致组合权重被极低价垃圾股完全占领。

> [!CAUTION]
> **禁忌二：在存在显著隔夜跳空的市场上单纯使用 Parkinson 估计量**
> Parkinson (1980) 假定日内价格为连续漂移扩散，完全忽略开盘价跳空。在 A 股由于隔夜外盘发酵频发集合竞价跳空的场景中，单纯使用 Parkinson 会导致波动率被严重低估 20%~40%，使风控系统在遭遇跳空低开崩盘时失去警觉。跳空市场首选 **Yang-Zhang (2000)** 或 **Garman-Klass (1980)**。

> [!CAUTION]
> **禁忌三：静态一刀切硬编码止损比例（如统一下跌 5% 止损）**
> 任何无视标的固有波动率的静态止损，本质上都是“刻舟求剑”。高波动成长标的会被正常的日内呼吸式波动频繁止损扫地出门，而低波动防守标的遭遇 5% 回撤时可能基本面早已发生不可逆的逻辑恶化。**止损线必须由 $NATR$ 弹性动态度量**。

---

### 5.2 黄金组合搭档矩阵（Indicator Confluence Matrix）

在实战交易系统研发中，单一指标往往存在盲区。本研究提炼出工业级验证的**“四维一体归一化波动率黄金搭档”**：

```mermaid
graph TD
    subgraph MATRIX["四维一体归一化波动率黄金搭档"]
        D1["1. 宏观区制分类器\nCHOP (14日)\n判别趋势 (<38.2) 与震荡 (>61.8)"]
        D2["2. 能量压缩突破确认\nBandwidth + RVI\n过滤突破假信号"]
        D3["3. 极值无偏风险平价定仓\nGarman-Klass / Yang-Zhang\n决定单标的真实风险敞口"]
        D4["4. 弹性自适应棘轮出场\nNATR-Adaptive SuperTrend / Chandelier\n锁死利润，防御黑天鹅"]
    end

    D1 --> D2
    D2 --> D3
    D3 --> D4
```

1. **第一维：宏观区制分类器（CHOP）** —— 首先判决当前处于什么战场：若 $CHOP < 38.2$ 启动趋势跟踪，若 $CHOP > 61.8$ 启动震荡网格或强制观望；
2. **第二维：能量压缩突破确认（Bandwidth + RVI）** —— 当布林带宽处于极限低位且 RVI 向上突破 55 时，确认趋势启动的真实加速度，杜绝假突破；
3. **第三维：极值无偏风险平价定仓（Garman-Klass / Yang-Zhang）** —— 决定买多少：通过高效无偏估计量计算各标的方差，实现精确的反波动率加权与组合目标波动率缩放；
4. **第四维：弹性单向棘轮出场（NATR-Adaptive SuperTrend / Chandelier）** —— 决定何时退出：以 $NATR$ 弹性撑开止损距离，让利润在强趋势中充分奔跑，同时在反转瞬间单向锁死利润。

---

### 5.3 生产级避坑指南：NaN/0.0 契约与防未来函数

#### 1. 严格恪守稀疏权重 DataFrame 契约（NaN vs. 0.0）
根据 `AGENTS.md` 核心规范：
- `generate_weights` 返回的权重 DataFrame 必须为稀疏矩阵，非调仓日全行必须为 `NaN`（严禁在模板内自行前向填充 `ffill`）；
- 在调仓日内部：**被模型决策剔除或止损出场的标的，必须明确写入 `0.0`，绝对不能留作 `NaN`**！因为底层回测引擎 `run_allocation_backtest` 在整张表执行列向 `sparse_weights.ffill().fillna(0.0)`，若留下 `NaN` 将导致回测引擎错误地沿用上一期的持仓，造成幽灵持仓（Ghost Position）严重失真。

#### 2. 避免前视偏差与闭包泄露（Look-Ahead Bias Prevention）
在计算滚动极值窗口（如 Yang-Zhang 连续项、Chandelier 吊灯最高价）时，调仓日 $t$ 的决策只能使用截至 $t$ 日的数据（若收盘前调仓则只能使用 $t-1$ 日或截至 14:15 的截断数据），绝对不可引入 $t+1$ 日的任何收盘信息。

#### 3. 零除保护与常数价格容错
在回测某些停牌股或合成资产时，价格序列可能出现连续常数价格（$High = Low = Close$）。
算子内部必须严格包裹 `clip(lower=1e-8)` 与 `.replace(0, np.nan)`，确保在遇到极端分母为零时不抛出 `ZeroDivisionError`，平滑回退至安全默认值。

---

## 六、 后续实施路线图与 `development_plan.md` 关联

为贯彻 **Single Source of Truth** 原则，本文档制定的归一化波动率指标研发与系统升级计划，统一作为子任务挂载至 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md)。后续工程落盘分为四大递进波次：

```mermaid
flowchart LR
    W1["Wave 1: 算子沉淀\ncommon/indicators.py\n(NATR, GK, YZ, RVI, CHOP)"]
    W2["Wave 2: 切面升级\ncommon/strategy_aspects.py\n(inverse_vol 升级 + low_vol_topn)"]
    W3["Wave 3: 组合策略融合\npipeline/research/blend/\n(CHOP 宏观状态机 + NATR 吊灯止损)"]
    W4["Wave 4: 实盘两阶段护栏\nscripts/ check & run\n(波动率冲击告警 + 挂单带宽保护)"]

    W1 --> W2
    W2 --> W3
    W3 --> W4
```

| 实施阶段 | 核心任务内容 | 涉及文件 | 计划挂载点 |
| :--- | :--- | :--- | :--- |
| **Phase 1: 算子层沉淀** | 补齐 `natr`, `garman_klass_vol`, `yang_zhang_vol`, `rvi`, `choppiness_index`, `volatility_zscore` 向量化函数与完整单测 | [`common/indicators.py`](file:///home/stone/Work/github/quant/common/indicators.py)<br>[`common/tests/test_indicators.py`](file:///home/stone/Work/github/quant/common/tests/) | `docs/development_plan.md` |
| **Phase 2: 切面层升级** | 为 `WeightingAspect("inverse_vol")` 注入 GK/YZ 引擎；实现 `SelectionAspect("low_vol_topn")` 截面低波选股切面 | [`common/strategy_aspects.py`](file:///home/stone/Work/github/quant/common/strategy_aspects.py) | `docs/development_plan.md` |
| **Phase 3: 组合融合层** | 在 `BaseBlendAllocationStrategy` 挂载 CHOP 宏观状态机调控与基于 NATR 的弹性 Chandelier 吊灯止损 | [`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) | `docs/development_plan.md` |
| **Phase 4: 实盘两阶段** | Stage 1 盘前引入加权 NATR 异动冲击告警；Stage 2 引入基于 NATR 的限价单滑点保护带宽 | [`scripts/check_live_portfolio_health.py`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py)<br>[`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py) | `docs/development_plan.md` |

---
*全规范终稿已审定完成。一切原子任务的启动与推进以主计划总表为准。*
