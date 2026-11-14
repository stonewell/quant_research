# 中短期量化交易 Alpha 策略构建与细分因子挖掘指南

在中短期量化交易（通常指持仓周期在 **1天至20天** 之间）中，从原始因子（Alpha Factors）转化为能够实盘稳定盈利的有效策略，核心在于**信号提纯、非线性融合、精确的微观结构交易成本建模，以及动态风控**。

---

## 第一部分：从因子到有效 Alpha 策略的完整转化链路

在 A 股或美股等市场中，单纯的线性多因子加权已很难获得超额收益。完整的工业级转化链路分为以下五个核心环节：

### 一、 因子预处理与正交化（信号提纯）
原始因子往往包含大量市场噪声、行业贝塔（Beta）以及彼此间的高度共线性。在进入模型之前，必须进行严格的清洗：

1. **极值处理与标准化**：
   * 使用 **MAD（中位数绝对偏差）** 或分位数缩尾（Winsorization）剔除极端异常值，避免回归或树模型产生过拟合偏移。
   * 截面标准化（Z-Score 或 Rank 秩变换）。中短期交易中，**截面 Rank 归一化**对分布偏态的鲁棒性远高于 Z-Score。
2. **中性化（Neutralization）**：
   * 剥离行业暴露与市值暴露（Barra 风格因子中性化），确保策略赚取的是纯特质性（Idiosyncratic）Alpha，而非风格轮动的 Beta。
   * 对于日内或隔夜高频量价因子，还需剥离**隔夜跳空、换手率以及波动率**等基础量价特征的影响。
   * **小样本截面自由度守卫**：在精简股票池中执行截面多元回归时，
     必须内置样本量门禁（DoF Guard）以防残差过拟合（详见第二部分一、3 与 MS1 规范）。
3. **因子正交化（Orthogonalization）**：
   * 解决多因子共线性问题。相比传统的施密特正交（依赖因子顺序），推荐使用**对称正交（Löwdin Orthogonalization）**，它在消除相关性的同时，最大程度保留了原始因子的物理含义与排序信息。

### 二、 非线性因子合成与特征工程
中短期市场微观结构具有极强的状态依赖性（Regime-dependent），例如“放量上涨”在高位和低位的含义截然不同。

1. **交互特征构造与遗传规划（GP）**：
   * 利用树模型分裂路径或遗传规划（Genetic Programming），将基础量价特征（如订单流不平衡 OFI、筹码分布成本偏离度、特质波动率）与基本面/事件特征（如分析师预期修正、MAX 反转效应）进行非线性交叉。
2. **模型选型与防过拟合验证**：
   * **GBDT 家族（LightGBM / CatBoost）**：中短期截面预测的主力工具。需严格限制树深（`max_depth` 通常在 3~5 之间）并提高叶子节点最小样本数，防止记住特定交易日的偶然噪声。
   * **时空深度学习（GRU / Transformer / 图神经网络）**：若引入高频快照或行业供应链/资金流向图谱（Graph-based Network），可用时序网络捕捉动态特征，并将网络倒数第二层的 Embedding 作为新因子输入下游模型。
   * **严格的交叉验证**：金融时间序列存在自相关与标签重叠（如预测未来5日收益）。必须使用 **Purged Group TimeSeries Cross-Validation（带清洗期的分组时序交叉验证）**，在训练集与验证集之间留出足够的 Isolation Gap（隔离期），并使用**对抗验证（Adversarial Validation）**剔除分布漂移严重的特征。

### 三、 投资组合优化（从预测值到目标权重）
预测出个股的预期收益率（Expected Return, $\alpha$）只是第一步，如何在**高换手率与交易摩擦**之间取得平衡，决定了策略能否活到实盘。

在标准的均值-方差优化（MVO）框架下，必须引入中短期交易特有的约束项：

$$\max_{w} \quad w^T \alpha - \frac{\lambda}{2} w^T \Sigma w - \gamma_{\text{lin}} \|w - w_0\|_1 - \gamma_{\text{imp}} \|w - w_0\|_{1.5}$$

