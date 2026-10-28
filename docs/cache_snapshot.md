# 行情缓存快照（`data/cache.duckdb`）

> **用途**：在跑手动 walkforward 回测**之前**把行情缓存钉成一个可复核的参照点；跑完（或日后怀疑数字漂移）时按 §7 重跑同一组 SQL、逐列对比，即可回答"数据变了没有、变在哪一列"。
> **测量时间**：**2026-10-24 19:51–19:53 PDT（UTC−7，本机时区）**。以下每一个数字都出自本轮实测，SQL 原样附在对应小节；**未执行或无法测得的项目一律写「未测」**，不从任何既往报告、台账或 `docs/version.md` 转抄。
> **解释器**：`pipeline/.venv/bin/python`（Python **3.11.15**）、`duckdb` **1.5.5**。全部连接形态恒为 `duckdb.connect("<path>", read_only=True)` —— 未 `ATTACH`、未建表、未 `CHECKPOINT`/`EXPORT`/`VACUUM`，未把库拷进仓库（草稿脚本只落在 `/tmp/opencode`）。

## 1. 库文件事实

| 项 | 实测值 |
|---|---|
| 路径 | `data/cache.duckdb`（绝对：`/home/stone/Work/github/quant/data/cache.duckdb`，由 `duckdb_databases()` 的 `path` 列确认） |
| 大小 | **324,022,272** 字节 = **310 MiB**（库内 `PRAGMA database_size` 报 `309.0 MiB`） |
| mtime | **2026-10-24 12:55:25.615392116 −0700** |
| md5 | **`ac7b5df94ad12481bd8d5e8bae54d6d0`** |
| sha256（旁证） | `9e475f33f2d05ad245342b3b6bb3b0ac5b3f8ece9189c6613ed5621676fd6866` —— 与 `docs/version.md` 记载的 2026-10-24 修复**后**指纹一致，即本快照取的是"修复已完成"的库 |
| 是否被 git 忽略 | **是**：`.gitignore` **第 5 行** `**/data/*.duckdb*`（同文件第 4 行另忽略 `**/data/*.csv`） |
| 同目录备份 | **6 个**，每个 **323,235,840** 字节，合计 **1,939,415,040** 字节（**1.9 GiB**，`du -cb`） |
| `*.wal` | **不存在**。`ls data/*.wal` 原文报错：`ls: cannot access 'data/*.wal': No such file or directory` |

备份清单（`ls -la --time-style=long-iso data/`）：

| 文件 | 字节 | mtime |
|---|---|---|
| `cache.duckdb.bak` | 323,235,840 | 2026-10-21 15:12 |
| `cache.duckdb.bak.20261022-213155` | 323,235,840 | 2026-10-22 19:02 |
| `cache.duckdb.bak.20261022-215322` | 323,235,840 | 2026-10-22 21:34 |
| `cache.duckdb.bak.20261023-211859` | 323,235,840 | 2026-10-22 21:53 |
| `cache.duckdb.bak.20261023-212614` | 323,235,840 | 2026-10-22 21:53 |
| `cache.duckdb.bak.20261024-125524` | 323,235,840 | 2026-10-23 21:38 |

> 无 `.wal` 的含义：只读打开**不会**产生 WAL；反过来"没有 `.wal`"也不证明库没被写过 —— 唯一可靠的"没变"证据是 §8 的前后 md5/size/mtime 三一致。

## 2. 结构

```sql
SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1;
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='<t>' ORDER BY ordinal_position;
```

| 表 | 行数 | 列（压缩） | 自然键 |
|---|---|---|---|
| `ohlcv_bars` | **568,474** | `provider, symbol, date, open, high, low, close, volume` | `(provider, symbol, date)` |
| `intraday_ohlcv_bars` | **2,460,576** | `provider, symbol, interval, ts, open, high, low, close, volume` | `(provider, symbol, interval, ts)` |
| `asset_sync_metadata` | **318** | `provider, symbol, earliest_available_date, is_earliest_known, latest_available_date, last_sync_time, expected_last_session, probe_attempts, last_probe_result, last_probe_time` | `(provider, symbol)` |
| `intraday_sync_metadata` | **46** | `provider, symbol, interval, earliest_available_ts, is_earliest_known, latest_available_ts, latest_close_ref, last_sync_time, expected_last_session, probe_attempts, last_probe_result, last_probe_time` | `(provider, symbol, interval)` |
| `stock_fundamentals_annual` | **0（当前为空）** | `provider, symbol` + 18 个基本面字段（`report_year, stat_date, roe, net_profit, …, market_cap`）+ `last_sync_time` | `(provider, symbol, report_year)`（推定，未测） |
| `stock_industry_metadata` | **0（当前为空）** | `provider, symbol, industry_name, industry_tier, terminal_pe, is_blacklisted, blacklist_reason, last_sync_time` | `(provider, symbol)`（推定，未测） |

布局说明：**长表（窄表）**，一行一根 bar，`provider` 是主维度而非列名后缀 —— 同一 `symbol` 在不同 provider 下是**独立序列**（见 §6）。`asset_sync_metadata` 的 **318 行恰好等于** `ohlcv_bars` 的 `(provider,symbol)` 组数，即元数据一序列一行、无孤行无缺行（§3 实测）。

（2026-10-27 复核更正：本列清单为 M3.6 迁移前快照；当前已增 `amount`/`adjustflag` 列与新增 `adjust_factors` 表，见 §11 括注与 `docs/version.md` §22.2 第 1 条）

## 3. 覆盖

```sql
WITH per AS (SELECT provider, symbol, count(*) AS n, min(date) AS dmin, max(date) AS dmax
             FROM ohlcv_bars GROUP BY 1,2)
SELECT provider, count(*) AS series, sum(n) AS rows, min(n) AS n_min,
       CAST(median(n) AS BIGINT) AS n_median, max(n) AS n_max,
       count(*) FILTER (WHERE n < 250) AS series_lt250,
       min(dmin) AS first_date, max(dmax) AS last_date
FROM per GROUP BY provider ORDER BY provider;
```

| provider | 序列 | 行数 | n_min | n_中位 | n_max | 历史 <250 行 | 最早 | 最新 |
|---|---|---|---|---|---|---|---|---|
| `BaoStockDataProvider` | 7 | 18,311 | 2,611 | 2,617 | 2,618 | 0 | 2016-01-04 | 2026-10-16 |
| `EastMoneyDataProvider` | 7 | 18,311 | 2,611 | 2,617 | 2,618 | 0 | 2016-01-04 | 2026-10-16 |
| `FuyaoDataProvider` | **160** | **267,777** | **20** | 1,397 | 2,618 | **1** | 2016-01-04 | **2026-10-22** |
| `MarketDBDataProvider` | 79 | 105,950 | 242 | 1,455 | 2,618 | **2** | 2016-01-04 | 2026-10-16 |
| `YFinanceDataProvider` | 65 | 158,125 | 1,155 | 2,766 | 3,292 | 0 | **2012-11-29** | 2025-12-31 |

