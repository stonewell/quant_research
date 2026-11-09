"""Throwaway verifier: recompute LV23 flip-vs-stable lag segmentation from debug logs.

Reads /tmp/opencode/diag_a0*.log (produced by /tmp/opencode/run_bt_debug.py with
logging.DEBUG on pipeline.research.blend.blend_base), parses per-bar:
  - budgets            -> macro regime label = argmax(budgets)
  - pre-renorm         -> immediate re-normalized target (blend_base.py:584)
  - delta-clamped      -> applied bucket weights (blend_base.py:604); absent => applied == pre
Then: lag_t = (applied_t != pre_t); regime_flip_t = (regime_t != regime_{t-1}).
"""
import ast
import re
import sys
from collections import Counter

WINDOW_START = "2022-01-04"
WINDOW_END = "2026-10-22"

ARMS = [
    ("a00_base_015", 0.15),
    ("a01_none", None),
    ("a02_030", 0.30),
    ("a03_060", 0.60),
]

LINE_RE = re.compile(r"regime weighting bar=(\d{4}-\d{2}-\d{2})[^:]*: regime weighting bar=\S+ (.*)$")
DATE_RE = re.compile(r"bar=(\d{4}-\d{2}-\d{2})")


def parse_log(path):
    bars = {}  # date -> {"budgets":..., "pre":..., "app":...}
    order = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if "regime weighting bar=" not in line:
                continue
            m = DATE_RE.search(line)
            if not m:
                continue
            d = m.group(1)
            if d not in bars:
                bars[d] = {}
                order.append(d)
            rec = bars[d]
            if "budgets=" in line:
                rec["budgets"] = ast.literal_eval(line.split("budgets=", 1)[1].split(" members=", 1)[0])
            if "pre-renorm=" in line:
                rec["pre"] = ast.literal_eval(line.split("pre-renorm=", 1)[1].strip())
            if "delta-clamped=" in line:
                rec["app"] = ast.literal_eval(line.split("delta-clamped=", 1)[1].strip())
    return order, bars


def argmax_vec(v):
    # deterministic: highest value, tie -> fixed key order
    keys = sorted(v, key=lambda k: (-v[k], k))
    return keys[0]


def l1(a, b):
    keys = set(a) | set(b)
    return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def analyze(arm, order, bars, lo, hi):
    dates = [d for d in order if lo <= d <= hi]
    regime = {}
    lag = {}
    l1s = {}
    for d in dates:
        rec = bars[d]
        regime[d] = argmax_vec(rec["budgets"]) if "budgets" in rec else None
        pre = rec.get("pre", {})
        app = rec.get("app", pre)
        lag[d] = l1(pre, app) > 1e-12
        l1s[d] = l1(pre, app)

    flips = 0
    for i, d in enumerate(dates):
        if i == 0:
            continue
        if regime[d] != regime[dates[i - 1]]:
            flips += 1

    hist = Counter(regime[d] for d in dates)
    lag_dates = [d for d in dates if lag[d]]

    # max consecutive lag run
    max_run, cur = 0, 0
    for d in dates:
        cur = cur + 1 if lag[d] else 0
        max_run = max(max_run, cur)
    max_l1 = max(l1s.values()) if l1s else 0.0
    max_l1_date = max(l1s, key=lambda d: l1s[d]) if l1s else None

    # --- Definition (i) STRICT: lag bar on its own flip day ---
    flip_days = {dates[i] for i in range(1, len(dates)) if regime[dates[i]] != regime[dates[i - 1]]}
    strict_flip = [d for d in lag_dates if d in flip_days]
    strict_stable = [d for d in lag_dates if d not in flip_days]

    # --- Definition (ii) SEGMENT: lag bars inside a window that starts at a flip
    # and ends at the first bar where applied == immediate again ---
    seg_flip, seg_stable = [], []
    in_segment = False
    for i, d in enumerate(dates):
        is_flip = d in flip_days
        if is_flip:
            in_segment = True
        if in_segment and lag[d]:
            # re-convergence: applied == immediate on a non-flip bar closes the segment
            if is_flip:
                seg_flip.append(d)
            else:
                seg_flip.append(d)
        if in_segment and not lag[d] and not is_flip:
            in_segment = False
    # stable = lag bars not captured by any segment
    seg_set = set(seg_flip)
    seg_stable = [d for d in lag_dates if d not in seg_set]

    return {
        "n_bars": len(dates),
        "hist": dict(hist),
        "flips": flips,
        "n_lag": len(lag_dates),
        "max_run": max_run,
        "max_l1": max_l1,
        "max_l1_date": max_l1_date,
        "strict_flip": len(strict_flip),
        "strict_stable": len(strict_stable),
        "seg_flip": len(seg_flip),
        "seg_stable": len(seg_stable),
        "lag_dates": lag_dates,
        "flip_days": flip_days,
        "regime": regime,
        "l1s": l1s,
    }