* **协方差矩阵压缩（Covariance Shrinkage）**：样本协方差矩阵在维度高时极不稳定，需采用 Ledoit-Wolf 压缩或基于 Barra 风险模型的结构化协方差矩阵来精确控制组合波动。
* **非线性冲击成本惩罚（Market Impact Penalty）**：中短期策略换手极高（如日换手 30%~80%），除了线性的印花税与佣金（$\|w - w_0\|_1$），必须加入与**个股日均成交额（ADV）和波动率**挂钩的非线性冲击惩罚项（通常呈 $1.5$ 次方或二次方关系），自动抑制对流动性枯竭标的的大额调仓。
* **换手率硬约束与缓冲区（Buffer Rule）**：设置单日总换手上限；或者在排名买入策略中引入“买入阈值（如 Top 5%）与卖出阈值（如跌出 Top 15%）”的缓冲机制，大幅过滤边缘股票反复横跳带来的无效摩擦。

### 四、 执行算法与微观结构优化
中短期策略的理论 Alpha 往往在 5~15 bps（基点）之间，粗糙的市价单执行会直接吞噬全部利润。

1. **预测周期与执行窗口对齐**：
   * 如果因子在开盘集合竞价后衰减极快（如隔夜情绪因子），需采用 **TWAP/VWAP 加速执行**或集合竞价挂单；若因子具有日内均值回归特性，则应结合盘口买卖价差（Bid-Ask Spread）与盘口挂单量（Queue Position）进行被动限价单（Passive Limit Order）撮合。
2. **涨跌停与流动性过滤**：
   * 在存在涨跌停限制的市场中，回测与实盘优化器必须严格剔除一字涨停无法买入、一字跌停无法卖出的标的，避免回测出现“纸面富贵”。

### 五、 归因分析与 Alpha 衰减监控
策略上线后，需建立闭环的监控体系以应对市场适应性进化（Alpha Decay）：

* **事后归因（Ex-post Attribution）**：结合 **Barra 风险归因**与 **Brinson 归因**，拆解每日收益究竟来自纯 Alpha、行业偏离、风格暴露（如小市值/高波），还是交易执行滑点。
* **多维度衰减预警**：
  * 监控滚动窗口下的 **RankIC 均值、ICIR** 以及**多头端超额收益（Top-Group Excess Return）**——有时整体 IC 依然稳定，但空头端贡献了 80% 的 IC，而多头端已完全失效（在融券受限市场中这意味着策略实质性失效）。
  * 追踪**因子拥挤度（Factor Crowding）**：通过观察高因子得分股票池的估值离散度、换手率集中度及相关性飙升情况，在发生集体踩踏（Quant Quake）前主动降仓。

---

## 第二部分：特定市场适配与细分微观结构因子挖掘

在 A 股等具有鲜明微观结构特征的市场（如 T+1 交易制度、融券受限、涨跌停限制以及散户博弈占比高）中，必须将**制度摩擦转化为因子特征**，并在微观结构中提取深层信号。

### 一、 特定市场制度与微观结构适配（以 A 股纯多头/指增为例）

#### 1. 融券受限下的“非对称 Alpha 筛选”
在融券成本高、券源稀缺的环境中，传统的全市场 RankIC 具有极大欺骗性——
许多因子的 IC 主要由**空头端（暴雷股、极端高估值垃圾股）贡献**，而多头端毫无超额收益。
* **多头端独立评估指标**：放弃单纯看全截面 IC，改为重点追踪
  **Top 组超额收益（Top-Group Excess Return）**、**多头端单调性（Monotonicity）**
  以及 **Top 50/100 组合的换手调整后夏普比率**。
* **标签改造（Label Engineering）**：训练 LightGBM 等模型时，不对全市场收益率做
  均方误差（MSE）拟合，而是采用**非对称损失函数**（加重多头端预测错误的惩罚），
  或直接将目标变量定义为截面分位数排名 $\text{Rank}(R_{t+1, t+5})$，
  迫使树模型把分裂节点集中在头部优质标的的区分上。

#### 2. 涨跌停与 T+1 制度的因子化处理
* **涨跌停磁吸效应（Magnet Effect）与封单强度**：股价接近涨停（如涨幅 $>8\%$）时，
  T+1 制度会引发追涨资金的流动性抢筹；而炸板（触及涨停后回落）则是极强的次日低开信号。
  可将**触及涨停次数、封单金额占流通市值比、开板次数与炸板后成交量衰减率**
  直接构造为状态变量，与传统动量因子做交互。
