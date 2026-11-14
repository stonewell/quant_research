# 行情预刷新新鲜度契约与 `last_sync_time` 时区口径（冻结版 v1）

> **文档性质**：**冻结的接口契约（frozen contract）**，不是实施记录，也不是设计探讨。本文定义持仓预刷新（`patch_equity_daily_bars`）的**逐命名空间新鲜度判据**、`last_sync_time` 的**时区口径**、以及预刷新**必须产出的逐命名空间留痕**。预刷新实现、调用方与后续审计必须**消费**本文，不得反过来由实现倒推口径。
> **冻结日期**：**2026-11-06**。契约版本号：`patch_freshness/v1`（判据）+ `namespace_trace/v1`（留痕）+ `sync_time/utc-naive-v1`（时间戳）。
> **状态**：**FROZEN**。本文是 `LV19`（持仓预刷新与估值取数的**新鲜度契约不一致**，且 `last_sync_time` 落**裸本地时间**）的冻结文档；本文冻结的五条决定即实施依据，实施侧不得另行解释。
> **任务状态 SSOT**：[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.21 `LV19` 行是任务状态、优先级与验收的唯一权威；本文只维护**新鲜度判据与留痕字段**这一项 SSOT。本文**不维护**任务状态。
> **修订方式**：本文正文一经冻结即**不可改写**；任何变更以 **append-only** 的 `## Revision YYYY-MM-DD` 追加段落在文末落地（见 §8）。冻结文本 + 全部修订段共同构成现行契约，冲突时**修订段覆盖冻结文本**，但冻结文本的字面保留以便追溯。

## 0. 冻结范围、非目标与判断口径

### 0.1 本文冻结什么

| 项 | 冻结内容 | 章节 |
|---|---|---|
| 严格命名空间 | 「估值真正读取的那个命名空间」如何从站点配置**派生**（禁止硬编码 provider 名） | §1 |
| 容忍度 | 严格命名空间**零日**、其余命名空间**保留 2 日**（周末／假日容忍**不得**一刀切删除） | §2 |
| 时区 | `last_sync_time` 落 **naive UTC**；市场本地时间只是**读取／报告侧**的渲染 | §3 |
| 留痕 | `per_namespace: {provider → skipped｜patched｜failed}` 为**强制输出**，三态语义与聚合优先级 | §4 |
| 已定案测量 | `§4.48` 的「Fuyao 元数据写入者」结论（本条**零代码**） | §5 |
| 已知残留 | 读穿透路径（`common/data.py:1095`）**不在**本次留痕范围内 | §6 |
| 验证 | 离线用例与「反向改坏即转红」的实测证据 | §7 |

### 0.2 非目标（本文明确**不**改变什么）

1. **不改估值口径**。`common/freshness.py` 的「bar 必须恰在 `as_of`」（零容忍）是读取侧**既有**契约，本文只是让写入侧与之对齐，不放松读取侧任何一条判据。
2. **不改全 universe 刷新的容忍度**。周末／假日仍需容忍，见 §2.3。
3. **不实现读穿透留痕**（`read_through` 状态**保留但不在本次范围**，见 §4.5 与 §6）。
4. **不碰版本轴**。不涉及任何策略／模板的 `version` 或 `BASE_VERSION`，不触发 `docs/version.md` 变更，不跑 `--dump-strategies`。
5. **不改写任何已归档 `data/runs/**`**。
6. **不改 `fetch_errors` 的形状与含义**（见 §4.4）。

## 1. 严格命名空间：从站点配置派生，禁止硬编码

**冻结决定 1（逐字）**：严格命名空间 = 站点 `default_data_provider`，经 `DuckDBCache.resolve_provider_key` 解析（`marketdb` → `MarketDBDataProvider`，`fuyao` → `FuyaoDataProvider`；默认 `marketdb`）。它作为 `strict_namespaces=...` 传入 `patch_equity_daily_bars`；**绝不硬编码 provider 名**。

### 1.1 为什么必须派生而不是写死

写入侧今天对两个命名空间是**硬编码双写**（`FuyaoDataProvider` + `MarketDBDataProvider`），而读取侧由配置决定（`settings.local.json` > `default_data_provider`）。两侧本应由**同一配置项**派生。2026-11-05 的失败正是这个缺口：补数步骤把两个命名空间都判为「新鲜」，而 `compute_nav` 读的那个命名空间其实差一个交易日。

### 1.2 派生点唯一

`web/livetrading/execution_service.py::refresh_held_book_daily_bars` 是**唯一**派生点：它已经持有站点的 provider 值，并在打开可写缓存后用 `cache.resolve_provider_key(...)` 把它映射到缓存实际存储的键（短名 `marketdb` 与类名 `MarketDBDataProvider` 是同一命名空间，比较走 `canonicalize_provider_key`）。解析结果同时写进返回结构与本轮日志（`strict_namespace` 字段），因此「解析成了什么」永远可见。

`scripts/sync_china_5m_and_indices.py::patch_equity_daily_bars` 的形参默认值为 `("MarketDBDataProvider",)`（仓库文档化的默认 provider `marketdb`），仅为独立 CLI 运行时的兜底；**它不构成站点侧的真值来源** —— 站点永远显式传入自己解析出的键。

### 1.3 解析不到时**不得静默降级**

若传入的 provider 名解析不到任何已存键（拼写错误，或该缓存从未存过它的 bar），派生结果为空 ⇒ 没有任何命名空间获得零容忍。此时：

* `resolve_provider_key` 本身会打 `WARNING`（既有的「不静默修复」契约）；
* 解析值以空串形式出现在 `strict_namespace` 字段与日志中（`<unresolved>`），**不会**被伪装成一个具体 provider。

这是**已披露的边界**，不是静默回退：读者能看见「零容忍没有被施加到任何命名空间」。

## 2. 逐命名空间容忍度

**冻结决定 2（逐字）**：逐命名空间容忍：严格命名空间要求 `max_date >= target_end_date`（零日）；非严格命名空间保留 `max_date >= target_end_date - timedelta(days=2)`（周末／假日容忍**不得**整体删除）。

### 2.1 判据（唯一形式）

对每个写入命名空间 `p`、每个标的：

```
严格：  is_fresh(p) ⟺ max_date(p, sym) >= target_end_date
非严格：is_fresh(p) ⟺ max_date(p, sym) >= target_end_date - NAMESPACE_FRESHNESS_TOLERANCE_DAYS   # = 2
```

`max_date` 缺失（该命名空间从未存过该标的）在两个分支下都**不算新鲜** —— 首次同步有权拉全窗口。

### 2.2 「严格」的成员判定

`strict_namespaces` 的每个条目先经 `canonicalize_provider_key` 归一，再与写入命名空间比较。因此调用方可以传设置里的短名（`marketdb`）或缓存键（`MarketDBDataProvider`），两者命中同一命名空间；未知名字**不匹配任何命名空间**（不会被悄悄提升为零容忍）。

### 2.3 为什么非严格侧必须保留 2 日

全 universe 刷新在周末／假日运行时，最后一个交易日到 `--end` 之间本来就没有 bar；若无容忍度，每次周末运行都会把整池重新抓一遍（网络成本、超时风险、以及无意义的重复写入）。因此**只收紧「估值真正读取的那个命名空间」**，其余保持不变。任何「一刀切删掉容忍度」的改动都违反本条。

## 3. `last_sync_time`：naive UTC

**冻结决定 3（逐字）**：`last_sync_time` 是**naive UTC**：`datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)`，并写在列的 schema 注释上。市场本地（+08:00）只是**读取／报告侧**的渲染；**绝不**把裸值当作本地时间呈现。

### 3.1 唯一 owner

`common/duckdb_cache.py::sync_timestamp_utc_naive` 是**唯一**产生该列值的地方（`store_bars` 与 `store_intraday_bars` 各一处写入点）。三条不变式：

1. 返回值**永远 tz-naive**（`TIMESTAMP` 列没有偏移量；交给驱动去 cast 一个 aware 对象，等于让驱动的会话时区**静默决定**写入的是哪个时刻）；
2. 该 naive 值**永远是 UTC**（不是本地墙钟）；
3. 显式传入的 `sync_time` 同样被归一（aware → 转 UTC 再去 tz；naive → 按「已是 UTC」的入参契约原样使用），故不变式对**所有**调用方成立，而不只是默认分支。

### 3.2 为什么这是事故级修正

2026-11-05 运维把 `20:23` 读成「前一晚」，而它实际是同一轮运行的**第 3 分钟**：裸本地时间与市场时钟相差 **16 小时**，任何事后审计读这一列都会读错——而这一列恰恰是「这根 bar 是不是本轮写的」的唯一依据。

### 3.3 实测证据（DuckDB 会把 aware 时间按会话时区转成墙钟）

本机时区为 UTC−08:00。把写入端改成 `datetime.datetime.now(datetime.timezone.utc)`（**只删 `.replace(tzinfo=None)`**）后，插入并未报错，而 `asset_sync_metadata.last_sync_time` 读回来是 **2026-11-06 20:03:19**，同一时刻的 UTC 是 **2026-11-07 04:03:19**：驱动把 aware UTC 转换成了**本地墙钟**再落列。这正是 §3.1 第 1 条不变式存在的理由——「aware 也能写进去」不等于「写进去的是 UTC」。该改坏实验见 §7.2。

### 3.4 报告侧渲染（读取侧契约，不是写入侧）

若某报告要展示本地时间，**唯一**允许的写法是「读出的 naive 值先标记为 UTC、再转换」：

```
stored.replace(tzinfo=datetime.timezone.utc).astimezone(MARKET_TZ)
```

禁止直接把裸值 `strftime` 后标成 `+08:00`。当前无任何报告渲染该列，因此本次实现只落**列不变式**；本节约束的是将来任何渲染点。

### 3.5 已披露的行为后果

`is_range_covered` 的**遗留**分支（未提供 `expected_date` 时）存在「今天已同步过」的短路，它拿该列的**日期**与宿主本地日期比较：在 00:00–08:00（+08:00）之间，UTC 日期可能仍是前一天，短路因此可能判「今天未同步」。该分支：

* 在**实盘路径**上被 `expected_date` 分支完全绕过（`L2.2`：新鲜度只看期望交易日）；
* 即使命中也不会单独决定 bar 新鲜度——bar 层仍须 `MAX(date)` 到达同一交易日。

本条选择**保留已存 UTC 口径**，而不是为遗留短路回退成本地时间：可审计性优先于一条已被绕过路径的便利。

## 4. `per_namespace`（强制输出）

**冻结决定 4（逐字）**：`per_namespace: {provider → skipped｜patched｜failed}` 是**强制输出**：`skipped` = 新鲜、未被触碰；`patched` = 有 bar 写入；`failed` = 抓取报错／被拒绝。`read_through` 保留但**不在本次范围**。

### 4.1 三态语义（精确，互斥）

| 状态 | 触发条件（本轮的精确判据） | 它**不**表达什么 |
|---|---|---|
| `skipped` | 该命名空间本轮**未发生写入**且**无错误**：被自身容忍度判为新鲜；或响应在本命名空间窗口内无可写入 bar（`no new bars` / `ALREADY CURRENT`） | **不**证明该命名空间满足零容忍判据——它只说明「本轮没在这里写」。这正是它必须与 `fetch_errors` 并列展示的原因 |
| `patched` | 本轮通过 `store_bars` 为该命名空间**写入了 bar** | 不表达写入是否覆盖到 `target_end_date`（覆盖范围看 `ohlcv_bars` 的 `MAX(date)`） |
| `failed` | 抓取抛错；或写入被拒（`QfqWriteRefused` / `NullVolumeRefused`） | 不表达成功了多少行（被拒批次的其余合法行仍已提交） |

### 4.2 跨标的聚合优先级（worst-first）

`per_namespace` 是**整轮**的映射，而一个命名空间在多个标的上可能有不同结局。聚合采用 **`failed` > `patched` > `skipped`**：19 个 `patched` + 1 个 `failed` 记为 `failed`。理由：一个部分被拒的命名空间不是「绿」的；逐标的明细已由 `fetch_errors`（失败标的）与 `ohlcv_bars`（成功行）承载，映射只需回答「这个命名空间本轮是否干净地完成」。

### 4.3 键的存在性

一个命名空间一旦被评估（进入容忍度检查），它的键就**可能缺失**吗？不会：本轮任何被检查过的命名空间都会写下状态；因此**只要 `failed`，键必然存在**——「试过但没成功」与「根本不需要」绝不可表现为同一形状。这正是 2026-11-05 归档里 `fetch_errors=none` 无法回答「估值要读的命名空间被跳过了吗」的直接原因。

### 4.4 为什么走缓存通道而不是改返回值

`patch_equity_daily_bars` 的返回值为 `symbol -> "<provider>: Type: msg"`，其形状与含义被 Stage-1 子进程、Station 与本仓约 10 个离线用例共同依赖，**冻结不变**。生产调用点 grep 结果**多于 2 个**（除预期的 `web/livetrading/execution_service.py` 与 `scripts/check_live_portfolio_health.py`，还有本脚本自身 CLI 的 `:1591`，以及多处测试），因此按「多于 2 个即不改签名」的裁决，本次**不改签名**，改为在原调用方已经持有的**缓存对象**上开一条独立通道：

| 侧 | 接口 | 语义 |
|---|---|---|
| 写 | `DuckDBCache.record_namespace_trace(dict)` | 由 `patch_equity_daily_bars` 在 `refusals.raise_if_any()` **之前**调用（异常展开不得吞掉留痕）；未知状态直接 `ValueError` |
| 读 | `DuckDBCache.namespace_trace()` | 返回副本；从未跑过则 `{}` |

作用域：**进程内、仅上一轮**——该映射描述的是一次刷新尝试，不是数据事实，因此**不落库**（数据事实仍是 `ohlcv_bars` 与 `*_sync_metadata`）。

此外该映射以一行 `[NAMESPACE-TRACE] p=state ...` 打进 PART A.2 的 stdout，Station 侧则把它并入本轮日志（`per_namespace=...` 与 `strict_namespace=...`）。

### 4.5 `read_through`（保留，未实现）

读穿透（缓存未覆盖时 `CachedDataProvider.get_bars` 直接 `store_bars` 回写）是**第二个写入者**，它落在补数步骤之外，且**不留任何 `Patching` 痕迹**（见 §5）。本契约预留 `read_through` 作为**将来**的第四态入口，但**本次不实现**：`record_namespace_trace` 只接受 `skipped` / `patched` / `failed`，传入其他字符串（含 `read_through`）会被**显式**拒绝，而不会被静默存下——预留不等于默许。

## 5. `§4.48` 已定案的测量（本条零代码）

`LV19` 第 4 条要求先定案「谁写了 Fuyao 元数据」。该测量已在 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) `§4.48`（`:2133-2178`）定案，**本节只引用结论，不做任何新的测量或代码改动**。

