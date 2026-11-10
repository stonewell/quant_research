# `prior_state` 前推语义、`nav_history_10d` 最小样本门槛与版本元数据交叉校验（冻结版 v1）

> **文档性质**：**冻结的接口契约（frozen contract）**，不是实施记录，也不是设计探讨。本文定义 Stage 1 / Stage 2 之间 `prior_state` 的**可用性判据**、`nav_history_10d` 的**最小样本门槛与状态码词表**、以及票据/状态文件的**版本元数据交叉校验**。实现、调用方与后续审计必须**消费**本文，不得反过来由实现倒推口径。
> **冻结日期**：**2026-11-06**。契约版本号：`prior_state/v1` + `nav10d/v1` + `live_version_parity/v1`。
> **状态**：**FROZEN**。本文是 `LV7`（`prior_state` 前推、版本元数据与 `nav_history_10d` 截断）的冻结文档；本文冻结的五条决定即实施依据，实施侧不得另行解释。
> **任务状态 SSOT**：[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.19 `LV7` 行是任务状态、优先级与验收的唯一权威；本文只维护**判据、状态码词表与留痕字段**这一项 SSOT。本文**不维护**任务状态。
> **修订方式**：本文正文一经冻结即**不可改写**；任何变更以 **append-only** 的 `## Revision YYYY-MM-DD` 追加段落在文末落地（见 §7）。冻结文本 + 全部修订段共同构成现行契约，冲突时**修订段覆盖冻结文本**，但冻结文本的字面保留以便追溯。

## 0. 冻结范围、非目标与判断口径

### 0.1 本文冻结什么

| 项 | 冻结内容 | 章节 |
|---|---|---|
| `prior_state` 语义 | 「上一交易日的**已确认**快照」，不是「上一次运行的状态」；选择逻辑**已正确**，本文只做**校验** | §1 |
| 冻结态标记 | `prior_state.current_nav` 与当日 NAV **精确相等** ⇒ 落 `prior_state_frozen_suspect: true` + 原因串；否则**键不存在**（不是 `false`） | §2 |
| `nav_history_10d` 门槛 | **去重后相邻不同值计数** `>= 10` 且起点可解析 `> 0` 才可计算；否则只出状态码，**禁止**补位/凑数/当 0 | §3 |
| 状态码词表 | 只有 `OK` 与 `INSUFFICIENT_HISTORY`；**任何状态码都不得被读成 0** | §4 |
| 版本元数据 | dump ↔ 活类、carried `account_state` ↔ 活类，两处交叉校验，**失败关闭**、**不自动改写** | §5 |
| 非目标 | 不重写选择逻辑、不改门禁阈值、不改估值口径、不改归档、**零版本轴变动** | §0.2 |

### 0.2 非目标（本文明确**不**改变什么）

1. **不重写账户状态的选择逻辑**。`web/livetrading/storage.py::latest_account_state_at_or_before`（`:2731-2790`，回退遍历本体在 `:2779`）的实例排序问题**已由排序修复解决**（见 §6 的订正 ①）。本文**只**在它选出的结果上加**校验与标记**，不重新实现、不改选谁。
2. **不改门禁阈值**。`dd_reduce_thresh` / `dd_defensive_thresh` / `dd_stop_thresh` / cooldown bars 全部原样（阈值归 `LV1`）。
3. **不改估值口径**。`nav_effective` 的来源、`resolve_gate_nav_context` 的 MTM 优先规则、`DEGRADED_NAV_UNAVAILABLE` 的失败关闭语义全部原样。
4. **不改写任何已归档 `data/runs/**`**。归档不可变；本文约定的标记只对**本次新写入**生效。
5. **不碰版本轴**。**本任务改动零个 `version` / `BASE_VERSION` 字符串**，不触发 `docs/version.md` 变更，**不跑** `--dump-strategies`，不改 `pipeline/research/blend/*`、`pipeline/research_strategy/rs/*`、`common/allocation_templates.py` 的任何一行。版本交叉校验是**只读比对**，绝不以"重新盖章"修复不一致（见 §5.4）。
6. **不引入前向填充**。本文不涉及任何 weights 帧的 `ffill()`；`nav_history_10d` 的去重是**计数**，不是对序列做去重改写（序列本身一字不动）。

## 1. 冻结决定 1：`prior_state` 只表示「上一交易日的已确认快照」

**冻结决定 1（逐字）**：`prior_state` 是**上一交易日**的**已确认**快照，不是「上一次运行的状态」。选择逻辑（`latest_account_state_at_or_before`）按订正 ① 已经正确，**不得重开或重写**；本任务新增的是**校验**，不是**重选**。

### 1.1 为什么"选得对"仍然不够

选择逻辑修好之后，「选到哪一份」不再出错；但「选到的那一份**本身是不是一份新的结算**」没有被任何一层检查过。2026-11-02 的归档事实是：顶层 `as_of_date` 与 `prior_state.as_of_date` **相差一天**（正确），而两侧的 `current_nav` **同值**（`prior_state.current_nav` = 199935.46 = 顶层值）。也就是说：**日期在前进，净值在复制**。一旦上游表单值冻结，整条链会**自我确认** —— 今天读到"昨天"的 NAV 恰好等于今天，于是加仓/减仓、tenure lock、`[持仓N日]` 标签、HWM 与 10 日恢复度量全部建立在一个**没有前进的账本**上。

订正 ③ 进一步指出剩余缺口比"缺 `as_of_date` 过滤"更宽：Stage 1 会把顶层 `as_of_date` **重新盖上今天**、同时把嵌套 `prior_state` 原样带出（写回点 `:742` / `:772`），故一份过期状态进入流水线后其**顶层**日期会被改写，而嵌套块仍是旧的；Stage 2 只比较**顶层**日期（`run_live_strategy.py` `:1871`），随后从嵌套块**整块替换** `entry_signal_streak` / `zero_target_streak` / `entry_prices` / `last_exit_date` / `lifecycle_states`。因此"日期对不对"不足以证明"账本前进了没有"——**必须**再核对**净值是否真的变了**。

**结论（本决定的落地口径）**：把"净值与当日完全相同"当作**可观测的冻结嫌疑**记录在案，而不是当作"今日无变化"。这是**注释**，不是**修正**。

## 2. 冻结决定 2：冻结态判定 = 当日与 `prior_state` 的 NAV **精确相等**

**冻结决定 2（逐字）**：若 `prior_state.current_nav ==` 当日使用的 NAV（对 NAV 值**精确相等**），则落 `prior_state_frozen_suspect: true` **外加**一个人类可读 `reason` 字符串；否则该键**不存在**（载荷里**从不**写 `false` —— 缺失即为干净，旧消费方看不到新键）。**绝不**改写 NAV 数字本身；这**只是**注释。

### 2.1 判定输入

* 左值：被保留下来的那份 `prior_state`（Stage 1 里是 `prior_state_to_preserve`）的 `current_nav`。
* 右值：**今日真正参与门禁算术的净值** `nav_resolution["nav_gate_nav"]`（MTM）。降级运行下该值为 `None`（只用于显示的表单占位值**不**参与本比较），此时**不**落标记 —— 没有测得任何净值，就不能声称"和昨天一样"。
* 两侧任一缺失 / 不可解析（`None`、空串、非数值字符串）⇒ **不落标记**。这不是"干净"，而是"无法判定"：本标记只表达**可证实的**冻结嫌疑。

### 2.2 为什么是"精确相等"而不是容差

容差会立刻把"小幅真实波动"也标成嫌疑，标记就会变成噪声进而被忽略。冻结值的形态是**逐位复制**：两个 `float` 由同一个十进制字面量解析而来时**逐位相等**。因此判据是 `float(a) == float(b)`，**不**引入 `approx`、**不**引入百分比阈值。

### 2.3 落盘位置与形状

* 字段在 `account_state.json` 的**顶层**，与 `prior_state` 并列（`build_account_state_update` 的**两个分支**都要落：降级分支与测得分支 —— 冻结态**不得**躲在"绿色报告"后面）。
* 形状：`{"prior_state_frozen_suspect": true, "prior_state_frozen_reason": "<人类可读原因>"}`；干净时**两个键都不出现**。
* 同时打印一条 `[WARN] (LV7)` 控制台告警，点名"前一日 NAV 与当日完全相同"。

## 3. 冻结决定 3：`nav_history_10d` 的最小样本门槛 = **去重后相邻不同值** `>= 10`

**冻结决定 3（逐字）**：10 日 NAV 统计量**只**在**去重后相邻不同值**计数 `>= 10` **且**起点值可解析为 `> 0` 时才可计算；否则输出 `nav_10d_status: "INSUFFICIENT_HISTORY"` 且**不得**输出数值 `nav_10d`（实现中的字段名是 `nav_10d_return`）。**绝不**补位、**绝不**把尾值重复到 10 个、**绝不**把缺失历史当作 0 收益。**相邻重复**（与前一个点同值）只计**一次**。

### 3.1 为什么必须去重计数

归档事实：`nav_history_10d` 只有 5 个原始点，而**末两点同为 199935.46**（`L12` / `L13`）；Stage 1 自己在 `summary.json` `health_stdout` `L77` 里同时报了 `[WARN] nav_history_10d 出现重复点` 与 `FAST-RECOVERY NAV UNAVAILABLE … （4/10 个交易日, 数据不足）`。也就是说：**原始长度**与**真正不同交易日的个数**是两个数，而"能否测量"取决于**后者**。把 5 个原始点当成 5 个交易日，会让一条冻结的平坦序列在攒够 10 条重复之后**看起来**达到了门槛 —— 这是"重复点补位"最危险的形态：不需要任何人主动补数，冻结自己就会凑满 10 个。

### 3.2 计数规则（唯一形式）

`count_distinct_nav_points(nav_history)`：

1. 逐点解析为 `float`；不可解析的点**跳过**（不计入分母，也不让整条序列崩溃）。
2. 只有与**紧邻前一个已计数的值**不同才 `+1`；相同则**不再计数**（相邻重复算一次）。
3. 非相邻的同值（A, B, A）算**两次** —— 它是两个真实的观测点，去重只针对**连续的复制**。
4. 计数**只**用于"能否测量"的判断；**不**改写 `nav_history_10d` 序列本身（写入侧仍按既有规则保留平坦点并告警，见 `append_nav_history_point`）。

### 3.3 与既有字段的关系（向后兼容）

| 字段 | 含义 | 变动 |
|---|---|---|
| `nav_10d_history_len` | `nav_history_10d` 的**原始条数** | **不变**（旧消费方逐字节可读） |
| `nav_10d_distinct_len` | **去重后相邻不同值**个数（本决定的判据） | **新增** |
| `nav_10d_required_len` | 门槛（10） | 不变 |
| `nav_10d_status` | 词表见 §4 | 不变（但**判定输入**改为 distinct） |
| `nav_10d_return` | 数值或 `null` | 不足时**必须**是 `null` |

人类可读渲染里的 `n/10 个交易日` 采用**去重后**计数（必要时回落到原始条数），因为"你手上有几个不同交易日的净值"才是运维真正需要知道的量；JSON 里两个数都在，审计不必依赖渲染。

## 4. 冻结决定 5：状态码词表 —— **任何状态码都不得被读成 0**

**冻结决定 5（逐字）**：已知良好状态词表必须在本文显式列出：有哪些码（`INSUFFICIENT_HISTORY`），各是什么意思，以及**任何码都不得被解释为 0**。

| 状态码 | 含义 | 数值字段 `nav_10d_return` | 快速恢复覆盖 | 读者**不得**做的推断 |
|---|---|---|---|---|
| `OK` | 历史满足门槛且起点 `> 0`，`nav_10d_return` 是**真测量值** | 数值（可为负、可为零 —— 零是**测出来的**平坦） | 仅在 `nav_10d_return > 0` 时为 `True` | 不得把 `OK` 当作"覆盖已触发" |
| `INSUFFICIENT_HISTORY` | 历史**不足**或起点不可用/非正，**未测得** | **`null`** | **恒为 `False`**（缺数据永不打开覆盖） | **不得**当作 `0.0`、`0.00%`、"持平"、"未触发"或 PASS |

词表**只有这两项**。`INSUFFICIENT_HISTORY` 是"未测得"的**唯一**表达方式；"未测得"与"测得为 0"是两个不同事实，任何把前者渲染成后者的路径都是缺陷（2026-10-30 事故的成因即此）。状态码**从不**参与熔断层级 / `linear_equity_scale` 的算术（见 `test_nav_10d_status_never_changes_tier_or_linear_scale`）。

## 5. 冻结决定 4：版本元数据交叉校验 —— **只读、失败关闭、不自动修**

**冻结决定 4（逐字）**：持久化 dump 中记录的 `strategy_version` / `base_version` 必须等于活类自身的 `version` / `base_version`；任何不一致（dump vs 活类，或 carried `account_state` 与今日活类不符）都必须在**任何票据写出之前**抛出**专用异常**（`LiveDumpPreflightError`）。**绝不**降级为警告；**绝不**通过重新盖章自动修复。

### 5.1 为什么"重新盖章"是缺陷而不是修复

`resolve_strategy_definition` 今天取版本用的是 `getattr(template, "version", None) or strategy_def.get("strategy_version")`：**活类优先**，落盘时再把结果**覆盖**回 `strategy_def["strategy_version"]`。于是"dump 说 1.2.2、活类说 1.3.0"这种不一致**永远不会被看见** —— 它会被活类的值盖掉，票据上印出一个与 dump 无关的数字。这正是 2026-11-02 归档里"票据 `strategy_version` 1.2.2 / `base_version` 1.7.0 与 dump 并存两套"的可观测形态：**没有一处比对**，所以两套值可以长期共存。本决定把"取谁"变成"先比，再取"。

### 5.2 轴一：dump ↔ 活类（Stage 2 解析期）

* 比对点：`resolve_strategy_definition` 覆盖 `strategy_def["strategy_version"]` / `["base_version"]` **之前**。
* 左值（记录值）：**加载自持久化 dump 时**该 JSON 里的 `strategy_version` / `base_version`。
* 右值（活值）：`getattr(template, "version", None)` / `getattr(template, "base_version", None)`。
* **键缺失 ⇒ 不比**：键**不存在**意味着"这份载体没有断言任何版本"，此时票据采用活类的值不构成**虚假陈述**，因此不报错（否则 `strategies_config.json` 合成路径与全部不带版本键的历史 dump 都会被误杀）。
* **键存在但为空**（`null` / `""` / 纯空白）而活值非空 ⇒ **比** ⇒ 报错。一份**显式**携带版本字段却不给出值的 dump 是畸形记录，不是"没断言"。
  * 例外（载体约定，不是放宽）：`base_version` 的 `null` 是 dumper 对**非 blend 策略**的**规范写法**（真实 dump 中 51/58 份如此），因此 `null` 与活类的 `None` **相等**；只有"一侧有值、另一侧为空"才算不一致。
* 任一侧不一致 ⇒ `LiveDumpPreflightError`，消息点名**两个取值**与**dump 路径**。

### 5.3 轴二：carried `account_state` ↔ 今日活类（Stage 2 持久化期）

* 左值：读入的 `account_state.json`（含 carried 前推状态）顶层 `strategy_version` / `base_version`。
* 右值：今日活类（`getattr(template, "version")` / `("base_version")`）。
* 规则同 §5.2（缺失即不比、存在但为空且活值非空即错、两侧有值必须相等）。
* **执行两次，同一判据**：①在票据构造**之前**（`account_state` 读入且活类版本已知之后）—— 保证"不一致 ⇒ 一张票都不出"；②在写回 `account_state.json` **之前** —— 期间状态对象被大量改写，写盘前再断言一次是防御纵深，不是重复劳动。

### 5.4 失败关闭的运维后果（必须写清楚，不许悄悄软化）

本决定**没有**自动修复路径：引擎**不会**把 dump 的版本改成活类的值，也**不会**把旧 `account_state` 的版本"顺便刷新"。版本升级后若 carried 状态仍带旧 `base_version`（例如归档里的 1.7.0 vs 活类 1.8.0），**下一次 Stage 2 将拒绝出票**。这是**有意的**：一份"上一次由别的引擎算出来的"账本，不能因为今晚的引擎更新了就自动获得信任。

允许的解除方式（**都必须在审计留痕中显式记录**，且都不是引擎自动执行）：

1. 由操作者**显式**提供一个已更新的状态文件（`--account-state-file` 指向新状态）；
2. 由操作者在**明确知道**"这是同一策略的合法版本推进"的前提下，手工更新状态的版本字段，并在运行记录中写明依据；
3. 回滚到与状态匹配的 dump / 引擎版本。

### 5.5 为什么异常类型复用 `LiveDumpPreflightError`

`scripts/live_preflight.py` 已经是三条实盘入口**共用的**"出票前体检"模块，异常类型已在 `main()` 中抛出并被调用方认知。版本不一致与权重模式漂移属于**同一类**失败（"今晚这张票读的不是我以为的那台引擎"），因此复用同一异常类型，而不是新增第二个近义异常。该校验**不可**被 `--allow-weighting-mode-override` 放行 —— 那个逃生口只覆盖权重模式钉住值，不覆盖版本轴。

### 5.6 2026-11-09 孤儿种子事件与两处新增护栏（实现记录）

**现象**：2026-11-09 首跑 Stage 2 以 `base_version` `1.8.0` vs 活类 `2.0.0` 拒绝出票（`LiveDumpPreflightError`）。引擎行为正确，问题在"它读到的是哪一份账本"。

**根因一（孤儿实例）**：`web/livetrading/execution_service.py::run_health_check` 在启动 Stage-1 子进程**之前**就已分配 `<date>/<HHMMSS>/` 并把 `latest_account_state_at_or_before` 的结果拷贝成 `<run_dir>/account_state.json`；而显式 `--as-of-date 2026-11-08`（周日，非交易日）只由子进程的 `common/cli_utils.resolve_live_clock_context` 拒绝。于是留下 `2026-11-08/184755/`：一份 11-05 的账本（`base_version=1.8.0`）加一份 `health_status: "FAILED"` 的 `summary.json`。

**根因二（选种只看目录新旧）**：`web/livetrading/storage.py::latest_account_state_at_or_before` 只按"日期降序 → 当日实例降序 → 第一份存在且非空的 `account_state.json`"返回，既不校验实例是否跑成，也不校验状态自身的 `as_of_date`。该孤儿（11-08）因此压过持有正确 `2.0.0` 账本的 11-06 目录，§5.4 第 2 条的一次手工修正被架空——"只更新一次"只在修正对象恰好是下一次被选中的种子时成立。

**修改（不含任何版本轴改动）**：

1. `web/livetrading/execution_service.py::_assert_explicit_as_of_is_trading_session`：仅在操作者显式指定 as-of 时校验其是否为交易日；在 `run_health_check` 与 `run_live_deploy` 中均于 `get_run_dir` / `_allocate_stage1_run_dir` / `os.makedirs` / 账本播种**之前**调用，拒绝时归档不被改动。
2. `web/livetrading/storage.py::_instance_state_is_trustworthy`：`latest_account_state_at_or_before` 跳过 `health_status` 存在且非 `SUCCESS`、或仅有 `error_kind` / `error_headline` 的实例。无 `summary.json` 的旧归档照常作种子；`health_status: "SUCCESS"` 而 `deploy_status: "FAILED"` 的实例照常作种子（该轮仍拥有需要延续的生命周期状态）。

**不变式**：本改动只改变"哪一份实例被选作种子"，不改变版本盖章。§5.4 的 fail-closed 契约、`assert_account_state_version_parity` 与 `assert_dump_version_parity` 均未改动；错版本账本若仍到达 Stage 2，依旧拒绝出票。

**残留缺口**：`run_multi_strategy_netted_deploy` 尚未接入同一闸门（矩阵路径不参与按 `(strategy, universe)` 的前推链、不播种 `account_state.json`，故不构成同类版本风险，只会留下一个无效归档条目）；跨市场组合只校验主市场日历，次市场休市仍由子进程拒绝（孤儿由上面的过滤器兜住）。

**验证**：`web/livetrading/tests/test_account_state_seed_selection.py`（11 条离线用例，含 AST 级顺序守卫与"本过滤不得引入版本比较"的结构守卫）；`cd web/livetrading && .venv/bin/python -m pytest tests -q` → 285 passed, 1 skipped.

## 6. 归档事实与订正原文（逐字引用）

`LV7` 行的三处订正原文如下（引自 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) `LV7` 行，**逐字**，不改写）：

