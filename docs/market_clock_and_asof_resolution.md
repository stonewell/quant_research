# 实盘市场时钟与估值日期解耦（Market Clock & As-Of Resolution）

| 项目 | 内容 |
|---|---|
| 设计状态 | 待确认 |
| 关联 SSOT | `docs/development_plan.md`（L2.1–L2.5） |
| 提出日期 | 2026-10-22 |
| 触发事件 | 2026-10-22 `chan_four_state_blend` 实盘运行 |
| 文档性质 | 设计 / 审计（不含任务状态跟踪） |

按 `AGENTS.md` 的 SSOT 约定，本文只承载设计建模、代码审计与模块规格；任务拆分、优先级与执行状态一律记录在 `docs/development_plan.md`（L2.1–L2.5），本文不维护任何"已完成 / 进行中"标记。

---

## 1. 问题陈述：UTC−7 主机 vs 三个市场时区

实盘链路横跨三个市场时区，但宿主机的系统时钟是**美国太平洋时间（UTC−7）**：

| 环境 | 时区 | 与宿主的关系 |
|---|---|---|
| 宿主 `date` / `datetime` | US Pacific（UTC−7） | 基准 |
| China A-share | `Asia/Shanghai`（UTC+8） | 领先宿主 15 小时 |
| Hong Kong | `Asia/Hong_Kong`（UTC+8） | 领先宿主 15 小时 |
| US | `America/New_York`（UTC−4 / UTC−5） | 领先宿主 3 小时（夏令时）或 2 小时 |

2026-10-22 的实盘运行留下三条硬证据：

| 证据 | 观测值 | 换算 |
|---|---|---|
| `summary.json` 时间戳 | 裸 `2026-10-22T19:05:27`（无 tz） | 北京/香港 `2026-10-23 10:05`（上午盘中）；太平洋 `2026-10-22 19:05`（美股已收盘） |
| 缓存写入时刻 | PT `18:59:41`–`19:02:10` | 北京 `2026-10-23 09:59`–`10:02`，即 **A 股上午连续竞价时段内** |
| 实际运行窗口 | 北京 `10:05` 左右 | 距设计预期的 `13:55`–`14:20` 尾盘窗口约 4 小时 |

一次"按尾盘口径设计"的实盘运行，实际发生在次日**早盘盘中**，而全链路没有任何一处代码对此提出异议。

---

## 2. 关键更正：本次的 as_of 日期其实是"碰巧正确"的（必须写进文档，防止被误记）

事后第一直觉是"`date.today()` 在 UTC−7 主机上算错了日期"。**这个结论是错的，必须纠正并固化**，否则后续排期会把工时投在不存在的问题上，而真正的缺陷被放过。

对 UTC−7 宿主观察 UTC+8 市场，存在一个 **15 小时偏移不变量**：

| 太平洋时间 | 北京时间 | 已收盘的最近交易日 | 宿主 `date.today()` |
|---|---|---|---|
| D 00:00 | D 15:00 | D | D |
| D 09:00 | D+1 00:00 | D（D+1 未收盘） | D |
| D 19:05 | D+1 10:05 | D | D |

实质：15 小时偏移不足以把北京时间的日期推过下一个交易日的收盘（15:00）。北京 D+1 的收盘（15:00）对应太平洋 D+1 的 00:00——此刻宿主的日期已经是 D+1，而北京的 D+1 也刚刚收盘。因此**宿主"今天"总是落在北京/香港同一个日历日 D 之内**，`date.today()` 与正确的 China/HK as_of 恒等。2026-10-22 产物里的 `as_of=2026-10-22` 是**构造上正确的（by construction）**，既非运气，更**不是错的**。

该不变量真实失效的三种情形：

| # | 情形 | 示例 | 后果 |
|---|---|---|---|
| 1 | 宿主处于周末/节假日 | PT 周日 20:00 = 北京周一 11:00（盘中） | `date.today()` 给周日，正确 as_of 是上一个周五 |
| 2 | UTC−7 宿主看**美股** | ET = PT+3，PT 06:00 = ET 09:00 开盘前 | `date.today()` 给 D，正确 as_of 是 **D−1**，真实错一天 |
| 3 | 任何非 UTC−7 宿主 | 开发者本机、云主机、容器未挂时区 | 巧合整体消失 |

### 2.1 未声明的关键前提：宿主是固定偏移还是带夏令时

上面的不变量**只在宿主是"固定 UTC−7"偏移时成立**。§1 的市场表把美股写作 `America/New_York（UTC−4 / UTC−5）`、并称其"领先宿主 3 小时（夏令时）或 2 小时"，这本身就暴露了一个从未被声明的前提：宿主时区**究竟是不是**跟 US DST 一起移动。差别如下：

| 宿主时区 | 北京 = 宿主 + | 本地 D 23:59 时北京 | 最近已收盘 | `date.today()` | 一致 |
|---|---|---|---|---|---|
| 固定 UTC−7（如 `Etc/GMT+7`） | 15h | D+1 14:59（盘中） | D | D | ✅ |
| `America/Los_Angeles` 冬季（UTC−8） | 16h | D+1 15:59（已收盘） | D+1 | D | ❌ 差一天 |

