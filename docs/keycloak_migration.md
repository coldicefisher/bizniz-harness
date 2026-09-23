# Queued: FusionAuth → Keycloak, everywhere

**Status: planned, not started.** Decided 2026-09-22. Nothing below has been built.

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

### K1 — Provision Keycloak beside FusionAuth

A `keycloak` template in `provisioner/templates/`, a realm import JSON (the equivalent of
FusionAuth's kickstart), and a `KeycloakOrchestrator` mirroring
`auth_orchestrators/fusionauth_orchestrator.py`. The auth contract the operator renders
becomes Keycloak's: issuer, JWKS, audiences, roles claim — the same fields
`bizniz/discovery` already harvests, which is how the two halves meet.

Both providers work; the architect chooses. Nothing existing breaks.

**Done when** a greenfield project provisions Keycloak, `bizniz smoke` passes against it,
and the generated backend refuses an anonymous request and accepts a minted token.

### K2 — The fastapi skeleton

Ten files: token verification against JWKS rather than FusionAuth's introspection, config
keys, the compose service, the auth tests. This is the contract every other skeleton
depends on, so it goes first and alone.

**Done when** the skeleton's own test suite passes against a Keycloak realm, and
`bizniz hosted` — the gate that mints a real token and probes every route — is green.

### K3 — The saas skeleton

Thirty-five files, and the one most likely to hide assumptions: registration, password
reset, email verification and tenant handling are all provider-shaped. Expect this to be
the milestone that finds the real differences, and budget for it accordingly.

### K4 — Expo, then flip the default

Two files in Expo, then the architect prefers Keycloak for new projects. FusionAuth stays
available behind a flag: muvnit and anything else already generated keeps working, and
nothing is forced to migrate on our schedule.

### K5 — Retire

When no active project provisions FusionAuth, delete the orchestrator, the templates and
the agent. Not before: a dead code path is cheaper than a broken one.

## Decisions to take before K1

- **One realm per application, or one shared realm with a client each?** Conduit uses one
  realm (`conduit`) with several clients, and a hosted application joins it rather than
  standing up its own. A standalone generated application probably wants its own realm in
  its own Keycloak container. Both shapes need to exist; which is the default decides what
  the architect emits.
- **Where the realm definition lives.** FusionAuth's kickstart is a file the provisioner
  renders. A Keycloak realm export is much larger and easily stale; importing a minimal
  realm and configuring the rest through `kcadm` (as `gates/keycloak.py` already does) may
  age better than a checked-in export.
- **What a carve-out does.** A hosted application does not provision an identity provider
  at all — it verifies against the host's. That is already expressed as `press check`'s
  hosted profile; the provisioner needs to know not to emit an auth service for it.

## Why this is queued rather than in flight

knowhow is to be rebuilt as a Conduit carve-out, which needs the hosted path and Keycloak
verification, not a provisioned FusionAuth. That rebuild will exercise exactly the pieces
K1 and K2 cover, so the order matters: do knowhow's carve-out first, learn from it, and
let what it proves shape the templates rather than guessing at them now.
