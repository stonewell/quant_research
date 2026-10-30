# `L2.52` 阶段一 · 往返台账的取证归档（`_provenance/`）

本目录是 `docs/development_plan.md` 行 **`L2.52`**（换手引擎 = 止损出局 → 冷却买回往返的取证）
的**可复现存档**。它存在的唯一理由是让一个新 clone 的仓库**不依赖本机 `backtests_research/`
的产物、也不依赖任何未被版本化的中间文件**，就能核验 `STAGE1_REPORT.md` 里的数字，
并让 `backtester/tests/test_roundtrip_ledger.py` 真正跑起来（而不是 self-skip）。

> 归档动机（2026-10-29）：`/backtests_research/**` 在 `.gitignore` 里被整体忽略，只有
> `!*.md` / `!universe_*.txt` / `!_provenance/**` 等白名单例外。此前脚本 `roundtrip_ledger.py`
> 与 `out/`、`out_all/` 都不在版本库里 ⇒ 新 clone 上测试只能跳过 ⇒ 取证**不可复核**。
> 本目录用既有白名单（`.gitignore` 第 23 行 `!/backtests_research/**/_provenance/**`）收口。
>
> `.gitignore` 的唯一改动是**末尾追加**字节码守卫（`__pycache__/` + `*.pyc`）：因为白名单
> 第 23 行排在文件头的 `__pycache__/`（第 2 行）/ `*.pyc`（第 3 行）**之后**，而 git 取
> **最后**一个匹配的规则 ⇒ 若不追加，`_provenance/**/__pycache__/*.pyc` 会变成**可入库**
> （已用 `git add -n` 实测确认）。追加后 `git check-ignore -v` 报 `__pycache__/`（新第 27 行）
> 为最后匹配，`git add -n` 拒绝。**既有规则一条未删、未重排。**

## 1. 目录内容（什么进了库、什么没进）

| 路径 | 大小 | 说明 |
| --- | --- | --- |
| `roundtrip_ledger.py` | 43,023 B | 取证脚本本体（纯 pandas + 只读 DuckDB，**零回测、零网络**） |
| `out/summary.json` | 26,025 B | `--scope primary`（裁决口径）的完整摘要 |
| `out/summary_by_fold.csv` | 44,282 B | primary 按 (run, strategy, fold) 切片 |
| `out/summary_by_strategy.csv` | 7,496 B | primary 按 (run, strategy) 切片 |
| `out/summary_by_exit_depth.csv` | 1,998 B | primary 按出场深度桶切片 |
| `out_all/summary.json` | 49,574 B | `--scope all`（稳健性层，**不进裁决**）的完整摘要 |
| `out_all/summary_by_fold.csv` | 319,286 B | all 按 (run, strategy, fold) 切片 |
| `out_all/summary_by_strategy.csv` | 26,928 B | all 按 (run, strategy) 切片 |
| `out_all/summary_by_exit_depth.csv` | 2,040 B | all 按出场深度桶切片 |

**刻意不进库（仍留原处、仍被 `.gitignore` 忽略）**：

- `../out/roundtrips.csv`（**2,017,302 B**）、`../out_all/roundtrips.csv`（**6,698,528 B**）——
  逐往返台账，单文件 2.0 MB / 6.7 MB，超出「可复核证据」的体积预算；其 sha256 见 §4，
  可用 §5 的命令**重新生成后对账**。
- `../STAGE1_REPORT.md` 已在库里（`!*.md` 白名单），不在本目录重复放置。
- 任何 `__pycache__/` / `*.pyc` / `data/**`：一律不入库。

## 2. 生成时的确切命令

原始运行目录为 `backtests_research/20261025_125300/roundtrip_ledger/`（脚本当时尚无 `_provenance/`
这一层），**从仓库根执行**：

```bash
# 裁决口径（4 个主跑批：01_r1_longwin / 03_r2b_oos / 07_r3_oos_bear / 08_ablate_wf/baseline）
python backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py \
    --scope primary \
    --db data/cache.duckdb \
    --aux-db data/cache.backup-20261027-pre-m38raw.duckdb \
    --out backtests_research/20261025_125300/roundtrip_ledger/out

# 稳健性层（全部 75 份 walkforward_rebalances.csv；不进裁决，仅交叉核对三种切法一致）
python backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py \
    --scope all \
    --db data/cache.duckdb \
    --aux-db data/cache.backup-20261027-pre-m38raw.duckdb \
    --out backtests_research/20261025_125300/roundtrip_ledger/out_all
```

