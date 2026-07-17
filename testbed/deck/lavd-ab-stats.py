#!/usr/bin/env python3
"""Compare two lavd-ab result labels with Welch's t-test per metric.

Usage: lavd-ab-stats.py <labelA> <labelB> [--root ~/.crucible/lavd-ab]

Metrics per rep:
  capped_p99_ms / capped_p999_ms  — frame-time tail at 90fps cap (jank; lower better)
  uncapped_fps_avg / uncapped_fps_p1 — throughput + 1% low (higher better)
  schbench_wake_p99_us            — wakeup latency p99 (lower better)
  messaging_s                     — sched messaging total time (lower better)
"""
import csv
import math
import re
import sys
from pathlib import Path


def frame_times_ms(path: Path) -> list[float]:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    # MangoHud: row0 sysinfo hdr, row1 sysinfo, row2 column hdr, rows 3+ frames
    hdr_idx = next(i for i, r in enumerate(rows) if r and r[0] == "fps")
    ft_col = rows[hdr_idx].index("frametime")
    return [float(r[ft_col]) for r in rows[hdr_idx + 1:] if len(r) > ft_col and r[ft_col]]


def pct(sorted_vals: list[float], p: float) -> float:
    # nearest-rank
    if not sorted_vals:
        return float("nan")
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]


def rep_metrics(rep: Path) -> dict[str, float]:
    m: dict[str, float] = {}
    capped = rep / "frames-capped.csv"
    if capped.exists():
        ft = sorted(frame_times_ms(capped))
        m["capped_p99_ms"] = pct(ft, 99)
        m["capped_p999_ms"] = pct(ft, 99.9)
    uncapped = rep / "frames-uncapped.csv"
    if uncapped.exists():
        ft = frame_times_ms(uncapped)
        if ft:
            m["uncapped_fps_avg"] = len(ft) / (sum(ft) / 1000.0)
            worst = sorted(ft, reverse=True)
            n1 = max(1, len(worst) // 100)
            m["uncapped_fps_p1"] = 1000.0 / (sum(worst[:n1]) / n1)
    sch = rep / "schbench.txt"
    if sch.exists():
        # first "* 99.0th" block = wakeup latencies (usec)
        hits = re.findall(r"\*\s*99\.0th:\s+(\d+)", sch.read_text())
        if hits:
            m["schbench_wake_p99_us"] = float(hits[0])
    msg = rep / "messaging.txt"
    if msg.exists():
        hit = re.search(r"Total time:\s*([\d.]+)", msg.read_text())
        if hit:
            m["messaging_s"] = float(hit.group(1))
    return m


def collect(root: Path, label: str) -> dict[str, list[float]]:
    series: dict[str, list[float]] = {}
    reps = sorted((root / label).glob("rep*"))
    if not reps:
        sys.exit(f"no reps under {root / label}")
    for rep in reps:
        for k, v in rep_metrics(rep).items():
            series.setdefault(k, []).append(v)
    return series


def welch(a: list[float], b: list[float]):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    if va == 0 and vb == 0:
        return None
    se2 = va / na + vb / nb
    t = (mb - ma) / math.sqrt(se2)
    df = se2**2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    p = 2 * t_sf(abs(t), df)
    return t, df, p, ma, mb


def t_sf(t: float, df: float) -> float:
    """Survival function of Student's t via numeric integration of the pdf."""
    if t > 60:
        return 0.0
    c = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    n, hi = 4000, t + 60
    h = (hi - t) / n
    s = 0.0
    for i in range(n + 1):
        x = t + i * h
        w = 1 if i in (0, n) else (4 if i % 2 else 2)
        s += w * (1 + x * x / df) ** (-(df + 1) / 2)
    return c * s * h / 3


LOWER_BETTER = {"capped_p99_ms", "capped_p999_ms", "schbench_wake_p99_us", "messaging_s"}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--root")]
    root = Path.home() / ".crucible/lavd-ab"
    for a in sys.argv[1:]:
        if a.startswith("--root="):
            root = Path(a.split("=", 1)[1]).expanduser()
    la, lb = args[0], args[1]
    sa, sb = collect(root, la), collect(root, lb)
    print(f"{'metric':<24} {'A=' + la:>14} {'B=' + lb:>14} {'delta%':>8} {'t':>7} {'p~':>7}  verdict")
    for k in sorted(set(sa) & set(sb)):
        r = welch(sa[k], sb[k])
        if r is None:
            print(f"{k:<24} insufficient/degenerate samples")
            continue
        t, df, p, ma, mb = r
        delta = (mb - ma) / ma * 100 if ma else float("nan")
        better = delta < 0 if k in LOWER_BETTER else delta > 0
        sig = p is not None and p < 0.05
        verdict = ("WIN" if better else "REGRESS") if sig else "neutral"
        pstr = f"{p:.3f}" if p is not None else f"df={df:.1f}"
        print(f"{k:<24} {ma:>14.3f} {mb:>14.3f} {delta:>+7.1f}% {t:>7.2f} {pstr:>7}  {verdict}")
        print(f"{'':<24} A={['%.2f' % x for x in sa[k]]} B={['%.2f' % x for x in sb[k]]}")


if __name__ == "__main__":
    main()
