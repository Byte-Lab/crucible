# Upstream patch candidates

Every patch that is a possible candidate for upstreaming lives here, one
directory per patch, containing ALL files relevant to it (diff, commit
message, EVIDENCE.md, reproducers, raw benchmark logs, rejected iteration
diffs) plus a SCORECARD.md.

## Layout

```
candidates/
  patchctl               <- inspection/management tool (see below)
  kern/                  <- Linux kernel patches (LKML)
  scx/                   <- sched_ext scheduler patches (sched-ext/scx GitHub)
    created/             <- written, reviewed, not yet sent
    sent/                <- posted upstream, awaiting outcome
    merged/              <- accepted upstream
    rejected/            <- rejected upstream or by the user
      <slug>/            <- one directory per patch
        SCORECARD.md     <- metadata + rating + trajectory (parsed by patchctl)
        *.diff *.commitmsg *EVIDENCE.md ...
```

Move patches between states with `./patchctl move <slug> <state>` - it
relocates the directory and updates the scorecard's state field.

## Scorecard ratings

- TIER_1: very likely to be accepted upstream (evidence complete,
  reviews at APPROVE steady state, only mechanical prep left)
- TIER_2: possible but needs work (listed in the scorecard's
  "Prep before sending" / "Why TIER_x" sections)
- TIER_3: unlikely as-is (blocked on data, hardware, or a bar we
  cannot currently meet; upgrade path documented)

## patchctl quick reference

```
./patchctl                     # table of all patches, kern/scx split
./patchctl -t 1                # TIER_1 only (also: -t 2, -t TIER_3)
./patchctl -c scx              # one class only
./patchctl -s sent             # filter by state
./patchctl summary             # counts by class/tier/state
./patchctl path fd-array       # print matching patch dir path(s)
./patchctl evidence -t 1       # dump EVIDENCE.md of matching patches
./patchctl scorecard mmap      # dump SCORECARD.md
./patchctl show <slug>         # scorecard + full file listing (one patch)
./patchctl move <slug> <state> # advance a patch through the pipeline
```

Filters compose and also apply to path/evidence/scorecard.

## Trajectories

Each scorecard has a "Review trajectory" section: the investigation
chain (how the bottleneck was found), the adversarial review rounds
with what each round caught, and pointers to the EVIDENCE.md that
records the round-by-round summaries plus raw measurement logs.
Caveat: the verbatim conversational transcripts of the review agents
were session-ephemeral and are not archived; the EVIDENCE files and
scorecards are the durable record. Perfetto traces that pointed at a
specific patch are co-located in that patch's directory (e.g.
amd-pstate-epp-boost/traces/baseline-gfx.pftrace is the dynamic_epp
root-cause trace); traces from investigations that produced no patch
stay in ../evidence/traces/. Traces are deliberately UNTRACKED
(.gitignore: *.pftrace) - they exist only on the machine that captured
them; everything derived from them (findings docs, numbers) is
committed.

## What is NOT here

- ../negative-results/ - patches killed by measurement (mmap_lock
  offset-56 restore lives inside mm-mmap-lock-comment-fix/investigation/
  since it is that patch's data foundation; rq-shared-tags, drm-sched
  WQ_HIGHPRI, sis-util-idle-floor are standalone kills)
- ../evidence/ - shared investigation corpus for findings that did not
  map to a single patch (no-patch traces, memcached-3way scheduler
  comparison, general findings docs)
- ../SUMMARY.md - narrative index of wins and kills