两处 Db 层级的语义（与 `STAGE1_REPORT.md` §4 一致，勿混用）：

- `--db data/cache.duckdb --db-mode raw_factor`（默认）：**预注册的第 2 层**，只用于
  `bars_elapsed` 的交易日历与 `mae` / `mfe` / 反事实；当时该库对 16 只标的只有 **2024-01-02** 起的 OHLC。
- `--aux-db data/cache.backup-20261027-pre-m38raw.duckdb --aux-db-mode preadj`（默认）：
  **非预注册的第 3 层**，`m38 raw` 迁移**之前**的备份，用来把 2021–2023 覆盖补全并做交叉校验。
  用与不用都写进 `summary.json` 的 `provenance`。

## 3. 数据库指纹（**as used**，取自 `summary.json` 的 `provenance` 块，不是事后补记）

| 文件 | sha256（生成时） | 大小（生成时） | 备注 |
| --- | --- | --- | --- |
| `data/cache.duckdb` | `97dfcda13e5a4a5af282d92bf424c4550d153d66b5f80830569b8b4f2b0edf74` | 7,876,608 B | 20 标的 / 21,365 根，覆盖至 `2026-10-27` |
| `data/cache.backup-20261027-pre-m38raw.duckdb` | `b9eaf7e1a1ee17f5a7c068a9bc5af40a229019399c38cbd4df29adf55f4e720e` | 322,973,696 B | 42,376 行，覆盖 `2017-01` → `2026-10-26` |

⚠️ **诚实披露：主库已不是生成时那一份。** 归档当日（2026-10-29）磁盘上的
`data/cache.duckdb` 为 `b615306a8a74cd2eb688161c7e761528994990b19ec9f8cbd625706bb6333408`、
9,187,328 B、**33,774 根 / 27 标的 / `2016-01-04` → `2026-10-29`**（该库在 `L2.54` / `L2.55`
期间被删除并重建过，见 `docs/development_plan.md` 对应行）。`aux-db` 的 sha256 **与生成时逐字节相同**。

**后果已实测，不是推测**（2026-10-29 用重建后的主库 + 同一 aux-db 重跑 `--scope primary`）：

- `summary.json` 的 **19 个顶层块中 17 个逐键相同**（含 `pooled` 23 键、`dedup` 24 键、
  `verdict_dedup` / `verdict_pooled`、`turnover`、`cost_attribution`、`frequency`、
  `path_status_counts`、`dedup_excess`、`bars_histogram`）；
- 三个 by-* 切片 CSV **逐字节相同**；
- 重新生成的 `roundtrips.csv` 有 **42 列、3,881 行**完全相同，**仅 6 列不同**：
  `bars_mae` / `bars_mfe` / `bars_cf_return` / `mae` / `mfe` / `cf_return`（NaN 计数都不变，
  差值来自主库重建后的复权收盘价）；
- 只有 `price_calibration` / `gap_calibration` 两块（**只读主库**）整体不同 ——
  这两块正是 `STAGE1_REPORT.md` §3.1 里「复权路径 vs CSV `price` 列」的自校验，重建后不复现属预期。

⇒ **结论**：第 1 层（配对 / `gap_return` / 频率 / 摩擦份额）与**裁决**是**完全可复现**的；
需要**逐位复现** `mae` / `mfe` / `cf_return` 时，必须拿到 §3 表里那份主库（sha 见上），
否则只能按「同 aux-db 覆盖」的等价口径读。

## 4. 未入库的大 CSV 的完整性指纹

这两份是**逐往返台账**（体积原因不入库）。它们由 §5 的命令重新生成后，应与下表逐字节一致
（前提：主库 sha 与 §3 相同；用重建后的主库会命中 §3 所述的 6 列差异）：

| 文件 | 大小 | sha256 | 行数 |
| --- | --- | --- | --- |
| `out/roundtrips.csv` | 2,017,302 B | `b6ec062c35bf437af5ea876ce443ba8db05a65b1d466efc974d0c87327d35469` | 3,881 数据行（+1 表头 = 3,882 行） |
| `out_all/roundtrips.csv` | 6,698,528 B | `772f70884ebc7088e67f410903d0f89530766daaed7a443c8a3e26bc16466879` | 14,289 数据行（+1 表头 = 14,290 行） |

## 5. 如何复现