* **隔夜与日内收益的强拆分（Overnight vs. Intraday）**：由于 T+1 限制，当日买入无法当日卖出，
  导致**机构与知情交易者倾向于在尾盘（最后 30 分钟）或隔夜集合竞价调仓**，
  而散户情绪集中在早盘前 30 分钟释放。因此，必须将日收益率拆解为：
  $$R_{\text{overnight}} = \frac{P_{\text{open}, t}}{P_{\text{close}, t-1}} - 1, \quad R_{\text{intraday}} = \frac{P_{\text{close}, t}}{P_{\text{open}, t}} - 1$$
  在多数中短期策略中，剥离早盘前 30 分钟噪声后的**尾盘动量**与**隔夜跳空反转**具有截然不同的半衰期。

#### 3. 截面回归残差纯化与小样本自由度守卫（DoF Guard，吸取 M2.1 经验）
在纯特质 Alpha 信号提纯中，通常通过截面多元 OLS 回归剥离行业哑变量与连续风险暴露：
$$f_i = \sum_{s=1}^{S} \beta_s \cdot \text{Industry}_{s, i} + \sum_{k=1}^{K} \gamma_k \cdot \text{Risk}_{k, i} + \epsilon_i$$
其中残差 $\epsilon_i$ 即为剥离行业与连续风险因子后的特质 Alpha 信号。

* **小样本截面自由度守卫（DoF Guard）**：
  当策略运行在核心卫星精简池（如 $N=20\text{–}22$ 只标的却横跨 10+ 个行业）时，
  若直接将全部行业哑变量与 4 个连续风险暴露并入多元 OLS，会导致残差自由度
  $N - K - 1 \le 4$ 并产生严重截面过拟合与数值病态。
  因此 `cs_neutralize_ols`（`MS1`）必须内置严格的样本量门禁：
  1. **完整回归门禁**：仅当有效截面标的数 $N \ge \max(30, 3K)$（如百股池
     `core_satellite_100_stocks`）时，启用完整行业哑变量 + 连续暴露多元 OLS；
  2. **自动剥离降级**：当 $10 \le N < \max(30, 3K)$ 时，自动剥离高维行业哑变量矩阵，
     仅对连续暴露向量执行低维正交回归（行业中性交由 `M2.1` 的 `sector_neutral_demean`
     或 `M1.3` 的 QP 行业偏离约束处理）；
  3. **低样本退化**：当 $N < 10$ 时，退化为基础截面 z-score 标准化。

---

### 二、 三类高含金量细分因子工程化构造

#### 1. 订单流不平衡（Order Flow Imbalance, OFI）与逐笔成交微观因子
相比粗颗粒度的日线量价，基于 Level-2 盘口快照（3秒一帧）与逐笔成交（Tick）构建的资金流因子能更早捕捉主力建仓痕迹。

* **多档位加权订单流不平衡（Multi-level OFI）**：
  衡量买卖盘口挂单净增量的变化，比单纯的“主动买入额 - 主动卖出额”更能反映盘口真实支撑力度。对于第 $k$ 档盘口，定义买方盘口变化 $\Delta V_{t}^{B,k}$ 与卖方盘口变化 $\Delta V_{t}^{A,k}$：
  $$\Delta V_{t}^{B,k} = \begin{cases} V_{t}^{B,k}, & P_{t}^{B,k} > P_{t-1}^{B,k} \\ V_{t}^{B,k} - V_{t-1}^{B,k}, & P_{t}^{B,k} = P_{t-1}^{B,k} \\ -V_{t-1}^{B,k}, & P_{t}^{B,k} < P_{t-1}^{B,k} \end{cases}$$
  同理计算卖方 $\Delta V_{t}^{A,k}$（价格上涨为负、下跌为正）。将前 5 档按距离中间价的权重 $w_k$ 聚合，并用当日总成交量标准化：
  $$\text{OFI}_{\text{norm}} = \frac{\sum_{t} \sum_{k=1}^{5} w_k (\Delta V_{t}^{B,k} - \Delta V_{t}^{A,k})}{\text{Volume}_{\text{day}}}$$
  *逻辑内核*：剔除高频撤单干扰后，持续的正向 $\text{OFI}_{\text{norm}}$ 配合股价横盘，往往预示着隐蔽的冰山买单（Iceberg Orders）正在吸筹。

