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
4. **同一资产存在两个键名（裸代码 vs 带后缀），行数差一到两个数量级。** 证据：`Fuyao` 下 `000938`（**529 行**，2024-08-07→2026-10-16）与 `000938.SZ`（**2,332 行**，2017-01-18→2026-10-22）在 **529 个重叠日上 OHLCV 逐位相同**（`exact_matches=529`，`max_rel_diff=0.0`）；`601088`（**20 行**，2026-09-14→2026-10-19）与 `601088.SH`（**2,293 行**）在 20 个重叠日上同样逐位相同。**后果**：按裸代码取数会拿到 1/4.4 乃至 1/115 的历史；同库内同时存在两条看起来都合法、但长度差一个数量级以上的序列。
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
`md5` + `size` + `mtime`（是否被动过）→ `ohlcv_bars` 总行数 与 `(provider,symbol)` 组数（应为 318）→ `max(date)` → **最后数据日分布**（是否又有成批序列被截断）→ **每个交易日的截面宽度**（`10-22` 是否还只有 20 个标的）→ 各 provider 的 `n_min/中位/最大` 与 `<250 行` 计数 → 跨 provider 重叠观测的差异行数与最大相对差 → 裸代码/带后缀键对是否还在 → 零成交量/空值/断档计数。

## 8. 只读声明

本轮全部测量均以 `duckdb.connect(..., read_only=True)` 执行，未改动库文件：查询前 `size=324,022,272` / `mtime=2026-10-24 12:55:25.615392116 -0700` / `md5=ac7b5df94ad12481bd8d5e8bae54d6d0`，查询后**三项完全一致**；全程未出现 `data/cache.duckdb.wal`。