全库：`SELECT min(date), max(date), count(DISTINCT symbol), count(DISTINCT provider) FROM ohlcv_bars;`
→ **2012-11-29 / 2026-10-22 / 203 个 `symbol` / 5 个 provider**；合计 **318 条序列**、**568,474 行**、**3,567 个不同交易日**。
（203 < 318 是因为 **80 个 `symbol` 被多 provider 重复持有**：`SELECT count(*) FROM (SELECT symbol FROM ohlcv_bars GROUP BY 1 HAVING count(DISTINCT provider)>1)` → **80**。）

历史 <250 行的 3 条序列（`WHERE n < 250`）：`Fuyao/601088` **20 行**（2026-09-14→2026-10-19）、`MarketDB/000001.SZ` **242 行**、`MarketDB/600519.SH` **242 行**（两者均 2023-01-03→2023-12-29）。

## 4. 新鲜度

```sql
SELECT current_date AS today, max(date) AS db_max,
       date_diff('day', max(date), current_date) AS days_behind FROM ohlcv_bars;
```

全库最新日线 **2026-10-22**，距今 **2 个日历日**（今天 2026-10-24，周六）。但**这个日期只由少数标的撑着** —— 按标的的最后数据日分布（`GROUP BY` per-series `max(date)`）：

| 最后数据日 | 距全库最大日 | 序列数 | 构成 |
|---|---|---|---|
| **2026-10-22** | **0 天** | **20** | 全部 `Fuyao` |
| 2026-10-20 | 2 | 3 | Fuyao |
| 2026-10-19 | 3 | 1 | Fuyao |
| 2026-10-16 | 6 | 54 | BaoStock 7 + EastMoney 7 + Fuyao 11 + MarketDB 29 |
| 2026-09-28 | 24 | 1 | MarketDB |
| 2026-09-18 | 34 | 1 | Fuyao |
| **2026-09-01** | **51** | **83** | 全部 Fuyao |
| **2025-12-31** | **295** | **149** | Fuyao 39 + MarketDB 46 + YFinance 64 |
| 2025-12-29 | 297 | 1 | MarketDB |
| 2025-08-25 | 423 | 2 | Fuyao（`000001.SZ`、`002415.SZ`，各 641 行） |
| 2024-08-05 | 808 | 1 | YFinance（`TMF`） |
| 2023-12-29 | 1,028 | 2 | MarketDB（`000001.SZ`、`600519.SH`） |

```sql
-- 每个交易日"真的有多少标的"（截面宽度）
SELECT date, count(DISTINCT symbol) AS symbols, count(*) AS rows
FROM ohlcv_bars WHERE date >= DATE '2026-08-25' GROUP BY 1 ORDER BY 1;
```
结果为：**2026-08-25…09-01 = 118 个标的** → **09-02 起 35 个** → **10-19 = 24** → **10-20 = 23** → **10-21 = 20** → **10-22 = 20**。

日内库：`SELECT max(ts) FROM intraday_ohlcv_bars;` → **2026-10-16 15:00**（BaoStock 与 EastMoney 各 23 序列、各 1,230,288 行，`interval` 全为 `5m`）。日线表里晚于该时点的交易日有 **4 个**（`2026-10-19/20/21/22`）：

```sql
SELECT DISTINCT date FROM ohlcv_bars WHERE date > DATE '2026-10-16' ORDER BY 1;
```

同步元数据与事实的对照：`asset_sync_metadata` 的 `latest_available_date` 与各序列 `max(date)` **逐行相等，0 处不一致**（318/318）；`intraday_sync_metadata.latest_available_ts` 与 `max(ts)` 亦**逐行相等**，`ts` 超出元数据的行数 **0**。**但**两张表的 `expected_last_session` **全部为 NULL**（318 行 / 46 行各计 0 个非空）—— 见 §6 第 5 条。

## 5. 数据质量检查

| 检查 | 结果 | SQL |
|---|---|---|
| 自然键重复（日线） | **0 组** | `SELECT count(*) FROM (SELECT provider,symbol,date FROM ohlcv_bars GROUP BY 1,2,3 HAVING count(*)>1);` |
| 自然键重复（日内） | **0 组** | `SELECT count(*) FROM (SELECT provider,symbol,interval,ts FROM intraday_ohlcv_bars GROUP BY 1,2,3,4 HAVING count(*)>1);` |
| 价格列空值 | **0 行**（两表、5/2 个 provider 全为 0） | `SELECT provider, count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) AS null_px FROM ohlcv_bars GROUP BY 1;`（`volume IS NULL` 亦全为 0） |
| 非正价格 | **0 行** | `... count(*) FILTER (WHERE open<=0 OR high<=0 OR low<=0 OR close<=0) ...` |
| 零/负成交量（日线） | **仅 `YFinanceDataProvider` 18 行**；其余provider 0 | `... count(*) FILTER (WHERE volume=0) AS zero_vol, count(*) FILTER (WHERE volume<0) AS neg_vol ...` |
| 零/负成交量（日内） | BaoStock **155** 行 / EastMoney **155** 行，落在 **18 个标的** | `SELECT provider, count(DISTINCT symbol), count(*) FROM intraday_ohlcv_bars WHERE volume<=0 GROUP BY 1;` |
| OHLC 逻辑矛盾 | **0 行**（两表） | `count(*) FILTER (WHERE high < greatest(open,close) OR low > least(open,close) OR high < low)` |
| 单日涨跌 >50% | **0 行**（5 个 provider 全为 0，各扫 18,311–267,777 行） | 见下方窗口 SQL |
| 断档 >20 日历日 | **13 对 / 13 条序列**（318 条中） | 见下方窗口 SQL |

```sql
-- 单日涨跌 >50%
WITH s AS (SELECT provider, symbol, date, close,
       lag(close) OVER (PARTITION BY provider, symbol ORDER BY date) AS pc FROM ohlcv_bars)
SELECT provider, count(*) FILTER (WHERE pc > 0 AND abs(close/pc-1) > 0.5) AS big_moves
FROM s GROUP BY 1;

-- 断档 >20 日历日
WITH s AS (SELECT provider, symbol, date,
       lag(date) OVER (PARTITION BY provider, symbol ORDER BY date) AS pd FROM ohlcv_bars)
SELECT provider, symbol, pd, date, date_diff('day', pd, date) AS gap_days
FROM s WHERE pd IS NOT NULL AND date_diff('day', pd, date) > 20
ORDER BY gap_days DESC;
```

