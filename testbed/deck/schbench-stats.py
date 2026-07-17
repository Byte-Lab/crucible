#!/usr/bin/env python3
"""Pool schbench outputs across interleaved rounds and Welch-compare arms.

Parses `*sleep 20` schbench text: 'Wakeup Latencies' + 'Request Latencies'
percentile blocks. Extracts wakeup p50/p99/p99.9 and request p50/p99 (usec).

Usage: schbench-stats.py <dir> <armAprefix> <armBprefix> <rounds>
  e.g. schbench-stats.py ~/.crucible/cpu-bench/sisfloor stock test 4
"""
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/void/upstream/crucible/deck")
import importlib.util as u
_s = u.spec_from_file_location("cs", "/home/void/upstream/crucible/deck/civ6-stats.py")
_m = u.module_from_spec(_s); _s.loader.exec_module(_m)
welch = _m.welch


def parse(txt: str) -> dict[str, float]:
    m = {}
    # sections: "Wakeup Latencies percentiles (usec)" then lines "  50.0th: N"
    for sect, tag in [("Wakeup Latencies", "wake"), ("Request Latencies", "req"),
                      ("RPS", "rps")]:
        pass
    # generic: capture each named block's 50/99/99.9
    blocks = re.split(r"(\w[\w ]+Latencies) percentiles", txt)
    for i in range(1, len(blocks), 2):
        name = blocks[i].strip().split()[0].lower()  # Wakeup / Request
        body = blocks[i + 1]
        for pctl, key in [("50.0000th", "p50"), ("90.0000th", "p90"),
                          ("99.0000th", "p99"), ("99.9000th", "p999"),
                          ("50.0th", "p50"), ("99.0th", "p99"), ("99.9th", "p999")]:
            mm = re.search(rf"{re.escape(pctl)}:\s*(\d+)", body)
            if mm:
                m[f"{name}_{key}_us"] = float(mm.group(1))
    mm = re.search(r"average rps:\s*([\d.]+)", txt)
    if mm:
        m["rps"] = float(mm.group(1))
    return m


def main():
    root = Path(sys.argv[1])
    pa, pb, rounds = sys.argv[2], sys.argv[3], int(sys.argv[4])

    def pool(prefix):
        s = {}
        for r in range(1, rounds + 1):
            for f in sorted(root.glob(f"{prefix}-r{r}-r*.txt")):
                for k, v in parse(f.read_text()).items():
                    s.setdefault(k, []).append(v)
        return s

    sa, sb = pool(pa), pool(pb)
    lower_better = lambda k: k != "rps"
    na = len(next(iter(sa.values()), []))
    nb = len(next(iter(sb.values()), []))
    print(f"{pa} n={na}  vs  {pb} n={nb}")
    for k in sorted(set(sa) & set(sb)):
        r = welch(sa[k], sb[k])
        if not r:
            print(f"  {k:<16} insufficient"); continue
        t, df, p, ma, mb = r
        d = (mb - ma) / ma * 100 if ma else float("nan")
        better = (d < 0) if lower_better(k) else (d > 0)
        sig = p is not None and p < 0.05
        print(f"  {k:<16} {ma:>9.1f} -> {mb:>9.1f} {d:>+6.1f}%  p={p:.3f}  "
              f"{'WIN' if better and sig else 'REGRESS' if sig else 'ns'}")


if __name__ == "__main__":
    main()