**订正 ①（该机制现已「完全解决」，不是「部分前置解决」）**
> **（订正 ①：该机制现已「完全解决」，不是「部分前置解决」。）**`latest_account_state_at_or_before`（`web/livetrading/storage.py` `:2731-2790`，回退遍历本体在 `:2779`）**自身没有任何排序**，顺序完全继承 `list_instances_for_date` 的 `_instance_sort_key`，故排序改动**能够**到达它。具体到 `2026-11-03` 容器：`210229` 的 `summary.json` `timestamp` = `2026-11-03T05:02:29Z`、`205541` = `2026-11-04T04:58:18Z` ⇒ 升序 `[flat, 210229, 205541]` ⇒ `reversed()` ⇒ `[205541, 210229, flat]` ⇒ **`205541` 胜出**（`2026-11-03/` 顶层 flat 目录只有 `holdings.json` / `operator_holdings.json`，**无** `account_state.json`，反转后居末）。因此该实例不再复现。

**订正 ②（初稿 `holding_bars` 引错了账本）**
> **（订正 ②：初稿 `holding_bars` 引错了账本。）**初稿写的 `000999:6` / `600660:22` / `601728:21` / `601919:2` 是 `205541` **自己**的真值（11-03 账本），不是被服务的那份；实际 `prior_state` 带的是 **11-02** 的值：`holding_bars` = `{000157:0, 000999:5, 600660:21, 600900:5, 601728:20, 601919:1}`，`as_of_date: 2026-11-02`。

