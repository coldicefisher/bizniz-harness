"""Seed muvnit's run state with the decided plan + architecture.

The architecture came from the Claude-native bizniz-architect (see
docs/architecture/muvnit_architecture.md) rather than the pipeline's
own architect call. Marking PLAN + ARCHITECT complete via RunState
lets ``v2_build --phase provision`` (and everything after) consume
these artifacts through the normal resume path — same machinery, no
special cases.

Usage: PYTHONPATH=. .venv/bin/python examples/seed_muvnit_run.py
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.state import RunState, TopPhase
from bizniz.planner.types import Milestone, ProjectPlan

PROBLEM = (
    "Muvnit (muvnit.com) is a trucking paperwork application. Truckers "
    "photograph receipts, bills of lading, invoices, weigh tickets and "
    "tolls; AI identifies the document type, extracts key fields, and "
    "catalogues everything. Browse by month, by trip (a trip is a "
    "specific load: origin first pickup to destination last drop off), "
    "and by state (IFTA). Extraction accuracy via convergence: two "
    "independent model extractions must agree per field; disagreements "
    "escalate. Engines: tesseract/heuristics, local Ollama VLMs, and "
    "OpenRouter models — cost per image stays near zero."
)

PLAN = ProjectPlan(
    project_slug="muvnit",
    problem_statement=PROBLEM,
    description="Trucking paperwork capture, AI cataloguing, IFTA-ready organization.",
    milestones=[
        Milestone(
            sequence_index=0,
            name="Capture and keep",
            problem_slice=(
                "Truckers sign up / log in from the mobile app, photograph "
                "paperwork with the camera or pick from the photo library, "
                "and upload it. Originals persist server-side with a "
                "Document record (status=pending, no extraction yet). A "
                "'My Documents' list shows thumbnails newest-first. Every "
                "query is scoped to the authenticated user."
            ),
            use_cases=[
                "Sign up and log in from the app",
                "Photograph a receipt and upload it",
                "Pick an existing photo and upload it",
                "See my uploaded documents, newest first",
                "Never see another user's documents",
            ],
            success_criteria=[
                "Upload from emulator persists file + Document row",
                "List returns only the authenticated user's documents",
                "App cold-start restores the session",
                "All gates green: backend pytest, mobile tsc/jest/lint, "
                "stack smoke, mobile maestro smoke",
            ],
        ),
        Milestone(
            sequence_index=1,
            name="It identifies itself",
            problem_slice=(
                "Uploaded documents are classified (fuel receipt, meal "
                "receipt, BOL, invoice, weigh ticket, toll) and key fields "
                "extracted (vendor, date, amount, state) by the tier-1 "
                "engine (deterministic heuristics + tesseract) via the "
                "extraction worker consuming a Redis Streams queue. "
                "Mobile shows extraction status and a document detail "
                "screen with manual field correction."
            ),
            use_cases=[
                "Uploaded document gets a type and extracted fields",
                "See extraction status on the list",
                "Correct a wrong field on the detail screen",
            ],
            success_criteria=[
                "Worker consumes queue and writes fields + confidence",
                "Corrections persist and win over extracted values",
            ],
        ),
        Milestone(
            sequence_index=2,
            name="It gets good",
            problem_slice=(
                "Extraction accuracy reaches trust level via convergence: "
                "two independent engines (ollama:<tag> local VLM and an "
                "openrouter:<model>) extract independently; fields where "
                "both agree are confirmed; disagreements go to a "
                "tiebreaker engine or are flagged for manual correction. "
                "Adds odometer and BOL/invoice-number extraction, engine "
                "provenance per run, and a re-extract action."
            ),
            use_cases=[
                "Low-confidence fields are flagged, not silently wrong",
                "Re-extract a document after a bad scan",
            ],
            success_criteria=[
                "Field confirmed only when two engines agree",
                "Provenance recorded per extraction run",
            ],
        ),
        Milestone(
            sequence_index=3,
            name="Find anything, survive IFTA",
            problem_slice=(
                "Browse by month, by trip, and by state. A Trip models a "
                "specific load (origin first pickup to destination last "
                "drop off); documents are assigned to trips. Search and "
                "filter by vendor, type, date range, amount. Per-state "
                "per-quarter fuel totals (gallons, dollars) for IFTA. "
                "Organization only — no form generation in v1."
            ),
            use_cases=[
                "Create a trip (load) and assign documents",
                "Browse documents by month / trip / state",
                "See per-state fuel totals for a quarter",
            ],
            success_criteria=[
                "State view totals match the underlying documents",
                "Trip assignment survives edits",
            ],
            refactor_after=True,
        ),
    ],
)

_shared = ["documents"]
ARCHITECTURE = SystemArchitecture(
    project_name="Muvnit",
    project_slug="muvnit",
    description=(
        "Trucking paperwork capture and cataloguing: Expo mobile client, "
        "FastAPI backend, FusionAuth identity, Redis Streams extraction "
        "queue, tiered document-AI worker (tesseract → ollama → "
        "openrouter with convergence), Postgres + shared documents volume."
    ),
    services=[
        ServiceDefinition(
            name="postgres", service_type="database", framework="postgres",
            language="sql", description="App data + FusionAuth schema.",
            workspace_name="postgres", port=5432, skeleton="none",
        ),
        ServiceDefinition(
            name="fusionauth", service_type="auth", framework="fusionauth",
            language="none", description="Identity provider; backend validates its RS256 JWTs.",
            workspace_name="fusionauth", port=9011,
            depends_on=["postgres"], skeleton="none",
        ),
        ServiceDefinition(
            name="redis", service_type="cache", framework="redis",
            language="none",
            description="Extraction queue broker (Redis Streams: extraction:jobs).",
            workspace_name="redis", port=6379, skeleton="none",
        ),
        ServiceDefinition(
            name="backend", service_type="backend", framework="fastapi",
            language="python",
            description=(
                "API for the mobile client: multipart photo upload to the "
                "shared documents volume, Document rows, extraction "
                "enqueue, list/detail/search, month/trip/state groupings, "
                "manual corrections. Auth-scoped per user."
            ),
            workspace_name="backend", port=8000,
            depends_on=["postgres", "fusionauth", "redis"],
            requirements=["python-multipart", "redis", "pillow"],
            skeleton="fastapi", shared_volumes=_shared,
        ),
        ServiceDefinition(
            name="extraction_worker", service_type="worker",
            framework="redis-streams", language="python",
            description=(
                "Tiered document-AI worker consuming extraction:jobs. "
                "Engine specs per auto_receipt: tesseract | ollama:<tag> "
                "| openrouter:<model> | hf:<repo>. Convergence gate: two "
                "independent extractions must agree per field."
            ),
            workspace_name="extraction_worker", port=None,
            depends_on=["redis", "postgres"],
            requirements=[
                "redis", "pytesseract", "pillow", "httpx", "sqlalchemy",
                "psycopg[binary]", "pydantic", "pydantic-settings",
                "python-dateutil", "pytest", "pytest-asyncio",
            ],
            skeleton="none", shared_volumes=_shared,
        ),
        ServiceDefinition(
            name="mobile", service_type="mobile", framework="expo",
            language="typescript",
            description=(
                "Expo mobile app: camera/library capture, upload with "
                "progress, status polling, detail + corrections, "
                "month/trip/state browsing. Secure-store JWT session."
            ),
            workspace_name="mobile", port=None,
            depends_on=["backend"],
            requirements=[], skeleton="expo",
        ),
    ],
)


def main() -> int:
    projects_root = Path(
        os.environ.get("BIZNIZ_PROJECTS_ROOT", str(Path.home() / "bizniz_projects"))
    )
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = projects_root / "muvnit" / ".bizniz" / "runs" / job_id
    run_root.mkdir(parents=True, exist_ok=False)

    state = RunState(run_root)
    state.mark_top_phase(TopPhase.PLAN, PLAN)
    state.mark_top_phase(TopPhase.ARCHITECT, ARCHITECTURE)

    print(f"seeded {run_root}")
    print("next: PYTHONPATH=. .venv/bin/python examples/v2_build.py "
          "--project muvnit --phase provision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