也就是说：**若宿主跟随美国夏令时，则不变量在每年 11 月 DST 结束到次年 3 月 DST 开始之间失效**，失效窗口很窄——本地 23:00–24:00 之间（此时北京已是次日 15:00 之后）。2026-10 观测到的 UTC−7 恰好对应美国夏令时仍在生效，所以该缺陷在事故发生时**完全不可见**；正因为它不可见，才必须在此记录，而不允许由"当时是 −7"反推为"永远 −7"。

解析器命令（必须实际执行，不能靠推断）：

```bash
timedatectl | grep -i "time zone"
cat /etc/timezone
```

`Etc/GMT+7`（固定偏移）与 `America/Los_Angeles`（带 DST）是**两个不同的世界，需要两套不同的证明**：前者适用上文不变量，后者必须在 §12 的夹具里显式构造冬季/夏季两种偏移。

**宿主时区标识目前标记为 UNKNOWN — 待确认**，直到上述命令在某台实盘主机上跑过。受此答案影响的 §12 夹具：用例 1–7 的冻结时刻写法（写死 `−07:00` 还是 `ZoneInfo("America/Los_Angeles")`）、用例 8 的 DST 边界期望、以及新增的用例 13（宿主时区标识敏感性）。

**事故归因修正：**

| 归因 | 判定 | 依据 |
|---|---|---|
| (a) 无任何新鲜度断言 | **是（近因）** | 6 只标的 K 线停在 `2026-10-21`，仍参与盯市与出票 |
| (b) 无运行窗口门禁 | **是（近因）** | 北京 10:05 运行，距预期 `13:55`–`14:20` 窗口约 4 小时 |
| (c) as_of 算错 | **否** | 见上表不变量 |

市场时钟专项的目标是让 as_of **构造上正确且跨宿主稳健**，并为 (b) 提供可断言的窗口语义；它**不是**在修一个算错的日期。

---

## 3. 全链路清单（Inventory）

下表列出实盘路径上每一处"本地日期/时间进入决策"的位置；第三列描述**该假设的真实脆弱点**，不是"已经错了"。

| site (file:line) | 假设 | 从 UTC−7 主机看如何失效 |
|---|---|---|
| `common/duckdb_cache.py:361–362` `is_intraday_range_covered` 的 `pd.Timestamp.now()`→`current_date` | 宿主今天 == 市场今天 | 日内覆盖以宿主日历日为锚，盘中/盘后边界整体错位 |
| `common/duckdb_cache.py:381–396` 日内 end-coverage 7 天容忍（387、396） | 7 天内缺口都算周末/节假日容忍 | 交易日缺口被当周末吞掉；停一天完全无声 |
| `common/duckdb_cache.py:489–490` `is_range_covered` 的 `pd.Timestamp.now()` | 同上 | 日线覆盖判定的锚点是宿主日历日 |
| `common/duckdb_cache.py:501–520` 日线 end-coverage 分支 + 7 天容忍（508、520） | `last_sync_date >= current_date` 即"今天已同步"→ covered | 发布前抓取时"今天已同步"为真但 bar 停在昨天，整日不重取 |
| `common/duckdb_cache.py:547–553` layer-2 仅容忍 `weekday() < 5` | 只有周六日需容忍，工作日必有 bar | 交易所在工作日放假被判定永久缺口，反向制造无谓重取 |
| `common/duckdb_cache.py:627, 721, 996, 1148` 裸 `datetime.datetime.now()` 写入 `last_sync_time` | 该字段可作时间轴比较对象 | 写入宿主本地无 tz 时间，跨宿主/DST 即失真 |
| `common/data.py:654–663` `_cache_covers_requested_end`（659 `now`、663 7 天） | 与上面同源 | **同一启发式的第二份独立实现**；只修 `duckdb_cache` 会留下这扇门（已于 2026-10-22 作为死代码删除，见 `development_plan.md` `L2.19`；`is_range_covered` 成为唯一新鲜度闸门） |
| `common/data.py:685–729` `CachedDataProvider.fetch_ohlcv` | `is_range_covered` 真即返回缓存 | 覆盖判定一旦宽松，这里不会产生"是否重取"的念头 |
| `common/cli_utils.py` | 无日期默认（仅 107 行一句文档注释） | **干净，无需修改** |
| `common/scheduling.py:8` `get_rebalance_dates` | 纯 index 驱动（`index[::freq_days]`） | **干净，无墙钟、无节假日推断，无需修改** |
| `scripts/run_live_strategy.py:1043` lookback 由 `as_of_date` 反推 | as_of 正确⇒lookback 起点正确 | as_of 在周末/非 UTC−7 宿主偏移，连带覆盖错误窗口 |
| `scripts/run_live_strategy.py:469` cooldown helper 的 `date.today()` 兜底 | 缺参时用"今天" | 第二处独立取"今天"，与主流程 as_of 无绑定 |
| `scripts/run_live_strategy.py:352–356` 从 `output_dir` basename 反推上一轮日期，否则哨兵 `"9999-99-99"` | 目录名即交易日 | 目录名若为宿主本地日则持仓天数错；哨兵把未知压成"极久远" |
| `scripts/run_live_strategy.py:826` `--cutoff-time` 默认 `"14:15"` | 硬编码 A 股尾盘 | 港股/美股语义不符；字面量无时区，见 §8 |
| `scripts/run_live_strategy.py:1436` `price = float(universe[sym]["Close"].iloc[-1])` | 最后一根 bar 就是今天 | **静默陈旧兜底，本次事故的落点** |
| `scripts/run_live_strategy.py:1483–1488` 按后缀推断市场 | 后缀可定市场 | 只产出**标签**，不产出时区、session、expected session |
| `scripts/check_live_portfolio_health.py:814` `date.today().isoformat()` 作门禁日期 | 门禁日期 = 交易日 | 周末/节假日运行得到非交易日 as_of |
| `scripts/check_live_portfolio_health.py:818` `start_date` 由 as_of 派生 | 同上 | 同上，回看窗口整体偏移 |
| `scripts/check_live_portfolio_health.py:4` docstring 写死 "14:00 – 14:15" | 窗口是已知常量 | 从未由任何时钟计算得出 |
| `scripts/check_live_portfolio_health.py:165–166` `--cutoff-time` 默认 `"14:00"` | A 股口径 | 港股/美股无意义 |
| `scripts/check_live_portfolio_health.py:974` cutoff 与市场本地 bar 时间比较 | bar index 是市场本地时间 | 被比较的**字面量**是裸字符串（无 tz/无市场绑定） |
| `web/livetrading/storage.py:282, 821, 861, 1570` | runs 根目录 + 日期兜底 | 归档目录键写成宿主本地日，回看窗口随之偏移 |
| `web/livetrading/server.py:93, 285, 296` | 默认日期 = 交易日 | 前端一打开就带错误 as_of，并进入两份估值响应 |
| `web/livetrading/execution_service.py:133, 298, 444` | 归档/运行键 = 宿主今天 | **三处**独立兜底，行为一致但互不共享判断 |
| `web/livetrading/execution_service.py:67, 236, 394, 531` 裸 `datetime.now().isoformat()` | 时间戳可读即可 | 无 tz⇒事后无法判定盘中/盘后 |