**订正 ③（剩余缺口比「缺 `as_of_date` 过滤」更宽）**
> **（订正 ③：剩余缺口比「缺 `as_of_date` 过滤」更宽 —— 是「全链路没有『期望的上一交易日』不变量」，且朴素地只在顶层加过滤会被绕过。）**①Stage 1 会把顶层 `as_of_date` **重新盖上今天**、同时把嵌套 `prior_state` 原样带出（`scripts/check_live_portfolio_health.py` `:2349-2371` 的 `baseline_state = copy.deepcopy(state)` / `prior_state_to_preserve`；写回点 `:742` / `:772`），故一份过期状态进入流水线后其顶层日期会被改写；②Stage 2 只比较**顶层**日期（`scripts/run_live_strategy.py` `:1871`），随后从嵌套块**整块替换** `entry_signal_streak` / `zero_target_streak` / `entry_prices` / `last_exit_date` / `lifecycle_states`（`:1871-1882`），`holding_bars` 则由 `resolve_holding_bars`（`:623-645`，读 `prior_state.holding_bars` 于 `:628-631`）取得；③`get_account_state`（`storage.py:2666-2690`）对读到的状态**完全不做 `as_of_date` 校验**。

### 6.1 订正 ②/③ 是否扩大本任务范围

**不扩大。** 订正 ① 明确把"选谁"判为**已解决**，与冻结决定 1「不重开选择逻辑」一致；订正 ② 是**证据更正**（引用错了账本，且更正后的值仍是"11-02 的旧值"），只强化了"内容不能自证新鲜"这一事实，不新增机制要求；订正 ③ 描述的是**剩余缺口的形状**（"全链路没有『期望的上一交易日』不变量"），与冻结决定 2/4 的"**校验而非重选**"是同一件事的两种表述 —— 本任务交付的正是**可观测的校验与标记**（冻结态标记 + 版本交叉校验），**不**新增"期望上一交易日"的强制过滤（那会改变选谁，属于冻结决定 1 明确排除的范围）。因此本任务严格按五条冻结决定实施。