### 5.1 结论（原文引用）

> `§4.48.1`：**写入者是 Stage-1 子进程自己，且不是补数步骤，而是紧随其后的加载步骤**：
>
> `scripts/check_live_portfolio_health.py:2333 load_live_universe` → `common/cli_utils.py:599` → `:629 load_universe_with_banner` → `:160`（打印 `Loading N symbols ...`）→ `common/data.py:1678 load_universe` → `CachedDataProvider.get_bars` 在缓存**未覆盖**时 `:1095 store_bars(provider_name, ...)`。
>
> ⇒ §4.47.5 的两种猜测（「写入者不在两个归档进程之内」与「归档 stdout 不完整」）**都不成立**。二者并不矛盾的原因很简单：`PART A.2` 里没有 `Patching <sym> [FuyaoDataProvider]`，是因为**从来没有发生过 Fuyao 的补数**；Fuyao 那 20 行是加载路径的**读穿透写回**，它不打印 `Patching`。

### 5.2 支撑事实（`§4.48.2`，全部实测）

| 证据 | 实测值 |
|---|---|
| 写入条数 | **20 只**（非 7 只持仓）⇒ 排除「持仓预刷新循环」 |
| Fuyao 批次 | `04:23:28.047` → `04:23:36.419`（UTC），**间隔 0.44 s 均匀** ⇒ 排除网络抓取，是**本地 DuckDB 读** |
| MarketDB 批次 | `04:20:10.8` → `04:22:24.5`；两批相隔 64 s ⇒ 不可能是同一次补数调用 |
| `PART A.2` 原文 | 13 行全部为 `[MarketDBDataProvider]`；7 只持仓未出现（预刷新时已判新鲜） |

