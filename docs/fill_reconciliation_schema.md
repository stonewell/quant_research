# 成交回单与现金变动对账 Schema（冻结版 v1）

> **文档性质**：**冻结的接口契约（frozen contract）**，不是实施记录，也不是设计探讨。本文定义两张表的结构、单位、精度、时间戳语义与闭合恒等式；对账器的实现必须**消费**本文，不得反过来由实现倒推字段。
> **冻结日期**：**2026-11-05**。Schema 版本号：`fill_receipt/v1`、`cash_movement/v1`。
> **状态**：**FROZEN**。本文是 `LV4`（券商成交与现金账对账）的**第一项交付物**；在本文冻结之前**不允许写任何对账实现**（`LV4` 计划行第 1 条：「先冻结两张最小表的结构，再谈实现；**不得**先写解析再倒推字段」）。
> **任务状态 SSOT**：[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV4` 行是任务状态、优先级与验收的唯一权威；本文只维护**两张表的 schema**这一项 SSOT。本文**不维护**任务状态。
> **修订方式**：本文正文一经冻结即**不可改写**；任何变更以 **append-only** 的 `## Revision YYYY-MM-DD` 追加段落在文末落地（见 §11）。冻结文本 + 全部修订段共同构成现行契约，冲突时**修订段覆盖冻结文本**，但冻结文本的字面保留以便追溯。

## 0. 冻结范围、非目标与判断口径

### 0.1 本文冻结什么

| 项 | 冻结内容 | 章节 |
|---|---|---|
| 表 A | 成交回单（fill receipt）全部字段：名称、类型、单位、精度、必需性、语义 | §2 |
| 表 B | 现金变动（cash movement）全部字段：同上 + 方向符号约定 + sweep 判别 | §3 |
| 连接 | 票据行 ↔ 回单行 ↔ 现金流水行的连接键与基数 | §4 |
| 判定 | 成交结论的六值定义与短路优先级 | §5 |
| 算术 | 逐分闭合恒等式、容差、残差分类 | §6 |
| 时间 | 交易日 / 交收日 / 快照 as_of / 迟到成交 / 时区 | §7 |
| 缺省 | 必需字段 vs 可降级字段；缺必需字段的处置 | §8 |

### 0.2 非目标（本文明确**不**改变什么）

1. **不改变** `nav_breakdown.json` 的产出。该文件是 **Stage-1 门禁快照**（`web/livetrading/execution_service.py::_write_nav_breakdown`（`:2273-2304`）在门禁子进程启动前写出，Stage 1 经 `--nav-breakdown-file` 读它）；2026-11-11 前它曾被 Stage 2 覆写为部署后估值，现已拆分：Stage-2 的部署后估值另存 `nav_breakdown.stage2.json`，`summary.json` 裸名（`nav_effective` / `cash` / `cash_source` / `cash_as_of` …）为门禁值、`*_stage2` 族为部署值。其 `cash` 取自 `_resolve_nav`（`:2080`）→ `compute_nav`（`web/livetrading/valuation.py:688-736`），现金值经 `_archived_cash_for`（`:2020-2078`）回溯上一轮 `summary.json::cash`。该链路属**估值侧**，本文只**读取**其结果作为对账的「观测期末现金」。
2. **不改变**票据生成。`scripts/run_live_strategy.py:3491-3492` 写出 `live_trading_ticket.csv` 的行为、列集合、行序一律不动。
3. **不改变** `delta_weight` 算术。`trade_val = delta_w * portfolio_value`（`scripts/run_live_strategy.py:2798`）保持不变。
4. **不改变** sweep 逻辑。现金腿动作的赋值（`scripts/run_live_strategy.py:2440-2448`：`SWEEP_CASH` / `RELEASE_CASH`）保持不变。
5. **不改变** `cash_source` 语义（`"explicit"` / `"bil_fallback"` / `"missing"`，见 `valuation.py:154`）。
6. **不做**下单、不做资金划拨、不做任何写回。`LV4` 的边界是**只读对账**。
7. **不裁定成交事实**。本文**不声称**任何票据已成交或未成交；它只定义系统**如何判定并报告**成交状态，以及在**无法判定时必须记录什么**（§5、§9.4）。任何「某票据已成交」的陈述若出自本文，均为对本文的误读。

> 一句话：本文是把「成交事实」变成**可复核的算术**的那张纸，不是改引擎的那张纸。

### 0.3 判断口径

- 凡属**判断**而非**事实**的条目，本文同时写出**决定**与**一行理由**（`→ 理由：`）。
- 凡本文引用的 `file:line`，均为 **2026-11-05 本次会话核对过**的行号；未核对者不引用行号，只引用符号名。
- 本文**不**提出任何量化结论（不估收益、不估滑点分布、不推断成交概率）。

## 1. 冻结所依据的现状诊断

本节只陈述**可复核的事实**，作为 §5 / §6 判定规则的动机。

1. 票据由 Stage 2 `scripts/run_live_strategy.py` 在 `:3491-3492` 写出，现金腿金额由 `:2798` 的 `trade_val = delta_w * portfolio_value` 决定，现金腿动作在 `:2440-2448` 被赋为 `SWEEP_CASH`（`delta_w > 0`）或 `RELEASE_CASH`（`delta_w <= 0`）。
2. `nav_breakdown.json::cash` 是 Stage-1 **门禁快照**的现金腿（`_write_nav_breakdown`（`:2273-2304`）在门禁前写出；2026-11-11 前曾被 Stage 2 覆写为部署后估值，现 Stage-2 部署后值另存 `nav_breakdown.stage2.json` / `summary.json` 的 `*_stage2` 字段族），源头是上一轮归档的 `summary.json::cash`（`_archived_cash_for`，`:2020-2078`），即**操作员/归档值**，不是本轮的成交结果。
3. **全仓不存在成交回单表，也不存在现金流水账。** 与成交判定相关的内容只有若干**说明性注释**，明示「无回单」：`execution_service.py:2678`、`:3060-3062`、`:3215-3217`、`:3453`。
4. 实测形状（2026-10-30 / 2026-11-02）：BIL 的 `SWEEP_CASH` 票据在自身内部承认 **−10836.00** 的现金流出，而 `nav_breakdown.json::cash` 两轮均停在 **47800.06**；2026-10-29 → 10-30 现金由 **59063.46** 变为 **47800.06**（**−11263.40**），同样无流水可对。

> **由此得到的唯一结论**：「000157 是否成交」在现有归档内**不可判定**。因此本文的第一性要求是：**不可判定必须变成一个显式的、带差额的判定结果**，而不是一个默认值。`LV4` 计划行对此的表述是「`LV4` 只登记对账**能力**缺失，不登记对账**失败**」，本文遵循该区分。

## 2. 表 A — 成交回单（`fill_receipt/v1`）

### 2.1 表示约定（先于字段，属于契约本身）

| 概念 | 表示 | 理由（一行） |
|---|---|---|
| 数量 | **整数股**（`int64`，A 股按手/整股；港股按股；债券按张） | 分数股不存在，用整数即可消除「1800 股 vs 1800.0000001 股」的比较误差 |
| 金额 | **整数分**（`int64`，字段名一律以 `_cents` 结尾） | 浮点累加会产生「差 1 分」的假残差，而对账容差被定为**恰好 0**，浮点不可用 |
| 价格 | **整数价格刻度** `price_ticks`，`int64`，1 tick = **0.0001 元**（字段名以 `_ticks` 结尾） | A 股最小变动 0.01 元、基金/债券可到 0.001 元；刻度取 4 位可无损承载三者，且仍是整数 |
| 比率/权重 | 不进入表 A / 表 B | 收益率不属于「成交事实」，混入会让两张表不再是账本 |
| 边界 | 任何字段**不得**以 IEEE-754 `float` 存储或跨进程传递 | 浮点是本条最常被引用的具名风险（`LV4` 计划行「精度」要求） |

金额换算恒等式：`notional_cents = filled_quantity × fill_price_ticks ÷ 100`。**除法必须整除**；不能整除到分时**不得**就地舍入，而是按 §6.3 记 `price_rounding` 并以券商流水金额为权威值。

### 2.2 字段表

| 字段 | 类型 | 单位 / 精度 | 必需性 | 语义 |
|---|---|---|---|---|
| `fill_id` | `str` | — | **必需** | 回单行主键。券商给出的成交编号；券商不给时用 `sha256(source_file ‖ source_row_no)` 派生（**派生 ≠ 留空**） |
| `run_id` | `str` | — | **必需** | 对账所属的归档轮次标识，取 `web/livetrading/data/runs/<run_dir>` 的目录名；同一 `run_id` 下票据与流水必须同源 |
| `ticket_id` | `str` | — | **必需**（派生） | 我方票据行连接键，定义见 §4.1。回单侧若无此列，由 `broker_order_id` 反查票据；反查不唯一即 `unreconciled` |
| `broker_order_id` | `str` | — | **必需**（撤单前即已撤销的委托可缺） | 券商委托号。一笔委托下的多笔成交共享同一 `broker_order_id` |
| `symbol` | `str` | 归一化 `000157.SZ` / `600519.SH` / `BIL` | **必需** | 标的代码。必须与票据 `symbol` 列**同构**（含交易所后缀），不做大小写/后缀的隐式归一 |
| `side` | `enum` | `BUY` / `SELL` | **必需** | 方向。不允许 `B`/`S`、`买入` 等别名进入本字段，别名只能在解析层映射 |
| `ordered_quantity` | `int64` | 股 | **必需** | 该委托的**委托**数量（非本笔成交量）；部分成交时同 `broker_order_id` 的每行取同一值 |
| `filled_quantity` | `int64` | 股 | **必需** | **本行**成交数量，`>= 0`；`status=rejected/cancelled` 的行必须为 `0` |
| `fill_price_ticks` | `int64` | 1e-4 元 | **必需**（`filled_quantity == 0` 时可为 `0`） | 本行成交均价。**分笔成交时每笔一行**，不合并 |
| `notional_cents` | `int64` | 分，非负 | 派生 | `filled_quantity × fill_price_ticks ÷ 100`，见 §2.1 |
| `commission_cents` | `int64` | 分，**非负** | **必需** | 佣金，存**绝对值**，符号由 §2.3 公式施加 |
| `other_fees_cents` | `int64` | 分，**非负** | **必需**（可合并进 `commission_cents` 并置 `fee_breakdown_available=false`） | 印花税 / 过户费 / 经手费 / 证管费合计 |
| `total_fees_cents` | `int64` | 分，非负 | 必需 | `commission_cents + other_fees_cents` |
| `fee_breakdown_available` | `bool` | — | 可选（默认 `true`） | 券商只给合计费用时置 `false`，此时三项费用字段可只填 `total_fees_cents` |
| `cash_impact_cents` | `int64` | 分，**带符号** | **必需** | 本行成交**不含费用**的现金影响：`BUY → −notional`，`SELL → +notional` |
| `cash_impact_net_cents` | `int64` | 分，带符号 | 派生 | `cash_impact_cents − total_fees_cents`（见 §2.3） |
| `currency` | `str` | ISO-4217，`CNY` / `HKD` / `USD` | **必需** | 计价币种。跨币种不得相加，必须先分组 |
| `fill_ts` | `str` | ISO-8601 **带数字时区偏移**，如 `2026-11-02T10:14:33.512+08:00` | **必需** | 成交时刻。**禁止**无偏移的裸时间戳 |
| `fill_tz` | `str` | IANA，如 `Asia/Shanghai` | 可选（可从 `fill_ts` 偏移与交易所推出） | 成交发生地的时区名，用于跨夏令时换算 |
| `trade_date` | `date` | `YYYY-MM-DD`，**交易所本地日** | **必需** | 成交归属交易日，决定该笔成交记入哪个轮次（§7） |
| `settle_date` | `date` | `YYYY-MM-DD` | 可选（缺则按交易日历 + T+1 推导，并置 `settle_date_derived=true`） | 资金交收日，决定现金可用性，**不**决定轮次归属 |
| `status` | `enum` | `FILLED` / `PARTIAL_FILL` / `REJECTED` / `CANCELLED` | **必需** | 本行状态：`PARTIAL_FILL` 表示「该委托整体未全成，本行是其部分成交」；`FILLED` 表示该委托已全成 |
| `status_reason` | `str` | — | 可选 | 券商状态原文或拒单原因（如 `资金不足` / `涨跌停无对手方`），仅用于人读，不参与判定 |
| `account_id` | `str` | — | 可选（单账户账户可缺省） | 资金账户号。多账户时必须填，否则跨账户合并会伪造成交 |
| `exchange` | `str` | `SSE`/`SZSE`/`HKEX`/`NASDAQ` | 可选（可由 `symbol` 后缀推出） | 交易场所，决定时区与交易日历 |
| `source_file` | `str` | — | 可选 | 原始回单文件名，用于取证 |
| `source_row_no` | `int64` | 1-based | 可选 | 原始行号，与 `source_file` 共同定位证据 |
| `ingested_at` | `str` | ISO-8601 带偏移 | 可选 | 入库时刻（**非**成交时刻） |
| `raw_row_hash` | `str` | `sha256` 十六进制 | 可选 | 原始行指纹，防重复导入 |

### 2.3 现金影响符号约定（表 A 的规范部分）

以**资金账户**为视角，`−` = 现金流出账户：

```
BUY :  cash_impact_cents     = − (filled_quantity × fill_price_ticks) ÷ 100
       cash_impact_net_cents =   cash_impact_cents − total_fees_cents      # 两者均为负
SELL:  cash_impact_cents     = + (filled_quantity × fill_price_ticks) ÷ 100
       cash_impact_net_cents =   cash_impact_cents − total_fees_cents      # 净流入 = 卖出额 − 费用
```

统一写法（`s = +1` 表示 `BUY`，`s = −1` 表示 `SELL`）：

```
cash_impact_net_cents = − s × notional_cents − total_fees_cents
```

> **约定**：费用**永远**以非负绝对值存储，方向由公式施加。理由：若允许费用自身带符号，同一笔成交可以被写成两组符号组合都「看起来闭合」，残差会被符号错误掩盖。

## 3. 表 B — 现金变动（`cash_movement/v1`）

### 3.1 符号约定（本表的规范部分）

以**资金账户**为视角：

- **`amount_cents < 0` ⇒ 现金流出（出金）**；`amount_cents > 0` ⇒ 现金流入（入金）。
- `amount_cents == 0` **不是**合法流水行（零金额流水是噪声，必须丢弃并在解析层计数）。
- `direction` 为派生字段（`IN` / `OUT`），**必须**与 `amount_cents` 的符号一致；不一致即数据损坏，该行直接判 `unreconciled`。
- 币种分组：不同 `currency` 的 `amount_cents` **不得**相加。

### 3.2 字段表

| 字段 | 类型 | 单位 / 精度 | 必需性 | 语义 |
|---|---|---|---|---|
| `cash_movement_id` | `str` | — | **必需** | 流水行主键（券商流水号；无号则 `sha256(source_file ‖ source_row_no)` 派生） |
| `run_id` | `str` | — | **必需** | 所属归档轮次（决定它能否计入该轮闭合，见 §7.3） |
| `ticket_id` | `str` | — | 交易/sweep 类**必需**；`dividend`/`deposit`/`withdrawal` 类必为空 | 连接我方票据行（§4.1） |
| `fill_id` | `str` | — | 可选（见 §4.2 的聚合决定） | 连接到具体成交行；按票据聚合的 `trade_settlement` 行可置 `null` 并以 `ticket_id` 连接 |
| `account_id` | `str` | — | 可选（单账户可缺省） | 资金账户号 |
| `currency` | `str` | ISO-4217 | **必需** | 币种，闭合恒等式按币种分别成立 |
| `amount_cents` | `int64` | 分，**带符号** | **必需** | 变动金额，`−` = 出金（§3.1） |
| `direction` | `enum` | `IN` / `OUT` | 派生 | 由 `amount_cents` 符号推出，且必须一致 |
| `balance_after_cents` | `int64` | 分 | **必需** | 本笔之后的账户余额。它是闭合恒等式的**唯一定点锚**：抽取排序后 `balance_after_cents` 必须逐笔递增一致 |
| `movement_ts` | `str` | ISO-8601 带偏移 | **必需** | 记账时刻（银行/券商侧的入账时间），非交易日 |
| `movement_tz` | `str` | IANA | 可选（可从 `movement_ts` 偏移推出） | 记账时区 |
| `value_date` | `date` | `YYYY-MM-DD` | **必需** | 资金交收/起息日（A 股 T+1 交收；逆回购为到期日） |
| `trade_date` | `date` | `YYYY-MM-DD` | 交易/sweep 类必需 | 对应的成交交易日 |
| `source` | `enum` | `trade_settlement` / `cash_sweep` / `dividend` / `fee` / `deposit` / `withdrawal` | **必需** | 变化来源，见 §3.3 |
| `income_type` | `enum` | `dividend` / `interest` / `repo_interest` / `null` | 可选（仅 `source=dividend`） | 收益细分。`dividend` 一词在本 schema 中**兼指**分红、存款利息与回购利息 |
| `instrument_is_cash_proxy` | `bool` | — | 仅 `source=cash_sweep` 必需 | 本笔 sweep 的对手标的是否为现金代理（`BIL` / `511880` / `GC001`） |
| `counter_symbol` | `str` | — | 仅 `source=cash_sweep` 必需 | 对手标的代码 |
| `external_ref` | `str` | — | 可选 | 银行流水号 / 券商对账单号，跨系统取证的唯一凭据 |
| `recon_status` | `enum` | `matched` / `unmatched` | 派生 | 是否已连接到票据或已归类为无票据类流水 |
| `source_file` / `source_row_no` / `ingested_at` / `raw_row_hash` | 同表 A | — | 可选 | 取证与幂等 |

### 3.3 `source` 枚举与符号

| `source` | 典型符号 | 是否需 `ticket_id` | 说明 |
|---|---|---|---|
| `trade_settlement` | 买 `−` / 卖 `+` | **必需** | 证券买卖的资金交收。**不含费用**——费用单独成行（§3.4） |
| `fee` | `−` | **必需** | 佣金/印花税/过户费等单独记账的扣费行。若券商把费用并入交收额，则该轮 **只能有一行** `trade_settlement` 且 `fee_breakdown_available=false`，不允许既并计又另出费行（重复计费） |
| `cash_sweep` | 扫入 `−` / 释放 `+` | **必需** | 闲置资金归集（`BIL` / `511880`）与国债逆回购（`GC001`）本金。**本金与利息必须分行** |
| `dividend` | `+` | 禁止 | 现金分红、存款利息、逆回购利息（`income_type` 区分）。**允许**为负（红利税补扣），此时 `income_type=dividend` |
| `deposit` | `+` | 禁止 | 外部入金 |
| `withdrawal` | `−` | 禁止 | 外部出金 |

> **判断**：逆回购到期返还「本金 + 利息」拆成两条流水（本金 `cash_sweep` 正向、利息 `dividend`/`repo_interest`）。
> → 理由：合并会让 `cash_sweep` 的本金闭合恒等式（扫出本金 = 到期返还本金）在数学上不成立，任何一次 GC001 滚动都会产生假残差。

### 3.4 sweep 与 trade 的判别（三条件同时成立才算同一事件）

一笔「买入 `BIL`」与一笔「买入 `000157`」在资金侧**都是负数**，金额相同也**不构成**同一事件的证据。判别必须同时满足：

1. **票据侧**：`action ∈ {SWEEP_CASH, RELEASE_CASH}`，且该行 `symbol ∈ {BIL, 511880, GC001}`；
2. **流水侧**：`source = cash_sweep`，且 `instrument_is_cash_proxy = true`；
3. **金额一致**：`|cash_movement.amount_cents| == |ticket.trade_value|` 换算成分后**逐分相等**。

任一条不成立 ⇒ 该 sweep 票据判 `unreconciled`，**不得**按金额相近自动匹配。

> **判断**：sweep 的对手标的是**现金代理**，其经济含义是「现金换了个容器」，与买入风险资产不同，故必须靠 `source` + `instrument_is_cash_proxy` 显式标注，而不是靠金额。
> → 理由：否则「卖出 10,836 元股票、同时扫入 10,836 元 BIL」这一组真实流水会被误判成「一买一卖相互抵消」。

## 4. 连接键与基数

### 4.1 票据行 → 回单行：`ticket_id`

`live_trading_ticket.csv` 的现有列集合（`symbol, name, action, price, current_weight, target_weight, delta_weight, current_shares, target_shares, delta_shares, shares, trade_value, holding_bars, reason, summary_reason, blocking_reason`）**没有**任何票据 ID 列；全仓也**不存在** `ticket_id` 字段。

> **决定**：`ticket_id` 为**派生键**，形式固定为
> `ticket_id = f"{as_of or execution_date}:{row_ordinal:04d}:{symbol}:{action}"`
> 其中 `row_ordinal` 是该行在 `live_trading_ticket.csv` 中的 **0-based 从 1 计**行序，`as_of` / `execution_date` 取自 `live_trading_ticket.json` 的 LV2 信封（`common/live_execution_timing.py:337-349`）。
> → 理由：给票据**加一列**会改变票据产出，而 §0.2 第 2 条明确禁止；归档票据是**不可变**的，行序因此是稳定的，派生键可复算、可复核、无需写回。

派生键的三条约束：

1. 同一 `run_id` 内**必须唯一**；若派生冲突（同 symbol 同 action 出现两行且 `as_of` 相同），则该两行**全部**判 `unreconciled` 并报 `AMBIGUOUS_TICKET_KEY`——不允许用「顺手取第一行」消解。
2. 一旦某票据被某份回单引用，其派生键**冻结**；后续票模板变更导致键变形时，旧键以别名表映射，不改历史。
3. `ticket_id` 只标识**票据行**，不标识「意图」。同一票据跨轮重发（补跑）会得到不同的 `ticket_id`（`as_of` 不同），这是刻意的：两轮各自的成交必须各自记账。

### 4.2 基数表

| 关系 | 基数 | 规则 |
|---|---|---|
| 票据行 → 回单行 | **1 : N** | 一笔委托可部分成交多次；每次成交一行，共用 `broker_order_id` |
| 回单行 → `trade_settlement` 流水 | **N : 1（按票据聚合）** | 同一 `ticket_id` 的所有成交合并为**一条**交收流水（A 股 T+1 净额交收） |
| 回单行 → `fee` 流水 | **1 : N** 或 **N : 1** | 券商逐笔计费则 1:N；按日合并计费则 N:1，两者**只能选其一**（由 `fee_breakdown_available` 与 `source` 分布共同判定，禁混用） |
| 票据行 → `cash_sweep` 流水 | **1 : 1** | 一笔 sweep 票据对应一条本金流水；本金与利息已按 §3.3 拆开，故不会出现 1:N 的本金 |
| 流水 → 票据 | **N : 1**（交易/sweep 类）；**0 : 1**（`dividend`/`deposit`/`withdrawal`/无主 `fee`） | 无票据类流水不参与 §6 的票据闭合，只参与 §6 的账户闭合 |

> **判断（部分成交的现金侧）**：`trade_settlement` 按**票据聚合**为一条，不逐笔出流水。
> → 理由：A 股资金交收本来就是 T+1 按委托/证券净额，逐笔流水在券商对账单上**不存在**；强行逐笔会要求对账器自己造流水，那就不是对账而是编账。

> **判断（部分成交的金额闭合）**：聚合流水金额必须等于 `Σ_fill cash_impact_net_cents` **逐分相等**；不等即 `unreconciled`，差分单列。
> → 理由：聚合是「N:1」，唯一可校验的量就是总和；若总和也不等，说明存在未回单的成交或漏记费用。

## 5. 结论判定：六值定义与短路优先级

### 5.1 两个正交轴（先说清结构，再给枚举）

对账结论被拆成**两个字段**，不压成一个：

| 字段 | 取值域 | 回答的问题 |
|---|---|---|
| `quantity_outcome` | `filled` / `partial` / `unfilled` / `rejected` / `cancelled` / **`null`**（`null` = 证据不足，**不给**数量结论） | **成交了多少**（数量事实） |
| `recon_status` | `reconciled` / `unreconciled` | **这份事实是否可信**（证据是否充分、账是否闭合） |

对外呈现的**单一结论** `outcome` 取二者的**降级合成**：

```
outcome = unreconciled            if recon_status == unreconciled
        = quantity_outcome        otherwise
```

> **理由**：若把「未成交」和「不知道有没有成交」压进同一枚举，就只能靠约定区分二者，而约定迟早会被读错——`LV4` 计划行第 3 条正是要求二者**语义可分**且 `unreconciled` **显式**。分成两轴后，「票说 −10836、账上没动」这类情形可以同时保留「票据侧金额」与「不可判定」两个事实。

### 5.2 六值定义（互斥）

| 值 | 定义（充要） |
|---|---|
| `filled` | `Σ filled_quantity == ordered_quantity`，且所有相关回单行 `status ∈ {FILLED, PARTIAL_FILL}`，且无未了结委托 |
| `partial` | `0 < Σ filled_quantity < ordered_quantity`，且该委托已终态（无在途） |
| `unfilled` | `Σ filled_quantity == 0`，且**存在**明确回单证明「有委托但零成交」（如过期未成交、废单） |
| `rejected` | 该票据的全部回单行 `status == REJECTED`（券商拒绝受理，从未进入撮合） |
| `cancelled` | 该票据存在 `status == CANCELLED` 且 `Σ filled_quantity == 0`（撤单先于任何成交） |
| `unreconciled` | 证据不足或账不闭合：无回单、必需字段缺失、连接键歧义、现金闭合残差非零、观测期末现金不可得（`cash_source == "missing"`） |

**`unreconciled` 是「大声」状态**：它必须带上差额与原因码，**不得**回退成 `filled` 或 `unfilled` 的任何默认值。允许的原因码（封闭集合，新增需走 §11 修订）：

| 原因码 | 触发 |
|---|---|
| `NO_FILL_RECEIPT` | 票据行在回单侧零匹配 |
| `MISSING_REQUIRED_FIELD` | 必需字段缺失（须列出字段名） |
| `NO_LEDGER` | 该轮不存在现金流水账，闭合恒等式**无法验证**（真空闭合不算闭合） |
| `CASH_NOT_MOVED` | 存在应发生现金影响的票据，但流水侧对应金额为 0 |
| `CASH_RESIDUAL` | §6 闭合残差 ≠ 0 |
| `AMBIGUOUS_TICKET_KEY` | `ticket_id` 派生冲突或 `broker_order_id` 反查不唯一 |
| `DIRECTION_MISMATCH` | `amount_cents` 符号与 `direction` 不一致 |
| `LATE_ARRIVAL` | 成交时间晚于该轮 `as_of`（§7.3） |

### 5.3 优先级（自上而下，先命中者胜）

| 序 | 规则 | 产出 |
|---|---|---|
| **P0** | 任一必需字段缺失，或 `ticket_id` 歧义，或币种缺失 | `recon_status=unreconciled`（`MISSING_REQUIRED_FIELD` / `AMBIGUOUS_TICKET_KEY`） |
| **P1** | 该票据零回单行 | `unreconciled` / `NO_FILL_RECEIPT`（**不是** `unfilled`） |
| **P2** | 成交时间晚于本轮 `as_of` | `unreconciled` / `LATE_ARRIVAL`，且该笔**不计入本轮**（§7.3） |
| **P3** | 全部回单行 `REJECTED` | `rejected` |
| **P4** | 存在 `CANCELLED` 且 `Σfilled == 0` | `cancelled` |
| **P5** | `Σfilled == 0` 且有终态回单 | `unfilled` |
| **P6** | `0 < Σfilled < ordered` | `partial`（若同时存在撤单，`quantity_outcome` 仍为 `partial`，`cancel_cause` 记录于 `status_reason`） |
| **P7** | `Σfilled == ordered` | `filled` |
| **P8** | §6 现金闭合残差 ≠ 0，或 `NO_LEDGER`，或观测期末现金不可得 | **把 P3–P7 的结论整体降级**为 `unreconciled`，原结论保留在 `quantity_outcome` 字段 |

> **P8 的理由**：数量事实与账务事实可以同时为真且互相矛盾（今天就是这个形状：回单可以说「成交了」，而账上钱没动）。降级而不是覆盖，是为了让「成交了但账没动」可被**直接读出**，而不是被读成「没成交」。

### 5.4 幂等与确定性

相同 `(票据, 回单, 流水, 期初现金, as_of)` 输入必须得到**逐字段相同**的 `outcome`、`quantity_outcome`、`reason_codes`、`difference_cents`。排序必须是**全序**（`movement_ts`, `cash_movement_id`）；禁止依赖文件遍历顺序。

## 6. 闭合恒等式（逐分）

### 6.1 两条恒等式，都必须成立

**（I）账户内部闭合（账户级）**

```
closing_cash_cents(currency) == opening_cash_cents(currency) + Σ_{b ∈ B(currency)} b.amount_cents
```

其中 `B(currency)` 是该轮**计入**（§7.3）的全部流水行，按币种分组，不跨币种求和。

**（II）票据 ↔ 账本闭合（逐票据）**

对每张 `quantity_outcome ≠ unreconciled` 的票据 `t`：

```
Δcash_observed(t) == Σ_fill cash_impact_net_cents(f)
```

其中 `Δcash_observed(t) = Σ_{b: b.ticket_id == t} b.amount_cents`。

展开成可读形式（`s = +1` 买、`s = −1` 卖）：

```
BUY :  Δcash(t) == − Σ_f (q_f × p_f) − Σ_f fees_f
SELL:  Δcash(t) == + Σ_f (q_f × p_f) − Σ_f fees_f
统一:  Δcash(t) == − s_t × Σ_f (q_f × p_f) − Σ_f fees_f
```

**（III）全局一致性（由 I、II 导出，必须独立断言）**

```
closing_cash_cents − opening_cash_cents
  == Σ_{票据类流水} amount_cents + Σ_{无票据类流水} amount_cents
  == Σ_{已对账票据} cash_impact_net_cents(t) + Σ_{dividend|deposit|withdrawal|无主 fee} amount_cents
```

三条恒等式都必须成立；只验 (I) 会漏掉「票出钱不动」（§9 正是这种情形：(I) 在无流水时**真空成立**）。

### 6.2 容差：**恰好 0 分**

- 每条恒等式、每个币种、每一轮，残差必须 `== 0`（整数分）。
- **没有**百分比容差，**没有**「±1 分以内算过」，**没有**「近似相等」分支。
- 残差 `≠ 0` ⇒ 该轮 `recon_status = unreconciled`，并记录 `difference_cents = 实际残差`。
- `difference_cents` 是**逐闭合项的字段**，不是一个全局标量：恒等式 (I) 按币种各一个；恒等式 (II) 按票据各一个；成交组合不可判定时按**候选**各一个（§9.4）。实现不得把这些残差压成一个数，也不得只报其中一个。

### 6.3 非零残差的分类（只有两类，且都得记账）

| 类别 | 判据 | 处置 |
|---|---|---|
| `price_rounding` | 我方 `q×p` 换算到分后不能整除，且**券商流水金额**与 `closing_cash` 逐分闭合 | 不构成残差。差异单列为 `rounding_note_cents`（每笔 `abs(note) <= 1` 分），`recon_status` 仍可为 `reconciled`，但 note 必须随结果输出 |
| `genuine_discrepancy` | 以上不成立 | `unreconciled`；`difference_cents` 为**带符号**的实际差额；不得以任何舍入解释吸收 |

> **判断**：闭合以**券商流水金额**为权威，自算金额只做交叉校验。
> → 理由：自算金额是我们对成交的**推断**，券商流水是**观测**；让推断去修正观测，就会把「账错了」写成「算错了」。

### 6.4 不可删除的断言清单（对应 `LV4` 验收第 4 条）

在实现对账器时，下列断言每一条都必须存在，且**删掉任意一条即测试转红**：

| # | 断言 |
|---|---|
| A1 | 无回单 ⇒ `outcome == unreconciled` 且 `reason_codes` 含 `NO_FILL_RECEIPT`，且票据侧金额原样保留可查 |
| A2 | 无流水账 ⇒ `reason_codes` 含 `NO_LEDGER`，**不得**因 `0 == 0` 判 `reconciled` |
| A3 | 全额成交 ⇒ `filled` 且 §6.1(I)(II)(III) 残差均为 0 分 |
| A4 | 部分成交 ⇒ `partial`，且聚合流水 == `Σ cash_impact_net_cents`（逐分） |
| A5 | 拒单 ⇒ `rejected`，且该票据不产生任何 `trade_settlement` 流水 |
| A6 | 票据声称现金影响但流水为 0 ⇒ `CASH_NOT_MOVED` + `difference_cents` 非零（即 §9 形状） |
| A7 | `balance_after_cents` 逐笔递推一致 ⇒ 否则 `CASH_RESIDUAL` |

## 7. 时间戳、时区与迟到成交

### 7.1 三个不同的「日期」

| 概念 | 字段 | 决定什么 |
|---|---|---|
| 成交交易日 | `fill_receipt.trade_date` | **该笔成交归属哪一轮**（唯一的轮次归属依据） |
| 资金交收日 | `fill_receipt.settle_date` / `cash_movement.value_date` | 现金**何时可用**（A 股股票 T+1；债券/逆回购按合约） |
| 本轮快照时刻 | LV2 信封 `as_of`（`common/live_execution_timing.py:337-349`） | 对账的**观测截止面**；晚于此的成交不属于本轮 |

> **决定**：「as of」在**数量**上取 `trade_date <= as_of`，在**现金**上取 `movement_ts <= as_of`。
> → 理由：持仓是可交易事实（T 日成交即入账于 T 日），现金可用性是交收事实（T+1 才可用）；把二者混成同一个日期会让每一次 T+1 都产生一天的假残差。

### 7.2 时区规则

1. 所有时间戳必须是 **ISO-8601 带数字偏移**；裸时间戳一律视为**格式错误**（属 `MISSING_REQUIRED_FIELD` 范畴）。
2. 交易日/交收日按**交易所本地日**判定（SSE/SZSE → `Asia/Shanghai`；HKEX → `Asia/Hong_Kong`；美股 → `America/New_York`）。
3. 存储可统一为 UTC，但**判定必须**先还原到交易所本地日；否则 23:30 `+08:00` 的成交会被判成前一日。
4. 跨夏令时（美股）以 IANA 时区名 + 交易日历换算，不用固定偏移加减。
5. **不做时钟容差**：`movement_ts` 早于 `fill_ts` 属异常（资金先于成交入账），记 `TIMESTAMP_ORDER` 告警，但不自动判 `unreconciled`（部分券商存在预冻结记账）；该判断需在实现时以真实对账单确认。

### 7.3 迟到成交（late arrival）

| 情形 | 处置 |
|---|---|
| `fill_ts <= as_of` 且 `trade_date <= as_of` 的交易日 | 计入本轮 |
| `fill_ts > as_of`，但 `trade_date == as_of` 的交易日 | **不计入本轮**。记 `LATE_ARRIVAL`，作为**结转（carry）**进入下一轮 |
| 下一轮到来后该笔仍未入账 | 下一轮继续记 `LATE_ARRIVAL` 并累加 `carry_age_rounds`；**不得**因为「久了就算了」而丢弃 |
| 已闭合快照 | **禁止回溯修改**。归档 `nav_breakdown.json` / `summary.json` 是已发布的测量值，任何补记只**追加**在下一轮 |

> **决定**：迟到成交不重写已闭合快照，只作为下一轮的期初调整（carry）。
> → 理由：`nav_breakdown.json::cash` 是 Stage 1 熔断门禁的输入（`LV1` 已将其改为读冻结净值），回溯改写会让一个**已据此做过决策**的门禁输入事后变形，等于让风控无法被审计。

## 8. 最小可用字段集（必需 / 可降级）

### 8.1 必需字段（缺失即 `unreconciled`，**绝不**取默认值）

**表 A 必需**：`fill_id`、`run_id`、`ticket_id`、`symbol`、`side`、`ordered_quantity`、`filled_quantity`、`fill_price_ticks`、`total_fees_cents`、`currency`、`fill_ts`（带偏移）、`trade_date`、`status`。
（`broker_order_id` 对「撤单前即已撤销」的委托可缺，其余必需。）

**表 B 必需**：`cash_movement_id`、`run_id`、`currency`、`amount_cents`、`balance_after_cents`、`movement_ts`（带偏移）、`value_date`、`source`；交易/sweep 类另需 `ticket_id`。

### 8.2 可降级字段（可缺、可由规则推导，且必须标注推导标记）

| 字段 | 降级方式 | 标记 |
|---|---|---|
| `settle_date` | 按交易所交易日历 + T+1 推导 | `settle_date_derived=true` |
| `fill_tz` / `movement_tz` | 由 `fill_ts` 偏移 + 交易所推出 | `tz_derived=true` |
| `commission_cents` / `other_fees_cents` | 合并为 `total_fees_cents` | `fee_breakdown_available=false` |
| `account_id` | 单账户账户可缺省 | `single_account_assumed=true` |
| `exchange` | 由 `symbol` 后缀推出 | `exchange_derived=true` |
| `status_reason`、`source_file`、`source_row_no`、`ingested_at`、`raw_row_hash` | 纯取证用途，可缺 | — |

### 8.3 缺失处置（fail-loud）

1. 缺任一必需字段 ⇒ 该票据/该流水判 `unreconciled`，`reason_codes` 含 `MISSING_REQUIRED_FIELD`，并把**缺失字段名列表**原样输出。
2. **禁止**以 `0`、`""`、`None`→默认值、`ffill` 上一轮值等方式补必需字段。
3. 券商导出**没有** `price` 但有 `amount` 时，允许按 `price_ticks = amount_cents × 100 ÷ qty` 反解，且必须整除；不整除 ⇒ `unreconciled`（反解出的价格不是观测值）。
4. 允许的降级**只有** §8.2 列出的项；新增降级项须走 §11 修订。

## 9. 实测案例逐字段推演（2026-11-02）

本节用 §2–§6 的契约把「票出钱不动」这个实际形状走完一遍。参与金额均为整数分，逐分精确。

### 9.1 输入

| 项 | 值 | 来源 |
|---|---|---|
| `run_id` | `2026-11-02`（归档轮次） | `data/runs/<date>/` |
| `opening_cash_cents` | **4,780,006**（= 47,800.06 元） | 上一轮 `summary.json::cash`，`cash_source="explicit"`，经 `_archived_cash_for`（`:2020-2078`）解析 |
| 表 A 回单 | **0 行** | 该轮归档无任何回单文件 |
| 表 B 流水 | **0 行** | 该轮归档无任何现金流水账 |
| 观测 `closing_cash_cents` | **4,780,006**（= 47,800.06 元） | 本轮 `nav_breakdown.json::cash`（`_write_nav_breakdown`，`:2273-2304`） |

### 9.2 票据侧（只读，不改）

**T1 — 000157 买入**

| 字段 | 值 |
|---|---|
| `ticket_id` | `2026-11-02:<row_ordinal>:000157.SZ:BUY`（行序由归档实测填列，本节不预填） |
| `symbol` | `000157.SZ` |
| `side` | `BUY` |
| `ordered_quantity` | 1800 |
| `fill_price_ticks`（票据 `price`） | 60,200（= 6.0200 元） |
| `notional_cents` | 1800 × 60,200 ÷ 100 = **1,083,600**（= 10,836.00 元） |
| `total_fees_cents` | **不可得**（票据不含量费用） |
| 期望 `cash_impact_net_cents` | **−1,083,600**（费用未计；费用只会让它更负） |

**T2 — BIL 扫入现金**

| 字段 | 值 |
|---|---|
| `ticket_id` | `2026-11-02:<row_ordinal>:BIL:SWEEP_CASH` |
| `symbol` | `BIL` |
| `action` | `SWEEP_CASH`（`:2440-2448`，`delta_w > 0`） |
| 票据 `trade_value` | **−10,836.00** ⇒ 期望 `amount_cents = −1,083,600` |
| 期望 `source` | `cash_sweep`，`instrument_is_cash_proxy=true` |

### 9.3 逐步判定

| 步 | 动作 | 结果 |
|---|---|---|
| S1 | 表 A 匹配 T1 | 0 行 ⇒ `NO_FILL_RECEIPT` |
| S2 | 表 A 匹配 T2 | 0 行 ⇒ `NO_FILL_RECEIPT` |
| S3 | 恒等式 (I)：`4,780,006 == 4,780,006 + 0` | **算术成立，但真空成立** ⇒ `NO_LEDGER`（§6.4 A2：真空闭合不算闭合） |
| S4 | 恒等式 (II) **以「T1 已按票据成交」为条件**：`Δcash_observed(T1) == 0` vs 票据声称 `−1,083,600` | 差 **1,083,600 分** ⇒ `CASH_NOT_MOVED`（这是**条件差额**：它证明「若 T1 成交则账不对」，**不**证明 T1 成交） |
| S5 | `quantity_outcome` | `null`（无数量结论；P1 短路，不给「未成交」） |
| S6 | `recon_status` | `unreconciled` |

### 9.4 观测与候选（只报告差额，**不裁定**）

**（一）观测（datum，先说事实）**

`nav_breakdown.json::cash` 在 2026-10-30 与 2026-11-02 两轮**均为 `47800.06`（4,780,006 分）**，即该账本在两轮之间**没有变动**。

这是**观测值，不是结论**。它究竟意味着「未成交」、「成交但记账延迟」还是「记账缺陷」，从本组工件（0 张回单、0 条流水）**不可判定**。本节只把这一观测与票据侧的现金声称量并列，把差额算出来。

**（二）候选集合（对称并列，不排序、不指定主候选）**

由票据可派生的成交组合只有三种，本节按对称方式列出。`观测 − 应有` 的符号约定：**正数 = 账上比该候选「本该有的余额」多出的钱**。

| 候选 | 假设成交的腿 | 应有 `closing_cash_cents` | 观测 − 应有 | 对应元 | 判定 |
|---|---|---|---|---|---|
| **C1** | 仅 T1（000157 买入） | 4,780,006 − 1,083,600 = **3,696,406** | **+1,083,600** | +10,836.00 | `unreconciled` |
| **C2** | T1 与 T2（BIL 扫入）皆成交 | 4,780,006 − 2,167,200 = **2,612,806** | **+2,167,200** | +21,672.00 | `unreconciled` |
| **C3** | 两腿均未成交 | 4,780,006 − 0 = **4,780,006** | **0** | 0.00 | `unreconciled` |

三条判定**全部**是 `unreconciled`，理由分列：

- **C1 / C2**：票据声称了现金流出，而流水侧为 0 ⇒ `CASH_NOT_MOVED` + `NO_LEDGER`，账**不闭合**，差额即表中数值。
- **C3**：差额恰为 0，但**不得**据此判「未成交」。它的 0 差额来自「假设什么都没发生」，而这一假设本身**同样没有证据**（§6.4 A2：真空闭合不算闭合）。把一个与观测自洽的空假设升格为结论，等于用「没有被证伪」冒充「已被证实」。

**（三）本文件不对 `36,964.06` 的立场（显式声明）**

`LV4` 计划行中的 **`36,964.06`** 是 **2026-11-03 登记时点**写入的推定值，本次会话简报将其作为上下文转述。本文**不**把它当作 ground truth，**也绝不**因某个候选与它逐分相同而把该候选提升为主候选或据此定 `difference_cents`——那等于用一份**后写的文档**去裁定**先发生的成交**，正是对账规范必须免疫的动机性推理。

`36,964.06` 在本例中唯一可接受的用途是**算术校验**：它等于候选 C1 的应有余额，说明 C1 的算式没写错。算术正确 ≠ 事实成立。

**（四）对账器的职责边界**

对账器的工作**到此为止**：输出观测余额、候选集合、每个候选的应有余额与差额、以及 `unreconciled` 判定。它**不**挑选候选、**不**按差额大小猜真值、**不**把「与历史文档一致」当作证据。候选集合的坍缩只能由**新证据**（回单或流水）完成，不能由推理完成。

### 9.5 终局输出（无裁定，仅报告）

```
outcome                     = unreconciled
quantity_outcome             = null
recon_status                 = unreconciled
reason_codes                 = ["NO_FILL_RECEIPT", "NO_LEDGER", "CASH_NOT_MOVED"]
observed_closing_cash_cents  = 4780006        # 观测值，未改动
opening_cash_cents           = 4780006
selected_candidate           = null           # 未裁定：无候选被选中

candidates = [
  {id:"C1", legs:["T1"],      implied_closing_cents: 3696406, delta_cents:  1083600},
  {id:"C2", legs:["T1","T2"], implied_closing_cents: 2612806, delta_cents:  2167200},
  {id:"C3", legs:[],          implied_closing_cents: 4780006, delta_cents:        0},
]

tickets_preserved = [T1, T2]   # 票据侧金额原样保留可查（§6.4 A1）
adjudicated       = false      # 候选集合只能由新证据坍缩（§9.4 四）
```

### 9.6 同一形状的第二例（2026-10-29 → 10-30）

| 项 | 值 |
|---|---|
| 10-29 `cash` | 59,063.46 ⇒ 5,906,346 分 |
| 10-30 `cash` | 47,800.06 ⇒ 4,780,006 分 |
| 观测变动 | **−1,126,340 分**（−11,263.40 元） |
| 表 B 流水 | 0 行 |
| 结论 | 恒等式 (I) 差 **−1,126,340 分** ⇒ `NO_LEDGER` + `CASH_RESIDUAL`，`recon_status=unreconciled` |

> 该例说明：即使没有票据参与，只要存在**无法归因的现金变动**，也必须报差。对账器不得只在「有票据」时才工作。

## 10. 验收映射（`LV4` 四条验收 → 本文）

| `LV4` 验收条目（计划行原意） | 本文对应章节 | 落地时如何被消费 |
|---|---|---|
| ① 无回单 ⇒ `unreconciled` 显式且非空，票据侧金额原样保留可查 | §5.2 `unreconciled` 定义 + §5.3 **P1** + §6.4 **A1/A2** + §9.5 `tickets_preserved` | 断言 `outcome=="unreconciled"` 且 `reason_codes` 含 `NO_FILL_RECEIPT`；票据行不得被丢弃或改写 |
| ② 合成回单（全额成交 / 部分成交 / 拒单）三态各一条，期末现金**逐分闭合** | §2 表 A 全字段 + §4.2 基数（1:N、N:1）+ §5.2 六值 + §6.1 (I)(II)(III) + §6.4 **A3/A4/A5** | 三组合成回单各跑一次，残差必须 `== 0` 分 |
| ③ 现金未按票据扣减（票说 −10836、账上没动）⇒ 必须**报差**而非静默通过 | §6.1 恒等式 (II)(III)（(I) 不足以发现此形）+ §6.2 逐项 `difference_cents` + §6.4 **A6** + §9.4/§9.5 | 断言 `CASH_NOT_MOVED` 出现，且候选集合逐项报出差额（C1 `+1083600`、C2 `+2167200`、C3 `0`），**不得**只报其中一个；`adjudicated == false` |
| ④ 删掉任一条闭合断言即转红 | §6.4 **A1–A7** 封闭清单 + §6.2 零容差 | 每条断言对应一个独立测试；移除即红 |

## 11. 变更控制与 SSOT 归属

### 11.1 修订流程（append-only）

1. 本文正文（§0–§11 冻结文本）**一经发布不可改写**：不修正、不「顺手改个字段名」、不删除任何段落。
2. 任何变更以文末新段落地：

```
## Revision YYYY-MM-DD

- **变更类型**：新增字段 / 收紧语义 / 放宽必需性 / 修正错误 / 废弃字段
- **动机**：实测证据或验收失败的具体形态（不得只写「优化」）
- **字段级 diff**：旧 → 新，逐字段
- **兼容性**：旧数据是否仍可解析；不可解析时给出迁移规则
- **影响章节**：§x.y 被覆盖的具体条款
- **验证**：受影响的断言编号（§6.4）与用例
```

3. 冲突时**修订段覆盖冻结文本**，但冻结文本字面保留，便于追溯「当时为什么这么定」。
4. 修订段本身同样 append-only；不允许在旧修订段上二次编辑，只能追加新修订段并将其标记为作废。

### 11.2 SSOT 归属（不得混用）

| 内容 | SSOT |
|---|---|
| 表 A / 表 B 的结构、单位、精度、时间戳语义、连接键、闭合恒等式 | **本文**（`docs/fill_reconciliation_schema.md`） |
| `LV4` 的任务状态、优先级、`[ ]`/`[x]`、验收进度、执行次序 | [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV4` 行 |

- 本文**不**维护状态；计划表**不**再复述字段定义（只链接本文）。
- 实现对账器时若发现本文有缺字段或语义冲突，**先**走 §11.1 修订，**再**改实现。**不允许**先写代码、再倒推文档（这正是 `LV4` 计划行第 1 条禁止的顺序）。

### 11.3 冻结签署

| 项 | 值 |
|---|---|
| 冻结版本 | `fill_receipt/v1` + `cash_movement/v1` |
| 冻结日期 | **2026-11-05** |
| 依据诊断 | §1（票据 `:3491-3492`、`trade_val` `:2798`、现金腿 `:2440-2448`、`nav_breakdown` `:2273-2304`、`_archived_cash_for` `:2020-2078`；全仓无回单、无流水账） |
| 下一交付物 | `LV4` 计划行第 3 条：纯函数对账器 + 新增 `pipeline/tests/test_fill_reconciliation.py`。**在本文冻结之后**方可开始 |
