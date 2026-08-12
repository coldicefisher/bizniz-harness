# Flirpie — architecture (bizniz-architect output, 2026-08-10)

First app cut through the Claude-native harness. Produced by the
`bizniz-architect` subagent from the problem statement; recorded here
verbatim-in-substance for the Provisioner work and milestone planning.

## Services

| service | type | framework | port | skeleton | notes |
|---|---|---|---|---|---|
| postgres | database | postgres 16 | 5432 | none | app data + FusionAuth schema |
| fusionauth | auth | fusionauth | 9011 | none | identity; backend only validates JWTs |
| redis | cache | redis 7 | 6379 | none | extraction queue ONLY (Streams: `extraction:jobs`, group `extractors`) |
| backend | backend | fastapi | 8000 | fastapi | multipart upload → volume + Document row (pending) → enqueue; list/detail/search; month/trip/state groupings; manual corrections; per-user scoping |
| extraction_worker | worker | redis-streams | — | none | tiered engines, auto_receipt spec strings: T1 heuristics+tesseract → T2 `ollama:<tag>` → T3 heavier local, confidence-routed; writes fields + per-field confidence + provenance |
| mobile | mobile | expo | — | expo | camera/library capture, upload, status polling, detail + corrections, month/trip/state browse; secure-store tokens |

Photo persistence: shared named volume (`flirpie_documents` at
`/data/documents` in backend + worker), paths in Postgres. No
MinIO/S3 in v1; object storage later is a repository-layer swap.
Ollama is an EXTERNAL endpoint (`OLLAMA_BASE_URL`, default
`http://host.docker.internal:11434`) — no ollama compose service in v1.

## Provisioner gaps the architect verified in code (harness tickets)

1. `skeleton "expo"` missing from `bizniz/architect/skeletons.py` —
   needs SkeletonInfo (service_type mobile, no container port) + the
   `bizniz-skeleton-expo` repo (exists as of 2026-08-10).
2. `service_type "mobile"` in neither `_INFRASTRUCTURE_TYPES` nor
   `_APP_TYPES` (provisioner.py:56-57): must materialize a workspace
   but emit NO compose service/Dockerfile, and be skipped by
   SmokePhase's backend/frontend probes + WebUITester (mobile smoke
   is the Maestro gate instead).
3. Generic PythonAppTemplate Dockerfile must apt-install
   `tesseract-ocr` (+ eng traineddata) for the worker, or tier 1 is
   dead on arrival.
4. No way to express a shared named volume between two services in
   the Architect model — backend + worker need `flirpie_documents`.
   Provisioner-level feature.
5. (Optional, deferred) `ollama` template if a containerized Ollama
   is ever wanted; v1 uses the host daemon.

## Milestones (architect's slicing — pending operator approval)

- **M1 — Capture and keep.** Auth in-app; camera/library capture;
  upload; photo persisted + Document row (status=pending); My
  Documents list w/ thumbnails; strict per-user isolation. Services:
  postgres, fusionauth, backend, mobile. (No AI yet — already beats
  the shoebox.)
- **M2 — It identifies itself.** redis + worker, tier 1 only:
  doc-type classification + vendor/date/amount/state extraction,
  per-field confidence; mobile shows status, detail screen w/ manual
  correction.
- **M3 — It gets good.** Tier 2 (Ollama VLM) + tier 3 (heavier
  local), confidence routing, provenance per run, re-extract action,
  odometer/BOL/invoice numbers.
- **M4 — Find anything, survive IFTA.** Month/trip/state browse
  (Trip entity + assignment), search/filter, per-state per-quarter
  fuel totals.

## Open product decisions (Jamey)

1. **"Trip" definition**: user-created entity with date-range
   auto-assign (assumed) vs inferred from odometer/date clustering
   (materially different feature).
2. **Tier 3**: heavier local model (assumed) vs human-in-the-loop
   review queue (adds a mobile surface).
3. **"opentrain"** reading: open/locally-trainable models via
   Ollama/HF fine-tuned on the auto_receipt corpus (assumed) vs a
   specific external product.
4. IFTA scope in v1 = views + totals only (no form generation/export).

## Assumed out of v1

Offline capture/sync queue, push notifications (polling instead),
fleet/multi-driver accounts, image editing beyond picker crop,
websockets.