### 5.3 对本契约的意义（`§4.48.3`）

同一轮里存在**三条**新鲜度口径：补数（2 日容忍，有留痕）、估值取值（零容忍，抛 `StalePriceError`）、读穿透覆盖（严格但**静默修复**、**无留痕**）。门禁之所以 `[FRESHNESS PASS]`，是因为第三条在 `04:23` 把第一条留下的 `11-04` 补成了 `11-05`；而 `fetch_errors={}` / `steps.patch_equity_daily_bars = "ok"` 对此**零留痕**。

⇒ 本条把「逐命名空间留痕」从可读性改善升级为**必需**（§4），同时把读穿透本身留在 §6 的已知残留里。

### 5.4 两条被引用的附带结论（`§4.48.4`）

1. **命名空间名不等于数据血缘**：本轮 `FuyaoDataProvider` 命名空间存的是**本地 MarketDB 的字节**（`prefer_local=True`），不是 Fuyao 的字节；引用 provider 名作数据来源证据时必须同时说明 `prefer_local` 的实际命中。
2. **同一命名空间在一轮内有两个写入者**（补数 + 读穿透）。任何「某命名空间是否被跳过」的审计必须同时覆盖两者；本契约目前只覆盖前者。

## 6. 已知残留（明示，不伪装成已解决）