def main():
    for arm, delta in ARMS:
        path = f"/tmp/opencode/diag_{arm}.log"
        order, bars = parse_log(path)
        res = analyze(arm, order, bars, WINDOW_START, WINDOW_END)
        print(f"===== {arm} (max_delta={delta}) =====")
        print(f"  total bars parsed            : {len(order)} (first {order[0]}, last {order[-1]})")
        print(f"  window bars {WINDOW_START}..{WINDOW_END}: {res['n_bars']}")
        print(f"  regime histogram (argmax budgets): {res['hist']}")
        print(f"  regime flips                 : {res['flips']}")
        print(f"  lag bars (applied != immediate): {res['n_lag']}")
        print(f"  max consecutive lag run      : {res['max_run']}")
        print(f"  max L1                       : {res['max_l1']:.6f} @ {res['max_l1_date']}")
        print(f"  DEF(i)   strict flip-day lag : {res['strict_flip']}   stable: {res['strict_stable']}")
        print(f"  DEF(ii)  segment   lag       : {res['seg_flip']}   stable: {res['seg_stable']}")
        # extra: definition (iii) flip-day + next N bars lag window
        for n in (1, 2, 3, 4, 5):
            flip_days = res["flip_days"]
            cnt = 0
            for i, d in enumerate(sorted(res["regime"])):
                pass
            print(f"     (ref) DEF(iii) flip day + {n} following bars -> see below")
        print()

    # --- detailed definition (iii): lag bar is "flip-attributed" if there is a flip
    # at or within `k` bars before it (k = 0..5) ---
    print("### Definition (iii) sensitivity: count lag bars whose most recent flip is within k bars")
    for arm, delta in ARMS:
        path = f"/tmp/opencode/diag_{arm}.log"
        order, bars = parse_log(path)
        res = analyze(arm, order, bars, WINDOW_START, WINDOW_END)
        dates = sorted(res["regime"])
        flip_days = res["flip_days"]
        lag_dates = res["lag_dates"]
        row = [f"{arm:14s} lag={len(lag_dates):3d}"]
        for k in range(0, 7):
            n = 0
            for i, d in enumerate(dates):
                if d not in lag_dates:
                    continue
                j0 = max(0, i - k)
                if any(dates[j] in flip_days for j in range(j0, i + 1)):
                    n += 1
            row.append(f"k={k}:{n:3d}/{len(lag_dates)-n:3d}")
        print("  " + "  ".join(row))

    # --- sanity: print lag bars with regime context for a00 and a02 ---
    print("\n### Lag-bar listing (a00_base_015): date, regime, is_flip, L1, most-recent-flip-distance")
    for arm, _ in ARMS[:1] + ARMS[2:]:
        path = f"/tmp/opencode/diag_{arm}.log"
        order, bars = parse_log(path)
        res = analyze(arm, order, bars, WINDOW_START, WINDOW_END)
        dates = sorted(res["regime"])
        flip_days = res["flip_days"]
        print(f"\n--- {arm} ---")
        idx = {d: i for i, d in enumerate(dates)}
        for d in res["lag_dates"]:
            i = idx[d]
            dist = None
            for j in range(i, -1, -1):
                if dates[j] in flip_days:
                    dist = i - j
                    break
            print(f"  {d} regime={res['regime'][d]:9s} is_flip={d in flip_days} "
                  f"L1={res['l1s'][d]:.6f} last_flip_dist={dist}")


if __name__ == "__main__":
    main()