> **已了结（原警告："必须在 L2.2 同批处理"）：** `_cache_covers_requested_end`（`common/data.py:654–663`）确实是 `duckdb_cache` 那套"7 天容忍 + 今天已同步"启发式的**第二份独立实现**，而只改 `duckdb_cache` 会让 CSV/DataFrame 通道继续静默返回昨日收盘、症状相同但排查方向被引向 DuckDB。此事已于 2026-10-22 了结：该 helper 经全仓 grep 证明**零生产调用方**（其守护的 CSV 磁盘缓存早已被 `DuckDBCache` 取代），随后作为死代码删除，见 `development_plan.md` `L2.19`。**不得重建**这第二份实现；未来任何非 DuckDB 磁盘缓存必须从 `common/duckdb_cache.py::is_range_covered` 移植规则，而不是凭记忆重写。

**归档键与影响面。** 归档目录形状为 `data/runs/<strategy_key>/<universe_key>/<date>`，其中 `<date>` 来源正是上表的宿主本地"今天"。同一套逻辑被复制到归档键上**至少 9 处**（`storage.py` 3 处、`server.py` 3 处、`execution_service.py` 3 处）。因此缺陷产物不是"一张错工单"，而是一段**系统性错键的归档历史**——每个工作日一个错键目录，跨周末/节假日还会跳号或重复，清理成本远高于重发一张工单。这就是归档键必须与估值 as_of 同阶段收敛（L2.4 / L2.5）的原因。

---

## 4. 设计：`common/market_clock.py`

**模块落点。** 放 `common/`：它是两个 project group（`pipeline/`、`ml/`）共享的基础设施，自身无 `pyproject.toml`/venv，靠 `sys.path` 注入被两边运行。依赖仅 `zoneinfo`（标准库）+ `datetime`，**不引入新依赖**，时区数据由系统 tzdata 提供。

**为什么是值对象而不是一组自由函数：**

| 理由 | 对应失败模式 |
|---|---|
| 可注入：测试与回测可构造任意时刻的 clock | 自由函数 `now()` 只能 monkeypatch 全局 `datetime`，脆弱且互相污染 |
| 一个实例 = 一个 (tz, 节假日历, session 表) 三元组 | 每次调用都要重传参数，漏一项就退化成"宿主本地时间" |
| `now` 是可 monkeypatch 的接缝，实例不可变（`frozen=True`） | 可变单例会让两个市场的判定互相串味 |
| `SessionState` 归属于该对象 | 否则枚举只能是模块级全局，任何市场都能解释任何 session |

**`SessionState`（逐字）：**

```python
class SessionState(str, enum.Enum):
    NON_TRADING_DAY = "non_trading_day"
    PRE_OPEN = "pre_open"
    OPENING_AUCTION = "opening_auction"
    OPEN = "open"
    LUNCH_BREAK = "lunch_break"
    CLOSED = "closed"
```

**各市场 session：**

| Market | ZoneInfo | 开盘集合竞价 | 上午 | 午休 | 下午 | 连续交易 |
|---|---|---|---|---|---|---|
| China A-share | `Asia/Shanghai` | 09:15–09:25 | 09:30–11:30 | 11:30–13:00 | 13:00–15:00 | 否 |
| Hong Kong | `Asia/Hong_Kong` | —（09:00–09:20 仅预开） | 09:30–12:00 | 12:00–13:00 | 13:00–16:00 | 否 |
| US | `America/New_York` | — | 09:30–16:00 | — | — | 是 |