断档最长的 5 条：`600309.SH` 182 天（2017-12-04→2018-06-04）、`002049.SZ` 150 天、`600050.SH` 143 天、`601872.SH` 137 天、`002812.SZ` 112 天；最新的两对是 `600000.SH` / `601872.SH` 各 73 天（2024-05-10→2024-07-22）。零成交量 18 行的构成：`002812.SZ` 15 行 + `002049.SZ` 3 行（均为 YFinance）；日内 155 行集中在 **2024-09-27 10:30–11:20**。

## 6. 坑（运行 walkforward 前必看）

1. **"最新日期"是 20 条 Fuyao 序列撑起来的假象。** 证据：`2026-10-22` 有 bar 的只有 **20 个标的**（占 203 个标的的 **9.9%**、占 318 条序列的 **6.3%**），而 `2026-09-01` 有 **118 个**标的、`2026-10-16` 有 35 个。**后果**：把窗口终点钉在"全库最大日期"时，最近截面只有 **20 只**，与 `2026-09-01` 的 **118 只**不可直接比较（基数相差 5.9 倍）—— 换手、集中度、行业暴露的分母都随之改变。
2. **按最大日期截窗口会砍掉成批标的，且被砍的成批出现。** 证据（§4 分布）：**83 条 Fuyao 序列止于 2026-09-01**、**39 条 Fuyao 止于 2025-12-31**、**64 条 YFinance 与 46 条 MarketDB 止于 2025-12-31**；`2025-12-31` 这一档共 **149 条（46.9%）**，距最大日 **295 天**。**后果**：一个"看起来覆盖到最新"的窗口，实际有近半数序列在尾部缺 10 个月；这类缺失是**按 provider/批次成块**的，不是随机缺测，会系统性偏向仍在更新的那一小撮标的。
3. **provider 之间不可互换 —— 指数层面完全一致，个股层面存在系统性基准差。** 证据：BaoStock / EastMoney / Fuyao / MarketDB 两两在 **7 个指数标的、18,311 个重叠观测**上 **最大相对差 0.0**（逐位相同）。但 **Fuyao vs MarketDB** 在 **77 个共享标的、101,556 个重叠观测**上有 **8,748 行（8.6%）不同、最大相对差 2.5781%（`600941.SH`）**，且其中 **14 个标的是恒定比例**（`max-min < 1e-12`，如 `600941.SH` 中位≈最大≈**2.5781%**、`600028.SH` **1.9886%**、`601318.SH` **1.7419%**）—— 这是**基准/复权台阶**，不是价格噪声；**YFinance vs Fuyao** 在 34,239 个重叠观测中仅 **327 行 ≤1e-9**，**1,898 行 >1%**，最大 **29.44%（`300308.SZ`，中位 28.61%）**。**后果**：同一标的换 provider 会凭空产生一个 2.6%（或 29%）的阶跃收益；跨 provider 拼接长历史等于注入假 alpha。
4. **同一资产存在两个键名（裸代码 vs 带后缀），行数差一到两个数量级。** —— **已处理（2026-10-24）**：两个裸键已从库中删除，实测与回滚方式见 **§8bis**。以下为清理前的原始记录，保留不改。证据：`Fuyao` 下 `000938`（**529 行**，2024-08-07→2026-10-16）与 `000938.SZ`（**2,332 行**，2017-01-18→2026-10-22）在 **529 个重叠日上 OHLCV 逐位相同**（`exact_matches=529`，`max_rel_diff=0.0`）；`601088`（**20 行**，2026-09-14→2026-10-19）与 `601088.SH`（**2,293 行**）在 20 个重叠日上同样逐位相同。**后果**：按裸代码取数会拿到 1/4.4 乃至 1/115 的历史；同库内同时存在两条看起来都合法、但长度差一个数量级以上的序列。

> **状态更新（2026-10-24）**：`000938`、`601088` 两个裸键已连同其 metadata 一并删除（`ohlcv_bars` 549 行 + `asset_sync_metadata` 2 行）；带后缀兄弟键未动。删除动作、前后对照、回滚点与"删除不持久"的风险见 **§8bis**。**注意**：本条描述的是清理前的库状态，若日后有调用方再用裸代码写库，本条会重新成立。
5. **同步元数据不承担新鲜度告警。** 证据：`asset_sync_metadata` / `intraday_sync_metadata` 的 `expected_last_session` **全部 NULL**（318 行、46 行各 0 个非空）；同时 `latest_available_date` 与事实 `max(date)` **100% 相等**。**后果**：元数据只忠实记录"上次同步看到了什么"，**无法**区分"已经取到最新"和"自 2025-12-31 起就没再取过"—— 止于 2025-12-31 的那 149 条序列在元数据里各自都是"自洽"的。
6. **日内库比日线库更旧，且只有 23 个标的。** 证据：日内 `max(ts)` = **2026-10-16 15:00**，日线表在该时点之后还有 **4 个**交易日（10-19/20/21/22）；日内只有 **23 个 `symbol`**（另有 1 个 `511880.SH` 只在日内库出现），而日线有 203 个。**后果**：最近 4 个交易日（10-19/20/21/22）在日内表里**一行都没有**；`511880.SH` 这类"只有日内、没有日线"的标的会让两个库的标的集合对不上（`EXCEPT` 双向差集：日内多 1 个、日线多 181 个）。

## 7. 复现方式

最小脚本（**只读**，把 §1–§6 的头条数字一次算完；存盘后在仓库根执行 `pipeline/.venv/bin/python <path>`）：

```python
import duckdb
con = duckdb.connect("data/cache.duckdb", read_only=True)   # 绝不 read_only=False
print(con.execute("SELECT current_date, max(date), date_diff('day', max(date), current_date) FROM ohlcv_bars").fetchall())
print(con.execute("""WITH per AS (SELECT provider, symbol, count(*) n, min(date) d0, max(date) d1 FROM ohlcv_bars GROUP BY 1,2)
  SELECT provider, count(*), sum(n), min(n), median(n), max(n), count(*) FILTER (WHERE n<250) FROM per GROUP BY 1 ORDER BY 1""").fetchall())
print(con.execute("""WITH per AS (SELECT provider, symbol, max(date) dmax FROM ohlcv_bars GROUP BY 1,2)
  SELECT dmax, count(*) FROM per GROUP BY 1 ORDER BY 1 DESC""").fetchall())      # 最后数据日分布
print(con.execute("""SELECT date, count(DISTINCT symbol) FROM ohlcv_bars
  WHERE date >= DATE '2026-08-25' GROUP BY 1 ORDER BY 1""").fetchall())          # 截面宽度
```

