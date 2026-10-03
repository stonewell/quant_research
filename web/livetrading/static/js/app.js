/**
 * Live Trading Station - Modern Responsive Frontend Controller
 */

(function () {
    // -------------------------------------------------------------------------
    // Application State
    // -------------------------------------------------------------------------
    const state = {
        config: null,
        currentDate: new Date().toISOString().split("T")[0],
        holdings: {},
        accountState: {},
        activeTicket: [],
        activeHealth: null,
        historyRuns: [],
        isHistoricalView: false,
    };

    const KNOWN_STOCK_NAMES = {
        "601872.SH": "招商轮船", "601728.SH": "中国电信", "601288.SH": "农业银行",
        "601225.SH": "陕西煤业", "601919.SH": "中远海控", "601111.SH": "中国国航",
        "601857.SH": "中国石油", "601601.SH": "中国太保", "600941.SH": "中国移动",
        "600028.SH": "中国石化", "601088.SH": "中国神华", "601398.SH": "工商银行",
        "600362.SH": "江西铜业", "600000.SH": "浦发银行", "000157.SZ": "中联重科",
        "601166.SH": "兴业银行", "601390.SH": "中国中铁", "600519.SH": "贵州茅台",
        "601186.SH": "中国铁建", "600862.SH": "中航高科",
        "300394.SZ": "天孚通信", "601899.SH": "紫金矿业", "600584.SH": "长电科技",
        "600660.SH": "福耀玻璃", "000938.SZ": "紫光股份", "002371.SZ": "北方华创",
        "688008.SH": "澜起科技", "603501.SH": "韦尔股份", "688012.SH": "中微公司",
        "688041.SH": "海光信息", "688981.SH": "中芯国际", "002156.SZ": "通富微电",
        "688072.SH": "拓荆科技", "688120.SH": "华海清科", "300750.SZ": "宁德时代",
        "300059.SZ": "东方财富", "300033.SZ": "同花顺", "300760.SZ": "迈瑞医疗",
        "002460.SZ": "赣锋锂业", "600309.SH": "万华化学", "002049.SZ": "紫光国微",
        "688111.SH": "金山办公", "300496.SZ": "中科创达", "300308.SZ": "中际旭创",
        "300502.SZ": "新易盛", "300274.SZ": "阳光电源", "002812.SZ": "恩捷股份",
        "688599.SH": "天合光能", "600406.SH": "国电南瑞", "300347.SZ": "泰格医药",
        "300122.SZ": "智飞生物", "688271.SH": "联影医疗", "600276.SH": "恒瑞医药",
        "BIL": "流动性现金", "SHV": "短期美债ETF", "511880.SH": "银华日利ETF", "511990.SH": "华宝添益ETF",
    };

    function resolveStockDisplayName(sym, name) {
        if (name && name !== "Custom" && name !== "custom" && name !== sym) {
            return name;
        }
        return KNOWN_STOCK_NAMES[sym] || name || sym;
    }

    // DOM Elements
    const el = {
        selectStrategy: document.getElementById("select-strategy"),
        inputDate: document.getElementById("input-as-of-date"),
        inputValue: document.getElementById("input-portfolio-value"),
        inputPeakNav: document.getElementById("input-peak-nav"),
        selectProvider: document.getElementById("select-provider"),
        btnRunHealth: document.getElementById("btn-run-health"),
        btnRunDeploy: document.getElementById("btn-run-deploy"),
        btnRunAll: document.getElementById("btn-run-all"),
        btnSettings: document.getElementById("btn-open-settings"),
        btnShowAll: document.getElementById("btn-show-all-segments"),

        // Universe
        selectUniverse: document.getElementById("select-universe"),
        textCustomSymbols: document.getElementById("textarea-custom-symbols"),
        badgeUniverseCount: document.getElementById("badge-universe-count"),
        btnSaveCustomUniverse: document.getElementById("btn-save-custom-universe"),

        // Holdings
        btnAddHolding: document.getElementById("btn-add-holding"),
        btnSaveHoldings: document.getElementById("btn-save-holdings"),
        tbodyHoldings: document.getElementById("tbody-holdings"),
        valHoldingsEquity: document.getElementById("val-holdings-equity"),
        valHoldingsCash: document.getElementById("val-holdings-cash"),
        valHoldingsCashRatio: document.getElementById("val-holdings-cash-ratio"),

        // History
        selectHistoryDate: document.getElementById("select-history-date"),
        btnLoadDate: document.getElementById("btn-load-date"),
        btnTodayView: document.getElementById("btn-today-view"),

        // Health Outputs
        badgeDirective: document.getElementById("badge-gate-directive"),
        mDrawdown: document.getElementById("m-drawdown"),
        mPeakInfo: document.getElementById("m-peak-info"),
        mTier: document.getElementById("m-tier"),
        mTierScale: document.getElementById("m-tier-scale"),
        mBreadth: document.getElementById("m-breadth"),
        mThrust: document.getElementById("m-thrust"),
        mVol: document.getElementById("m-vol"),
        mVolScale: document.getElementById("m-vol-scale"),
        directiveTitle: document.getElementById("directive-title"),
        directiveDesc: document.getElementById("directive-desc"),
        directiveBox: document.getElementById("directive-box"),

        // Orders Outputs
        btnDownloadCsv: document.getElementById("btn-download-csv"),
        ticketSummaryText: document.getElementById("ticket-summary-text"),
        tbodyOrders: document.getElementById("tbody-orders"),

        // Console
        consoleToggle: document.getElementById("console-toggle"),
        consoleBody: document.getElementById("console-body"),
        consoleOutput: document.getElementById("console-output"),
        consoleStatus: document.getElementById("console-status"),

        // Mobile
        mobileTabs: document.getElementById("mobile-tabs"),
        mBtnRunHealth: document.getElementById("m-btn-run-health"),
        mBtnRunDeploy: document.getElementById("m-btn-run-deploy"),
        mBtnSaveHoldings: document.getElementById("m-btn-save-holdings"),

        // Modals
        modalSettings: document.getElementById("modal-settings"),
        btnCloseSettings: document.getElementById("btn-close-settings"),
        btnSaveSettings: document.getElementById("btn-save-settings"),
        modalSaveUniverse: document.getElementById("modal-save-universe"),
        btnCloseUnivModal: document.getElementById("btn-close-universe-modal"),
        btnConfirmSaveUniv: document.getElementById("btn-confirm-save-universe"),
        inputCustomUnivName: document.getElementById("input-custom-universe-name"),
        inputCustomUnivDesc: document.getElementById("input-custom-universe-desc"),
        previewCustomSymbols: document.getElementById("preview-custom-symbols"),

        // Running overlay & toast
        loadingOverlay: document.getElementById("loading-overlay"),
        loadingTitle: document.getElementById("loading-title"),
        loadingDetail: document.getElementById("loading-detail"),
        toastNotification: document.getElementById("toast-notification"),
    };

    // -------------------------------------------------------------------------
    // Toast & Running Indicator Helpers
    // -------------------------------------------------------------------------
    function showToast(msg, type = "success", duration = 4000) {
        if (!el.toastNotification) return;
        el.toastNotification.textContent = msg;
        el.toastNotification.className = `toast-notification active toast-${type}`;
        setTimeout(() => {
            el.toastNotification.classList.remove("active");
        }, duration);
    }

    let runningRestoreHandlers = [];

    function setRunningIndicator(isRunning, actionText = "", activeBtn = null) {
        const runButtons = [
            el.btnRunHealth, el.btnRunDeploy, el.btnRunAll,
            el.mBtnRunHealth, el.mBtnRunDeploy
        ].filter(Boolean);

        if (isRunning) {
            runButtons.forEach(btn => {
                const origHtml = btn.innerHTML;
                btn.disabled = true;
                runningRestoreHandlers.push(() => {
                    btn.disabled = false;
                    btn.innerHTML = origHtml;
                });
            });

            if (activeBtn) {
                activeBtn.innerHTML = '<span class="spinner-sm"></span> 运行中...';
            }

            if (el.loadingOverlay) {
                el.loadingOverlay.classList.add("active");
                if (el.loadingTitle) el.loadingTitle.textContent = "⏳ 正在执行实盘运算与风控门禁...";
                if (el.loadingDetail) el.loadingDetail.textContent = actionText;
            }

            setConsoleStatus("运行中 (Running)...", true);
            if (el.consoleBody) el.consoleBody.style.display = "block";
        } else {
            while (runningRestoreHandlers.length > 0) {
                const restore = runningRestoreHandlers.pop();
                try { restore(); } catch (e) {}
            }
            if (el.loadingOverlay) {
                el.loadingOverlay.classList.remove("active");
            }
            setConsoleStatus("就绪 (Idle)", false);
        }
    }

    // -------------------------------------------------------------------------
    // Session State Management (localStorage)
    // -------------------------------------------------------------------------
    const SESSION_KEYS = {
        STRAT_KEY: "livetrading_session_strat_key",
        STRAT_VAL: "livetrading_session_strat_val",
        UNIV_KEY: "livetrading_session_univ_key",
        UNIV_VAL: "livetrading_session_univ_val",
        CUSTOM_SYMBOLS: "livetrading_session_custom_symbols",
        DATA_PROVIDER: "livetrading_session_data_provider",
    };

    function getActiveKeys() {
        const stratOpt = el.selectStrategy ? el.selectStrategy.selectedOptions[0] : null;
        const univOpt = el.selectUniverse ? el.selectUniverse.selectedOptions[0] : null;

        const stratKey = stratOpt?.dataset.strategyKey || "";
        const stratName = stratOpt?.dataset.strategyName || stratOpt?.textContent || "";
        const stratVal = stratOpt?.value || "";

        let univKey = "";
        let univName = "";
        let univVal = univOpt?.value || "";

        if (univVal === "__CUSTOM__") {
            const customText = el.textCustomSymbols ? el.textCustomSymbols.value.trim() : "";
            const syms = customText ? customText.split(/[\s,;\n]+/).filter(s => s && s.trim()) : [];
            univKey = syms.length > 0 ? `custom_${syms.length}_symbols` : "";
            univName = syms.length > 0 ? `自定义标的池 (${syms.length} 只标的)` : "";
        } else if (univOpt) {
            univKey = univOpt.dataset.universeKey || "";
            univName = univOpt.textContent || "";
        }

        return {
            stratKey,
            stratName,
            stratVal,
            univKey,
            univName,
            univVal,
            isBothSelected: Boolean(stratKey && univKey),
        };
    }

    function saveSessionState() {
        const keys = getActiveKeys();
        try {
            if (keys.stratKey) {
                localStorage.setItem(SESSION_KEYS.STRAT_KEY, keys.stratKey);
                localStorage.setItem(SESSION_KEYS.STRAT_VAL, keys.stratVal);
            }
            if (keys.univVal) {
                localStorage.setItem(SESSION_KEYS.UNIV_VAL, keys.univVal);
                localStorage.setItem(SESSION_KEYS.UNIV_KEY, keys.univKey);
            }
            if (keys.univVal === "__CUSTOM__" && el.textCustomSymbols) {
                localStorage.setItem(SESSION_KEYS.CUSTOM_SYMBOLS, el.textCustomSymbols.value);
            }
            if (el.selectProvider && el.selectProvider.value) {
                localStorage.setItem(SESSION_KEYS.DATA_PROVIDER, el.selectProvider.value);
            }
        } catch (e) {
            console.warn("Could not save session state to localStorage:", e);
        }
    }

    function restoreSessionState() {
        try {
            const savedStratVal = localStorage.getItem(SESSION_KEYS.STRAT_VAL);
            const savedStratKey = localStorage.getItem(SESSION_KEYS.STRAT_KEY);
            if (savedStratVal || savedStratKey) {
                for (let i = 0; i < el.selectStrategy.options.length; i++) {
                    const opt = el.selectStrategy.options[i];
                    if (opt.value === savedStratVal || (savedStratKey && opt.dataset.strategyKey === savedStratKey)) {
                        el.selectStrategy.selectedIndex = i;
                        break;
                    }
                }
            }

            const savedUnivVal = localStorage.getItem(SESSION_KEYS.UNIV_VAL);
            const savedUnivKey = localStorage.getItem(SESSION_KEYS.UNIV_KEY);
            if (savedUnivVal || savedUnivKey) {
                for (let i = 0; i < el.selectUniverse.options.length; i++) {
                    const opt = el.selectUniverse.options[i];
                    if (opt.value === savedUnivVal || (savedUnivKey && opt.dataset.universeKey === savedUnivKey)) {
                        el.selectUniverse.selectedIndex = i;
                        if (opt.value === "__CUSTOM__") {
                            const syms = localStorage.getItem(SESSION_KEYS.CUSTOM_SYMBOLS) || "";
                            el.textCustomSymbols.value = syms;
                            onCustomSymbolsInput();
                        } else {
                            onUniverseChange(false);
                        }
                        break;
                    }
                }
            }

            const savedProvider = localStorage.getItem(SESSION_KEYS.DATA_PROVIDER);
            if (savedProvider && savedProvider !== "synthetic" && el.selectProvider) {
                for (let i = 0; i < el.selectProvider.options.length; i++) {
                    if (el.selectProvider.options[i].value === savedProvider) {
                        el.selectProvider.selectedIndex = i;
                        break;
                    }
                }
            }
        } catch (e) {
            console.warn("Could not restore session state from localStorage:", e);
        }
    }

    function renderRunBundle(bundle) {
        if (!bundle) return;
        if (bundle.health_report) {
            renderHealthReport(bundle.health_report);
        }
        if (bundle.trading_ticket) {
            renderTradingTicket(bundle.trading_ticket);
            el.btnDownloadCsv.disabled = false;
        }
        const priceMap = bundle.prices ? { ...bundle.prices } : {};
        if (bundle.trading_ticket && Array.isArray(bundle.trading_ticket)) {
            bundle.trading_ticket.forEach(t => {
                if (t.symbol && t.price !== undefined && t.price !== null) {
                    const p = parseFloat(t.price);
                    if (p > 0) priceMap[t.symbol.toUpperCase()] = p;
                }
            });
        }
        if (bundle.health_report?.market_macro?.leaders) {
            bundle.health_report.market_macro.leaders.forEach(l => {
                if (l.symbol && l.last_price && priceMap[l.symbol.toUpperCase()] === undefined) {
                    const p = parseFloat(l.last_price);
                    if (p > 0) priceMap[l.symbol.toUpperCase()] = p;
                }
            });
        }

        const runProvider = bundle.summary?.data_provider || bundle.data_provider;
        if (runProvider && el.selectProvider) {
            el.selectProvider.value = runProvider;
            saveSessionState();
        }

        if (bundle.account_state) {
            state.accountState = bundle.account_state;
            if (bundle.account_state.peak_nav && el.inputPeakNav) {
                el.inputPeakNav.placeholder = `历史高位: ¥${bundle.account_state.peak_nav.toLocaleString()}`;
            }
            if (bundle.account_state.current_nav && el.inputValue) {
                el.inputValue.value = bundle.account_state.current_nav;
            }
        }

        if (bundle.holdings && Object.keys(bundle.holdings).length > 0) {
            renderHoldingsTable(bundle.holdings, priceMap);
            recalcHoldings();
            fetchQuotesForCurrentHoldings();
        } else {
            recalcHoldings();
        }
    }

    function resetRunDisplaysToAwaiting(reason = "") {
        el.badgeDirective.className = "badge badge-lg badge-neutral";
        el.badgeDirective.textContent = "待运行 (AWAITING)";
        el.mDrawdown.textContent = "--%";
        el.mTier.textContent = "NORMAL";
        el.mTierScale.textContent = "仓位乘数: 100%";
        el.mBreadth.textContent = "--%";
        el.mThrust.textContent = "--%";
        el.mVol.textContent = "--%";
        el.mVolScale.textContent = "Barroso缩放: 1.00x";
        el.directiveTitle.textContent = "尚未执行健康检查";
        el.directiveDesc.textContent = reason || "点击上方「1. 健康门禁」评估账户回撤梯级、连续冷却状态与市场宽度。";

        el.tbodyOrders.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无调仓指令</td></tr>';
        el.ticketSummaryText.textContent = reason || "尚未生成调仓指令。请点击上方「2. 生成指令」按最新行情计算。";
        el.btnDownloadCsv.disabled = true;
    }

    async function loadLatestRunForCurrentPair(silent = false) {
        const keys = getActiveKeys();
        if (!keys.isBothSelected) {
            resetRunDisplaysToAwaiting("请先选择策略与标的池以查看运行结果。");
            return null;
        }

        try {
            const res = await fetch(`/api/runs/latest?strategy=${encodeURIComponent(keys.stratKey)}&universe=${encodeURIComponent(keys.univKey)}`);
            if (res.ok) {
                const bundle = await res.json();
                renderRunBundle(bundle);
                if (bundle.date) {
                    el.inputDate.value = bundle.date;
                    if (el.ticketSummaryText) {
                        el.ticketSummaryText.textContent = `[展示 ${bundle.date} 最新运算结果] ` + el.ticketSummaryText.textContent;
                    }
                }
                if (!silent) {
                    logConsole(`[SESSION] 已恢复上次会话: 策略「${keys.stratName}」| 标的池「${keys.univName}」，已呈现最新运行结果 (${bundle.date})`);
                }
                return bundle;
            } else {
                resetRunDisplaysToAwaiting(`当前策略「${keys.stratName}」与标的池「${keys.univName}」暂无历史运行记录。点击上方「1. 健康门禁」或「2. 生成指令」开始计算。`);
                return null;
            }
        } catch (err) {
            console.warn("Could not load latest run:", err);
            return null;
        }
    }

    async function updateScopedHistoryRuns() {
        const keys = getActiveKeys();
        if (!keys.isBothSelected) {
            el.selectHistoryDate.innerHTML = '<option value="">-- 请先选择策略与标的池以查看归档 --</option>';
            el.selectHistoryDate.disabled = true;
            el.btnLoadDate.disabled = true;
            state.historyRuns = [];
            return [];
        }

        try {
            const res = await fetch(`/api/runs?strategy=${encodeURIComponent(keys.stratKey)}&universe=${encodeURIComponent(keys.univKey)}&require_both=true`);
            const runs = await res.json();
            state.historyRuns = runs || [];

            el.selectHistoryDate.innerHTML = "";
            if (!runs || runs.length === 0) {
                el.selectHistoryDate.innerHTML = '<option value="">-- 该策略与标的池暂无历史归档 --</option>';
                el.selectHistoryDate.disabled = true;
                el.btnLoadDate.disabled = true;
            } else {
                const defaultOpt = document.createElement("option");
                defaultOpt.value = "";
                defaultOpt.textContent = `-- 选择该策略与标的池的历史归档 (${runs.length}条记录) --`;
                el.selectHistoryDate.appendChild(defaultOpt);

                runs.forEach(r => {
                    const opt = document.createElement("option");
                    opt.value = r.date;
                    opt.textContent = `${r.date} | ${r.directive} (${r.trade_count} 笔交易) | ${r.tier}`;
                    el.selectHistoryDate.appendChild(opt);
                });
                el.selectHistoryDate.disabled = false;
                el.btnLoadDate.disabled = false;
            }
            return runs;
        } catch (err) {
            logConsole(`[WARN] 获取历史归档列表失败: ${err}`);
            return [];
        }
    }

    async function onStrategyChange() {
        saveSessionState();
        await updateScopedHistoryRuns();
        await loadLatestRunForCurrentPair(true);
    }

    async function handleDateSelected(targetDate, updateUrl = true) {
        if (!targetDate) return null;
        state.currentDate = targetDate;
        if (el.inputDate) el.inputDate.value = targetDate;

        const keys = getActiveKeys();

        if (updateUrl) {
            const url = new URL(window.location.href);
            url.searchParams.set("date", targetDate);
            if (keys.stratKey) url.searchParams.set("strategy", keys.stratKey);
            if (keys.univKey) url.searchParams.set("universe", keys.univKey);
            window.history.replaceState({}, document.title, url.toString());
        }
        saveSessionState();

        if (!keys.isBothSelected) {
            await loadHoldings(targetDate);
            await loadAccountState(targetDate);
            recalcHoldings();
            return null;
        }

        // 1. Try to load exact historical run bundle for targetDate
        try {
            const res = await fetch(`/api/runs/${encodeURIComponent(keys.stratKey)}/${encodeURIComponent(keys.univKey)}/${encodeURIComponent(targetDate)}`);
            if (res.ok) {
                const bundle = await res.json();
                const hasHealth = !!bundle.health_report;
                const hasTicket = bundle.has_csv || (bundle.trading_ticket && bundle.trading_ticket.length > 0);
                if (hasHealth || hasTicket) {
                    state.isHistoricalView = true;
                    renderRunBundle(bundle);
                    if (el.selectHistoryDate) {
                        el.selectHistoryDate.value = targetDate;
                    }
                    logConsole(`[ARCHIVE] 成功加载 ${targetDate} 实盘运行快照 (含持仓、状态与工单)`);
                    showToast(`✅ 已刷新并呈现 ${targetDate} 最新实盘运算结果！`, "success");
                    return bundle;
                }
            }
        } catch (err) {
            // Ignore error and proceed to previous run fallback
        }

        // 2. Exact run bundle not available for targetDate:
        // Reset historical view state and reset run displays to awaiting
        state.isHistoricalView = false;
        if (el.selectHistoryDate) {
            el.selectHistoryDate.value = "";
        }
        resetRunDisplaysToAwaiting(`基准日 ${targetDate} 暂无历史运行归档。可点击上方「1. 健康门禁」或「2. 生成指令」按此基准日计算。`);

        // Load account info and holdings from previous available date run data
        try {
            const prevRes = await fetch(`/api/runs/previous?date=${encodeURIComponent(targetDate)}&strategy=${encodeURIComponent(keys.stratKey)}&universe=${encodeURIComponent(keys.univKey)}`);
            if (prevRes.ok) {
                const prevData = await prevRes.json();
                if (prevData.has_previous) {
                    const prevDate = prevData.previous_date;
                    // Load account state FIRST so nav is available for holdings recalc
                    state.accountState = prevData.account_state || {};
                    if (prevData.account_state) {
                        if (prevData.account_state.peak_nav && el.inputPeakNav) {
                            el.inputPeakNav.placeholder = `历史高位: ¥${prevData.account_state.peak_nav.toLocaleString()}`;
                        }
                        if (prevData.account_state.current_nav && el.inputValue) {
                            el.inputValue.value = prevData.account_state.current_nav;
                        }
                    }

                    // Load holdings
                    state.holdings = prevData.holdings || {};
                    const priceMap = prevData.prices ? { ...prevData.prices } : {};
                    if (prevData.bundle?.trading_ticket) {
                        prevData.bundle.trading_ticket.forEach(t => {
                            if (t.symbol && t.price) priceMap[t.symbol.toUpperCase()] = parseFloat(t.price);
                        });
                    }
                    renderHoldingsTable(state.holdings, priceMap);
                    recalcHoldings();
                    fetchQuotesForCurrentHoldings();

                    const symCount = Object.keys(state.holdings).filter(k => k !== "BIL" && k !== "CASH").length;
                    logConsole(`[SESSION] 基准日 ${targetDate} 暂无运行归档，已自动继承前一可用基准日 (${prevDate}) 的账户信息与持仓 (${symCount} 只标的)，就绪待计算。`);
                    showToast(`📅 基准日 ${targetDate} 无运行数据，已载入前一日 (${prevDate}) 账户与持仓`, "info");
                    return null;
                }
            }
        } catch (err) {
            console.warn("Failed to load previous run:", err);
        }

        // 3. Fallback if no previous run data found at all
        await loadAccountState(targetDate);
        await loadHoldings(targetDate);
        recalcHoldings();
        logConsole(`[SESSION] 已切换至基准日 ${targetDate} (该日暂无历史归档，就绪待计算)`);
        showToast(`📅 已切换至基准日 ${targetDate} (该日暂无归档，就绪待计算)`, "info");
        return null;
    }

    async function onTopDateChange() {
        const newDate = el.inputDate.value;
        if (!newDate) return;
        await handleDateSelected(newDate, true);
    }

    // -------------------------------------------------------------------------
    // Initialization
    // -------------------------------------------------------------------------
    async function init() {
        bindEvents();
        await loadConfig();
        restoreSessionState();

        // Check if redirected/refreshed with specific URL query parameters
        const urlParams = new URLSearchParams(window.location.search);
        const qStrat = urlParams.get("strategy");
        const qUniv = urlParams.get("universe");
        const qDate = urlParams.get("date");
        const qTab = urlParams.get("tab");

        // Prioritize URL strategy/universe if present
        if (qStrat) {
            for (let i = 0; i < el.selectStrategy.options.length; i++) {
                const opt = el.selectStrategy.options[i];
                if (opt.value === qStrat || opt.dataset.strategyKey === qStrat) {
                    el.selectStrategy.selectedIndex = i;
                    break;
                }
            }
        }
        if (qUniv) {
            for (let i = 0; i < el.selectUniverse.options.length; i++) {
                const opt = el.selectUniverse.options[i];
                if (opt.value === qUniv || opt.dataset.universeKey === qUniv) {
                    el.selectUniverse.selectedIndex = i;
                    if (opt.value === "__CUSTOM__") {
                        const syms = localStorage.getItem(SESSION_KEYS.CUSTOM_SYMBOLS) || "";
                        el.textCustomSymbols.value = syms;
                        onCustomSymbolsInput();
                    } else {
                        onUniverseChange(false);
                    }
                    break;
                }
            }
        }

        const qProvider = urlParams.get("provider");
        if (qProvider && el.selectProvider) {
            for (let i = 0; i < el.selectProvider.options.length; i++) {
                if (el.selectProvider.options[i].value === qProvider) {
                    el.selectProvider.selectedIndex = i;
                    saveSessionState();
                    break;
                }
            }
        }

        await updateScopedHistoryRuns();

        const activeDate = qDate || el.inputDate.value || state.currentDate;
        if (activeDate) {
            await handleDateSelected(activeDate, false);
        } else {
            await loadHoldings();
            await loadAccountState();
            await loadLatestRunForCurrentPair(false);
            recalcHoldings();
        }

        // If on mobile, switch tab; on desktop, keep all four segments visible
        if (window.innerWidth <= 1024 && qTab && qTab !== "all") {
            if (qTab === "orders") {
                handleMobileTabSwitch("tab-orders");
                const tabBtn = el.mobileTabs?.querySelector('[data-tab="tab-orders"]');
                if (tabBtn) {
                    el.mobileTabs.querySelectorAll(".mobile-tab").forEach(t => t.classList.remove("active"));
                    tabBtn.classList.add("active");
                }
            } else if (qTab === "health") {
                handleMobileTabSwitch("tab-health");
                const tabBtn = el.mobileTabs?.querySelector('[data-tab="tab-health"]');
                if (tabBtn) {
                    el.mobileTabs.querySelectorAll(".mobile-tab").forEach(t => t.classList.remove("active"));
                    tabBtn.classList.add("active");
                }
            }
        } else {
            showAllFourSegments(false);
        }

        // Smooth scroll to results
        if (qTab) {
            setTimeout(() => {
                const targetEl = (qTab === "orders") ? document.getElementById("card-orders") : document.getElementById("card-health");
                if (targetEl) targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
            }, 300);
        }
    }

    function bindEvents() {
        // Show All 4 Segments (Universe, Holdings, Health, Orders)
        if (el.btnShowAll) el.btnShowAll.addEventListener("click", () => showAllFourSegments(true));

        // Date on top of screen: change triggers page refresh & loading archive data
        if (el.inputDate) {
            el.inputDate.addEventListener("change", onTopDateChange);
            el.inputDate.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    e.preventDefault();
                    onTopDateChange();
                }
            });
            // Open Chrome calendar picker immediately when clicking anywhere on the input
            el.inputDate.addEventListener("click", () => {
                try {
                    if (typeof el.inputDate.showPicker === "function") {
                        el.inputDate.showPicker();
                    }
                } catch (e) {
                    // Ignore if picker already open or browser restriction
                }
            });
        }

        // Strategy, Universe & Data Provider Change
        el.selectStrategy.addEventListener("change", onStrategyChange);
        el.selectUniverse.addEventListener("change", () => onUniverseChange(true));
        el.textCustomSymbols.addEventListener("input", onCustomSymbolsInput);
        if (el.selectProvider) {
            el.selectProvider.addEventListener("change", () => {
                saveSessionState();
                fetchQuotesForCurrentHoldings();
            });
        }

        // Run Actions
        el.btnRunHealth.addEventListener("click", () => runHealthCheck(el.btnRunHealth));
        el.btnRunDeploy.addEventListener("click", () => runLiveDeploy(el.btnRunDeploy));
        el.btnRunAll.addEventListener("click", () => runFullWorkflow(el.btnRunAll));

        if (el.mBtnRunHealth) el.mBtnRunHealth.addEventListener("click", () => runHealthCheck(el.mBtnRunHealth));
        if (el.mBtnRunDeploy) el.mBtnRunDeploy.addEventListener("click", () => runLiveDeploy(el.mBtnRunDeploy));
        if (el.mBtnSaveHoldings) el.mBtnSaveHoldings.addEventListener("click", () => saveHoldings());

        // Custom Universe Modal
        el.btnSaveCustomUniverse.addEventListener("click", openSaveUniverseModal);
        el.btnConfirmSaveUniv.addEventListener("click", confirmSaveUniverse);
        el.btnCloseUnivModal.addEventListener("click", () => el.modalSaveUniverse.classList.remove("active"));

        // Holdings
        el.btnAddHolding.addEventListener("click", () => addHoldingRow("", 0, null));
        el.btnSaveHoldings.addEventListener("click", () => saveHoldings());
        el.inputValue.addEventListener("input", recalcHoldings);

        // History
        el.btnLoadDate.addEventListener("click", onLoadDateClicked);
        el.btnTodayView.addEventListener("click", () => showAllFourSegments(true));
        el.selectHistoryDate.addEventListener("change", onLoadDateClicked);

        // Orders
        el.btnDownloadCsv.addEventListener("click", downloadTicketCsv);

        // Console
        el.consoleToggle.addEventListener("click", () => {
            el.consoleBody.style.display = el.consoleBody.style.display === "none" ? "block" : "none";
        });

        // Settings Modal
        el.btnSettings.addEventListener("click", openSettingsModal);
        el.btnCloseSettings.addEventListener("click", () => el.modalSettings.classList.remove("active"));
        el.btnSaveSettings.addEventListener("click", saveSettings);

        // Mobile Tabs
        if (el.mobileTabs) {
            el.mobileTabs.querySelectorAll(".mobile-tab").forEach(tab => {
                tab.addEventListener("click", () => {
                    el.mobileTabs.querySelectorAll(".mobile-tab").forEach(t => t.classList.remove("active"));
                    tab.classList.add("active");
                    const tabId = tab.dataset.tab;
                    handleMobileTabSwitch(tabId);
                });
            });
        }
    }

    // -------------------------------------------------------------------------
    // API & Data Loaders
    // -------------------------------------------------------------------------
    async function loadConfig() {
        try {
            const res = await fetch("/api/config");
            const data = await res.json();
            state.config = data;

            // Populate Strategies: SHOW ONLY STRATEGY NAME, NOT FILE NAME!
            el.selectStrategy.innerHTML = "";
            (data.strategies || []).forEach(st => {
                const opt = document.createElement("option");
                opt.value = st.rel_path || st.path;
                opt.dataset.strategyKey = st.strategy_key || "";
                opt.dataset.strategyName = st.strategy_name || st.name;
                opt.textContent = st.strategy_name || st.name;
                el.selectStrategy.appendChild(opt);
            });

            // Set default date
            if (data.default_date && !el.inputDate.value) {
                el.inputDate.value = data.default_date;
                state.currentDate = data.default_date;
            }

            // Set default NAV
            if (data.default_portfolio_value) {
                el.inputValue.value = data.default_portfolio_value;
            }

            // Set default data provider: if saved in localStorage and is not stale 'synthetic', use it; else use settings default
            const savedProvider = localStorage.getItem(SESSION_KEYS.DATA_PROVIDER);
            const serverDefaultProvider = (data.settings && data.settings.default_data_provider) || "marketdb";
            if (el.selectProvider) {
                if (savedProvider && savedProvider !== "synthetic") {
                    el.selectProvider.value = savedProvider;
                } else {
                    el.selectProvider.value = serverDefaultProvider;
                    localStorage.setItem(SESSION_KEYS.DATA_PROVIDER, serverDefaultProvider);
                }
            }

            // Populate Universes
            populateUniverseSelect(data.universes || []);
        } catch (err) {
            logConsole(`[ERROR] 加载配置失败: ${err}`);
        }
    }

    function populateUniverseSelect(universes) {
        el.selectUniverse.innerHTML = "";

        // Custom Entry Option
        const optCustom = document.createElement("option");
        optCustom.value = "__CUSTOM__";
        optCustom.dataset.universeKey = "";
        optCustom.textContent = "✏️ 自定义输入标的代码 (Custom Symbols)";
        el.selectUniverse.appendChild(optCustom);

        universes.forEach(u => {
            const opt = document.createElement("option");
            opt.value = u.path || u.name;
            opt.textContent = `${u.name} [${u.count}只标的]`;
            opt.dataset.symbols = JSON.stringify(u.symbols || []);
            opt.dataset.type = u.type;
            opt.dataset.universeKey = u.universe_key || "";
            if (u.rel_path) opt.dataset.relPath = u.rel_path;
            el.selectUniverse.appendChild(opt);
        });

        // Default to first preset if available
        const firstPreset = universes.find(u => u.type === "preset");
        if (firstPreset) {
            el.selectUniverse.value = firstPreset.path || firstPreset.name;
            onUniverseChange(false);
        }
    }

    function onUniverseChange(isUserAction = true) {
        const selected = el.selectUniverse.selectedOptions[0];
        if (!selected) return;

        if (selected.value === "__CUSTOM__") {
            el.textCustomSymbols.value = "";
            el.badgeUniverseCount.textContent = "0 标的";
        } else {
            try {
                const syms = JSON.parse(selected.dataset.symbols || "[]");
                el.textCustomSymbols.value = syms.join(", ");
                el.badgeUniverseCount.textContent = `${syms.length} 标的`;
            } catch (e) {
                el.textCustomSymbols.value = "";
            }
        }

        if (isUserAction) {
            saveSessionState();
            updateScopedHistoryRuns();
            loadLatestRunForCurrentPair(true);
        }
    }

    function onCustomSymbolsInput() {
        const raw = el.textCustomSymbols.value;
        const syms = raw.split(/[\s,;\n]+/).filter(s => s && s.trim());
        el.badgeUniverseCount.textContent = `${syms.length} 标的`;
        saveSessionState();
        updateScopedHistoryRuns();
        loadLatestRunForCurrentPair(true);
    }

    // -------------------------------------------------------------------------
    // Holdings Interactive Manager
    // -------------------------------------------------------------------------
    function renderHoldingsTable(holdingsMap, priceMap = {}) {
        el.tbodyHoldings.innerHTML = "";
        const keys = Object.keys(holdingsMap).filter(k => k !== "BIL" && k !== "CASH");

        if (keys.length === 0) {
            addHoldingRow("", 0, null, null);
            return;
        }

        keys.forEach(sym => {
            const val = holdingsMap[sym];
            const price = priceMap[sym.toUpperCase()] !== undefined ? priceMap[sym.toUpperCase()] : (priceMap[sym] !== undefined ? priceMap[sym] : null);
            if (typeof val === "number") {
                if (val <= 1.0 && !Number.isInteger(val)) {
                    addHoldingRow(sym, null, val, price);
                } else {
                    addHoldingRow(sym, parseInt(val), null, price);
                }
            } else if (val && typeof val === "object") {
                const p = price !== null ? price : (val.price || null);
                addHoldingRow(sym, val.shares || 0, val.weight || null, p);
            }
        });
    }

    function addHoldingRow(symbol = "", shares = 0, weight = null, price = null) {
        const tr = document.createElement("tr");

        const tdSym = document.createElement("td");
        const inSym = document.createElement("input");
        inSym.type = "text";
        inSym.className = "form-input form-input-sm holding-sym";
        inSym.value = symbol;
        inSym.placeholder = "如: 600519.SH";
        inSym.addEventListener("input", () => {
            recalcHoldings();
            debounceFetchQuotes();
        });
        tdSym.appendChild(inSym);

        const tdShares = document.createElement("td");
        const inShares = document.createElement("input");
        inShares.type = "number";
        inShares.className = "form-input form-input-sm holding-shares";
        inShares.value = (shares !== null && shares > 0) ? shares : "";
        inShares.placeholder = "股数 (优先)";
        inShares.addEventListener("input", recalcHoldings);
        tdShares.appendChild(inShares);

        const tdWeight = document.createElement("td");
        const inWeight = document.createElement("input");
        inWeight.type = "number";
        inWeight.className = "form-input form-input-sm holding-weight";
        inWeight.value = (weight !== null) ? (weight * 100).toFixed(1) : "";
        inWeight.placeholder = "%";
        inWeight.step = "0.1";
        inWeight.addEventListener("input", recalcHoldings);
        tdWeight.appendChild(inWeight);

        const tdPrice = document.createElement("td");
        tdPrice.className = "holding-price text-muted font-mono";
        if (price !== null && !isNaN(price) && Number(price) > 0) {
            tdPrice.textContent = `¥${Number(price).toFixed(2)}`;
            tdPrice.dataset.price = Number(price);
        } else {
            tdPrice.textContent = "--";
            tdPrice.dataset.price = "";
        }

        const tdVal = document.createElement("td");
        tdVal.className = "holding-val font-mono";
        tdVal.textContent = "¥0.00";

        const tdAction = document.createElement("td");
        const btnDel = document.createElement("button");
        btnDel.className = "btn btn-secondary btn-sm";
        btnDel.textContent = "✕";
        btnDel.title = "删除此行";
        btnDel.addEventListener("click", () => {
            tr.remove();
            recalcHoldings();
        });
        tdAction.appendChild(btnDel);

        tr.appendChild(tdSym);
        tr.appendChild(tdShares);
        tr.appendChild(tdWeight);
        tr.appendChild(tdPrice);
        tr.appendChild(tdVal);
        tr.appendChild(tdAction);

        el.tbodyHoldings.appendChild(tr);
        recalcHoldings();
    }

    function getHoldingsFromTable() {
        const rows = el.tbodyHoldings.querySelectorAll("tr");
        const holdings = {};
        rows.forEach(r => {
            const sym = r.querySelector(".holding-sym")?.value.trim().toUpperCase();
            if (!sym) return;
            const sharesVal = parseFloat(r.querySelector(".holding-shares")?.value);
            const weightVal = parseFloat(r.querySelector(".holding-weight")?.value);

            if (!isNaN(sharesVal) && sharesVal > 0) {
                holdings[sym] = sharesVal;
            } else if (!isNaN(weightVal) && weightVal > 0) {
                holdings[sym] = weightVal / 100.0;
            }
        });
        return holdings;
    }

    function recalcHoldings() {
        const nav = parseFloat(el.inputValue.value) || 100000.0;
        const rows = el.tbodyHoldings.querySelectorAll("tr");
        let totalEquity = 0.0;

        rows.forEach(r => {
            const sharesInput = r.querySelector(".holding-shares");
            const weightInput = r.querySelector(".holding-weight");
            const priceTd = r.querySelector(".holding-price");
            const valTd = r.querySelector(".holding-val");

            const shares = parseFloat(sharesInput?.value);
            const weight = parseFloat(weightInput?.value);
            const price = parseFloat(priceTd?.dataset.price || (priceTd?.textContent ? priceTd.textContent.replace(/[¥,]/g, "") : "0")) || 0.0;

            let rowVal = 0.0;
            if (!isNaN(shares) && shares > 0) {
                if (price > 0) {
                    rowVal = shares * price;
                } else if (!isNaN(weight) && weight > 0) {
                    rowVal = (weight / 100.0) * nav;
                }
                valTd.textContent = `¥${rowVal.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
                valTd.dataset.val = rowVal;
                totalEquity += rowVal;

                if (weightInput && document.activeElement !== weightInput) {
                    const rowWeightPct = nav > 0 ? ((rowVal / nav) * 100.0) : 0.0;
                    weightInput.value = rowWeightPct.toFixed(1);
                }
            } else if (!isNaN(weight) && weight > 0) {
                rowVal = (weight / 100.0) * nav;
                valTd.textContent = `¥${rowVal.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
                valTd.dataset.val = rowVal;
                totalEquity += rowVal;

                if (sharesInput && document.activeElement !== sharesInput && price > 0) {
                    const estShares = Math.floor(rowVal / price / 100) * 100;
                    if (estShares > 0 && !sharesInput.value) {
                        sharesInput.placeholder = `~${estShares}`;
                    }
                }
            } else {
                valTd.textContent = "¥0.00";
                valTd.dataset.val = 0;
                if (weightInput && document.activeElement !== weightInput && (!sharesInput || !sharesInput.value)) {
                    weightInput.value = "";
                }
            }
        });

        const cash = Math.max(0, nav - totalEquity);
        const cashRatio = nav > 0 ? (cash / nav) * 100.0 : 0.0;

        el.valHoldingsEquity.textContent = `¥${totalEquity.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        el.valHoldingsCash.textContent = `¥${cash.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        el.valHoldingsCashRatio.textContent = `${cashRatio.toFixed(1)}%`;
    }

    let fetchQuotesTimer = null;
    function debounceFetchQuotes() {
        if (fetchQuotesTimer) clearTimeout(fetchQuotesTimer);
        fetchQuotesTimer = setTimeout(fetchQuotesForCurrentHoldings, 500);
    }

    async function fetchQuotesForCurrentHoldings() {
        const rows = el.tbodyHoldings.querySelectorAll("tr");
        const symbols = [];
        rows.forEach(r => {
            const sym = r.querySelector(".holding-sym")?.value.trim().toUpperCase();
            if (sym && sym !== "BIL" && sym !== "CASH") symbols.push(sym);
        });

        if (symbols.length === 0) return;

        const dateVal = el.inputDate?.value || state.currentDate;
        const provider = el.selectProvider?.value || "marketdb";
        const keys = getActiveKeys();

        try {
            const res = await fetch("/api/quotes", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    symbols: symbols,
                    as_of_date: dateVal,
                    data_provider: provider,
                    strategy_key: keys.stratKey,
                    universe_key: keys.univKey,
                })
            });
            if (res.ok) {
                const data = await res.json();
                const quotes = data.quotes || {};
                rows.forEach(r => {
                    const sym = r.querySelector(".holding-sym")?.value.trim().toUpperCase();
                    if (sym && quotes[sym] && quotes[sym].price !== null && quotes[sym].price !== undefined) {
                        const price = Number(quotes[sym].price);
                        const priceTd = r.querySelector(".holding-price");
                        if (priceTd && price > 0) {
                            priceTd.textContent = `¥${price.toFixed(2)}`;
                            priceTd.dataset.price = price;
                        }
                    }
                });
                recalcHoldings();
            }
        } catch (err) {
            console.warn("Failed to fetch quotes:", err);
        }
    }

    async function loadHoldings(dateStr = null) {
        try {
            const targetDate = dateStr || el.inputDate?.value || state.currentDate;
            const keys = getActiveKeys();
            let url = "/api/holdings";
            const params = new URLSearchParams();
            if (targetDate) params.append("date", targetDate);
            if (keys.stratKey) params.append("strategy", keys.stratKey);
            if (keys.univKey) params.append("universe", keys.univKey);
            if (params.toString()) url += `?${params.toString()}`;

            const res = await fetch(url);
            if (res.ok) {
                const data = await res.json();
                state.holdings = data || {};
                renderHoldingsTable(state.holdings);
                recalcHoldings();
                fetchQuotesForCurrentHoldings();
            }
        } catch (err) {
            console.warn("Failed to load holdings:", err);
        }
    }

    async function loadAccountState(dateStr = null) {
        try {
            const targetDate = dateStr || el.inputDate?.value || state.currentDate;
            const keys = getActiveKeys();
            let url = "/api/account-state";
            const params = new URLSearchParams();
            if (targetDate) params.append("date", targetDate);
            if (keys.stratKey) params.append("strategy", keys.stratKey);
            if (keys.univKey) params.append("universe", keys.univKey);
            if (params.toString()) url += `?${params.toString()}`;

            const res = await fetch(url);
            if (res.ok) {
                const data = await res.json();
                state.accountState = data || {};
                if (data.peak_nav && el.inputPeakNav) {
                    el.inputPeakNav.placeholder = `历史高位: ¥${data.peak_nav.toLocaleString()}`;
                }
                if (data.current_nav && el.inputValue) {
                    el.inputValue.value = data.current_nav;
                    recalcHoldings();
                }
            }
        } catch (err) {
            console.warn("Failed to load account state:", err);
        }
    }

    async function saveHoldings() {
        const holdings = getHoldingsFromTable();
        const dateVal = el.inputDate.value || state.currentDate;
        const nav = parseFloat(el.inputValue.value) || 100000.0;
        const keys = getActiveKeys();

        try {
            setConsoleStatus("正在保存持仓...", true);
            const res = await fetch("/api/holdings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    holdings: holdings,
                    date: dateVal,
                    portfolio_value: nav,
                    strategy_key: keys.stratKey,
                    universe_key: keys.univKey,
                }),
            });
            const data = await res.json();
            logConsole(`[SUCCESS] 持仓配置已成功保存 (${Object.keys(holdings).length} 只持仓标的已持久化)`);
            setConsoleStatus("持仓已保存", false);
        } catch (err) {
            logConsole(`[ERROR] 保存持仓失败: ${err}`);
            setConsoleStatus("保存出错", false);
        }
    }

    // -------------------------------------------------------------------------
    // Execution: Health Check & Live Deploy
    // -------------------------------------------------------------------------
    // -------------------------------------------------------------------------
    // Execution: Health Check & Live Deploy
    // -------------------------------------------------------------------------
    function getExecutionParams() {
        const strat = el.selectStrategy.value;
        const dateVal = el.inputDate.value || state.currentDate;
        const nav = parseFloat(el.inputValue.value) || 100000.0;
        const peakNav = parseFloat(el.inputPeakNav.value) || null;
        const provider = el.selectProvider.value;

        const keys = getActiveKeys();

        // Custom symbols or file
        const selectedUnivOpt = el.selectUniverse.selectedOptions[0];
        const isCustomUniv = !selectedUnivOpt || selectedUnivOpt.value === "__CUSTOM__" || selectedUnivOpt.dataset.type === "custom";
        const customText = el.textCustomSymbols.value.trim();
        const customSymbols = (isCustomUniv && customText) ? customText.split(/[\s,;\n]+/).filter(s => s && s.trim()) : null;
        const univFile = (!isCustomUniv && selectedUnivOpt && selectedUnivOpt.value !== "__CUSTOM__") ? selectedUnivOpt.value : null;

        return {
            strategy_file: strat,
            universe_file: univFile,
            custom_symbols: customSymbols,
            strategy_key: keys.stratKey,
            strategy_name: keys.stratName,
            universe_key: keys.univKey,
            universe_name: keys.univName,
            as_of_date: dateVal,
            portfolio_value: nav,
            peak_nav: peakNav,
            data_provider: provider,
        };
    }

    async function runHealthCheck(activeBtn = el.btnRunHealth) {
        const params = getExecutionParams();
        const keys = getActiveKeys();
        setRunningIndicator(true, "正在评估账户最大回撤、熔断等级、市场宽度与实现波动率...", activeBtn);
        logConsole(`\n[Stage 1] 启动账户健康与宏观风控门禁...\n策略: ${keys.stratName}\n标的池: ${keys.univName}\n基准日: ${params.as_of_date} | NAV: ¥${params.portfolio_value.toLocaleString()}`);

        try {
            const res = await fetch("/api/run-health", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(params),
            });
            const data = await res.json();

            if (data.stdout) logConsole(data.stdout);
            if (data.stderr) logConsole(`[STDERR]\n${data.stderr}`);

            if (data.status === "error") {
                throw new Error(data.message || "执行返回错误");
            }

            if (data.health_report) {
                renderHealthReport(data.health_report);
            }
            if (data.account_state) {
                state.accountState = data.account_state;
            }

            saveSessionState();
            await updateScopedHistoryRuns();

            setRunningIndicator(false);
            showToast("✅ Stage 1 健康门禁完成！已呈现最新风控指标", "success");

            const targetDate = params.as_of_date || state.currentDate;
            const newUrl = `${window.location.pathname}?strategy=${encodeURIComponent(keys.stratKey)}&universe=${encodeURIComponent(keys.univKey)}&date=${encodeURIComponent(targetDate)}&tab=health`;
            window.history.pushState({}, document.title, newUrl);

            const targetEl = document.getElementById("card-health");
            if (targetEl) targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
            return data;
        } catch (err) {
            logConsole(`[ERROR] 健康门禁运行失败: ${err.message || err}`);
            setRunningIndicator(false);
            showToast(`❌ 健康门禁运行失败: ${err.message || err}`, "error");
        }
    }

    async function runLiveDeploy(activeBtn = el.btnRunDeploy) {
        const baseParams = getExecutionParams();
        const keys = getActiveKeys();
        const holdings = getHoldingsFromTable();
        const deployParams = {
            ...baseParams,
            current_holdings: holdings,
        };

        setRunningIndicator(true, "正在计算标的目标权重、惯性过滤、持仓对账与买卖工单...", activeBtn);
        logConsole(`\n[Stage 2] 启动实盘调仓工单生成...\n策略: ${keys.stratName}\n标的池: ${keys.univName}\n持仓数: ${Object.keys(holdings).length} | 数据源: ${deployParams.data_provider}`);

        try {
            const res = await fetch("/api/run-deploy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(deployParams),
            });
            const data = await res.json();

            if (data.stdout) logConsole(data.stdout);
            if (data.stderr) logConsole(`[STDERR]\n${data.stderr}`);

            if (data.status === "error") {
                throw new Error(data.message || "执行返回错误");
            }

            if (data.health_report) {
                renderHealthReport(data.health_report);
            }
            if (data.trading_ticket) {
                renderTradingTicket(data.trading_ticket);
                el.btnDownloadCsv.disabled = false;
            }
            if (data.account_state) {
                state.accountState = data.account_state;
            }

            saveSessionState();
            await updateScopedHistoryRuns();

            setRunningIndicator(false);
            showToast("✅ Stage 2 调仓指令生成完成！已呈现最新工单", "success");

            const targetDate = deployParams.as_of_date || state.currentDate;
            const newUrl = `${window.location.pathname}?strategy=${encodeURIComponent(keys.stratKey)}&universe=${encodeURIComponent(keys.univKey)}&date=${encodeURIComponent(targetDate)}&tab=orders`;
            window.history.pushState({}, document.title, newUrl);

            const targetEl = document.getElementById("card-orders");
            if (targetEl) targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
            return data;
        } catch (err) {
            logConsole(`[ERROR] 实盘调仓运行失败: ${err.message || err}`);
            setRunningIndicator(false);
            showToast(`❌ 实盘调仓运行失败: ${err.message || err}`, "error");
        }
    }

    async function runFullWorkflow(activeBtn = el.btnRunAll) {
        const baseParams = getExecutionParams();
        const keys = getActiveKeys();
        const holdings = getHoldingsFromTable();
        const deployParams = {
            ...baseParams,
            current_holdings: holdings,
        };

        setRunningIndicator(true, "正在依次执行 Stage 1 健康门禁 与 Stage 2 实盘调仓工单生成...", activeBtn);
        logConsole(`\n[WORKFLOW] 启动全流程执行 (Stage 1 + Stage 2)...\n策略: ${keys.stratName}\n标的池: ${keys.univName}`);

        try {
            // Stage 1
            if (el.loadingDetail) el.loadingDetail.textContent = "第 1/2 步: 正在执行 Stage 1 账户健康与宏观风控门禁...";
            const resHealth = await fetch("/api/run-health", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(baseParams),
            });
            const dataHealth = await resHealth.json();
            if (dataHealth.stdout) logConsole(dataHealth.stdout);
            if (dataHealth.stderr) logConsole(`[STDERR]\n${dataHealth.stderr}`);
            if (dataHealth.status === "error") {
                throw new Error(`Stage 1 失败: ${dataHealth.message}`);
            }
            if (dataHealth.health_report) {
                renderHealthReport(dataHealth.health_report);
            }

            // Stage 2
            if (el.loadingDetail) el.loadingDetail.textContent = "第 2/2 步: 正在执行 Stage 2 实盘调仓工单生成与买卖对账...";
            const resDeploy = await fetch("/api/run-deploy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(deployParams),
            });
            const dataDeploy = await resDeploy.json();
            if (dataDeploy.stdout) logConsole(dataDeploy.stdout);
            if (dataDeploy.stderr) logConsole(`[STDERR]\n${dataDeploy.stderr}`);
            if (dataDeploy.status === "error") {
                throw new Error(`Stage 2 失败: ${dataDeploy.message}`);
            }
            if (dataDeploy.trading_ticket) {
                renderTradingTicket(dataDeploy.trading_ticket);
                el.btnDownloadCsv.disabled = false;
            }
            if (dataDeploy.account_state) {
                state.accountState = dataDeploy.account_state;
            }

            saveSessionState();
            await updateScopedHistoryRuns();

            setRunningIndicator(false);
            showToast("⚡ 全流程执行完成！已更新 Stage 1 风控与 Stage 2 调仓工单", "success");

            const targetDate = deployParams.as_of_date || state.currentDate;
            const newUrl = `${window.location.pathname}?strategy=${encodeURIComponent(keys.stratKey)}&universe=${encodeURIComponent(keys.univKey)}&date=${encodeURIComponent(targetDate)}&tab=all`;
            window.history.pushState({}, document.title, newUrl);
        } catch (err) {
            logConsole(`[FAIL] 全流程中断: ${err.message || err}`);
            setRunningIndicator(false);
            showToast(`❌ 全流程中断: ${err.message || err}`, "error");
        }
    }

    // -------------------------------------------------------------------------
    // Rendering: Health & Orders
    // -------------------------------------------------------------------------
    function renderHealthReport(rep) {
        if (!rep) return;

        // Directive Badge
        const dir = rep.directive || "UNKNOWN";
        el.badgeDirective.className = "badge badge-lg";
        if (dir === "GO") {
            el.badgeDirective.classList.add("badge-go");
            el.badgeDirective.textContent = "🟢 GO (准许交易 / 正常风控预算)";
        } else if (dir === "CAUTION") {
            el.badgeDirective.classList.add("badge-caution");
            el.badgeDirective.textContent = "🟡 CAUTION (防守阻尼 / 缩减仓位)";
        } else {
            el.badgeDirective.classList.add("badge-nogo");
            el.badgeDirective.textContent = "🔴 NO-GO (熔断停牌 / 严禁开仓)";
        }

        // Metrics
        const dd = rep.drawdown !== undefined ? (rep.drawdown * 100).toFixed(2) : "--";
        el.mDrawdown.textContent = `${dd}%`;
        if (rep.peak_nav) {
            el.mPeakInfo.textContent = `HWM: ¥${rep.peak_nav.toLocaleString()}`;
        }

        el.mTier.textContent = rep.circuit_breaker_tier || "NORMAL";
        if (rep.linear_equity_scale !== undefined) {
            el.mTierScale.textContent = `仓位乘数: ${(rep.linear_equity_scale * 100).toFixed(0)}%`;
        }

        const breadth = rep.market_breadth_50d !== undefined ? (rep.market_breadth_50d * 100).toFixed(1) : "--";
        el.mBreadth.textContent = `${breadth}%`;

        const thrust = rep.breadth_thrust_10d !== undefined ? (rep.breadth_thrust_10d * 100).toFixed(1) : "--";
        el.mThrust.textContent = `${thrust}%`;

        const vol = rep.realized_vol_21d !== undefined ? (rep.realized_vol_21d * 100).toFixed(1) : "--";
        el.mVol.textContent = `${vol}%`;
        if (rep.vol_scaling_factor !== undefined) {
            el.mVolScale.textContent = `Barroso缩放: ${rep.vol_scaling_factor.toFixed(2)}x`;
        }

        // Directive Box
        el.directiveTitle.textContent = rep.directive_label || `门禁指令: ${dir}`;
        el.directiveDesc.textContent = rep.directive_summary || "宏观宽度、动量冲击与账户回撤已综合核算。";
    }

    function renderTradingTicket(ticketRows) {
        el.tbodyOrders.innerHTML = "";
        if (!ticketRows || ticketRows.length === 0) {
            el.tbodyOrders.innerHTML = '<tr><td colspan="9" class="text-center text-muted">今日无需执行任何调仓指令</td></tr>';
            el.ticketSummaryText.textContent = "今日无需调仓 (No trades required).";
            return;
        }

        let buysCount = 0;
        let sellsCount = 0;
        let totalBuyVal = 0.0;
        let totalSellVal = 0.0;

        ticketRows.forEach(r => {
            const tr = document.createElement("tr");

            // Action Badge
            const tdAct = document.createElement("td");
            const badge = document.createElement("span");
            badge.className = "badge";
            const act = r.action || "HOLD";

            if (act === "SELL") {
                badge.classList.add("badge-sell");
                badge.textContent = "🔴 SELL (卖出)";
                sellsCount++;
                totalSellVal += Math.abs(r.trade_value || 0);
            } else if (act === "BUY") {
                badge.classList.add("badge-buy");
                badge.textContent = "🟢 BUY (买入)";
                buysCount++;
                totalBuyVal += Math.abs(r.trade_value || 0);
            } else if (act.includes("CASH") || act.includes("SWEEP")) {
                badge.classList.add("badge-sweep");
                badge.textContent = "💰 CASH SWEEP";
            } else {
                badge.classList.add("badge-hold");
                badge.textContent = "⚪ HOLD (维持)";
            }
            tdAct.appendChild(badge);

            const tdSym = document.createElement("td");
            tdSym.className = "font-mono";
            tdSym.textContent = r.symbol;

            const tdName = document.createElement("td");
            tdName.textContent = resolveStockDisplayName(r.symbol, r.name);

            const tdPrice = document.createElement("td");
            tdPrice.className = "text-right font-mono";
            tdPrice.textContent = r.price ? r.price.toFixed(2) : "--";

            const tdCurW = document.createElement("td");
            tdCurW.className = "text-right font-mono";
            tdCurW.textContent = `${((r.current_weight || 0) * 100).toFixed(1)}%`;

            const tdTgtW = document.createElement("td");
            tdTgtW.className = "text-right font-mono";
            tdTgtW.textContent = `${((r.target_weight || 0) * 100).toFixed(1)}%`;

            const tdDeltaW = document.createElement("td");
            tdDeltaW.className = "text-right font-mono";
            const delta = (r.delta_weight || 0) * 100;
            tdDeltaW.textContent = `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}%`;
            if (delta > 0.01) tdDeltaW.classList.add("text-success");
            else if (delta < -0.01) tdDeltaW.classList.add("text-danger");

            const tdShares = document.createElement("td");
            tdShares.className = "text-right font-mono";
            const deltaShares = r.delta_shares || 0;
            tdShares.textContent = deltaShares !== 0 ? `${deltaShares > 0 ? "+" : ""}${deltaShares.toLocaleString()}` : "0";

            const tdVal = document.createElement("td");
            tdVal.className = "text-right font-mono";
            const tradeVal = r.trade_value || 0;
            tdVal.textContent = tradeVal !== 0 ? `¥${Math.abs(tradeVal).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : "--";

            tr.appendChild(tdAct);
            tr.appendChild(tdSym);
            tr.appendChild(tdName);
            tr.appendChild(tdPrice);
            tr.appendChild(tdCurW);
            tr.appendChild(tdTgtW);
            tr.appendChild(tdDeltaW);
            tr.appendChild(tdShares);
            tr.appendChild(tdVal);

            el.tbodyOrders.appendChild(tr);
        });

        el.ticketSummaryText.textContent = `调仓计划: 卖出 ${sellsCount} 只 (~¥${totalSellVal.toLocaleString()}) | 买入 ${buysCount} 只 (~¥${totalBuyVal.toLocaleString()})`;
    }

    // -------------------------------------------------------------------------
    // Historical Date Viewer (Scoped to Strategy & Universe)
    // -------------------------------------------------------------------------
    async function loadHistoricalDate(dateStr, stratKey = null, univKey = null) {
        if (!dateStr) return null;
        const keys = getActiveKeys();
        const sk = stratKey || keys.stratKey;
        const uk = univKey || keys.univKey;
        if (!sk || !uk) {
            showToast("请先选择策略与标的池以查看归档！", "warning");
            return null;
        }

        state.isHistoricalView = true;
        setConsoleStatus(`查看历史归档: ${dateStr}`, false);
        logConsole(`\n[ARCHIVE] 加载策略「${keys.stratName}」标的池「${keys.univName}」日期 ${dateStr} 的历史记录...`);

        try {
            const res = await fetch(`/api/runs/${encodeURIComponent(sk)}/${encodeURIComponent(uk)}/${encodeURIComponent(dateStr)}`);
            if (!res.ok) throw new Error("无法加载该日期的历史归档");
            const bundle = await res.json();

            renderRunBundle(bundle);

            el.inputDate.value = dateStr;
            if (el.selectHistoryDate) {
                el.selectHistoryDate.value = dateStr;
            }
            logConsole(`[ARCHIVE] 成功加载 ${dateStr} 实盘运行快照 (含持仓、状态与工单)`);
            return bundle;
        } catch (err) {
            logConsole(`[ERROR] 加载历史归档失败: ${err.message || err}`);
            throw err;
        }
    }

    async function onLoadDateClicked() {
        const keys = getActiveKeys();
        if (!keys.isBothSelected) {
            showToast("请先选择策略与标的池以查看归档！", "warning");
            return;
        }
        const dateStr = el.selectHistoryDate.value;
        if (!dateStr) return;
        await handleDateSelected(dateStr, true);
    }

    function onTodayViewClicked() {
        showAllFourSegments(true);
    }

    function downloadTicketCsv() {
        const keys = getActiveKeys();
        const dateVal = el.inputDate.value || state.currentDate;
        if (keys.isBothSelected) {
            window.open(`/api/runs/${encodeURIComponent(keys.stratKey)}/${encodeURIComponent(keys.univKey)}/${encodeURIComponent(dateVal)}/csv`, "_blank");
        } else {
            window.open(`/api/runs/${encodeURIComponent(dateVal)}/csv`, "_blank");
        }
    }

    // -------------------------------------------------------------------------
    // Modals: Custom Universe & Settings
    // -------------------------------------------------------------------------
    function openSaveUniverseModal() {
        const raw = el.textCustomSymbols.value.trim();
        const syms = raw.split(/[\s,;\n]+/).filter(s => s && s.trim());
        if (syms.length === 0) {
            alert("请先在标的代码框中输入至少一个标的代码！");
            return;
        }

        el.previewCustomSymbols.innerHTML = "";
        syms.forEach(s => {
            const span = document.createElement("span");
            span.className = "symbol-tag";
            span.textContent = s.toUpperCase();
            el.previewCustomSymbols.appendChild(span);
        });

        el.inputCustomUnivName.value = "";
        el.inputCustomUnivDesc.value = "";
        el.modalSaveUniverse.classList.add("active");
    }

    async function confirmSaveUniverse() {
        const name = el.inputCustomUnivName.value.trim();
        const desc = el.inputCustomUnivDesc.value.trim();
        const raw = el.textCustomSymbols.value.trim();
        const syms = raw.split(/[\s,;\n]+/).filter(s => s && s.trim());

        if (!name) {
            alert("请输入标的池名称！");
            return;
        }

        try {
            const res = await fetch("/api/universes", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, description: desc, symbols: syms }),
            });
            const created = await res.json();
            el.modalSaveUniverse.classList.remove("active");
            logConsole(`[SUCCESS] 自选标的池 '${name}' 已保存 (${syms.length} 标的)`);
            await loadConfig();
        } catch (err) {
            alert(`保存失败: ${err}`);
        }
    }

    async function openSettingsModal() {
        try {
            const res = await fetch("/api/settings");
            const cfg = await res.json();

            document.getElementById("cfg-repo-root").value = cfg.repo_root || "";
            document.getElementById("cfg-strategy-dir").value = cfg.strategy_dir || "";
            document.getElementById("cfg-universe-dir").value = cfg.universe_dir || "";
            document.getElementById("cfg-data-dir").value = cfg.data_dir || "";
            document.getElementById("cfg-archive-dir").value = cfg.archive_dir || "";
            document.getElementById("cfg-stage1-script").value = cfg.stage1_script || "";
            document.getElementById("cfg-stage2-script").value = cfg.stage2_script || "";
            document.getElementById("cfg-python-bin").value = cfg.python_executable || "";
            const cfgProv = document.getElementById("cfg-default-provider");
            if (cfgProv && cfg.default_data_provider) {
                cfgProv.value = cfg.default_data_provider;
            }

            el.modalSettings.classList.add("active");
        } catch (err) {
            alert(`读取配置失败: ${err}`);
        }
    }

    async function saveSettings() {
        const payload = {
            repo_root: document.getElementById("cfg-repo-root").value.trim(),
            strategy_dir: document.getElementById("cfg-strategy-dir").value.trim(),
            universe_dir: document.getElementById("cfg-universe-dir").value.trim(),
            data_dir: document.getElementById("cfg-data-dir").value.trim(),
            archive_dir: document.getElementById("cfg-archive-dir").value.trim(),
            stage1_script: document.getElementById("cfg-stage1-script").value.trim(),
            stage2_script: document.getElementById("cfg-stage2-script").value.trim(),
            python_executable: document.getElementById("cfg-python-bin").value.trim(),
            default_data_provider: document.getElementById("cfg-default-provider")?.value || "marketdb",
        };

        try {
            const res = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            await res.json();
            el.modalSettings.classList.remove("active");
            logConsole("[SUCCESS] 系统路径与配置已更新");
            await loadConfig();
        } catch (err) {
            alert(`保存设置失败: ${err}`);
        }
    }

    // -------------------------------------------------------------------------
    // Four Segments View Controller (Universe, Holdings, Health, Orders)
    // -------------------------------------------------------------------------
    function showAllFourSegments(clearArchive = true) {
        const leftPanel = document.getElementById("panel-left");
        const rightPanel = document.getElementById("panel-right");
        const cardUniv = document.getElementById("card-universe");
        const cardHold = document.getElementById("card-holdings");
        const cardHealth = document.getElementById("card-health");
        const cardOrders = document.getElementById("card-orders");
        const cardConsole = document.getElementById("card-console");
        const histBar = document.getElementById("history-bar");

        // 1. Remove all inline display overrides on cards and panels to show all 4 segments
        [cardUniv, cardHold, cardHealth, cardOrders, cardConsole].forEach(c => {
            if (c) c.style.display = "";
        });
        if (leftPanel) leftPanel.style.display = "";
        if (rightPanel) rightPanel.style.display = "";
        if (histBar) histBar.style.display = "";

        if (clearArchive) {
            // 2. Clear historical archive view state
            state.isHistoricalView = false;
            if (el.selectHistoryDate) {
                el.selectHistoryDate.value = "";
            }
            if (el.inputDate) {
                el.inputDate.value = state.currentDate;
            }

            // 3. Clear URL search query parameters (remove ?date=... and ?tab=...)
            if (window.location.search) {
                const cleanUrl = window.location.pathname;
                window.history.pushState({}, document.title, cleanUrl);
            }

            // 4. Reload latest active holdings and account state, and latest run for current pair
            loadHoldings();
            loadAccountState();
            loadLatestRunForCurrentPair(true);

            setConsoleStatus("就绪 (全部4板块全景)", false);
            logConsole(`\n[VIEW] 已恢复全部四大板块全景展示 (1.标的池 2.实盘持仓 3.健康风控 4.调仓工单)，退出归档查看。`);
            showToast("📑 已呈现全部四大核心板块 (标的、持仓、风控、指令)", "info", 2000);
        }

        // 5. Update mobile tabs active button to tab-all
        if (el.mobileTabs) {
            el.mobileTabs.querySelectorAll(".mobile-tab").forEach(t => t.classList.remove("active"));
            const tabAll = el.mobileTabs.querySelector('[data-tab="tab-all"]');
            if (tabAll) tabAll.classList.add("active");
        }
    }

    // -------------------------------------------------------------------------
    // Mobile Tab Navigation
    // -------------------------------------------------------------------------
    function handleMobileTabSwitch(tabId) {
        if (tabId === "tab-all") {
            showAllFourSegments(true);
            return;
        }

        const leftPanel = document.getElementById("panel-left");
        const rightPanel = document.getElementById("panel-right");
        const cardUniv = document.getElementById("card-universe");
        const cardHold = document.getElementById("card-holdings");
        const cardHealth = document.getElementById("card-health");
        const cardOrders = document.getElementById("card-orders");
        const cardConsole = document.getElementById("card-console");
        const histBar = document.getElementById("history-bar");

        // Hide all cards first on mobile
        [cardUniv, cardHold, cardHealth, cardOrders, cardConsole].forEach(c => {
            if (c) c.style.display = "none";
        });
        if (leftPanel) leftPanel.style.display = "flex";
        if (rightPanel) rightPanel.style.display = "flex";

        if (tabId === "tab-setup") {
            if (cardUniv) cardUniv.style.display = "block";
            if (rightPanel) rightPanel.style.display = "none";
        } else if (tabId === "tab-holdings") {
            if (cardHold) cardHold.style.display = "block";
            if (rightPanel) rightPanel.style.display = "none";
        } else if (tabId === "tab-health") {
            if (leftPanel) leftPanel.style.display = "none";
            if (cardHealth) cardHealth.style.display = "block";
            if (histBar) histBar.style.display = "none";
        } else if (tabId === "tab-orders") {
            if (leftPanel) leftPanel.style.display = "none";
            if (cardOrders) cardOrders.style.display = "block";
            if (histBar) histBar.style.display = "none";
        } else if (tabId === "tab-history") {
            if (leftPanel) leftPanel.style.display = "none";
            if (cardHealth) cardHealth.style.display = "block";
            if (cardOrders) cardOrders.style.display = "block";
            if (histBar) histBar.style.display = "flex";
        }
    }

    // -------------------------------------------------------------------------
    // Utilities
    // -------------------------------------------------------------------------
    function logConsole(msg) {
        el.consoleOutput.textContent += `\n${msg}`;
        el.consoleOutput.scrollTop = el.consoleOutput.scrollHeight;
    }

    function setConsoleStatus(text, isBusy) {
        el.consoleStatus.textContent = text;
        if (isBusy) {
            el.consoleStatus.classList.add("text-warning");
        } else {
            el.consoleStatus.classList.remove("text-warning");
        }
    }

    // Window Resize Handler: Ensure desktop always displays all 4 segments
    window.addEventListener("resize", () => {
        if (window.innerWidth > 1024) {
            const leftPanel = document.getElementById("panel-left");
            const rightPanel = document.getElementById("panel-right");
            const cardUniv = document.getElementById("card-universe");
            const cardHold = document.getElementById("card-holdings");
            const cardHealth = document.getElementById("card-health");
            const cardOrders = document.getElementById("card-orders");
            const cardConsole = document.getElementById("card-console");
            const histBar = document.getElementById("history-bar");

            [cardUniv, cardHold, cardHealth, cardOrders, cardConsole].forEach(c => {
                if (c) c.style.display = "";
            });
            if (leftPanel) leftPanel.style.display = "";
            if (rightPanel) rightPanel.style.display = "";
            if (histBar) histBar.style.display = "";
        }
    });

    // Bootstrap
    window.addEventListener("DOMContentLoaded", init);
})();
