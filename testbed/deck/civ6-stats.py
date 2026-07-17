#!/usr/bin/env python3
"""Compare Civ6 first-party benchmark CSVs (one frame-time-ms per line) across arms.

Usage: civ6-stats.py <dir> <armA> <armB> [...]   # dir contains <arm>/rep*.csv
Prints per-arm rep metrics and Welch t-test of each arm vs armA.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from importlib import util as _u
_spec = _u.spec_from_file_location("s", Path(__file__).parent / "lavd-ab-stats.py")
_s = _u.module_from_spec(_spec)
_spec.loader.exec_module(_s)
welch, pct = _s.welch, _s.pct


def rep_metrics(csv: Path) -> dict[str, float]:
    ft = sorted(float(x) for x in csv.read_text().split() if x.strip())
    if not ft:
        return {}
    n1 = max(1, len(ft) // 100)
    worst = ft[-n1:]
    return {
        "fps_avg": len(ft) / (sum(ft) / 1000.0),
        "fps_p1_low": 1000.0 / (sum(worst) / n1),
        "frametime_p99_ms": pct(ft, 99),
        "frametime_p999_ms": pct(ft, 99.9),
    }


def main():
    root = Path(sys.argv[1])
    arms = sys.argv[2:]
    series: dict[str, dict[str, list[float]]] = {}
    for arm in arms:
        series[arm] = {}
        for rep in sorted((root / arm).glob("rep*.csv")):
            for k, v in rep_metrics(rep).items():
                series[arm].setdefault(k, []).append(v)
    base = arms[0]
    lower_better = {"frametime_p99_ms", "frametime_p999_ms"}
    for arm in arms[1:]:
        print(f"== {base} vs {arm}")
        for k in sorted(series[base]):
            a, b = series[base].get(k, []), series[arm].get(k, [])
            r = welch(a, b)
            if r is None:
                print(f"  {k:<20} insufficient data A={a} B={b}")
                continue
            t, df, p, ma, mb = r
            d = (mb - ma) / ma * 100
            better = d < 0 if k in lower_better else d > 0
            sig = p is not None and p < 0.05
            v = ("WIN" if better else "REGRESS") if sig else "neutral"
            print(f"  {k:<20} {ma:>9.2f} -> {mb:>9.2f} {d:>+6.1f}%  t={t:>6.2f} p={p:.3f}  {v}")
            print(f"    A={[f'{x:.2f}' for x in a]} B={[f'{x:.2f}' for x in b]}")


if __name__ == "__main__":
    main()