**下次比对时看这些列**（按敏感度排序）：
`md5` + `size` + `mtime`（是否被动过）→ `ohlcv_bars` 总行数 与 `(provider,symbol)` 组数（本快照为 318；**2026-10-24 清理裸代码键后为 316，见 §8bis**）→ `max(date)` → **最后数据日分布**（是否又有成批序列被截断）→ **每个交易日的截面宽度**（`10-22` 是否还只有 20 个标的）→ 各 provider 的 `n_min/中位/最大` 与 `<250 行` 计数 → 跨 provider 重叠观测的差异行数与最大相对差 → 裸代码/带后缀键对是否还在（**2026-10-24 后应为 0 对，见 §8bis**）→ 零成交量/空值/断档计数。

## 8. 只读声明

本轮全部测量均以 `duckdb.connect(..., read_only=True)` 执行，未改动库文件：查询前 `size=324,022,272` / `mtime=2026-10-24 12:55:25.615392116 -0700` / `md5=ac7b5df94ad12481bd8d5e8bae54d6d0`，查询后**三项完全一致**；全程未出现 `data/cache.duckdb.wal`。

## 8bis. 2026-10-24 清理：删除裸代码键

> **定位**：本节记录一次针对 **§6 第 4 条**（裸代码 / 带后缀重复键）的**实际数据清理**。§1–§8 的既有数字是**清理前**的实测值，保持原样不改写；本节给出清理的动作、前后对照与回滚方式。下面每个数字都是本轮实测，SQL / 命令原样附上。

### 8bis.1 删了什么、为什么

| 裸键 | 行数 | 带后缀兄弟键 | 兄弟行数 | 共享交易日 | 裸键独有日 | OHLCV 逐位相同 | metadata 行 |
|---|---:|---|---:|---:|---:|:---:|---:|
| `000938` | **529** | `000938.SZ` | 2,332 | **529** | **0** | 是（0 处不等） | 1 |
| `601088` | **20** | `601088.SH` | 2,293 | **20** | **0** | 是（0 处不等） | 1 |

判定链（五条，缺一不可）：

1. **真子集**：裸键的每一个交易日都在兄弟键里（529/529、20/20 共享），且**裸键独有日 = 0**（`NOT EXISTS` 反查）。
2. **数值等同**：重叠日上 `open, high, low, close, volume` 五列**逐位相同** —— 五列任一不等的行数 **0**，单看 `close` 不等的行数也是 **0**。
3. **长度悬殊**：裸键只有兄弟的 22.7%（529/2,332）与 0.87%（20/2,293），留着只会复现 §6.4 的"同一资产两条合法序列"歧义。
4. **裸键只此两处**：`regexp_matches(symbol,'^[0-9]{6}$')` 在 `ohlcv_bars` 命中 **2** 个、`asset_sync_metadata` 命中 **2** 个、`intraday_ohlcv_bars` **0** 个、`intraday_sync_metadata` **0** 个；`stock_fundamentals_annual` / `stock_industry_metadata` 各 **0** 个。且这 2 个裸键**只在** `FuyaoDataProvider` 下（无第二 provider 持有，故按 provider 加条件的 DELETE 覆盖面是完整的）。
5. **仓内取数走带后缀键**：裸形式只出现在散文/文档里，没有代码按裸键取数。

⇒ 裸键不提供任何兄弟键没有的信息，删除**不损失任何 bar**。

### 8bis.2 执行语句与影响行数

单事务、`read_only=False`、原子提交（`BEGIN` → 两条 `DELETE` → `COMMIT`）：

```sql
BEGIN;
DELETE FROM ohlcv_bars          WHERE provider = 'FuyaoDataProvider' AND symbol IN ('000938', '601088');
DELETE FROM asset_sync_metadata WHERE provider = 'FuyaoDataProvider' AND symbol IN ('000938', '601088');
COMMIT;
```

| 语句 | 影响行数 |
|---|---:|
| `DELETE FROM ohlcv_bars …` | **549**（529 + 20） |
| `DELETE FROM asset_sync_metadata …` | **2** |
| 合计 | **551** |

未执行 `VACUUM` / `CHECKPOINT` / `EXPORT`（空间回收不在本次范围，见 §8bis.5(i)）。

### 8bis.3 前后对照

| 指标 | 清理前 | 清理后 |
|---|---:|---:|
| `ohlcv_bars` 行数 | 568,474 | **567,925**（−549） |
| `(provider,symbol)` 序列数 | 318 | **316**（−2） |
| 其中 `Fuyao` 序列数 | 160 | **158** |
| 其中 `Fuyao` 行数 | 267,777 | **267,228**（−549） |
| `ohlcv_bars` 内不同 `symbol` | 203 | **201**（−2） |
| `asset_sync_metadata` 行数 | 318 | **316**（−2） |
| 四表行数合计 | 3,029,414 | **3,028,863**（−551） |
| `intraday_ohlcv_bars` / `intraday_sync_metadata` | 2,460,576 / 46 | **2,460,576 / 46（未变）** |
| 孤儿（metadata 无 bar / bar 无 metadata） | 0 / 0 | **0 / 0** |
| 自然键重复（日线 / metadata） | 0 / 0 | **0 / 0** |
| 文件大小 | 324,022,272 字节 | **324,022,272 字节（未变）** |
| mtime | 2026-10-24 12:55:25.615392116 −0700 | **2026-10-24 20:03:35.642546344 −0700** |
| md5 | `ac7b5df94ad12481bd8d5e8bae54d6d0` | **`840d5993e6a2aa35103472c1b3f2a1f1`** |
| `data/cache.duckdb.wal` | 不存在 | **不存在**（提交后连接已关闭） |

兄弟键完好：`000938.SZ` 仍 **2,332** 行（2017-01-18→2026-10-22）、`601088.SH` 仍 **2,293** 行（2017-01-18→2026-10-22），两者 `asset_sync_metadata` 各 1 行。

> **一处口径更正（重要，别沿用错数）**：任务书把 **3,029,414** 当作 `ohlcv_bars` 的行数，并据此推算"3,029,414 − 549 = 3,028,865"。实测 **3,029,414 是四张表行数的合计**：`568,474 + 2,460,576 + 318 + 46 = 3,029,414`（整数精确命中）。`ohlcv_bars` 自身一直是 **568,474**（与 §2 一致）。因此：
> - `ohlcv_bars` 的行数变化是 **568,474 → 567,925**（−549）；
> - **四表合计**的变化是 **3,029,414 → 3,028,863**（−551，因为两条 DELETE 跨了两张表）；
> - **"3,028,865" 这个数不存在** —— 它从四表合计里只减了 549 行、漏掉了 2 行 metadata。
>
> 同理，任务书的"distinct series 318 → 316"指的是 `(provider,symbol)` 组数（不是 `count(DISTINCT symbol)`，后者是 203 → 201）；`asset_sync_metadata` 的 318 行恰好一行一序列，两个口径在清理前同为 318、清理后同为 316，容易混。

