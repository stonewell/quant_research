# 账本前推、NAV 历史计数与版本元数据 Schema（冻结版 v1）

> **文档性质**：**冻结的接口契约（frozen contract）**，不是实施记录，也不是设计探讨。本文定义 `prior_state` 的**语义**、`frozen_suspect` 的**谓词**、`nav_history_10d` 的**计数口径与最小样本门槛**、以及票据版本与 `strategy_dumps` 的**交叉校验要求**；写状态侧（Stage 1 / Stage 2）与出票侧必须**消费**本文，不得反过来由实现倒推字段。
> **冻结日期**：**2026-11-05**。Schema 版本号：`prior_state/v1`、`nav_history_10d_counting/v1`、`version_xcheck/v1`。
> **状态**：**FROZEN**。本文是 `LV7`（`prior_state` 前推、版本元数据与 `nav_history_10d` 截断）的**第一项交付物**；在本文冻结之前**不允许写任何实现**（`LV7` 计划行第 1 条：「先冻结 `prior_state` 的语义（「上一交易日的**已确认**状态」而非「上一次运行的状态」）、以及 `nav_history_10d` 的**最小样本门槛**（不足时应输出什么状态码而不是凑数）」）。
> **任务状态 SSOT**：[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV7` 行是任务状态、优先级与验收的唯一权威；本文只维护**前推语义 / 计数口径 / 校验要求**这一组 SSOT。本文**不维护**任务状态。
> **修订方式**：本文正文一经冻结即**不可改写**；任何变更以 **append-only** 的 `## Revision YYYY-MM-DD` 追加段落在文末落地（见 §9.1）。冻结文本 + 全部修订段共同构成现行契约，冲突时**修订段覆盖冻结文本**，但冻结文本的字面保留以便追溯。

## 0. 冻结范围、非目标与判断口径

### 0.1 本文冻结什么

| 项 | 冻结内容 | 章节 |
|---|---|---|
| 语义 | `prior_state` 是「上一**交易日**的**已确认**快照」，及其整字典前推规则 | §2 |
| 标记 | `frozen_suspect` 的名称、类型、位置、写入分支、谓词（按分相等）、不存在与复跑处置 | §3 |
| 计数 | `nav_history_10d` 的持久化保留契约（不变）+ `raw` / `distinct` 口径 + 恒等式 + 最小样本门槛 `n` | §4 |
| 版本 | 票据 `strategy_version` / `base_version` 与 `strategy_dumps` 的对齐关系、边界、失败级别 | §5 |
| 输出 | 新增字段的**准确键名**、落点（`account_state.json` / `stage1_health_report.json`）与加性规则 | §6 |
| 验收 | `LV7` 四条验收 → 本文的映射，以及不可删除的断言清单 | §8 |

### 0.2 非目标（本文明确**不**改变什么）

1. **不改变**估值、分档、阈值数学（`LV1` / `LV12` 的地盘）：熔断档位、`linear_equity_scale`、快速恢复覆盖的任何阈值一律不动；本文只影响「某个值是否被记为**已测得**」这一事实陈述。
2. **不碰** Stage 2 的票据定量：`delta_weight` / `current_shares` / `current_weight` / `target_shares` 的计算与写出**一个字节不改**。
3. **不重写**历史归档：已闭合的 `data/runs/<date>/account_state.json` 是已发布的测量值，**禁止**回溯修正；本文新增的字段只从下一轮起生效。
4. **不进入**版本轴：不改任何策略 / 模板的 `version`、不改 `BASE_VERSION`、不跑 `--dump-strategies`、不写 `docs/version.md`。`LV7` 全部工作是「状态文件的语义与校验」。
5. **不改变** `append_nav_history_point`（`scripts/check_live_portfolio_health.py:984-1024`）的**字节级行为**：上限 10、溢出丢最旧（`:1022-1023`）、重复点**保留**（`:1019-1021` 仅 WARN）一律不动。本文改的是**读侧计数**，不是写侧序列。
6. **不引入**放行白名单：不为「合法的平盘日」开任何标记豁免或 allowlist（理由见 §3.6）。
7. **不裁定**「上游表单值是否真的冻结」。本文只定义**如何把可判定的那一部分判出来并写成标记**，不声称任何具体归档日属于冻结。

> 一句话：本文是规定「哪份账本才配叫上一日、哪段序列才配叫已测得」的那张纸，不是改门禁或改出票的那张纸。

### 0.3 判断口径

- 凡属**判断**而非**事实**的条目，本文同时写出**决定**与**一行理由**（`→ 理由：`）。
- 凡本文引用的 `file:line`，均为本会话给定并核对过的行号。为固定**字面量**而额外核对的三处（`NAV_10D_RECOVERY_WINDOW = 10`（`:121`）、`NAV_10D_STATUS_OK = "OK"` / `NAV_10D_STATUS_INSUFFICIENT_HISTORY`（`:126-127`）、`nav_10d_status` 进 `dd_info`（`:1204`））在正文中**单独标注**，不冒充给定行号。
- 本文**不**提出任何量化结论（不估恢复幅度、不估冻结发生率、不推断成因）。

## 1. 冻结所依据的现状诊断

本节只陈述**可复核的事实**，作为 §2 / §3 / §4 判定规则的动机。

1. `prior_state` 有两个写入点：`web/livetrading/execution_service.py:2364-2367` / `:2383-2386` 以 `prior_state = _carry_forward_account_state(...)` 播种；`scripts/check_live_portfolio_health.py::build_account_state_update`（`:784` 正常支、`:749` 降级支）持久化 `prior_state_to_preserve`，后者由 `main()`（`:2366-2384`）算出。
2. `_carry_forward_account_state`（`web/livetrading/storage.py:2057`）返回的是**上一日 `account_state` 字典的整体**——包括它**自己的** `as_of_date`、`current_nav`、`nav_history_10d`。即：前推是整字典级别的，不是挑选若干键的。
3. 当日首次运行：`prior_state_to_preserve = deepcopy(state)` 后 `pop("prior_state")`（`:2380-2384`）。即：**被前推的源就是当时加载到的那份 state 本身**，唯一切掉的键是 `prior_state`。
4. `append_nav_history_point`（`:984-1024`）追加的是**有效净值**（`effective_nav`），溢出处 `history[-10:]`（`:1022-1023`）丢最旧；与上一点完全相同的点**仍然追加**（`:1019-1021` 只置一个 `is_frozen_duplicate` 标志供调用方 WARN），序列因此**保持时序且不去重**。
5. 10 日门禁以 `len(nav_hist)` 判定（`:1136-1138`），不足即 `INSUFFICIENT_HISTORY`（`:1152-1157`）。
6. **全仓不存在 `frozen_suspect`**（`grep` 无任何匹配）。
7. `verify_live_strategy_dump_preflight` 在 `scripts/run_live_strategy.py:1805` 被调用；它**在版本不一致时是抛错还是只告警，本轮未核实**（§5.4 把「先核实」写成实现阶段的第一步）。

> **由此得到的唯一结论**：`prior_state` 的**来源**由代码决定（「当时加载到的 state」），而它的**语义**从未被任何断言固定过——只要上游表单 / 盯市值冻结，前推的「上一日」就会与「当日」逐分相同，整条恢复链随之**自我确认**；同时「序列有 10 个点」与「有 10 个交易日的观测」在今天的门禁里是**同一件事**，于是重复点可以静默凑满门槛。本文的第一性要求是：**把「哪份账本才算上一日」与「几个点才算已测得」都变成可断言的谓词**。

## 2. `prior_state` 的语义（核心）

### 2.1 定义（冻结）

```
prior_state ∶= 满足下列条件的 account_state 快照 S
  (1) S.as_of_date < 当日 as_of_date                          （严格早于当日）
  (2) S.as_of_date == max{ d : d < 当日 as_of_date ∧ 该日的 state 已确认 }   （最近一个已确认日）
  (3) 「已确认」⇔ 该日 state 的 current_nav 来自当轮**实测盯市值**，
      不是表单回落值、不是降级轮沿用值
  (4) S 内不得含 prior_state 自身（禁止嵌套），见 §2.3
```

**一句话语义**：`prior_state` 是「上一交易日的**已确认结算**」，**不是**「上一次运行的状态」，**更不是**「今天这份状态的一份拷贝」。

| 被前推的"源" | 是否符合本文 | 理由（一行） |
|---|---|---|
| 上一**已确认**交易日的整份快照（`as_of_date` 严格更早） | **符合** | 它是唯一独立于当日观测的账本 |
| 当日加载到的那份 state（`as_of_date` 可能已被重盖成今天） | **不符合** | 源与目标同值 ⇒ 派生的「上一日」不携带任何新信息，见 §2.4 |
| 上一次运行的产物（同日复跑的第一轮结果） | **不符合** | 同日复跑必须取**保留下来的 `prior_state`**，不得把今天当昨天（§3.7） |
| 降级轮（未取到实测盯市值）的 state | **不符合** | 它的 `current_nav` 不是当轮观测值，作前推源等于把「没测」洗成「测了」 |

### 2.2 必须携带什么

**规则（整字典前推，不做键白名单）**：`prior_state` 必须**逐键**等于 §2.1 选定快照 `S` 的同名键值，仅剥离 `prior_state` 自身（及 §6.2 列出的三个「对本日判断」字段）。

→ 理由：`_carry_forward_account_state`（`web/livetrading/storage.py:2057`）本来就是整字典返回，今天的两处读取方也各自按名取键；若改写成「挑选若干键」，任何**新增**的状态键都会被静默漏带，而漏带的后果是下游读到缺省值而不是上一日的值——这正是本任务要消灭的「无第二观测」缺陷的另一种形状。

由此，下列键**必然**被携带（不是额外要求，而是整字典规则的推论）：

| 键 | 谁读它 | 用途 |
|---|---|---|
| `as_of_date` | `frozen_suspect` 谓词（§3.3）、`prior_state_age_days`（§6.3） | 证明「这是哪一天的账本」 |
| `current_nav` | `frozen_suspect` 谓词（§3.3）、Stage 1 回撤 / HWM 基线（`:1061-1063`） | 上一日的已确认净值 |
| `nav_history_10d` | 快速恢复度量（`:1135-1160`） | 上一日的序列副本 |
| `holding_bars` | `resolve_holding_bars`（`scripts/run_live_strategy.py:623-645`，读点 `:628-631`） | 持仓周期门 / tenure lock |
| `entry_signal_streak` / `zero_target_streak` / `entry_prices` / `last_exit_date` / `lifecycle_states` | `scripts/run_live_strategy.py:1871-1882` 整块替换；`lifecycle_states` 另见 `storage.extract_lifecycle_states`（`storage.py:663-665`） | 入场确认、退出冷却锚点、UPLSM 状态机 |

### 2.3 禁止形态（可断言的负面清单）

| # | 禁止形态 | 可观测判据 |
|---|---|---|
| F1 | 当日复制：`prior_state` 与顶层除 `as_of_date` 外逐字段相同，且 `prior_state.as_of_date == 当日 as_of_date` | 由 §3.3 谓词的 `true` 支与 `prior_state_age_days == 0` 共同暴露 |
| F2 | 同日回授：把本轮写出的 state（含本轮 `current_nav`）当作本轮 `prior_state` | `prior_state.current_nav == 本轮实测净值`（即 §3.3 的 `true` 支） |
| F3 | 嵌套：`prior_state` 内含 `prior_state` | 存在即违约（无歧义） |
| F4 | 携带判断：`prior_state` 内含 `frozen_suspect` / `frozen_suspect_basis` / `prior_state_age_days` | 存在即违约（§6.2）——这三个字段是对「某一对日期」的判断，不是状态的属性 |
| F5 | 日期不前进：`prior_state.as_of_date >= 当日 as_of_date` | 直接比较两个 ISO 日期字符串（同格式字典序即时间序） |

### 2.4 自我确认的失效机理（本文存在的理由）

设当日数据链为：表单 / 盯市值 `v` → 顶层 `current_nav` → `nav_history_10d` 追加点 → 10 日恢复度量 → 覆盖判定（fast-recovery override）。若上游 `v` **冻结**（同一个数被反复送入），且 `prior_state` 就是当日这份 state 的拷贝，则：

1. `prior_state.current_nav == 本轮有效净值`（同一个 `v`）；
2. 追加的点也与上一点相同 ⇒ 序列里出现重复；
3. 于是**每一个下游检查都在与自己比对**：它读到的「上一日」不携带任何独立观测，任何一致性校验都必然通过。

→ 理由：这就是「**没有第二观测**」的失效形态。校验要想有意义，它必须至少有两个**互相独立**的输入；今天的前推把它变成了一个输入的两份副本。也因此，§3 的标记只能是**advisory**——系统能判出「两值按分相同」，但**无法**判出「这是真的平盘还是上游冻结」，两者在数据上不可区分（§3.6）。

## 3. `frozen_suspect` 契约

### 3.1 名称、类型、位置

| 项 | 冻结值 |
|---|---|
| 字段名 | **`frozen_suspect`**（精确，全小写 `snake_case`，无前缀、无连字符） |
| 类型 | `bool \| null`（三值，**禁止**用 `"true"`/`"yes"`/`0`/`1` 等替代） |
| 值域 | `true` = 命中；`false` = 明确不命中；`null` = **不可判定** |
| 放置层级 | `account_state.json` **顶层**，与 `prior_state` 同级（不是 `prior_state` 内部，见 F4） |
| 配套证据 | 同层 `frozen_suspect_basis`（对象，§6.3），与该布尔**同源同轮**产出 |
| 计算点 | 决定 `prior_state` 的**同一个点**：`build_account_state_update`（`:784` 实测支 / `:749` 降级支）与 `execution_service` 的两处播种（`:2364-2367` / `:2383-2386`），共用**同一个纯函数谓词** |

→ 理由（单一计算点）：两处各写一份同义实现，迟早分叉成「Stage 1 说命中、Stage 2 说不命中」，而读方只会看其中一个。

### 3.2 写入分支（**两个分支都必须写这个键**）

| 分支 | 位置 | `frozen_suspect` 取值 | 说明 |
|---|---|---|---|
| 实测（measured） | `build_account_state_update` `:784` | `true` / `false`（按 §3.3） | 本轮取到了实测有效净值，可以做比较 |
| 降级（degraded） | `build_account_state_update` `:749` | **`null`** | 本轮**没有**有效净值观测 ⇒ 无第二值可比；**不得**写 `false`（未测得 ≠ 否），**不得**写 `true`（不得凭空指控） |
| 当日复跑的播种 | `execution_service` `:2364-2367` / `:2383-2386` | 按 §3.7 从**保留的** `prior_state` 复算 | 与首轮同值，不累积 |

→ 理由：降级轮保留 `prior_state` 而不写新点（计划 §4.30 第 7 条），此时「今日值」在机器里并不存在；把不存在的关系写成 `false` 就是把「没看」写成「看了，正常」。

### 3.3 谓词（冻结原文）

```
cents(x) = int(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)

frozen_suspect =
    null   ⇔  prior_state 不存在
          ∨  prior_state.current_nav 缺失 / 非有限数 / 不可解析
          ∨  本轮未取得实测有效净值（降级分支）
    true   ⇔  prior_state 存在 ∧ 两值均为有限数
          ∧  cents(prior_state.current_nav) == cents(today_effective_nav)
    false  ⇔  其余情形（两值均为有限数 ∧ 两个分整不相等）
```

判定顺序是**规范**的一部分：先判 `null`（能否判），再判 `true`，剩下的才是 `false`。任何把 `false` 当默认值兜底的实现（例如 `frozen_suspect = a == b`，缺值时 Python 会因 `None == 199935.46` 得 `False`）都是违约——它会在最该报警的那一分支上静默给出「正常」。

### 3.4 为什么是**按分相等**，而不是 `math.isclose(rel_tol=...)`

**决定**：相等性一律在**整数分**上判定（`Decimal(str(x))` 量化到 0.01，`ROUND_HALF_UP`），**不**使用 `math.isclose` 的任何容差。

→ 理由（一行）：净值本身就以 2 位小数被持久化与展示，按分比较在这个精度上是**精确**的整数运算；相对容差只会额外掩盖真实的微小变动，对「冻结」这一失效形态毫无帮助。

三条展开（每条一行为限）：

1. **失效形态是「精确复制」而非「近似抖动」**：上游冻结意味着同一个数被反复送入，`cents` 相等必然命中，用不着容差。→ 容差解决的是一个本任务不存在的问题。
2. **`rel_tol` 的尺度不可见**：`rel_tol=1e-9` 在 199 935.46 上仅约 0.0002 元（只吸收浮点噪声），而 `rel_tol=1e-4` 约为 20 元——后者会把一次**真实的** 20 元以内净值变动掩成「冻结」。选哪个容差都得不出可复算的结论。
3. **可打印、可复核**：`199935.46` vs `199935.46` 两个数直接进 `frozen_suspect_basis`，人一眼可判；`isclose` 的结论无法在报告里被读者复算。

**代价（显式接受）**：任何 ≥ 0.01 元的真实变动都不会命中，任何**恰好**按分相同的合法平盘都会命中。前者是漏报，后者是误报；两者都由 §3.6 的 advisory 定位吸收。

### 3.5 `prior_state` 缺失时（当日首次运行）

| 情形 | 必须 | 禁止 |
|---|---|---|
| `prior_state` 键不存在 | 写入 `frozen_suspect: null`；不得抛异常；`frozen_suspect_basis.prior_nav = null` | 写 `true`（凭空指控）、写 `false`（伪造阴性）、省略该键 |

→ 理由：`null` 是本契约里唯一诚实的第三态；省略键会让读方退化成 `dict.get("frozen_suspect", False)`。

### 3.6 合法平盘也会命中（接受，并把缓解写死在这里）

单一停牌标的、或一次真实的零变动日，同样会让 `cents` 相等 ⇒ `frozen_suspect: true`。**这是可接受的**，因为：

1. **它是 advisory**：`frozen_suspect` **永不**作为任何门禁输入——不进 `nav_10d_status`、不进 `fast_recovery_nav`、不进 tier / 熔断 / `linear_equity_scale` / 覆盖判定。→ 理由：一个既可能误报、又可能漏报的信号一旦接进门禁，就等于把「猜测」升格为「阈值」。
2. **误报成本 = 一次人工阅读**：`frozen_suspect_basis` 同时给出两侧数值、两侧 `as_of_date`、以及「序列本身是否也全同」，操作员可在一眼内区分「平盘」与「冻结」；`frozen_suspect` 只负责**把这件事推到人眼前**。
3. **不设 allowlist / 不设「已确认平盘」豁免字段**：→ 理由：豁免字段会变成一个事实上的门禁输入（谁填谁就让它消失），而被消掉的正是本任务唯一的信号。对真实平盘的正确处置是**在当轮运行记录/工单里说明**，不改归档、不改标记。
4. **与既有的点级 WARN 并存且不合并**：`append_nav_history_point` 的 `is_frozen_duplicate`（`:1019`）检测的是「**本次追加**与上一点完全相同」（且用精确浮点 `==`），`frozen_suspect` 检测的是「**今日有效净值**与**上一日已确认净值**按分相同」。→ 理由：前者能发现一日内的重复追加，后者能发现跨日的自我确认；两者互不覆盖，合并不出更强的信号，只会丢掉其中一个维度。二者均**不**是门禁输入。

### 3.7 同日复跑

`main()` 的 `is_same_day` 分支把 `prior_state` **回滚为已保存的那份 `prior_state`**（`:2364-2384`），即复跑沿用首轮的前推源，而不是把当日顶层 state 再复制一次。冻结如下：

| 项 | 冻结规则 |
|---|---|
| 标记的计算基 | **保留的 `prior_state`**（不是当日顶层 state）——即复跑与前一次运行读同一个前推源 |
| 标记的值 | 与当日首轮**逐字相同**（同一谓词、同一输入 ⇒ 同一输出）；复跑**不累积**、不追加 `_v2` 之类 |
| `nav_history_10d` | **不重复追加**：若加载到的 state 已满足 `as_of_date == 当日` **且**该点是以实测净值得来的（非降级轮），则本轮**跳过追加**，序列原样保留 |
| 降级 → 实测的复跑 | 允许追加**一个**点（首轮降级未写点，计划 §4.30 第 7 条）；表征「本日曾降级」的既有字段（计划同条记为 `nav_degraded_as_of`）**不得**新造同义字段 |
| 幂等断言 | 同一 `as_of_date` 连续运行两次 ⇒ `nav_history_10d` 的长度与逐元素内容**完全相同**，`frozen_suspect` / `frozen_suspect_basis` 逐字段相同 |

→ 理由（不重复追加）：`append_nav_history_point` 本身不做去重（§0.2 第 5 条），所以**唯一**能阻止「同日两次运行 = 两个『交易日』」的地方就是调用点；而重复点正是把「10 个点」凑成「10 天」的最短路径。

**边界（勿越界）**：上述跳转是**调用点**的守卫，`append_nav_history_point` 的签名、返回标志、上限与丢最旧行为**一律不动**（§0.2 第 5 条）。

## 4. `nav_history_10d` 的保留与计数

本节是全文**后果最重**的一节：它决定「已测得」这三个字还能不能成立。

### 4.1 持久化序列（冻结：一个字节不改）

| 项 | 冻结值 | 依据 |
|---|---|---|
| 元素 | `list[float]`，裸数值，**不是** `{date, nav}` 对象 | `float(history[-1])` / `float(nav_hist[0])` 的既有读法（`:1016`、`:1140` 附近） |
| 上限 | **10** | `NAV_10D_RECOVERY_WINDOW = 10`（`:121`，本次会话为固定字面量单独核对） |
| 溢出 | **丢最旧** `history[-10:]` | `:1022-1023` |
| 重复点 | **保留**，仅返回 `is_frozen_duplicate` 供调用方 WARN | `:1019-1021` |
| 顺序 | 严格时序（索引 0 最旧，末位最新） | 同上 |
| 去重 | **永不** | 去重即改写已发布的序列 |
| 日期戳 | **不新增** | 加日期会改变持久化形状（§0.2 第 3、5 条） |

**为什么必须承认「元素没有日期」这一事实**：序列里只有数，没有交易日标签。因此「有 10 个点」与「有 10 个交易日的观测」在数据结构上本来就不是同一件事，而今天的门禁把二者当成了同一件事（`:1136-1138` 只取 `len`）。本文不通过「给序列加日期」来修它（那会改持久化形状），而是通过**计数口径**来修它（§4.2）。

### 4.2 计数口径（冻结）

```
raw               = len(nav_history_10d)                   # 持久化条目数，含重复
unparseable_count = |{ v ∈ nav_history_10d : v 不可解析 / 非有限数 }|
parseable_count   = raw − unparseable_count
distinct          = |{ cents(v) : v ∈ nav_history_10d，v 可解析为有限数 }|
duplicate_count   = parseable_count − distinct
```

其中 `cents(x)` 与 §3.3 **逐字相同**（`Decimal(str(x))` → 0.01 → `ROUND_HALF_UP` → 整数分）。

**恒等式（可断言）**：

```
distinct + duplicate_count + unparseable_count == raw
distinct ≤ parseable_count ≤ raw ≤ 10 = NAV_10D_RECOVERY_WINDOW
duplicate_count ≥ 0
```

若观测到 `raw > 10`，说明序列被**外部改写**（写入路径不可能产出）：此时**不得**在计数侧截断，按原值报出——→ 理由：截断会替外部改写做无害化处理，从而让「账本被改过」这件事从报告里消失。

### 4.3 `distinct` 与 `raw`：谁决定状态

**决定（冻结）**：`nav_10d_status` **只**由 `distinct` 与最小门槛 `n` 决定；`raw` **只**作为证据字段随报告输出。

| 量 | 一行理由 |
|---|---|
| 为什么 `raw` 是错的 | `raw` 把同一观测被重复追加的**副本**当成新的交易日，这正是「冻结的表单值」把 10 日恢复伪造成已测得的那条最短路径。 |
| 为什么 `distinct` 是对的 | 在没有日期戳的前提下，按持久化精度（分）的**值唯一性**是「这是一次独立观测」唯一可计算的代理，且它的误差只朝**失败关闭**方向——两个不同交易日恰好按分相同只会让门槛**更难**通过，绝不会让它虚假通过。 |

**显式披露（双向误差，不许掩饰）**：`distinct` 会把「两个真实交易日 NAV 恰好按分相同」计为 1 个观测。在 2 位小数（约 1e-7 相对精度）上，这几乎只在**真实平盘**时发生；此时门槛确实会被压低，后果是「暂时测不出」，而不是「测出一个假数」。**这个方向的误差是我们想要的**。

### 4.4 最小样本门槛 `n`

| 项 | 冻结值 | 来源（三者互证） |
|---|---|---|
| `n` | **10** | ①字段名 `nav_history_10d`（10 日窗口）；②既有文案「`FAST-RECOVERY NAV UNAVAILABLE … （4/10 个交易日, 数据不足）`」（`summary.json` `health_stdout` L77）；③既有常量 `NAV_10D_RECOVERY_WINDOW = 10`（`scripts/check_live_portfolio_health.py:121`，本次会话为固定字面量单独核对） |
| 判定 | `nav_10d_status = "OK"` ⇔ `distinct >= 10` **且**起点 NAV（`nav_history_10d[0]`）可解析且 `> 0` | 起点非法是既有的第二条 `INSUFFICIENT_HISTORY` 通道（`:1152-1157`），保留 |
| 否则 | `nav_10d_status = "INSUFFICIENT_HISTORY"` | `:126-127` 的字面量，**值域不变** |

→ 理由（`n` 不重新取值）：`n` 就是窗口长度本身；把 `n` 调小等于允许用半段窗口宣称「10 日恢复」，那需要另一个任务与另一份文档，且必须先证明半段窗口的度量有意义。**改 `n` 须走 §9.1 修订。**

**必须写明的后果**：由于 `raw ≤ 10`，`distinct >= 10` 等价于「窗口内**一个重复都没有**」。因此**任意一个重复点都会让该度量在它老化出窗口之前不可测**（最多约 10 轮）。这是刻意的：→ 理由：另一条路（重复点照样算数）已经被证明会产出假测量（§7），而「暂时测不出」在本系统里是**失败关闭**——覆盖只可能被一个**正的、已测得的**恢复打开（`:1125-1131` 的既有设计意图）。

### 4.5 三态与状态码（`LV7` 验收 ②）

| 样本形态 | `raw` | `distinct` | `duplicate_count` | `nav_10d_status` | 今天的行为 |
|---|---|---|---|---|---|
| **4/10**：4 个点，四值互异 | 4 | 4 | 0 | `INSUFFICIENT_HISTORY` | `INSUFFICIENT_HISTORY`（`len<10`）——**不变** |
| **5/10**：10 个点，5 值互异（5 个重复） | 10 | 5 | 5 | `INSUFFICIENT_HISTORY` | **`OK`**（`len==10`）——**本次要改掉的那一格** |
| **10/10**：10 个点，十值互异 | 10 | 10 | 0 | `OK` | `OK`——**不变** |

**逐字冻结**：**一个 10 条目的序列若只有 4 个互异值，`nav_10d_status` 必须是 `INSUFFICIENT_HISTORY`，不是 `OK`、不是「有 10 个点所以已测得」。**

### 4.6 冻结序列**不得静默满足**门槛（缺陷陈述）

当上游表单 / 盯市值冻结时，10 个条目可以是**同一个数**的 10 份副本。此时今天的行为是：

1. `len(nav_hist) == 10` ⇒ 通过 `:1136-1138` 的长度检查；
2. `base_nav = nav_hist[0]` 合法 ⇒ `nav_10d_status = "OK"`；
3. `r_10d = current_nav / base_nav − 1 = 0`（首末同值）⇒ 报告出一个**精确 0.00% 的「10 日恢复」**。

这个 `0.00%` 不是观测，它是「同一个数除以它自己」。缺陷的本质不是数值错了，而是**一个从未被观测过的量被贴上了 `OK` 标签**——而下游只能靠 `nav_10d_status` 区分「测过」与「没测过」，它没有第二个信息源。

→ 因此本节的规则不是「更聪明的去重」，而是把 `OK` 的**准入条件**改成「至少 10 个互异观测」：**只要有一份副本，就承认没测到。**

## 5. 版本交叉校验（`LV7` 交付 ③）

### 5.1 什么必须与什么对齐、在哪个边界

| 项 | 冻结内容 |
|---|---|
| 左值 | 票据记录的 `strategy_version`（11-02 归档 L89 = `"1.2.2"`）与 `base_version`（L90 = `"1.7.0"`） |
| 右值 | `strategy_dumps/*.json` 中**同一策略**条目的对应版本字段（键名由实现阶段核对，见 §5.4-②；本文只冻结**关系**，不冻结 dump 的键名） |
| 配对键 | 「同一策略」由 Stage 2 本来就知道的策略标识确定（票据写出时必已知其策略名/ID）；**不做**跨策略的模糊匹配 |
| 边界 | **Stage 2 写出 `live_trading_ticket.csv` / `live_trading_ticket.json` 之前** |

**关系（冻结）**：同一策略、同一轴、**逐字相等**（字符串相等，不解析、不比较大小、不归一化 `v` 前缀）。

| 轴 | 左（票据） | 右（dump） | 缺失处置 |
|---|---|---|---|
| 策略版本 | `strategy_version` | dump 的对应字段 | 两侧都缺 ⇒ 该轴 `NOT_APPLICABLE`；**仅一侧缺 ⇒ 视为不一致** |
| 引擎版本 | `base_version` | dump 的对应字段 | 同上 |

→ 理由（仅一侧缺 ⇒ 不一致）：`BASE_VERSION` 今天只存在于 `BaseBlendAllocationStrategy`（AGENTS.md §7 明载），因此非 blend 策略本来就可能没有该轴；但「无法证明一致」不得读作「一致」，这正是本任务反复出现的同一类错误。

### 5.2 失败级别：**硬失败（fail-fast，拒绝出票）**

**决定**：任一轴不一致 ⇒ **非零退出，且不写出任何票据**（CSV 与 JSON 都不写、也不覆盖上一轮的票据文件）。

→ 理由：版本戳是票据**唯一的**「这是哪套逻辑、哪些参数算出来的数」凭据；不一致意味着无法把这张票据归因到任何一份可复核的 dump，而 `target_shares` / `delta_weight` 一旦发出就是不可撤销的市场指令——把不可撤销的风险建立在「日志里有一条警告」上是不成立的。

三条展开：

1. **失败的可见性差异**：告警只出现在 stderr/日志里，而票据文件本身看起来完全正常（有行、有数量、有价格）——读票据的人不会去看日志。拒绝出票则是**不可忽略**的失败。→ 理由：能被忽略的失败等于没有失败。
2. **补救成本极低且机械化**：重新生成 dump 是一条固定命令（`uv run python pipeline/research_strategy/run_research_strategy.py --dump-strategies`，AGENTS.md §7 已要求每次版本变更后执行）。→ 理由：把「一分钟可修的阻塞」与「不可撤销的错单」放在天平两端，选择是明确的。
3. **先例一致**：`LV11` 已确立「拿不到可信输入 ⇒ 拒绝出票，不得静默降级」的口径；版本戳属于同一类输入可信度问题。→ 理由：同一条链上的两处护栏不应对「不可信输入」给出不同级别的反应。

**明确接受的代价**：一次纯粹的版本号笔误（改了 `version` 忘了 dump）会**阻断当日出票**。→ 这是有意的：该状态本来就是「仓库自相矛盾」，此时任何出票都缺乏可复核来源。

### 5.3 失败载荷（结构化）

不一致时**不**往票据文件里写失败信息（那正是我们拒绝产出的文件），而是：

1. `stderr` 打**一行** JSON：`{"event": "VERSION_XCHECK_FAILED", "strategy": …, "ticket": {"strategy_version": …, "base_version": …}, "dump": {…}, "mismatched_axes": […], "dump_path": …}`；
2. 进程退出码非 0；
3. 磁盘上**不新增、不修改**任何票据工件。

成功时，`live_trading_ticket.json` 的 LV2 信封内新增 `version_check`（§6.4）。

### 5.4 实现阶段**第一步**：先核实 `verify_live_strategy_dump_preflight` 今天的行为

`verify_live_strategy_dump_preflight` 在 `scripts/run_live_strategy.py:1805` 被调用，但**它在版本不一致时是抛错还是只告警、是否覆盖两个轴、对「dump 里没有该策略」如何处理，本轮未核实**。因此：

| 核实结果 | 本条交付的量级 |
|---|---|
| **A**：已对**两个轴**做硬失败（抛错/非零退出） | 交付 ③ 降级为「**确认并文档化**」：补一条钉死该行为的回归用例（§8 G8），**不新写检查** |
| **B**：只告警 / 只比一个轴 / 缺 dump 条目不失败 / 只比票据侧自身 | 必须补成 §5.2 的硬失败，并在同一函数（或其后紧邻的一处）落地 |

→ 理由：冻结文档必须定义**要求**，而**代码量**取决于现场；把「先核实」写进契约，是为了禁止实现者在不了解现状的情况下另起一套并行检查（两套检查迟早对同一输入给出不同结论）。

**② 同批核实的第二项**：`strategy_dumps/*.json` 里承载两个版本的**实际键名**。本文 §5.1 只冻结「同一策略、同一轴、逐字相等」这一关系；实现时若 dump 键名与票据键名不同，映射方式必须写进当轮实现记录（不改本文）。

**③ 不产生版本变更**：本条只读版本号、只比较，**不**修改任何 `version` / `BASE_VERSION`，故不触发 `docs/version.md` 的变更义务。

## 6. 报告 / 输出契约

### 6.1 加性总则（先于字段）

1. **全部新增字段都是加性的**：**不得**重定义、重命名、或挪用任何既有字段的含义。
2. **唯一被收紧的既有字段**是 `nav_10d_status`：它的**值域不变**（`"OK"` / `"INSUFFICIENT_HISTORY"`，`:126-127`），但**选择谓词**从 `raw` 收紧为 `distinct`（§4.3）。这是一次**语义收紧**，必须由 §4.5 三态用例钉死，并靠 `nav_10d_counting_rule` 与历史归档区分（§6.3）。
3. **单一计算点**：`frozen_suspect` / `frozen_suspect_basis` 只在 §3.1 规定的那一处算；健康报告里的同名字段是**镜像**，不得重新推导。镜像与状态文件不一致即缺陷。

### 6.2 `account_state.json`（顶层，与 `prior_state` 同级）

| 键 | 类型 | 值域 | 语义 |
|---|---|---|---|
| `frozen_suspect` | `bool \| null` | `true` / `false` / `null` | §3.3 谓词的输出 |
| `frozen_suspect_basis` | `object` | 见下 | 支撑该判定的最小事实集 |
| `prior_state_age_days` | `int \| null` | `≥ 0` / `null` | `当日 as_of_date − prior_state.as_of_date` 的**日历日**差；`prior_state` 缺失时 `null` |

`frozen_suspect_basis` 的键（全部为事实，不含推断）：

| 键 | 类型 | 说明 |
|---|---|---|
| `prior_nav` | `float \| null` | `prior_state.current_nav` 原值 |
| `current_nav` | `float \| null` | 本轮实测有效净值原值（降级轮为 `null`） |
| `prior_nav_cents` / `current_nav_cents` | `int \| null` | §3.3 的 `cents()` 结果 |
| `prior_as_of_date` | `str \| null` | `prior_state.as_of_date` |

**`prior_state` 内不得出现**上述三个键（§2.3 F4）：前推时与本字段自身一并剥离。

→ 理由（剥离）：`frozen_suspect` 描述的是「某一对日期之间发生了什么」，若被整字典前推，昨天的 `true` 会变成今天 `prior_state` 里的 `true`，明天又会被下游读成「昨天也已经冻结」——一个只该存在一轮的判断会变成永久烙印。

**`prior_state_age_days` 的定位**：advisory + 响亮，**不是**门禁输入。正常隔夜为 `1`；`> 1` 表示中间夹了降级轮或缺失归档（`LV7` 计划行 2026-11-04 订正 ③ 的「年轻一个交易日」正是此形态）。→ 理由：把它做成硬失败会让一次数据修补变成当日停摆；而它的价值在于「让账本年轻几天」这件事第一次变成可读的数字。

### 6.3 `stage1_health_report.json`

新增键与既有 `nav_10d_status` **同处**（`nav_10d_status` 今天经 `dd_info` 输出，`:1204`——本次会话为固定落点单独核对）：

| 键 | 类型 | 语义 |
|---|---|---|
| `nav_10d_raw_count` | `int` | `raw`（§4.2） |
| `nav_10d_distinct_count` | `int` | `distinct`（§4.2） |
| `nav_10d_duplicate_count` | `int` | `parseable_count − distinct`（§4.2） |
| `nav_10d_unparseable_count` | `int` | 不可解析条目数 |
| `nav_10d_min_required` | `int` | `n = 10`（镜像常量，便于报告自证，不是新的可调参数） |
| `nav_10d_counting_rule` | `str` | 固定字面量 **`"distinct/v1"`** |
| `frozen_suspect` / `prior_state_age_days` | 同 §6.2 | **镜像**（单一计算点，§6.1-3） |

**`nav_10d_counting_rule` 的存在理由**：历史归档里的 `"OK"` 是**按 `raw` 判定**的，与本文生效后的 `"OK"`（按 `distinct`）不是同一个断言。→ 理由：没有这个标记，任何人回看旧报告都会把旧的 `OK` 读成「10 个互异观测」，而这个字段**只在新规则下出现**，缺省即可判为 `raw/v0`。

### 6.4 `live_trading_ticket.json`（LV2 信封）

| 键 | 类型 | 值域 |
|---|---|---|
| `version_check.status` | `str` | `"MATCH"` / `"NOT_APPLICABLE"`（仅当两侧都缺该轴） |
| `version_check.strategy_version` | `object` | `{"ticket": str, "dump": str}` |
| `version_check.base_version` | `object` | 同上 |

失败态**不**出现在此文件中（§5.3）。→ 理由：一张「拒绝出票」的运行不应留下一个看起来像票据的工件。

## 7. 逐例推演（构造示例，算术精确）

> 本节是**构造的**：标的无关，日期与数值为让两种情形可逐一对照而设定。`prior_state.as_of_date = 2026-11-03`，当日 `as_of_date = 2026-11-04`，`prior_state.current_nav = 199935.46`（=`19 993 546` 分）。

### 7.1 场景 A：冻结序列（前一日 = 当日）

当日实测有效净值 = **199935.46**。序列（10 条，索引 0 最旧，末位为本日追加）：

| 索引 | 值 | 分整 |
|---|---|---|
| 0 | 199595.40 | 19 959 540 |
| 1 | 200658.01 | 20 065 801 |
| 2 | 197351.17 | 19 735 117 |
| 3 | 199935.46 | 19 993 546 |
| 4 | 199935.46 | 19 993 546 |
| 5 | 199935.46 | 19 993 546 |
| 6 | 199935.46 | 19 993 546 |
| 7 | 199935.46 | 19 993 546 |
| 8 | 199935.46 | 19 993 546 |
| 9 | 199935.46 | 19 993 546 |

| 量 | 本文口径 | 今日代码 |
|---|---|---|
| `raw` | **10** | `len = 10` |
| `distinct` | **4**（{19 959 540, 20 065 801, 19 735 117, 19 993 546}） | 不计算 |
| `duplicate_count` | **6** | 仅 1 个点级 `is_frozen_duplicate=true`（末位与上一点相同） |
| `frozen_suspect` | **`true`**（`cents(199935.46) == cents(199935.46)`） | **字段不存在** |
| `nav_10d_status` | **`INSUFFICIENT_HISTORY`**（`4 < 10`） | **`OK`**（`10 >= 10`） |
| `r_10d` | 不产出（未测得） | `19 993 546 / 19 959 540 − 1 = 34 006 / 19 959 540 = 0.001703747…` ⇒ **+0.170375%** |

**今天代码在场景 A 的实际结论**：它把一个**由 4 个观测**构成的序列报成「10 日恢复已测得，+0.17%」。这个 `+0.17%` 完全由最早那一个点（`199595.40`）与同一份冻结值的差决定——序列越往后越是同一个数，读数却越像「测过了」。

### 7.2 场景 B：真实的小幅变动（+10.01）

当日实测有效净值 = **199945.47**（=`19 994 547` 分），其余 9 条与场景 A 相同。

| 量 | 本文口径 | 今日代码 |
|---|---|---|
| `raw` | **10** | `len = 10` |
| `distinct` | **5**（新增 19 994 547） | 不计算 |
| `duplicate_count` | **5** | 点级 `is_frozen_duplicate=false`（末位与上一点不同） |
| `frozen_suspect` | **`false`**（19 993 546 ≠ 19 994 547） | **字段不存在** |
| `nav_10d_status` | **`INSUFFICIENT_HISTORY`**（`5 < 10`） | **`OK`** |
| `r_10d` | 不产出 | `19 994 547 / 19 959 540 − 1 = 35 007 / 19 959 540 = 0.001753898…` ⇒ **+0.175390%** |

**这张表是 §3.4 / §3.6 的直接证据**：场景 B 与场景 A 只差 **10.01 元**（0.005%），`frozen_suspect` 必须是 `false`——相对容差若取到 1e-4（约 20 元），就会把这一变动判成冻结；按分相等则不会。

### 7.3 `LV7` 验收 ② 的三态（同一序列族，只改条目数与互异值数）

| 用例 | `raw` | `distinct` | `nav_10d_status` | 与今日代码的差异 |
|---|---|---|---|---|
| 4/10 | 4 | 4 | `INSUFFICIENT_HISTORY` | 无（今日也是 `INSUFFICIENT_HISTORY`） |
| 5/10 | 10 | 5 | `INSUFFICIENT_HISTORY` | **有**：今日为 `OK` ← §4.6 的缺陷 |
| 10/10 | 10 | 10 | `OK` | 无 |

→ 三行都必须逐字断言（§8 G5/G6）。`10/10` 一行同时说明：本文**没有**把门禁变得更严到不可达——只要 10 个观测都是互异的，`OK` 照旧可达。

### 7.4 版本核对（构造）

| 项 | 票据 | dump | 结论 |
|---|---|---|---|
| `strategy_version` | `"1.2.2"` | `"1.2.2"` | 一致 |
| `base_version` | `"1.7.0"` | `"1.8.0"` | **不一致** ⇒ 硬失败、不出票、退出码非 0 |
| `base_version` | 缺失 | 缺失 | `NOT_APPLICABLE`（两侧都缺） |
| `base_version` | 缺失 | `"1.8.0"` | **不一致**（无法证明一致 ≠ 一致） |

> 本表**不**断言 11-02 归档与当前 `strategy_dumps` 是否一致（未核实），只冻结判定规则。

## 8. 验收映射与不可删除的断言

### 8.1 `LV7` 四条验收 → 本文

| `LV7` 验收条目（计划行原意） | 本文对应章节 | 落地时如何被消费 |
|---|---|---|
| ① 合成「前一日 = 当日」的冻结序列 ⇒ `frozen_suspect` 命中 | §2.1/§2.2（语义）+ §3.3（谓词）+ §3.4（按分相等）+ §7.1 | 断言 `frozen_suspect is True` 且 `frozen_suspect_basis` 两侧分整相等；把谓词改成 `false` 兜底 ⇒ 转红 |
| ② 样本 4/10、5/10、10/10 三态各一条，状态码精确 | §4.2（口径）+ §4.4（`n`）+ §4.5（三态表）+ §7.3 | 三条独立用例逐字断言 `INSUFFICIENT_HISTORY` / `INSUFFICIENT_HISTORY` / `OK`；5/10 一格是主用例 |
| ③ 重复末点 ⇒ 去重后计数而非当成两个交易日 | §4.2（`distinct` 定义）+ §4.3（为何 distinct）+ §4.6 + §7.1 | 断言 `raw=10 ∧ distinct=5`（或 4）时 `nav_10d_status != "OK"`；改回按 `len` 计数 ⇒ 转红 |
| ④ 版本号不一致 ⇒ 报错；删任一判据即转红 | §5.1（关系）+ §5.2（硬失败）+ §5.3（载荷）+ §5.4（先核实现状） | 断言非零退出且**未产生**票据文件；删掉任一轴或改成告警 ⇒ 转红 |

### 8.2 不可删除的断言清单（对应验收 ④）

| # | 断言 |
|---|---|
| **G1** | `prior_state` 与当日同值（按分）⇒ `frozen_suspect is True`；**不得** `False`、**不得** `None` |
| **G2** | `prior_state` 缺失 ⇒ `frozen_suspect is None` 且不抛异常；**不得** `False`（不伪造阴性）、**不得** `True` |
| **G3** | 降级分支 ⇒ `frozen_suspect is None` 且该键**存在**；`current_nav` / `nav_history_10d` 不被写（沿用计划 §4.30 第 7 条） |
| **G4** | 同一 `as_of_date` 连跑两次 ⇒ `nav_history_10d` 长度与逐元素内容不变，`frozen_suspect` / `frozen_suspect_basis` 逐字段相同 |
| **G5** | `raw=10 ∧ distinct=5` ⇒ `INSUFFICIENT_HISTORY`（**不得** `OK`） |
| **G6** | `raw=4` ⇒ `INSUFFICIENT_HISTORY`；`raw=10 ∧ distinct=10` ⇒ `OK` |
| **G7** | 恒等式 `distinct + duplicate_count + unparseable_count == raw` 且 `distinct ≤ raw ≤ 10` 恒成立（含 `raw > 10` 的外部改写输入，此时**不截断**） |
| **G8** | 任一版本轴不一致 ⇒ 非零退出且磁盘上**未新增/未修改**任何票据文件；`version_check` 不出现于失败态 |
| **G9** | `append_nav_history_point` 的重复保留与丢最旧行为**逐字不变**：相同输入 ⇒ 逐元素相同输出（含重复点） |

→ 每条断言都指向同一类缺陷：**「未观测」被写成了「已观测的某个值」**。G1/G2/G3 防「自我确认 / 未测得 → 否」，G5/G6/G7 防「副本 → 已测得」，G4 防「复跑 → 多一个交易日」，G8 防「不可信版本 → 照常出票」，G9 反向钉死「修计数不许顺手改序列」。

## 9. 变更控制与 SSOT 归属

### 9.1 修订流程（append-only）

1. 本文正文（§0–§9 冻结文本）**一经发布不可改写**：不修正字段名、不「顺手补一句」、不删除任何段落。
2. **任何**变更——包括「把 `n` 从 10 改成别的值」「把按分相等改成容差」「把版本校验从硬失败改成告警」——**只**能以文末新段落落地：

```
## Revision YYYY-MM-DD

- **变更类型**：新增字段 / 收紧谓词 / 放宽必需性 / 修正错误 / 废弃字段 / 调整门槛 n
- **动机**：实测证据或验收失败的具体形态（不得只写「优化」）
- **字段/谓词 diff**：旧 → 新，逐字段或逐谓词原文
- **兼容性**：旧 `account_state.json` / 旧 `stage1_health_report.json` 是否仍可解析；不可解析时给出迁移规则
- **影响章节**：§x.y 被覆盖的具体条款
- **验证**：受影响的断言编号（§8.2）与用例
```

3. 冲突时**修订段覆盖冻结文本**，冻结文本字面保留，便于追溯「当时为什么这么定」。
4. 修订段本身同样 append-only：不允许二次编辑旧修订段，只能追加新段并显式标记前者作废。
5. **禁止的顺序**：先写代码、再倒推本文。发现本文缺字段或语义冲突时，**先**走本节修订，**再**改实现（`LV7` 计划行第 1 条的原文要求）。

### 9.2 SSOT 归属（不得混用）

| 内容 | SSOT |
|---|---|
| `prior_state` 语义、`frozen_suspect` 谓词、`nav_history_10d` 计数口径与门槛 `n`、版本交叉校验要求、新增字段名 | **本文**（`docs/prior_state_nav_history_schema.md`） |
| `LV7` 的任务状态、`[ ]`/`[x]`、优先级、验收进度、执行次序 | [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV7` 行 |
| 估值 / 分档 / 熔断阈值 | `LV1` / `LV12`（本文只读取其结果） |

- 本文**不**维护状态；计划表**不**再复述谓词与口径（只链接本文）。

### 9.3 冻结签署

| 项 | 值 |
|---|---|
| 冻结版本 | `prior_state/v1` + `nav_history_10d_counting/v1` + `version_xcheck/v1` |
| 冻结日期 | **2026-11-05** |
| 依据诊断 | §1（`prior_state` 两个写入点 `execution_service.py:2364-2367` / `:2383-2386`、`build_account_state_update` `:784` / `:749`、`main()` `:2366-2384`、`_carry_forward_account_state` `storage.py:2057`、首次运行 `deepcopy` + `pop` `:2380-2384`、`append_nav_history_point` `:984-1024`、`len` 门禁 `:1136-1138`、`INSUFFICIENT_HISTORY` `:1152-1157`；全仓无 `frozen_suspect`） |
| 本文择定的分支 | 相等性取**按分（整数分）相等**，不取 `isclose`（§3.4）；计数取 **`distinct`**，不取 `raw`（§4.3）；版本不一致取**硬失败**，不取告警（§5.2） |
| 未核实项（实现阶段第一步） | `verify_live_strategy_dump_preflight`（`run_live_strategy.py:1805`）今天是否已抛错、是否覆盖两轴（§5.4）；`strategy_dumps` 的版本键名（§5.4-②） |
| 下一交付物 | `LV7` 计划行第 3 条：`prior_state` 前推 + `frozen_suspect` 标记 + `nav_10d_status` 按 `distinct` + 版本交叉校验，配套新增 `pipeline/tests/test_account_state_rollforward.py`（全离线，**禁止**读 `data/runs/**`）。**在本文冻结之后**方可开始 |

---

**文档结束（冻结文本 §0–§9）**。此后任何修订**只**以 `## Revision YYYY-MM-DD` 段追加于本节之后（§9.1），不允许在冻结文本内就地修改；`LV7` 的任务状态与 `[ ]`/`[x]` 一律回 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 查询，本文不维护状态。