"连续交易"是 `LUNCH_BREAK` 与 `CLOSED` 的判据：美股无午休，非交易时段一律落 `CLOSED`。

**API（逐字）：**

```python
@dataclass(frozen=True)
class MarketClock:
    market: str
    now: _dt.datetime                          # tz-aware UTC
    holidays: Optional[frozenset[_dt.date]] = None
    publication_grace_minutes: int = 30
    @property
    def tz(self) -> ZoneInfo: ...
    def local_now(self) -> _dt.datetime: ...
    def session_state(self) -> SessionState: ...
    def is_trading_day(self, d: Optional[_dt.date] = None) -> bool: ...
    def last_completed_session(self) -> _dt.date: ...   # 纯日历：收盘时刻已过
    def as_of_trading_date(self) -> _dt.date: ...       # 收盘+发布宽限，才算"应已发布"
    def publication_ready(self) -> bool: ...
    def in_run_window(self, window: tuple[_dt.time, _dt.time]) -> bool: ...

def market_now(market: str, now=None, holidays=None, publication_grace_minutes: int = 30) -> MarketClock: ...
def infer_market_from_symbols(symbols) -> Optional[str]: ...
def resolve_market(explicit, symbols) -> str: ...
```

**两个日期函数的区别（本模块最易做错处）：**

| 函数 | 语义 | 用途 | 对发布延迟 |
|---|---|---|---|
| `last_completed_session()` | **纯日历**：该市场收盘时刻已过的最近交易日 | 运行窗口门禁（§8） | 完全不关心 |
| `as_of_trading_date()` | 收盘 **+ 发布宽限** 后才承认的最近交易日 | 估值 as_of、新鲜度期望（§5、§6） | 内建 `publication_grace_minutes` |

门禁问"现在该不该跑"，估值问"现在应拿到哪一天的收盘"。**两个不同的问题不允许共用一个函数**，否则会出现"窗口内但数据未发布"或"数据已发布但窗口已关"的错配。

---

## 5. as_of 决策表（核心）

设 \(T\) 为市场本地时间、日期 \(D\)，`last_before(D)` 为 D 之前最近的已收盘交易日。

| T（市场本地，日期 D） | session 状态 | as_of | 应已发布的收盘 | 行为 |
|---|---|---|---|---|
| 开盘前 / 集合竞价 | `PRE_OPEN` / `OPENING_AUCTION` | `last_before(D)` = D−1 | D−1 | 允许 |
| 盘中（含午休） | `OPEN` / `LUNCH_BREAK` | `last_before(D)` = D−1 | D−1 | **默认拒绝**（`--allow-intraday` 可强制，但必须告警） |
| 收盘 .. 收盘+grace | `CLOSED`（未 `publication_ready`） | `last_before(D)` = D−1 | D−1 | 告警；**不得信任当日 bar** |
| ≥ 收盘+grace | `CLOSED` | D | D | 允许 |
| 周末 / 节假日 | `NON_TRADING_DAY` | D 之前最近交易日 | 同左 | 允许 |

**显式规则：盘中运行不得使用当日未完成的日线 bar。** 这与"14:00 跑一次"不矛盾——5m 日内路径是**独立且被显式标注**的机制：`common/intraday_features.py:770–791` 把当日部分 bar 标成 `Is_Partial_Bar=1`。该 bar 只作**执行价参考**，永不充当估值收盘价：

| 用途 | 数据源 | 允许盘中 |
|---|---|---|
| 估值 / 盯市 NAV / 权重 | 日线，必须属于 as_of 交易日 | 否 |
| 执行价引导 / 微观结构 | 5m，允许 `Is_Partial_Bar=1` | 是（受窗口与标注约束） |

---

## 6. 缓存新鲜度重构

**新签名（向后兼容）：**

```python
def is_range_covered(self, provider, symbol, start=None, end=None, now=None, interval="1d",
                     expected_last_session: Optional[datetime.date] = None) -> bool: ...
```

**判定规则：**

| `expected_last_session` | 判定 | 理由 |
|---|---|---|
| `None`（回测/历史查询） | **逐字保留现有"永不过期"行为** | 回测必须与墙钟无关、可复现 |
| 已给出（实盘） | covered ⇔ `latest_available_date >= expected_last_session` | 7 天容忍与 `last_sync_date >= current_date` 短路全部废除 |

给了期望之后，**无论刚同步过多近**，`latest_available_date < expected_last_session` 就是 INCOMPLETE——这正是本次六只标的所处状态。同一处理必须施加到 `common/duckdb_cache.py:381–396`（`is_intraday_range_covered`）与 `:501–520`（日线分支）。（原并列列出的**重复实现** `common/data.py:654–663`（`_cache_covers_requested_end`）已于 2026-10-22 作为死代码删除，见 `development_plan.md` `L2.19`，故该施加点已不存在。）

