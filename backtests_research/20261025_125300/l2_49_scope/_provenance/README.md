# `L2.49` 换手削减测量 · 可复现性附录（`_provenance`）

本目录是 [`../L2_49_SCOPING_MEMO.md`](file:///home/stone/Work/github/quant/backtests_research/20261025_125300/l2_49_scope/L2_49_SCOPING_MEMO.md)（裁定书，439 行）全部表格的**唯一生成链路**。裁定书正文里每一张表都标了它的 `out/*.csv` 来源，本目录提供四支脚本、运行顺序、输入产物与逐文件哈希，使「读结论」可以回退到「重算结论」。

> **历史沿革（2026-10-29）**：这些脚本与 `out/` 原本直接放在 `l2_49_scope/` 下（`l2_49_scope/out/`），随本轮提交整体搬入 `_provenance/`（`_provenance/out/`），**保留 `out/` 子目录层级**、未摊平 —— 这样四支脚本里既有的 `__file__` 相对默认（`OUT = HERE/out`）语义不变。唯一随之修改的代码是对**仓库根**的解析层级：脚本位置下移一级，故 `REPO`/`SCOPE` 的 `..` 链由 3 级改为 4 级（`decompose_turnover.py:74`、`friction_rate.py:47`、`make_tables.py:27`），改后从仓库根直接运行解析结果与搬迁前逐字相同：
>
> ```
> REPO = .../quant                                  （搬迁前后一致）
> decompose_turnover.SCOPE = .../backtests_research/20261025_125300
> friction_rate.SCOPE      = .../quant
> OUT                      = .../l2_49_scope/_provenance/out
> ```
>
> **搬迁未重算、未改口径、未改任何阈值**；搬迁后实跑复核：`friction_rate.py` 仍产出 `201.15349087 bp/年`，`make_tables.py` 仍产出 `16.6290×/年`（漂移 `0.0319` / `0.5308×`），与裁定书正文一致。

---

## 1. 运行顺序（四条命令，必须按序；全部离线、只读、零网络）

```bash
cd /home/stone/Work/github/quant

# 1) 桶分解（T1）+ 信号/漂移拆分 + 锚 + 跨窗口份额
#    -> out/summary.json, out/per_fold_{shares,anchored}.csv, out/bucket_by_strategy.csv,
#       out/b17_rows_classified.csv, out/delta_w_histogram.csv
pipeline/.venv/bin/python backtests_research/20261025_125300/l2_49_scope/_provenance/decompose_turnover.py

# 2) 逐杠杆天花板（T3）+ 漂移量级 + 带宽射程 + 空转 vs 真实摩擦 + 消融/网格换手
#    -> out/ceilings.json, out/ablation_turnover.csv, out/param_grid_turnover.csv
pipeline/.venv/bin/python backtests_research/20261025_125300/l2_49_scope/_provenance/lever_ceilings.py

# 3) 摩擦率实测（Σtotal_cost / 平均权益 / 折年数，刻意不经过任何换手约定）
#    -> out/friction_rate.json, out/friction_per_fold.csv
#    这一步是 §6「85.26 bp/年 低 2 倍」裁定的唯一直接证据
pipeline/.venv/bin/python backtests_research/20261025_125300/l2_49_scope/_provenance/friction_rate.py

# 4) 汇总出 T1–T5（裁定书所有表格的唯一来源，表格一律由脚本生成、无手抄）
#    -> out/T1_decomposition.csv, T2_per_strategy.csv, T3_lever_ceilings.csv,
#       T4_band_arms_measured.csv, T5_noise_floor.csv
pipeline/.venv/bin/python backtests_research/20261025_125300/l2_49_scope/_provenance/make_tables.py
```

依赖关系：`lever_ceilings.py` 以 `import decompose_turnover as dt` 复用其分类/加载逻辑（`sys.path.insert` 到自身目录）；`make_tables.py` 只读 `out/*.json`。四支脚本都支持 `--out <dir>` 覆盖输出目录（1/2/3 支持；`make_tables.py` 无 `--out`，固定写 `HERE/out`）。

## 2. 输入产物（全部为既有、只读，本轮零改动）

| 输入 | 路径 | 被谁读 | 用途 |
| :--- | :--- | :--- | :--- |
| `blend_17_baseline` 逐折报告与调仓流水 | `pipeline/results/blend_17_baseline/{1,2}/<strategy>_strategy/walkforward_{report,rebalances}.csv` | 1 / 2 / 3 / 4 | 108 折（6 策略 × 18 折）、5,426 行调仓记录；`total_turnover`、`total_cost`、`portfolio_equity` 的原始来源 |
| 消融臂 | `backtests_research/20261025_125300/08_ablate_wf/{baseline,<arm>}/…` | 1 / 2 / 4 | §10.3「其它旋钮射程」同几何对照（基线 15.235×） |
| 参数网格臂 | `backtests_research/20261025_125300/09_param_grid/…` | 2 / 4 | §10「已实测臂」的 `0.10/5`、`0.15/10`（63 bar / 18 折几何，见第 4 节第 3 条） |
| 长窗臂 | `backtests_research/20261025_125300/01_r1_longwin/…` | 1 | §5 跨窗口份额的右端（756 天长折，**仅方向参考**） |
| 往返账本 | `backtests_research/20261025_125300/roundtrip_ledger/out/summary.json` | 2 | `L2.52` 往返侧口径对照（不并入本行分解） |

## 3. 产物清单与哈希（sha256 + 字节数）

除第 4 节标注的**唯一一个 > 500 KB 文件**外，`out/` 下全部产物均已随本提交入库且可逐字节比对。

| 文件 | 字节 | sha256 | 入库 |
| :--- | ---: | :--- | :---: |
| `out/summary.json` | 7,280 | `c6b907e678153a79511c389fd877efa848ad589dab4a5b81cb1c708a28aa503e` | 是 |
| `out/ceilings.json` | 33,498 | `32c7f6d898124614b92a48f615ca7c6ef72df3d91905cad488f10014e23655a8` | 是 |
| `out/friction_rate.json` | 1,375 | `8f6e22318b305e71a2b4fe4152135964e719cd7af37dfe7abf049273badbe494` | 是 |
| `out/T1_decomposition.csv` | 1,019 | `050c4e2a067b6ff8f001a5bcba0a8387269e524efbaf62a668443203b9a53e29` | 是 |
| `out/T2_per_strategy.csv` | 1,766 | `2531c4381d5a0867a8d66d6aa914b0ac7a26cbb343b13883fe08025bdedaa743` | 是 |
| `out/T3_lever_ceilings.csv` | 2,713 | `38d79a8b8b765d4d0818090c56fe75dbf98c7fac342facc539927cc70d280e65` | 是 |
| `out/T4_band_arms_measured.csv` | 843 | `f9cc6f5780d96371bfb6432b9dc146ce79b607153f17a8783e99d6dc9052e00e` | 是 |
| `out/T5_noise_floor.csv` | 1,027 | `65de1491dd7403668393ca6d5f57308f04661d574a4046bbb53ade158d29e6ad` | 是 |
| `out/per_fold_shares.csv` | 28,098 | `d3617ff0d90793289e4698f7d3ad91a35aa9825e3a3182c348478822d675bc76` | 是 |
| `out/per_fold_anchored.csv` | 56,188 | `462295b178d22a11330ca933491ad97a884b068bd087c58b85f6b5da67d5f9e1` | 是 |
| `out/bucket_by_strategy.csv` | 4,065 | `99d5c1723424f875e6cb1506cce44a4186d6930b3f0d09008cc658e2fe967f73` | 是 |
| `out/delta_w_histogram.csv` | 3,183 | `b3022d460fa9023a441b548bba35a03242b0371fa797a9a4e81f95448783a618` | 是 |
| `out/friction_per_fold.csv` | 23,478 | `f6f264d3e528df63c807a421b8b44769682a179aec906d91e66a5b2099cf1596` | 是 |
| `out/ablation_turnover.csv` | 41,212 | `568af97e5226275ec08c2fed604eecd91573e8266f3f028d8b38f92e03da87d2` | 是 |
| `out/param_grid_turnover.csv` | 1,222 | `0575811dd74546cd3c48f27eea36f6340cc42a98640ee2f188d538aabad1cc02` | 是 |
| `out/b17_rows_classified.csv` | **2,037,338** | `ae4d05b33ab86369238011ddc3aa2d71ef6e61f2bb6fcaaebdb60f8f6ecda1f3` | **否**（> 500 KB，见 §4） |

脚本本身：

| 脚本 | 字节 | sha256 |
| :--- | ---: | :--- |
| `decompose_turnover.py` | 17,980 | `32e99dd4be36bae372ceeadaaf73aef04f98deb2e7d47fd7bbac97b723e480a0` |
| `friction_rate.py` | 5,971 | `b07c46ed758072fdc12c3c61edf41c037397fc0fabe374cbed0232e12f465792` |
| `lever_ceilings.py` | 24,013 | `60d11e9752b080f89f7c280d21df24acb895cb85572f40f8beb527b501271d78` |
| `make_tables.py` | 15,467 | `841d2a8b8f03b523bdf284aea13731e355299ba70bd4b4ce0595846d6380232a` |

> 上述脚本哈希是**搬迁 + 解析层级修正之后**的值（修正是 `..` 链 3 → 4，见文首沿革）。要核对「只改路径、没改算法」，把 `REPO`/`SCOPE` 行的 `..` 数量改回 3 并置于 `l2_49_scope/` 下，输出应与上表逐字节一致。

## 4. 复现性与已知不可复现项（与裁定书 §13 一致，不美化）

1. **`16.3608×` 非比特可复现**：`L2.36`/`§16.15.13` 当时的 5,403 行口径已无法逐字重建；本轮回算为 **5,426 行 / 16.6290×/年**（+1.64%）。所有份额类的结论**免疫于年化约定与这 0.23% 的行数差**（份额是占比，不是水平）。
2. **`pipeline/results/` 已于 2026-10-23 原地覆盖**：`L2.36` 当时所用产物不可考；本目录所有数字只对应**覆盖后**的 `blend_17_baseline`。
3. **`01_r1_longwin`（756 天长折）只作方向参考**：与短折的几何不同，**不得**与短折合并求均值。
4. **`09_param_grid`（63 bar / 18 折）与 `blend_17_baseline`（94 天折）几何不同**：其**相对**变化（如 `0.10/5` → `8.47×`）**不可**乘到 `16.6290×` 上，只能作为「同向、同量级」的旁证。
5. **`b17_rows_classified.csv`（2.0 MB）未入库**：超过仓库 500 KB 的单文件提交上限。它是 `decompose_turnover.py` 的直接产物、可一条命令再生（见 §1 第 1 步），哈希已记在 §3 供比对；`out/.gitignore` 显式记录了这条排除，不是意外漏提交。
6. **杠杆 (b′) 只有算术上界、无 ΔCAGR**：`asymmetric_sell_ratio` 的空转是**严格 0**（可测），但「若改为按组合级施加会如何」只有算术上界，没有回测臂 —— 裁定书 §9 已按「上界」标注，勿当实测引用。
7. **摩擦口径**：`201.15 bp/年` 为 `Σtotal_cost / 平均权益 / 折年数` 的 108 折均值（`out/friction_rate.json::friction_bp_yr_mean_over_folds = 201.15349087511441`），中位数 `180.65`。**不要**用 `85.26 bp/年`（单边折算，低 2 倍，见 `docs/version.md` §16.15.13 的 2026-10-29 更正）。
