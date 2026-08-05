---
name: bizniz-review
description: >
  Parallel review unit for a generated bizniz project: dispatches
  bizniz-code-reviewer (source, cold read) and bizniz-quality-engineer
  (tests vs spec, bias-firewalled) per service concurrently, merges
  their findings into one clustered defect report. Read-only — feed
  the report to /bizniz-fix to act on it. Argument: project slug or
  path (e.g. /bizniz-review recipe_v4_v16).
---

Produce one clustered defect report for the project. This skill is
READ-ONLY: no fixes, no edits to the project — the output is the input
to /bizniz-fix.

## Setup

1. Resolve the project (`bizniz projects` if needed). Identify
   services and workspaces from `bizniz status` + the architecture
   (`.bizniz/runs/<latest>/architect.json`).
2. Locate the review context: AUTH_CONTRACT.md, SKELETON.md files,
   enriched-spec / issue artifacts under `.bizniz/`. These are what
   reviewers verify AGAINST.

## Dispatch (all agents in parallel, one message)

3. Per service with source: one **bizniz-code-reviewer** — project
   root, workspace, spec/contract locations, note that
   `bizniz validate` output should be folded in.
4. Per service with tests: one **bizniz-quality-engineer** — project
   root, spec/contract locations, test directories ONLY. Repeat the
   bias firewall in the dispatch: no application source reads.
5. While they run, gather the deterministic layer yourself:
   `bizniz validate <project>` per workspace, and if the stack is up,
   `bizniz smoke <project>`. Do not start fixing anything you see.

## Merge

6. Cluster all findings by ROOT CAUSE across sources — a CR
   "missing 409 handling" and a QE "no test asserts 409" about the
   same endpoint are ONE cluster with two kinds of evidence.
7. Rank: critical (crash/leak/spec violation/auth bypass) first, then
   warnings, then coverage-only gaps. Deduplicate; keep every
   cluster's evidence lines (file:line from CR, missing-scenario
   names from QE, validator/smoke output verbatim).

## Report

8. Final output: per cluster — severity, one-sentence claim, evidence
   list, affected files, suggested fix direction (one line, no code).
   Then: per-service QE approval verdicts, CR approve/block verdict,
   gate statuses. If everything is clean, say so plainly.

## Rules

- Read-only: reviewers never edit; you never edit. If something is
  trivially wrong, it still goes in the report, not into a fix.
- QE dispatches must never name source directories; if a QE report
  shows it read source, mark its verdict tainted and rerun it.
- Deterministic gates outrank LLM opinion: a validator/smoke failure
  is a finding even if both reviewers missed it, and a reviewer claim
  contradicted by a passing gate needs its evidence re-checked.