* **大小单资金流背离（Smart Money Divergence）**：
  根据逐笔成交还原单笔母单金额，区分机构大单（如 $>20$ 万元）与散户小单（如 $<4$ 万元）。计算过去 $N$ 日**大单净流入率与小单净流入率的相关系数或差值**。当股价下跌但大单逆势净流入、小单恐慌流出时，构成极强的底部反转 Alpha。

#### 2. 筹码分布（Volume Profile）与博弈成本因子
行为金融学中的**处置效应（Disposition Effect）**——投资者倾向于过早卖出盈利股票、死拿亏损股票——在散户占比高的市场中尤为显著。筹码分布因子正是用来量化当前全市场持仓者的盈亏状态与抛压阻力。

* **换手率衰减加权筹码分布（Turnover-Decayed Volume Profile）**：
  假设每日换手率 $TR_t$ 等比例替换了历史所有价格区间的筹码。对于第 $t-k$ 日的成交量分布（可用当日成交均价 VWAP 或日内分时成交分布近似），其在第 $t$ 日的留存权重为：
  $$W_{t, t-k} = TR_{t-k} \prod_{j=0}^{k-1} (1 - TR_{t-j})$$
* **核心衍生因子公式**：
  1. **获利盘比例（Winner Ratio, WR）**：当前收盘价 $P_t$ 下方留存筹码的权重之和。当 WR 突破 $85\% \sim 90\%$ 且近期换手充分时，上方套牢盘抛压极轻，极易触发趋势加速。
  2. **平均持仓成本偏离度（Cost Deviation）**：
     $$\text{CostDev}_t = \frac{P_t - \sum_{k=0}^{M} W_{t, t-k} \cdot \text{VWAP}_{t-k}}{\text{VWAP}_{t-k}}$$
     衡量全市场平均浮盈/浮亏幅度。
  3. **筹码峰集中度（Chip Concentration）**：计算留存筹码分布的 $90\%$ 分位价格与 $10\%$ 分位价格之差除以中位数价格：$\frac{P_{90\%} - P_{10\%}}{P_{50\%}}$。该值越低，说明筹码在窄幅区间内高度锁定，一旦放量突破，爆发力极强。

#### 3. 图神经网络（GNN）与关联网络动量溢出因子
传统截面模型假设股票之间是独立同分布（i.i.d.）的，忽略了资金在板块、产业链或同概念标的之间的轮动传导效应。

* **动态关联图构建（Graph Construction）**：
  除了静态的申万/中信行业分类与供应链上下游图谱，中短期交易中更有效的是**资金共现图（Co-occurrence / Lead-Lag Graph）**：
  * 计算过去 20 个交易日个股日内分钟级收益率的**偏相关系数（Partial Correlation）**或 **格兰杰因果检验（Granger Causality）**，构建稀疏邻接矩阵 $A_{ij}$（保留相关性最高的 Top-K 邻居并归一化）。
* **邻居动量溢出因子（Peer Momentum Spillover）**：
  即使不训练复杂的端到端图神经网络，仅通过一阶图卷积聚合（Graph Aggregation）即可生成极强的 Alpha 因子：
  $$\text{PeerMom}_i = \sum_{j \in \mathcal{N}(i)} \tilde{A}_{ij} \cdot R_{j, t-1} - R_{i, t-1}$$
  *逻辑内核*：当与股票 $i$ 具有高度资金联动性的龙头或关联股 $j$ 已经启动大涨，而股票 $i$ 尚未补涨（$\text{PeerMom}_i$ 显著为正）时，资金的比价效应会驱动 $i$ 在未来 $1 \sim 3$ 个交易日产生显著的补涨超额收益。
* **多阶特征交互与遗传规划（GP）喂入**：
  将 $\text{OFI}_{\text{norm}}$、$\text{CostDev}_t$ 和 $\text{PeerMom}_i$ 作为基础算子输入遗传规划引擎（如 `gplearn` 或自研 C++ 表达式树引擎），限制表达式树深度（`depth <= 4`）以防止过拟合，自动挖掘类似 `Rank(PeerMom) * Ts_Delta(Chip_Concentration, 5)` 的高阶复合因子。

