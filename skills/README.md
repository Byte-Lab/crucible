# Crucible skills

Task-scoped reference material. Load only the subtree that matches what
you are about to do; CLAUDE.md stays the always-loaded operational
baseline and these files deliberately do NOT repeat it.

## Layout

```
skills/
  architecture/       system design (load before architectural changes)
    design.md
  discovery/          finding bottlenecks (start of a cycle)
    profiling.md
  validation/         proving a patch is real (end of a cycle)
    adversarial-review.md
    winner-validation.md
  platform/           where the workload runs
    virt/             virtme-ng VM lane (host 7950X + passthrough GPU)
      gpu-passthrough.md    VFIO/GPU constraints, host setup, game mode
      synthetic-mode.md     stress-ng path + kernel-patch corpus grind
      steam-mode.md         Steam-in-VM launch/presentation/rootfs
      perfetto-loop.md      trace capture stage + known gaps
    deck/             Steam Deck bare-metal lane
      deck-lane.md
```

The virt platform files are the SOURCE OF TRUTH for granular
lane/mode constraints; CLAUDE.md only summarizes and points here.

## When to load what

By cycle stage:

| You are about to... | Load |
|---|---|
| Change orchestrator/agent/protocol architecture | architecture/design.md (original design; CLAUDE.md wins on drifted operational detail) |
| Start a discovery/profiling session (find a bottleneck) | discovery/profiling.md |
| Author a patch | discovery/profiling.md (prototype-first rule) + validation/adversarial-review.md (the loop starts at authoring) |
| Review a patch | validation/adversarial-review.md |
| Validate a measured winner for upstream | validation/winner-validation.md |
| Interpret measurement numbers / set the accept bar | validation/winner-validation.md (calibration section) |

By platform:

| You are running on... | Load |
|---|---|
| The virt VM lane with GPU (mode = "game", VFIO, vkmark/glmark2) | platform/virt/gpu-passthrough.md |
| The virt VM lane synthetic grind (mode = "synthetic", no GPU) | platform/virt/synthetic-mode.md |
| Steam-in-VM mode (mode = "steam") | platform/virt/steam-mode.md (plus gpu-passthrough.md) |
| Anything touching the Perfetto profiling stage | platform/virt/perfetto-loop.md |
| The Steam Deck | platform/deck/deck-lane.md |

Related material that lives elsewhere on purpose:

- patches/candidates/README.md -- the candidate pipeline (states, tiers,
  patchctl). Load when filing or advancing a patch.
- patches/evidence/ -- raw findings, A/B data, and killed-idea records
  (negative results are deliverables). Search here before re-deriving a
  bottleneck.
- CLAUDE.md "Upstream patch corpus" section -- the non-negotiable
  discipline rules in short form; the skills above expand them.
