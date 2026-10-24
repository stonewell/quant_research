# Chan Crisis Shield Blend Strategy: 实盘交易操作规程与规则手册 (SOP)

> **策略标识**: `chan_crisis_shield_blend` ([`ChanCrisisShieldBlendStrategy`](file:///home/stone/Work/github/quant/pipeline/research/blend/chan_crisis_shield_blend.py#L20-L104))  
> **实盘就绪度评分**: **80.5 / 100 (Grade B 进取战术型实盘级 / Tactical Aggressive Live Ready)**  
> **专属发单工具**: [`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py)  
> **交易标的**: 中国 A 股高流动性核心-卫星股票池 (22 只精选标的) + 现金管理工具 (`511880.SH` 银华日利、`511990.SH` 华宝添益、`GC001` / `R-001` 国债逆回购；美股/回测中使用 `BIL`)  
> **调仓频率**: 每日 **14:00** 评估，**14:20 – 14:45** 集中发单，**14:55 – 15:00** 闲置现金归集 (避开早盘 09:30–10:00 剧烈博弈与 14:57 尾盘收盘集合竞价冲击)。  
> **底层因子**: `regime_trend_strength` (环境趋势强度), `absolute_momentum_trend` (绝对动量), `relative_momentum` (相对动量), `volatility_targeting` (波动率目标控制), `mean_reversion` (放量恐慌均值回归)  
> **前瞻步进回测 (Walkforward) 核心审计表现**:
> - **爆发性年化超额收益**: 在多次滚动窗口中斩获全策略最高原始收益与夏普率：`blend_8/2` 年化 CAGR **+49.7%** (夏普 1.349, Calmar 11.94)，`blend_12/2` 年化 CAGR **+41.9%** (夏普 1.295, Calmar 12.91)，`blend_13/2` 年化 CAGR **+31.8%** (夏普 1.607, Calmar 9.07)；
> - **下行最大回撤控制**: 得益于 35% 权重的 Keller VAA 避险压舱石，全周期平均最大回撤严密压制在 **4.3% ~ 4.7%** 之间；
> - **实盘注意要点 (Caveat)**: 因 Alpha#12 (成交量放量背离逆转) 收益集中于恐慌急跌后的剧烈脉冲修复，收益序列偏度较高，步进 DSR 统计分值较低（需警惕盘中急跌滑点与追跌执行纪律）。

---

## 1. 策略架构与多因子模型体系

**Chan Crisis Shield Blend Strategy (缠论危机盾牌风控配置策略)** 是一套专注于捕获极端恐慌错杀与趋势修复的机构级混合配置策略。该策略以实盘审计风控框架为底座，由三大异构模块组成：
1. **缠论三类买点 (Three-Type 45%)**：捕获趋势中枢突破主升浪；
2. **缠论 VAA 复合防御 (VAA Compound 35%)**：提供宏观下行避险压舱石；
3. **WorldQuant Alpha#12 成交量放量背离逆转 (Alpha#12 Volume Shock Reversal 20%)**：
   $$\text{Alpha\#12} = \text{sign}(\Delta \text{Volume}) \times (-\Delta \text{Close})$$
   该因子专门在标的出现放量暴跌（成交量放大但价格暴挫）时捕捉恐慌抛售衰竭点，在恐慌盘涌出次日实现高胜率均值回归反弹。

```mermaid
flowchart TD
    Universe["经审计 22 只核心股票池 + 现金管理工具"] --> SubAlphas["底层多策略 Alpha 信号计算引擎"]
    
    subgraph Ensemble ["底层核心子策略三合一装配"]
        SubAlphas --> S1["缠论三类买点 (Three-Type 45%)\n(中枢突破 1B/2B/3B 趋势主升)"]
        SubAlphas --> S2["缠论 VAA 复合防御 (VAA Compound 35%)\n(Keller 13612W 动量防黑天鹅基石)"]
        SubAlphas --> S3["WQ Alpha#12 恐慌放量反转 (Alpha#12 20%)\nsign(Delta Vol) * (-Delta Close)"]
    end
    
    S1 & S2 & S3 --> Ortho["Löwdin 对称正交化去相关性\nW* = W(W^T W)^(-1/2)"]
    Ortho --> BayesianIC["Rolling 60d IC-IR 贝叶斯自适应加权\n(HLZ t >= 2.0 显著性门槛)"]
    BayesianIC --> RawBlend["计算各资产原始目标配置权重"]
    
    RawBlend --> DDGate{"账户历史最高净值 (HWM) 回撤检查"}
    DDGate -- "回撤 >= 20% (三级极端熔断)" --> HardStop["全面清仓离场 -> 100% 现金品\n强制交易冷冻 15 个交易日"]
    DDGate -- "10% <= 回撤 < 20% (一级平滑阻尼)" --> LinearDamp["平滑线性阻尼压缩 (1.0x -> 0.0x)\n15日自动冷却自愈 & 动量快速反弹豁免"]
    DDGate -- "回撤 < 10% (正常运行)" --> BreadthEngine{"市场宽度与动量脉冲评估\n(广度 >= 30% 或 10日脉冲 >= 60%)"}
    
    BreadthEngine -- "多头广度/脉冲生效" --> BullExposure["动态释放资金池:\n仓位上限扩展至 65%~80%"]
    BreadthEngine -- "中性/偏弱环境" --> DefensiveExposure["收紧持仓硬顶: 单只股票 <= 20% NAV"]
    BullExposure --> SizingCap["单票仓位硬顶限制: <= 20%"]
    DefensiveExposure --> SizingCap
    LinearDamp --> SizingCap
    
    SizingCap --> VolTarget{"Garman-Klass 真实波动率目标控盘\n(年化目标波动率 12%)"}
    VolTarget --> FrictionFilter{"调仓惰性门槛\n(已持仓: |权重变动| >= 5%\n新建仓: 目标权重 >= 3%\n平仓需 2 日确认\n回测: 自适应 2.5%-8%)"}
    
    FrictionFilter -- "调仓触发或紧急熔断" --> ExecutionEngine["实盘下单操作次序 (通用发单器):\n1. 14:20 优先卖出释放资金\n2. 14:35 后买入建仓 (整手向下取整)\n3. 14:45 维持未超阈值持仓\n4. 14:55 闲置现金归集"]
    FrictionFilter -- "未达门槛" --> HoldPrior["放弃调仓，维持原有股数不变 (零摩擦)"]
    ExecutionEngine --> RepoSweep["14:55 剩余闲置资金借出 GC001 / R-001 逆回购"]
```

---

## 2. 实盘交易员日内标准作业程序 (SOP 时间表)

实盘交易员在每个交易日必须严格遵循以下时间轴，杜绝随意调换顺序：

* **14:00 – 14:15 (Stage 1 宏观健康度门禁)**:
  - 运行健康审计：
    ```bash
    uv run python scripts/check_live_portfolio_health.py --data-provider marketdb
    ```
  - 检查最高净值 (HWM) 回撤、多级熔断状态与冷冻期天数。
* **14:15 – 14:25 (Stage 2 调仓指令单生成)**:
  - 若门禁结论为 **🟢 GO** 或 **🟡 CAUTION**，启动通用发单器：
    ```bash
    uv run python scripts/run_live_strategy.py \
      --strategy chan_crisis_shield_blend \
      --current-holdings-file docs/ruleset/chan_crisis_shield_blend/current_holdings.json \
      --portfolio-value <ACCOUNT_NAV> \
      --data-provider marketdb
    ```
* **14:25 – 14:35 (Phase 1: 优先执行卖出平仓/减仓)**:
  - **先卖后买**。按指令单红色 SELL 标的挂单平仓，释放可用资金。优先处理破位离场与止损标的。
* **14:35 – 14:45 (Phase 2: 执行买入建仓/加仓)**:
  - 按指令单绿色 BUY 标的挂单。
  - **针对 Alpha#12 逆转买单特殊纪律**：Alpha#12 常在标的恐慌放量时买入，交易员**严禁市价追单或挂涨停板价格**，必须使用**买一/对手方最优限价单**报单，防止买在盘中冲高回落毛刺上。
  - 向下取整至 100 股单手整数倍。对于显示 `⚠️ 低于单手门槛` 的标的，跳过不买。
* **14:45 – 14:50 (Phase 3: 维持既有持仓确认)**:
  - 核对 Phase 3 全部白色 HOLD / HOLD_FILTERED 标的，一律不报单，维持原有股数。产生该行的门槛有**三条彼此独立**的规则，**不是**单一的 5% 规则，详见规则 3。
* **14:55 – 15:00 (Phase 4: 尾盘闲置资金归集)**:
  - 账户所有未买入股票的闲置资金通过国债逆回购 (`GC001` 或 `R-001`) 融出，隔夜获取无风险增厚。

---

## 3. 详细交易逻辑与分步规则

### 规则 1: 组合级回撤熔断器 (Portfolio-Level Circuit Breakers)

$$\text{Drawdown} = \frac{\text{Current NAV} - \text{Peak NAV (HWM)}}{\text{Peak NAV}}$$

* **正常状态 ($\text{Drawdown} < 10\%$)**: 风险预算全额开放，单票仓位上限 20% NAV；
* **一级平滑阻尼 ($10\% \le \text{Drawdown} < 15\%$) — 代码执行 (code-executed)**:
  - 按 $\text{Scale} = \max\left(0.0,\ 1.0 - \dfrac{\text{DD} - 0.10}{0.10}\right)$ 同比例压缩股票仓位，即**回撤 10% 时不减、15% 时恰好减半、20% 时减至 0**——阻尼在整个 10%–20% 区间连续生效，实盘采用同一公式 (`scripts/check_live_portfolio_health.py:380,384`)；
  - 代码中存在"直接砍半 (乘 0.5)"的离散分支 (`pipeline/research/blend/blend_base.py:1075-1081`)，但**不是**当前部署配置：六个 blend 策略均设置 `smooth_drawdown: true`；
  - 启动 15 个交易日冷却期自动自愈计数器；
* **二级战术防御 ($15\% \le \text{Drawdown} < 20\%$) — 人工执行 (human-executed)**:
  - 强制股票总仓位降至 30% 以下，70% 资金停泊于现金品；
  - 该 30%/70% 拆分与防守轮动是健康报告打印给人工的 advisory 指令 (`scripts/check_live_portfolio_health.py:618-622`)，**工单生成器并不执行它**；
* **三级极端熔断 ($\text{Drawdown} \ge 20\%$) — 代码执行 (code-executed, hard halt)**:
  - **无条件 100% 清仓**，全额切换为国债逆回购/货币 ETF；
  - **强制交易冷冻 15 个交易日**，冷却期满且市场动量反转后方可重启。

> **注意 — Tier-2 的轮动指令不会被工单执行。** 健康报告在 Tier-2 打印的"轮动至防守子策略"只是给人工的 advisory 指令 (`scripts/check_live_portfolio_health.py:618-622`)；`scripts/run_live_strategy.py` 全文没有任何 `TIER_1` / `TIER_2` 分支，到 Tier-2 时工单只做组合层缩放（`current_targets *= live_scale`，`scripts/run_live_strategy.py:1766`；`live_scale` 取自 `account_state` / 健康报告的 `linear_equity_scale`，`scripts/check_live_portfolio_health.py:380`）。仓位轮动须人工按健康报告操作；因此**在 Tier-2 当日，工单产出的持仓结构可能仍停留在 Tier-1 的缩放形态**。

### 规则 2: Alpha#12 恐慌买入与流动性保护规则

* **买入准入**: $\Delta \text{Close} < 0$ 且 $\Delta \text{Volume} > 0$（量升价跌），且未跌穿近期中枢下沿 3.0 ATR；
* **单票限额**: 单只标的不得超过 20% NAV，防止黑天鹅个股连续跌停无法卖出；
* **买单挂单规则**: 仅使用对手方一档限价单（挂买一至卖一中间价），严禁市价追入。

### 规则 3: 调仓惰性 — 三条实盘规则 (与回测规则不同)

对每只标的，在 14:00 计算带符号的权重变动：
$$\Delta W = W_{\text{目标}} - W_{\text{当前}}$$

本策略携带 **一个惰性参数** (`ccsb_min_weight_change = 0.05`)，**外加一个独立且更宽松的建仓门槛** (`ccsb_entry_threshold = 0.03`)。按标的所处情形，套用**第一个**命中的规则：

| # | 适用情形 | 实盘规则 (交易员实际执行) | 回测规则 (模型当年的行为，与实盘不同) |
| :--- | :--- | :--- | :--- |
| **1** | 该标的**已有持仓**，模型目标权重发生变化 | 要求 $\lvert\Delta W\rvert \ge \mathbf{5\%}$。未达则**不交易**；指令单打印 `HOLD`，原因 "调仓未超惰性阈值 (< 5%)"。门槛每日重算。 | 逐资产阈值是**自适应的 2.5% – 8%**：$\mathrm{clip}\big(\tfrac{\sigma^{21d}_{\text{标的}}}{\sigma^{21d}_{\text{全池}}} \times 5\%,\ 2.5\%,\ 8\%\big)$。随后回测器层再套一次固定 5% —— **仅对已持仓标的生效**。 |
| **2** | 该标的**当前无持仓** (0% -> 新建仓) | 要求**目标权重 $\ge \mathbf{3\%}$** (`ccsb_entry_threshold = 0.03`)，比规则 1 **更宽松**。低于 3% 则**不开仓**；指令单打印 `HOLD_FILTERED`，原因 "目标仓位未达最低建仓/调仓门槛 (… 防微量试错损耗)"。一个全新的 2.9% 仓位会被拒绝。 | 回测器层的 5% 门槛**不适用于新建仓** (未持仓标的豁免)；模板层惰性过滤仅按 $\min(\text{自适应阈值},\ 3\%)$，即 **2.5% – 3%** 拦截建仓。 |
| **3** | 模型对已持仓标的给出 **0.0%** (全额清仓) | 清仓**豁免** 5% 门槛，但必须**连续 2 个评估日持续成立** (`guard_latency_bars = 2`) —— 对 Alpha#12 逆转袖套尤其关键，其入场本质上是单日脉冲。第 1 日打印 `PENDING_EXIT` / `HOLD_FILTERED`，原因 "平仓信号进入确认期 (第 1/2 日待确认)"。 | 调仓行携带 0.0% 的当日即成交，没有持续性要求。 |
| **4** | 个股硬止损、结构破位或账户熔断 | **立即 100% 卖出**。所有惰性门槛被彻底绕过——根本不读取阈值，因此不存在所谓 "0.1% 底线"。买入方向永远不享有该豁免。 | 当日该标的的 `asset_emergency_df` 掩码绕过惰性过滤。 |

* **已验证：熔断的减仓本身也要过惰性门槛，且按标的逐只判定，常被拒绝。** 减仓是**逐只标的**施加的：每个已持仓标的的权重都乘以同一个 `scale` 系数，余量归集到现金替代品 (`scripts/run_live_strategy.py:1766`; `pipeline/research/blend/blend_base.py:1069-1071`)。因此每只标的都是一次**独立的减仓，各自受自己的门槛约束** —— 回撤 15% 时系数为 0.5，6% 的仓位被砍到 3%，实际变动 3 个百分点，低于 5% 门槛，**当日被拒绝**；指令单动作显示 `HOLD`，原因 "调仓未超惰性阈值 (< 5%)"。50% 的减仓只有在**持仓占净值 $\ge 10\%$** 时才能过门槛。**实盘与回测两条路径都是如此**：实盘没有任何"风控减仓豁免"（`is_stop` 快通道只会清到 0.0，且仅限硬止损 / `is_emergency_halt`，`common/position_lifecycle.py:363-420`；`is_emergency_halt` 只有三级熔断才成立，`scripts/run_live_strategy.py:1927`），回测的惰性过滤同样会吞掉这笔减仓，除非当日资产级应急掩码被打开。只有规则 3/4 的全额清仓/止损路径才不受仓位大小限制。
* **两条路径在回撤 ∈ (17%, 20%) 区间还会分叉**：回测在 `scale_dd < 0.30` 即回撤 > 17% 时就打开豁免 (`pipeline/research/blend/blend_base.py:1072` → `is_global_emergency`，`common/allocation_templates.py:233-251`)，而实盘只在回撤 ≥ 20%（三级）才强制清仓。因此回撤落在 17%–20% 之间时，回测已执行减仓，实盘仍在拦截。
* **本文不规定任何"覆盖阈值"。** 遇到一级/二级熔断日，执行前须人工核对熔断档位与工单实际减仓量；被拒的减仓是否由人工补足，属于账户所有者的政策决定，手册不代为设定。

* **本策略没有相对减仓门槛**：未配置 `inertia_relative_ratio`，故减仓门槛就是固定的绝对 5%，不随持仓大小放大。
* **回测同样没有行级门槛**：只要有**任意**一个标的的目标权重发生变化，就会产出一行调仓记录，而该行携带全部标的——因此实盘指令单里的"逐标的静默"在回测的行级别上没有对应物。

> ⚠️ **实盘路径与回测路径在当前实现下是"设计上不同"的两套规则。** 规则 2 会拒绝回测在 2.5% – 3% 就愿意接受的全新仓位；规则 1 用固定 5% 拦截减仓，而回测用自适应 2.5% – 8%；规则 3 让每次清仓延后两个评估日。因此，**同一日期实盘持仓的标的数量可能合法地少于回测。** 不得为了让实盘"对齐"回测而放弃规则 2、按回测持仓数量补齐仓位，或抢跑平仓确认。

---

## 4. 实盘交易员快速决策卡 (Cheat Sheet)

| 触发环节 | 核心指标条件 | 交易员强制执行动作 |
| :--- | :--- | :--- |
| **阶段 1: 熔断** — **代码执行 (code-executed)** | $\text{Drawdown} \ge 20\%$ | 🔴 **全额清仓 (100% 现金)**，挂单卖出全部股票，冷冻 15 交易日 |
| **阶段 1: 阻尼** — **代码执行 (code-executed)** | $10\% \le \text{Drawdown} < 15\%$ | 🟡 **仓位平滑线性压缩**，下调目标权重，多余资金转入现金 |
| **阶段 2: 广度** | $\text{Breadth} \ge 30\%$ 或 $\text{Thrust} \ge 60\%$ | 🟢 **牛市进攻**，按模型积极配置高弹性三类买点龙头 |
| **阶段 3: 卖出** | 触发结构卖点 / 跌破 ATR 止损 / 调仓减持 | 🔴 **优先卖出** (14:20~14:35)，挂对手盘最优 1 档即时成交 |
| **阶段 4: 买入** | Alpha#12 逆转或三类买点加仓，目标 $\ge 5\%$ (新仓 $\ge 3\%$) | 🟢 **后买入** (14:35~14:45)，限价单买入，向下取整至 100 股 |
| **阶段 4: 零股** | 目标增量不足 1 手 (100股) | ⚪ **跳过不买**，等待下次资金充裕或继续维持原有股数 |
| **阶段 5: 惰性 (已持仓)** | 已持仓标的 $\lvert\Delta W\rvert < 5\%$ | ⚪ **维持不动 (HOLD)**，不报单 |
| **阶段 5: 惰性 (建仓)** | 新仓目标权重 $< 3\%$ | ⚪ **HOLD_FILTERED**：全新仓位低于 3% 一律不开 |
| **阶段 5: 平仓** | 已持仓标的模型目标 $= 0.0\%$ | 🕒 **等待确认**：信号连续 2 个评估日成立后才卖出 |
| **阶段 6: 现金** | 14:55 账户结余闲置资金 | 💰 **全额借出 GC001 / R-001**，锁定隔夜利息增厚 |

---

## 5. 生产发单命令规范

```bash
# 每日生产发单标准指令 (推荐在 14:20 运行):
uv run python scripts/run_live_strategy.py \
  --strategy chan_crisis_shield_blend \
  --universe-file docs/universe/china/core_satellite_22_stocks.txt \
  --output-dir docs/ruleset/chan_crisis_shield_blend \
  --portfolio-value 100000 \
  --data-provider marketdb
```