> **历史记录：这套启发式曾在仓库里存在两份**（`common/duckdb_cache.py` 与 `common/data.py`），当年要求**在同一阶段一起改**，否则 CSV 通道会继续静默返回昨日收盘。第二份实现已于 2026-10-22 经 grep 证明零生产调用方后作为死代码删除（见 `development_plan.md` `L2.19`），当前只剩 `common/duckdb_cache.py` 一份——不要重新造出第二份。

**发布宽限 + 有界重试：**

| 参数 | 默认 | 语义 |
|---|---|---|
| `publication_grace_minutes` | 30（China/HK）、15（US） | 收盘后多久认为上游"应当已发布" |
| `max_probe_attempts` | 3 | 重试上限 |
| 退避 | `0.5 · 2^n` 秒 | 指数退避，**仅 LIVE 模式** |

重取后仍返回 `< expected_last_session` 时重试至多 `max_probe_attempts` 次；每次探测回写 metadata 列。

**Schema 增列**（经 `_init_tables` 内的防御性 `_ensure_column` 完成，因 `ADD COLUMN IF NOT EXISTS` 在不同 DuckDB 版本支持不一）：

```sql
ALTER TABLE asset_sync_metadata ADD COLUMN expected_last_session DATE;
ALTER TABLE asset_sync_metadata ADD COLUMN probe_attempts INTEGER DEFAULT 0;
ALTER TABLE asset_sync_metadata ADD COLUMN last_probe_result VARCHAR;   -- 'fresh' | 'stale' | 'empty' | 'error'
ALTER TABLE asset_sync_metadata ADD COLUMN last_probe_time TIMESTAMP;
```

`intraday_sync_metadata` 增同四列。**无需数据迁移**：既有 `latest_available_date` / `last_sync_time` 由新判定重新解释。

**`last_sync_time` 口径。** 当前四处写裸 `datetime.datetime.now()`（`:627, 721, 996, 1148`），从今往后一律写 **tz-aware UTC**。历史行无法追溯修正，必须视为语义不明，因此：**任何逻辑都不得依赖 `last_sync_time` 的绝对数值，只依赖 `latest_available_date`。**

---

## 7. 取价的唯一所有者：`common/freshness.py`

```python
class StalePriceError(ValueError): ...
def latest_close(universe: Dict[str, pd.DataFrame], sym: str,
                 expected_session: Optional[str], strict: bool) -> float: ...
```

| 输入 | 行为 |
|---|---|
| `expected_session is None` | 遗留永不抛错路径，等价 `.iloc[-1]`（回测/研究） |
| `strict=True` 且 bar 日期 `< expected_session` | 抛 `StalePriceError` |
| `strict=False` | 高声告警 + 返回陈旧收盘（操作者显式 `--allow-stale-prices` 的降级） |
| 返回值 | 恒定 `float`，不返回 tuple/dict |

**调用点形状：**

```python
if sym in universe and not universe[sym].empty:
    price = latest_close(universe, sym, as_of_date, strict=not args.allow_stale_prices)
else:
    price = 1.0
```

"空数据 / universe 未命中"的兜底**留在调用点**——**"完全没有数据" ≠ "数据陈旧"**。前者是调用者契约（如现金代理 `price = 1.0`），后者才是新鲜度问题；混为一谈会让陈旧数据借道空数据分支蒙混过关。

**必须避免的形态：**

```python
# 反模式：不要这样
price = latest_close(universe, sym, as_of_date, strict=True)
if universe[sym].index[-1].date() != as_of_date:   # ← 第二份、会漂移的判断
    raise RuntimeError("stale")
```

在 `latest_close` 旁边再放一段内联 `if df.index[-1].date() != as_of_date: raise`，立刻产生**两个相互漂移的检查**：一处改了严格性、另一处没改，于是"有时拦得住有时拦不住"——正是本次要消灭的失败类型。**判断必须有且只有一个所有者。**

---

## 8. 运行窗口门禁与 CLI

```python
def assert_run_window(clock, stage, window, *, allow_intraday=False, strict=True): ...
```

抛 `RunWindowError`，消息必须同时含 **stage、market、market-local time、session state、expected window**：只报"不在窗口内"的操作性为零，操作者需要知道现在是什么市场时间、处于会话哪一段、窗口何时开。

| Flag | 默认 | 用途 |
|---|---|---|
| `--market {china,hk,us}` | 由 universe 后缀推断；混合则报错 | 权威市场 |
| `--as-of-date` | 由 `as_of_trading_date(market)` 计算 | 显式覆盖，仍受时钟校验 |
| `--allow-intraday` | `False` | 允许盘中运行（回退到上一已收盘交易日并告警） |
| `--strict-market-clock` | 实盘 `True` | 窗口外硬拒绝；`False` 降级为告警 |
| `--strict-freshness` | 实盘 `True` | 任一标的 bar 日期 ≠ 预期即硬失败 |
| `--publication-grace-minutes` | 30（中/港）/15（美） | 覆盖发布宽限 |

**窗口默认值（市场本地时间）**：Stage 1 `(13:55, 14:20)`；Stage 2 `(14:10, close)`→China `(14:10, 15:00)`、HK `(14:10, 16:00)`、US `(14:10, 16:00)`。必须经 `clock.local_now()` 归一化后再比较。

