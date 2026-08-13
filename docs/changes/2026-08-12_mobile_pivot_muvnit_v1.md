# Mobile pivot → Muvnit v1 (2026-08-10 → 2026-08-13)

The mission (Jamey, 2026-08-10): cut a native App Store app with the
Claude-native harness, refining the harness as we go. Three days
later: **Muvnit v1 complete** — four milestones, all gates green,
zero test carve-outs, proven on Jamey's real trucking paperwork.

## The product

**Muvnit** (muvnit.com; slug renamed from dev codename flirpie —
that domain is reserved for a future game). Trucking paperwork:
photograph receipts/BOLs/invoices/weigh tickets; AI classifies and
extracts (vendor, date, amount, state, gallons, odometer,
BOL/invoice numbers); browse by month/trip/state; per-state
quarterly IFTA fuel totals. A **trip is a load** (origin first
pickup → destination last drop-off). Accuracy model: **convergence**
— a field is confirmed (0.95) only when two independent models
agree; disagreements ship as disputed (0.3) with candidate values
surfaced in the mobile review UI, never silently wrong.

Stack: Expo mobile (skeleton `expo`) + FastAPI + FusionAuth +
Redis-Streams extraction worker (engine specs `tesseract` /
`ollama:<tag>` / `openrouter:<model>`; default pair
qwen3-vl:8b-instruct + glm-ocr per the auto_receipt bench, which
measured that pair at 74% agreement / 91% right-when-agreeing).
Photos on a shared `documents` volume. Cost per image ≈ 0 (local).

## Milestones (all closed, evidence per commit)

- **M1 capture/keep** — auth, camera upload, per-user isolation;
  Maestro E2E green.
- **M2 tier-1 extraction** (`184f878`) — tesseract+heuristics
  adapted from the bench; 76/76 real docs extracted; corrections +
  re-extract recovery exercised live.
- **M3 convergence** (`d656b1d`) — measured vs tier-1 on the real
  corpus: date/amount coverage 92% (was 70%/67%); field trust 42%
  converged / 32% disputed / 26% tier-1-fallback.
- **M4 organization/IFTA** (`25704e2`) — trips CRUD + assignment,
  effective-date filters + summaries via one shared date helper,
  gallons field, 4-tab mobile. Live IFTA 2021-Q1: ID 3 receipts
  $1,269.06; TN 19.91 gal. Final battery: backend 107 / worker 81 /
  mobile 54 tests, smoke 15/15, E2E [Passed].

## Harness earnings (fixed at source, with tests)

Provisioner: `expo` skeleton registry, `service_type: mobile`
(workspace, no container), `shared_volumes`, requirement-conditional
sysdeps (pytesseract→tesseract-ocr), per-project app-identity
substitutions. CLI: web/mobile gate routing; mobile smoke = release
build → explicit `adb -s` install → Maestro, emulator by AVD name.
Validator: framework-attr false positives, distribution-name aliases.
Contract-test template: container-correct FA URL resolution.
Skeleton: auth-gated layout (never-restructure), login screen,
`api.upload`, network-security config plugin, Hermes runtime + honest
API typing lessons.

## Verification lessons (the writeup-worthy ones)

1. **Trust-but-verify caught real misses eight times** — agents
   claiming done with skipped live checks, a maestro "pass" that was
   a pipe swallowing a flow syntax error, a "Hermes gap" that was
   actually string-typed API amounts hitting `.toFixed` (typed
   `number`, mocked as numbers — only the release E2E could see it),
   a cargo-cult fix hiding a cents-corruption bug, silently dropped
   tests, a silently weakened flow.
2. **Live corpus tests as acceptance gates** work: uploading real
   paperwork through the actual API exposed the stranded-PEL worker
   bug, the issuer mismatch, and the IFTA effective-date drift that
   unit suites all missed.
3. **Same-agent continuations beat fresh dispatches** for fix
   iterations — context is capital.
4. Full narrative of the run-up (CLI + skills + v16 validation):
   `2026-08-04_claude_harness_fix_loop_v16.md`.

## Open items

OpenRouter key → convergence pair swap + re-measure (target: cut the
32% dispute rate); store runway (Apple dev account, EAS, Moovit
trademark check, muvnit.com registration); ollama systemd drop-in
(retires the socat `ollama-bridge`); bench truth-scoring on the
trucking corpus (`~/flirpie/Trucking_png`, 366 PNGs); archive
bizniz-2/flirpie/bizniz-harness repos.
