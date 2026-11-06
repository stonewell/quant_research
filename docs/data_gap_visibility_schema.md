# 日线缺口可见化 Schema（冻结版 v1）

> **文档性质**：**冻结的接口契约（frozen contract）**，不是实施记录，也不是设计探讨。本文定义「日线缺口」的**三态判别谓词**、**标记词表**、**拒写条件**与**缺口报告字段/计数规则**；缺口检查器与写入侧守卫必须**消费**本文，不得反过来由实现倒推定义。
> **冻结日期**：**2026-11-05**。Schema 版本号：`data_gap/v1`（三态与标记）+ `data_gap_report/v1`（报告结构）。
> **状态**：**FROZEN**。本文是 `LV5`（日线缺口与 `ohlcv_bars.volume` `NOT NULL` 失败：`NULL` 与 `0.0` 的区分与可见化）的**第一项交付物**；在本文冻结之前**不允许写任何实现**（`LV5` 计划行第 1 条：「先冻结『缺口』的定义……三者混为一谈就等于把『没采到』当成『真零成交』」）。
> **任务状态 SSOT**：[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV5` 行是任务状态、优先级与验收的唯一权威；本文只维护**三态定义与报告契约**这一项 SSOT。本文**不维护**任务状态。
> **修订方式**：本文正文一经冻结即**不可改写**；任何变更以 **append-only** 的 `## Revision YYYY-MM-DD` 追加段落在文末落地（见 §10）。冻结文本 + 全部修订段共同构成现行契约，冲突时**修订段覆盖冻结文本**，但冻结文本的字面保留以便追溯。

## 0. 冻结范围、非目标与判断口径

### 0.1 本文冻结什么

| 项 | 冻结内容 | 章节 |
|---|---|---|
| 三态 | `MISSING_SESSION` / `NULL_VOLUME` / `ZERO_VOLUME` 的**互斥充要谓词** | §2 |
| 日历 | 「这一天是交易日」的**权威来源**，以及日历 UNKNOWN / 合成数据下的判定 | §2.2 |
| 词表 | 行级标记与覆盖聚合态的**精确字符串**，及其唯一派生方向 | §3 |
| 拒写 | `NullVolumeRefused` 的触发条件、载荷与作用域；`NOT NULL` DDL 不动的理由 | §4 |
| 报告 | 单条缺口记录的字段、覆盖维度、**计数规则**（含「一行坏」与「批次半途死」的区分） | §5 |
| 归属 | 逐标的归属与批次中断后的 `unverified` 传播 | §6 |
| 成交额 | `amount` 为 `NULL` 时**禁止**反推 | §7 |

### 0.2 非目标（本文明确**不**改变什么）

1. **不改变** `ohlcv_bars` 的 `NOT NULL` 约束。`volume DOUBLE NOT NULL`（`common/duckdb_cache.py:1200`）保持原样；理由见 §4.2。
2. **不重写历史**。不做历史回填、不修正已落库的 `amount`、不改写已闭合的归档 `data/runs/<date>/`。回填是独立决策，且会改写不可变层。
3. **不碰版本轴**。不涉及任何策略/模板的 `version` 或 `BASE_VERSION`，不触发 `docs/version.md` 变更。
4. **不改 5m 路径**。分钟级管线属 §2.20 `R3` / §2.19 `D11`，本文全部谓词与计数只面向**日线**（`interval="1d"`）。
5. **不决定实现的错误粒度**。本文只定义「必须能被看见什么」，不规定实现用逐行、逐标的还是逐批次的方式去捕获（§6.3 显式声明）。
6. **不裁定事实**。本文**不**声称 `2026-10-28` 目录缺失是 1 天还是 4 天的缺口（`LV5` 计划行明示「待确认」）。§8 的推演是**构造的示例**，其日期只为让三种状态可逐一对照，不构成对归档事实的陈述。

> 一句话：本文是把「没采到 / 没有成交 / 没看」三件事分开写清楚的那张纸，不是改存储引擎的那张纸。

### 0.3 判断口径

- 凡属**判断**而非**事实**的条目，本文同时写出**决定**与**一行理由**（`→ 理由：`）。
- 凡本文引用的 `file:line`，均为 **2026-11-05 本次会话核对过**的行号；未核对者只引用符号名（如 `store_bars`、`patch_equity_daily_bars`、`plan_history_write`），不编造行号。
- 本文**不**提出任何量化结论（不估缺失率、不估数据质量分数、不推断缺口成因）。

## 1. 冻结所依据的现状诊断

本节只陈述**可复核的事实**，作为 §2 三态定义与 §4 拒写语义的动机。

1. `ohlcv_bars` 的 `volume` 列声明为 `DOUBLE NOT NULL`（`common/duckdb_cache.py:1200`）；同表 `amount` 列为 `DOUBLE`，**可为空**。
2. `store_bars` 在 `common/duckdb_cache.py:2188` 写：
   `df_to_store["Volume"] = df["Volume"] if "Volume" in df.columns else 0.0`。
   其中的 `else 0.0` **只在列整体缺失时**触发；**列存在但某个单元格是 `NaN`/`None` 时，该值原样穿过**，不被补为 `0.0`。
3. 因此 `:2180` 的 docstring「default Volume to 0.0 if omitted」描述的是**列缺失**这一分支；把它读成「覆盖 NaN 单元格」是**误读**。该措辞在本任务语境下具误导性，构成本文要求「显式拒写 + 显式计数」的直接理由（§4.3）。
4. `:2209` 以 `df_to_store["Volume"].astype(float).values` 直接进 `records_df`，**没有** `dropna`、没有 `fillna`、没有 `clip`，没有任何校验。
5. 于是 `volume` 为 `NULL` 的行会在 `INSERT OR REPLACE` 处触发 `Constraint Error: NOT NULL constraint failed: ohlcv_bars.volume`；`store_bars` 回滚其事务（事务始于 `:2233`）。
6. 该异常向上穿透至 `scripts/check_live_portfolio_health.py:928`，被打印为 `[STAGE 1 DATA REFRESH WARN] 个股日线补充拉取异常 (容错忽略)`，并在 `summary["steps"]["patch_equity_daily_bars"]` 记一条 `"warn: …"`。**没有符号名、没有日期、没有被拒行数**。
7. `patch_equity_daily_bars`（`scripts/sync_china_5m_and_indices.py`）的**拉取**环节确实做了逐标的隔离（每个 `fetch_*` 各自 try，失败记录后继续），但 `cache.store_bars(...)` 的**写入**调用点不在任何逐标的 `try` 之内。因此一次写入失败会**直接跳出整段循环**，该符号之后的标的**一次都不会被尝试**；到 `:928` 只剩一行笼统 WARN。
8. **缺失交易日**（某日期整行不存在）是**另一种**失败形态：它不触发任何异常、不落任何日志、不改变任何计数——**当前完全没有检测**。

> **由此得到的唯一结论**：这三种情形（行缺失 / 行在但 `volume` 为 `NULL` / 行在且 `volume == 0.0`）在今天**共用同一套"看不见"的出口**：前两种被吞成一行 WARN 或彻底静默，第三种与它们在下游**不可区分**。本文的第一性要求是：**三态各有自己的名字，且任一处混淆都必须可被断言捕获**。

## 2. 三态定义（互斥、可判定）

### 2.1 三态表

三条谓词作用于同一个二元组 `(symbol, session_date)`，其中 `session_date` 取自 §2.2 的权威日历。**先判行是否存在，再判单元格取值**；因此三态天然互斥（§2.3）。

| 状态 | 精确谓词 | 判据来源 | 与另两态混淆的后果（为什么必须分开） |
|---|---|---|---|
| `MISSING_SESSION` | `session_date ∈ Sessions(window)` **且** `∄` 任何行满足 `(provider, symbol, date) == (·, symbol, session_date)` | 日历判定该日为交易日（§2.2）+ 表内 `SELECT 1 … WHERE date=?` 零命中 | 与 `ZERO_VOLUME` 混淆 ⇒ 把「没采到」记成「当天真的没成交」，缺口被永久洗白；与 `NULL_VOLUME` 混淆 ⇒ 丢失「采集端返回了一行坏数据」这一证据，无从定位坏源 |
| `NULL_VOLUME` | 存在行 `(provider, symbol, session_date)` **且** `volume IS NULL`（帧层面 `pd.isna(volume)` 为真） | 行存在性 + 单元格取值 | 与 `ZERO_VOLUME` 混淆 ⇒ 直接制造本文要防的第一个实质错误：**把拒写/未采集当成真零成交**，进入成交额与广度计算的输入；与 `MISSING_SESSION` 混淆 ⇒ 让人以为「源里没有这一天」，而实际情况是源给了数据、我们在写入口把它丢了 |
| `ZERO_VOLUME` | 存在行 `(provider, symbol, session_date)` **且** `volume == 0.0`（精确等于零，非 `NaN`、非 `None`） | 行存在性 + 单元格取值 | 与 `NULL_VOLUME` 混淆 ⇒ 把合法的停牌/无成交日当成数据缺陷去「修」，或反过来把缺陷当真零放行；与 `MISSING_SESSION` 混淆 ⇒ 一个**已被采集且确认为零**的交易日被当成缺口反复重拉 |

三态的互斥性由谓词结构保证，不依赖实现的判定顺序：

```
MISSING_SESSION  ⇔  ¬∃row(symbol, date)
NULL_VOLUME      ⇔   ∃row(symbol, date) ∧ isna(volume)
ZERO_VOLUME      ⇔   ∃row(symbol, date) ∧ ¬isna(volume) ∧ volume == 0.0
```

`¬∃row` 与 `∃row` 互补，`isna` 与 `¬isna` 互补，故三者两两互斥。**每一对都穷尽不了全集**：`volume > 0` 的正常行不属于任何一态，这正是它不该出现在缺口清单里的原因。

### 2.2 「这一天是交易日」的权威来源

`MISSING_SESSION` 断言的是**一个缺席**，因此它需要一个能对「这一天本来应该有一行」负责的日历。若日历本身不可靠，该断言就退化为猜测。

**决定**：权威来源是 `common/market_clock.py::MarketClock.is_trading_day(date)`，其节假日表来自 `common/market_holidays.py` 的静态表。

→ 理由：它是**离线**的、**三态**的（未覆盖年份抛 `HolidayCalendarCoverageError`，绝不把「不知道」答成「不开市」），且把「已知无假期」`frozenset()` 与「未知」`None` 明确分开；一个需要断言缺席的检查器只能建立在这种日历上。

**明确排除**：`common/financial_api.py::get_trading_days_calendar()` **不**作为本任务的权威来源。

→ 理由：它在缓存未命中时依赖网络（`fuyao_client.calendar_trading_days()`），且在取不到时返回**空 `set()`**——「没有假期」与「拉取失败」得到同一个值。用空集当权威，等价于在日历挂掉时把每一天都判成非交易日，缺口清单会因网络故障而**归零**，这是比不检查更坏的失败形态。

| 场景 | 会话日历取值 | 报告中的 `calendar.status` |
|---|---|---|
| 实盘/真实数据，年份被静态表覆盖（2020–2027） | `MarketClock.is_trading_day` | `covered` |
| 同上，年份为 `PROVISIONAL`（2026 / 2027） | 同上，结论照常采用，但状态随记录带出 | `provisional` |
| 同上，年份未被覆盖 ⇒ `HolidayCalendarCoverageError` | 不判定 | `uncovered` ⇒ 覆盖态 `unverified`（§3.2） |
| 调用方显式传入 `holidays=frozenset()` 等显式集合 | 该显式集合（`MarketClock(..., holidays=...)` 的 `calendar_status()` 为 `"explicit"`） | `explicit` |
| 合成数据 / 离线测试（`SyntheticDataProvider`，`pd.bdate_range` 只跳周末） | **显式传入的日历**；缺省时取该 provider 自身产出的日期索引 | `explicit` |

三条判断，均给出理由：

1. **`provisional` 不降级为 `unverified`**。→ 理由：`PROVISIONAL` 影响的是「该表能否用于下单授权门禁」，而本文只需回答「这一天是否开市」；若把 2026–2027 的每个会话都降级，覆盖报告在**实际在跑的这两年**将完全不可用。
2. **合成数据不得用真实节假日表判缺口**。→ 理由：`SyntheticDataProvider` 的日线日期由 `pd.bdate_range` 生成，只跳周末，**真实节假日会被它当成交易日造出数据**；反过来若拿真实节假日表去检查合成数据，每一个法定假日都会变成假的 `MISSING_SESSION`。合成/离线路径必须显式传日历，或以其自身日期索引为准。
3. **日历 UNKNOWN ⇒ 该标的的覆盖态为 `unverified`，不是 `complete`，也不是 `gapped`**。→ 理由：`gapped` 断言「某会话存在而该行缺席」，日历未知时无从断言缺席；`complete` 断言「都齐了」，更无从断言。

### 2.3 判别顺序（先存在性，后取值）

```
for session_date in Sessions(window ∩ listed_interval(symbol)):
    if no row (provider, symbol, session_date):        -> MISSING_SESSION
    elif isna(volume):                                  -> NULL_VOLUME
    elif volume == 0.0:                                 -> ZERO_VOLUME
    else:                                               -> 正常行（不入缺口清单）
```

顺序是**规范**的一部分而非实现细节：它把「行缺失」与「行内缺陷」在判定树上强制分开，任何把 `isna` 判断前置的实现都会在**行缺席**时先拿到一个 `NaN`（因为读出来就是空），从而把 `MISSING_SESSION` 静默成 `NULL_VOLUME`。这正是 `AGENTS.md` 里 cell-level `NaN` 与 `0.0` 的同类陷阱在数据层的复现。

### 2.4 核心规则（不可协商）

**缺数据 ≠ 零成交量。**

把 `MISSING_SESSION` 或 `NULL_VOLUME` 写成 `ZERO_VOLUME`——无论通过 `fillna(0.0)`、`else 0.0`、`coalesce(volume, 0)`、还是「反正下游要的是数字」——都是本条任务存在的**唯一理由**。其后果不是格式问题，而是把「未观测」变成「已观测的零」，该值随后会作为事实进入成交额、换手、广度、流动性过滤与停牌判别，且**在下游不可再复原**：`0.0` 与「真的 0.0」在库里字节相同。

### 2.5 三态之外（显式出界）

`volume < 0`、`volume` 为 `inf`、以及 `amount`、`open/high/low/close` 的缺失，**不属于**本节三态，也不得被归类进三态中的任何一态。→ 理由：三态是为「缺口可见化」而冻结的封闭词表，把负成交量塞进 `ZERO_VOLUME` 会把一个数据错误伪装成一个合法状态；这类值应走既有的数据校验/拒写路径抛出，若要为它新增标记，须走 §10 修订。

## 3. 标记词表（marker vocabulary）

### 3.1 行级标记（封闭集合，逐 `(symbol, session_date)` 一个）

| 标记（精确字符串） | 语义 | 是否进入缺口清单 |
|---|---|---|
| `missing_session` | 会话存在，行完全缺席 | **是** |
| `null_volume` | 行存在，`volume` 为 `NULL`/`NaN` | **是** |
| `zero_volume` | 行存在，`volume == 0.0`（合法无成交） | **否**，但必须在「已核实为零」清单中出现 |

### 3.2 覆盖聚合态（封闭集合，逐 `(symbol, window)` 一个）

| 标记（精确字符串） | 充要条件 |
|---|---|
| `complete` | 该标的窗口内**每一个**会话都被检查过，且 `missing_session == 0` 且 `null_volume == 0`（`zero_volume` 不影响 `complete`：真零是合法数据） |
| `gapped` | 该标的窗口内所有会话都被检查过，且 `missing_session + null_volume >= 1` |
| `unverified` | 该标的的窗口**未被完整检查**：批次在它之前中断、日历 UNKNOWN、或窗口超出该标的已知可得区间（§5.4）。此时**不得**给出 `complete` / `gapped` 任一结论 |

三条判断：

1. **`zero_volume` 计入 `complete`。**→ 理由：它是一条**已采集且已确认为零**的事实；把它算作缺口会让每一次停牌都触发重拉，并让真实的采集缺陷淹没在合法零里。
2. **`unverified` 与 `gapped` 是两件事。**→ 理由：`gapped` 断言「我看过，确实缺」；`unverified` 断言「我没看完」。把后者写成前者会把检查器的中断伪装成数据缺陷，把前者写成后者则会让真实缺口永远免于被追问。
3. **聚合态只由行级计数导出，不独立判定。**→ 理由：若允许另写一套聚合逻辑，聚合态与行级记录迟早互相矛盾，而读报告的人只会看聚合列。

### 3.3 稳定性与派生方向

- 上述**六个字符串是稳定标识符**，不是展示文本：ASCII、小写、`snake_case`、前后无空白、**永不本地化**、**永不翻译**、不得承载任何数字或标的代码。
- **派生方向是单向的**：中文/本地化文案（如「交易日缺失」「成交量未采集」「真零成交」）**由标记派生**；任何以展示文本反推标记、或让标记随语言环境变化的设计，均违反本节。→ 理由：标记要进 JSON、进断言、进 grep、进归档比对，一旦可翻译就不再可比；文案可以改，标识符不能。
- 若某状态无对应标记，实现**不得**自造近义字符串（如 `"MISSING"`、`"null"`、`"nan"`、`"zero"`、`"volume_null"`）；须走 §10 修订新增。

## 4. 拒写语义（不改 schema）

### 4.1 `NullVolumeRefused`（条件名与载荷）

`LV5` 验收第 ① 条给出两条互斥分支：**落 `NULL`** 或 **显式拒写并计数**。本文**择定后者**（见 §9.1），条件名为：

```
NullVolumeRefused
```

冻结载荷（字段缺一不可）：

| 载荷字段 | 类型 | 语义 |
|---|---|---|
| `symbol` | `str` | 归一化标的代码（含交易所后缀） |
| `provider` | `str` | 写入命名空间（如 `FuyaoDataProvider`），`NULL_VOLUME` 的归属必须能定位到来源 |
| `interval` | `str` | 本契约固定为 `"1d"` |
| `dates` | `list[date]` | **全部**被拒日期的升序去重列表；只报第一天即违反本节 |
| `n_rows` | `int` | 被拒行数，必须 `== len(dates)`（同一 `(provider, symbol, date)` 主键下不可能有两行，故二者恒等是可断言的） |
| `first_date` / `last_date` | `date` | `dates` 的最小 / 最大值，便于单行日志与归档摘要 |
| `value_repr` | `str` | 违约值的字面表示：`"null"` 或 `"nan"`（**不得**写成 `"0.0"`、`"None"`、`"<NA>"` 之外的其它变体） |

三条硬性要求：

1. **不得静默强制为 `0.0`。**→ 理由：`0.0` 是对「当天无成交」的事实断言，而数据未提供该断言；写进去以后无法与真零区分（§2.4）。
2. **不得以裸 `Constraint Error` 作为出口。**→ 理由：SQL 层错误**不带符号名、不带日期、不带行数**，且发生在事务开始之后，调用方唯一能做的只剩打印字符串——今天 `:928` 的 `[STAGE 1 DATA REFRESH WARN]` 正是这个形态，它把「哪个标的、哪一天、几行」全部丢掉了。
3. **守卫必须发生在 `frame_min_date` / `frame_max_date` 与 `records_df` 计算之前。**→ 理由：否则 `asset_sync_metadata` 会把被拒的那一天计入「已覆盖区间」的上/下界，报告说该日缺失、元数据说该日已覆盖，两者互相矛盾且都无法自证。

### 4.2 为什么 `NOT NULL` 不动

**决定**：`ohlcv_bars.volume DOUBLE NOT NULL`（`:1200`）保持原样，一字符不改。

→ 理由：`NOT NULL` 是**不可变层唯一的自证**——它保证「凡落库的行，成交量必有一个已观测的值」。若把列放宽为可空，`NULL` 就可以合法落库，「未采集」与「合法缺失」在库里从此不可区分，本任务要消灭的歧义反而被**制度化**成 schema 的一部分，且此后每个读方都必须自己重新判一次。

由此产生一条推论：**拒写是 `NOT NULL` 的正确使用方式**。约束不是障碍，它是把缺陷推到写入边界、让缺陷无法穿过边界的那个装置；今天的错误只是它被当成异常吞掉了（`:928`），而不是它不该存在。

### 4.3 拒写的**作用域**：逐行拒、不逐批死

**决定**：守卫在构建 `records_df` 之前逐行判定，把 `NULL_VOLUME` 行**移除**并各自形成一条 `NullVolumeRefused` 记录；同一批次的其余合法行照常写入；若移除后批次为空，则不执行 `INSERT`（不得插入空帧）。

→ 理由：一次 `store_bars` 调用是一个事务（`:2233` 起），若整批拒绝，则「一行坏」与「整段坏」在调用方看来完全一样，正是 §5.3 要求区分而当前无法区分的那一对。

**同时冻结**：该条件**不得越过标的边界**。它的归属载体是既有惯例中的「逐标的累积 + 显式 `raise_if_any`」模式（`scripts/sync_china_5m_and_indices.py` 的 `RefusalCollector` 对 `QfqWriteRefused` 已确立该形状）：

1. 逐标的逐行拒写 ⇒ 生成记录，写入运行报告（§5）；
2. 记录累积到调用方（如 `patch_equity_daily_bars` 的返回值 / 报告对象）；
3. **不**在该处直接 `raise` 到 `:928` 的批次级 `except`；
4. 需要硬失败的调用方显式调用 `raise_if_any()`，由它聚合抛出（报**全部**被拒标的与日期，而非第一个）。

→ 理由：批次级 `except` 一旦接住，剩余标的**一次都不会被尝试**（§1 第 7 条），缺口清单随之系统性短缺，而报告会看起来「很干净」。

## 5. 缺口报告契约（`data_gap_report/v1`）

### 5.1 单条缺口记录（gap record）字段

| 字段 | 类型 | 必需性 | 语义 |
|---|---|---|---|
| `symbol` | `str` | **必需** | 归一化标的代码 |
| `date` | `date`（`YYYY-MM-DD`） | **必需** | 该会话日；**交易所本地日**，不做时区换算 |
| `state` | `enum` | **必需** | §3.1 三值之一；缺口清单里只允许出现 `missing_session` / `null_volume` |
| `evidence` | `object` | **必需** | 支撑该判定的最小事实集，见 §5.2 |
| `detected_at` | `str` | **必需** | ISO-8601 **带数字时区偏移**（如 `2026-11-05T18:04:12.480+08:00`）；裸时间戳视为格式错误 |
| `source_step` | `str` | **必需** | 产生该记录的处理步骤标识（如 `stage1.patch_equity_daily_bars`、`stage1.coverage_check`）；它命名**步骤**，不是状态，故不属 §3 封闭词表 |

### 5.2 `evidence` 的形状（按状态冻结）

| `state` | `evidence` 必需键 |
|---|---|
| `missing_session` | `row_present=false`、`calendar_status`（§2.2 五值）、`calendar_authority`（如 `market_clock` / `explicit`） |
| `null_volume` | `row_present=true`、`volume_is_null=true`、`amount_is_null`（`true`/`false`/`null`）、`refusal`（固定 `"NullVolumeRefused"`） |
| `zero_volume` | `row_present=true`、`volume=0.0`（JSON 数字） |

`evidence` **只放事实，不放推断**：不得写入「疑似停牌」「疑似源故障」等因果判断。→ 理由：成因推断会随时间被证伪，而记录是不可变归档的一部分；一旦混入，事后无法把观测与猜测分开。

### 5.3 计数规则（含「一行坏」与「批次半途死」的区分）

报告的顶层 `totals` 与每个标的的 `counts` 都必须给出下列计数；**未检查的维度一律 `null`，不得写 `0`**。

| 计数 | 定义 | 未检查时 |
|---|---|---|
| `symbols_requested` | 本次刷新被要求处理的标的数（分母） | 恒有值 |
| `symbols_attempted` | 至少进入过一次读/写判定的标的数 | 恒有值 |
| `symbols_completed` | 窗口被完整检查的标的数（其覆盖态为 `complete` 或 `gapped`） | 恒有值 |
| `symbols_unverified` | 覆盖态为 `unverified` 的标的数 | 恒有值 |
| `sessions_in_window` | 该标的窗口内的会话数（按 §2.2 日历） | 日历 UNKNOWN 时 `null` |
| `sessions_checked` | 实际被判定过的会话数 | `null` |
| `bars_present` | 实际存在的行数 | `null` |
| `bars_refused` | **写入侧**被 `NullVolumeRefused` 拒写的行数 | `null` |
| `missing_sessions` | **读取侧**判定为 `missing_session` 的会话数 | `null` |
| `null_volume_bars` | 判定为 `null_volume` 的会话数 | `null` |
| `zero_volume_bars` | 判定为 `zero_volume` 的会话数（合法） | `null` |
| `missing_sessions_lower_bound` | `bool`：`missing_sessions` 是否为下界 | 有值为 `true` 时必给 |

四条计数规则（C1–C4）是本节的实质内容：

- **C1（`null` 与 `0` 不同）**：未检查 ⇒ `null`；检查过且为零 ⇒ `0`。→ 理由：这与 §2.4 是**同一条原则在计数层**的复现——未采到 ≠ 零，未检查 ≠ 无缺口。把未检查写成 `0`，等于用「我没有看」冒充「我看了，没有」。
- **C2（下界标注）**：仅当 `sessions_checked == sessions_in_window` 时 `missing_sessions` 才是精确值；否则必须把 `missing_sessions_lower_bound` 置 `true`。→ 理由：批次中断后，剩余会话既不是「缺」也不是「齐」，把下界当精确值会让缺口被系统性低估。
- **C3（两计数永不合并）**：`bars_refused`（写入口计数）与 `missing_sessions`（读侧计数）分列，**禁止**相加成一个 `gaps` 总数。→ 理由：二者回答不同的问题——「我们丢了几行」与「库里少了几行」；合并后无法判断某一天的缺失是采集端没给，还是写入端拒掉。
- **C4（闭合恒等式，可断言）**：对任一 `sessions_checked == sessions_in_window` 的标的：

  ```
  complete_sessions + missing_sessions + null_volume_bars == sessions_in_window
  zero_volume_bars ⊆ complete_sessions
  ```

  其中 `complete_sessions = sessions_in_window − missing_sessions − null_volume_bars`。`zero_volume` 的会话计入 `complete_sessions`，故第二个式子必须成立；它同时是「真零没有被算成缺口」的机器可检证明。

**「一行坏」与「批次半途死」的判别（冻结）**：

| 形态 | 充要判据 | 报告长相 |
|---|---|---|
| **一行坏**（blast radius = 1 行） | `abort.aborted == false` **且** `symbols_completed == symbols_requested` **且** `bars_refused >= 1` | 每个标的覆盖态 ∈ {`complete`,`gapped`}；恰好 1 条 `null_volume` 记录；其余标的计数完整 |
| **批次半途死**（blast radius = 未处理尾部） | `abort.aborted == true` 或 `symbols_attempted < symbols_requested` | `abort.at_symbol` 非空；`abort.at_symbol` 及其后**所有**标的覆盖态 = `unverified`、`records = []`、`missing_sessions = null`；`symbols_unverified >= 1` |

→ 理由：单看「记录条数」永远分不清这两种形态——一行坏产生 1 条记录，半途死在中断点之前也可能只产生 1 条记录。区分它们**只能靠分母 + 中断点**：`symbols_requested` 给出应有总量，`abort.at_symbol` 给出从谁开始没被看过。这正是 §1 第 7 条那个真实形状：写入失败跳出循环后，报告若只数记录，会得出「今天只有 1 个问题」的结论。

**报告顶层结构（冻结）**：

```
{
  "schema": "data_gap_report/v1",
  "window":   {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "calendar": {"authority": "market_clock|explicit", "market": "china|us|hk",
               "status": "covered|provisional|uncovered|explicit"},
  "abort":    {"aborted": bool, "at_symbol": str|null, "reason": str|null},
  "totals":   { …§5.3 全部计数… },
  "symbols":  [ {"symbol": str, "coverage": "complete|gapped|unverified",
                 "counts": { …同名字段… },
                 "records": [ …§5.1… ],
                 "verified_zero_dates": ["YYYY-MM-DD", …]} ]
}
```

`verified_zero_dates` 是**必需**字段，不是可选装饰：没有它，「缺口清单为空」就同时兼容「检查过且无缺口」与「什么都没检查」两种情形。→ 理由：一个只能表达「有缺口」的报告，无法证明自己**看见过**真零；这正是 `LV5` 交付第 ③ 条「缺口清单」与第 ④ 条「缺数据与真零分开呈现」之间的连接点。

### 5.4 覆盖维度与窗口

- 覆盖维度是 **`symbol` × `session` × `window`**：逐标的、逐会话判定，聚合到窗口。
- 窗口由调用方给出 `(start, end)`；`end` 之后（含当日未收盘）的会话**不进入**窗口，不得因此报 `missing_session`。
- 窗口须与标的自身可得区间求交：`asset_sync_metadata.earliest_available_date` 之前的会话，若 `is_earliest_known == false`，一律记**未检查**（计入 `unverified`），**不得**记 `missing_session`。→ 理由：上市前/未回溯到的日期从来不是「被跳过的会话」；把它报成缺口就是同一个缺陷换了个方向——用「我没去那儿」冒充「那儿没有东西」。这与 `LV5` 证据中「补数起点对不齐」互为旁证，但本文**不**据此断言任何具体缺口的成因或天数（§0.2 第 6 条）。

## 6. 与多标的刷新的交互

### 6.1 逐标的归属

**要求**：任一条 `NullVolumeRefused` 与其派生的 `null_volume` 记录，**必须**携带 `symbol`（及 `provider`）。

→ 理由：`store_bars` 的错误文本里没有符号名（§1 第 6 条），若拒写条件不在载荷里带上它，调用方即使捕获也无法把缺陷归到具体标的；**无法归属的告警等于没有告警**——它会退化回今天那行 WARN。

### 6.2 批次中断 ⇒ 未处理标的一律 `unverified`

**要求**：若处理在标的 `S` 处中断（无论中断由哪一类异常引起），则

1. `S` 本身：若其窗口未被完整检查 ⇒ `unverified`；若已完整检查（例如拒写发生在写完之后的收尾步骤）⇒ 按 §3.2 正常判；
2. `S` 之后**尚未被尝试**的每一个标的 ⇒ `unverified`，`records = []`，`missing_sessions = null`（不是 `0`，不是窗口全会话数）；
3. 全局 `abort = {"aborted": true, "at_symbol": S, "reason": …}`；
4. **禁止**把未处理标的写成 `complete`。

→ 理由：这是一种**真空成立**的陷阱——未处理的标的没有任何记录，把「没有记录」读成「没有缺口」，报告就会在失败最严重的一天显示得最干净。`LV5` 计划行的第 2 条已明确把该形态定性为「失败被吞掉」，本节只是要求它在报告里留下可数的痕迹。

### 6.3 本文**不**决定实现的错误粒度

本文只要求「blast radius 必须可见」，不规定实现用逐行 try、逐标的 try、还是逐批次 try 去捕获。→ 理由：粒度是实现选择（受事务边界、性能、既有 `RefusalCollector` 惯例约束），而**可观测的结果**是契约；把粒度也冻结会让本文在实现时的合理调整都要走修订，反而侵蚀冻结的严肃性。唯二不可协商的点是 §4.3 的「逐行拒、不逐批死」与 §6.2 的「未处理 ⇒ `unverified`」。

## 7. 成交额守卫规则

**决定**：`volume > 0` 且 `amount` 为 `NULL` 时，`amount` **保持 `NULL`**；**严禁**以 `volume × close`（或 `volume × vwap`、`volume × any_price`）反推填充。

→ 理由：`amount` 是**观测值**，反推值是**推断值**；把推断写进观测列后，二者在库里字节相同、在下游不可分离，任何以成交额为输入的过滤（dollar-volume 阈值、流动性/换手筛选、广度与冲击成本估计）都会在不知不觉中消费一个从未被观测过的数。

补充冻结：

1. `amount == 0.0` 与 `amount IS NULL` 同样**必须分开**：前者是「真零成交额」，后者是「未采集」。这与 `docs/version.md` 对 `amount` 的既有禁令一致，本文只是把它接到三态同一套原则上。
2. 反推**在任何调用路径上都不允许**，包括「只是一次性的临时分析脚本」。→ 理由：临时脚本会变成下游依赖，而它写出的列名与真观测完全相同。
3. 该禁令必须由**可执行断言**守护（§9.2 **G7**），而不只由注释声明。→ 理由：`LV5` 验收第 ③ 条要求「若代码路径出现该反推，测试转红」，注释无法转红。

## 8. 逐例推演（构造示例）

> 本节**是构造的**：标的、窗口与三处异常均为人工设定，只为让三种状态逐一对照。日历按「该 5 个工作日均为交易日」的假设给出，**不**引用真实节假日表，也**不**对归档中的 `2026-10-28` 作任何事实陈述（§0.2 第 6 条）。

### 8.1 输入

| 项 | 值 |
|---|---|
| `symbol` / `provider` / `interval` | `600000.SH` / `FuyaoDataProvider` / `1d` |
| `window` | `2026-10-26`（一）→ `2026-10-30`（五） |
| `calendar` | `{"authority": "market_clock", "market": "china", "status": "covered"}` |
| 窗口内会话 | 5 个：`10-26`、`10-27`、`10-28`、`10-29`、`10-30` |
| 库内实际行 | 4 行：`10-26`、`10-27`、`10-29`、`10-30` |

三处异常（各一条，互不重叠）：

| 日期 | 库内形状 | `volume` | 判定 |
|---|---|---|---|
| `2026-10-28` | **无行** | —（不存在） | `MISSING_SESSION` |
| `2026-10-29` | 有行 | `NULL` | `NULL_VOLUME` |
| `2026-10-30` | 有行 | `0.0` | `ZERO_VOLUME` |

### 8.2 判定与拒写（逐步）

| 步 | 动作 | 结果 |
|---|---|---|
| S1 | `10-26` 属会话、行存在、`volume = 1.24e6` | 正常行，不入缺口清单 |
| S2 | `10-27` 属会话、行存在、`volume = 9.87e5` | 正常行，不入缺口清单 |
| S3 | `10-28` 属会话、`SELECT 1 … WHERE date='2026-10-28'` 零命中 | `missing_session`，记录 **R1** |
| S4 | `10-29` 行存在、`pd.isna(volume)` 为真 | `null_volume`，记录 **R2**；写入侧生成 `NullVolumeRefused(symbol="600000.SH", provider="FuyaoDataProvider", interval="1d", dates=["2026-10-29"], n_rows=1, first_date="2026-10-29", last_date="2026-10-29", value_repr="null")`，该行**不落库** |
| S5 | `10-30` 行存在、`volume == 0.0` | `zero_volume`，**不**入缺口清单；写入侧**正常放行**（合法数据） |

### 8.3 当前代码对这三种情形各做什么

| 日期 | 当前行为 | 形态定位 |
|---|---|---|
| `2026-10-28` | **完全静默**：不抛异常、不打日志、不计入任何计数。既有的 `check_missing_trading_days(latest, target)` 只检查 `(最新已取日期, 目标日]` 这个**尾部开区间**，对窗口**内部**的洞不作任何断言，故该日不可见 | 「缺数据」被读成「无事发生」 |
| `2026-10-29` | `:2188` 的 `else 0.0` **不**触发（列存在）⇒ `:2209` 把 `NULL` 原样送入 ⇒ `INSERT OR REPLACE` 抛 `Constraint Error: NOT NULL constraint failed: ohlcv_bars.volume` ⇒ 事务回滚（`:2233` 起）⇒ 异常穿到 `:928`，打印**一行** `[STAGE 1 DATA REFRESH WARN] 个股日线补充拉取异常 (容错忽略)` 并记 `"warn: …"`；**该标的后面的标的未被尝试** | 「拒写」退化为「吞掉」，且 blast radius 不可见 |
| `2026-10-30` | **成功写入**（结果上正确）。但报告里它与「被 `fillna(0.0)` 伪造出来的零」**不可区分**，故其正确性无法被证明，只能被相信 | 「真零」与「伪零」同形 |

### 8.4 报告输出（逐字段）

```json
{
  "schema": "data_gap_report/v1",
  "window":   {"start": "2026-10-26", "end": "2026-10-30"},
  "calendar": {"authority": "market_clock", "market": "china", "status": "covered"},
  "abort":    {"aborted": false, "at_symbol": null, "reason": null},
  "totals": {
    "symbols_requested": 1,
    "symbols_attempted": 1,
    "symbols_completed": 1,
    "symbols_unverified": 0,
    "sessions_in_window": 5,
    "sessions_checked":   5,
    "bars_present":       4,
    "bars_refused":       1,
    "missing_sessions":   1,
    "null_volume_bars":   1,
    "zero_volume_bars":   1,
    "missing_sessions_lower_bound": false
  },
  "symbols": [
    {
      "symbol": "600000.SH",
      "coverage": "gapped",
      "counts": {
        "sessions_in_window": 5, "sessions_checked": 5, "bars_present": 4,
        "bars_refused": 1, "missing_sessions": 1, "null_volume_bars": 1,
        "zero_volume_bars": 1, "missing_sessions_lower_bound": false
      },
      "records": [
        {
          "symbol": "600000.SH",
          "date": "2026-10-28",
          "state": "missing_session",
          "evidence": {"row_present": false, "calendar_status": "covered",
                       "calendar_authority": "market_clock"},
          "detected_at": "2026-11-05T18:04:12.480+08:00",
          "source_step": "stage1.coverage_check"
        },
        {
          "symbol": "600000.SH",
          "date": "2026-10-29",
          "state": "null_volume",
          "evidence": {"row_present": true, "volume_is_null": true,
                       "amount_is_null": true, "refusal": "NullVolumeRefused"},
          "detected_at": "2026-11-05T18:04:12.480+08:00",
          "source_step": "stage1.patch_equity_daily_bars"
        }
      ],
      "verified_zero_dates": ["2026-10-30"]
    }
  ]
}
```

算术校验（§5.3 C4）：`complete_sessions = 5 − 1 − 1 = 3`（`10-26`、`10-27`、`10-30`）；`3 + 1 + 1 = 5 == sessions_in_window` ✓；`zero_volume_bars = 1 ⊆ complete_sessions = 3`，且其在 `verified_zero_dates` 中逐字出现 ✓。

同一份输入必须得到**逐字段相同**的报告（除 `detected_at`）。→ 理由：缺口清单要能与历史归档 diff，可复现是它能被比对的前提。

## 9. 验收映射与不可删除的断言

### 9.1 `LV5` 四条验收 → 本文

| `LV5` 验收条目（计划行原意） | 本文对应章节 | 落地时如何被消费 |
|---|---|---|
| ① 注入 `volume = NULL` ⇒ 落 `NULL`（不是 `0.0`）**或**显式拒写计数，**二者择一并写明** | **本文择定「显式拒写计数」**：§4.1 `NullVolumeRefused` 载荷 + §4.3 逐行拒写 + §5.3 `bars_refused` + §2.1 `NULL_VOLUME` 谓词；**不取**「落 `NULL`」分支（§4.2 说明理由：放宽 `NOT NULL` 会让歧义制度化） | 断言 `bars_refused == 1` 且该日不出现在 `verified_zero_dates`，且库内该 `(provider, symbol, date)` **无行** |
| ② 注入缺口交易日 ⇒ 缺口清单精确命中且**不**被静默裁掉 | §2.1 `MISSING_SESSION` 谓词 + §2.2 日历权威 + §2.3 先存在性后取值 + §5.1 记录字段 + §5.3 C1/C2 + §8 逐例（R1） | 断言 `records` 中 `(date, state) == ("…", "missing_session")` 精确命中；并断言**删除该检测**后测试转红 |
| ③ **禁止**以 `volume × close` 反推 `amount` | §7 全节（含 `amount IS NULL` 与 `amount == 0.0` 的分列）+ §9.2 **G7** | 断言注入路径上 `amount` 仍为 `NULL`；出现反推即红 |
| ④ 删掉三态判别即转红 | §2.1 谓词的互斥结构 + §3 封闭词表 + §9.2 **G1–G8** | 每条断言独占一个用例；移除任一三态分支即红 |

### 9.2 不可删除的断言清单（对应验收 ④）

| # | 断言 |
|---|---|
| **G1** | 行存在且 `volume` 为 `NULL`/`NaN` ⇒ 产生 **1 条 `null_volume`** 记录 + `bars_refused` 加 1，且该行**不落库**；若实现把该行写成 `0.0` 或写成 `NULL` 落库，G1 转红 |
| **G2** | 会话在日历内、行完全缺失 ⇒ 产生 **`missing_session`**；`zero_volume` **不得**出现在 `records` 中 |
| **G3** | 行存在且 `volume == 0.0` ⇒ `coverage` 计为 `complete`，该日进入 `verified_zero_dates`，`records` 为空 |
| **G4** | 同一输入重跑，`state` 标记逐字相同；`missing_session` 与 `null_volume` **永不**互换（对调即红） |
| **G5** | 日历 UNKNOWN（未覆盖年份 / 未传显式日历的合成路径）⇒ `coverage == "unverified"`，`records == []`，`missing_sessions is null` |
| **G6** | 批次在标的 `S` 处中断 ⇒ `abort.at_symbol == S`，`S` 之后每个标的 `coverage == "unverified"` 且 `missing_sessions is null`；任何一个被写成 `complete` 即红 |
| **G7** | `volume > 0` 且 `amount` 为 `NULL` ⇒ `amount` 仍为 `NULL`；任何 `volume × close` 之类的反推出现在该路径上即红 |
| **G8** | 对 `sessions_checked == sessions_in_window` 的标的，恒等式 `complete + missing + null_volume == sessions_in_window` 与 `zero_volume ⊆ complete` 必须成立 |

→ 每条断言的理由都指向同一个缺陷类：**「未观测」被写成了「已观测的某个值」**。G1/G2 防「缺 → 零」，G5/G6 防「未检查 → 无缺口」，G7 防「未采集 → 反推值」，G3/G8 则反向防「真零 → 缺口」，即两侧都要有守卫，否则修正会滑向另一个方向的错误。

## 10. 变更控制与 SSOT 归属

### 10.1 修订流程（append-only）

1. 本文正文（§0–§10 冻结文本）**一经发布不可改写**：不修正字段名、不「顺手补一句」、不删除任何段落。
2. 任何变更以文末新段落地：

```
## Revision YYYY-MM-DD

- **变更类型**：新增标记 / 收紧谓词 / 放宽必需性 / 修正错误 / 废弃标记
- **动机**：实测证据或验收失败的具体形态（不得只写「优化」）
- **词表 diff**：旧 → 新，逐标记；若为谓词变更，给出旧/新谓词
- **兼容性**：旧报告是否仍可解析；不可解析时给出迁移规则
- **影响章节**：§x.y 被覆盖的具体条款
- **验证**：受影响的断言编号（§9.2）与用例
```

3. 冲突时**修订段覆盖冻结文本**，冻结文本字面保留，便于追溯「当时为什么这么定」。
4. 修订段本身同样 append-only：不允许二次编辑旧修订段，只能追加新段并显式标记前者作废。

### 10.2 SSOT 归属（不得混用）

| 内容 | SSOT |
|---|---|
| 三态谓词、日历权威、标记词表、`NullVolumeRefused` 载荷、缺口报告字段与计数规则 | **本文**（`docs/data_gap_visibility_schema.md`） |
| `LV5` 的任务状态、`[ ]`/`[x]`、优先级、验收进度、执行次序 | [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV5` 行 |

- 本文**不**维护状态；计划表**不**再复述三态定义（只链接本文）。
- 实现缺口检查器/写入守卫时若发现本文缺字段或语义冲突，**先**走 §10.1 修订，**再**改实现。**不允许**先写代码、再倒推文档（这正是 `LV5` 计划行第 1 条禁止的顺序）。

### 10.3 冻结签署

| 项 | 值 |
|---|---|
| 冻结版本 | `data_gap/v1` + `data_gap_report/v1` |
| 冻结日期 | **2026-11-05** |
| 依据诊断 | §1（`volume DOUBLE NOT NULL` `:1200`；`else 0.0` 仅覆盖列缺失 `:2188`；docstring 具误导性 `:2180`；`astype(float)` 无校验 `:2209`；事务回滚 `:2233`；WARN 吞掉 `scripts/check_live_portfolio_health.py:928`；写入调用点未逐标的隔离） |
| 本文择定的分支 | `LV5` 验收 ① 取**「显式拒写并计数」**，不取「落 `NULL`」（§4.2、§9.1） |
| 下一交付物 | `LV5` 计划行「交付」第 ②③④ 条：写入侧 `NullVolumeRefused` 守卫 + 日线覆盖缺口清单 + 报告分列，配套新增 `common/tests/test_ohlcv_null_volume_contract.py`（全离线，**禁止**读 `cache.duckdb`）。**在本文冻结之后**方可开始 |