**现存字面量的来历。** 仓库里已有的 `"14:15"`（`run_live_strategy.py:826`）与 `"14:00"`（`check_live_portfolio_health.py:165–166`）是**裸的、面向 A 股的字符串**，不带任何时区。本文反复提到的"14:00–14:15 窗口"就源自这两个 `argparse` 默认值——它**从未由任何时钟计算得出**。这也是 10:05 运行毫无告警的原因：链路上根本不存在"当前时刻是否在窗口内"的比较。

---

## 9. 可观测性

每张工单/报表必须打印并持久化：

| 字段 | 说明 |
|---|---|
| UTC 时间戳 | 唯一绝对锚，跨宿主可对齐 |
| 市场本地时间戳 | 操作者可读锚（带 tz 偏移） |
| market + session state | 如 `china/open` 的二元组 |
| `as_of_trading_date` | 本次估值依据的交易日 |
| 逐标的 price bar date | 每个标的实际使用的 bar 属哪一天 |
| freshness PASS/FAIL 汇总 | **逐条列出** bar 日期 ≠ as_of 的标的 |

落地：在 `common/market_clock.py` 提供 `ClockStamp` + `render_clock_stamp()`，两个 stage 脚本开头各打印一次，并把字段写入 `stage1_health_report.json` / `summary.json`。

**0 行日内数据的假成功（必须显式修掉）。** 当 `data_provider=fuyao` 时 5m 在结构上不可能取得：

| 环节 | 行为 |
|---|---|
| `common/financial_api.py:753–754` | 对任何 `interval != "1d"` 直接 `raise` |
| `common/intraday_features.py:849–853` | 逐标的 `except` 吞掉 |
| 结果 | 日内 frame 为空，`intraday_vwap` / `chan_5m_state` 全为 null 或 `"N/A"` |
| `scripts/run_live_strategy.py:1241` | 仍打印 `成功加载并聚合 5m 日内特征 (标的数: 20)`，而该 20 是**日线**标的数 |
| `scripts/check_live_portfolio_health.py:925–954` | NaN→默认值，产出 `intraday_vwap_breadth=1.0`，一个**通过**的 breadth 门槛值 |

**规则：** 统计 `as_of_date` 当日**真实 5m 行数**（不是标的数）；为 0 时置 `intraday_5m_info["enabled"] = False`、打 `WARNING`、**不得**打印"成功"字样，所有依赖日内的门槛返回 `UNKNOWN` / `SKIPPED`，**绝不静默 PASS**。

---

## 10. 节假日历：必须能表达 UNKNOWN（设计硬约束）

| 事实 | 锚点 |
|---|---|
| 唯一在库日历 `get_trading_days_calendar() -> set[datetime.date]` | `common/financial_api.py:176` |
| 缓存 `_TRADING_DAYS_CACHE` | `common/financial_api.py:173` |
| **仅覆盖中国 A 股**，缓存未命中需联网（`import fuyao_client`） | 实现内固定调用 `fuyao_client.calendar_trading_days()` |
| **任何异常或空结果都返回空 `set()`** | `common/financial_api.py:197` 附近 |
| 调用方随后静默退化为 weekday 推断 | 如 `common/duckdb_cache.py:547–553` |

**后果（与本次事故同类）：** 返回值里 **"今天没有节假日" 与 "日历取不到" 完全无法区分**。对一个**授权下单**的门禁，这是与陈旧价格**同一种**静默降级：日历挂了 → 门禁照样通过 → 什么都没记录。

> **硬约束：节假日数据源必须能表达 UNKNOWN**——例如返回 `(holidays, is_trustworthy)` 二元组，或抛错由调用方显式选择降级。门禁**永远不得**把"日历不可用"当作"没有节假日"。

**美股/港股：**

| 项 | 状态 |
|---|---|
| 仓库内来源 | 无 |
| `exchange_calendars` / `pandas_market_calendars` | 未在任何 `pyproject.toml` / `uv.lock` 声明（已核对，零匹配） |
| 因此"不引入新重依赖"前提 | 在**配置层**成立 |
| 剩余待跑检查（L2.1） | 是否已作为传递依赖存在于 `pipeline/.venv`：`uv run python -c "import exchange_calendars"` |

| 检查结果 | 动作 |
|---|---|
| 可 import | 直接使用 |
| 不可 import | `common/market_holidays.py` 提供人工维护的 **2024–2028 静态表**，`now` 落在覆盖范围外时**高声告警** |

---

## 11. 分阶段落地

| 阶段 | 内容 | 行为变化 |
|---|---|---|
| **Phase 0** | 新增 `common/market_clock.py` + `common/market_holidays.py` + `common/freshness.py` 及完整离线测试；**暂无调用方** | 零 |
| **Phase 1** | 缓存新增可选 `expected_last_session` + schema 四列；`None` 路径与遗留行为逐位一致；由 `--strict-freshness` 控制 | 仅新开关下 |
| **Phase 2** | 脚本 as_of 切为 `as_of_trading_date(market)`；加新 flag、窗口门禁、新鲜度断言；保留 `--legacy-wallclock`（高声告警）逐字使用 `date.today()` 作回滚逃生门 | 实盘路径 |
| **Phase 3** | `web/livetrading` 采用 `as_of_trading_date` 与 tz-aware 时间戳；run 目录键改为 as-of 交易日，并提供**读兼容回退**（扫描遗留本地日期目录） | Web 归档 |

