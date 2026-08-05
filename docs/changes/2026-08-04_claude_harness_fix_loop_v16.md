# Claude-as-harness fix loop: recipe_v4_v16 convergence (2026-08-04)

First live validation of the inverted-harness architecture (step 2b of
the packaging plan): Claude Code drives the convergence loop directly —
`bizniz` CLI gates as ground truth, root-cause clustering in the main
loop, parallel Haiku subagents as fixers, Opus as the escalation tier.
This is the `/bizniz-fix` skill executed on the project the v5
pipeline left broken on 2026-05-20 (killed mid-M1, repair iter 3,
"empty edits" no-op refusals).

## Headline

- Baseline: backend 60 failed + 19 errors / 156 passed; smoke 11/11
  PASS (misleading — see findings); validator 3 findings.
- Result: **234 passed, 1 failed** (stable across 2 consecutive full
  runs; the 1 is FusionAuth tenant default-role config — FA-agent
  owned, not app code), smoke 11/11, in **37 wall-clock minutes**,
  4 fix iterations, 8 fixer dispatches (7 Haiku + 1 Opus) + 1
  review-driven continuation.
- Trajectory: 79 → 22 → 11 → 2 → 1 defects.
- The v5 pipeline's repair loop had stalled on this same project
  (agents returning empty edits, ResolutionChecker disagreeing with
  5 votes). The harness loop converged it in one session.

## What was actually wrong in v16 (real product bugs found)

1. **Double-prefix routes** (~50 failures): me/logout/signup route
   files declared `prefix="/api"` under the `/api/v1` auto-mount →
   `/api/v1/api/*`. Frontend and tests both called `/api/*`. The SPA's
   login/me were 404 in production.
2. **SPA login broken twice more**: frontend posted `{loginId,...}`
   while backend read only `email`; and backend returned no `user`
   object while `setSession(token, user)` requires one.
3. **Missing contract behavior**: `redirect_target`/`next`
   sanitization (open-redirect prevention), `WWW-Authenticate` on
   401s, 400-not-422 on malformed auth input per AUTH_CONTRACT.
4. **Test-side drift**: httpx ≥0.27 `AsyncClient(app=)` removal,
   async-fixture misuse, `User(user_id=)` field drift in conftest,
   `alembic.versions` import, `AsyncMock().json()` returning
   coroutines.

## Bizniz-repo findings (bank these as tickets)

1. **Symbol validator false positives** — ALL 4 validator findings on
   v16 are framework attributes the class index doesn't know:
   `User.__tablename__` (SQLAlchemy), `Model.model_rebuild` /
   `Model.model_validate` (Pydantic classmethods). The attribute
   check needs a framework-methods allowlist (or to inherit
   `BaseModel`/`DeclarativeBase` members).
2. **SmokePhase blind spot** — smoke probes OpenAPI-registered routes,
   so it certified `/api/v1/api/me` green while the SPA's actual
   calls 404'd. Smoke should ALSO probe the paths the frontend
   actually fetches (grep `src/` for fetch/axios paths, or drive the
   real login page), not just the backend's self-description.
3. **Provisioner env bug** — backend container gets
   `FUSIONAUTH_HOST_URL=http://localhost:9026` (host-perspective,
   dead inside the network). Container env should carry
   container-reachable URLs; host-perspective values belong to
   host-side tooling only.
4. **Unit-vs-integration contract conflict left in v16**:
   `SignUpResponse.token` is pinned `None` by unit tests, but the
   SPA's `signup.tsx` does `setSession(response.token, ...)` →
   stores null and the next authenticated call 401s. Product
   decision needed: auto-login on signup (return a real token) or
   SPA redirects to /login. Neither side edited.
5. **FusionAuth tenant default-role config**:
   `test_empty_roles_returns_403` fails because FA assigns the
   default "user" role even when registration sends `roles: []` —
   tenant/registration config, owned by the FusionAuth agent.
6. **Signup race → RESOLVED in-project** (iteration 4): the loser of
   a concurrent duplicate signup hit a FusionAuth-internal 500 that
   the backend mapped to 502. Fixed with a verified-duplicate 409
   (FA `user_exists` probe before claiming duplicate; probe failure
   falls through to an honest, logged 502) + a unique constraint on
   the local mirror's email column. First fix attempt mapped ANY
   FA-500 to 409 — caught in review as error-masking and tightened
   via agent continuation. Race gate green 3×, no regressions.

## What the harness experiment proved

- **Hard gates work as designed.** Exit codes were the only
  definition of progress; two fixers' "done" claims were checked
  against re-runs every iteration. No gate was ever talked past.
- **Root-cause clustering beats per-finding dispatch.** 79
  failures/errors → 4 clusters → 4 parallel fixers; iteration 1
  closed 72% in ~6 min of wall time.
- **The escalation ladder earned its keep.** The Haiku source-rewrite
  introduced 9 unit regressions (interface breaks under mock
  patching, an altitude problem, not a knowledge problem). One Opus
  dispatch reconciled all 9 with minimal edits and found the
  `loginId` break the whole prior chain had missed.
- **Cross-fire is real but manageable.** Iteration 1's URL
  canonicalization overcorrected FusionAuth-host URLs in tests →
  4 new failures; caught by re-gate, fixed in iteration 2 by a
  2-line revert. Dispatch prompts now need an explicit "URLs to
  OTHER services keep THEIR paths" warning (added to the skill's
  lesson list via this doc).
- **Scope discipline needs teeth.** One fixer edited `app/core/*`
  despite an explicit prohibition (diff audited: additive and
  correct, kept). Subagent constraints are advisory; the harness
  must diff-audit after every dispatch — the project-git makes this
  cheap (`git diff` in the generated repo).

## Costs

- Wall: 37 min end-to-end (baseline → final gates).
- Subagent tokens: ~745k across 9 agent runs (4+2+1+1 dispatches +
  1 continuation); main-loop overhead on top. $0 marginal (Max plan).
- v5 comparison: the 2026-05-20 run spent ~2h in review/repair on
  this milestone and did not converge (BROKEN no-op refusals,
  checker/debugger disagreement loop).

## Follow-ups

- Wire `bizniz-coder` agent + `/bizniz-fix` skill natively (they
  load from `.claude/` next session; this run inlined the brief).
- Add the diff-audit step and the other-service-URL warning to the
  skill.
- File the five bizniz findings above as tickets; validator
  false-positives first (it's the cheapest and gates every build).
- Decide signup auto-login vs redirect (product call).
