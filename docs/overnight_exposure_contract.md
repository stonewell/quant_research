# 隔夜暴露下调的固定时点决策契约（冻结版 v1）

> **文档性质**：**冻结的接口契约（frozen contract）**，不是实施记录、不是研究结论、也不是设计探讨。本文定义 `R6` 的**唯一判据、决策时点、状态词表与确定性边界**；实现、调用方与后续审计必须**消费**本文，不得反过来由实现倒推口径。
> **冻结日期**：**2026-11-06**。契约版本号：`overnight_exposure/v1`，决策规则规格号 `r6.1`（实现侧常量 `common/overnight_exposure.py::DECISION_SPEC_VERSION`）。
> **状态**：**FROZEN**。本文冻结的六条决定即实施依据，实施侧不得另行解释；实现与本文字面冲突时以本文为准，并须提出修订段而非就地改实现。
> **任务状态 SSOT**：[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.20 `R6` 行是任务状态、优先级与验收的唯一权威；本文只维护**判据、状态词表与留痕口径**这一项 SSOT。本文**不维护**任务状态。
> **修订方式**：本文正文一经冻结即**不可改写**；任何变更以 **append-only** 的 `## Revision YYYY-MM-DD` 追加段落在文末落地。冻结文本 + 全部修订段共同构成现行契约，冲突时**修订段覆盖冻结文本**，但冻结文本的字面保留以便追溯。
> **实现位置**：`common/overnight_exposure.py`（纯函数 + 冻结 dataclass）；配套离线用例 `pipeline/tests/test_overnight_exposure_decision.py`。

## 0. 冻结范围与非目标

### 0.1 本文冻结什么

| 项 | 冻结内容 | 章节 |
|---|---|---|
| 定位 | **降隔夜暴露**的收盘前决策，不是日内择时、不是个股选择、不是波动率目标 | §1 |
| 决策时点 | `14:45:00`（`Asia/Shanghai`）= 14:45 那根 5m bar 的**结束时刻**；只允许使用 bar-END `<= 14:45` 的 bar | §2 |
| 触发判据 | 日内回撤 `(cutoff_bar_close - running_high) / running_high`，比较符 `<=`，阈值 `-0.15` | §3 |
| 下调幅度 | 触发 ⇒ 市场层暴露系数 `0.5`；未触发 ⇒ `1.0` | §4 |
| 状态词表 | 封闭三值 `TRIGGERED` / `NOT_TRIGGERED` / `INSUFFICIENT_DATA`；数据不足 ⇒ `scale is None` 且**永不可读作 0** | §5 |
| 确定性 | `(bars, as_of_date, cutoff, thresholds)` 的纯函数；无时钟、无 I/O、无随机、无全局状态；成交价口径**不是**输入 | §6 |
| 未触发的零改动 | 未触发 ⇒ `scale` 为字面 `1.0`，除新可观测字段外不动任何票据字段 | §7 |
| 非目标 | 见 §8（含 `R7` 的 5m 开关面、`R1`/`R4`/`R5` 的阈值、以及**尚未接线**的消费面） | §8 |

### 0.2 零版本轴声明

本任务**新增一个纯函数模块**：不改 `pipeline/research/blend/blend_base.py`、不改 `common/allocation_templates.py`、不改 `pipeline/research_strategy/rs/config.py`、不改任何策略 / 模板类、不碰 `BASE_VERSION`、不动任何 per-class `version`、不跑 `--dump-strategies`、不改 [`docs/version.md`](file:///home/stone/Work/github/quant/docs/version.md)。`DECISION_SPEC_VERSION = "r6.1"` 是**决策规则规格号**，不是策略版本号 —— AGENTS §7 对纯函数模块不产生版本义务，也**不得**「为对称」凭空造一个 `BASE_VERSION`。

## 1. 冻结决定 1：这是「隔夜暴露下调」，不是日内择时

**冻结决定 1（逐字）**：本契约描述的是 **overnight exposure reduction（隔夜暴露下调）** —— 一个在**收盘前**做出的、关于「愿意带多少市场暴露穿过隔夜跳空」的决策。它**不是**日内择时、**不是**逐标的选择、**不是**波动率目标化（volatility-targeting）叠加层。

三条排除项的字面含义（后续「优化」最容易踩的三种误读）：

1. **不是日内择时**：它不预测日内路径，不做盘中多空切换。决策在**一个固定钟点**做一次，关心的是**夜里**而不是接下来十分钟。
2. **不是逐标的选择**：模块里**没有任何 symbol 维度**。输出是**市场层乘数**，乘在整个簿上；改单个标的权重是另一件事，不属于本条。
3. **不是波动率目标化**：不估已实现波动、不算波动率比值、不引入任何方差预测派生的缩放。触发量是一**回撤读数**，不是波动估计。

### 1.1 与 `M3.2` 负结果的关系（既不依赖，也不被证伪）

**必须写在实现之前**：本条**不**建立在 `M3.2` 的负结果之上，也**不得**被读成「`M3.2` 已证伪，所以 `R6` 不成立」。

- `M3.2` 检验的是**成交价口径的偏差** —— 「窗口 VWAP 作为成交价是否系统性偏离引擎记账价」，被解释变量是**成交价**。
- `R6` 检验的是**隔夜跳空风险** —— 「收盘前减仓是否值得」，被解释变量是**隔夜收益**。

被解释变量不同 ⇒ `M3.2` 的负结果**既不构成**对 `R6` 的否定证据、**也不构成**支持。`R6` 只能由它自己的验收用例独立成立，或独立按**负结果结项**（后者完全允许，见 §2.20 表头「本表是待验证的假设清单」）。

### 1.2 数字来源的诚实披露（不伪造文献）

`14:45` / `-0.15` / `0.5` 三个数字是**业主固定的运维风险预算约定**（2026-11-06 冻结），**不是**经样本优化的参数、**不是**任何论文的引用值。实现与文档**不得**为它们编造学术出处；需要改写这三个数字时，**先**以修订段更新本文，**再**改实现，不得「顺手调参」。

## 2. 冻结决定 2：决策时点 = `14:45:00`（仅用 bar-END `<= 14:45` 的 bar）

**冻结决定 2（逐字）**：决策时点为会话时区（默认 `Asia/Shanghai`）的 **`14:45:00`**，即 **14:45 那根 5m bar 的结束时刻**。判定**只允许**使用 **bar-END `<= 14:45:00`** 的 bar。

### 2.1 为什么必须是 14:45（而不是「收盘前若干分钟」这类含糊说法）

回购引擎今天只有四种**日频**成交价模式（`common/allocation_backtester.py::EXECUTION_PRICE_MODES = ("close", "next_open", "next_vwap", "intraday_twap")`）。其中两种与本条相关：

- `close`：价格取 **15:00 收盘**；
- `intraday_twap`：价格是**整个会话的 TWAP**，其**最后一个切片不早于 14:45**。

`14:45` **严格早于**这两个取价时刻（`14:45 < 15:00`，且 `14:45 <= TWAP 最后切片`）。因此该决策**不可能**是它所要作用的价格的函数 —— 这正是「固定时点」的全部意义。

若把判定时点挪到取价时点**之后**（例如 `15:05`），决策就会读入「价格被取走之后才存在」的 bar，同一决策在 `close` 与 `intraday_twap` 两套口径下退化为两个不同的策略。这是「执行价格未来函数」的最小可测形式：**把默认 `cutoff_time` 改成 `time(15, 5)`，`test_frozen_cutoff_precedes_both_price_taking_moments`（切断序断言）必须转红**（见 §10）。

### 2.2 bar 时间戳约定

输入必须是 **END 标签**（标签 `t` 覆盖 `(t-5m, t]`），与 `R3` 冻结的面板规格一致（`common/intraday_synthetic.py::BAR_TIMESTAMP_CONVENTION = "END"`，48 根/日：09:35 … 11:30 + 13:05 … 15:00）。一个 START 标签的 feed 会让决策整体前移 5 分钟；本契约不为其做兼容，也不在运行时猜 —— 调用方必须提供 END 标签。

### 2.3 `cutoff` 的解析

`cutoff = tz_localize(session_timezone, as_of_date @ cutoff_time)`。默认时区 `Asia/Shanghai` 自 1991 年起无夏令时，故该时刻唯一。若把 `session_timezone` 换成在该钟点存在**折返 / 跳变**的时区，本地化会产生歧义 ⇒ **拒绝并抛 `ValueError`**，不得任选一个瞬时。

## 3. 冻结决定 3：触发判据（日内回撤，边界**包含**）

**冻结决定 3（逐字）**：触发量是**日内回撤**，定义为「会话内至决策时点的 running high 到 cutoff bar 的 close」的回撤，取

```
running_high = max(high)   over 可用 bar 中 bar-END <= cutoff 者（仅 as_of_date 当日）
drawdown     = (cutoff_bar_close - running_high) / running_high
触发         ⇔  drawdown <= -0.15
```

**边界为包含（inclusive）**：**恰好 `-0.15` 触发**；`-0.1499` **不触发**。即实现必须用 `<=`，不得用 `<`。

### 3.1 为什么定义要这么「最小」

- **只用 `high` 的 running max 与 cutoff bar 的 `close`**：两个量都可从公开 OHLC 逐 bar 复算，不引入 VWAP、不引入成交量加权、不引入跨 bar 插值。任何更「精细」的定义（例如逐 tick 峰值、或对 close 做 running max）都会让「同一面板、两套实现、两个结论」成为常态。
- **分母是 running high 本身**（而非会话首 bar 的开盘价）：这样 `drawdown` 是「自当日峰值回撤」，与日内高点的相对位置直接可比，也与 `R1` 消费的 `dd_trough`（自日内峰值到谷的回撤）在**语义**上同族 —— 但**阈值不共享**，见 §8。
- **先减后除**：`(close - high) / high`，**不是** `close / high - 1.0`。这不是风格偏好：只有前者能让「恰好 `-0.15`」作为一个**浮点字面量**存在（取 `high = 20.0`、`close = 17.0` 时 `(17-20)/20 == -0.15` 精确成立；`close/high - 1.0` 对所有浮点价格对都**无法**等于字面量 `-0.15`）。边界用例因此断言**精确相等**，而不是 `approx`。
- **无下界（刻意）**：`running_high` 覆盖 `as_of_date`（**会话时区**日期）当日、bar-END `<= cutoff` 的**全部**可用 bar，**不设 09:30 下界**。因此一根早于开盘的 bar（`R3` 分类里的 `pre_session` 缺陷）会参与峰值。此处选择**披露而非静默过滤**：在判据内部悄悄丢行等于新增一条本文没有冻结的隐藏规则。正确做法是修 feed，而不是教决策忽略它。

### 3.2 可用 bar 与「数据不足」的判定顺序

实现按下列顺序判定，任一条不满足即 `INSUFFICIENT_DATA`（**不得**静默通过）：

| 序号 | 判定 | 原因码 |
| :--- | :--- | :--- |
| 1 | 帧非空 | `empty_frame` |
| 2 | 该 `as_of_date` 在会话时区下存在 bar | `no_bars_for_date` |
| 3 | 至 cutoff 的**可用** bar（`high`/`close` 均有限）`>= 2` | `too_few_usable_bars` |
| 4 | 存在**恰好**结束于 cutoff 的 bar | `no_bar_ending_at_cutoff` |
| 5 | 该 cutoff bar 自身的 `high`/`close` 有限 | `cutoff_bar_unusable` |
| 6 | 价格基准为正（`running_high > 0` 且 `cutoff_close > 0` 且所有可用 `high > 0`） | `non_positive_price_base` |

第 3 条的门槛 `>= 2` 的理由：只有一根 bar 时「running high」与「当前价」是同一根 bar，回撤退化为该 bar 自身振幅的恒等式，不是路径读数。

第 4 条**按身份匹配**（`bar-END == cutoff`），**不按邻近匹配**：半日市（上午收盘后无下午）在 14:45 根本没有价格，插值出一个价格就是编造。缺 14:45 那根 bar 的 feed（缺口、或 START 标签）同样落 `no_bar_ending_at_cutoff`。

## 4. 冻结决定 4：下调幅度 = 市场层系数 `0.5`

**冻结决定 4（逐字）**：触发时市场暴露系数为 **`0.5`**；未触发为 **`1.0`**。该系数是**市场层乘数**，**永远不是**对单标的权重的编辑。

推论（实施侧必须遵守）：

- 系数只有一个作用点：把目标暴露（或其等价的市场层规模）**整体**乘以该系数；**不得**逐 symbol 相乘、**不得**先归一化再乘、**不得**把它混进选股 / 排序逻辑。
- 参数名固定为 `scale_on_trigger`，值域 `(0, 1]`：`0.0` 等于清仓（不是「下调」），`> 1` 等于加杠杆，二者都超出本条语义 ⇒ 取值越界时**抛 `ValueError`**。

## 5. 冻结决定 5：状态词表（封闭三值）

**冻结决定 5（逐字）**：状态词表**封闭且显式**，只有三个值：`TRIGGERED`、`NOT_TRIGGERED`、`INSUFFICIENT_DATA`。`INSUFFICIENT_DATA` ⇒ `scale is None`，且 **永远不得**被读作 `0`，**永远不得**被读作「没有回撤」。

| 状态 | `scale` | `drawdown` | 含义 | 消费方必须做什么 |
| :--- | :--- | :--- | :--- | :--- |
| `TRIGGERED` | `0.5` | 数值（`<= -0.15`） | 隔夜暴露按冻结比例下调 | 乘 `0.5`，并在可观测字段写明下调原因 |
| `NOT_TRIGGERED` | `1.0` | 数值（`> -0.15`） | 不触发，暴露**完全不变** | 乘 `1.0`（或等价地跳过），**不得**产生任何附带的额度改动 |
| `INSUFFICIENT_DATA` | **`None`** | **`None`** | 无法判定 | **禁止**当成 `1.0`（静默通过）、**禁止**当成 `0.0`（清仓）；须显式降级并告警 |

### 5.1 为什么必须区分「数据不足」与「未触发」

这是本仓库反复出现的失败模式：缺失被容错吞掉，于是「没数据」在下一层看起来像「风险结论」。`0.0` 是双重谎言 —— 对 `scale` 而言它等于清仓，对 `drawdown` 而言它等于「当日没有回撤」。因此：

- 两个字段在 `INSUFFICIENT_DATA` 下必须是 **`None`**，不是 `0.0`、不是 `nan`、不是缺键；
- 冻结 dataclass 在**构造时**校验该不变量（`INSUFFICIENT_DATA` 携带数值 ⇒ 立即 `ValueError`），因此手工拼出的实例也无法冒充这个状态；
- 用例 `test_insufficient_data_never_read_as_zero` 对五种不足形态逐一断言 `0.0 not in {scale, drawdown}`。

## 6. 冻结决定 6：确定性（纯函数）

**冻结决定 6（逐字）**：该函数是 `(bars, as_of_date, cutoff, thresholds)` 的**纯函数** —— 无时钟读取（无 `datetime.now`）、无 I/O、无全局状态、无随机。调用两次返回**相等**结果；**成交价口径不是输入**（这正是两套口径必须产生同一决策序列的原因）。

由此产生的可验证推论：

1. 同一输入两次调用，冻结 dataclass **相等**（`test_not_triggered_is_bit_identical_scale` 断言整对象相等）。
2. 输入**顺序无关**（实现内部按 max/身份匹配取值，不依赖行序）。
3. 同一瞬时的不同时区标注 ⇒ 同一决策（`test_utc_stamped_feed_yields_the_same_decision`）。
4. `cutoff < close 取价时刻` 且 `cutoff <= TWAP 最后切片`（`test_frozen_cutoff_precedes_both_price_taking_moments`）。
5. 收盘后（`> 14:45`）的 bar 取值再极端，决策**逐位不变**（`test_decision_ignores_post_cutoff_bars`）。

**诚实披露（两套成交价口径的一致性为什么不能被「用事例测」）**：`close` 与 `intraday_twap` 下「决策序列一致」是一个**构造性**结论，而不是经验结论 —— 本模块的签名里**根本没有**成交价 / 口径参数（`bars, as_of_date, cutoff_time, drawdown_threshold, scale_on_trigger, session_timezone`），无论调用方将来用哪套口径成交，传给它的输入完全相同，因此不存在任何可观测的差异可供检验；把同一函数用同一份 `bars` 跑两遍再断言「两遍相等」，只是在把函数与它自身比较，什么也证明不了。本契约里「执行价格未来函数」的**唯一可测形式**是**切断序回归**：冻结决策瞬时必须严格早于 15:00 收盘、且不晚于 TWAP 最后切片（`test_frozen_cutoff_precedes_both_price_taking_moments`）；而「成交价口径不是输入」这一半由**签名结构**断言（`test_decision_is_independent_of_execution_price_by_construction`：不得存在 name 含 price/mode/convention/execution 的形参，且传入 `execution_price=...` 必须抛 `TypeError`，即不得有 `**kwargs` 把口径静默吞掉）。反向改坏仍以**切断序**转红为准：默认 `cutoff_time` 改为 `time(15, 5)` 时，转红的是切断序断言，而不是某个「双口径相等」断言。

### 6.1 输入契约（拒绝优先于猜测）

| 项 | 要求 | 违反时 |
| :--- | :--- | :--- |
| 容器 | `pandas.DataFrame` | `TypeError` |
| 索引 | **tz-aware** `DatetimeIndex`，bar-**END** 标签 | 非 `DatetimeIndex` / **tz-naive** ⇒ `ValueError` |
| 列 | 至少 `high`、`close`（数值） | 缺列 ⇒ `ValueError` |
| 时间戳唯一性 | 单标的序列；重复时间戳 | 重复 ⇒ `ValueError` |
| 跨度 | 可跨多个会话；只读 `as_of_date` 当日 | 无当日 bar ⇒ `INSUFFICIENT_DATA` |

**tz-naive 必须拒绝**：一个不带时区的 `14:45` 是**歧义瞬时**，替它猜一个时区，就会让一条差一小时的 feed 静默地在 13:45 或 15:45 做决策。错误信息必须**可操作**（指明先 `tz_localize`，且只有在确实知道 feed 时区时才这么做），并点明冻结会话时区。

## 7. 「未触发」= bit-identical（零改动）

**冻结**：未触发时，`scale` 是字面量 **`1.0`**（`SCALE_UNCHANGED`），不是 `0.9999`、不是缺键、不是 `None`。除本条新增的**可观测字段**外，票据 / 报告的任何既有字段**不得**发生变化。

- 实现侧：未触发分支只回 `scale=SCALE_UNCHANGED` 与读数，不做任何「顺手」的归一化或舍入。
- 验收侧：`test_not_triggered_is_bit_identical_scale` 同时断言 `scale == 1.0 == SCALE_UNCHANGED`、两次调用整对象相等、`as_dict()["scale"] == 1.0`。
- **本条不含消费面**：票据字段与 Stage-2 缩放属 `R6` 的**待确认**部分，尚未接线（见 §8），因此「零改动」今天只对**新增可观测字段**成立，不对任何既有工单形状作出承诺。

## 8. 已知非目标（known non-goals）

以下各项**明确不在**本条范围内，日后不得以「顺手一并」的方式塞进 `common/overnight_exposure.py`：

1. **5m 开关面（`R7`）**：`pipeline/research/blend/blend_base.py` 的 `enable_intraday_5m` / `enable_5m_interval_nesting` / `use_intraday_downside_vol`（三者默认均 `False`）不动。缺 5m 数据时冻结新开仓是 `R7`，不是本条的降级动作。
2. **日内择时**：不做盘中多空切换、不做 bar 级进出场。固定时点、一次决策。
3. **逐标的选择**：不做任何 symbol 维度的判别、排序或权重编辑。
4. **阈值不合并**：与 `R1`（`dd_trough` 幅度决定目标暴露）、`R4`（横截面离散度崩塌）、`R5`（盘中回撤门禁）**各自的阈值互不复用、互不调用**。同一数据、不同目的；合并即两侧同时失真。特别是 `-0.15` **不得**被解释为 `R1` / `R5` 的阈值。
5. **消费面未接线（待确认）**：Stage 2 目标持仓与 cash sweep 段、票据可观测字段均为 `R6` 的**待确认**部分，本次**未实施**。落地时必须先冻结字段名与 `INSUFFICIENT_DATA` 在票据上的显式取值，且**不得**让 `None` 在票据里退化成 `1.0` 或 `0.0`。
6. **不做日历 / 交易日历推断**：本函数不判断 `as_of_date` 是不是交易日，也不填补缺失会话。
7. **不写任何 I/O**：不落 JSON、不写归档、不改 `data/runs/**`。

## 9. 输出结构（可观测字段的最小集合）

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `as_of_date` | `datetime.date` | 被评估的会话日期（会话时区） |
| `decision` | `str` | 封闭三值之一（§5） |
| `scale` | `float \| None` | `0.5` / `1.0` / `None`（`None` 仅数据不足） |
| `drawdown` | `float \| None` | 冻结定义（§3）；`None` 仅数据不足 |
| `cutoff` | `pd.Timestamp` | **实际解析出**的决策瞬时（带时区） |
| `bars_used` | `int` | 参与 running high 的可用 bar 数（至 cutoff） |
| `reason_code` | `str` | 机器稳定原因码（`drawdown_at_or_below_threshold` / `drawdown_within_threshold` / 六个不足码） |
| `reason` | `str` | 人类可读说明（不得被解析） |
| `running_high` / `cutoff_close` | `float \| None` | 取证用，使回撤可被第二人复算 |

`as_dict()` 提供 JSON-safe 视图（含 `spec_version`），供将来的票据字段使用；**当前无任何消费方**。

## 10. 验收与「反向改坏即转红」留痕

| 编号 | 验收项 | 对应用例 | 反向改坏（必须转红） |
| :--- | :--- | :--- | :--- |
| ① | 固定时点触发 ⇒ 暴露按冻结比例下调 | `test_triggers_on_fixed_time_drawdown`、`test_trigger_ignores_the_size_of_the_later_collapse` | 删掉触发判据 |
| ② | 未触发 ⇒ bit-identical | `test_not_triggered_is_bit_identical_scale` | 让未触发分支返回非 `1.0` |
| ③ | `close` 与 `intraday_twap` 决策序列一致（**构造性**，见 §6 披露）＋切断序回归（时点挪到取价之后即转红） | `test_decision_is_independent_of_execution_price_by_construction`、`test_frozen_cutoff_precedes_both_price_taking_moments`、`test_decision_ignores_post_cutoff_bars` | 默认 `cutoff_time` 改为 `time(15, 5)`；或给签名加上 `execution_price` / `**kwargs` |
| ④ | 时区 / 半日市边界固定 | `test_naive_index_rejected`、`test_insufficient_data_never_read_as_zero`、`test_utc_stamped_feed_yields_the_same_decision` | 把 `<=` 改成 `<`；或把不足分支的 `scale` 改成 `0.0` |

三处反向改坏的实测留痕（改坏 → 转红 → 还原 → 复绿）记录在 `R6` 的结项附注里，不在本文复述；本文只钉住「改哪里必须转红」。
