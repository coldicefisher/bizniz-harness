# Queued: FusionAuth → Keycloak, everywhere

**Status: K1–K4 done, 2026-09-22/23.** K5 (retirement) is deliberately not done.

The Press runs Keycloak. Conduit authenticates against it, jhup-chat and knowledge-mcp
verify its tokens, and every application the Press hosts will do the same. The harness
provisions FusionAuth, so every application it generates authenticates against a provider
the Press does not run — and the first thing anyone does with a generated app is rip that
out. That is backwards: the harness should emit what the Press actually operates.

## What is actually there

Counted 2026-09-22.

| Where | Files naming FusionAuth | Notes |
|---|---|---|
| `bizniz/` (excluding tests and perf fixtures) | **63** | Concentrated in `provisioner/`, `auth_orchestrators/`, `auth_operator/`, `auth_agent/` |
| `bizniz-skeleton-saas` | 35 | The heaviest: a full product skeleton with auth throughout |
| `bizniz-skeleton-fastapi` | 10 | The backend contract: token verification, config, tests |
| `bizniz-skeleton-expo` | 2 | Mobile login |
| `bizniz-skeleton-react`, `-angular`, `-teams` | 0 | They authenticate through their backend, so they follow it |

The front-end skeletons having no hits is the good news: the change is a backend and
provisioning change, and the UIs inherit it.

**Keycloak is not starting from nothing.** `bizniz/gates/keycloak.py` already mints a real
token from a running Keycloak — it creates a service-account client, adds the audience and
role mappers an app needs, and fetches the token from inside the docker network so the
issuer matches. `bizniz/discovery/` already reads a host's realm, JWKS, accepted audiences
and roles claim. Those solved the three problems that make Keycloak fiddly, and the
migration should reuse them rather than rediscover them:

1. Keycloak stamps `iss` with the hostname it was reached on, so a token fetched through a
   published port is refused by services configured for the network hostname.
2. Client-credentials tokens carry `aud: account`; an audience mapper is required per
   audience the app accepts.
3. Keycloak puts realm roles under `realm_access.roles`, so an app reading a flat `roles`
   claim needs a role mapper.

## The plan

### K1 — Provision Keycloak beside FusionAuth ✅

A `keycloak` template in `provisioner/templates/`, a realm import JSON (the equivalent of
FusionAuth's kickstart), and a `KeycloakOrchestrator` mirroring
`auth_orchestrators/fusionauth_orchestrator.py`. The auth contract the operator renders
becomes Keycloak's: issuer, JWKS, audiences, roles claim — the same fields
`bizniz/discovery` already harvests, which is how the two halves meet.

Both providers work; the architect chooses. Nothing existing breaks.

**Done when** a greenfield project provisions Keycloak, `bizniz smoke` passes against it,
and the generated backend refuses an anonymous request and accepts a minted token.

### K2 — The fastapi skeleton ✅

Ten files: token verification against JWKS rather than FusionAuth's introspection, config
keys, the compose service, the auth tests. This is the contract every other skeleton
depends on, so it goes first and alone.

**Done when** the skeleton's own test suite passes against a Keycloak realm, and
`bizniz hosted` — the gate that mints a real token and probes every route — is green.

### K3 — The saas skeleton ✅

Thirty-five files, and the one most likely to hide assumptions: registration, password
reset, email verification and tenant handling are all provider-shaped. Expect this to be
the milestone that finds the real differences, and budget for it accordingly.

### K4 — Expo, then flip the default ✅

Two files in Expo, then the architect prefers Keycloak for new projects. FusionAuth stays
available behind a flag: muvnit and anything else already generated keeps working, and
nothing is forced to migrate on our schedule.

### K5 — Retire

When no active project provisions FusionAuth, delete the orchestrator, the templates and
the agent. Not before: a dead code path is cheaper than a broken one.

## What was actually built

| Piece | Where |
|---|---|
| Realm, clients, mappers, roles, seed user | `bizniz/provisioner/templates/keycloak.py` |
| Reconcile a running realm against the plan | `bizniz/auth_operator/keycloak_operator.py` |
| The contract generated code reads | `bizniz/auth_operator/keycloak_contract.py` |
| Token verification and identity routes | `bizniz-skeleton-fastapi` |
| Shared auth core, admin client, SPA login | `bizniz-skeleton-saas` |
| Provider named in the mobile client | `bizniz-skeleton-expo` |

The pipeline picks the operator from **what a project actually has** — `.env` carrying
`KEYCLOAK_*` or `FUSIONAUTH_*` — rather than from a flag, so re-running a build on a
project cut before the cutover does not strand it against the wrong provider.

Verified against a real Keycloak 26 rather than in rendering alone: the realm imports, the
seeded admin exchanges its password for a token carrying the right `aud` and a flat
`roles` claim, and the service account reaches the admin API. The skeletons' own suites
pass on python:3.12 — 31 for fastapi, 13 for the saas auth core — and the full harness
suite is 2,657 green.

## Decisions taken

- **A realm per generated application**, in its own Keycloak container, with an API client
  and an SPA client inside it. A carve-out is the other case entirely: it provisions no
  identity service and verifies against its host's realm, which `press check`'s hosted
  profile already describes.
- **A minimal realm import, not an export.** A full export is thousands of lines, goes
  stale the moment anyone opens the console, and buries the handful of objects that
  matter. What is imported is the realm, two clients, their mappers, two roles and a seed
  user; Keycloak defaults the rest.
- **Both issuers, always.** `KEYCLOAK_ISSUERS` carries the in-network and the public URL,
  because the same realm calls itself by whichever hostname reached it.

## What is still queued

**K5 — retirement.** FusionAuth's template, orchestrator, agent and operator are still
present and still registered; nothing new selects them. They come out when no active
project provisions FusionAuth, and not before: a dead code path is cheaper than a broken
one, and muvnit still has a tenant.

The `auth_agent` and `auth_orchestrators` packages remain FusionAuth-shaped. The pipeline
prefers the operator path, which is now Keycloak-aware, and only falls back to the legacy
`AuthAgent` when the factories are not wired — which is tests, and projects mid-migration.

---

*The open questions this plan started with — one realm or a shared one, an export or a
minimal import, what a carve-out provisions — are answered under "Decisions taken" above.
The original sequencing put this work behind knowhow's carve-out rebuild; that order was
reversed on 2026-09-23, so knowhow is now greenfielded on a harness that already emits
what the Press runs.*