---

## 第三部分：与本代码库现有架构及 S/W/MS 轨道的工程映射规范

为确保本指南中的量化理论模型在代码库中具备可落地性、零概念漂移与严格的因果防线，本部分定义
中短期 Alpha 五阶段链路、微观结构因子与现有系统各模块的映射关系与工程规范。

### 一、 理论阶段与 MS1–MS8 工程模块映射表

对照代码库工程架构与
[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.26，
理论五阶段与三大微观因子在工程中的原子模块落位映射如下：

| 理论阶段 / 因子模块 | 核心理论内核与数学模型 | 工程任务编号 | 对应代码库文件与关键符号 | 核心交付功能与输出物 |
| :--- | :--- | :---: | :--- | :--- |
| **第一阶段：信号提纯与正交** | MAD 缩尾、截面秩变换、行业/市值 OLS 剥离、Löwdin 对称正交 | **`MS1`** | `common/cross_sectional.py`<br>`common/sector_metadata.py` | `cs_mad_winsorize`、`cs_rank_gaussian_or_uniform`、`cs_neutralize_ols`、`cs_lowdin_orthogonalize` |
| **第二阶段：筹码博弈成本** | 换手衰减持仓成本偏离度与 90-10 分位距筹码峰集中度 | **`MS2`** | `common/structural_factors.py`<br>`common/financial_api.py` | `cyq_cost_deviation`、`cyq_quantile_spread_90_10`；BaoStock 日线透传真实换手率 `Turn` |
| **第二阶段：网络动量溢出** | 偏相关精度矩阵 $\Omega = \Sigma_{\text{LW}}^{-1}$ 稀疏图与一阶图卷积动量溢出 | **`MS3`** | `common/peer_network.py`<br>`common/covariance.py` | `common/covariance.py::ledoit_wolf_shrinkage`（前移至 Phase P0 供精度矩阵求逆）、`build_partial_corr_adjacency`、`compute_peer_momentum_spillover` (`PeerMom`)、复合因子 |
| **第二阶段：微观资金流拆分** | 隔夜/日内收益强拆分、分板涨跌停磁吸炸板、5m OFI 与聪明钱背离 | **`MS4`** | `common/daily_factors.py`<br>`common/intraday_features.py` | `overnight_return`、`intraday_return`、`limit_up_magnet_and_break`、`Core_Intraday_Ret_5m`、`OFI_Norm_5m`、`smart_money_flow_divergence` |
| **第五阶段：归因与衰减监控** | 融券受限纯多头非对称评价 (Top IC/单调性)、因子拥挤度预警 | **`MS5`** | `common/factor_analyzer.py`<br>`common/factor_crowding.py` | `asymmetric_long_only_factor_summary` (告警 `SHORT_LEG_ILLUSION`)、`compute_factor_crowding_metrics` (分级预警) |
| **第三阶段：凸优化与冲击控制** | Almgren-Chriss $L_{1.5}$ 冲击惩罚凸优化与稳健协方差源接入 | **`MS6`** | `common/allocation_templates.py`<br>复用 `common/covariance.py` | 直接复用 MS3 交付的 `ledoit_wolf_shrinkage` 与 M1.3 底座，实现 SLSQP 求解器 `_alpha_impact_penalized_weights`（$L_{1.5}$ 冲击平滑），作为 `min_variance`/`hrp` 可选稳健协方差源 |
| **全链路复合策略模板** | 串联 MS1–MS6 原语，集成 RankBand 双阈值缓冲与非线性冲击优化 | **`MS7`** | `pipeline/research_strategy/rs/microstructure_alpha_strategy.py`<br>`common/selection_band.py` | `MicrostructureAlphaCompositeStrategy` (`version = "1.0.0"`，注册至 `STRATEGY_CLASS_MAP`) |
| **高级演进与实盘护栏接线** | GP 算子扩充、LightGBM 秩标签与对抗验证、实盘 Stage-1 拥挤度挂载 | **`MS8`** | `ml/alpha_mining/` (`ML1`)<br>`scripts/check_live_portfolio_health.py` | LightGBM 非对称损失与对抗验证 (AUC > 0.65 过滤)；Stage-1 健康检查接入 `factor_crowding` Advisory 观测 |

---

### 二、 双套 NaN 契约硬规范（因子层 vs 策略权重层）

代码库在底层因子计算与上层资产配置之间存在严格的**语义隔离**，全流程必须恪守双套 `NaN` 契约：

1. **因子计算层契约 (`MS1`–`MS4` 算子层)**：
   - **停牌与缺失保留 `NaN` 铁律**：对于停牌无交易、上市初期历史不足或处于滑动窗口预热期的资产单元，
     因子值**必须保持为 `NaN`**；
   - **严禁填充 `0.0`**：绝对禁止通过 `fillna(0.0)` 处理缺失值。在横截面排序（`Rank`）或标准化中，
     `0.0` 会被当成有效的数值信号（如中性或特定分位数），直接扭曲截面排名的均值与离散度，
     导致停牌股被错误建仓或评分失真。
2. **策略权重生成层契约 (`MS7` 策略层)**：
   - **调仓日退场显式写入 `0.0` 铁律**：策略调仓行中，
     任何被主动剔除、触发硬止损或生命周期转为出场的标的，
     其目标权重**必须显式写入 `0.0`**，绝对禁止留为 `NaN`；
   - **防范全局 `ffill()` 泄漏**：回测系统（`common/allocation_backtester.py`）使用
     `sparse_weights.ffill().fillna(0.0)` 对全截面做列向前向填充。若调仓日为 `NaN`，
     回测器会将该标的上一调仓日的历史持仓无条件延续，
     导致系统在不知情状态下持续持有本应清仓的已止损或破位标的。

---

### 三、 5 分钟列式存储与日频降级代理对齐规范

为了充分利用本地高性能列式存储并保证跨资产环境的连续回测能力，高频微观因子采用双轨机制：

1. **DuckDB 5分钟高频列式存储运行轨**：
   - `OFI_Norm_5m`（归一化订单流不平衡）、
     `Core_Intraday_Ret_5m`（剥离早盘 30 分钟散户情绪噪声的核心日内收益）
     与 `iceberg_accumulation_score`（横盘吸筹冰山指数）基于 `data/cache.duckdb` 中的
     `intraday_ohlcv_bars` 表运行；
   - 依赖 `common/intraday_features.py` 原语（如 `load_intraday_5m_universe` 与
     `enrich_universe_with_intraday_features`）批量读取，并受 `intraday_cutoff_time`
     （默认 14:00/14:15）因果截断约束，严禁读取当日未发生的未来分钟线。
2. **离线与缺乏 5m 数据时的日频平滑降级代理轨**：
   - 当标的缺失 5 分钟历史线（例如宽基指数、美股离线合成回测或早期数据）时，
     算子必须自动激活平滑降级代理：
     - `Core_Intraday_Ret_5m` 降级为基于日频开盘/收盘价的
       `intraday_return = Close / Open - 1.0`；
     - `OFI_Norm_5m` 降级为由日内价格振幅与成交量加权的日频伪订单流代理
       $\text{Proxy\_OFI}_t = \frac{C_t - O_t}{H_t - L_t + \epsilon} \times \text{Volume}_t$；
     - `smart_money_flow_divergence` 降级为基于日内 VWAP 与尾盘价格走势的量价背离代理；
   - 降级代理保证回测全流程零崩溃、无静默断流，并输出诊断日志注明代理激活状态。

---

### 四、 与存量策略体系的协同边界及历史实证防呆守则（对照已结项任务裁决）

`MS1`–`MS6` 产出的核心原语与凸优化求解器在工程架构上保持对存量系统的严格隔离。
为杜绝此前已被实证否决的侵入式改造并保护现有已投产旗舰策略的确定性表现，
确立以下协同边界与防呆守则：

#### 1. 直接复用已闭环存量基建（零重复造轮子）
在研发新建独立策略 `MS7`（`MicrostructureAlphaCompositeStrategy`）时，
严禁重复开发已有基础设施，一律通过正交接口直接复用以下已闭环生产级组件：
- **选股与排名缓冲机制**：直接复用 `M1.2` 已落地的
  `common/selection_band.py::RankBandSelection` (`v1.0.0`)，严格沿用其双阈值买卖缓冲带、
  停牌 `NaN` 保仓逻辑以及生命周期退场显式写入 `0.0` 规范；
- **行业映射与持仓状态机**：直接复用 `M2.1` 的离线行业元数据层
  `common/sector_metadata.py` 与 `M1.1` 的统一持仓生命周期状态机
  `common/position_lifecycle.py` (UPLSM)；
- **凸优化与协方差收缩求解**：直接复用 `MS3`（Phase P0）交付的
  `common/covariance.py::ledoit_wolf_shrinkage` 与 `M1.3`（`_turnover_penalized_weights`，
  `MinimumVarianceAllocation v1.1.0` 内置 $L_1$ 换手惩罚与现金盒约束）底座；
  `MS6`（Phase P1）专注实现
  `common/allocation_templates.py::_alpha_impact_penalized_weights`
  （带 $L_{1.5}$ 非线性市场冲击成本平滑项的 SLSQP 求解器），并作为
  `min_variance` 与 `hrp` 的可选稳健协方差源（消除小样本协方差奇异与
  `HierarchicalRiskParityAllocation` 退化聚类 `IndexError`），
  保持存量模板默认行为与版本号不变。

#### 2. 严守历史负面实证禁区（明令禁止的侵入式改造）
对照代码库历史实证评测（`LV8`、`LV20`、`C4.1`、`C5.1`、`C5.2`、`C6.1`、`S3.1` 与 `LV18`），
严禁在 Track MS 中实施以下侵入式改造：

- **禁区一（`LV8` / `LV20` / `version.md` §7 铁律）：**
  **严禁篡改 `ChanFourStateBlendStrategy` (`cfsb`) 三腿结构或强塞第 4 条 `"reversal"` 风格腿**：
  1. `LV20` 三组窗口实测（1134 根日线回测/实盘池占 5.38%–5.64%、最长连续 8–11 bar；
     687 根日线实盘窗口占 4.66%、最长连续 6 bar）均证实 `reversal` 制度占比不足 6%，
     远低于 10% 最小有效性门槛；
  2. `blend_base.py:575-578` 内置的自动重归一化机制能自动将空桶预算按比例分配至非空桶，
     绝无预算漏入现金端的结构性缺陷；
  3. `crb`（`chan_risk_managed_blend`）运行 `rolling_ic_ir` 本身不走风格分桶；强行塞入第 4 条腿只会
     破坏已被 walkforward 多折验证的稳态结构。
- **禁区二（`C4.1` / `C5.1` / `C5.2` 零交易死锁教训 & `LV8`）：**
  **严禁在既有策略上叠加硬前置过滤或额外棘轮止损**：
  1. 严禁在 `ChanVolumeProfileHybridStrategy` 或 `ChanFourStateExecutionStrategy` 上叠加硬形态过滤或
     额外棘轮止损（`C4.1`/`C5.2` 曾因叠加过严硬门槛导致全样本 0 交易死锁；`LV8` 实测证实
     `ChanFourStateExecutionStrategy` 在 24 次实盘观测中 22 次为 0）；
  2. `MS2` 导出的 `cyq_cost_deviation` 与 `cyq_quantile_spread_90_10` 仅作为连续截面因子服务于新建
     独立策略 `MS7`，不得以硬阈值形式反向污染存量策略逻辑。
- **禁区三（`S3.1` / `C6.1` / `LV18` / `MS8` 边界）：**
  **严禁在 Track MS 中侵入修改 `blend_base.py`**：
  1. 宏观与资产级 SuperTrend 锚点 7/8 早已归口于 `S3.1`（`Tier P5` 孵化池），
     与当前微观 Alpha 轨道正交；
  2. 锚点 2 在 6/6 Blend 中默认全开，不可加入成本乖离硬否决以免误杀占 91% 时间的动量期强势龙头；
  3. `MS5` 拥挤度监控严格遵循 `MS8` 规范，作为 Stage-1 `gates_orders: False` 只读
     Advisory 预警及 `MS7` 策略内可选缩放项，杜绝 `LV18` 披露的与底座
     Garman-Klass Vol Target 产生双重降仓叠加风险；
  4. 底座保持 `BASE_VERSION = 2.0.0`，现役 58 份存量策略 dump 与回测基线零变动。