"""Generated boilerplate for a Python app service when no skeleton applies."""
from __future__ import annotations

from typing import Optional

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


_FRAMEWORK_DEFAULTS = {
    "fastapi": ["fastapi", "uvicorn", "pydantic", "httpx"],
    "flask": ["flask"],
    "django": ["django"],
}


# requirements whose import needs a system package the slim image
# lacks. Checked against the service's declared requirements so only
# services that need a tool pay for it (flirpie lesson: pytesseract
# without the tesseract binary is dead on arrival).
_SYSTEM_DEPS_BY_REQUIREMENT = {
    "pytesseract": ["tesseract-ocr", "tesseract-ocr-eng"],
}


def _generate_dockerfile(
    port: int,
    service_type: str = "backend",
    requirements: Optional[list] = None,
) -> str:
    """Render the Dockerfile.

    Web/API services get ``uvicorn main:app`` so the entry contract
    matches the SKELETON.md hard contract. Workers get a placeholder
    ``sleep infinity`` CMD so the container stays up while the Coder
    fills in the real entrypoint — overriding to e.g.
    ``celery -A main worker --loglevel=info`` once main.py exists.
    Without this fork, every worker boilerplate fails compose-up
    immediately because uvicorn is not in the worker's requirements.
    """
    apt_packages: list = []
    for req in requirements or []:
        req_name = req.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip().lower()
        apt_packages.extend(_SYSTEM_DEPS_BY_REQUIREMENT.get(req_name, []))
    apt_line = ""
    if apt_packages:
        pkgs = " ".join(dict.fromkeys(apt_packages))  # dedupe, keep order
        apt_line = (
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkgs} && rm -rf /var/lib/apt/lists/*\n"
        )
    base = (
        "FROM python:3.12-slim\n"
        + apt_line
        + "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n"
        "ENV PYTHONPATH=/app\n"
    )
    if (service_type or "").lower() == "worker":
        return base + 'CMD ["sleep", "infinity"]\n'
    return (
        base
        + f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]\n'
    )


def _generate_requirements(framework: str, declared: list) -> str:
    packages = list(declared) if declared else []
    base = ["pytest"]
    for pkg in base:
        if pkg not in packages:
            packages.append(pkg)
    for pkg in _FRAMEWORK_DEFAULTS.get(framework, []):
        if pkg not in packages:
            packages.insert(0, pkg)
    return "\n".join(packages) + "\n"


def _generate_skeleton_md(framework: str, port: int) -> str:
    """Minimal SKELETON.md for the no-skeleton fallback so the engineer
    follows the same Dockerfile contract that the Provisioner just
    emitted. Without this, the engineer invents a parallel package
    (e.g. ``pet_groomer/``) that pytest can import but the Dockerfile
    (``CMD ["uvicorn", "main:app", ...]``) can't reach, leaving the
    deployed stack un-runnable.
    """
    if framework == "fastapi":
        entry_hint = (
            "Define your FastAPI app at module path ``main`` with the\n"
            "instance bound to the name ``app`` — i.e. create ``main.py``\n"
            "with ``app = FastAPI()`` plus your routers ``include_router``-ed\n"
            "into it. The Dockerfile runs ``uvicorn main:app``."
        )
    elif framework == "flask":
        entry_hint = (
            "Define your Flask app at module path ``main`` with the\n"
            "instance bound to ``app`` — i.e. ``main.py`` with\n"
            "``app = Flask(__name__)``."
        )
    else:
        entry_hint = (
            "Define an ASGI/WSGI app at module path ``main`` with the\n"
            "instance bound to the name ``app``. The Dockerfile runs\n"
            "``uvicorn main:app``."
        )

    return f"""# Minimal Python App Contract (no skeleton)

This service has no skeleton — you are building it from scratch.
The Provisioner has emitted a minimal ``Dockerfile`` and
``requirements.txt``; everything else is yours.

## Hard contract: the entrypoint

{entry_hint}

The Dockerfile (``infra/development/<service>/Dockerfile``) runs
``uvicorn main:app --host 0.0.0.0 --port {port}``. Your code MUST
expose ``app`` from a top-level ``main.py``. If you put your code
in a parallel package (e.g. ``my_package/``), the deployed
container won't be able to reach it — even if pytest can.

## Where files go

- ``main.py`` — top-level entrypoint, exports ``app``
- ``<package_name>/`` — your domain code, organized however you like
  (models, schemas, routes, services), then imported into ``main.py``
- ``tests/`` — pytest test files; pytest discovers them automatically
- ``requirements.txt`` — append your dependencies (pre-seeded with
  framework basics)

## What you may NOT do

- ❌ Edit the Dockerfile to point at a different module path. ``main:app``
  is the contract; align your code to it, not the other way around.
- ❌ Define ``app`` only inside a package without re-exporting from
  ``main.py``. The Dockerfile imports ``main`` literally; nothing else.

## The contract in one sentence

Top-level ``main.py`` exports ``app``. Everything else is your call.
"""


class PythonAppTemplate(InfraTemplate):
    """Emits a Dockerfile + requirements.txt + SKELETON.md for a
    Python app service when no real skeleton applies. SKELETON.md
    declares the entrypoint contract the Dockerfile expects, so the
    engineer aligns its code to ``main:app`` instead of inventing a
    parallel package the deployed container can't reach.
    """

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        port = ctx.service.port or 8000
        dockerfile = _generate_dockerfile(
            port, ctx.service.service_type, ctx.service.requirements,
        )
        requirements = _generate_requirements(ctx.service.framework, ctx.service.requirements)
        skeleton_md = _generate_skeleton_md(ctx.service.framework, port)

        return TemplateOutput(
            workspace_files={
                "requirements.txt": requirements,
                "SKELETON.md": skeleton_md,
            },
            infra_files={"Dockerfile": dockerfile},
        )
