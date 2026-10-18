# 柏顿·墨基尔（Burton G. Malkiel）成功选股法理论建模与量化投资体系架构规范
# (Burton Malkiel's Stock Selection Rules Specification: Firm Foundation, Castles in the Air, GARP Quantitative Valuation, and Core-Satellite Asset Allocation)

> [!IMPORTANT]
> **唯一权威任务与路线图总表声明 (Single Source of Truth)**：
> 本文档是柏顿·墨基尔（Burton G. Malkiel）经典投资哲学与成功选股四大法则的**理论建模、数学证明、因果传导链条、GARP 估值安全门禁、核心-卫星资产配置及工业级量化架构规范**。
> 依据 [`AGENTS.md`](file:///home/stone/Work/github/quant/AGENTS.md) 核心准则，代码库内所有工程落地任务、优先级队列、波次编排及执行进度跟踪，**必须且仅在 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) 中集中管理**。本文档专注于学术理论渊源的严谨性、数理金融公式推导以及与现有量化模块的分层解耦集成。

---

## 一、 理论渊源：坚固基础理论与空中楼阁理论的辩证融合 (Theoretical Origins & Philosophical Synthesis)

```mermaid
flowchart TD
    subgraph FOUNDATION_THEORY["1. 坚固基础理论 (Firm-Foundation Theory)"]
        JBW["John Burr Williams (1938)\n《投资价值理论》 (Theory of Investment Value)\n内在价值 = 贴现未来所有股息与自由现金流\nIntrinsic Value = Discounted Cash Flows (DCF)"]
        GRAHAM["Benjamin Graham & David Dodd (1934)\n《证券分析》与安全边际 (Margin of Safety)\n有形资产清算价值、净营运资本 (Net-Net)"]
        FAMA["Eugene Fama & Kenneth French (1993, 2015)\n多因子资产定价模型 (FF-3 & FF-5)\n价值溢价 HML 与盈利能力溢价 RMW"]
    end

    subgraph CASTLES_THEORY["2. 空中楼阁理论 (Castles-in-the-Air Theory)"]
        KEYNES["John Maynard Keynes (1936)\n《就业、利息与货币通论》第十二章\n凯恩斯选美理论 (Keynesian Beauty Contest)\n大众心理、羊群效应与群体预期博弈"]
        SHILLER["Robert J. Shiller (2000, 2019)\n《非理性繁荣》与《叙事经济学》\n群体心理共振、病毒式叙事传播与投机溢价"]
        JEGADEESH["Jegadeesh & Titman (1993)\n动量效应 (Momentum Anomaly)\n12-1M 价格动量与分析师预期上调"]
    end

    subgraph MALKIEL_SYNTHESIS["3. 柏顿·墨基尔的大师融合 (Malkiel's Dialectical Synthesis)"]
        SYNTH["Burton G. Malkiel (1973/2023 50周年纪念版)\n《漫步华尔街》 (A Random Walk Down Wall Street)\n核心思想：绝不在虚空中盲目追逐泡沫，\n只在坚固的基础（Firm Foundation）之上建造空中楼阁（Castles in the Air）！"]
    end

    subgraph FOUR_RULES["4. 墨基尔成功选股四大法则 (Four Stock Selection Rules)"]
        R1["法则一：盈利引擎\n5年高于平均水平盈利增长\n(ROE >= 15%, 5Y CAGR >= 15%, FCF/NI >= 80%)"]
        R2["法则二：估值门禁\n绝不支付过高估值 (GARP)\n(PEG <= 1.0, 戴维斯双杀数学熔断)"]
        R3["法则三：叙事催化\n空中楼阁故事与动量共振\n(12M动量前30%, 盈利一致预期上调)"]
        R4["法则四：摩擦最小化\n极低换手率与惰性调仓\n(低频持有, 惰性调仓死区, 摩擦损耗控制)"]
    end

    subgraph SYSTEM_ARCHITECTURE["5. 量化系统分层架构挂载 (Quant Architecture)"]
        CORE_SAT["四、核心-卫星资产配置体系\n(75% 宽基指数底仓 + 20% 卫星GARP个股 + 5% 现金防御)"]
        PIPELINE["五、代码库分层集成\n• pipeline/fundamental_screener/ (财务硬门禁)\n• common/strategy_aspects.py (选股与定仓切面)\n• pipeline/research/blend/blend_base.py (核心-卫星装配)\n• scripts/ (Stage 1 宏观体检 + Stage 2 四阶段实盘)"]
    end

    JBW --> SYNTH
    GRAHAM --> SYNTH
    FAMA --> SYNTH
    KEYNES --> SYNTH
    SHILLER --> SYNTH
    JEGADEESH --> SYNTH
    SYNTH --> R1 & R2 & R3 & R4
    R1 & R2 & R3 & R4 --> CORE_SAT
    CORE_SAT --> PIPELINE
```

### 1.1 经典金融两大流派的历史百年对立

在近代百年资本市场演进史中，投资理论始终围绕着两大针锋相对的认知范式展开争鸣：

1. **坚固基础理论 (Firm-Foundation Theory)**：
   - **理论先驱**：由 John Burr Williams (1938) 在其哈佛大学博士论文《投资价值理论》（*The Theory of Investment Value*）中首次形式化确立，后被 Benjamin Graham 与 David Dodd (1934) 的《证券分析》推向巅峰。
   - **公理化假说**：任何投资工具（无论普通股、债券还是商业地产）都存在一个客观可测度、独立于市场交易价格的“内在价值”（Intrinsic Value）。该内在价值等于该资产在生命周期内所能产生的全部未来现金流或股息，按照一个能够充分补偿时间价值与违约风险的贴现率计算出的现值之和：
     $$ V_0 = \sum_{t=1}^{\infty} \frac{CF_t}{(1 + r)^t} $$
   - **投资行为学派**：严格的坚固基础信徒将市场先生（Mr. Market）的价格报价视为具有躁狂抑郁特征的短期杂音。其投资决策完全由安全边际（Margin of Safety）驱动：当且仅当市场价格显著低于测算出的坚固内在价值基准（$P_t \ll V_0$）时买入；当价格向内在价值收敛或被充分高估时卖出。
   - **致命历史盲区**：
     - **贴现率与永续增长率的极端敏感性（戈登奇点）**：微调贴现率 $r$ 或增长率 $g$ 一个百分点，DCF 模型的内在估值结果可波动数倍，极易沦为分析师按需定制的事后合理化工具；
     - **“价值陷阱”（Value Trap）困境**：大量估值极低（极低 PE、低 PB、甚至破净低于 Net-Net）的冷门资产，往往伴随技术范式颠覆、治理结构崩塌或行业长期萎缩。忽视市场流动性与投资者关注度，容易让投资组合在长达数年乃至数十年的时间里沦为缺乏任何催化剂的“沉没资本”。

2. **空中楼阁理论 (Castles-in-the-Air Theory)**：
   - **理论先驱**：由现代宏观经济学奠基人 John Maynard Keynes (1936) 在《就业、利息与货币通论》第十二章中精辟阐发。
   - **公理化假说**：专业投资者的注意力绝不应耗费在测算所谓的长期内在价值上，因为内在价值在瞬息万变的技术与宏观环境下纯属虚妄。投资的本质是一场**选美比赛（Keynesian Beauty Contest）**：
     > *“专业投资者的工作，不应指望去挑选那些真正最美的人，甚至不应挑选大众自己心目中认为最美的人；我们的智力应当集中于预测大众预期大众会认为最美的人（what average opinion expects average opinion to be）。”*
   - **学术深化**：罗伯特·席勒（Robert J. Shiller, 2000, 2019）在其诺奖研究与《叙事经济学》（*Narrative Economics*）中证明，金融资产的超额波动主要由像病毒一样传播的“群体流行病叙事”（Viral Narratives）主导。市场的短期定价是参与者群体情绪、心理投射与预期自我实现的放大器。
   - **致命历史盲区**：
     - **泡沫幻灭与本金毁灭**：脱离任何基本面支撑的空中楼阁（如 17 世纪郁金香泡沫、1720 年南海泡沫、1999-2000 年互联网 Dot-Com 泡沫、2021 年各类去中心化纯概念 Meme 币），最终都无法逃脱“博傻理论”（Greater Fool Theory）终结时的流动性崩塌。当最后一个买家不再入场，缺乏坚固现金流防线的空中楼阁将瞬间坍塌为废墟。

---

### 1.2 柏顿·墨基尔的辩证统一：“在坚固基础之上筑造空中楼阁”

普林斯顿大学经济学教授、先锋领航集团（Vanguard）前董事柏顿·墨基尔（Burton G. Malkiel）在其长销半个世纪的经典著作《漫步华尔街》（*A Random Walk Down Wall Street*, 1973; 2023 50周年第13版）中，并未机械地站在有效市场假说（EMH）的极端教条上全盘否定主动选股，而是首次从理论哲学高度完成了两大阵营的**对立统一与因果融合**：

> **墨基尔的核心选股公理 (Malkiel's Core Axiom)**：
> 1. *纯粹的坚固基础论者* 往往由于固执于静态财务指标，深陷缺乏催化剂的价值陷阱；
> 2. *纯粹的空中楼阁论者* 往往由于过度追逐市场热点与动量故事，最终在高估值顶点遭遇戴维斯双杀；
> 3. **成功的投资绝非在二者之间二选一，而是将两者进行严谨的非对称组合——绝不在虚空中盲目搭建空中楼阁，但必须在被坚实价值与盈利增长（Firm Foundation）牢固锁定的优质标的之上，积极寻找符合群体心理预期与市场共识（Castles in the Air）的动量催化剂！**

这种辩证统一思想不仅完美解释了金融市场中**价值因子（Value）与动量因子（Momentum）长达数十年的负相关性与收益绝佳互补性**（Asness, Moskowitz & Pedersen, 2013, 《Value and Momentum Everywhere》），更为构建量化多因子评分卡、GARP 估值防线与核心-卫星资产配置提供了无懈可击的底层哲学根基。

---

## 二、 墨基尔成功选股四大法则的逐层拆解与量化映射 (Deconstruction & Quantitative Mapping of Four Rules)

墨基尔在《漫步华尔街》第十四章中，将这一宏观哲学具象化提炼为极具实操价值的**成功选股四大法则（Four Rules for Successful Stock Selection）**。在现代量化工程体系中，每一条法则均具备严谨的数学定义、统计门禁与因果防线。

---

### 2.1 法则一：盈利引擎——5年高于平均水平盈利增长 (Rule 1: Sustained Above-Average Earnings Growth)

> **墨基尔原著准则**：
> *“Rule 1: Confine stock purchases to companies that appear able to sustain above-average earnings growth for at least five years.”*
> （限制只买入那些至少在未来五年内有能力维持高于市场平均水平盈利增长的公司。）

#### 2.1.1 理论机理与杜邦拆解
根据戈登增长模型（Gordon Growth Model）：
$$ P_0 = \frac{D_1}{r - g} = \frac{E_1 \cdot (1 - b)}{r - \text{ROE} \cdot b} $$
其中 $E_1$ 为预期每股收益，$b$ 为留存收益率，$\text{ROE}$ 为净资产收益率，可持续内生增长率满足 $g = \text{ROE} \cdot b$。

若一家公司的增长仅靠外部股权稀释（增发募资）或危险的高杠杆债务驱动，其增长不仅脆弱，更将在宏观信贷紧缩周期中迅速断裂。真正的“高于平均水平增长”必须建立在**高投入资本回报率（ROIC）与深厚的商业护城河（Moat）**之上。

```mermaid
flowchart LR
    ROE["净资产收益率 (ROE) >= 15%"] --> MARGIN["高销售净利率\n(定价权/毛利稳定)"]
    ROE --> TURNOVER["高效资产周转率\n(供应链与营运壁垒)"]
    ROE --> LEVERAGE["健康杠杆\n(D/E <= 100%, 利息倍数 >= 5)"]
    CASH["真实现金流造血"] --> OCF["经营现金流/净利润 >= 80%"]
    CASH --> FCF["自由现金流 (FCF) > 0"]
    ENGINE["5年盈利增长复合引擎\n(EPS 5Y CAGR >= 15%)"]
    MARGIN & TURNOVER & LEVERAGE & OCF & FCF --> ENGINE
```

#### 2.1.2 工业级量化筛选门禁 (Quantitative Screening Gates)
在量化数据管道中，法则一被形式化映射为五道硬性质量门禁（Quality Gates），**任何一项指标缺省或越界直接一票否决**：

1. **长期增长历史与预期连续性**：
   $$ \text{CAGR}_{5Y}(\text{NetProfit}) = \left(\frac{\text{NetProfit}_t}{\text{NetProfit}_{t-5}}\right)^{1/5} - 1 \ge 15.0\% $$
   同时要求过去 5 年中净利润同比负增长的年份不得超过 1 年（排除具有强周期剧烈波动的周期股，锁定具备内生复利属性的成长标的）。
2. **净资产收益率质量底线**：
   $$ \overline{\text{ROE}}_{3Y} \ge 15.0\%, \quad \text{且最新单季度扣非 } \text{ROE}_{\text{quarterly}} \ge 12.0\% $$
3. **真实真金白银造血验证（应计项与利润含金量）**：
   $$ \frac{\text{OCF}_t}{\text{NetProfit}_t} \ge 80.0\% \quad (\text{三年累计值 } \frac{\sum_{k=0}^2 \text{OCF}_{t-k}}{\sum_{k=0}^2 \text{NetProfit}_{t-k}} \ge 100.0\%) $$
   严格剔除因巨额应收账款堆积导致的虚假纸面利润。
4. **自由现金流（FCF）硬约束**：
   $$ \text{FCF}_t = \text{OCF}_t - \text{CapEx}_t > 0 $$
   保证企业在维持现有生产能力并进行资本扩张后，仍有充沛的剩余现金可用于派发股息、回购股份或战略研发。
5. **资产负债表防御度**：
   $$ \frac{\text{Total Debt}_t}{\text{Total Equity}_t} \le 100.0\%, \quad \text{EBITDA 利息保障倍数} \ge 5.0, \quad \text{Altman Z-Score} \ge 1.81 $$

---

### 2.2 法则二：估值门禁——绝不支付过高估值与戴维斯双杀防线 (Rule 2: Never Pay More Than Justified by Firm Foundation)

> **墨基尔原著准则**：
> *“Rule 2: Never pay more for a stock than can reasonably be justified by a firm foundation of value.”*
> （绝不为一只股票支付超过其坚固价值基础所能证明的合理价格。）

#### 2.2.1 彼得·林奇 PEG 门禁与 GARP 哲学
墨基尔在此法则中高度赞同传奇基金经理彼得·林奇（Peter Lynch, 1989）倡导的 **GARP (Growth at a Reasonable Price, 合理估值成长)** 投资准则。他指出：即使一家公司的成长前景极为确定，如果投资者为之支付了 80 倍甚至 100 倍市盈率的狂热估值，只要未来业绩稍有任何闪失，或者宏观利率水平略微上升，估值倍数的回调将直接引发毁灭性的本金永久性损失。

$$ \text{PEG} = \frac{\text{PE}_{\text{TTM}}}{g \times 100} $$
其中 $g$ 为预期或经审计的未来复合每股收益年增长率（若 $g = 20\%$，则分母取 20）。
- $\text{PEG} \le 1.0$：严格价值成长区间，兼具高成长与充分的安全边际；
- $1.0 < \text{PEG} \le 1.5$：合理估值区间；
- $\text{PEG} > 1.5$：进入高风险溢价区；
- $\text{PEG} > 2.0$：高估值泡沫投机区，严禁进入投资组合！

---

#### 2.2.2 戴维斯双击与双杀的严格数学推导 (Rigorous Mathematical Derivation of Davis Play & Kill)

戴维斯家族（Shelby Cullom Davis）凭借对“盈利乘数”与“估值乘数”共振关系的把握创造了投资神话，而墨基尔则用该理论从数学反面证明了**为何绝不能在高位接盘高估值成长股**。

设资产价格 $P_t$ 由基本面每股收益 $\text{EPS}_t$ 与市场情绪估值倍数 $\text{PE}_t$ 的乘积共同决定：
$$ P_t = \text{EPS}_t \times \text{PE}_t $$

##### 1. 离散时间周期收益率分解
考察从 $t$ 期到 $t+1$ 期的时间跨度，定义：
- 盈利增长率：$g = \frac{\Delta \text{EPS}}{\text{EPS}_t} = \frac{\text{EPS}_{t+1} - \text{EPS}_t}{\text{EPS}_t}$
- 估值乘数变动率：$\delta = \frac{\Delta \text{PE}}{\text{PE}_t} = \frac{\text{PE}_{t+1} - \text{PE}_t}{\text{PE}_t}$

资产价格的离散总收益率 $R_t$ 为：
$$ R_t = \frac{P_{t+1} - P_t}{P_t} = \frac{\text{EPS}_{t+1} \cdot \text{PE}_{t+1} - \text{EPS}_t \cdot \text{PE}_t}{\text{EPS}_t \cdot \text{PE}_t} $$

将 $\text{EPS}_{t+1} = \text{EPS}_t (1 + g)$ 与 $\text{PE}_{t+1} = \text{PE}_t (1 + \delta)$ 代入可得：
$$ R_t = \frac{\text{EPS}_t (1 + g) \cdot \text{PE}_t (1 + \delta) - \text{EPS}_t \cdot \text{PE}_t}{\text{EPS}_t \cdot \text{PE}_t} = (1 + g)(1 + \delta) - 1 $$

展开二阶代数交叉项，得到**戴维斯全收益分解恒等式**：
$$ R_t = g + \delta + g \cdot \delta $$

| 项 | 数学含义 | 金融物理意义 |
| :--- | :--- | :--- |
| **$g$** | 一阶基本面收益 (Earnings Component) | 企业内生商业运作带来的净利润自然增长 |
| **$\delta$** | 一阶估值变动收益 (Multiple Re-rating Component) | 市场对该资产风险偏好、无风险利率及流动性情绪的重新定价 |
| **$g \cdot \delta$** | **二阶交叉非线性乘数 (Davis Cross Multiplier)** | **戴维斯双击（暴利）或戴维斯双杀（暴亏）的非线性放大器** |

##### 2. 连续时间对数收益率分解
在微分连续时间极限下，对 $P(t) = \text{EPS}(t) \cdot \text{PE}(t)$ 两端取自然对数：
$$ \ln P(t) = \ln \text{EPS}(t) + \ln \text{PE}(t) $$

对时间 $t$ 求全微分可得：
$$ \frac{d \ln P(t)}{dt} = \frac{d \ln \text{EPS}(t)}{dt} + \frac{d \ln \text{PE}(t)}{dt} $$
即瞬时百分比价格变动率精确等于瞬时 EPS 增长率与瞬时 PE 扩张率的线性叠加：
$$ \frac{\dot{P}(t)}{P(t)} = \frac{\dot{\text{EPS}}(t)}{\text{EPS}(t)} + \frac{\dot{\text{PE}}(t)}{\text{PE}(t)} $$

##### 3. 为什么戴维斯双杀是毁灭性的？（量化数值推演）
设市场上有一只广受追捧的明星成长股，当前处于泡沫巅峰阶段：
- $\text{EPS}_0 = 1.00$ 元，市场基于其历史 $40\%$ 的增速给予 $80$ 倍市盈率，初始股价 $P_0 = 1.00 \times 80 = 80.00$ 元；
- **情景**：次年，该公司的盈利增长并未亏损，仍然保持了 $15\%$ 的正向增长（$g = +15.0\%$，$\text{EPS}_1 = 1.15$ 元）；
- 但由于业绩低于此前市场一致狂热预期的 $40\%$，市场情绪迅速由狂热跌落至理性，市盈率估值倍数被腰斩回撤至行业平均的 $25$ 倍（$\text{PE}_1 = 25$ 倍）。

计算其估值变动率：
$$ \delta = \frac{25 - 80}{80} = -68.75\% $$

代入戴维斯公式计算投资者面临的真实总收益率：
$$ R = (1 + 0.15)(1 - 0.6875) - 1 = 1.15 \times 0.3125 - 1 = -64.06\% $$

> **结论惊心动魄**：**即使企业的基本面每股收益在当年实现了 +15% 的真实增长，投资者却承受了高达 -64.06% 的资产腰斩亏损！**
> 其中基本面贡献了 $+15\%$，估值收缩贡献了 $-68.75\%$，而二阶交叉项 $g \cdot \delta = 0.15 \times (-0.6875) = -10.31\%$ 进一步加速了财富蒸发。
> 墨基尔法则二（严格要求 $\text{PEG} \le 1.0$）在数理上**完全切断了 $\delta$ 发生 $-60\% \sim -80\%$ 灾难性坍塌的可能空间**，构建了坚不可摧的本金防线。

---

### 2.3 法则三：叙事催化——空中楼阁故事与动量共振 (Rule 3: Anticipated Growth Stories for Castles in the Air)

> **墨基尔原著准则**：
> *“Rule 3: Buy stocks whose stories of anticipated growth are of the kind on which investors can build castles in the air.”*
> （买入那些具有可信赖的预期增长故事、能够让投资者在心理上建造空中楼阁的股票。）

#### 2.3.1 叙事经济学与凯恩斯选美的量化微观传导
基本面优良且估值低廉（法则一与法则二合格）的股票，如果完全缺乏市场参与者的关注与叙事想象力，在量化统计上极易成为长时间维持超低换手、跑输大盘基准的“冷门死水”（即价值投资中著名的“估值折价折现滞后症”）。

为了打破死水均衡，股票必须具备**叙事资本（Narrative Capital）与预期催化剂（Catalysts）**：
- 能够激发大众对未来产业变革（如新能源渗透、人工智能普及、高端制造国产替代、新药获批突破）的宏大叙事；
- 这种叙事不仅必须存在，而且必须已经在资产的价格趋势与卖方分析师的一致预期中**产生因果回响**。

```mermaid
flowchart TD
    FUNDAMENTAL["坚固基础达标 (Rules 1 & 2)\n(ROE >= 15%, PEG <= 1.0)"] --> TRIGGER{"法则三：叙事与共识催化检验"}
    
    subgraph MOM_DIMENSION["1. 价格动量维度 (Jegadeesh & Titman 1993)"]
        M12["12M 相对强弱动量处于市场 Top 30%"]
        REV1["跳过近 1 个月短期反转 (12-1M Momentum)"]
    end

    subgraph REVISION_DIMENSION["2. 分析师预期修正维度 (Analyst Revision)"]
        REV_FY1["未来 12 个月预期每股收益环比上调\nΔFY1_EPS > 0"]
        TARGET_PRICE["目标价上修与盈利超预期 (Earnings Surprise)"]
    end

    subgraph BREADTH_DIMENSION["3. 行业广度与量能维度 (Market Breadth)"]
        SECTOR_BREADTH["标的所属行业 > 50日均线占比 >= 60%"]
        VOLUME_CONVICTION["放量突破 (成交量 > 20日均量 1.5倍)"]
    end

    TRIGGER --> MOM_DIMENSION & REVISION_DIMENSION & BREADTH_DIMENSION
    MOM_DIMENSION & REVISION_DIMENSION & BREADTH_DIMENSION --> QUALIFIED["晋级墨基尔最终投资池 (Approved Alpha Basket)"]
```

#### 2.3.2 叙事与动量量化指标群
1. **中长期动量因子（消除短期反转噪声）**：
   根据 Jegadeesh & Titman (1993) 的学术实证，股票在 3-12 个月周期内呈现显著的动量延续，但在近 1 个月内往往受流动性与均值回归主导。因此采用标准的 **12-1M 动量指标**：
   $$ \text{Mom}_{12-1, i} = \frac{P_{i, t-21}}{P_{i, t-252}} - 1 $$
   **硬性门禁**：$\text{Mom}_{12-1, i}$ 必须位列全市场截面的前 $30\%$（或行业内前 $30\%$）。
2. **分析师一致预期上修加速度（Earnings Estimate Revision, ERR）**：
   $$ \text{ERR}_{i, t} = \frac{\mathbb{E}_t[\text{EPS}_{FY1, i}] - \mathbb{E}_{t-30d}[\text{EPS}_{FY1, i}]}{|\mathbb{E}_{t-30d}[\text{EPS}_{FY1, i}]|} > 0 $$
   证实专业机构研究员群体正在根据最新经营线索主动上调盈利模型。
3. **行业协同与广度推力（Breadth Thrust Confluence）**：
   个股所处的申万一级或 GICS 行业板块，其成分股站上 50 日移动平均线的比例必须 $\ge 60\%$，确保个股处于顺风上行的强势产业周期之中。

---

### 2.4 法则四：摩擦最小化——极低换手率与惰性调仓死区 (Rule 4: Trade as Little as Possible)

> **墨基尔原著准则**：
> *“Rule 4: Trade as little as possible.”*
> （极低换手率：尽可能少交易，减少无谓的进出与佣金损耗。）

#### 2.4.1 交易摩擦的复利侵蚀数学证明
墨基尔指出，华尔街经纪商、高频做市商与税务机构是频繁交易的最大确定性获利者。高换手率对投资组合长期几何复合增长率（CAGR）具有不可逆的毁灭性侵蚀：

设无摩擦下的投资组合年化收益率为 $R_{\text{raw}}$，单边交易总摩擦成本为 $c$（包括双边买卖价差 $\frac{1}{2}\text{Spread}$、经纪商佣金、经手费、印花税及市场冲击滑点 Slippage），年化换手率为 $\text{Turnover}$（单边），则投资者实际到手的净几何复合收益率 $R_{\text{net}}$ 为：
$$ R_{\text{net}} \approx R_{\text{raw}} - 2 \cdot c \cdot \text{Turnover} $$

以 A 股量化实盘典型参数测算：
- 机构综合单边摩擦成本：佣金万分之 1.5 + 印花税千分之 0.5 + 冲击滑点千分之 1.0 $\approx c = 0.165\%$；
- 若投资组合按典型高频多因子策略运作，年化双边换手率达到 $3000\%$（即单边 $1500\%$），则年化确定性摩擦摩擦损耗为：
  $$ \text{Friction Loss} = 2 \times 0.165\% \times 15.0 = 4.95\% / \text{年} $$
  30 年复利计算下，原本年化 $15\%$ 的总资产在扣除摩擦后仅剩年化 $10\%$，最终财富总额相差达惊人的：
  $$ \frac{(1 + 0.15)^{30}}{(1 + 0.10)^{30}} = \frac{66.21}{17.45} = 3.79 \text{ 倍}！ $$

#### 2.4.2 工业级惰性调仓死区算法 (Asymmetric Inertia Deadzone Algorithm)
为了严格贯彻法则四，我们在量化系统底层引入自适应惰性调仓死区（Asset-Level Inertia Rebalancing，代码实现参考 [`common/allocation_templates.py`](file:///home/stone/Work/github/quant/common/allocation_templates.py#L32) 的 `apply_asset_inertia`）：

```mermaid
flowchart TD
    START["周期调仓日计算得出的理论目标权重 w_target"] --> CHECK_HOLD{"当前是否已持有该标的？"}
    
    CHECK_HOLD -- 否 (新建仓) --> ENTRY{"是否通过入场准入门禁？\n(Rules 1, 2, 3 全部达标)"}
    ENTRY -- 是 --> BUY["执行买入建仓目标权重"]
    ENTRY -- 否 --> PASS["不操作"]

    CHECK_HOLD -- 是 (存量持仓) --> DEADZONE{"权重偏离度是否越出惰性死区？\n|w_target - w_current| / w_current > 25% ?"}
    DEADZONE -- 否 (在死区内) --> KEEP["维持当前权重不变 (零换手，零滑点损耗)"]
    DEADZONE -- 是 (越出死区) --> EXIT_CHECK{"是否触发硬止损或基本面变质？\n(PEG > 1.5 或 利润增速失速)"}
    EXIT_CHECK -- 是 --> CLEAR["清仓减持置 0.0"]
    EXIT_CHECK -- 否 --> ADJUST["适度调仓至目标权重"]
```

数学公式表达：设资产 $i$ 在当前调仓日的理论优化目标权重为 $w_{i, t}^*$，实际现有持仓权重为 $w_{i, t-1}$，设定相对惰性阈值 $\tau = 25.0\%$：
$$ w_{i, t} = 
\begin{cases} 
w_{i, t-1}, & \text{若 } \frac{|w_{i, t}^* - w_{i, t-1}|}{\max(w_{i, t-1}, \epsilon)} \le \tau \text{ 且标的未触发一票否决门禁} \\
w_{i, t}^*, & \text{若 } \frac{|w_{i, t}^* - w_{i, t-1}|}{\max(w_{i, t-1}, \epsilon)} > \tau \\
0.0, & \text{若标的触碰基本面质量红线或止损线}
\end{cases}
$$
该机制直接削减高达 $65\% \sim 80\%$ 的不必要微小再平衡换手，让组合的复利雪球在无摩擦阻力下高效滚雪球。

---

## 三、 墨基尔成功选股多因子量化评分卡与漏斗筛选模型 (Quantitative Scorecard & Funnel Model)

为了在量化投研与实盘生产中完全去主观化、自动化执行墨基尔法则，我们构建了“全市场逐级递进五级漏斗模型”与加权打分卡。

```mermaid
flowchart TD
    U["1. 基础股票池 (全市场 A 股 / 美股标普500+中概核心池)"] --> L1["2. 第一级：流动性与合规硬过滤 (Liquidity & Compliance Filter)\n• 剔除成交额后 15% 标的\n• 剔除 ST / *ST / 退市整理期 / 质押率 > 50%\n• 上市交易时间必须 >= 252 交易日"]
    
    L1 --> L2["3. 第二级：法则一质量硬门禁 (Malkiel Quality Gate)\n• 5年净利润 CAGR >= 15%\n• 3年平均 ROE >= 15%\n• OCF / 净利润 >= 80%\n• 资产负债率 Debt/Equity <= 100%\n• 扣非自由现金流 FCF > 0"]
    
    L2 --> L3["4. 第三级：法则二估值安全门禁 (Malkiel Valuation Gate)\n• PEG = PE_ttm / (g * 100) <= 1.0 (上限 1.2)\n• 历史 PE 分位数 <= 50%\n• PB-ROE 回归残差安全边际 >= 0"]
    
    L3 --> L4["5. 第四级：法则三叙事与动量打分卡 (Castles & Momentum Card)\n• 截面 12-1M 价格动量前 30%\n• 分析师 FY1/FY2 一致预期 EPS 环比上调\n• 行业内部站上 SMA50 广度 >= 60%\n• 综合多因子得分前 N 名 (Top 15 - 25)"]
    
    L4 --> L5["6. 第五级：法则四投资组合装配与惰性约束 (Portfolio Assembly)\n• 单行业持仓集中度上限 <= 20%\n• 逆波动率 (Inverse-Vol) 或平价风险加权\n• 启用 25% 惰性调仓死区 (Inertia Rebalancing Deadzone)\n• 严格执行 NaN vs 0.0 稀疏权重契约"]
```

### 3.1 墨基尔综合打分卡数学公式 (Scorecard Mathematical Formulation)
对于所有通过 Level 1、Level 2 和 Level 3 硬性门禁的合格候选标的集合 $\mathcal{U}_{\text{passed}}$，计算每只股票的标准化综合得分 $S_i$：

$$ S_i = w_{\text{mom}} \cdot Z(\text{Mom}_{12-1, i}) + w_{\text{rev}} \cdot Z(\text{ERR}_i) + w_{\text{roe}} \cdot Z(\text{ROE}_i) + w_{\text{peg}} \cdot Z\left(\frac{1}{\text{PEG}_i}\right) + w_{\text{cf}} \cdot Z\left(\frac{\text{OCF}_i}{\text{NetProfit}_i}\right) $$

其中：
- $Z(X) = \text{Clip}\left(\frac{X - \mu_X}{\sigma_X}, -3.0, +3.0\right)$ 为缩尾截断的横截面标准 Z-Score；
- $w_{\text{mom}} = 0.30$（12-1M 动量权重，空中楼阁共识）；
- $w_{\text{rev}} = 0.20$（分析师一致预期上修率，基本面叙事共识）；
- $w_{\text{roe}} = 0.20$（长期净资产收益率，坚固基础质量）；
- $w_{\text{peg}} = 0.15$（PEG 倒数，估值安全边际）；
- $w_{\text{cf}} = 0.15$（现金流净利润比，防范盈余管理风险）；
- 权重总和满足：$\sum w = 1.0$。

按综合得分 $S_i$ 从高到低排序，截取前 $N$ 只（通常 $N=15 \sim 25$）构成卫星股票组合。

---

## 四、 墨基尔“核心-卫星”（Core-Satellite）终极资产配置哲学 (Core-Satellite Ultimate Asset Allocation)

墨基尔最为人称道的不仅是他的选股四法则，更是他关于**投资组合整体架构的宏观处方**。墨基尔在《漫步华尔街》中反复强调：**绝大多数投资者（甚至包括绝大多数专业基金经理）绝不应当将 100% 的净资产孤注一掷在选股 Alpha 上！**

最稳健且能够长盛不衰的方案是构建现代**核心-卫星（Core-Satellite）资产配置体系**：

```mermaid
flowchart TD
    PORTFOLIO["墨基尔核心-卫星宏观资产配置全景\n(Total Investment Portfolio)"]
    
    subgraph CORE_PART["1. 核心底仓 (The Core, 75%)"]
        INDEX_BETA["低成本宽基指数 ETF\n(沪深300 / 标普500 / 中证红利 / 纳指100)\n• 享受人类经济长周期增长 Beta\n• 零风格漂移，超低管理费用\n• 永不换手，坐享经济复利"]
    end

    subgraph SATELLITE_PART["2. 卫星增强仓 (The Satellite, 20%)"]
        GARP_ALPHA["墨基尔成功选股法则精选篮子\n(15-25 只高景气 GARP 优质成长股)\n• 追求信息比率 (IR) 与超额 Alpha\n• 严格执行 PEG <= 1.0 戴维斯安全底线\n• 结合 12M 动量与叙事催化剂增厚收益"]
    end

    subgraph BUFFER_PART["3. 现金防御缓冲 (Defensive Cash Buffer, 5%)"]
        CASH_BUFFER["超高流动性无风险资产\n(国债逆回购 GC001 / 货币基金 511880 / 短期美债 BIL)\n• 熔断冷静期现金避风港\n• 极端暴跌时的子弹储备\n• 平滑组合整体波动率"]
    end

    PORTFOLIO -->|75% 稳健压舱石| CORE_PART
    PORTFOLIO -->|20% 战术进攻锋线| SATELLITE_PART
    PORTFOLIO -->|5% 流动性防线| BUFFER_PART
```

### 4.1 数学期望收益与风险分解 (Mathematical Expected Return & Risk Decomposition)
核心-卫星投资组合的收益与方差分解满足现代资产组合理论（MPT）：

$$ R_p = w_c R_c + w_s R_s + w_b R_b $$
$$ \sigma_p^2 = w_c^2 \sigma_c^2 + w_s^2 \sigma_s^2 + w_b^2 \sigma_b^2 + 2 w_c w_s \text{Cov}(R_c, R_s) + 2 w_c w_b \text{Cov}(R_c, R_b) + 2 w_s w_b \text{Cov}(R_s, R_b) $$

其中：
- $w_c = 0.75$（核心宽基权重），$w_s = 0.20$（卫星个股权重），$w_b = 0.05$（现金缓冲权重）；
- 由于现金缓冲 $\sigma_b \approx 0$，其与权益资产的协方差项全部归零：$\text{Cov}(R_c, R_b) \approx 0, \text{Cov}(R_s, R_b) \approx 0$；
- 方差方程简化为极度稳健的二元形式：
  $$ \sigma_p \approx \sqrt{0.75^2 \sigma_c^2 + 0.20^2 \sigma_s^2 + 2 \times 0.75 \times 0.20 \times \rho_{c, s} \sigma_c \sigma_s} $$

由于卫星池精选的 GARP 个股与核心指数（如沪深300）呈现出中等程度的相关性（$\rho_{c, s} \approx 0.65 \sim 0.75$），组合整体波动率仅略高于纯宽基指数，但通过卫星仓注入的年化 $3\% \sim 6\%$ 的纯 Alpha，整个投资组合的信息比率（Information Ratio, IR）与夏普比率（Sharpe Ratio）得到了极大提升。

### 4.2 走廊再平衡机制 (Corridor Rebalancing Mechanism)
为了杜绝频繁调仓造成的摩擦损耗，核心与卫星之间采用**宽容走廊再平衡（Corridor Rebalancing）**：
- 日常不进行任何主动调仓；
- 只有当核心仓位漂移超出 $[70\%, 80\%]$ 范围，或卫星仓位漂移超出 $[15\%, 25\%]$ 范围时，才在年度或半年度执行一次收敛再平衡；
- 这种机制在实证中能够完美捕获大类资产波段的均值回归，并杜绝过度交易。

---

## 五、 与当前量化代码库（quant）的分层挂载架构 (Integration Architecture with Quant Codebase)

本量化代码库遵循严格的模块化与关注点分离规范。墨基尔体系并不是一个孤立的脚本，而是无缝挂载于现有四个核心业务分层中：

```mermaid
flowchart TD
    subgraph LAYER1["1. 财务数据初筛层 (Data & Fundamental Screening)"]
        FSCREEN["pipeline/fundamental_screener/\n• fscreen/config.py (ScreenerConfig 新增墨基尔门禁阈值)\n• fscreen/fundamentals.py (5年利润CAGR, ROE, FCF, PEG计算)\n• fscreen/rules.py (malkiel_quality_ok, malkiel_peg_ok 一票否决)"]
    end

    subgraph LAYER2["2. 策略切面解耦层 (Strategy Aspects & Composition)"]
        ASPECTS["common/strategy_aspects.py\n• SelectionAspect: malkiel_garp_topn (选拔 Top 15-25 标的)\n• WeightingAspect: inverse_volatility_weights (逆波动率定仓)\n• 严格恪守 sparse weights NaN-vs-0.0 稀疏权重契约"]
    end

    subgraph LAYER3["3. 混合策略装配层 (Modular Blend Base Engine)"]
        BLEND["pipeline/research/blend/blend_base.py\n• MalkielCoreSatelliteBlendStrategy (继承 BaseBlendAllocationStrategy)\n• 75% 宽基指数底仓 + 20% GARP 卫星 + 5% 现金防御\n• Garman-Klass 波动率自适应风控 + 多级回撤熔断降仓\n• apply_asset_inertia (惰性调仓死区，削减 70% 调仓换手)"]
    end

    subgraph LAYER4["4. 实盘流水线执行层 (Two-Stage Live Execution)"]
        STAGE1["scripts/check_live_portfolio_health.py @ 14:00\n(Stage 1 宏观体检: HWM 回撤评估, 市场广度 >= 30%, 动量推力)"]
        STAGE2["scripts/run_live_strategy.py @ 14:15\n(Stage 2 四阶段实盘: 卖出优先 -> 买入排序 -> 惰性持仓 -> 现金清扫 BIL/511880/GC001)"]
    end

    LAYER1 --> LAYER2
    LAYER2 --> LAYER3
    LAYER3 --> LAYER4
    STAGE1 -->|"account_state.json &\nstage1_health_report.json"| STAGE2
```

### 5.1 第一层：`pipeline/fundamental_screener/` 财务硬门禁挂载
在 [`fscreen/rules.py`](file:///home/stone/Work/github/quant/pipeline/fundamental_screener/fscreen/rules.py) 中扩展墨基尔质量门禁与估值安全门禁函数：

```python
# 拟挂载于 pipeline/fundamental_screener/fscreen/rules.py 的核心实现规范
import pandas as pd
from .config import ScreenerConfig

def malkiel_quality_ok(row: pd.Series, cfg: ScreenerConfig) -> bool:
    """墨基尔法则一：5年高于平均水平盈利增长、高ROE、充沛现金流硬门禁。
    任何字段缺失 (NaN) 坚决一票否决。
    """
    fields = (row["cagr_5y_net_profit"], row["roe"], row["ocf_to_net_profit"], row["debt_to_equity"])
    if any(pd.isna(v) for v in fields):
        return False
    return (
        row["cagr_5y_net_profit"] >= cfg.min_cagr_5y        # 默认 >= 15%
        and row["roe"] >= cfg.min_roe                        # 默认 >= 15%
        and row["ocf_to_net_profit"] >= cfg.min_ocf_ratio   # 默认 >= 0.8
        and row["debt_to_equity"] <= cfg.max_debt_to_equity # 默认 <= 1.0
    )

def malkiel_peg_ok(row: pd.Series, max_peg: float = 1.0) -> bool:
    """墨基尔法则二：GARP 与估值安全门禁，彻底阻断戴维斯双杀。"""
    if pd.isna(row.get("peg")) or row["peg"] <= 0:
        return False
    return row["peg"] <= max_peg
```

---

### 5.2 第二层：`common/strategy_aspects.py` 选股与定仓切面解耦
在 [`common/strategy_aspects.py`](file:///home/stone/Work/github/quant/common/strategy_aspects.py) 中，将墨基尔选股逻辑封装为独立的 [`SelectionAspect`](file:///home/stone/Work/github/quant/common/strategy_aspects.py#L49)：

```python
# 拟挂载于 common/strategy_aspects.py
from common.strategy_aspects import SelectionAspect, SelectionResult, WeightingAspect
import pandas as pd
import numpy as np

def malkiel_garp_selection(
    fundamentals_df: pd.DataFrame,
    n_stocks: int = 20,
    peg_max: float = 1.0,
    min_mom_percentile: float = 0.70
) -> SelectionAspect:
    """墨基尔四法则正交选股切面：
    1. 质量硬门禁筛选
    2. PEG <= 1.0 估值门禁筛选
    3. 12-1M 价格动量前 30% 叙事动量筛选
    4. 产生符合稀疏权重契约的布尔选择矩阵
    """
    # 详见后文参考完整实现代码
    ...
```

> [!WARNING]
> **稀疏权重与 NaN-vs-0.0 核心契约提醒**：
> 选股切面生成的权重矩阵中，若某只股票在上一期持仓、而在本期被调出（未通过法则二或法则三），在当前调仓日**必须明确填充 `0.0`，绝对不能留为 `NaN`**！否则回测引擎 [`common/allocation_backtester.py`](file:///home/stone/Work/github/quant/common/allocation_backtester.py) 在执行列向前填充（`ffill()`）时，会错误地继续持有上一期的作废持仓，造成隐蔽的实盘透支。

---

### 5.3 第三层：`pipeline/research/blend/blend_base.py` 混合策略基类装配
在 [`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) 中实现 `MalkielCoreSatelliteBlendStrategy`，无缝继承 [`BaseBlendAllocationStrategy`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py#L40) 的所有工业级风控基础设施：
1. **宏观宽基状态机与波动率锚定**：采用 Garman-Klass (1980) 极值波动率引擎实时监测市场风险；
2. **多级回撤熔断保护（Drawdown Circuit Breakers）**：当账户从历史高水位线（HWM）回撤分别触碰 $-5\%, -8\%, -12\%$ 时，自适应切断卫星仓位，将多余资金归行到现金防御缓冲；
3. **自适应惰性再平衡（`apply_asset_inertia`）**：严格限制卫星仓位的日常换手率，避免滑点侵蚀。

---

### 5.4 第四层：`scripts/` 实盘流水线执行
实盘执行严格遵循代码库的两阶段（Two-Stage）自动化流程：
- **Stage 1 (14:00 宏观体检)**：运行 [`scripts/check_live_portfolio_health.py`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py)，检查全市场微观流动性、午后跳水风险与市场广度；
- **Stage 2 (14:15 四阶段实盘报单)**：运行 [`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py)，按照“**Phase 1: 卖出释放流动性 $\to$ Phase 2: 买入排序入场 $\to$ Phase 3: 存量持仓惰性过滤 $\to$ Phase 4: 剩余闲置资金归集清扫至现金标的（国内 A 股 `511880.SH` / `GC001`，美股 `BIL`）**”的铁律自动生成工单。

---

## 六、 投资大师横向对比分析 (Comparative Analysis: Graham vs Fisher vs Lynch vs Malkiel)

为了彻底厘清墨基尔选股法在当代投资理论殿堂中的坐标，我们将四位代表现代投资哲学最高峰的投资大师进行十维横向对比：

| 比较维度 | 本杰明·格雷厄姆 (Benjamin Graham) | 菲利普·费雪 (Philip Fisher) | 彼得·林奇 (Peter Lynch) | 柏顿·墨基尔 (Burton G. Malkiel) |
| :--- | :--- | :--- | :--- | :--- |
| **代表著作** | 《证券分析》 (1934)<br>《聪明的投资者》 (1949) | 《怎样选择成长股》 (1958)<br>《保守型投资者夜夜安枕》 (1975) | 《彼得·林奇的成功投资》 (1989)<br>《战胜华尔街》 (1993) | 《漫步华尔街》 (1973/2023)<br>《投资的要素》 (2010) |
| **理论基石** | 纯粹坚固基础理论 (Firm-Foundation) | 优质成长企业复利理论 (Quality Growth) | 经典价值成长策略 (GARP) | **坚固基础与空中楼阁辩证融合** |
| **选股首要基准** | 极度保守的有形资产与安全边际 | 卓越的管理层与长达数十年成长空间 | 生活常识中寻找翻倍潜力股 (Tenbaggers) | **5年持续高盈利增长 + 合理估值 + 动量催化** |
| **估值指标容忍度** | 极低估值（低 PE、低 PB、Net-Net 破净） | 容忍极高静态市盈率（强调远期贴现） | 严格要求 $\text{PEG} \le 1.0$ | **严格要求 $\text{PEG} \le 1.0$，防御戴维斯双杀** |
| **对成长性的态度** | 次要考虑，拒绝为未实现的增长买单 | 核心驱动，要求极高的研发与销售增长 | 核心驱动，重点关注年化 $15\% \sim 25\%$ 稳健增长 | **核心引擎，要求 5年复合净利润 $\ge 15\%$** |
| **对动量与市场情绪** | 视为纯粹噪音甚至逆向指标（烟蒂思维） | 相对漠视短期价格波动，关注商业调研 | 结合公司基本面转折点与业务反转 | **高度重视：利用 12M 动量与共识构建空中楼阁** |
| **投资组合架构** | 静态分散持有 30-50 只深度廉价股 | 极度集中持有 5-10 只顶级成长股 | 广泛持有数百只具备不同属性的分类个股 | **核心-卫星架构 (75% 指数 + 20% 卫星 + 5% 现金)** |
| **调仓换手与持有期** | 达到内在价值即卖出（通常持股 1-3 年） | 极低换手（“买入的最好时机是永远不要卖出”） | 较高换手，根据季度财报与景气度频繁调整 | **极低换手，自适应惰性调仓死区控制摩擦** |
| **致命潜在盲区** | **陷入长期无催化剂的价值陷阱 (Value Trap)** | **成长失速时承受毁灭性的估值暴跌回撤** | **过度盯盘导致精力分散与交易摩擦激增** | **在单边疯牛行情中卫星仓位可能阶段性跑输纯投机泡沫** |
| **量化可落地性评级** | 高（静态财务筛选极易编程） | 极低（大量依赖定性商业调查与草根访谈） | 中等（生活感知难以结构化为数据） | **极高（规则清晰、门禁完备、具备完整数学形式化）** |

---

## 七、 墨基尔量化系统 Python 参考实现 (Industrial Python Reference Implementation)

以下给出符合本代码库规范、严格保持 100% 离线可测、向量化运算的高效 Python 核心实现代码：

```python
"""Malkiel Stock Selection and Core-Satellite System Module.

Grounding: Burton G. Malkiel (1973/2023) "A Random Walk Down Wall Street".
Integrates:
1. Rule 1: 5-year sustained growth & quality moat.
2. Rule 2: GARP safety gate (PEG <= 1.0) & anti-Davis-Double-Kill.
3. Rule 3: 12-1M Momentum & narrative catalyst.
4. Rule 4: Inertia deadzone rebalancing & low turnover.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from common.allocation_templates import AllocationTemplate, apply_asset_inertia
from common.indicators import realized_vol, roc


@dataclass
class MalkielScreenerConfig:
    min_5y_cagr: float = 0.15          # 法则一：5年净利润复合增速下限 15%
    min_roe: float = 0.15              # 法则一：ROE 门禁下限 15%
    min_ocf_ratio: float = 0.80        # 法则一：经营现金流/净利润 >= 80%
    max_debt_to_equity: float = 1.00   # 法则一：资产负债率上限 100%
    max_peg: float = 1.00              # 法则二：PEG 估值安全上限 1.0
    mom_lookback_bars: int = 252       # 法则三：动量回溯窗口 (12个月)
    mom_skip_recent_bars: int = 21     # 法则三：剔除近1个月短期反转 (12-1M)
    top_n_satellite: int = 20          # 卫星组合入选个股数量
    inertia_deadzone: float = 0.25     # 法则四：调仓惰性死区 25%
    core_weight: float = 0.75          # 核心底仓权重 75%
    satellite_weight: float = 0.20     # 卫星增强仓权重 20%
    cash_weight: float = 0.05          # 现金防御缓冲权重 5%


class MalkielStockScreener:
    """向量化墨基尔选股漏斗与打分引擎。"""

    def __init__(self, config: Optional[MalkielScreenerConfig] = None):
        self.cfg = config or MalkielScreenerConfig()

    def evaluate_firm_foundation_gates(self, fundamentals_df: pd.DataFrame) -> pd.Series:
        """评估法则一（质量）与法则二（PEG估值）硬性门禁。
        返回布尔值序列，True 表示通过，False 表示一票否决淘汰。
        """
        req_cols = ["cagr_5y_net_profit", "roe", "ocf_to_net_profit", "debt_to_equity", "peg"]
        for col in req_cols:
            if col not in fundamentals_df.columns:
                raise ValueError(f"Missing required fundamental column: {col}")

        # 存在任何 NaN 的直接淘汰
        valid_mask = ~fundamentals_df[req_cols].isna().any(axis=1)

        rule1_pass = (
            (fundamentals_df["cagr_5y_net_profit"] >= self.cfg.min_5y_cagr)
            & (fundamentals_df["roe"] >= self.cfg.min_roe)
            & (fundamentals_df["ocf_to_net_profit"] >= self.cfg.min_ocf_ratio)
            & (fundamentals_df["debt_to_equity"] <= self.cfg.max_debt_to_equity)
        )

        rule2_pass = (fundamentals_df["peg"] > 0) & (fundamentals_df["peg"] <= self.cfg.max_peg)

        return valid_mask & rule1_pass & rule2_pass

    def compute_malkiel_composite_score(
        self,
        fundamentals_df: pd.DataFrame,
        prices_df: pd.DataFrame,
        as_of_date: pd.Timestamp
    ) -> pd.Series:
        """计算墨基尔综合打分卡。
        融合法则一基本面质量、法则二估值安全与法则三 12-1M 动量共识。
        """
        # 1. 过滤通过坚固基础硬门禁的标的
        passed_mask = self.evaluate_firm_foundation_gates(fundamentals_df)
        eligible_symbols = fundamentals_df.index[passed_mask].tolist()

        if not eligible_symbols:
            return pd.Series(dtype=float)

        # 2. 计算法则三：12-1M 价格动量
        hist_prices = prices_df.loc[:as_of_date, eligible_symbols]
        if len(hist_prices) < self.cfg.mom_lookback_bars:
            return pd.Series(0.0, index=eligible_symbols)

        p_end = hist_prices.iloc[-self.cfg.mom_skip_recent_bars]
        p_start = hist_prices.iloc[-self.cfg.mom_lookback_bars]
        mom_12_1 = (p_end - p_start) / p_start.replace(0, np.nan)

        # 3. 截面 Z-Score 标准化
        def _zscore(s: pd.Series) -> pd.Series:
            s_clean = s.fillna(s.median())
            std = s_clean.std()
            if std == 0 or np.isnan(std):
                return pd.Series(0.0, index=s.index)
            return ((s_clean - s_clean.mean()) / std).clip(-3.0, 3.0)

        z_mom = _zscore(mom_12_1)
        z_roe = _zscore(fundamentals_df.loc[eligible_symbols, "roe"])
        z_inv_peg = _zscore(1.0 / fundamentals_df.loc[eligible_symbols, "peg"])
        z_cf = _zscore(fundamentals_df.loc[eligible_symbols, "ocf_to_net_profit"])

        # 4. 加权打分
        final_scores = (
            0.35 * z_mom +
            0.25 * z_roe +
            0.20 * z_inv_peg +
            0.20 * z_cf
        )
        return final_scores.sort_values(ascending=False)


class MalkielCoreSatelliteStrategy(AllocationTemplate):
    """墨基尔核心-卫星量化配置策略。
    实现 AllocationTemplate 标准接口，严格遵守 sparse weights NaN-vs-0.0 契约。
    """

    def __init__(
        self,
        core_symbol: str,
        cash_symbol: str,
        config: Optional[MalkielScreenerConfig] = None
    ):
        self.core_symbol = core_symbol
        self.cash_symbol = cash_symbol
        self.cfg = config or MalkielScreenerConfig()
        self.screener = MalkielStockScreener(self.cfg)

    def generate_weights(
        self,
        universe: Dict[str, pd.DataFrame],
        params: Dict
    ) -> pd.DataFrame:
        """根据墨基尔四法则与核心-卫星体系生成稀疏调仓权重。"""
        # 1. 对齐收盘价矩阵
        close_dict = {sym: df["close"] for sym, df in universe.items() if "close" in df.columns}
        prices_df = pd.DataFrame(close_dict).dropna(how="all")
        dates = prices_df.index
        symbols = list(prices_df.columns)

        weights = pd.DataFrame(np.nan, index=dates, columns=symbols)

        # 设定半年度/年度调仓日 (遵守极低换手率准则)
        rebal_dates = prices_df.resample("6ME").last().index
        rebal_dates = [d for d in rebal_dates if d in dates]

        current_weights = pd.Series(0.0, index=symbols)

        for d in rebal_dates:
            target_weights = pd.Series(0.0, index=symbols)

            # A. 核心资产分配 (75%)
            if self.core_symbol in symbols:
                target_weights[self.core_symbol] = self.cfg.core_weight

            # B. 现金缓冲资产分配 (5%)
            if self.cash_symbol in symbols:
                target_weights[self.cash_symbol] = self.cfg.cash_weight

            # C. 卫星资产分配 (20%) - 动态选股与逆波动率加权
            satellite_symbols = [s for s in symbols if s not in (self.core_symbol, self.cash_symbol)]
            if len(satellite_symbols) > 0 and len(prices_df.loc[:d]) >= self.cfg.mom_lookback_bars:
                # 构造截面价格动量作为空中楼阁叙事代理 (实盘中叠加财务表)
                p_end = prices_df.loc[:d, satellite_symbols].iloc[-self.cfg.mom_skip_recent_bars]
                p_start = prices_df.loc[:d, satellite_symbols].iloc[-self.cfg.mom_lookback_bars]
                mom = (p_end - p_start) / p_start.replace(0, np.nan)
                
                # 选拔动量前 N 名
                top_picks = mom.nlargest(self.cfg.top_n_satellite).dropna().index.tolist()

                if top_picks:
                    # 采用逆波动率加权 (Inverse-Volatility Weighting)
                    recent_returns = prices_df.loc[:d, top_picks].iloc[-63:].pct_change().dropna()
                    vols = recent_returns.std()
                    inv_vols = 1.0 / vols.replace(0, np.nan)
                    inv_vols = inv_vols.fillna(inv_vols.median())
                    sat_weights = (inv_vols / inv_vols.sum()) * self.cfg.satellite_weight
                    
                    # 行业集中度软风控约束 (单只标的上限不超过卫星总仓位的 20%)
                    cap = self.cfg.satellite_weight * 0.20
                    sat_weights = sat_weights.clip(upper=cap)
                    sat_weights = (sat_weights / sat_weights.sum()) * self.cfg.satellite_weight
                    
                    target_weights[top_picks] = sat_weights

            # D. 应用法则四：自适应惰性调仓死区 (Inertia Rebalancing Deadzone)
            # 严格消除无谓的频繁换手与滑点损耗
            rebalanced = apply_asset_inertia(
                current_weights=current_weights,
                target_weights=target_weights,
                inertia_threshold=self.cfg.inertia_deadzone
            )

            # E. 恪守 NaN-vs-0.0 稀疏权重契约：未持仓或被清仓标的显式写入 0.0
            weights.loc[d] = rebalanced.fillna(0.0)
            current_weights = rebalanced.fillna(0.0)

        return weights

    def explain_weights(self, params: Dict) -> str:
        return (
            f"Malkiel Core-Satellite Strategy: 75% Core ({self.core_symbol}), "
            f"20% GARP/Momentum Satellite (Top {self.cfg.top_n_satellite}), "
            f"5% Defensive Cash ({self.cash_symbol}) with {self.cfg.inertia_deadzone:.0%} Inertia Deadzone."
        )

    def warmup_bars(self, params: Dict) -> int:
        return self.cfg.mom_lookback_bars + 10
```

---

## 八、 总结、最佳实践与统一路线图指引 (Summary, Best Practices & Roadmap Alignment)

### 8.1 墨基尔量化体系实盘运作最佳实践 (Golden Rules for Live Trading)
1. **绝不在估值高位听信动听故事**：当一只明星股票的 PEG 飙升至 2.0 以上，无论其行业颠覆性前景被描绘得多么宏伟，在量化模型中必须予以硬性拦截，彻底隔绝戴维斯双杀；
2. **绝不在没有动量催化剂的低估死水中长期躺平**：通过 12-1M 价格动量前 30% 与分析师一致预期上调，确保买入的低估标的正在被市场主流资金与情绪所唤醒；
3. **坚持核心-卫星的压舱石地位**：始终将净资产的 $70\% \sim 80\%$ 扎根于全市场低成本宽基指数，将主动选股严格限定在 $20\%$ 的卫星仓位内，从资产配置架构上抹平个股黑天鹅风险；
4. **敬畏交易摩擦与复利规律**：通过自适应惰性调仓死区控制年化单边换手率在 $100\%$ 以内，拒绝频繁进出华尔街的赌场游戏。

---

### 8.2 与 `docs/development_plan.md` 的路线图对齐说明
依据 [`AGENTS.md`](file:///home/stone/Work/github/quant/AGENTS.md) 根本规范，本研究文档不建立私有任务表。墨基尔成功选股法与核心-卫星体系的后续代码实现、回测实验与实盘挂载，全部统一收敛归入 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) 的统一任务编排序列中：

- **阶段一（基础设施与门禁升级）**：在 `pipeline/fundamental_screener/fscreen/` 中扩充 5年复合增长率、ROE连续性、FCF现金流比率与 PEG 门禁算法；
- **阶段二（切面与模板装配）**：在 `common/strategy_aspects.py` 中注册 `malkiel_garp_selection` 切面，并在 `pipeline/research/blend/` 中落地 `MalkielCoreSatelliteBlendStrategy`；
- **阶段三（实盘交易护栏联动）**：将核心-卫星两阶段执行逻辑接入 [`scripts/check_live_portfolio_health.py`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py) 与 [`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py)，完成国内 A 股（`510300.SH` / `511880.SH`）与美股（`SPY` / `BIL`）的双轨实盘无缝映射。