1. **读穿透路径不入 `per_namespace`**：`common/data.py:1095`（`CachedDataProvider.get_bars` 未覆盖即 `store_bars`）不属于 `patch_equity_daily_bars`，本轮**零留痕**，且**不在本次范围**。因此 `per_namespace` 的 `skipped` **不能**被读成「该命名空间本轮没有获得任何写入」——它只覆盖补数步骤。§4.5 的 `read_through` 是为这条路径预留的入口。
2. **`strict_namespace` 解析不到时不施加零容忍**：见 §1.3（显式可见，非静默）。
3. **`last_sync_time` 的遗留短路**：见 §3.5（仅影响绕过 `expected_date` 的遗留分支）。
4. **市场本地时间未进入任何报告**：见 §3.4（当前无渲染点；渲染规则已冻结）。
5. **其余三张写 `last_sync_time` 的表**（`adjust_factors`、`stock_fundamentals_annual`、`stock_industry_metadata`）不在本契约的冻结范围内：`LV19` 指向的是行情同步的两处写入点（`asset_sync_metadata` / `intraday_sync_metadata`），另外三张表的时区口径属独立决策。
6. **Stage-1 子进程自己的全 universe 刷新不派生严格命名空间**：`scripts/check_live_portfolio_health.py:926` 调用 `patch_equity_daily_bars` 时不传 `strict_namespaces`，因此使用形参默认值（`MarketDBDataProvider`，即文档化的默认 provider `marketdb`）。当操作员把 `default_data_provider` 改为 `fuyao` 时，子进程仍会对 MarketDB 施加零容忍、对 Fuyao 施加 2 日容忍——方向是**多抓一次**（`Fuyao` 侧若真落后，§6.1 的读穿透会把它补齐），**不是**读到陈旧 bar；而真正决定预置估值的那次刷新在 Station 侧（§1.2），已派生。此项登记为残留，不在本次改动范围。