**回测护栏：** `backtester/run_backtest.py`、`pipeline/*`、`common/order_netting.py`、`run_live_multi_strategy.py` **永远不得**用真实时钟调用 `market_now` / `as_of_trading_date`，须保持显式 `as_of_date` / `end` 且 `expected_last_session=None`；并加一条 backtest smoke 断言：回测阶段被触达的**仅有 `None` 路径**。

---

## 12. 离线测试清单

新建 `common/tests/test_market_clock.py` 与一个实盘新鲜度测试文件；所有用例通过 monkeypatch clock 实例的 `now` 冻结时刻（`freezegun` 不是本项目 dev 依赖，也无需引入）。

**前提注记：** 下列冻结时刻均假定宿主为**固定 UTC−7**。若宿主对 DST 敏感（`America/Los_Angeles`），夹具必须由 `ZoneInfo("America/Los_Angeles")` 构造而非写死 `−07:00` 偏移，这样意图才能穿过季节性切换（见 §2.1、用例 13）。

| # | 冻结时刻 | 期望 |
|---|---|---|
| 1 | UTC−7 宿主看北京 09:59 → `2026-10-23T01:59:00Z` | `OPEN`；`as_of=2026-10-22`；Stage 2 在 strict 下拒绝，`--allow-intraday` 下告警并按 10-22 估值 |
| 2 | 北京 15:05 收盘未发布 → `2026-10-23T07:05:00Z` | `CLOSED`；`as_of=2026-10-22` |
| 3 | 北京 15:30 已发布 → `2026-10-23T07:30:00Z` | `CLOSED`；`as_of=2026-10-23` |
| 4 | 周六 | `NON_TRADING_DAY`；`as_of` = 上一个周五 |
| 5 | 注入的中国节假日 | `NON_TRADING_DAY`；`as_of` 回退至上一交易日 |
| 6 | 美股 06:00 PT → `2026-10-23T13:00:00Z` | `PRE_OPEN`；`as_of=2026-10-22`（**旧 `date.today()` 在此返回 10-23**） |
| 7 | 美股 14:00 PT → `2026-10-23T21:00:00Z` | `CLOSED`；`as_of=2026-10-23` |
| 8 | `America/New_York` DST 边界 2026-03-08 / 2026-11-01 | 偏移分别 `-05:00` / `-04:00`；session 与 as_of 稳定 |
| 9 | **2026-10-22 六标的陈旧场景回归** | 6 只 `latest_available_date=2026-10-21`、14 只 `2026-10-22`；`expected_last_session=2026-10-22`⇒6 只 `is_range_covered` 为 `False`、14 只为 `True`；`latest_close(strict=True)` 对 6 只**抛错**而非返回 10-21 收盘。这是"大声失败"回归 |
| 10 | 回测非回归 | `expected_last_session=None` 在代表性区间上与改造前启发式结果**完全一致** |
| 11 | 0 行日内 | `enabled=False` + `WARNING` + 无"成功"字样 + 相关标的出现在 `freshness_summary` |
| 12 | **节假日历 UNKNOWN**（§10） | 日历源抛错/不可信 ⇒ 门禁报 `UNKNOWN`/跳过并记录原因，**不**表现为"无节假日" |
| 13 | **宿主时区标识敏感性** | 若宿主为 `America/Los_Angeles`，则冬季本地 23:30（`2027-01-15T07:30:00Z` ⇒ PT 标准时间 1 月 14 日 23:30）时 `date.today()` 给出的 as_of 与 `as_of_trading_date(market="china")` **不一致**；夹具必须用 `ZoneInfo("America/Los_Angeles")` 而非写死 `−08:00`。若宿主为固定 `Etc/GMT+7`，构造同一时刻的等价夹具并断言两者**一致** |

---

## 13. 与在途修复的关系（避免两套判断）

`scripts/run_live_strategy.py:1436` 处有一份并行落地的临时修复（bar 日期 ≠ `as_of_date` ⇒ 抛错，`--allow-stale-prices` 可降级）。校对结果：该修复已落地，锚点由 `:1436` 下移至约 `:1550`，实现为 `latest_close(universe, sym, as_of_date, strict=not getattr(args, "allow_stale_prices", False))`，边界模块为 `common/freshness.py`。

**约束：该修复必须写在 §7 的 `common/freshness.py::latest_close` 边界上**，于是 L2.3 只是"函数体替换（已在模块内）+ 调用点一个 token 变化"：`expected_session=as_of_date` → `expected_session=as_of_trading_date(...)`，**不得新增第二套检查**。若另起一套判断，就退化成 §7 的"两个漂移的检查"，把本次事故的失败类型原样重造。

---

## 14. 风险与待确认

