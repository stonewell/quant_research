[ English | [简体中文](ml_dl_trading_landscape_ZH.md) ]

# ML/DL trading solutions landscape (beyond AutoBNN) — research summary

**Status: reference/survey doc, not an implementation plan.** Nothing here is wired into any
project in this workspace, and this file is deliberately not linked from any README or referenced
by any code — same convention as `docs/TODO_autobnn_future_uses.md`. This captures a deep-research
pass (2026-08-27) into recent (~2022-2026) ML/DL solutions for trading OTHER than AutoBNN (which has
its own dedicated project, `ml/bnn_forecaster/`, and its own future-uses doc, the file above), for
whenever any of these are worth evaluating for this workspace. Every claim below is attributed to a
specific paper/project; "evidence" is called out explicitly as backtested-only vs. live-validated,
and author-reported vs. independently reproduced, matching this codebase's own disclosure culture
(see `AGENTS.md`'s "Documentation & coding style conventions").

**Headline finding, stated up front:** across all four categories researched, the pattern that
recurred most consistently was strong evidence of "does this forecast/classify well" and weak-to-
negative evidence of "does this make money after realistic costs." The single most credible result
in this entire pass is a *negative* one (§2, "Benchmarking Deep Time Series Models for Equity
Portfolios"). Treat everything below accordingly — this doc is a survey of the state of evidence,
not a recommendation to adopt any of it.

## 1. Transformer / state-space forecasters

### 1.1 Temporal Fusion Transformer (TFT)
Lim et al. 2019/2021, *International Journal of Forecasting* — attention-based multi-horizon
forecaster with variable-selection networks and native quantile outputs.

Actual trading backtests exist for this one (rare in this category):
- Multi-crypto strategy (technical + on-chain features): **Sharpe 1.06, 38.6% cumulative return
  over a 3-year backtest**, beating both traditional models and a passive benchmark ([MDPI FinTech
  2025](https://www.mdpi.com/2079-8954/13/6/474)).
- "TFT-ASRO" (*Sensors* 2025): predicts Sharpe ratios directly (multi-task return+volatility) on US
  equities, reports outperforming traditional/DL baselines.

**HONEST CAVEAT:** neither paper's transaction-cost/slippage/survivorship-bias handling could be
verified from the abstract/search layer; both are single, non-independently-replicated academic
results.

### 1.2 PatchTST / Informer / Autoformer / FEDformer
2021-2023 long-sequence forecasting Transformers. SOTA only on GENERIC benchmarks (Weather,
Traffic, Electricity, ILI, ETT) — none report financial trading backtests in their original papers.
PatchTST (ICLR 2023) appears only as a *baseline* in the financial foundation-model studies below,
where it does not stand out. Mainly of historical/architectural interest for finance.

### 1.3 DLinear / NLinear — the skeptical result
Zeng, Chen, Zhang, Xu, **AAAI 2023 (Oral)**, ["Are Transformers Effective for Time Series
Forecasting?"](https://ojs.aaai.org/index.php/AAAI/article/view/26317/26089)
([code](https://github.com/cure-lab/LTSF-Linear)). Claim: a single linear layer (with
trend/seasonal decomposition) beats Informer/Autoformer/FEDformer/Pyraformer on long-horizon
GENERIC forecasting — self-attention's permutation-invariance loses temporal-ordering information
that matters more than attention's expressiveness gains. No financial backtest in the original
paper.

**Independent pushback exists:** Hugging Face's own blog ("Yes, Transformers Are Effective for Time
Series Forecasting") argues DLinear's win is partly an artifact of an unfair comparison (Autoformer
wasn't given covariates DLinear implicitly uses). **Net take: "are Transformers even worth it for
time series" is still a contested, unresolved question in the base ML literature — before any
finance-specific noise is even added.**

### 1.4 iTransformer
Liu et al., **ICLR 2024 Spotlight** — inverts the standard axis: treats each variate's whole series
as one token instead of tokenizing across variates at one timestep. SOTA on generic multivariate
benchmarks, especially high-dimensional series. No dedicated stock-market backtest in the original
paper, but notably **beats several pretrained financial foundation models on META specifically** in
the benchmark cited in §2 below — a plain (non-pretrained) Transformer outperforming "foundation
models" is itself a telling data point.

### 1.5 Mamba / state-space models applied to trading
- **MambaStock** ([arXiv 2402.18959](https://arxiv.org/abs/2402.18959), 2024): first Mamba
  (selective SSM) application to stock price prediction, no hand-crafted features. Only qualitative
  "surpasses baseline RNNs" claims were retrievable — no numeric error/return table found. A 2025
  follow-up (["From Rattle to Roar," arXiv 2508.04707](https://arxiv.org/abs/2508.04707)) varies
  only the optimizer on the same platform; it neither confirms nor contradicts the original
  accuracy claims. As of this research, no rigorous independent group appears to have tried to
  reproduce-and-beat MambaStock's original claims against strong baselines.
- **Mamba + attention / Mamba + Transformer hybrids** for stock trend prediction (2025 papers):
  report CSI 300 / CSI 800 (2008-2024) experiments claiming to "consistently outperform mainstream
  baselines" on Information Coefficient (IC), RankIC, ICIR, RankICIR — genuine quant-finance
  signal-quality metrics, a positive sign of finance-aware evaluation. Actual IC/RankIC numbers
  were not retrievable, so the magnitude of any edge is unverified here.

**HONEST CAVEAT:** IC/RankIC-style results in the Chinese A-share ML literature have a well-known
history of not surviving out-of-sample/live deployment even when in-sample IC looks good.

### 1.6 N-BEATS / N-HiTS
Oreshkin et al., ICLR 2020 (N-BEATS): beat the M4 competition's own winning hybrid model by 3% and
a statistical benchmark by 11% — the first pure-DL approach to beat well-established statistical
methods on M4. This is a real, well-replicated, non-financial-forecasting result, one of the more
solid DL-forecasting wins in the literature — but M4 is retail/macro/demographic series, not asset
prices. A financial-specific follow-up
([arXiv 2409.00480](https://arxiv.org/abs/2409.00480), "N-HiTS vs N-BEATS" for financial
prediction) claims accuracy/robustness improvements but no concrete numeric results were
extractable — treat as an unverified claim.

## 2. Time-series "foundation models" — the most rigorously finance-tested category

**Models:** Amazon Chronos / Chronos-2, Google TimesFM / TimesFM-2.5, Salesforce Moirai /
Moirai-2.0, Nixtla TimeGPT-1, Lag-Llama. All rank near the top of general TSFM leaderboards
(GIFT-Eval, fev-bench, Monash Archive) — strong evidence of general forecasting competence, which
says nothing about financial alpha on its own. Three recent papers specifically stress-tested these
models against financial baselines:

1. **["Pretrained Time-Series Foundation Models for Financial Return Forecasting"
   (2026)](https://arxiv.org/abs/2606.27100)** — benchmarks TimeGPT, TimesFM-2.5, Moirai-2.0,
   Chronos against N-BEATS/N-HiTS/PatchTST/iTransformer/KAN on 5 US equities. Foundation models won
   8 of 10 task-level rank comparisons, but a **Diebold-Mariano significance test against a
   random-walk benchmark** found significant outperformance in only **2 of those 10 cases**
   (Chronos on AMZN, Moirai-2.0 on GOOG) — everywhere else the "win" wasn't statistically
   distinguishable from noise. iTransformer (not a foundation model) beat every pretrained
   foundation model on META specifically. Authors' own verdict: foundation models are "useful
   practical priors that reduce model-development costs," but "not universal engines for
   statistically reliable alpha generation."
2. **["Re(Visiting) Time Series Foundation Models in Finance"
   (2025)](https://arxiv.org/abs/2511.18578)** — large-scale daily-excess-returns dataset across
   multiple markets, forecasting/trading/portfolio/risk tasks. Off-the-shelf pretrained TSFMs
   perform poorly, zero-shot AND fine-tuned, on financial returns. Models **pretrained from scratch
   on financial data** achieve "substantial forecasting and economic improvements." Verdict: value
   comes from domain-specific pretraining DATA, not from generic foundation-model architecture/
   scale — directly undercuts the "just use Chronos/TimesFM off the shelf for finance" pitch.
3. **["Benchmarking Deep Time Series Models for Equity Portfolios"
   (2026)](https://arxiv.org/abs/2606.09420)**, 15 architectures, 2018-2024 — **the most
   damaging-to-hype result found in this entire research pass**: best model's "rank-1
   acceptability" is only 0.352 (no architecture exceeds ~0.36, i.e. no model is a clear, reliable
   winner across conditions), and **net Sharpe at 20bps transaction cost is negative for every
   single top-ranked model.** Authors' own framing: results "support model selection and
   diagnosis rather than a standalone trading-strategy claim."

## 3. Deep reinforcement learning

### FinRL / FinRL-Meta / FinRL-Podracer (AI4Finance Foundation)
Open-source DRL framework for quant trading (data -> environment -> agent -> backtest pipeline).
FinRL ([arXiv 2011.09607](https://arxiv.org/abs/2011.09607)), FinRL-Meta (NeurIPS 2022 Datasets &
Benchmarks, [arXiv 2112.06753](https://arxiv.org/abs/2112.06753) — hundreds of near-real market
simulation environments), FinRL-Podracer ([arXiv 2111.05188](https://arxiv.org/abs/2111.05188) —
scalable cloud training). Supports PPO/A2C/DDPG/SAC/DQN and an "ensemble" strategy.

**Author-reported backtests (all frictionless-simulator results unless noted):**
- Ensemble Strategy: 30 DJIA stocks, trained 2009-2015, tested 2016/01-2020/05. Ensemble
  (PPO+A2C+DDPG): **Sharpe 1.30**, cumulative return 70.4%, max drawdown -9.7%, vs. DJIA
  buy-and-hold Sharpe 0.47 (return 38.6%, drawdown -37.1%) and min-variance Sharpe 0.45.
- FinRL-Podracer: cumulative returns up to 362.4%, annual return 111.5%, **Sharpe 2.42**, max
  drawdown -15.9% (crypto/minute-level data); daily-bar equities Sharpe generally lower (~1.35-2.05)
  than minute-bar.
- ElegantRL/Stable-Baselines3 agents in the same framework: annual returns 22-32%, Sharpe 1.46-1.62
  (equities); crypto ElegantRL Sharpe 2.99.

No independent third-party reproduction or live-trading confirmation of these specific numbers was
found.

### DRL backtest-overfitting detection (AI4Finance, [arXiv 2209.05559](https://arxiv.org/abs/2209.05559))
A hypothesis-testing method to estimate each trained DRL agent's PROBABILITY of being overfit to
the backtest, and reject high-risk agents before deployment — a direct response to the field's
overfitting problem, tested on 10 cryptocurrencies through the May-June 2022 crash. Agents flagged
as "low overfitting probability" outperformed both more-overfit agents and an equal-weight/index
baseline during a genuinely out-of-sample crash period. **This is the most credible artifact in the
entire DRL category, precisely because it's testing for the failure mode rather than just reporting
a favorable Sharpe.**

### HONEST CAVEAT — critical surveys on DRL-for-trading
- **Millea (2021), "Deep Reinforcement Learning for Trading — A Critical Survey,"** *Data* 6(11):119
  (MDPI, DOI 10.3390/data6110119): essentially all reviewed DRL-trading papers assume **no
  transaction costs, no liquidity constraints, no bid/ask spread** — and where realistic costs were
  added, they materially hurt or eliminated the reported edge over baselines.
- **Annual Reviews (2024), "A Review of Reinforcement Learning in Financial Applications"**
  ([arXiv 2411.12746](https://arxiv.org/abs/2411.12746)): more recent, less hostile, but still finds
  the field "promising but immature" — across the whole literature it surfaces only **one** paper
  (Wei et al. 2019) with ANY live-market validation at all, covering just **five trading days**.
- Other well-documented failure modes raised across multiple surveys: reward/state-representation
  sensitivity, non-stationary market regimes (a policy trained on one regime doesn't transfer),
  simulator fidelity (the backtester itself doesn't model market impact), random-seed sensitivity.

### FinRL Contests (2023-2025, [arXiv 2504.02281](https://arxiv.org/abs/2504.02281))
Community benchmarking effort created specifically because individual papers' reported results
weren't comparable/reproducible — itself an implicit admission of the field's reproducibility gap.

## 4. LLM-based / agentic trading systems — flashiest numbers, least credible

### TradingAgents ([arXiv 2412.20138](https://arxiv.org/abs/2412.20138), Dec 2024)
Multi-agent LLM framework with role-specialized agents (fundamental/sentiment/technical analysts,
bull/bear researchers, trader, risk manager) debating to a decision. Author-reported, 3 tickers, 3
months (Jan 1 - Mar 29, 2024): AAPL +26.6% cumulative return / **Sharpe 8.21**; GOOGL +24.4% /
Sharpe 6.39; AMZN +23.2% / Sharpe 5.60 — beating the best baseline by wide margins.

**HONEST CAVEAT:** Sharpe ratios of 5-8 are far outside any historically plausible range for a real
strategy at this scale. The authors themselves flag this as anomalous ("examined decision sequences
to ensure calculation correctness," attribute it to "few pullbacks... during that period") rather
than fully explain it away. A 3-month, 3-ticker window is also far too short/narrow to generalize
from — the cost structure (11+ LLM calls, 20+ tool calls per prediction) is explicitly why the
window is so short.

### FinMem ([arXiv 2311.13743](https://arxiv.org/abs/2311.13743), 2023-24) vs. FinGPT
Layered-memory LLM trading agent (short/mid/long-term memory, configurable risk "character").
Author-reported single-stock case studies: TSLA — FinMem 61.8% cumulative return, Sharpe 2.68, max
drawdown 10.8%, vs. FinGPT's **-7.46%** return on the same window; NFLX — FinMem 36.5% return,
Sharpe 2.02, vs. FinGPT +9.0%.

**HONEST CAVEAT:** single-stock, author-selected case studies, not a broad universe/portfolio-level
test; the wide gap vs. FinGPT should be read as "these two systems aren't well-controlled
comparisons" as much as "FinMem is much better."

### FinGPT ([arXiv 2307.10485](https://arxiv.org/abs/2307.10485), 2023) — sentiment/data pipeline, not a strategy
Open, LoRA-fine-tuned financial LLM for sentiment extraction, positioned as an open alternative to
closed proprietary financial LLMs. Independent backtests feeding FinGPT-derived sentiment into
rule-based long/short and RL strategies found the sentiment signal "provides some added value" over
technical-only strategies, but sentiment-weighted portfolios showed LOWER annual returns and Sharpe
ratios than purely technical ones (lower drawdown, though). No rigorous, large-universe,
cost-inclusive backtest with a clear net-positive Sharpe was found.

### Open-source "AI hedge fund" projects
Popular, actively maintained multi-agent LLM systems styled after named investors (Buffett/Munger
personas; ~7,500 GitHub forks each for the most popular ones). **No public backtest or
paper-trading performance numbers are published by the maintainers** — the flagship project's own
README states it is "for educational/research purposes only... not intended for real trading, no
guarantees of performance." Notable purely for adoption/community interest, not evidence.

### The distinct risk specific to LLMs: parametric look-ahead bias
This is the most important caveat in this whole category, and is DISTINCT from ordinary backtest
overfitting:
- **["Summoning the Oracle to Slay It: Mitigating Look-Ahead Bias in Financial Backtesting with
  LLMs"](https://arxiv.org/abs/2605.24564)** names the failure mode "parametric look-ahead bias": an
  LLM with a 2025 training cutoff has already seen how NVDA/MSFT/NFLX moved through 2010-2024, so
  ANY backtest over a historical window the model was trained on is contaminated INSIDE the model's
  own weights — invisible to ordinary data-pipeline leakage audits, and not fixable by just
  filtering the prompt's context data.
- **Sarkar & Vafa (2024)** found direct evidence: prompting Llama 2 to assess risks from Sept-Nov
  2019 earnings calls, the model mentions "Covid-19" in **over 25%** of responses — a concrete,
  measurable instance of future knowledge leaking into a "historical" prediction.
- **["Assessing Look-Ahead Bias in Stock Return Predictions Generated by GPT Sentiment Analysis"
  (arXiv 2309.17322)](https://arxiv.org/abs/2309.17322)** exists specifically to quantify this for
  GPT sentiment-trading pipelines — its existence as a dedicated study is itself evidence the
  community treats this as a serious, measurable problem, not a theoretical one.
- **Practical implication:** almost every backtested LLM-trading-agent result for tickers/periods
  before the underlying model's training cutoff — the overwhelming majority of published results,
  including TradingAgents' and FinMem's above — carries this unresolved contamination risk on top
  of ordinary overfitting risk.

## 5. Graph neural networks for stock relation modeling

### HATS — Hierarchical Graph Attention Network ([arXiv 1908.07999](https://arxiv.org/abs/1908.07999), 2019; foundational, underlies most later work)
Learns which TYPE of inter-company relation (supply chain, sector, ownership, etc.) matters at each
moment via hierarchical attention over a company relation graph, rather than fusing all relation
types uniformly. Author-reported: S&P 500 constituents, 2013/02-2019/06 (1,174 trading days). ~6%
accuracy improvement on both index-level and individual-stock movement prediction over prior SOTA
baselines (LSTM, GCN variants); a portfolio built from HATS's predictions **beat the S&P 500 index
by 34% on Sharpe ratio**. Only HATS and one GCN variant beat plain LSTM on F1 score despite most
relational models beating LSTM on raw accuracy — a sign some of the "improvement" is
distributional/class-imbalance-sensitive, not a clean win everywhere.

### DGDNN — Decoupled Graph Diffusion Neural Network ([arXiv 2401.01846](https://arxiv.org/abs/2401.01846), ICAART 2024)
Automatically constructs the stock relation graph itself (entropy-driven edge generation) instead
of using a hand-picked relation set, plus decoupled diffusion for hierarchical intra-stock
features. Authors claim "substantial improvements over SOTA baselines" on NASDAQ/NYSE/SSE
(accuracy/MCC/F1); a disclosed ablation shows removing predefined industry/corporate relations
DROPS accuracy by 9.23% on average (evidence the graph structure is doing real work). Actual
accuracy/MCC numbers were not retrievable from accessible sources — flagged as an unverified claim,
not a negative finding.

**HONEST CAVEAT:** nearly the entire GNN-for-stock literature (HATS, DGDNN, and the broader field
per a 2024 ACM Computing Surveys review) is evaluated on classification metrics (accuracy/F1/MCC)
or idealized paper portfolios, essentially never on cost-inclusive, slippage-aware, walk-forward
portfolio backtests the way the DRL literature at least attempts to. HATS's 34%-better-Sharpe claim
is the one genuine portfolio-level result found here, and even that has no transaction-cost or
capacity discussion.

## 6. Cross-cutting caveats (apply to nearly everything above)

- **Backtest overfitting** is the dominant, well-documented failure mode for DL trading claims
  (Bailey & López de Prado, "The Probability of Backtest Overfitting").
- **Survivorship bias**: excluding delisted/bankrupt names systematically inflates apparent
  predictive skill — estimated ~0.9%/yr bias even for diversified fund returns, larger for
  individual stock-picking studies.
- **Transaction-cost neglect** can flip results entirely: one documented case had a reported
  +23.26% cumulative return become **-22.04%** (a full sign reversal) once transaction costs were
  included on a slightly shifted evaluation window — a textbook overfitting signature. The
  equity-portfolio benchmark in §2 shows this is not a one-off: it's systematic across 15 modern
  architectures at just 20bps.

## 7. Overall verdict

1. **Nothing surveyed rises to "proven for live real-money trading"** by independent,
   cost-inclusive, out-of-sample evidence — across all four categories.
2. **The most credible result in the entire pass is a negative one**: 15 modern deep architectures,
   tested as equity-portfolio models with realistic 20bps costs, ALL produce negative net Sharpe
   ([arXiv 2606.09420](https://arxiv.org/abs/2606.09420)).
3. **Foundation models (Chronos/TimesFM/Moirai) are strong generically, weak-to-unproven
   financially** — outperformance vs. a plain random walk survives significance testing in only
   2/10 tested cases. Domain-specific pretraining on financial data beats generic foundation-model
   scale.
4. **RL-for-trading's own academic surveys are self-skeptical** (near-universal zero-transaction-
   cost assumption; one 5-day live validation in the whole field) — this is a field that has
   already had its reckoning, and says so in its own literature.
5. **LLM-agent trading hasn't developed that self-critical tradition yet**, and carries an
   ADDITIONAL failure mode (training-data contamination / look-ahead bias) that RL doesn't —
   combined with the most inflated headline numbers (Sharpe 8.21) of anything surveyed, this is the
   category to be most skeptical of.
6. **GNN stock-relation models have the most methodologically narrow but plausible edge** (HATS's
   accuracy/Sharpe gains) — worth watching, but "predicts direction better" is not "profitable
   after costs," which almost nobody in this sub-field tests.
7. **This mirrors `bnn_forecaster`'s own experience this session** (mechanism worked end-to-end,
   calibration didn't hold up — see its README's "Calibration is NOT verified" section): the
   engineering/plumbing side of these solutions is usually solid and reproducible; the "does it
   actually generate alpha after realistic costs" side is where almost everything in this space —
   including AutoBNN — currently falls short of real evidence.

## References (primary sources cited above)

- TFT crypto strategy: https://www.mdpi.com/2079-8954/13/6/474
- DLinear/NLinear (AAAI 2023): https://ojs.aaai.org/index.php/AAAI/article/view/26317/26089 ; code: https://github.com/cure-lab/LTSF-Linear
- MambaStock: https://arxiv.org/abs/2402.18959 ; optimizer follow-up: https://arxiv.org/abs/2508.04707
- N-HiTS vs N-BEATS for finance: https://arxiv.org/abs/2409.00480
- Pretrained TSFMs for financial return forecasting (2026): https://arxiv.org/abs/2606.27100
- Re(Visiting) TSFMs in Finance (2025): https://arxiv.org/abs/2511.18578
- Benchmarking Deep Time Series Models for Equity Portfolios (2026): https://arxiv.org/abs/2606.09420
- FinRL: https://arxiv.org/abs/2011.09607 ; FinRL-Meta: https://arxiv.org/abs/2112.06753 ; FinRL-Podracer: https://arxiv.org/abs/2111.05188
- DRL backtest-overfitting detection: https://arxiv.org/abs/2209.05559
- FinRL Contests: https://arxiv.org/abs/2504.02281
- RL in Financial Applications review (2024): https://arxiv.org/abs/2411.12746
- TradingAgents: https://arxiv.org/abs/2412.20138
- FinMem: https://arxiv.org/abs/2311.13743
- FinGPT: https://arxiv.org/abs/2307.10485
- Parametric look-ahead bias in LLM backtesting: https://arxiv.org/abs/2605.24564
- Look-ahead bias in GPT sentiment analysis: https://arxiv.org/abs/2309.17322
- HATS: https://arxiv.org/abs/1908.07999
- DGDNN: https://arxiv.org/abs/2401.01846