## 7. 验证（全离线，合成数据）

### 7.1 用例

| 文件 | 用例 | 钉住的条文 |
|---|---|---|
| `pipeline/tests/test_patch_equity_daily_bars_contract.py` | `test_strict_zero_tolerance_patches` | §1、§2.1（严格侧零容忍：差一日必补，且 `MAX(date)` 到达 `end`） |
| 同上 | `test_default_strict_namespace_is_the_stored_marketdb_key` | §1.2（形参默认兜底是 `MarketDBDataProvider`） |
| 同上 | `test_strict_namespace_accepts_the_settings_name` | §2.2（`marketdb` 与 `MarketDBDataProvider` 同一命名空间） |
| 同上 | `test_non_strict_keeps_two_day_tolerance` | §2.3（非严格侧保留 2 日，且「不发请求」） |
| 同上 | `test_fresh_strict_is_skipped` | §4.1（新鲜 ⇒ `skipped`，零抓取） |
| 同上 | `test_failed_reported` | §4.1、§4.3（`failed` 且**键存在**） |
| 同上 | `test_refused_write_is_reported_as_failed` | §4.1（拒写 ⇒ `failed`，且异常前已发布留痕） |
| `common/tests/test_sync_timestamp_utc.py` | `test_store_bars_naive_utc` / `test_store_intraday_naive_utc` | §3.1（两处写入点均为 naive 且在 UTC 秒级内） |
| 同上 | `test_explicit_aware_sync_time_is_normalised_to_naive_utc` | §3.1 第 3 条 |
| 同上 | `test_sync_timestamp_utc_naive_contract` | §3.1（helper 三条不变式） |

