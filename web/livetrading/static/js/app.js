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
    // Initialization
    // -------------------------------------------------------------------------
    async function init() {
        bindEvents();
        await loadConfig();
        await loadHoldings();
        await loadAccountState();
        await loadHistoryRuns();
        recalcHoldings();

        // Check if redirected/refreshed with a specific date
        const urlParams = new URLSearchParams(window.location.search);
        const qDate = urlParams.get("date");
        const qTab = urlParams.get("tab");
        if (qDate) {
            try {
                await loadHistoricalDate(qDate);
                showToast(`✅ 已刷新并呈现 ${qDate} 最新实盘运算结果！`, "success");

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
                    // On desktop or tab=all, ensure all four segments are shown
                    showAllFourSegments(false);
                }

                // Smooth scroll to results
                setTimeout(() => {
                    const targetEl = (qTab === "orders") ? document.getElementById("card-orders") : document.getElementById("card-health");
                    if (targetEl) targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
                }, 300);
            } catch (e) {
                console.warn("Could not load date from URL:", e);
            }
        }
    }

    function bindEvents() {
        // Show All 4 Segments (Universe, Holdings, Health, Orders)
        if (el.btnShowAll) el.btnShowAll.addEventListener("click", () => showAllFourSegments(true));

        // Run Actions
        el.btnRunHealth.addEventListener("click", () => runHealthCheck(el.btnRunHealth));
        el.btnRunDeploy.addEventListener("click", () => runLiveDeploy(el.btnRunDeploy));
        el.btnRunAll.addEventListener("click", () => runFullWorkflow(el.btnRunAll));

        if (el.mBtnRunHealth) el.mBtnRunHealth.addEventListener("click", () => runHealthCheck(el.mBtnRunHealth));
        if (el.mBtnRunDeploy) el.mBtnRunDeploy.addEventListener("click", () => runLiveDeploy(el.mBtnRunDeploy));
        if (el.mBtnSaveHoldings) el.mBtnSaveHoldings.addEventListener("click", () => saveHoldings());

        // Universe
        el.selectUniverse.addEventListener("change", onUniverseChange);
        el.textCustomSymbols.addEventListener("input", onCustomSymbolsInput);
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

            // Populate Strategies
            el.selectStrategy.innerHTML = "";
            (data.strategies || []).forEach(st => {
                const opt = document.createElement("option");
                opt.value = st.rel_path || st.path;
                opt.textContent = `${st.name} (${st.template_name || 'Template'})`;
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
        optCustom.textContent = "✏️ 自定义输入标的代码 (Custom Symbols)";
        el.selectUniverse.appendChild(optCustom);

        universes.forEach(u => {
            const opt = document.createElement("option");
            opt.value = u.path || u.name;
            opt.textContent = `${u.name} [${u.count}只标的]`;
            opt.dataset.symbols = JSON.stringify(u.symbols || []);
            opt.dataset.type = u.type;
            if (u.rel_path) opt.dataset.relPath = u.rel_path;
            el.selectUniverse.appendChild(opt);
        });

        // Default to first preset if available
        const firstPreset = universes.find(u => u.type === "preset");
        if (firstPreset) {
            el.selectUniverse.value = firstPreset.path || firstPreset.name;
            onUniverseChange();
        }
    }

    function onUniverseChange() {
        const selected = el.selectUniverse.selectedOptions[0];
        if (!selected) return;

        if (selected.value === "__CUSTOM__") {
            el.textCustomSymbols.value = "";
            el.badgeUniverseCount.textContent = "0 标的";
            return;
        }

        try {
            const syms = JSON.parse(selected.dataset.symbols || "[]");
            el.textCustomSymbols.value = syms.join(", ");
            el.badgeUniverseCount.textContent = `${syms.length} 标的`;
        } catch (e) {
            el.textCustomSymbols.value = "";
        }
    }

    function onCustomSymbolsInput() {
        const raw = el.textCustomSymbols.value;
        const syms = raw.split(/[\s,;\n]+/).filter(s => s && s.trim());
        el.badgeUniverseCount.textContent = `${syms.length} 标的`;
    }

    async function loadHoldings(dateStr = null) {
        try {
            const url = dateStr ? `/api/holdings?date=${dateStr}` : "/api/holdings";
            const res = await fetch(url);
            const data = await res.json();
            state.holdings = data || {};
            renderHoldingsTable(state.holdings);
            recalcHoldings();
        } catch (err) {
            logConsole(`[ERROR] 加载持仓失败: ${err}`);
        }
    }

    async function loadAccountState(dateStr = null) {
        try {
            const url = dateStr ? `/api/account-state?date=${dateStr}` : "/api/account-state";
            const res = await fetch(url);
            const data = await res.json();
            state.accountState = data || {};
            if (data.peak_nav) {
                el.inputPeakNav.placeholder = `历史高位: ¥${data.peak_nav.toLocaleString()}`;
            }
            if (data.current_nav && !el.inputValue.value) {
                el.inputValue.value = data.current_nav;
            }
        } catch (err) {
            logConsole(`[ERROR] 加载账户状态失败: ${err}`);
        }
    }

    async function loadHistoryRuns() {
        try {
            const res = await fetch("/api/runs");
            const runs = await res.json();
            state.historyRuns = runs || [];

            el.selectHistoryDate.innerHTML = '<option value="">-- 选择历史日期查看实盘归档 --</option>';
            runs.forEach(r => {
                const opt = document.createElement("option");
                opt.value = r.date;
                opt.textContent = `${r.date} | ${r.directive} (${r.trade_count} 笔交易) | ${r.tier}`;
                el.selectHistoryDate.appendChild(opt);
            });
        } catch (err) {
            logConsole(`[WARN] 获取历史归档列表失败: ${err}`);
        }
    }

    // -------------------------------------------------------------------------
    // Holdings Interactive Manager
    // -------------------------------------------------------------------------
    function renderHoldingsTable(holdingsMap) {
        el.tbodyHoldings.innerHTML = "";
        const keys = Object.keys(holdingsMap).filter(k => k !== "BIL" && k !== "CASH");

        if (keys.length === 0) {
            addHoldingRow("", 0, null);
            return;
        }

        keys.forEach(sym => {
            const val = holdingsMap[sym];
            if (typeof val === "number") {
                if (val <= 1.0 && !Number.isInteger(val)) {
                    addHoldingRow(sym, null, val);
                } else {
                    addHoldingRow(sym, parseInt(val), null);
                }
            } else if (val && typeof val === "object") {
                addHoldingRow(sym, val.shares || 0, val.weight || null);
            }
        });
    }

    function addHoldingRow(symbol = "", shares = 0, weight = null) {
        const tr = document.createElement("tr");

        const tdSym = document.createElement("td");
        const inSym = document.createElement("input");
        inSym.type = "text";
        inSym.className = "form-input form-input-sm holding-sym";
        inSym.value = symbol;
        inSym.placeholder = "如: 600519.SH";
        inSym.addEventListener("input", recalcHoldings);
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
        tdPrice.className = "holding-price text-muted";
        tdPrice.textContent = "--";

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
            const valTd = r.querySelector(".holding-val");

            const shares = parseFloat(sharesInput?.value);
            const weight = parseFloat(weightInput?.value);

            let rowVal = 0.0;
            if (!isNaN(shares) && shares > 0) {
                // If price is unknown, estimate from weight or placeholder
                rowVal = 0.0; // will be updated when price feeds run
            } else if (!isNaN(weight) && weight > 0) {
                rowVal = (weight / 100.0) * nav;
                totalEquity += rowVal;
                valTd.textContent = `¥${rowVal.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            }
        });

        const cash = Math.max(0, nav - totalEquity);
        const cashRatio = (cash / nav) * 100.0;

        el.valHoldingsEquity.textContent = `¥${totalEquity.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        el.valHoldingsCash.textContent = `¥${cash.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        el.valHoldingsCashRatio.textContent = `${cashRatio.toFixed(1)}%`;
    }

    async function saveHoldings() {
        const holdings = getHoldingsFromTable();
        const dateVal = el.inputDate.value || state.currentDate;
        const nav = parseFloat(el.inputValue.value) || 100000.0;

        try {
            setConsoleStatus("正在保存持仓...", true);
            const res = await fetch("/api/holdings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    holdings: holdings,
                    date: dateVal,
                    portfolio_value: nav,
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
    function getExecutionParams() {
        const strat = el.selectStrategy.value;
        const dateVal = el.inputDate.value || state.currentDate;
        const nav = parseFloat(el.inputValue.value) || 100000.0;
        const peakNav = parseFloat(el.inputPeakNav.value) || null;
        const provider = el.selectProvider.value;

        // Custom symbols or file
        const customText = el.textCustomSymbols.value.trim();
        const customSymbols = customText ? customText.split(/[\s,;\n]+/).filter(s => s && s.trim()) : null;
        const selectedUnivOpt = el.selectUniverse.selectedOptions[0];
        const univFile = (selectedUnivOpt && selectedUnivOpt.value !== "__CUSTOM__") ? selectedUnivOpt.value : null;

        return {
            strategy_file: strat,
            universe_file: univFile,
            custom_symbols: customSymbols,
            as_of_date: dateVal,
            portfolio_value: nav,
            peak_nav: peakNav,
            data_provider: provider,
        };
    }

    async function runHealthCheck(activeBtn = el.btnRunHealth) {
        const params = getExecutionParams();
        setRunningIndicator(true, "正在评估账户最大回撤、熔断等级、市场宽度与实现波动率...", activeBtn);
        logConsole(`\n[Stage 1] 启动账户健康与宏观风控门禁...\n策略: ${params.strategy_file}\n基准日: ${params.as_of_date} | NAV: ¥${params.portfolio_value.toLocaleString()}`);

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

            if (el.loadingTitle) el.loadingTitle.textContent = "✅ 健康检查完成！正在刷新页面呈现最新结果...";
            if (el.loadingDetail) el.loadingDetail.textContent = "即将刷新页面载入 Stage 1 完整风控指标...";

            const targetDate = params.as_of_date || state.currentDate;
            setTimeout(() => {
                window.location.href = `${window.location.pathname}?date=${encodeURIComponent(targetDate)}&tab=health`;
            }, 600);
            return data;
        } catch (err) {
            logConsole(`[ERROR] 健康门禁运行失败: ${err.message || err}`);
            setRunningIndicator(false);
            showToast(`❌ 健康门禁运行失败: ${err.message || err}`, "error");
        }
    }

    async function runLiveDeploy(activeBtn = el.btnRunDeploy) {
        const baseParams = getExecutionParams();
        const holdings = getHoldingsFromTable();
        const deployParams = {
            ...baseParams,
            current_holdings: holdings,
        };

        setRunningIndicator(true, "正在计算标的目标权重、惯性过滤、持仓对账与买卖工单...", activeBtn);
        logConsole(`\n[Stage 2] 启动实盘调仓工单生成...\n标的持仓数: ${Object.keys(holdings).length} | 数据源: ${deployParams.data_provider}`);

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

            if (el.loadingTitle) el.loadingTitle.textContent = "✅ 调仓指令生成完成！正在刷新页面呈现最新工单...";
            if (el.loadingDetail) el.loadingDetail.textContent = "即将刷新页面载入 Stage 2 完整调仓指令表...";

            const targetDate = deployParams.as_of_date || state.currentDate;
            setTimeout(() => {
                window.location.href = `${window.location.pathname}?date=${encodeURIComponent(targetDate)}&tab=orders`;
            }, 600);
            return data;
        } catch (err) {
            logConsole(`[ERROR] 实盘调仓运行失败: ${err.message || err}`);
            setRunningIndicator(false);
            showToast(`❌ 实盘调仓运行失败: ${err.message || err}`, "error");
        }
    }

    async function runFullWorkflow(activeBtn = el.btnRunAll) {
        const baseParams = getExecutionParams();
        const holdings = getHoldingsFromTable();
        const deployParams = {
            ...baseParams,
            current_holdings: holdings,
        };

        setRunningIndicator(true, "正在依次执行 Stage 1 健康门禁 与 Stage 2 实盘调仓工单生成...", activeBtn);
        logConsole("\n[WORKFLOW] 启动全流程执行 (Stage 1 + Stage 2)...");

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

            if (el.loadingTitle) el.loadingTitle.textContent = "✅ 全流程执行完成！正在刷新呈现最新结果...";
            if (el.loadingDetail) el.loadingDetail.textContent = "即将刷新页面载入最新风控指标与调仓工单...";

            const targetDate = deployParams.as_of_date || state.currentDate;
            setTimeout(() => {
                window.location.href = `${window.location.pathname}?date=${encodeURIComponent(targetDate)}&tab=all`;
            }, 600);
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
            tdName.textContent = r.name || r.symbol;

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
    // Historical Date Viewer
    // -------------------------------------------------------------------------
    async function loadHistoricalDate(dateStr) {
        if (!dateStr) return null;

        state.isHistoricalView = true;
        setConsoleStatus(`查看历史归档: ${dateStr}`, false);
        logConsole(`\n[ARCHIVE] 加载日期 ${dateStr} 的历史记录...`);

        try {
            const res = await fetch(`/api/runs/${dateStr}`);
            if (!res.ok) throw new Error("无法加载该日期的历史归档");
            const bundle = await res.json();

            // Render health
            if (bundle.health_report) {
                renderHealthReport(bundle.health_report);
            }
            // Render ticket
            if (bundle.trading_ticket) {
                renderTradingTicket(bundle.trading_ticket);
                el.btnDownloadCsv.disabled = false;
            }
            // Render holdings as of that day
            if (bundle.holdings) {
                renderHoldingsTable(bundle.holdings);
            }
            // Render account state
            if (bundle.account_state) {
                state.accountState = bundle.account_state;
                if (bundle.account_state.peak_nav) {
                    el.inputPeakNav.placeholder = `历史高位: ¥${bundle.account_state.peak_nav.toLocaleString()}`;
                }
            }

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
        const dateStr = el.selectHistoryDate.value;
        if (!dateStr) return;
        try {
            await loadHistoricalDate(dateStr);
        } catch (err) {
            showToast(`加载 ${dateStr} 失败: ${err.message || err}`, "error");
        }
    }

    function onTodayViewClicked() {
        showAllFourSegments(true);
    }

    function downloadTicketCsv() {
        const dateVal = el.inputDate.value || state.currentDate;
        window.open(`/api/runs/${dateVal}/csv`, "_blank");
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

            // 4. Reload latest active holdings and account state
            loadHoldings();
            loadAccountState();

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
