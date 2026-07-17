# Adversarial review procedure

Every patch -- autonomous-loop output or hand-written -- goes through an
author-vs-reviewer loop before it is applied/measured, and again before
it is called submission-ready. LLM patches look plausible by
construction; a fresh skeptical reading is what catches wrong-mechanism
patches, API misuse, and comment/code drift before an expensive measure
cycle (or an LKML embarrassment) is spent on them.

## The loop

1. Author produces patch + commit message + claimed evidence.
2. A SEPARATE reviewer (fresh context, no author state) critiques it.
   Reviewer verdicts: APPROVE / REVISE (with required changes) / SCRAP.
3. REVISE goes back to the author with the critiques; iterate.
4. Terminal states only:
   - a FRESH reviewer returns APPROVE with no required changes, or
   - author and reviewer agree the patch should be scrapped.
   Round-cap without approval = scrap (conservative default).

In the orchestrator this is the PatchReviewer agent between
GenerateOptimization and ApplyChanges (bounded rounds, default 3;
scrapped cycles continue unpatched -- free A/A calibration data). For
corpus patches the same loop runs with subagents until a fresh reviewer
approves; record the round-by-round trajectory in the patch's
EVIDENCE.md and SCORECARD.md "Review trajectory" section.

## Reviewer obligations (what makes it adversarial)

- Independently RECOMPUTE every claimed number from the raw logs; do
  not trust the author's summary statistics.
- Verify cited commits, mechanisms, and APIs against actual source
  (does the function exist in the target tree? does the comment the
  patch "fixes" say what the author claims?).
- Attack the measurement: interleaving, n, CV, kernel-identity
  verification per boot, thermal state, baseline staleness.
- Attack the mechanism: is there a cheaper explanation? does the
  claimed cost have a prototype probe proving it exists
  (see discovery/profiling.md, prototype-first)?
- Attack the general patch: does it integrate correctly with the surrounding
  code it's mutating? Does it introduce any new bugs, race conditions, etc?
  Does it copy paste when it should have added the proper abstraction?
- Attack the commit format: Do the title and commit summary sound like they
  were written by a human? Are any comments that the patch added overly
  verbose, or out of place compared to the rest of the file?
- Default to REVISE/SCRAP when uncertain. An approval must be earned.

## Related bars

- Statistical bar and A/A calibration for accepting a win:
  winner-validation.md in this directory.
- Negative outcomes are recorded, not discarded:
  patches/negative-results/ with a full EVIDENCE.md.