### 7.2 「反向改坏即转红」实测

| 改坏 | 结果 |
|---|---|
| 删除严格命名空间分支（容忍度退回统一 2 日） | `test_strict_zero_tolerance_patches`、`test_default_strict_namespace_is_the_stored_marketdb_key`、`test_strict_namespace_accepts_the_settings_name`、`test_failed_reported`、`test_refused_write_is_reported_as_failed` **5 条转红**；非严格侧两条仍绿（判据确实只被削掉了严格那一半） |
| 写入端退回裸本地墙钟 `datetime.datetime.now()` | `test_store_bars_naive_utc`、`test_store_intraday_naive_utc`、`test_sync_timestamp_utc_naive_contract` **3 条转红**（偏差 `28800 s` = 8 小时，即本机 UTC 偏移） |
| 只删 `.replace(tzinfo=None)`（落 aware UTC） | 同上 **3 条转红**；且落库值被驱动转成**本地墙钟**（§3.3 实测），说明「更「正确」的 aware 值」在这里是**更糟**的结果 |

## 8. 修订记录

（暂无修订。任何修订以 `## Revision YYYY-MM-DD` 追加于本文末尾，不回改上文。）

## Revision 2026-11-13

> **本节覆盖范围**：只关闭 §6.6 登记的两半缺口中的 **(b) 逐命名空间留痕缺失**。
> §6.6 的冻结原文**不回改**；冲突时以本节为准。它的另一半 **(a) 子进程不派生严格命名空间**
> **仍然开**：`scripts/check_live_portfolio_health.py` 的调用点（`:1531`）依旧**不传**
> `strict_namespaces`，方向仍是「多抓一次，而不是读到陈旧 bar」。
> 代码依据：`a37f186`（实现）；任务状态 SSOT：`docs/development_plan.md` §2.21 `LV24`
> （`d2adfff` 收口）。本节**不改**任何上文条文、不新增第五态、不动 §4.4。
> 随本节落地，§8 那句「（暂无修订。）」的状态随之失效；§8 的**机制条款**（append-only、
> 不回改上文、修订段覆盖冻结文本）继续有效。

### R1. 关闭的残留

* **§6.6(b)**「Stage-1 子进程的全 universe 刷新没有 `per_namespace` 留痕」——**已关闭**。
  该路径此前只有**聚合** `fetch_errors`，于是 `fetch_errors={}` 与「每个命名空间都是最新」
  在读起来上完全一样（2026-11-05 的形状）。现在每个**被评估**的命名空间都会写下状态与理由。

### R2. 新增的通道：出参，不是返回值

`patch_equity_daily_bars` 新增**可选关键字出参** `trace_out`（就地填充，且在
`refusals.raise_if_any()` **之前**填充——拒写异常不得吞掉留痕）：

```
{"namespace_trace":  {provider -> "skipped" | "patched" | "failed"},
 "namespace_detail": {provider -> {"state", "reason", "symbol"[, "rule"]}}}
```

* `namespace_trace` 与缓存通道 `DuckDBCache.record_namespace_trace` 发布的映射**同键同值**，
  词表**逐字沿用 §4.1**：**没有第四态**。`read_through` 仍是 §4.5 的预留、**仍不实现**。