## 7. 验证（离线、合成数据）

| 契约点 | 用例 | 反向改坏（必须转红） |
|---|---|---|
| §2 冻结态命中（含两个分支的落盘） | `test_frozen_suspect_equal` | 删掉 `detect_frozen_prior_state` 的调用，或删掉 `prior_state_frozen_suspect=` 的传递 |
| §2 干净时无键 | `test_no_marker_when_distinct` | 把"缺失"改成写 `False` |
| §3 三态门槛 | `test_nav10d_distinct_three_states` | 把判据退回原始 `len(...)` |
| §3 相邻重复算一次 | `test_duplicate_tail_counts_once` | 同左 |
| §4 不足永不为 0 | `test_insufficient_never_zero` | 让 `nav_10d_return` 回落到 `0.0` |
| §5 版本不一致必抛 | `test_version_mismatch_raises` | 让比对提前 `return` |
| §5 一致即通过 | `test_match_passes` | 无条件抛异常 |
| §5 显式空记录必抛 | `test_missing_recorded_version_raises` | 把"存在但为空"并入"缺失" |

用例落点：Stage 1 侧的五条（冻结态命中 / 干净无键 / 三态门槛 / 去重计数 / 不足永不为 0）与版本交叉校验的三条，全部落在
[`pipeline/tests/test_stage1_nav_source.py`](file:///home/stone/Work/github/quant/pipeline/tests/test_stage1_nav_source.py)
（该文件原本就是 LV1 的 Stage-1 NAV 冻结契约套件，且已用 `importlib` 直接加载脚本；版本判据本身是
[`scripts/live_preflight.py`](file:///home/stone/Work/github/quant/scripts/live_preflight.py) 的纯函数，测试只 import 该模块，
**不** import `run_live_strategy.py`，故不会把整个票据脚本拖进测试）。`test_frozen_suspect_equal` 末尾附带**源码级接线断言**
（定义 + 调用各一次、`**frozen_annotation` 两个分支、比较值取 `nav_gate_nav`），否则"纯函数对了但 `main()` 没接线"会无人发现。

全部用例 100% 离线：只用 `tmp_path` 与手工构造的字典/JSON，**不读** `data/runs/**`，不联网、不取真实行情。

## 8. 版本轴声明（显式）

**本任务不改变任何版本号。** 具体地：

* 不修改 `pipeline/research/blend/blend_base.py` 的 `BASE_VERSION`；
* 不修改 `common/allocation_templates.py` 与任何策略类的 `version`；
* 不运行 `--dump-strategies`，不改动 `pipeline/research_strategy/results/strategy_dumps/*.json`；
* 不修改 `docs/version.md`；
* 不修改 `common/tests/test_strategy_versioning.py` 与 `pipeline/research_strategy/tests/test_blend_strategy_versioning.py` 的任何断言值。

改动范围仅限：Stage 1 健康检查脚本（冻结态标记 + 去重门槛 + 渲染口径）、Stage 2 票据脚本（版本交叉校验的调用点）、`scripts/live_preflight.py`（版本交叉校验的纯函数）、以及相应的离线测试。均属"运行时脚本与其测试"，无信号计算、无参数默认值、无子策略组成变化。
