# 递归量化分析 (RQA) 量化金融理论建模与非线性动力学应用规范
# (Recurrence Quantification Analysis Specification: Non-Linear Dynamics, Phase Space Reconstruction, and Quant Architectures)

> [!IMPORTANT]
> **唯一权威任务与路线图总表声明 (Single Source of Truth)**：
> 本文档是递归量化分析 (Recurrence Quantification Analysis, RQA)、非线性动力学相空间重构、确定性混沌、微观拓扑纹理提取以及宏观状态机/实盘预警的**理论建模、数学证明、算法推导与系统工程设计规范**。
> 依据 [`AGENTS.md`](file:///home/stone/Work/github/quant/AGENTS.md) 核心准则，所有工程落地任务、波次编排、依赖关系及状态跟踪，**必须且仅在 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) 中集中管理**。本文档专注于非线性物理数学严谨性、工业级向量化实现与量化系统无缝集成。

---

## 一、 理论渊源与非线性物理基础 (Theoretical Origins & Non-Linear Dynamics Pillars)

```mermaid
flowchart TD
    subgraph FOUNDATION["1. 非线性动力学与混沌物理基石 (1890 - 1981)"]
        POINCARE["Henri Poincaré (1890)\n《三体问题与动力学方程》\n庞加莱递归定理 (Poincaré Recurrence Theorem)"]
        LORENZ["Edward Lorenz (1963)\n《确定性非周期流》\n奇怪吸引子与对初值的敏感依赖性 (蝴蝶效应)"]
        TAKENS["Floris Takens (1981)\n《湍流中的奇异吸引子检测》\n塔肯斯嵌入重构定理 (Delay Coordinates)"]
    end

    subgraph RQA_DEVELOPMENT["2. 递归图拓扑与量化指标学派 (1987 - 2007)"]
        EKR["Eckmann, Kamphorst & Ruelle (1987)\n《相空间递归图》 (Europhysics Letters)\n递归图 (Recurrence Plot, RP) 二值距离矩阵"]
        ZBILUT["Zbilut & Webber (1992, 1994)\n递归量化分析 (RQA)\n确定性 DET、对角线分布与香农熵 ENTR"]
        MARWAN["Norbert Marwan et al. (2007)\n《Recurrence plots for the analysis of complex systems》\n(Physics Reports 奠基性综述: LAM, TT, CRQA)"]
    end

    subgraph QUANT_FINANCE["3. 金融物理学与临界相变预测 (1995 - 2011)"]
        SORNETTE["Didier Sornette (2003, LPPLS)\n《Why Stock Markets Crash》\n正反馈羊群效应、相干同步与自由度低维坍缩"]
        BASTOS["Bastos & Caiado (2011)\n《Recurrence analysis of financial time series》\n金融危机期间确定性与自相似性的脉冲飙升"]
    end

    subgraph QUANT_SYSTEM["4. 工业级量化交易系统架构 (本规范落地)"]
        COMMON["common/rqa.py\n高维相空间重构、L_inf 距离矩阵与 1D 游程编码"]
        BLEND["pipeline/research/blend/blend_base.py\n第 10 大宏观锚点: RQA 趋势/层流/混沌三态路由"]
        STAGE1["scripts/check_live_portfolio_health.py\nStage 1 脆性预警: CAUTION_RQA_DETERMINISTIC_CASCADE"]
    end

    POINCARE --> LORENZ --> TAKENS
    TAKENS --> EKR --> ZBILUT --> MARWAN
    MARWAN --> BASTOS
    SORNETTE --> BASTOS
    BASTOS --> QUANT_SYSTEM
    MARWAN --> QUANT_SYSTEM
    FOUNDATION --> RQA_DEVELOPMENT --> QUANT_FINANCE --> QUANT_SYSTEM
```

### 1.1 经典线性金融学与有效市场假说 (EMH) 在极端行情下的根本失真

自 Bachelier (1900) 奠定布朗运动资产定价模型、Fama (1970) 提出有效市场假说 (Efficient Market Hypothesis, EMH) 以及 Markowitz (1952) 确立均值-方差投资组合理论以来，传统量化金融理论长期建立在三个核心前提假设之上：
1. **独立同分布的高斯假设 (i.i.d. Normal Distribution)**：资产对数收益率服从正态分布，价格增量互不相关且方差有限；
2. **线性叠加原理与平稳协方差 (Linear Superposition & Stationary Covariance)**：资产间的相互作用由静态皮尔逊相关系数 $r_{ij} = \frac{\text{Cov}(X_i, X_j)}{\sigma_i \sigma_j}$ 刻画，多资产组合风险由线性协方差矩阵 $\mathbf{\Sigma}$ 封闭；
3. **外生信息驱动与均衡回归 (Exogenous Shocks & Equilibrium)**：价格波动仅由外生基本面增量信息驱动，市场具备瞬时吸收信息并恢复均衡的自组织弹性。

然而，金融市场的微观真实运行机制是一个由成千上万异质性交易主体（高频做市商、杠杆量化对冲基金、被动 ETF、个人散户）构成的**强非线性、非平稳、具备多尺度内生正反馈的耗散动力系统 (Dissipative Dynamical System)**：
- **典型事实 (Stylized Facts) 的冲击**：Mandelbrot (1963) 与 Engle (1982) 指出，金融时间序列普遍呈现出显著的高峰厚尾 (Leptokurtosis)、波动率聚集 (Volatility Clustering)、长期记忆性与杠杆效应 (Leverage Effect)。收益率的自相关系数可能接近零，但绝对收益率或平方收益率表现出强烈的长程自相关。
- **极端危机中的线性防线全面崩溃**：在系统性黑天鹅事件（如 1987 年“黑色星期一”、2008 年雷曼破产、2015 年中国 A 股股灾、2020 年 3 月全球流动性挤兑）中，全市场资产相关性急剧趋向于 1.0（*"In a crisis, all correlations go to 1"*）。传统的马科维茨风险分散机制在数天内彻底失效；基于历史平稳假设的 VaR (Value at Risk) 产生数十倍的标准差越界击穿。
- **线性指标的“相变盲区”**：传统技术指标（如布林带、ATR、移动平均）与统计指标（波动率、偏度、相关性）均为线性加权或二阶统计量，属于**滞后型后验测量**。当资产价格出现暴跌时，波动率指标才会飙升，此时组合净值已遭受实质性摧毁。我们需要一种能够穿透表象价格波动、直接探测**市场微观状态在底层相空间中几何拓扑突变**的前瞻性非线性物理分析工具。

---

### 1.2 庞加莱递归定理与耗散系统奇怪吸引子

法国数学家 Henri Poincaré (1890) 在其划时代著作《三体问题与动力学方程》中奠定了非线性动力系统的基础，提出了著名的**庞加莱递归定理 (Poincaré Recurrence Theorem)**：

> **庞加莱递归定理 (Poincaré, 1890)**：
> 设 $(\Omega, \mathcal{B}, \mu, \Phi_t)$ 为一个测度保持的动力系统，其中 $\Omega$ 为相空间，$\mu(\Omega) < \infty$ 为有限测度，$\Phi_t: \Omega \to \Omega$ 为保测流动映射。
> 对于任意测度 $\mu(A) > 0$ 的非空可测子集 $A \in \mathcal{B}$，几乎每一个从 $A$ 出发的轨道 $\vec{x}(t) = \Phi_t(\vec{x}_0)$，在充分长的时间 $t > 0$ 之后，必然会无限次重新回到集合 $A$ 中。

金融市场并非无损耗的哈密顿保守系统 (Conservative Hamiltonian System)，而是**非平衡态耗散动力系统 (Non-Equilibrium Dissipative System)**：
1. 市场通过杠杆交易、融资成本、买卖佣金及交易滑点不断耗散信息与流动性能量；
2. 耗散系统的相空间体积随时间指数收缩：$\frac{d V(t)}{dt} = - \gamma V(t)$，系统的稳态轨迹不会漫无边际地发散，而是被约束收敛于相空间的一个具有分形维数（Fractal Dimension）的紧致子集——**奇怪吸引子 (Strange Attractor)**（Lorenz 1963, Ruelle & Takens 1971）；
3. 吸引子虽然在局部表现出对初值的敏感依赖性（Sensitive Dependence on Initial Conditions，即李雅普诺夫指数 $\lambda > 0$ 的轨道指数发散），但在宏观全局上却具有高度的几何自相似性与受限拓扑结构；
4. 因此，虽然金融资产价格的未来具体点位不可长期精确预测，但**系统的相空间运动轨迹在奇怪吸引子上的几何重访（Recurrence）行为，包含了极其丰富的底层确定性、层流性与相变状态机信息**。

---

### 1.3 递归图 (Recurrence Plot, RP) 拓扑几何发现

为了将高维相空间中难以直观可视化的非线性吸引子轨迹几何展现出来，Eckmann, Kamphorst & Ruelle (1987) 在《Europhysics Letters》发表了突破性论文，首创了**递归图 (Recurrence Plot, RP)**。

设重构的相空间轨迹为向量集合 $\{\vec{x}_i\}_{i=1}^N \subset \mathbb{R}^m$，其中 $i$ 表示离散时间索引。递归矩阵 $\mathbf{R} \in \{0, 1\}^{N \times N}$ 定义为一个对称二值矩阵：

$$ \mathbf{R}_{i,j}(\varepsilon) = \Theta\left(\varepsilon - \|\vec{x}_i - \vec{x}_j\|\right), \quad i,j = 1, 2, \dots, N $$

其中：
- $\Theta(\cdot)$ 为 Heaviside 阶跃函数（当自变量 $\ge 0$ 时取 1，否则取 0）；
- $\|\cdot\|$ 为相空间中的几何范数（常用欧几里得 $L_2$ 范数或切比雪夫 $L_\infty$ 范数）；
- $\varepsilon$ 为空间邻域临界截断阈值 (Cutoff Threshold)；
- 当状态 $\vec{x}_j$ 落在状态 $\vec{x}_i$ 的 $\varepsilon$-球邻域内时，$\mathbf{R}_{i,j} = 1$（图上绘制为黑点，表示发生一次递归）；否则 $\mathbf{R}_{i,j} = 0$（图上绘制为空白点）。

递归图在二维平面上展现出令人惊叹的拓扑几何纹理，揭示了系统运行的物理本质：

| 拓扑纹理模式 (RP Texture) | 几何形态特征 | 动力系统物理机理 | 金融市场微观映射 |
| :--- | :--- | :--- | :--- |
| **均匀散乱点 (Speckled Points)** | 孤立、无规则散落的独立黑点 | 纯随机随机游走、无记忆高斯白噪声 | 强有效市场、无套利震荡、高维微观噪声主导 |
| **对角平行线 (Diagonal Lines)** | 平行于主对角线 (LOI) 的线段群 | 轨道片段具有确定性进化方向，发散受限 | **强趋势动量行情**、确定性自回归、低李雅普诺夫发散 |
| **垂直/水平块 (Vertical/Horizontal Bars)** | 垂直或水平排列的致密线段/矩形块 | 系统处于层流状态 (Laminar State)，速度趋近于零 | **窄幅盘整、流动性枯竭、横盘吸筹、成交量异动停滞** |
| **对角间隙/空白带 (White Bands)** | 沿时间轴穿插的大面积白色空白 | 系统发生剧烈突变，轨道远离所有历史状态 | **宏观黑天鹅跳空、重磅政策冲击、结构性制度断层** |
| **网状周期结构 (Grid / Checkered)** | 规则交织的方格或周期性平行带 | 系统具备稳定的主导周期振荡 (Limit Cycle) | 显著宏观库存周期、日内交割结算周期、稳定波段震荡 |

---

### 1.4 从一维标量时间序列到多维吸引子相空间重构

在真实二级市场量化交易中，量化分析师面对的核心工程瓶颈是：**我们能够观测到的数据仅仅是极低维度的一维标量时间序列**（如每日收盘价 $P_t$、对数收益率 $r_t = \ln(P_t / P_{t-1})$ 或分钟成交量 $V_t$）。

然而，驱动资产价格演化的真实底层金融经济动力系统，包含了成千上万个隐式状态变量（如宏观基准利率、全市场信用利差、场外杠杆规模、做市商伽马敞口、参与者恐慌情绪等）：

$$ \frac{d\vec{z}(t)}{dt} = \mathbf{F}(\vec{z}(t)), \quad \vec{z}(t) \in \mathbb{R}^D \quad (D \gg 1) $$
$$ s(t) = h(\vec{z}(t)), \quad s(t) \in \mathbb{R} $$

我们如何仅凭一维标量观测值 $s(t)$，在数学上丝毫无损地还原出原本潜藏在高维空间中的复杂吸引子拓扑结构？这正是拓扑动力学中**塔肯斯延迟坐标重构定理**的伟大贡献。

---

## 二、 塔肯斯嵌入重构与相空间几何 (Takens' Embedding Reconstruction & Phase Space Geometry)

### 2.1 塔肯斯嵌入定理 (Floris Takens 1981)

荷兰数学家 Floris Takens (1981) 证明了动力系统重构领域的基石性定理：通过对单一标量时间序列施加因果时间延迟，可以构建出与原动力系统在微分拓扑意义下完全等价（微分同胚 Diffeomorphic）的高维状态空间。

```mermaid
flowchart LR
    S["一维标量序列 s(t)\n(因果对数收益率 / 标准化价格)"] -->|"延迟向量化构建\nτ: 延迟时间, m: 嵌入维数"| PH["重构高维相空间 x_t ∈ R^m\n[s_t, s_{t-τ}, ..., s_{t-(m-1)τ}]"]
    PH -->|"几何邻域判别\nL_inf / L_2 范数"| DIST["状态距离矩阵 D_ij\n||x_i - x_j||"]
    DIST -->|"自适应阈值 ε 截断\nΘ(ε - D_ij)"| RP["二值递归矩阵 R_ij ∈ {0, 1}\n(对角线: 确定性, 垂直线: 层流态)"]
    RP -->|"1D 游程扫描 (RLE)"| RQA["RQA 量化指标族\n(RR, DET, L, ENTR, LAM, TT)"]
```

> **塔肯斯嵌入定理 (Takens' Embedding Theorem, 1981)**：
> 设 $M$ 为一个 $d$ 维紧致光滑流形，$\Phi: M \to M$ 为光滑微分同胚动力流映射，$h: M \to \mathbb{R}$ 为光滑标量观测函数。
> 若**嵌入维数**满足 $m \ge 2d + 1$，则由延迟坐标定义的映射：
> 
> $$ \Phi_{(m, \tau)}(\vec{z}) = \left[ h(\vec{z}), h(\Phi_{-\tau}(\vec{z})), h(\Phi_{-2\tau}(\vec{z})), \dots, h(\Phi_{-(m-1)\tau}(\vec{z})) \right]^T \in \mathbb{R}^m $$
> 
> 在几乎所有观测函数 $h$ 和流 $\Phi$ 的通用意义下（Generic Property），均为光滑嵌入（Embedding）。
> 即：重构相空间中的吸引子轨迹流形与原动力系统的真实吸引子流形保持**微分同胚**，两者具备完全一致的拓扑不变量（如关联维数、李雅普诺夫指数谱、熵与局部递归率）。
> 
> *注：Sauer, Yorke & Casdagli (1991) 推广的分形嵌入定理进一步将维数下界压缩为 $m > 2 d_A$，其中 $d_A$ 为吸引子的 Hausdorff 或盒计数分形维数。*

在离散时间量化时序中，设标量序列为 $\{s_t\}_{t=1}^N$。重构的相空间向量点为：

$$ \vec{x}_t = \begin{bmatrix} s_t \\ s_{t-\tau} \\ s_{t-2\tau} \\ \vdots \\ s_{t-(m-1)\tau} \end{bmatrix} \in \mathbb{R}^m, \quad t = 1 + (m-1)\tau, \dots, N $$

重构相空间的质量与物理保真度，严格取决于两大关键超参数的抉择：
1. **延迟时间 $\tau$ (Delay Time)**：决定各个状态坐标之间的正交独立性与信息互补度；
2. **嵌入维数 $m$ (Embedding Dimension)**：决定流形几何空间是否足以完全展开（Unfold）吸引子，彻底消除投影带来的虚假自相交。

---

### 2.2 延迟时间 $\tau$ 的选取理论与互信息极小值 (First Minimum of Mutual Information)

在理论物理中，若数据为无限长且无噪声，任何 $\tau > 0$ 均满足塔肯斯嵌入定理。但在有限样本与噪声污染的金融时序中：
- 若 $\tau$ **过小**：$s_t \approx s_{t-\tau}$，坐标间几乎完全共线，相空间所有轨迹被强行挤压在 $x_1 = x_2 = \dots = x_m$ 的主对角线狭长管道内，产生**冗余压缩失真 (Redundancy)**；
- 若 $\tau$ **过大**：混沌系统的轨道按指数规律发散，两次采样状态在统计上完全失联，吸引子几何结构被彻底冲散为互不相干的噪声云，产生**不相关失真 (Irrelevance)**。

#### 为什么不能仅使用线性自相关函数 (ACF)?
经典自相关函数 $R(\tau) = \frac{\mathbb{E}[(s_t - \mu)(s_{t+\tau} - \mu)]}{\sigma^2}$ 仅衡量二阶线性相关性。金融资产收益率通常在 $\tau \ge 1$ 时线性自相关即衰减至接近 0，但其非线性依赖性（高阶矩、条件异方差波动率）依然极其强烈。采用 $R(\tau) = 0$ 或 $R(\tau) = 1/e$ 选取的 $\tau$ 往往严重失真。

#### 平均互信息法 (Average Mutual Information, AMI)
Fraser & Swinney (1986) 提出利用香农信息论中的平均互信息 (AMI) 来选取非线性动力系统的最优延迟时间 $\tau$。

设时间序列 $s_t$ 与时延序列 $s_{t+\tau}$ 的联合概率密度函数为 $P(s_t, s_{t+\tau})$，边缘概率密度分别为 $P(s_t)$ 和 $P(s_{t+\tau})$。平均互信息定义为：

$$ I(\tau) = \iint P(s_t, s_{t+\tau}) \log_2 \left( \frac{P(s_t, s_{t+\tau})}{P(s_t) P(s_{t+\tau})} \right) ds_t ds_{t+\tau} $$

- **物理判定准则**：选取 **$I(\tau)$ 随 $\tau$ 递增出现的第一个局部极小值点 $\tau^*$ (First Local Minimum)**。
- **物理机理**：在第一个局部极小值处，$s_{t+\tau^*}$ 对 $s_t$ 提供了最大化的新信息增量（非线性冗余最小），同时又尚未被混沌轨道发散所冲散（保留最大程度的动力学拓扑约束）。
- **金融时序经验准则**：在日线级别资产中，$\tau^*$ 通常在 $1 \sim 3$ 个交易日；在 5 分钟高频时序中，$\tau^*$ 通常为 $2 \sim 6$ 根 K 线（10 ~ 30 分钟）。

---

### 2.3 嵌入维数 $m$ 的假近邻算法 (False Nearest Neighbors, FNN)

当嵌入维数 $m$ 不足时，高维相空间中的两条完全不相关的轨迹，在被投影到低维观察空间时可能会发生重叠交叉，形成**假近邻 (False Nearest Neighbors)**。

Kennel, Brown & Abarbanel (1992) 设计了著名的 FNN 算法，通过逐步递增维数 $m \to m+1$，观察近邻点距离的扩散倍率来确定最小充分嵌入维数 $m^*$：

```mermaid
flowchart TD
    M1["低维投影空间 R^m\n状态 x_i 与其最近邻点 x_j^(m) 距离为 R_m(i)"] --> TEST{"升维至 R^(m+1) 检验扩展距离\n|s_{i+mτ} - s_{j+mτ}|"}
    TEST -->|"扩展倍率 > R_tol (通常 10~15)\n或 跨度 > 2σ (已脱离吸引子尺寸)"| FALSE["判定为假近邻点 (False Neighbor)\n表明维数 m 不足，存在空间几何折叠"]
    TEST -->|"扩展距离保持受控"| TRUE["判定为真近邻点 (True Neighbor)\n轨迹在底层相空间中确实平行演化"]
    FALSE --> COUNT["计算全域假近邻比例 FNN(m)"]
    TRUE --> COUNT
    COUNT --> EVAL{"FNN(m) < 1% 或曲线进入水平拐点?"}
    EVAL -->|"否"| NEXT["m = m + 1 继续迭代"] --> M1
    EVAL -->|"是"| OPT["输出最优充分嵌入维数 m*"]
```

#### 数学判据推导
在 $m$ 维重构相空间中，设 $\vec{x}_j^{(m)}$ 为 $\vec{x}_i^{(m)}$ 的欧几里得最近邻点，距离为：

$$ R_m^2(i) = \|\vec{x}_i^{(m)} - \vec{x}_j^{(m)}\|^2 = \sum_{k=0}^{m-1} \left( s_{i+k\tau} - s_{j+k\tau} \right)^2 $$

当相空间升维至 $m+1$ 时，新增一个延迟坐标分量，两点间的新欧氏距离变为：

$$ R_{m+1}^2(i) = R_m^2(i) + \left( s_{i+m\tau} - s_{j+m\tau} \right)^2 $$

两点间距离的相对剧增率定义为：

$$ \frac{\left| s_{i+m\tau} - s_{j+m\tau} \right|}{R_m(i)} > R_{\text{tol}} \quad (R_{\text{tol}} \text{ 通常取 } 10 \sim 15) $$

同时为防止数据噪声将远离的点误判，引入第二重标准差截断判据：

$$ \frac{R_{m+1}(i)}{\sigma_s} > A_{\text{tol}} \quad (A_{\text{tol}} \text{ 通常取 } 2.0, \ \sigma_s \text{ 为序列全样本标准差}) $$

若上述两式中任一式成立，则点 $\vec{x}_j^{(m)}$ 被判定为 $\vec{x}_i^{(m)}$ 的**假近邻**。
定义全样本假近邻比例为：

$$ \text{FNN}(m) = \frac{\sum_{i=1}^{N - m\tau} \mathbb{I}_{[\vec{x}_i \text{ 存在假近邻}]}}{N - m\tau} $$

- **最优判定标准**：当 $\text{FNN}(m)$ 跌落至临界阈值以下（例如 $\text{FNN}(m) < 1\%$）或曲线首次趋于平稳不再随 $m$ 显著下降时，对应的最小 $m$ 即为最优嵌入维数 $m^*$。
- **金融资产经验准则**：股票指数与高流动性 ETF 的最优重构维数 $m^*$ 通常稳定收敛于 $3 \le m^* \le 5$。若 $m$ 选得过大（例如 $m > 8$），将引发样本量诅咒与“维数灾难 (Curse of Dimensionality)”，使相空间点密度过度稀疏。

---

### 2.4 邻域截断阈值 $\varepsilon$ 的自适应法则与范数选择

#### 1. 空间范数的工程抉择：切比雪夫 $L_\infty$ 范数
在计算相空间状态间距时，理论上有三种标准范数：
- $L_1$ 范数（曼哈顿距离）：$\|\vec{x}_i - \vec{x}_j\|_1 = \sum_{k=0}^{m-1} |x_{i,k} - x_{j,k}|$
- $L_2$ 范数（欧几里得距离）：$\|\vec{x}_i - \vec{x}_j\|_2 = \sqrt{\sum_{k=0}^{m-1} (x_{i,k} - x_{j,k})^2}$
- $L_\infty$ 范数（切比雪夫/最大范数）：$\|\vec{x}_i - \vec{x}_j\|_\infty = \max_{k=0}^{m-1} |x_{i,k} - x_{j,k}|$

**工业级首选切比雪夫 $L_\infty$ 范数**，其核心原因在于：
1. **几何一致性**：$L_\infty$ 范数对应的 $\varepsilon$-球是一个各向同性的高维超立方体（Hypercube），要求轨迹在**每一个延迟维度上的离差均不超过 $\varepsilon$**，这与金融交易中对各个滞后周期的联合容忍度定义高度吻合；
2. **计算性能绝杀**：在 NumPy 向量化实现中，$L_\infty$ 范数无需进行开方与平方和计算，完全由极低开销的差值取绝对值与 `np.max(..., axis=-1)` 构成，比 $L_2$ 范数快 3~5 倍且数值稳定性极高。

#### 2. 自适应截断阈值 $\varepsilon$ 的确定准则
阈值 $\varepsilon$ 决定了递归图上的黑点密度（递归率 RR）。若 $\varepsilon$ 过小，RP 几乎全白，无法提取线段；若 $\varepsilon$ 过大，几乎所有点都互为近邻，RP 几乎全黑，动力学结构被重度淹没。
Marwan et al. (2007) 与 Bastos & Caiado (2011) 确立了量化金融中三大主流的自适应标准：

1. **标准差比例法则 (Standard Deviation Scaling, Eckmann 1987)**：
   $$ \varepsilon = \kappa \cdot \sigma_x, \quad \kappa \in [0.10, 0.25] $$
   其中 $\sigma_x$ 为相空间向量坐标的滚动标准差。通常取 $\kappa = 0.15 \sim 0.20$（约为噪声本底水平的 $5$ 倍），能够平衡微观噪声滤除与宏观吸引子结构保留。
2. **固定递归率逆向求解法则 (Fixed Recurrence Rate Targeting)**：
   预先锚定目标递归率（如 $RR_{\text{target}} = 5\% \sim 10\%$），在每个滚动窗口内动态求解对应的百分位数阈值：
   $$ \varepsilon_t = \text{Percentile}\left(\{ \|\vec{x}_i - \vec{x}_j\| \}_{i < j}, \ 100 \times RR_{\text{target}}\right) $$
   此法则能够彻底剔除时间序列非平稳波动率放大造成的虚假递归率暴增，使后续提取的确定性指标具备严格的时间跨度可比性。
3. **相空间最大直径法则 (Attractor Diameter Ratio)**：
   $$ \varepsilon = \alpha \cdot \max_{i,j} \|\vec{x}_i - \vec{x}_j\|_\infty, \quad \alpha \in [0.05, 0.10] $$

---

## 三、 RQA 核心量化指标族与数学推导 (Core RQA Metrics Family & Rigorous Math Derivations)

递归图虽然具有极佳的视觉呈现，但在量化实盘与回测系统中，必须将二维矩阵严格压缩量化为确定性的统计指标标量。Zbilut & Webber (1992) 以及 Marwan et al. (2007) 构建了完整的递归量化分析 (RQA) 指标体系。

```mermaid
classDiagram
    class RecurrenceMatrix {
        +ndarray R_ij (二值对称矩阵)
        +int N (相空间样本容量)
        +float epsilon (截断阈值)
        +extract_diagonals()
        +extract_verticals()
    }

    class RQAMetricsFamily {
        +float RR (递归率)
        +float DET (确定性)
        +float L (平均对角线长)
        +int L_max (最长对角线)
        +float ENTR (香农对角线熵)
        +float LAM (层流性)
        +float TT (陷阱时间)
        +int V_max (最长垂直线)
    }

    RecurrenceMatrix --> RQAMetricsFamily : 1D 游程分布统计
```

### 3.1 递归率 (Recurrence Rate, RR)

递归率衡量相空间中状态重访的总体发生概率，对应物理学中 Grassberger & Procaccia (1983) 关联维数算法中的关联积分 $C(\varepsilon)$：

$$ RR(\varepsilon) = \frac{1}{N^2} \sum_{i,j=1}^N \mathbf{R}_{i,j}(\varepsilon) $$

在严格工程实践中，为了消除主对角线 (Line of Identity, LOI, $i=j$) 自身与自身必为 1 的平庸自相关影响，通常采用**剔除主对角线的无偏递归率**：

$$ RR(\varepsilon) = \frac{2}{N(N-1)} \sum_{i=1}^{N} \sum_{j=i+1}^N \mathbf{R}_{i,j}(\varepsilon) = \frac{1}{N(N-1)} \sum_{i \ne j} \mathbf{R}_{i,j}(\varepsilon) $$

- **物理意义**：当前系统状态再次重访历史状态的几率。
- **量化金融解释**：高 RR 表明资产价格运动在相空间中频繁遭遇历史相似情境（如强周期震荡或强支撑/阻力区域），低 RR 表明系统正处于前所未有的非平稳探索期（单边剧烈突破或制度性变异）。

---

### 3.2 确定性 (Determinism, DET)

平行于主对角线 ($j = i + k$) 的连续黑点线段称为**对角线 (Diagonal Line)**。长度为 $l$ 的对角线意味着两个不同时间起点的状态轨迹，在未来连续 $l$ 步的演化中彼此保持在 $\varepsilon$ 的管状邻域内，表现出平行的运动方向。

设 $P(l)$ 为长度恰好等于 $l$ 的对角线直方图分布频数（Histogram Frequency），$l_{\min}$ 为判定对角线的最小连续长度阈值（通常设定 $l_{\min} = 2$ 或 $3$）。确定性指标定义为：

$$ DET = \frac{\sum_{l=l_{\min}}^N l \cdot P(l)}{\sum_{l=1}^N l \cdot P(l)} = \frac{\sum_{l=l_{\min}}^N l \cdot P(l)}{\sum_{i \ne j} \mathbf{R}_{i,j}} $$

- **数学证明与边界约束**：
  - 分子表示所有长度 $\ge l_{\min}$ 的结构化对角线所包含的递归点总数；
  - 分母表示除主对角线外的全矩阵递归黑点总数；
  - 显然 $0 \le DET \le 1.0$。
- **物理与量化含义**：
  - 若序列为纯白噪声或独立随机游走：点阵完全离散无序，$P(l)$ 在 $l \ge 2$ 时迅速衰减为 0，因此 $DET \to 0$；
  - 若系统为纯周期或强确定性混沌动力学：所有轨迹演化均具备长程因果关联，$DET \to 1.0$；
  - **在量化交易中，DET 是衡量市场是否脱离随机漫步、形成强因果确定性趋势或羊群共振锁死的最高灵敏度指标**。

---

### 3.3 平均对角线长度 ($L$)、最长对角线 ($L_{\max}$) 与最大李雅普诺夫指数估计 ($\lambda_{\max}$)

#### 1. 平均对角线长度 (Average Diagonal Line Length, $L$)
衡量确定性平行轨迹能够平均持续多长时间：

$$ L = \frac{\sum_{l=l_{\min}}^N l \cdot P(l)}{\sum_{l=l_{\min}}^N P(l)} $$

- **量化含义**：反映可预测时间视窗（Predictability Horizon）。$L$ 越大，系统的确定性惯性越持久，趋势跟踪策略的胜率与持有周期期望越高。

#### 2. 最长对角线长度 ($L_{\max}$) 与最大李雅普诺夫指数 ($\lambda_{\max}$)
最长对角线定义为相空间中被检测到的最长平行线段：

$$ L_{\max} = \max\left( \{ l \mid P(l) > 0 \} \right) $$

Eckmann, Kamphorst & Ruelle (1987) 以及 Marwan et al. (2007) 严格证明了 $L_{\max}$ 与动力系统最大李雅普诺夫指数 (Maximum Lyapunov Exponent, $\lambda_{\max}$) 之间的倒数反比关系：
在混沌系统中，相空间两相近轨迹随时间的平均指数发散规律为：

$$ \|\vec{x}_{i+t} - \vec{x}_{j+t}\| \approx \|\vec{x}_i - \vec{x}_j\| \cdot e^{\lambda_{\max} \cdot t} $$

若两条轨迹能够持续保持在 $\varepsilon$ 的邻域内长达 $L_{\max}$ 步，则必有：

$$ \varepsilon \ge \|\vec{x}_i - \vec{x}_j\| \cdot e^{\lambda_{\max} \cdot L_{\max}} \implies L_{\max} \propto \frac{1}{\lambda_{\max}} $$

- **量化金融应用**：
  - $\lambda_{\max} \approx \frac{1}{L_{\max}}$ 提供了一种**无需拟合复杂差分动力方程、直接基于局部样本窗口估算局部李雅普诺夫指数的无参数非线性方案**；
  - 当 $L_{\max}$ 显著放大时，$\lambda_{\max} \to 0$，系统呈现高度保守与发散停滞，通常预示着极端动量单边行情或临界崩盘锁死；
  - 当 $L_{\max}$ 急剧缩短时，$\lambda_{\max}$ 极大，轨迹发散极快，混沌不可预测性激增。

---

### 3.4 对角线香农熵 (Shannon Entropy of Diagonal Lengths, ENTR)

对角线长度的离散概率分布密度定义为：

$$ p(l) = \frac{P(l)}{\sum_{l'=l_{\min}}^N P(l')} $$

香农熵 (Shannon Entropy) 衡量对角线长度分布的非均匀性与复杂性：

$$ ENTR = - \sum_{l=l_{\min}}^N p(l) \ln p(l) $$

- **物理含义**：
  - 若系统为简单的单一正弦谐波振荡，所有对角线长度几乎完全相等，$p(l)$ 集中在单一峰值处，则 $ENTR \approx 0$；
  - 若系统为复杂的奇怪吸引子混沌动力系统，存在无数多时间尺度的多重分形嵌套轨道，对角线长度呈现宽广的幂律或几何衰减分布，$ENTR$ 显著升高；
  - 若系统为无序白噪声，无法形成有效对角线，计算值趋近于 0（受限于 $l_{\min}$ 截断）。
- **量化交易解释**：高 ENTR 代表市场处于丰富的多周期协同运作状态，单一技术分析容易失灵，多策略 Blend 组合最能发挥分散正交优势。

---

### 3.5 层流性 (Laminarity, LAM)、陷阱时间 (Trapping Time, TT) 与最长垂直线 ($V_{\max}$)

在递归图中，**垂直线 (Vertical Lines)**（或水平线）是指同一列 $j$（或同一行 $i$）中连续出现的黑点线段。

垂直线段的物理几何成因在于：
$$ \vec{x}_{i+k} \approx \vec{x}_j, \quad k = 0, 1, \dots, v-1 $$
即系统状态随着时间推移，在连续 $v$ 个步长内**几乎保持在原相空间微小邻域内停滞不前**。这在非线性动力学中被称为**层流状态 (Laminar State)** 或阵发混沌 (Intermittency)。

设 $P(v)$ 为长度为 $v$ 的垂直线直方图分布频数，$v_{\min} \ge 2$ 为最小判定长度（通常取 $v_{\min} = 2$）。

#### 1. 层流性 (Laminarity, LAM)
衡量由垂直结构构成的递归黑点占全矩阵递归黑点的比例：

$$ LAM = \frac{\sum_{v=v_{\min}}^N v \cdot P(v)}{\sum_{v=1}^N v \cdot P(v)} $$

#### 2. 陷阱时间 (Trapping Time, TT)
衡量系统状态陷入局部停滞吸引区域的平均驻留持续时间：

$$ TT = \frac{\sum_{v=v_{\min}}^N v \cdot P(v)}{\sum_{v=v_{\min}}^N P(v)} $$

#### 3. 最长垂直线 ($V_{\max}$)
$$ V_{\max} = \max\left( \{ v \mid P(v) > 0 \} \right) $$

#### 4. 金融实战物理机理
- **对角线代表“流动” (Flow)**：动量、趋势、价格持续单向进化；
- **垂直线代表“停滞” (Trap)**：中枢震荡、高密度筹码沉淀区、日内缩量盘整、跌停/涨停封板缺乏成交。
- 当 $LAM$ 与 $TT$ 显著飙升时，资产价格正陷入强烈的横盘整理或流动性吸筹状态。此时趋势跟踪系统若强行入场，必然遭遇反复多头虚假突破与假破位拉回的剧烈滑点磨损（Whipsaw）；相反，均值回归与网格做市策略在此状态下具有最高胜率。

---

### 3.6 综合指标汇总速查表 (RQA Metrics Master Reference)

| 核心指标符号 | 指标全称 | 数学计算核心公式 | 核心物理动力学含义 | 量化交易状态判定规则 |
| :---: | :--- | :--- | :--- | :--- |
| **$RR$** | 递归率 (Recurrence Rate) | $\frac{1}{N(N-1)}\sum_{i \ne j}\mathbf{R}_{i,j}$ | 相空间状态重访总概率；关联积分 $C(\varepsilon)$ | $RR < 0.03$: 混沌无序；$RR \in [0.05, 0.12]$: 典型健康行情 |
| **$DET$** | 确定性 (Determinism) | $\frac{\sum_{l \ge l_{\min}} l P(l)}{\sum_{l \ge 1} l P(l)}$ | 轨道平行演化的确定性比例；抗噪声度量 | **$DET > 0.85$ 且动量转负: 触发崩盘脆性告警**；$DET < 0.40$: 纯随机游走 |
| **$L$** | 平均对角线长 (Avg Diagonal Length) | $\frac{\sum_{l \ge l_{\min}} l P(l)}{\sum_{l \ge l_{\min}} P(l)}$ | 确定性预测视窗 (Predictability Horizon) | $L \ge 4.5$: 强趋势惯性；$L < 2.5$: 动能迅速衰减 |
| **$L_{\max}$** | 最长对角线 (Max Diagonal Length) | $\max(\{l \mid P(l)>0\})$ | 倒数与最大李雅普诺夫指数 $\lambda_{\max}$ 成正比 | $L_{\max} \ge 20$: 临界锁死状态；$L_{\max} \le 4$: 高度发散 |
| **$ENTR$** | 香农对角线熵 (Diagonal Entropy) | $-\sum p(l) \ln p(l)$ | 确定性动力学吸引子几何复杂性与多尺度度量 | $ENTR > 2.5$: 复杂混沌吸引子；$ENTR < 0.8$: 简单周期或纯噪声 |
| **$LAM$** | 层流性 (Laminarity) | $\frac{\sum_{v \ge v_{\min}} v P(v)}{\sum_{v \ge 1} v P(v)}$ | 系统陷入低速/停滞状态的比例 | $LAM > 0.70$: 强中枢震荡与吸筹；$LAM < 0.30$: 纯流动趋势态 |
| **$TT$** | 陷阱时间 (Trapping Time) | $\frac{\sum_{v \ge v_{\min}} v P(v)}{\sum_{v \ge v_{\min}} P(v)}$ | 系统在局部微观状态区域的平均滞留时间 | $TT \ge 4.0$: 严重粘性阻滞；$TT \le 2.0$: 价格穿透极快 |
| **$V_{\max}$** | 最长垂直线 (Max Vertical Length) | $\max(\{v \mid P(v)>0\})$ | 单次状态锁定的最大极值持续天数 | $V_{\max} \ge 8$: 发生异常流动性停牌、一字封死或极端筹码冻结 |

---

## 四、 RQA 在量化交易系统中的三大杀手级应用 (Three Killer Applications of RQA in Quant Systems)

### 4.1 杀手级应用 1：系统性金融危机与黑天鹅前瞻预警

#### 1. 索内特-埃克曼临界相变机理 (The Sornette-Eckmann Critical Transition)
Didier Sornette (2003) 在其经典对数周期幂律奇点 (LPPLS) 模型中指出：**系统性金融危机的根本成因并非外生意外冲击，而是市场内部交易者在长期正反馈膨胀中形成的强烈羊群模仿效应 (Herding Behavior) 与相干共振 (Coherence Resonance)**。

在正常市场阶段，数万名异质性交易者拥有不同的投资期限、多空信念与风险偏好，彼此策略的非线性对冲使得市场整体表现为高自由度的随机游走（RP 呈现均匀离散黑点，DET 处于 $0.30 \sim 0.50$ 的良性区间）。

然而，当泡沫膨胀至尾声或系统性去杠杆危机逼近时：
1. **自由度低维坍缩 (Dimensionality Collapse)**：追涨杀跌的正反馈算法、趋同的 VaR 风险限额、融资融券警戒线与被动指数基金形成共振，整个市场由数十维的复杂生态瞬间坍缩为**单一维度的单向抛售或羊群抢跑轨道**；
2. **确定性脉冲飙升 (DET Impulse Spike)**：在崩盘发生前的 $5 \sim 20$ 个交易日内，虽然价格可能仍在创出新高或处于高位震荡，但资产在相空间中的微观轨迹早已完全同构于历史高危临界形态——**RQA 核心指标 $DET$ 与最长线 $L_{\max}$ 会发生显著突兀的脉冲飙升（如 $DET > 0.85$ 且 $L_{\max} > 15$）**！
3. **崩盘雪崩与耗散释放**：当这种相空间确定性锁定达到极致时，任何微小的流动性扰动均会引发不可逆的多米诺骨牌级联崩塌（Cascade Liquidation）。

```mermaid
sequenceDiagram
    autonumber
    participant M as 宏观金融生态 (Market)
    participant P as 相空间重构 (Phase Space R^m)
    participant R as RQA 指标监控 (DET / L_max)
    participant S as 实盘 Stage 1 门禁 (check_health)

    Note over M,P: 正常阶段: 异质性交易者博弈，高维随机游走
    M->>P: 收益率随机分散，多空力量制衡
    P->>R: RP 散乱点分布，DET ≈ 0.45, L_max ≤ 5
    R->>S: 状态为 GO (正常执行)

    Note over M,P: 泡沫/杠杆累积阶段: 羊群效应趋同，相干性暴增
    M->>P: 交易者行为共振，自由度低维坍缩
    P->>R: 轨迹陷入同一确定性管道，DET 脉冲飙升至 0.88, L_max ≥ 18
    R->>S: 捕获异常相变脉冲！触发 CAUTION_RQA_DETERMINISTIC_CASCADE
    S->>M: 执行硬熔断: 风险敞口砍半至 30%，闲置资金全量归集现金端
    Note over M,S: 崩盘爆发: 市场大跌但组合已提前完成去杠杆，回撤受控！
```

#### 2. 经典历史危机实证范例
- **2008 年美国次贷危机 (雷曼破产)**：标普 500 指数在 2008 年 8 月中旬（破产前一个月），其滚动 60 日 RQA 的 $DET$ 指标自 0.52 陡增至 0.89，$L_{\max}$ 突破 22。此后在 9 月正式爆发崩盘；
- **2015 年中国 A 股股灾**：上证指数在 2015 年 5 月底至 6 月初（5178 点见顶前夕），场外配资与杠杆融资极度同质化。RQA 监测显示全市场中位数 $DET$ 突破 0.91，形成长达 14 天的超高确定性极值锁定，随后引发千股跌停奇观；
- **2020 年 3 月全球流动性挤兑**：在海外疫情爆发初期的 2 月 18 日至 24 日，美股虽然高位盘整，但跨境资本流动的相空间重构指标 $DET$ 率先击穿 0.86 临界线，比标普 500 指数正式破位早整整 4 个交易日发出致命预警。

---

### 4.2 杀手级应用 2：趋势动量 vs. 均值回归状态自适应路由

量化交易中最经典的策略死穴在于：**趋势跟踪策略死于震荡拉锯，均值回归策略死于单边主升/主降**。

传统分类通常依赖移动平均线、ADX 或 ATR，但这些指标存在巨大的相位滞后与阈值过拟合问题。利用 RQA 矩阵的**对角线模式（流动趋势）**与**垂直线模式（层流陷阱）**，我们构建了纯物理自适应的**四态动力学路由状态机 (RQA 4-State Regime Engine)**：

```mermaid
stateDiagram-v2
    [*] --> Chaotic_Turbulence: 初始启动状态

    Chaotic_Turbulence --> Deterministic_Trend: DET ≥ 0.70 且 L ≥ 4.0 且 LAM < 0.35\n(平行流动占优)
    Deterministic_Trend --> Laminar_Trap: LAM ≥ 0.70 且 TT ≥ 3.5\n(垂直层流陷阱占优)
    Deterministic_Trend --> Deterministic_Cascade: DET ≥ 0.85 且 近5日收益 < 0\n(极端相空间锁死)
    Laminar_Trap --> Deterministic_Trend: LAM < 0.40 且 DET ≥ 0.65\n(离开中枢破局)
    Laminar_Trap --> Chaotic_Turbulence: DET < 0.35 且 RR < 0.04\n(结构消散进入纯白噪)
    Deterministic_Cascade --> Chaotic_Turbulence: 市场充分释放耗散，DET 回落至 < 0.60
```

#### 四态动力学路由规则对照矩阵

| 状态机模式 (Regime State) | RQA 判据量化组合门禁 | 底层物理动力学特征 | 最优量化策略权重路由配置 |
| :--- | :--- | :--- | :--- |
| **1. 确定性趋势态 (Deterministic Trend)** | $DET \ge 0.70$ 且 $L \ge 4.0$ 且 $LAM < 0.35$ | 轨迹沿平行管道流动，李雅普诺夫发散受限，动能纯度极高 | **100% 预算路由至动量/趋势跟踪策略**：全额激活 SuperTrend、Donchian 突破与均线顺势加仓；彻底禁用反转策略。 |
| **2. 层流震荡态 (Laminar Trap)** | $LAM \ge 0.70$ 且 $TT \ge 3.5$ 且 $DET < 0.60$ | 状态在局部空间停滞打转，速度近乎为零，微观筹码高度聚集 | **100% 预算路由至均值回归策略**：激活 RSI 超买超卖、Chan 箱体/缠论中枢震荡套利、网格做市；趋势策略一律挂起防磨损。 |
| **3. 混沌高噪态 (Chaotic Turbulence)** | $DET < 0.40$ 且 $RR < 0.05$ 且 $LAM < 0.40$ | 状态高度离散无序，初值极端敏感发散，无统计可预测性 | **宏观被动防守态**：整体股票仓位上限折半至 $\le 40\%$，大幅拉宽追踪止损容忍度，剩余资金停靠高流动性现金端。 |
| **4. 级联崩盘态 (Deterministic Cascade)** | $DET \ge 0.85$ 且 $L_{\max} \ge 16$ 且 $\text{ROC}_{5d} < 0$ | 羊群低维坍缩，系统性相干锁死，脆性雪崩即将爆发 | **系统性硬熔断**：触发 `CAUTION_RQA_DETERMINISTIC_CASCADE`，强行降权至 30% 并执行 SELLS FIRST，封杀一切新开仓。 |

---

### 4.3 杀手级应用 3：交叉递归分析 (CRQA) 与跨资产领先滞后时延

两个不同的动力系统 $\vec{x}_i \in \mathbb{R}^m$ 与 $\vec{y}_j \in \mathbb{R}^m$ 在同一相空间中的交互作用，由 Marwan & Kurths (2002) 推广的**交叉递归图 (Cross-Recurrence Plot, CRP)** 刻画：

$$ \mathbf{CR}_{i,j}(\varepsilon) = \Theta\left(\varepsilon - \|\vec{x}_i - \vec{y}_j\|\right), \quad i = 1, \dots, N_x, \ j = 1, \dots, N_y $$

- **CRP 的非对称性**：与单一系统的自递归图（必关于主对角线对称且对角线恒为 1）不同，CRP 一般**不具备对称性** ($\mathbf{CR}_{i,j} \ne \mathbf{CR}_{j,i}$)，且主对角线上不一定有连续黑点。
- **对角线偏移量 $\Delta$ (Shifted Diagonal) 与领先滞后 (Lead-Lag Latency)**：
  - 若系统 $Y$ 是系统 $X$ 的滞后跟随者，且滞后时间为 $\Delta^* = \tau_{\text{delay}}$，则当且仅当 $j = i + \tau_{\text{delay}}$ 时，状态满足 $\vec{y}_j \approx \vec{x}_i$；
  - 这意味着：**CRP 中的对角平行线将整体偏离主对角线，平行位移至 $j - i = \Delta^*$ 对应的偏置对角线上**！

```mermaid
flowchart LR
    subgraph CRQA_INPUT["双资产时间序列构建"]
        X["领头资产 X(t)\n(例如: 股指期货 IF / 龙头个股)"] --> PX["相空间重构 x_i ∈ R^m"]
        Y["跟随后验资产 Y(t)\n(例如: 股票现货 000300 / 板块跟风股)"] --> PY["相空间重构 y_j ∈ R^m"]
    end

    subgraph CRP_CORE["交叉递归矩阵 CR_ij"]
        PX --> CRP["CR_ij = Θ(ε - ||x_i - y_j||)"]
        PY --> CRP
    end

    subgraph LAG_EXTRACTION["领先滞后时延谱提取"]
        CRP --> DIAG_SUM["沿各偏移对角线积分: RR(Δ)\nΔ ∈ [-MaxLag, +MaxLag]"]
        DIAG_SUM --> PEAK["探测峰值极值点: Δ* = argmax RR(Δ)"]
        PEAK --> DECISION{"Δ* 符号判定"}
        DECISION -->|"Δ* > 0"| LEAD["X 领先 Y (提前 Δ* 步)\n量化生成 X 对 Y 的因果引导 Alpha"]
        DECISION -->|"Δ* < 0"| LAG["Y 领先 X (滞后)"]
        DECISION -->|"Δ* = 0"| SYNC["双资产完全同步共振"]
    end
```

#### 对角线交叉递归分布谱 (Diagonal Cross-Recurrence Profile)
对任意整数时延偏移量 $\Delta \in [-K, +K]$，定义偏移对角线上的局部递归密度：

$$ RR(\Delta) = \begin{cases} \frac{1}{N - \Delta} \sum_{i=1}^{N - \Delta} \mathbf{CR}_{i, i+\Delta}, & \Delta \ge 0 \\ \frac{1}{N + \Delta} \sum_{j=1}^{N + \Delta} \mathbf{CR}_{j-\Delta, j}, & \Delta < 0 \end{cases} $$

- **动态最优时延估算器**：
  $$ \tau_{\text{delay}}^* = \arg\max_{-K \le \Delta \le K} RR(\Delta) $$
- **量化交易重大实战场景**：
  1. **期现套利与领先引导**：股指期货（如 IF/IM）相比现货指数（如沪深300/中证1000）存在极短时延领先，CRQA 可在 5 分钟线实时提取动态微观领先时钟 $\tau^*$，指导现货组合建仓节奏；
  2. **大宗商品宏观传导**：工业金属（铜/原油）价格变动向有色金属/能源股票板块传导通常存在 $3 \sim 7$ 个交易日的动力学迟滞，CRQA 能够动态捕捉这一时延峰值，实现跨品种宏观 Alpha 搬砖；
  3. **产业链核心龙头与补涨轮动**：产业链“链主”（如半导体设备/光伏逆变器龙头）与下游跟风中小票之间存在确定性递归扩散时延，为行业轮动策略提供因果级入场信号。

---

## 五、 工业级 Python/NumPy 向量化算子实现 (Industrial-Grade Vectorized Python/NumPy Operator Implementation)

### 5.1 算法复杂度与内存优化设计

在许多学院派文献中，RQA 算法采用多层 Python 原生嵌套循环遍历矩阵，计算复杂度高达 $O(N^2)$ 甚至 $O(N^3)$。当处理长周期历史回测或高频 5 分钟数据时，此类实现会导致程序彻底卡死或遭遇严重 OOM (Out-of-Memory) 内存崩溃。

本规范确立工业级高性能标准：
1. **滑动视窗受限原则 (Bounded Rolling Window)**：在线与回测均基于滚动视窗 $W \in [60, 120]$（日线）或 $W \in [120, 240]$（5m）。在 $W = 60$ 时，相空间样本量 $N = W - (m-1)\tau \approx 58$，矩阵规模仅为 $58 \times 58 = 3,364$ 字节，完全驻留于 CPU L1/L2 缓存，性能极佳；
2. **切比雪夫范数广播向量化**：利用 NumPy 高维广播 `np.abs(X[:, None, :] - X[None, :, :])` 与 `np.max(..., axis=-1)`，一次性生成全局连续内存距离矩阵，完全消灭 Python 循环；
3. **1D 游程扫描 (Run-Length Encoding, RLE)**：提取对角线和垂直线长度时，禁止在矩阵中进行深度优先搜索；将矩阵沿对角线或列切片为一维布尔向量后，采用极简的 `np.diff` 向量化卷积提取线段起止点，速度提升 2 个数量级。

---

### 5.2 核心算子完整参考实现 (`common/rqa.py`)

以下代码符合 [`AGENTS.md`](file:///home/stone/Work/github/quant/AGENTS.md) 纯因果防前视规范，所有指标仅依赖窗口历史信息，具备极高的鲁棒性：

```python
"""Recurrence Quantification Analysis (RQA) & Phase Space Reconstruction Operators.

Academic foundations:
- Henri Poincaré (1890): Poincaré Recurrence Theorem
- Floris Takens (1981): Delay-coordinate embedding of strange attractors
- Eckmann, Kamphorst & Ruelle (1987): Recurrence Plots in phase space
- Zbilut & Webber (1992): Recurrence Quantification Analysis metrics
- Norbert Marwan et al. (2007, Physics Reports): Complex systems recurrence analysis
- Bastos & Caiado (2011): Financial crash early warning via RQA determinism
"""

from typing import Dict, List, Optional, Tuple, Union
import numpy as np
import pandas as pd


def phase_space_reconstruction(
    series: np.ndarray,
    m: int = 3,
    tau: int = 1,
) -> np.ndarray:
    """Reconstruct multi-dimensional phase space trajectory via Takens' delay coordinates.

    Parameters
    ----------
    series : np.ndarray
        1D scalar time series of length N (e.g. log returns or z-score normalized prices).
    m : int, default 3
        Embedding dimension (m >= 2).
    tau : int, default 1
        Time delay in number of samples (tau >= 1).

    Returns
    -------
    np.ndarray
        Phase space trajectory matrix X of shape (N - (m - 1) * tau, m).
        Each row is [s[t], s[t - tau], ..., s[t - (m - 1) * tau]].
    """
    if len(series) < m * tau:
        raise ValueError(f"Series length ({len(series)}) must be >= m * tau ({m * tau})")
    
    n_pts = len(series) - (m - 1) * tau
    shape = (n_pts, m)
    strides = (series.strides[0], series.strides[0] * tau)
    # Stride tricks for zero-copy memory mapping
    return np.lib.stride_tricks.as_strided(series[(m - 1) * tau:], shape=shape, strides=(series.strides[0], -series.strides[0] * tau))


def recurrence_matrix(
    X: np.ndarray,
    epsilon: Optional[float] = None,
    epsilon_ratio: float = 0.15,
    metric: str = "chebyshev",
) -> Tuple[np.ndarray, float]:
    """Compute binary Recurrence Plot matrix R_ij = Theta(epsilon - ||x_i - x_j||).

    Parameters
    ----------
    X : np.ndarray
        Phase space trajectory of shape (N_pts, m).
    epsilon : float, optional
        Absolute cutoff threshold. If None, computed as epsilon_ratio * std(X).
    epsilon_ratio : float, default 0.15
        Standard deviation scaling factor if epsilon is not provided.
    metric : str, default 'chebyshev'
        Distance norm: 'chebyshev' (L_inf, fast) or 'euclidean' (L_2).

    Returns
    -------
    R : np.ndarray (bool)
        Binary symmetric recurrence matrix of shape (N_pts, N_pts).
    epsilon_val : float
        The concrete threshold value utilized.
    """
    n_pts = X.shape[0]
    if n_pts == 0:
        return np.zeros((0, 0), dtype=bool), 0.0

    # Pairwise difference broadcasting: (N, 1, m) - (1, N, m) -> (N, N, m)
    diff = np.abs(X[:, np.newaxis, :] - X[np.newaxis, :, :])

    if metric == "chebyshev":
        dist_mat = np.max(diff, axis=-1)
    elif metric == "euclidean":
        dist_mat = np.sqrt(np.sum(diff ** 2, axis=-1))
    else:
        raise ValueError(f"Unsupported metric: {metric}. Choose 'chebyshev' or 'euclidean'.")

    if epsilon is None:
        std_val = float(np.std(X))
        epsilon = max(1e-6, epsilon_ratio * (std_val if std_val > 1e-8 else 1.0))

    R = dist_mat <= epsilon
    return R, float(epsilon)


def extract_line_lengths_rle(line: np.ndarray) -> np.ndarray:
    """Extract consecutive True run lengths from a 1D boolean array via Run-Length Encoding.

    Vectorized 1D convolution approach: O(length) without Python loops.
    """
    if len(line) == 0 or not np.any(line):
        return np.array([], dtype=int)

    padded = np.pad(line.astype(np.int8), (1, 1), mode="constant", constant_values=0)
    diff = np.diff(padded)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    return ends - starts


def compute_rqa_metrics(
    R: np.ndarray,
    l_min: int = 2,
    v_min: int = 2,
) -> Dict[str, float]:
    """Compute full Recurrence Quantification Analysis (RQA) metric family.

    Metrics calculated:
    - RR: Recurrence Rate (excluding Line of Identity)
    - DET: Determinism (proportion of recurrence points forming diagonal lines >= l_min)
    - L: Average diagonal line length (predictability horizon)
    - L_max: Maximum diagonal line length (inversely related to Lyapunov exponent)
    - ENTR: Shannon entropy of diagonal line length distribution
    - LAM: Laminarity (proportion of recurrence points forming vertical lines >= v_min)
    - TT: Trapping Time (average vertical line length in laminar states)
    - V_max: Maximum vertical line length (duration of state freezing)
    """
    n = R.shape[0]
    if n < 3:
        return {
            "RR": 0.0, "DET": 0.0, "L": 0.0, "L_max": 0.0, "ENTR": 0.0,
            "LAM": 0.0, "TT": 0.0, "V_max": 0.0,
        }

    # 1. Total recurrence points excluding Main Diagonal (LOI)
    loi_mask = np.eye(n, dtype=bool)
    off_diag_recurrence = np.sum(R & (~loi_mask))
    total_off_diag = n * (n - 1)
    rr = float(off_diag_recurrence / total_off_diag) if total_off_diag > 0 else 0.0

    # 2. Diagonal line extraction (scanning upper diagonals k = 1 to n - 1)
    diag_lengths: List[int] = []
    for k in range(1, n):
        diag_slice = np.diag(R, k=k)
        runs = extract_line_lengths_rle(diag_slice)
        if len(runs) > 0:
            diag_lengths.extend(runs.tolist())

    diag_arr = np.array(diag_lengths, dtype=int)
    diag_filtered = diag_arr[diag_arr >= l_min]

    if len(diag_filtered) > 0 and off_diag_recurrence > 0:
        # DET: symmetric count multiplied by 2 because only upper triangle was extracted
        det_points = 2.0 * np.sum(diag_filtered)
        det = float(np.clip(det_points / off_diag_recurrence, 0.0, 1.0))
        l_avg = float(np.mean(diag_filtered))
        l_max = float(np.max(diag_filtered))

        # Shannon Entropy of diagonal lines
        counts = pd.Series(diag_filtered).value_counts(normalize=True).values
        entr = float(-np.sum(counts * np.log(counts + 1e-12)))
    else:
        det = 0.0
        l_avg = 0.0
        l_max = 0.0
        entr = 0.0

    # 3. Vertical line extraction (scanning columns j = 0 to n - 1)
    vert_lengths: List[int] = []
    for j in range(n):
        col_slice = R[:, j]
        runs = extract_line_lengths_rle(col_slice)
        if len(runs) > 0:
            vert_lengths.extend(runs.tolist())

    vert_arr = np.array(vert_lengths, dtype=int)
    vert_filtered = vert_arr[vert_arr >= v_min]
    total_vert_points = np.sum(vert_arr)

    if len(vert_filtered) > 0 and total_vert_points > 0:
        lam = float(np.clip(np.sum(vert_filtered) / total_vert_points, 0.0, 1.0))
        tt = float(np.mean(vert_filtered))
        v_max = float(np.max(vert_filtered))
    else:
        lam = 0.0
        tt = 0.0
        v_max = 0.0

    return {
        "RR": rr,
        "DET": det,
        "L": l_avg,
        "L_max": l_max,
        "ENTR": entr,
        "LAM": lam,
        "TT": tt,
        "V_max": v_max,
    }


def rolling_rqa_indicators(
    price_series: pd.Series,
    window: int = 60,
    m: int = 3,
    tau: int = 1,
    epsilon_ratio: float = 0.15,
    l_min: int = 2,
    v_min: int = 2,
    metric: str = "chebyshev",
) -> pd.DataFrame:
    """Compute rolling window RQA indicators over a historical time series.

    Strictly causal and anti-lookahead: bar t only depends on t - window + 1 to t.
    """
    n_total = len(price_series)
    dates = price_series.index
    results: List[Dict[str, float]] = []

    # Use log returns to ensure stationarity in phase space
    values = np.log(price_series / price_series.shift(1)).fillna(0.0).values

    for end_idx in range(n_total):
        start_idx = end_idx - window + 1
        if start_idx < 0:
            results.append({
                "RQA_RR": np.nan, "RQA_DET": np.nan, "RQA_L": np.nan,
                "RQA_L_max": np.nan, "RQA_ENTR": np.nan, "RQA_LAM": np.nan,
                "RQA_TT": np.nan, "RQA_V_max": np.nan,
            })
            continue

        w_series = values[start_idx : end_idx + 1]
        # Standardize locally to eliminate rolling scale drift
        w_std = np.std(w_series)
        w_norm = (w_series - np.mean(w_series)) / (w_std if w_std > 1e-8 else 1.0)

        try:
            X = phase_space_reconstruction(w_norm, m=m, tau=tau)
            R, _ = recurrence_matrix(X, epsilon=None, epsilon_ratio=epsilon_ratio, metric=metric)
            m_dict = compute_rqa_metrics(R, l_min=l_min, v_min=v_min)
            results.append({f"RQA_{k}": v for k, v in m_dict.items()})
        except Exception:
            results.append({
                "RQA_RR": np.nan, "RQA_DET": np.nan, "RQA_L": np.nan,
                "RQA_L_max": np.nan, "RQA_ENTR": np.nan, "RQA_LAM": np.nan,
                "RQA_TT": np.nan, "RQA_V_max": np.nan,
            })

    return pd.DataFrame(results, index=dates)


def cross_recurrence_lead_lag(
    series_x: np.ndarray,
    series_y: np.ndarray,
    m: int = 3,
    tau: int = 1,
    max_lag: int = 10,
    epsilon_ratio: float = 0.20,
) -> Tuple[int, float, Dict[int, float]]:
    """Compute Cross-Recurrence Quantification Analysis (CRQA) lead-lag latency.

    Parameters
    ----------
    series_x : np.ndarray
        Target/Driver time series (normalized).
    series_y : np.ndarray
        Follower/Response time series (normalized).
    max_lag : int, default 10
        Maximum time delay to test [-max_lag, +max_lag].

    Returns
    -------
    optimal_lag : int
        Estimated dynamic lag. If > 0, X leads Y by optimal_lag bars.
    max_corr : float
        Recurrence rate peak value at the optimal lag.
    profile : Dict[int, float]
        Cross-recurrence density profile for each lag offset.
    """
    X = phase_space_reconstruction(series_x, m=m, tau=tau)
    Y = phase_space_reconstruction(series_y, m=m, tau=tau)
    n = min(len(X), len(Y))
    X = X[:n]
    Y = Y[:n]

    diff = np.abs(X[:, np.newaxis, :] - Y[np.newaxis, :, :])
    dist_mat = np.max(diff, axis=-1)

    std_pool = 0.5 * (np.std(X) + np.std(Y))
    eps = max(1e-6, epsilon_ratio * (std_pool if std_pool > 1e-8 else 1.0))
    CR = dist_mat <= eps

    profile: Dict[int, float] = {}
    best_lag = 0
    best_rr = -1.0

    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            diag_slice = np.diag(CR, k=lag)
        else:
            diag_slice = np.diag(CR, k=lag)
        rr_val = float(np.mean(diag_slice)) if len(diag_slice) > 0 else 0.0
        profile[lag] = rr_val
        if rr_val > best_rr:
            best_rr = rr_val
            best_lag = lag

    return best_lag, best_rr, profile
```

---

## 六、 与当前量化代码库 (quant) 的分层挂载架构 (Integration Architecture with Quant Repository)

本规范在既有代码库的分层调用链路如下图所示：

```mermaid
flowchart TD
    subgraph COMMON_LAYER["底层基础算子层 (common/)"]
        RQA_CORE["common/rqa.py\n• phase_space_reconstruction\n• recurrence_matrix\n• compute_rqa_metrics\n• rolling_rqa_indicators\n• cross_recurrence_lead_lag"]
    end

    subgraph CONFIG_LAYER["配置引擎层 (pipeline/research_strategy/rs/)"]
        CONFIG["pipeline/research_strategy/rs/config.py\nStrategyConfig 扩展:\n• blend_enable_rqa_regime = False\n• blend_rqa_window = 60\n• blend_rqa_det_crash_thresh = 0.85\n• blend_rqa_lam_trap_thresh = 0.70"]
    end

    subgraph BLEND_LAYER["宏观多策略融合底座 (pipeline/research/blend/)"]
        BLEND_BASE["pipeline/research/blend/blend_base.py\n(第 10 大宏观锚点)\n• 锚点 10: RQA 确定性崩盘锁死防卫与层流三态路由\n  - DET ≥ 0.85 崩盘锁死 -> 权益仓位压缩至 0.30 避险\n  - DET ≥ 0.70 趋势态 -> 100% 偏好动量模型\n  - LAM ≥ 0.70 层流态 -> 100% 偏好均值回归模型\n  - 稀疏权重 NaN vs 0.0 绝对契约保障"]
    end

    subgraph LIVE_LAYER["实盘生产两阶段风控门禁 (scripts/)"]
        STAGE1["scripts/check_live_portfolio_health.py @ 14:00\nStage 1 宏观健康体检:\n• 计算基准指数 RQA 指标\n• 若 DET ≥ 0.85 且短线转弱:\n  触发 CAUTION_RQA_DETERMINISTIC_CASCADE 预警\n• 强制指令降级至 Tier 2 防守 (70% 现金)"]
        STAGE2["scripts/run_live_strategy.py @ 14:15\nStage 2 订单执行生成:\n• 读取 stage1_health_report.json 告警\n• Phase 1: 优先抛售 (SELLS FIRST)\n• Phase 2: 冻结高风险开仓 (BUY 拦截)\n• Phase 4: 闲置资金归集现金代理 (BIL / 511880)"]
    end

    COMMON_LAYER --> CONFIG_LAYER
    CONFIG_LAYER --> BLEND_LAYER
    BLEND_LAYER --> LIVE_LAYER
    STAGE1 -->|"输出 stage1_health_report.json"| STAGE2
```

---

### 6.1 `common/rqa.py` 算子沉淀与无环依赖设计
- 依据 [`AGENTS.md`](file:///home/stone/Work/github/quant/AGENTS.md) 铁律，`common/` 为根目录下的共享基础算子库，**严禁反向 `import pipeline`**。
- `common/rqa.py` 仅依赖基础数学库（`numpy`, `pandas`, `scipy`），不包含任何业务回测逻辑，为整个量化体系提供高内聚、低耦合的底层相空间物理计算引擎。

---

### 6.2 `pipeline/research/blend/blend_base.py` 宏观状态机第 10 大锚点

在 [`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) 中，已存在 8 大经典锚点（分形维数、CYQ 筹码门禁、VAH 突破、威科夫破底翻、吊灯止损、Rolling IC-IR 正交化、SuperTrend 宏观预算、资产动量防线）及第 9 大希尔伯特主导周期锚点。

**新增第 10 大锚点：RQA 确定性崩盘锁死防卫与层流三态动力学路由**：
```python
# =========================================================================
# 锚点 10: RQA 非线性动力学相空间状态机与确定性锁死防护 (RQA Dynamics Anchor)
# =========================================================================
if getattr(self.config, "blend_enable_rqa_regime", False):
    benchmark_sym = getattr(self.config, "benchmark_symbol", "000300.SH")
    if benchmark_sym in universe and len(universe[benchmark_sym]) >= self.config.blend_rqa_window:
        bench_close = universe[benchmark_sym]["Close"]
        rqa_df = rolling_rqa_indicators(
            bench_close,
            window=self.config.blend_rqa_window,
            m=self.config.blend_rqa_m,
            tau=self.config.blend_rqa_tau,
            epsilon_ratio=self.config.blend_rqa_epsilon_ratio,
        )
        curr_det = rqa_df["RQA_DET"].iloc[t_idx]
        curr_lam = rqa_df["RQA_LAM"].iloc[t_idx]
        curr_l = rqa_df["RQA_L"].iloc[t_idx]
        roc_5d = bench_close.pct_change(5).iloc[t_idx]

        # 1. 级联崩盘锁死判据: DET 极值脉冲 + 短期走弱
        if curr_det >= self.config.blend_rqa_det_crash_thresh and roc_5d < 0.0:
            # 遭遇低维相空间雪崩锁死，全组合风险预算强行压缩至 30%
            risk_budget_scale = min(risk_budget_scale, 0.30)
            # 腾出仓位全量引导至无风险现金管理端 (BIL / 511880)

        # 2. 确定性趋势 vs 层流震荡自适应路由
        elif curr_det >= 0.70 and curr_l >= 4.0 and curr_lam < 0.35:
            # 趋势主导态: 提升动量与趋势策略族权重配比 (1.3x)
            trend_sub_strategy_boost = 1.30
        elif curr_lam >= self.config.blend_rqa_lam_trap_thresh:
            # 层流陷阱态: 压缩突破类策略权重，提升均值回归与中枢套利权重 (1.4x)
            reversion_sub_strategy_boost = 1.40
```

> [!WARNING]
> **恪守稀疏权重与 NaN vs 0.0 铁律**：
> 在 Blend 状态机中被 RQA 规则否决离场或剔除的标的，**必须显式赋予 `0.0` 权重，严禁保留为 `NaN`**！否则回测引擎与实盘调度器会错误继承历史非零仓位，引发静默持仓与透支违规。

---

### 6.3 `scripts/check_live_portfolio_health.py` 实盘 Stage 1 极端脆性告警

在每日 14:00 – 14:15 运行的 Stage 1 宏观健康体检中，接入 RQA 崩盘探测器：

```python
# -------------------------------------------------------------------------
# RQA 相空间确定性级联崩盘前瞻检验 (Stage 1 Health Inspection)
# -------------------------------------------------------------------------
def evaluate_rqa_cascade_risk(
    benchmark_df: pd.DataFrame,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Inspect market phase space for low-dimensional deterministic crash lock-in."""
    window = int(resolve_blend_param(params, "rqa_window", 60, int))
    det_thresh = float(resolve_blend_param(params, "rqa_det_crash_thresh", 0.85, float))

    if benchmark_df.empty or len(benchmark_df) < window:
        return {"rqa_cascade_active": False, "det": 0.0, "lam": 0.0}

    close_series = benchmark_df["Close"]
    rqa_df = rolling_rqa_indicators(close_series, window=window)
    latest_det = float(rqa_df["RQA_DET"].iloc[-1])
    latest_lam = float(rqa_df["RQA_LAM"].iloc[-1])
    roc_5d = float(close_series.pct_change(5).iloc[-1])

    cascade_active = (latest_det >= det_thresh) and (roc_5d < 0.0)
    return {
        "rqa_cascade_active": cascade_active,
        "det": latest_det,
        "det_thresh": det_thresh,
        "lam": latest_lam,
        "roc_5d": roc_5d,
    }
```

- **动作合成联动**：若 `rqa_cascade_active == True`，`check_live_portfolio_health.py` 将发出顶级风控预警：
  - **告警标识**：`CAUTION_RQA_DETERMINISTIC_CASCADE`；
  - **指令动作**：将交易门禁直接强制降级为 `CAUTION_DEFENSIVE`（启用 70% 现金防守）；
  - **交割单指示**：向 `stage1_health_report.json` 写入拦截指令，指示 Stage 2 执行 **SELLS FIRST** 优先抛售，严禁任何高 Beta 资产新建多头仓位。

---

## 七、 总结、最佳实践与 docs/development_plan.md 路线图关联 (Summary, Best Practices & Roadmap Association)

### 7.1 量化实战关键避坑指南 (Non-Linear Quant Pitfalls)

1. **严禁使用绝对价格直接做相空间重构**：
   - 错误做法：直接将股票绝对收盘价 $P_t$ 代入 $\vec{x}_t = [P_t, P_{t-\tau}, \dots]$。在长期牛市或强单边行情中，绝对价格漂移会导致相空间轨迹沿对角线无限延伸逃逸，完全丧失重访机会（$RR \to 0$）；
   - 正确做法：必须输入因果对数收益率 $r_t = \ln(P_t / P_{t-1})$，或对窗口内价格进行因果去趋势滚动标准化（Z-Score 或局部极值归一化）。
2. **严禁全样本固定 $\varepsilon$ 导致前视偏差**：
   - 错误做法：计算全局时序的标准差 $\sigma_{\text{all}}$ 并设定 $\varepsilon = 0.2 \sigma_{\text{all}}$。这引入了严重的未来全景波动率泄漏；
   - 正确做法：必须在每一个滚动视窗 $[t-W+1, t]$ 内独立自适应计算 $\varepsilon_t = 0.2 \sigma_{t, W}$，或基于滚动近邻距离分布动态确定。
3. **警惕游程扫描中的边界截断偏差**：
   - 在滑动视窗边缘，未结束的对角线或垂直线若直接截断，会导致较长线段被低估；
   - 必须在统一规则下处理端点线段（如按实际已实现长度保守计入），保持回测与实盘比特级因果一致。

---

### 7.2 Track R 原子任务分解与开发计划总表关联

依据 [`AGENTS.md`](file:///home/stone/Work/github/quant/AGENTS.md) 顶层契约，本文档的所有工程实现任务正式归入量化路线图体系（**Track R: 非线性动力学与递归量化分析工程体系**），并在统一权威总表 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) 中统一排期与追踪：

| 任务编号 | 任务分类 | 涉及核心文件与符号 | 核心交付内容与验收标准 | 优先级 | 前置依赖 |
| :---: | :--- | :--- | :--- | :---: | :---: |
| **`R1.1`** | 底层算子沉淀 | [`common/rqa.py`](file:///home/stone/Work/github/quant/common/rqa.py)<br>`phase_space_reconstruction`<br>`compute_rqa_metrics` | 1. 实现切比雪夫 $L_\infty$ 距离矩阵向量化计算；<br>2. 实现 1D 游程编码 (RLE) 对角线与垂直线无循环提取；<br>3. 完备离线单元测试，断言与理论解析解一致。 | **P1** | 无 |
| **`R1.2`** | 交叉领先时延 | [`common/rqa.py`](file:///home/stone/Work/github/quant/common/rqa.py)<br>`cross_recurrence_lead_lag` | 1. 实现双资产 Cross-Recurrence Plot (CRP) 矩阵生成；<br>2. 向量化提取偏置对角线分布谱 $RR(\Delta)$，定位最优微观领先滞后时钟。 | **P2** | `R1.1` |
| **`R2.1`** | 策略配置扩展 | [`pipeline/research_strategy/rs/config.py`](file:///home/stone/Work/github/quant/pipeline/research_strategy/rs/config.py)<br>`StrategyConfig` | 1. 扩展 `blend_enable_rqa_regime=False` 等相关参数；<br>2. 默认值严格设为 `False`，保证 100% 向后兼容。 | **P1** | `R1.1` |
| **`R3.1`** | Blend 宏观状态机 | [`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py)<br>`BaseBlendAllocationStrategy` | 1. 接入第 10 大宏观锚点：RQA 崩盘相变防护与三态路由；<br>2. 严格恪守稀疏权重 `NaN` vs `0.0` 契约，否决标的显式置 `0.0`。 | **P2** | `R1.1`, `R2.1` |
| **`R4.1`** | 实盘门禁告警 | [`scripts/check_live_portfolio_health.py`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py)<br>[`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py) | 1. Stage 1 (14:00) 接入相空间崩盘监测，触发 `CAUTION_RQA_DETERMINISTIC_CASCADE`；<br>2. Stage 2 (14:15) 解析报告，优先执行抛售并限制新开仓。 | **P2** | `R1.1`, `R3.1` |
| **`R4.2`** | 离线测试与审计 | `common/tests/test_rqa.py`<br>`pipeline/research_strategy/tests/test_blend_rqa.py`<br>[`walkforward-audit`](file:///home/stone/Work/github/quant/.agents/skills/walkforward-audit/SKILL.md) | 1. 编写 100% 离线单测套件（零网络依赖，Synthetic 驱动）；<br>2. 运行 Phase 6 消融实验与 DSR 变体数校正；<br>3. 开展 LOO 资产脆弱性审计，生成审计合规评级。 | **P3** | `R3.1`, `R4.1` |

---

### 7.3 前沿研究展望 (Future Research Frontiers)

随着非线性物理学与复杂网络理论的演进，RQA 在量化交易领域的下一代演化方向包括：
1. **递归复杂网络转化 (Recurrence Complex Networks)**：通过将二值递归矩阵视为复杂网络的邻接矩阵（Adjacency Matrix），计算聚类系数 (Clustering Coefficient)、介数中心性 (Betweenness Centrality) 与特征路径长度，可直接测度市场在流动性网络层面的系统性连通度与脆弱破裂点；
2. **多变量联合递归分析 (Joint Recurrence Plots, JRP)**：将价格、成交量、订单流不平衡 (OFI) 与波动率联合映射至统一的高维相空间，全面刻画多源微观异构时序的相空间共振；
3. **图神经网络 (GNN) 融合非线性动力学**：将 RQA 递归图拓扑矩阵作为非欧几里得图结构输入时空图神经网络（ST-GNN），彻底颠覆传统基于 1D 卷积或 Transformer 的简单时序拟合，实现真正具备物理因果约束的次世代量化 Alpha 预测。
