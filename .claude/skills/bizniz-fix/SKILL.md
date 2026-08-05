---
name: bizniz-fix
description: >
  Converge a generated bizniz project to green gates. Runs the
  deterministic gates (smoke, validate, in-container tests), clusters
  the failures into root causes, dispatches bizniz-coder subagents to
  fix them, and re-gates until green or stalled. Argument: project
  slug or path (e.g. /bizniz-fix recipe_v4_v16).
---

Converge the given project to green. The gates decide, not you: a
gate's exit code is ground truth, and no amount of plausible-looking
code counts as progress until the gate re-runs green.

## Setup

1. Resolve the project (`bizniz projects` if unsure). Note the wall
   clock; you will report total convergence time.
2. `bizniz up <project>`, wait for containers, then confirm liveness
   with `bizniz status <project>` context and a quick health probe.

## Baseline (run all gates before touching anything)

3. `bizniz smoke <project>` — stack-level health/auth/routes.
4. `bizniz validate <project>` — AST symbol/import findings.
5. `bizniz test <project> --service backend` (and `--service frontend`
   with the appropriate command if the project has one) — full failure
   list.

Record the baseline numbers: N smoke failures, N validator findings,
N test failures per service. These are the progress metric.

## Fix loop (max 5 iterations)

6. **Cluster failures by root cause, not by symptom.** Read the
   actual errors (probe container logs for 5xx before concluding
   anything). Many failures usually share one cause — a missing
   fixture, a wrong import, a schema drift. One cluster = one fix
   dispatch.
7. **Dispatch one bizniz-coder subagent per cluster.** Each dispatch
   prompt must include: absolute project root, the service workspace,
   the clustered failing output (verbatim tail), the suspected root
   cause, the exact files in scope, and the gate command that must
   pass. Dispatch clusters touching different files in parallel;
   serialize clusters that overlap.
8. **Restart before re-gating.** After fixes land, restart the
   affected service (`docker compose -f <compose> restart <svc>`) —
   stale uvicorn serving old code has burned whole iterations before.
   If a dependency manifest changed, rebuild instead.
9. **Re-run the failed gates.** Compare against baseline:
   - All green → done.
   - Fewer failures → continue, new iteration from step 6.
   - No progress for 2 consecutive iterations → STOP. Report the
     stall honestly with the remaining failures and your analysis.
     Do not spin.
10. A subagent reporting "no change needed — the code already handles
    this" is a legitimate finding, not a failure: verify by re-running
    the gate, and if the gate still fails, the cluster's root-cause
    analysis was wrong — re-cluster instead of re-dispatching the
    same prompt.

## Wrap-up

11. `bizniz down <project>` unless the user wants it left up.
12. Report: converged or stalled, iterations used, wall time,
    before → after per gate, files touched (from subagent reports),
    and anything learned worth a backlog note. If this was a measured
    run, save the numbers for comparison against the v5 pipeline
    baseline.

## Rules

- Gates are hard. Never mark converged while any gate exits non-zero.
- Never fix code yourself in this skill — dispatch bizniz-coder. Your
  job is orchestration: cluster, dispatch, verify, decide.
- Scope discipline: fixes touch the generated project, never the
  bizniz orchestration repo. If the root cause IS a bizniz/skeleton
  bug, stop and report it as a finding instead of patching around it.
- Container test runs are the truth. Host-side pytest against the
  workspace is not a substitute (imports and env differ).