### 5.1 直接读已入库的摘要（不需要任何数据库、不需要 `data/`）

`out/summary.json` 与 `out_all/summary.json` 是**自足**的：配对计数、正/负 gap 占比、
加权 gap、`bars_elapsed` 分布、摩擦归因、预注册判据真值表（`verdict_*`）、数据层 `provenance`
全在里面。裁决只需读 `verdict_dedup` / `verdict_pooled` 的 `verdict` 字段（本批为 `mixed`）。

### 5.2 只凭 CSV 重算第 1 层（不需要 DuckDB，`--db ""` 即可）

```bash
python backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py \
    --scope primary --db "" --out /tmp/l252_csvonly
```

此模式只产出 CSV 可算的部分（配对、`gap_return`、日历日近似、再入率、摩擦）；
`bars_elapsed` / `mae` / `mfe` 为空，属预期（第 2 层需要日线缓存）。

### 5.3 完整重生成（需要本地只读 DuckDB，**禁止网络**）

```bash
# 第 1 步：确认数据库指纹与本文件 §3 一致（不一致就别拿它跟 §4 的 sha 对账）
sha256sum data/cache.duckdb data/cache.backup-20261027-pre-m38raw.duckdb

# 第 2 步：重生成 primary 与 all 两套台账
python backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py \
    --scope primary --db data/cache.duckdb \
    --aux-db data/cache.backup-20261027-pre-m38raw.duckdb --out /tmp/l252_repro/out
python backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py \
    --scope all --db data/cache.duckdb \
    --aux-db data/cache.backup-20261027-pre-m38raw.duckdb --out /tmp/l252_repro/out_all

# 第 3 步：与 §4 的指纹对账（只有主库 sha 相同时才期望逐字节一致）
sha256sum /tmp/l252_repro/out/roundtrips.csv     # 期望 b6ec062c…
sha256sum /tmp/l252_repro/out_all/roundtrips.csv # 期望 772f7088…

# 第 4 步：与入库摘要对账（§3 的等价口径下，除 price/gap_calibration 外应逐键相同）
python - <<'PY'
import json
a = json.load(open('backtests_research/20261025_125300/roundtrip_ledger/_provenance/out/summary.json'))
b = json.load(open('/tmp/l252_repro/out/summary.json'))
print([k for k in a if k != 'provenance' and a[k] != b.get(k)])
PY
```

**输出目录说明**：把 `--out` 指向 `/tmp/...`（如上）或任意新目录；若要覆盖归档口径，
`--out` 的默认值是 `<run_root>/roundtrip_ledger/out`，且 `--run-root` 的默认值**优先取仓库相对
路径**、从仓库外运行时**回退到由 `__file__` 推出的绝对路径**，因此默认输出**不会**静默落到调用者的
当前目录里。要写 `out_all` 必须**显式**给 `--out`（脚本不会自己切换两套目录）。

### 5.4 跑测试

```bash
pipeline/.venv/bin/python -m pytest backtester/tests/test_roundtrip_ledger.py -q   # 期望 10 passed
```

`backtester/tests/test_roundtrip_ledger.py` 按**文件路径**加载本目录的 `roundtrip_ledger.py`
（`backtests_research/` 不是包、目录名以数字开头，故不走 `import`）；该文件已在库中，
故在新 clone 上**应当运行而不是跳过**。`pytest.skip` 守卫仅作防御性兜底，且测试会**同时断言
脚本文件存在**，避免守卫把「文件丢了」伪装成「环境不支持」。

## 6. 口径纪律（读这些数字前必须知道）

1. **`--scope all` 不进裁决**，它只用来证明「去重 / 池化 / 全量」三种切法给出同一结论。
2. **`dedup` 口径的 `reentry_rate` 是 `null`**（去重后没有分母），要用再入率请看 `pooled`。
3. **`mae` / `mfe` / 反事实依赖非预注册的 `--aux-db`**；换库即换口径，任何跨库比较必须先声明。
4. 本目录只放**测量产物**，不含任何策略代码；`L2.52` 阶段一/阶段二均为**零代码改动、零版本轴变动**
   （不升 `version` / `BASE_VERSION`、不重跑 `--dump-strategies`）。

相关文件：`../STAGE1_REPORT.md`（阶段一完整报告）、`docs/development_plan.md` 行 `L2.52`
（预注册判据与结案记录）、`docs/version.md` §16.15.15（冷却 5 根的归因）。
