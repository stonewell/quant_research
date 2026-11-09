import ast, re, sys
from collections import OrderedDict

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/diag_a00_base_015.log"
pre, post = OrderedDict(), OrderedDict()
pat = re.compile(r"bar=(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (pre-renorm|delta-clamped)=(\{.*\})\s*$")
unparsed = 0
for line in open(path, encoding="utf-8", errors="replace"):
    m = pat.search(line)
    if not m:
        if "pre-renorm" in line or "delta-clamped" in line:
            unparsed += 1
        continue
    bar = m.group(1)
    d = ast.literal_eval(m.group(4))
    (pre if m.group(3) == "pre-renorm" else post)[bar] = d

keys = sorted(set(pre) | set(post))
print("parsed pre:", len(pre), "parsed post:", len(post), "unparsed candidate lines:", unparsed)
print("bar range pre:", min(pre), "->", max(pre))
print("bar range post:", min(post), "->", max(post))

allkeys = sorted(set(pre) & set(post))
print("common bars:", len(allkeys))

def l1(a, b):
    ks = set(a) | set(b)
    return sum(abs(float(a.get(k, 0.0)) - float(b.get(k, 0.0))) for k in ks)

rows = []
for bar in allkeys:
    l1v = l1(pre[bar], post[bar])
    rows.append((bar, l1v))

# full range (includes warmup)
n_diff_full = sum(1 for _, v in rows if v > 1e-12)
max_full = max(rows, key=lambda r: r[1])
print("\n[FULL RANGE incl warmup] bars=%d  applied!=immediate=%d  maxL1=%.6f @ %s" % (len(rows), n_diff_full, max_full[1], max_full[0]))

# backtest window 2022-01-04 .. 2026-10-22
START, END = "2022-01-04", "2026-10-22"
win = [r for r in rows if START <= r[0] <= END]
n_diff_win = sum(1 for _, v in win if v > 1e-12)
max_win = max(win, key=lambda r: r[1])
print("[BACKTEST WINDOW %s..%s] bars=%d  applied!=immediate=%d  maxL1=%.6f @ %s"
      % (START, END, len(win), n_diff_win, max_win[1], max_win[0]))
print("top5 L1 in window:", [(b, round(v, 6)) for b, v in sorted(win, key=lambda r: -r[1])[:5]])
print("first/last diff bar in window:", (win and [b for b, v in win if v > 1e-12][:1]), ([b for b, v in win if v > 1e-12][-1:]))
