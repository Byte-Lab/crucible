# Testbeds

Platform-specific setup and benchmarking machinery for the environments
Crucible measures kernels on. Product code does NOT live here - the
orchestrator is in crates/, host agents in agents/, the in-VM guest agent
in guest/. This directory is "everything you need to stand up and drive a
measurement platform".

## virt/ - virtme-ng VM testbed (host machine)

Rootfs builders and host preparation for the QEMU/vng loop the
orchestrator drives:

- setup-rootfs.sh        minimal bookworm rootfs (synthetic stress-ng mode)
- setup-game-rootfs.sh   trixie + Mesa/vkmark/glmark2/MangoHud (game mode)
- setup-steam-rootfs.sh  trixie + Steam client + weston (steam mode)
- setup-host.sh          VFIO GPU passthrough precheck/bind
- lib/rootfs-common.sh   shared mmdebstrap plumbing

See CLAUDE.md "Common commands" for invocation and the many hard-won
constraints (sudo elevation, insecure-apt on Ubuntu hosts, stamp files).

## deck/ - Steam Deck bare-metal testbed

Bootstrap, benchmark, and analysis scripts for the Deck lane
(skills/platform/deck/deck-lane.md has the architecture; the orchestrator side is
crates/crucible-orchestrator/src/deck.rs):

- deck-slot-b.sh             slot-B clone/install/select/mark-good (deployed
                             to /home/deck/ on the Deck; DeckBackend calls it)
- lavd-ab.sh                 interleaved scx_lavd A/B harness
- lavd-ab-stats.py           Welch stats for lavd-ab runs
- cpu-bench-interleaved.sh   schbench interleaved A/B
- civ6-stats.py              Civ6 benchmark CSV stats
- schbench-stats.py          schbench output stats
- bisect-3303.sh             scx#3303 bisect driver
- repro-3119.sh              scx#3119 reproducer
- forkstorm-test.sh          fork-storm protection check
- off8-experiment.sh         chcpu-offline real-topology validation
                             (auto-re-enables CPUs)
- replicate-cachenice.sh     alternating-boot replication harness
- eevdf-grind.sh             EEVDF baseline grind
- ntsync-ab.sh, bench-ntsync.c  ntsync micro-A/B (blocked: SteamOS Proton
                             uses fsync, not ntsync)

Deck operational landmines (LOCALVERSION doubling, module BTF matching,
5GB rootfs, thermal-drift measurement rules) are recorded in
patches/SUMMARY.md and the session memory; read before deploying kernels.