| # | 开放决策 | 建议 |
|---|---|---|
| 1 | 美股/港股节假日来源（静态表 vs 已存在依赖） | 先跑一次 import 检查；有则用，无则静态表 2024–2028 + 越界告警 |
| 2 | 盘中运行策略 | 不按"是否盘中"判，按窗口判：中国 `13:55`–`14:20` 放行，窗口外拒绝 |
| 3 | 混合市场 universe | 无 `--market` 时拒绝；给了则逐标的按其所属市场的 expected session 估值 |
| 4 | 发布宽限期时长（30/15 分钟）为**估计值** | 需用上游真实发布延迟校准，校准前按估计值并披露 |
| 5 | 已持久化的 naive 时间戳与旧归档目录 | 不重写，仅从今往后修正；历史数据按"语义不明"处理 |
| 6 | `resolve_exit_cooldown` / `check_missing_trading_days` 仅用中国日历 | 非中国策略存在**既有**正确性缺口；先记录，**不在本专项修复** |
| 7 | **宿主时区标识**（固定 UTC−7 vs `America/Los_Angeles`） | 未确认：决定 §2.1 不变量是否季节性失效，以及 §12 测试夹具的时区写法；先跑 `timedatectl` 定案 |

---

## 15. 任务拆分（供 `docs/development_plan.md` 引用）

| 任务编号 | 任务名称 | 核心文件与符号 | 验收标准 | 配套离线单测 | 规模 | 前置依赖 |
|---|---|---|---|---|---|---|
| L2.1 | 市场时钟抽象 + 静态节假日表 | `common/market_clock.py`, `common/market_holidays.py` | `market_now` / `as_of_trading_date` / `session_state` / `infer_market_from_symbols` 全部函数；`zoneinfo` 无新依赖；§5 决策表逐行断言 | `test_market_clock.py` 用例 1–8 | M | 无 |
| L2.2 | 缓存期望式新鲜度重构 | `common/duckdb_cache.py`（`is_range_covered`、`is_intraday_range_covered`、`_ensure_column`） | `expected_last_session` 参数 + 4 列迁移；`None` 路径与旧行为逐位一致；live 路径废除 7 天启发式；原计划的"两处重复启发式同批修复"已不适用——重复实现 `common/data.py::_cache_covers_requested_end` 已于 2026-10-22 作为死代码删除（见 `development_plan.md` `L2.19`），`is_range_covered` 为唯一新鲜度闸门 | 用例 9、10 | L | L2.1 |
| L2.3 | 实盘脚本 as-of 解耦 + 窗口门禁 + 硬新鲜度断言 | `scripts/run_live_strategy.py:826, 1043, 1436`, `scripts/check_live_portfolio_health.py:165–166, 814`, `common/freshness.py` | 五个新 flag；盘中默认拒绝；`StalePriceError` 取代 `.iloc[-1]`；Literal 时点改为市场本地窗口 | 用例 1–7、9 | L | L2.2 |
| L2.4 | 可观测性与 0 行日内假成功修复 | `web/livetrading/execution_service.py`, stage1/stage2 日志, `summary.json` | tz-aware UTC + 市场本地 + session + as_of + `freshness_summary`；0 行 5m 时 `enabled=False` 并 WARN；归档目录键改用 as_of | 用例 11 | M | L2.3 |
| L2.5 | Web 站点与回测回归护栏 | `web/livetrading/{storage,server,execution_service}.py`, `backtester/run_backtest.py` | 归档键统一；回测断言 `expected_last_session=None` 为唯一路径 | `test_web_livetrading.py` + 回测 smoke | M | L2.3 |

---

## 参考

行号取自 2026-10-22 快照；`scripts/run_live_strategy.py`、`common/duckdb_cache.py` 在写作期间正被并行修复，行号会随后移动。

| 文件 | 锚点 | 内容 |
|---|---|---|
| `common/duckdb_cache.py` | `361–362`, `381–396`(387/396), `489–490`, `501–520`(508/520), `547–553` | 宿主本地 now 锚点、7 天容忍、`weekday() < 5` |
| `common/duckdb_cache.py` | `627, 721, 996, 1148` | 裸 `datetime.datetime.now()` 写 `last_sync_time` |
| `common/data.py` | `654–663`(659/663), `685–729` | `_cache_covers_requested_end`（重复启发式；已于 2026-10-22 删除）、fast-hit |
| `common/cli_utils.py` / `common/scheduling.py` | 107 / 8 | 仅文档注释 / index 驱动调度（均无需修改） |
| `scripts/run_live_strategy.py` | `469`, `352–356`, `826`, `1043`, `1241`, `1436`, `1483–1488` | `date.today()` 兜底、哨兵、cutoff 字面量、lookback、假成功日志、陈旧兜底、后缀推断 |
| `scripts/check_live_portfolio_health.py` | `4`, `165–166`, `814`, `818`, `925–954`, `974` | 写死窗口 docstring、cutoff 字面量、门禁日期、`start_date`、NaN 假通过、裸字面量比较 |
| `web/livetrading/storage.py` | `282, 821, 861, 1570` | runs 根目录与宿主本地日期兜底 |
| `web/livetrading/server.py` / `execution_service.py` | 93/285/296 与 133/298/444, 67/236/394/531 | 默认日期兜底、归档键兜底、裸 `datetime.now()` |
| `common/financial_api.py` | `173`, `176`, `197`, `753–754` | 日历缓存、`get_trading_days_calendar`、空 `set()`、非日线抛错 |
| `common/intraday_features.py` | `770–791`, `849–853` | `Is_Partial_Bar=1` 标注、逐标的吞异常 |
| `common/freshness.py` | 全文 | `StalePriceError` / `latest_close` / `is_stale_bar` 边界 |
