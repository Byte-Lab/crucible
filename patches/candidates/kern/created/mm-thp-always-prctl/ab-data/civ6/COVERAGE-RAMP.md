# Civ6 AnonHugePages coverage ramp: stock vs prompt khugepaged (2026-07-17)

Method: single run per arm (mechanism probe, not statistics), Civ6 AI benchmark,
Steam Deck stock kernel, global enabled=always; /proc/<Civ6>/status AnonHugePages
sampled every 15s from game-up. Arms: stock khugepaged (scan_sleep 10000ms /
pages_to_scan 4096) vs prompt (100ms / 16384).

anonhuge_kb by sample (15s cadence; NA = benchmark exited):
  stock: 363M 698M 1143M 1165M 1371M 1214M 1256M 1265M 1272M
  eager: 277M 1154M 1175M 1388M 1224M 1266M 1281M 1266M

Read: both arms plateau ~1.27GB (~ full heap eligible+collapsed), but at the
30s mark the prompt arm is at 1154MB vs stock 698MB (+65%). The fps/p99 win in
the interleaved A/B (fps_avg +4.2%, p99 -6.6%) is therefore time-to-coverage:
the benchmark's early minute runs with substantially more of the heap
hugepage-backed under prompt collapse. Supports the per-mm promptness
mechanism of patch 2 (here approximated globally via sysfs).