* `reason` 回答三态回答不了的「为什么」：
  * `skipped`：`fresh_strict`（严格命名空间的零容忍判据已满足）／`fresh_tolerance`
    （**仅**在 2 日周末／假日容忍下算新鲜——即「我们没刷新它」的唯一可见证据）／
    `no_new_bars`（响应在本命名空间窗口内无可写 bar）／`already_current`
    （全部已在库且同基准）。
  * `failed`：`fetch_error`（该标的的取数源全部抛错）／`refused_write` + `rule`，
    其中 `rule ∈ {qfq_rebase_refused, null_volume_refused}`：前者是 `plan_history_write`
    的跨源拒绝（来窗与已存收盘价冲突），后者是逐行拒写（空成交量从不用 `0.0` 顶替）。
* 聚合仍是 §4.2 的 worst-first（`failed` > `patched` > `skipped`）；`reason`／`symbol`／`rule`
  记的是**把该命名空间推到当前（最坏）状态的那一次**结果，故「19 次 `patched` + 1 次 `failed`」
  记为 `failed`，且保留失败那次标的与规则。

### R3. §4.4 仍然冻结（本修订不触碰）

* `fetch_errors` 的**返回类型、排序与含义一字未动**：仍是裸 `dict`，形如
  `symbol -> "<provider>: Type: msg"`；trace 只走**出参**，源码里的 `return fetch_errors`
  **原样保留**（因此 §0.2 第 6 条继续成立）。
* 实测（同一合成输入，**改动前 `HEAD` 的原语** vs 改动后的原语，各跑四个场景：干净／
  仅 `MarketDB` 补数／双源抛错／跨源拒写）：序列化结果**逐字节相同**，
  `sha256 = 64d9dd4f…`（完整值见下；拒写场景连聚合异常的 `str()` 亦相同）。

  ```
  64d9dd4ffcb06fe18f7573e4b2845cc6628fbc0da28b1dce5458acbc795ced21
  ```

* 两条守卫各钉一面：`test_lv24_primitive_keeps_the_frozen_return_statement`（源码级：
  `) -> Dict[str, str]:` 与 `return fetch_errors` 存在、`fetch_errors.update(` 不存在、
  `trace_out` 存在）与 `test_lv24_fetch_errors_shape_is_frozen`（字节级：逐字符比对 +
  确认没有任何 trace 词汇漏进该映射）。

### R4. 报告面（只有这两处可见）

* `stage1_health_report.json::data_refresh.equity_daily_patch`（`summary["equity_daily_patch"]`，
  写入点 `scripts/check_live_portfolio_health.py:1547`）：即 R2 的两键结构。一个命名空间
  **都没被评估**时该块**缺席**（与 `fetch_errors` 同一约定：缺席不是空表）。
* 控制台**一行** `[STAGE 1 NAMESPACE TRACE]`（`summarise_namespace_trace`，`:1339`）：
  **仅**在存在 `failed`、或存在 `reason=fresh_tolerance` 的 `skipped` 时打印；
  全部 `patched` / 严格新鲜的绿跑**不打印**。
* **明确披露**：Stage-1 的 **markdown** 报告**不渲染**该 trace——它只进 JSON 与那一行控制台
  输出。需要追溯逐命名空间状态时请读 `stage1_health_report.json`，不要到 markdown 里找。

### R5. 验证（全离线、合成数据）

* `pipeline/tests/test_stage1_nav_source.py` 新增 **8 条** `test_lv24_*`（`pytest ... -k lv24`
  可单独筛出）：全部 `patched`／容忍度跳过仍整体成功／取数失败／拒写带 `rule`／
  worst-first 与标的顺序无关／`fetch_errors` 冻结字节／原语返回值源码级守卫／干净轮不打印。
* **反向改坏即转红**：注掉 `trace_out` 的填充后，其中 **5 条转红**；还原后 8 条全绿。
* 既有回归：`pipeline/tests` 全套 **1386 passed, 1 skipped**。

### R6. 不改变什么

* 方向不变：**宁可多抓一次，也不读到陈旧 bar**——不放松任何容忍度（§2 照旧），不改写入哪些
  命名空间、不改顺序、不改任何 fetch 行为。
* **零选股／权重／下单影响**：`enforcement: observation_only`，该留痕只报告、不参与任何闸门。
* **零版本轴**：不 bump 任何 `version` / `BASE_VERSION`，不跑 `--dump-strategies`，不改任何
  `strategy_dumps/*.json`（代码改动已在 `docs/version.md` §43 单独登记）。
