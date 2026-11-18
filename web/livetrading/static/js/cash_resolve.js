/**
 * (LVCASH, 2026-11-18) ONE cash resolution for the "闲置可用现金" summary card.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The card used to DERIVE cash as `NAV箱 − Σ持仓市值`.  That is only valid while both legs
 * describe the same instant.  After a run completed, the table was repainted from the
 * round's planned book (re-priced) while `#input-portfolio-value` still held the NAV the
 * operator had typed BEFORE the run -- so the subtraction produced a pure cross-timebase
 * residual.  Observed on the 2026-11-18 round: the engine marked NAV 198,739.52 with
 * equity 129,706.00 and cash 69,033.52, while the stale form NAV 197,652.52 printed
 * 67,946.52 -- exactly 1,087.00 short, and contradicting the NAV-provenance strip on the
 * same screen which already printed the authoritative 69,033.52.
 *
 * So the precedence is, in order:
 *   1. `authoritativeCash` -- the displayed round's OWN measured cash leg (same valuation
 *      date as the book on screen).  Used DIRECTLY, never subtracted from anything.
 *   2. `operatorCash`      -- an explicit `#input-cash` entry (blank => null, never 0).
 *   3. derived residual    -- `nav - Σequity`, clamped >= 0: the historical behaviour,
 *      kept only for a hypothetical NAV preview or a date with no run archive.
 *
 * Loaded BEFORE `app.js` (see `static/index.html`) and exposed as
 * `window.LiveTradingCash.resolveDisplayedCash`.  Also exported via `module.exports` so
 * `tests/test_cash_resolution_precedence.py` can run the SHIPPED arithmetic under node
 * instead of re-implementing it in Python.
 */
(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.LiveTradingCash = api;
    }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    //: Mirrors `recalcHoldings()`'s own default so a blank/invalid NAV box cannot change
    //: which number the card shows (it used to fall back to this same 100,000).
    const DEFAULT_NAV = 100000.0;

    /** Finite number or `null`. Blank strings are `null`, not 0 -- a blank field is absent. */
    function toFiniteNumber(value) {
        if (value === null || value === undefined) return null;
        if (typeof value === "string" && value.trim() === "") return null;
        if (typeof value === "boolean") return null;
        const num = typeof value === "number" ? value : parseFloat(value);
        if (!isFinite(num)) return null;
        return num;
    }

    /**
     * Resolve the cash the summary card displays.
     *
     * @param {Object} input
     * @param {number|null} input.authoritativeCash  The displayed round's measured cash leg
     *        (already bound to the book's valuation date by the caller; `null` when the
     *        date has no run archive or the operator typed a hypothetical NAV).
     * @param {number|null} input.operatorCash       `#input-cash` entry, blank => `null`.
     * @param {number|string|null} input.nav         NAV leg shown in the card's subtitle.
     * @param {number|string|null} input.totalEquity Sum of the repriced holdings' market value.
     * @returns {{cash: number, cashRatio: number, source: string, nav: number, totalEquity: number}}
     *          `source` is one of `authoritative_run` / `operator_input` / `derived_residual`.
     */
    function resolveDisplayedCash(input) {
        const opts = input || {};

        const navParsed = toFiniteNumber(opts.nav);
        const nav = navParsed === null ? DEFAULT_NAV : navParsed;
        const equityParsed = toFiniteNumber(opts.totalEquity);
        const totalEquity = equityParsed === null ? 0.0 : equityParsed;

        const authoritative = toFiniteNumber(opts.authoritativeCash);
        const operator = toFiniteNumber(opts.operatorCash);

        let cash;
        let source;
        if (authoritative !== null) {
            // 1. Measured this round: take it verbatim. NOT clamped -- if the engine ever
            // reports a negative balance the card must show the engine's number rather
            // than quietly rewriting it to zero.
            cash = authoritative;
            source = "authoritative_run";
        } else if (operator !== null) {
            // 2. Explicit operator entry wins over any DERIVED guess.
            cash = operator;
            source = "operator_input";
        } else {
            // 3. Historical fallback: residual of the two VISIBLE legs, clamped.
            cash = Math.max(0.0, nav - totalEquity);
            source = "derived_residual";
        }

        // (review NIT 4, 2026-11-18) Only the PERCENTAGE is clamped to [0, 100]: a negative or
        // >100% proportion is nonsense for the card's ratio line (it cannot be what the bar
        // should read), but the cash FIGURE above stays VERBATIM even when it is negative or
        // larger than NAV -- the card must show what the engine reported, never a corrected
        // number.  The derived branch is already >= 0 by construction.
        let cashRatio = nav > 0 ? (cash / nav) * 100.0 : 0.0;
        cashRatio = Math.min(100.0, Math.max(0.0, cashRatio));
        return {
            cash: cash,
            cashRatio: cashRatio,
            source: source,
            nav: nav,
            totalEquity: totalEquity,
        };
    }

    return {
        resolveDisplayedCash: resolveDisplayedCash,
        toFiniteNumber: toFiniteNumber,
        DEFAULT_NAV: DEFAULT_NAV,
    };
});