### 8bis.4 回滚点

| 项 | 值 |
|---|---|
| 备份文件 | **`data/cache.duckdb.bak.20261024-200255`** |
| 字节 | 324,022,272 |
| md5 | **`ac7b5df94ad12481bd8d5e8bae54d6d0`**（= 删除前原库 md5，逐位相同） |
| mtime | 2026-10-24 12:55:25.615392116 −0700（`cp -p` 原样保留） |
| 生成方式 | `cp -p data/cache.duckdb data/cache.duckdb.bak.$(date +%Y%m%d-%H%M%S)`，随后 `md5sum` 校验等于删除前 md5 |

回滚（一条命令）：

```bash
cp -p data/cache.duckdb.bak.20261024-200255 data/cache.duckdb
```

> 注意体积差异：本备份 **324,022,272** 字节，比 §1 表里 6 个旧备份（各 323,235,840 字节）大约 **767 KiB**。旧备份取自 2026-10-24 qfq 修复**之前**，本备份取自修复**之后**（md5 与 §1 的 `ac7b5df…` 一致），**两者不能互相替代**。

### 8bis.5 两条必须记住的保留意见

**(i) 删除不缩小文件。** `size` 在 DELETE 前后都是 **324,022,272 字节**（= 310 MiB 十进；库内 `PRAGMA database_size` 前后都是 `309.0 MiB`）。DuckDB 的 `DELETE` 只在行组上做标记、不把空间还给操作系统；不 `VACUUM` / `EXPORT`（本次明确不做）文件就不会变小。任何"清理后应该省了 551 行的空间"的期待都是错的。

**(ii) 删除不持久 —— 裸键会被重新写回。** `common/data.py:731` 的落库调用是 `self.duckdb_cache.store_bars(provider_name, symbol, …)`，用的是**调用方传入的原始 `symbol`**；`normalize_thscode()`（`common/financial_api.py:103`，裸 `000938` → `000938.SZ`）只在 provider 的**上游取数**里把代码规范化成 thscode 去查接口，**不会**改写 `data.py` 落库时用的键名（全仓 `common/data.py` 里对 `normalize_thscode` 零命中）。⇒ 任何调用方只要再传一次裸代码，`000938` / `601088` 就会被原样重建。**写入侧守卫（落库前强制规范化或显式拒绝裸键）本次没有实现**，本轮只清存量。要根治得改 `common/data.py` / `common/duckdb_cache.py` 的写入路径。该持久化修复现已登记为任务 **`L2.46`**（[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.13，Track L-A / Tier P1），**守卫尚未实现** —— 在本节与 §15 的口径下，裸键随时可能被下一次裸代码写入重建。

### 8bis.6 对普查工具的影响（旧报告里的 160 / 83 已是历史值）

`scripts/audit_qfq_alignment.py` 的 `census_coverage()` 用 `SELECT DISTINCT symbol FROM ohlcv_bars WHERE provider = 'FuyaoDataProvider'` 枚举序列，故清理后：

| 指标 | 清理前（对备份 `.bak.20261024-200255` 实测） | 清理后（对当前库实测） |
|---|---:|---:|
| `Fuyao` 序列数 | 160 | **158** |
| `with_reference` | 77 | **77**（未变） |
| `no_reference` | **83** | **81** |
| `thin_reference` | 0 | **0** |
| `judgeable` | 77 | **77**（未变） |

`000938`、`601088` 在清理前就在 `no_reference` 名单里（参考源 `MarketDBDataProvider` 没有这两个键），所以删掉后 `no_reference` 正好少 2、`judgeable` 不变。

⇒ **凡旧文档 / 旧报告说"`Fuyao` 160 条序列"或"`no_reference` 83"的，都是清理前的历史值**；`scripts/repair_qfq_alignment.py` 同样按 `SELECT DISTINCT symbol` 取数，口径一致。§4 的"最后数据日分布"表与 §6.2 里"83 条 Fuyao 序列止于 2026-09-01"是**另一回事**（指最后数据日分布，不是 no_reference 名单），不要与本节的 83 → 81 混读。清理后该分布只有两处变动，实测为：**`2026-09-01` 一档仍是 83（未变）**；`2026-10-19` 那一档（1 条 Fuyao，即裸 `601088`）**整行消失**，`2026-10-16` 由 **54 → 53**（裸 `000938` 止于此日）。其余各档不变。

---

# 下半部分：代码行为 —— 同一份库在 walkforward 里被怎么读

> **与上半部分的分工（不要混引）**：§1–§8 是**数据事实**——某一时刻这个库长什么样（md5 / 行数 / schema / 覆盖 / 质量），证据来自对 `data/cache.duckdb` 的只读 SQL，**随写入失效**。§9–§17 是**代码行为**——同一份库被 `backtester/run_backtest.py` 读进内存时发生的路径解析、并发、缺失处理与 fold 切分，证据是源码行号，**不随库内容变化**（只有改代码才会变）。两者唯一的接缝是 §15 那张"什么事件会让上半部分的 md5/行数失效"。
> **复核方式**：下文每条都给了 `path:line`，并被要求与当前 HEAD 的源码逐字对照；**与任务书不一致的三条按代码为准披露并标注**（§11 的成交量单位归属、§13 ③ 的 `_align_universe` 是否真的被调用、§13 ⑦ 的"唯一一处 ffill"计数）。

## 9. 缓存路径解析链

`--cache-dir`（`common/cli_utils.py:85-86` 注册）→ `cache_dir = args.cache_dir or DATA_DIR`（`backtester/run_backtest.py:1050`）→ `DATA_DIR = shared_data_dir()`（`backtester/run_backtest.py:64`）→ `shared_data_dir()` 返回 `<repo>/data`（`common/cli_utils.py:108`，docstring 明说"this always resolves relative to this file's own location, one level up from `common/`"）→ `DuckDBCache` 把**目录或无后缀路径**改写成 `<dir>/cache.duckdb`（`common/duckdb_cache.py:775-778`）→ 最终落在 `<repo>/data/cache.duckdb`。

- `read_only=True` **在构造时**就要求文件已存在，否则当场 `FileNotFoundError`（`common/duckdb_cache.py:784-791`，注释原文 "Fail at construction, not at the first query, when the file is absent."）。⇒ 路径写错不是"悄悄查空"，而是启动即失败。
- 反过来 `read_only=False` 会 `makedirs`（`:792`）并建好全套表（`:798-808`）。⇒ **一次拼错路径的写模式打开会凭空造出一个只有 schema 的空库**，看起来"运行正常"、其实零 bar。
- 回测/研究开的是**写模式**：`CachedDataProvider.__init__` 用 `DuckDBCache(db_path=self.cache_dir)`（`common/data.py:662`），没有 `read_only=True`。walkforward 因此既是读者也是**潜在写者**（见 §11、§14）。

## 10. 连接与并发

| 事实 | 位置 |
|---|---|
| 连接**每次调用新建、用完即关**（非池化） | `_get_connection` `common/duckdb_cache.py:810-828`；查询侧 `con = self._get_connection(read_only=True)` + `finally: … con.close()`：`query_bars` `:2053`/`:2057`、`query_universe` `:2178`/`:2182`、`query_intraday_bars` `:1995` |
| 唯一并发保护 = **写锁重试**：5 次、仅当异常文本含 `"lock"`、指数退避 | `:819-827`（`for attempt in range(5)` / `if "lock" in str(exc).lower() and attempt < 4` / `time.sleep(0.05 * (2 ** attempt))`） |
| **没有**跨进程锁、**没有** WAL 语义、**没有** fencing token | 全文对 `journal_mode` / `flock` / `fcntl` **零命中**（`grep -n` 实测），唯一同步原语就是上一条的 sleep-retry |

⇒ 读与写之间**没有一致性快照**。walkforward 读库时若另一进程正在写，你可能读到"半更新"状态（一部分标的已落新 qfq 基准、一部分还是旧基准）；那 5 次重试只保证**写者不因锁而崩**，不保证**读者看到同一个版本**。所以：**运行 walkforward 期间不要启动下载器 / 同步脚本。**

（并行 walkforward 本身不是风险：payload 合约注释原文 "no worker ever touches the OHLCV cache; so a parallel walk-forward puts exactly ONE reader on `common/duckdb_cache.py` no matter how many workers or folds there are"（`backtester/run_backtest.py:356-359`）——`--n-jobs N` 仍然只有 1 个读者。）

## 11. 复权口径与成交量单位都不在 schema 里（2026-10-27 复核更正：标题结论已失效 —— `amount`/`adjustflag` 列与 `adjust_factors` 因子表均已入 schema，详见本节正文括注与 `docs/version.md` §22.2 第 1 条）

`_init_tables` 的 `ohlcv_bars` 只有 `provider, symbol, date, open, high, low, close, volume`（`common/duckdb_cache.py:1162-1173`）——**没有任何 `adjust` / `qfq` / `adj_factor` 列**。同一 `(provider, symbol, date)` 键下数值的口径，完全由**写它的那条路径**决定（2026-10-27 复核更正：M3.6/M3.8 后 schema 已变 —— `ohlcv_bars` 增 `amount DOUBLE`（`common/duckdb_cache.py:1171`）；复权基落在 `asset_sync_metadata.adjustflag`（`:1181`）、`intraday_ohlcv_bars.adjustflag`（`:1197`）、`intraday_sync_metadata.adjustflag`（`:1210`），另新增 `adjust_factors` 因子表（`:1216-1224`）。本节标题与「无 adjust 列」结论针对 M3.8 之前旧 schema，权威口径见 `docs/version.md` §22.2 第 1 条）：

| provider | 复权口径（写入侧的写法） | 位置 |
|---|---|---|
| `BaoStockDataProvider` | 构造默认 `adjustflag: str = "2"`（前复权） | `common/financial_api.py:1411` |
| BaoStock **指数** | 强制 `req_adjustflag = "3"`（不复权），字段列表连 `adjustflag` 都不带 | `:1482` / `:1483` |
| `EastMoneyDataProvider` | `adjust="qfq"` → `fqt="1"` | `:2068`（`__init__` `:2060`） |
| `YFinanceDataProvider` | `auto_adjust=True` | `common/data.py:207` |
| `MarketDBDataProvider` / `Fuyao` | `adjust="forward"` → 读本地 `v_daily_qfq` | `common/financial_api.py` `__init__` `:355-390`（view `:374-378`）、`fetch_universe` `:478-545` |

（2026-10-27 复核更正：此表描述 M3.8 之前「直连写入原生复权价」的行为。当前默认 `derive_mode="auto"`，写侧统一为「`fetch_raw_ohlcv` 取 raw + `fetch_adjust_factors` 取因子 + `store_bars(adjustflag="raw")`」；`adjust`/`adjustflag`/`fqt` 仅用于 legacy 直连回退。MarketDB 新增 `fetch_adjust_factors` 直读 `calc_adjust_factor_daily.backward_factor`，Fuyao 纯委托本地 MarketDB，EastMoney 不实现该方法。权威口径见 `docs/version.md` §22.2 第 5 条）

⇒ **不能假设表内数值口径统一**：不复权的指数行与前复权的个股行在**同一张表**里。跨 provider 比较/拼接前必须逐列验——上半 §6 第 3 条的 2.58%（`600941.SH`）/ 29.44%（`300308.SZ`）台阶就是这条的直接后果。

**成交量单位**：全库**唯一一处显式"手→股"换算在 EastMoney 路径**，注释原文 "Mandatory lots-to-shares volume scaling (f56 is in lots of 100 shares)"，代码 `vol = float(parts[5]) * 100.0`（`common/financial_api.py:2180-2181`）。**BaoStock 路径对 `volume` 不做任何缩放**（`:1610` 只做 rename + `to_numeric`，全段无乘数），`duckdb_cache` 也不做（`store_bars` 里 `"volume": df_to_store["Volume"].astype(float).values`，`common/duckdb_cache.py:1985`；日内同，`:1728`）。⇒ **不要假设全库成交量同一单位**，`volume` 只在"同 provider 同序列"内部可比。

> **与任务书的偏差**：任务书称换算发生在 BaoStock 路径，实测不成立——BaoStock 侧零缩放，换算在 EastMoney 侧。结论（单位不统一）仍成立，归属已按代码更正。

## 12. 读路径不做补洞

- 缓存层查询**不 forward-fill**；唯一一处 `dropna` 在重采样里：`resampled = df.resample(rule).agg(agg_spec).dropna(subset=["Open","High","Low","Close"])`（`common/duckdb_cache.py:160`），日内重采样同形（`common/data.py:610`）。而 `interval="1d"` 时 `_get_resample_rule` 返回 `None`（`common/duckdb_cache.py:90-110` 的 `"1d": None`）→ **日线路径连"补齐交易日"这一步都不存在**，库里缺哪根就少哪根。
- provider 侧的坏行也是**丢**不是补：`_drop_invalid_ohlcv_rows` 做 `dropna(subset=[...])`，不可能 OHLC 的行被剔除并 `warnings.warn(f"Dropping {…} row(s) with impossible OHLC values for {label}: …")`（`common/data.py:619`、`:642`）。
- 单标的读不到 → **空 DataFrame**（带 5 个列名），不是异常：`if res_df.empty: return pd.DataFrame(columns=_REQUIRED_OHLCV_COLUMNS)`（`common/duckdb_cache.py:2060-2061`）。
- 批量读缺标的 → **`continue` 跳过**：`if sym not in grouped.groups: continue`（`:2193`；日内版 `:2122`）。上游 `fetch_universe` 对**单标的失败**是 `warnings.warn(f"Skipping {symbol}: {exc}")`（`common/data.py:815`）——不报错、不填充；**唯一硬失败**是 `HistoryRewriteRefused`，被收集后抛 `RuntimeError` 点名每个被拒 symbol（`common/data.py:818-838`，见 §14）。

⇒ 缺口会一路传到最后，靠引擎侧两处 ffill 兜底（§13 ⑦）——**"读到少一根"在结果里的表现是"那一天平盘"，不是缺失标记。**

## 13. walkforward 关键语义（最重要的一节）

**① 回测会话被钉死为 `None`。** `_LEGAL_BACKTEST_SESSION = None`（`backtester/run_backtest.py:71`），任何非 None 值（含 `0` / `""` / `False`，用 `is not` 判定）都抛 `BacktestSessionLeakError`（`:82-105`）。原因注释原文：*"a backtest must never do so, otherwise its results become a function of the wall clock (today's date decides whether a bar is 'covered' -> a re-fetch -> potentially different data and different numbers for the same command)"*（`:85-88`）。调用点带标记：`:1126`（主 universe）、`:938`（baseline）。⇒ 回测**不因"今天几号"改判覆盖度**；同理它也**不会**替你补上缺失的尾巴。

**② `_evaluate_walkforward_fold` 本身不加载数据。** 注释原文 *"Deliberately performs NO data loading: the universe is handed in already loaded."*（`:407-411`）。⇒ 全部 I/O 只发生在 `main()` 的**一次** `load_universe_with_banner`（`:1123-1126`），fold 内只有内存切片。

**③ `_align_universe` 确实是内连接，但当前 walkforward 路径不调用它。** 函数本体：`common_index = common_index.intersection(df.index)`（`:236-238`），返回 `{symbol: df.loc[common_index] …}`（`:264`）—— 即"所有标的日期取交集，单个短历史标的会把整个回测区间压到它自己的长度"。两条告警的触发条件都是**差值 > 7 天**（`:242`、`:254`，即尾部差 3 天**完全不告警**），文案为 `"_align_universe: aligned date range ends {…}, {…} day(s) short of the latest date available anywhere in the universe ({…}). Symbol(s) with the shortest trading history are limiting the whole walk-forward range: {…}."`（`:246-252`）与 `"… starts {…}, {…} day(s) later than the earliest date available anywhere … Symbol(s) listed latest are trimming the start date for the whole universe: {…}."`（`:258-263`）。**但全仓除定义与测试外无任何调用点**（定义 `:228`；唯一调用方是 `backtester/tests/test_run_backtest.py:26/40-104`；其余两处 `common/allocation_templates.py:468`、`backtester/run_backtest.py:916` 只是散文引用）。⇒ 真实运行中**不会**发生"被短历史标的压短"，**也**不会打印这两条告警；真正决定日历的是 ④⑤。若你真看到这两条告警，说明调用链被改了，值得停下查。

> **与任务书的偏差**：任务书把 `_align_universe` 当成 walkforward 的实际行为；实测它是**未接线的死代码**（仅测试覆盖），故本条按代码改写，并把"读告警"从检查清单里移除。

**④ 真实日历 = 并集 + 20% 覆盖门槛。** `_resolve_master_calendar` 先取所有标的日期的**并集**（`:306`），再只保留"至少 `min_coverage` 比例的标的当天有数据"的日期（`min_assets = max(1, int(np.ceil(min_coverage * total_assets)))`，`:311`）。**请求起始日早于该门槛日时，起始日被前移 —— 结果已改变**，告警原文：`"Requested start date {start} precedes earliest date where at least 20% of universe assets ({min_assets}/{total_assets}) have trading data ({…}). Moving start date to {…}."`（`:326-333`）。调用点把 `min_coverage=0.20` 写死：`:744`（walkforward）、`:692`（standard）、`:1286`/`:1379`（baseline）。

**⑤ fold 内静默丢弃无 bar 的标的。** `_slice_universe_for_range` 只保留"区间内至少有一根 bar"的标的，其余悄悄消失：docstring 原文 "Temporarily removes assets that have no bars for the given date range."（`:271-276`），全函数**无 `warn`**。⇒ 某 fold 少了几只不会上控制台；这些名在引擎里被 `reindex` 成 `NaN`（见 ⑦）。

**⑥ 窗口守卫直接抛异常。** `if window_bars >= n_bars or window_bars > (n_bars - base_start_idx): raise ValueError("Window size is larger than the available data.")`（`:773-774`）——硬失败，不是 warn。换算 `int(round(window_years * 252))`（`:267-268`）。

**⑦ fold 内唯一一处 ffill 是「首行播种」，不是补数据缺口。** 注释原文：*"Restrict to the eval window, but seed its first row with the carried-over (forward-filled) target as of the window's start -- otherwise a fold that starts between two buffer-period rebalances would open in all-cash instead of whatever the (now-warm) strategy actually held at that point."*（`:434-440`），代码 `eval_weights.iloc[0] = full_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]`（`:444`）。区别：它 ff 的是**仓位目标**（"新 fold 开场时我本来持有什么"），**不是价格**。价格侧由引擎兜底，语义完全不同：
- `target_weights = sparse_weights.ffill().fillna(0.0)`（`common/allocation_backtester.py:950`）—— 稀疏权重合约的"持仓延续"；`is_rebalance` 在 ffill **之前**就算好（`:944-948`），否则"重算成同一权重"会被抹成"今天什么都没发生"。
- `mark_arr = closes.ffill().fillna(0.0)`（`:1068`）—— 注释原文 *"Forward-filled so a missing bar marks at its last known close instead of poisoning equity with NaN"*，即**停牌/缺 bar 按最后收盘价平盘计价（收益 0）**。⇒ "库里有洞"在回测里表现为**一段 0 收益横盘**，既不是 NaN 也不是现金。

（standard 模式有同形的播种，`:705-706`；所以"全路径唯一一处 ffill"**不成立**——walkforward 可执行到的 ffill 共 3 处，含义各不相同，见本条与上两点。）

> **与任务书的偏差**：任务书称"全路径唯一一处 ffill"；实测 walkforward 路径上 ffill 出现 3 次（`:444`、`allocation_backtester.py:950`、`:1068`），仅第一处是播种。

**⑧ 逐 fold fail-soft。** fold 内任何异常被 `except Exception as e:  # noqa: BLE001 -- pre-B2.2 fail-soft contract, per fold` 捕获并 `return _nan_fold_metrics(), str(e)`（`:489-490`）→ 指标全 NaN（`_nan_fold_metrics`，`:383-395`）；调用方按 fold 次序打印 `"Error in window {start} to {end}: {err}"`（`:826`）与 `"[Fold i/N] Done: NaN metrics"`（`:831`），**整轮继续**。而 `_walkforward_score_fn` 只对 `np.isfinite` 的 fold 取均值（`:855-860`）——**NaN fold 不参与均值、也不会拉低均值**。⇒ **"跑完"≠"跑对"**：10 个 fold 里坏 7 个，你看到的 Sharpe 是剩下 3 个的均值，覆盖区间、换手分母、回撤深度全都变了口径。**必须回看 `Error in window` 与 `NaN metrics` 的行数。**

## 14. 两条写入路径的相反策略（决定快照何时失效）

| | 日线 `store_bars` | 日内 `store_intraday_bars` |
|---|---|---|
| 历史行数值变了 | **拒绝**：`rtol=1e-9`、`atol=0`，抛 `HistoryRewriteRefused`（`common/duckdb_cache.py:548`、`:2282`），**一行不写** | **静默清除**：`abs(new_close - ref_close) / ref_close > 1e-4` 时 `DELETE` 该 `(provider, symbol, interval)` 的**全部** bar 与 metadata，再重写（`:1801-1818`、`:1817-1823`） |
| 逃生口 | 必须 `allow_history_rewrite=True` **且**非空 `rewrite_reason`；flag 无 reason → 写入前 `ValueError`（`:2055-2057`） | 无（不需要，它自己就放行） |
| 比较范围 | 5 列全比：`open, high, low, close, volume`（`:2218`/`:2226`/`:2268`） | 只用最新一根的 `Close` 对 `latest_close_ref`（`:1801-1818`）—— 抽样一个点判定整段 |
| 不变量声明 | docstring "HISTORY-REWRITE INVARIANT (L2.32)"（`:2012`） | docstring "R4 QFQ Split/Dividend Drift Detection"（`:1743`） |

⇒ 日线侧"历史不能被偷偷改写"是**有门禁的**；日内侧"整段删掉重写"是**设计如此**。所以**日内表的 md5/行数比日线表更易变**：上半 §2 里 `intraday_ohlcv_bars` 的 2,460,576 行只在"没触发 R4 漂移"时才稳。

## 15. 快照有效期（什么会让 §1–§5 的数字失效）

| 事件 | 影响 | 代码路径 |
|---|---|---|
| 任何进程写入（含你自己并发的下载器/同步脚本） | md5、行数、mtime 变；可能读到跨版本混合 | §10；`store_bars` `common/duckdb_cache.py:1985`、`store_intraday_bars` `:1728` |
| **qfq 重基**（分红/拆股；或 Fuyao 重取数时触发 `scale = rem_c / loc_c`） | 既有行 OHLC 整体等比缩放 ⇒ 上半 §6 的跨 provider 台阶、涨跌幅检查全部变 | `common/financial_api.py:802-808`；日线写入会被门禁**拒绝**（除非走 `scripts/repair_qfq_alignment.py` 或显式 opt-in）（2026-10-27 复核更正：M3.8 后 qfq 不再重写既有已入库 OHLC —— 存储只存 raw，读时 `factor_qfq = factor_hfq[t]/factor_hfq[最新]` 派生，「既有行整体等比缩放」这一失效机制已被根除。Fuyao 的 `scale = rem_c/loc_c` 仍存在（现 `:1083-1086` / `:1233-1236`），但只对齐取数帧、不写库。权威口径见 `docs/version.md` §22.2） |
| **日内 R4 漂移** | 该序列历史被整段删除后重写 ⇒ 行数可能**减少** | `common/duckdb_cache.py:1801-1818` |
| `--no-cache` | **不改库**，但整轮回测绕开缓存直连 provider（`use_cache=not args.no_cache`，`backtester/run_backtest.py:1330`（另见 `:1885`）；`cached = bool(use_cache and cache_dir)`，`common/data.py:1487`（另见 `:1529`））⇒ 结果与上半的快照**无对应关系** | 同上 |
| 备份回滚（把 `cache.duckdb.bak*` 拷回 `cache.duckdb`） | 直接回到旧指纹；注意 6 个备份的**文件名与 mtime 并不同步**（上半 §1 表内两列明显错位） | 无代码路径，纯文件操作 |

**唯一可靠的"没变"证据**：跑前跑后 `md5sum data/cache.duckdb` + `stat -c '%s %y'` 三项一致（与上半 §8 同口径）。

## 16. 运行前检查清单（操作者）

- [ ] ① **universe 里不要同时出现同一资产的裸代码与带后缀写法**（`000938` vs `000938.SZ`）。上半 §6 第 4 条实测：两者在 529 个重叠日上**逐位相同**、长度 529 vs 2,332 —— 同时纳入等于同一资产算两次权重，长度差还会挪动 ④ 的 20% 门槛日。
- [ ] ② **显式给 `--start` / `--end`**。默认 `2015-01-01` / `2024-12-31`（`backtester/run_backtest.py:116-117`），与上半 §4"最近截面只剩 20 只"毫无关系；靠默认值跑出的区间没人复核过。
- [ ] ③ **读 `_resolve_master_calendar` 那条前移告警**（`:326-333`）；反之 `_align_universe` 的两条告警**不该出现**（§13 ③），出现即说明调用链被改。
- [ ] ④ **数一遍 `Error in window` 与 `NaN metrics`**（`:826`、`:831`）；NaN fold 不参与均值 ⇒ 报出的 Sharpe 可能只代表部分区间（§13 ⑧）。
- [ ] ⑤ **跑前记下 `md5sum data/cache.duckdb`**（当前 `ac7b5df94ad12481bd8d5e8bae54d6d0`，上半 §1），跑后再记；不一致就别把两次结果混着比。
- [ ] ⑥ **运行期间不要启动下载器 / 同步脚本**（§10：无跨进程锁、无 WAL、可能读到半更新状态）。

## 17. git 忽略的边界

`.gitignore` 只有两条与数据相关：**第 4 行** `**/data/*.csv`、**第 5 行** `**/data/*.duckdb*`；**目录本身没有任何规则**（`nl -ba .gitignore` 前 10 行无 `data/`）。实测：`git check-ignore -v data/cache.duckdb` → `.gitignore:5:**/data/*.duckdb*`；`data/` 下现有 7 个文件全被这两条覆盖，`git status --untracked-files=all data/` 为空，`git add -n data/` 无输出且退出码 0。
⇒ **别 `git add data/`**：今天它什么都不会加（也不报错），但目录没有忽略规则 —— 哪天 `data/` 里出现一个非 `*.csv`/`*.duckdb*` 的文件（日志、txt、草稿脚本），一条 `git add data/` 就会把它静默拖进索引。加文件请按**具体路径**加。
