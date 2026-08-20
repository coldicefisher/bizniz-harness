"""Deterministic docker-compose.yml builder.

Replaces the AI-generated compose YAML with a structured assembly from
the SystemArchitecture + per-service template outputs. Idempotent and
testable.
"""
from __future__ import annotations

from typing import Dict, List

import copy

import yaml

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.templates.base import TemplateOutput


_APP_SERVICE_TYPES = {"backend", "frontend", "worker"}


def build_compose(
    architecture: SystemArchitecture,
    template_outputs: Dict[str, TemplateOutput],
    project_slug: str,
    infra_dirname: str = "development",
) -> str:
    """Build the docker-compose.yml content for the project.

    Parameters
    ----------
    architecture:
        The plan from Architect — services + ports + dependencies.
    template_outputs:
        ``{service_name: TemplateOutput}`` for any service that had a
        template render (infrastructure templates plus app-template
        outputs for skeleton-less app services).
    project_slug:
        Project slug used for image tags ``<slug>-<service>:dev``.

    Returns
    -------
    YAML string ready to write to ``infra/development/docker-compose.yml``.
    """
    services_block: Dict[str, dict] = {}
    volumes: List[str] = []
    networks: List[str] = []
    # Networks that already exist on the host and must be referenced with
    # ``external: true`` rather than created. Populated by adopted
    # services -- see ServiceDefinition.external.
    external_networks: List[str] = []

    for service in architecture.services:
        # An adopted service is already running outside this project.
        # Emit NO service block for it: writing one would stand up a
        # duplicate alongside the real thing, which for a database means a
        # second empty volume the generated app talks to instead.
        if getattr(service, "external", False):
            net = getattr(service, "external_network", None)
            if net and net not in external_networks:
                external_networks.append(net)
            continue

        out = template_outputs.get(service.name)

        if out and out.compose_service is not None:
            # Template provided a complete service definition.
            #
            # DEEP-COPIED, not referenced. The post-processing below edits
            # these blocks (adopted networks, stripped depends_on, declared
            # environment), and editing the TemplateOutput in place would
            # leak one project's configuration into the next call that
            # reused the same rendered template. Caught by a test where two
            # builds shared a template dict and the second saw the first's
            # credentials.
            services_block[service.name] = copy.deepcopy(out.compose_service)
            for v in out.compose_volumes:
                if v not in volumes:
                    volumes.append(v)
            for n in out.compose_networks:
                if n not in networks:
                    networks.append(n)
            continue

        # No template entry — must be an app service (skeleton-seeded or
        # generated). Build a standard app-service compose entry.
        if service.service_type in _APP_SERVICE_TYPES:
            entry = _build_app_service_entry(
                service, project_slug, architecture, infra_dirname,
            )
            services_block[service.name] = entry
            if "app-network" not in networks:
                networks.append("app-network")
            for v in service.shared_volumes:
                if v not in volumes:
                    volumes.append(v)

    # ── Reconcile every emitted block against the adopted services ──────
    # Two things break otherwise, and both stop the stack coming up:
    #
    #  1. ``depends_on: <adopted>`` names a service that is NOT in this
    #     compose file, and compose refuses to start on an undefined
    #     dependency. Readiness of an adopted service is the host stack's
    #     business, not this one's.
    #  2. A TEMPLATED service (fusionauth, nginx) that talks to an adopted
    #     database never joins its network, so the hostname does not
    #     resolve. Only app services got that treatment above; templates
    #     render their own block and bypass it.
    _adopted = {s.name: s for s in architecture.services
                if getattr(s, "external", False)}
    if _adopted:
        _by_name = {s.name: s for s in architecture.services}
        for svc_name, block in services_block.items():
            spec = _by_name.get(svc_name)
            deps = spec.depends_on if spec else []
            joined = [_adopted[d].external_network for d in deps
                      if d in _adopted and _adopted[d].external_network]
            if joined:
                nets = list(block.get("networks") or [])
                for n in joined:
                    if n not in nets:
                        nets.append(n)
                block["networks"] = nets
            # Strip adopted names from depends_on, in both the mapping form
            # ({svc: {condition: ...}}) and the list form.
            dep_block = block.get("depends_on")
            if isinstance(dep_block, dict):
                kept = {k: v for k, v in dep_block.items() if k not in _adopted}
                if kept:
                    block["depends_on"] = kept
                else:
                    block.pop("depends_on", None)
            elif isinstance(dep_block, list):
                kept_l = [d for d in dep_block if d not in _adopted]
                if kept_l:
                    block["depends_on"] = kept_l
                else:
                    block.pop("depends_on", None)

    # ── Declared environment, merged over EVERY emitted block ───────────
    # Templated services (fusionauth, postgres, redis) render their own
    # compose block and so never passed through the app-service path. That
    # made their environment unreachable from the architecture, which is
    # how a generated FusionAuth ends up holding the database superuser's
    # password: the template names ${POSTGRES_USER} for both its root and
    # its application credentials, and nothing could say otherwise.
    #
    # A declared value of ``None`` REMOVES the key. That is the only way to
    # express "this service should not receive root credentials at all",
    # which is a stronger and more honest isolation than handing it a
    # different password.
    # Declared command overrides the image CMD.
    for _svc in architecture.services:
        cmd = getattr(_svc, "command", None)
        blk = services_block.get(_svc.name)
        if cmd and blk is not None:
            blk["command"] = cmd

    for _svc in architecture.services:
        declared = getattr(_svc, "environment", None) or {}
        block = services_block.get(_svc.name)
        if not declared or block is None:
            continue
        env = dict(block.get("environment") or {})
        for _k, _v in declared.items():
            if _v is None:
                env.pop(_k, None)
            else:
                env[_k] = _v
        if env:
            block["environment"] = env
        else:
            block.pop("environment", None)

    # Top-level structure. ``name`` pins the compose project name to the
    # slug. Without this, compose derives the project name from the parent
    # directory ("development"), and EVERY bizniz project would collide on
    # that name — `docker compose up` for project A would replace project
    # B's containers, and `compose down` would tear down the wrong stack.
    compose: Dict[str, object] = {
        "name": project_slug,
        "services": services_block,
    }
    if volumes:
        compose["volumes"] = {v: None for v in volumes}
    if networks or external_networks:
        net_block: Dict[str, object] = {n: None for n in networks}
        # ``external: true`` tells compose the network is pre-existing and
        # must not be created or torn down with this stack. Tearing down a
        # network the host stack owns would take its containers offline.
        for n in external_networks:
            net_block[n] = {"external": True}
        compose["networks"] = net_block

    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)


def _networks_for(
    service: ServiceDefinition, architecture: SystemArchitecture,
) -> List[str]:
    """Networks an app service joins.

    Always its own ``app-network``, plus the external network of every
    adopted service it depends on. Without the second part a generated
    backend cannot resolve the host stack's database by name, which is
    the whole point of adopting it.
    """
    nets = ["app-network"]
    by_name = {s.name: s for s in architecture.services}
    for dep in service.depends_on:
        target = by_name.get(dep)
        if target is None or not getattr(target, "external", False):
            continue
        net = getattr(target, "external_network", None)
        if net and net not in nets:
            nets.append(net)
    return nets


def _build_app_service_entry(
    service: ServiceDefinition,
    project_slug: str,
    architecture: SystemArchitecture,
    infra_dirname: str = "development",
) -> dict:
    """Compose entry for an application service (backend/frontend/worker).

    The image is tagged ``<slug>-<svc>:dev`` so compose reuses the
    Provisioner-built image when available; ``build:`` is included so
    ``docker compose build`` and CI rebuilds still work.

    The ``dockerfile`` field is relative to the *build context*, not the
    compose file, so it's one ``..`` fewer than the volume / context paths.

    For Node-based services we add an anonymous volume on
    ``/app/node_modules`` so the workspace bind-mount doesn't mask the
    npm-installed dependencies inside the image. Python's pip installs to
    system site-packages outside ``/app``, so it needs no equivalent.
    """
    ws = service.workspace_name
    volumes = [f"../../{ws}:/app"]
    if service.language in ("typescript", "javascript"):
        volumes.append("/app/node_modules")
    # Mount the shared ``core/`` library (Refactorer's output) into
    # every app service. Per-language to keep Python and TS isolated
    # — mounting the wrong-language tree just adds clutter to PATH.
    # The mount path matches the import convention documented in
    # ``core/README.md``: ``python_core.*`` and ``ts_core/*``.
    lang_lower = (service.language or "").lower()
    if lang_lower == "python":
        volumes.append("../../core/python:/python_core")
    elif lang_lower in ("typescript", "javascript"):
        volumes.append("../../core/typescript:/ts_core")
    # Mount the project-root ``docs/`` directory into every app
    # service at ``/app/docs`` (read-only). HumanDocsGenerator writes
    # markdown here after each milestone; the fastapi skeleton's
    # ``/api/v1/docs/*`` routes serve it via DocsLoader. React/Angular
    # skeletons' viewer routes consume those routes — no direct
    # filesystem read from the frontend. Read-only because docs are
    # content, not application state.
    volumes.append("../../docs:/app/docs:ro")
    # Shared named volumes (cross-service file exchange, e.g. an
    # upload-accepting backend + a worker reading the same files).
    # Mount path convention: /data/<volume-name>.
    for shared in service.shared_volumes:
        volumes.append(f"{shared}:/data/{shared}")
    # Declared host mounts, verbatim. Appended last so they cannot be
    # shadowed by a convention mount above them.
    for extra in getattr(service, "mounts", []) or []:
        if extra not in volumes:
            volumes.append(extra)

    entry: dict = {
        "image": f"{project_slug}-{service.name}:dev",
        "build": {
            "context": f"../../{ws}",
            "dockerfile": f"../infra/{infra_dirname}/{ws}/Dockerfile",
        },
        "env_file": ".env",
        "volumes": volumes,
        "networks": _networks_for(service, architecture),
    }

    # Per-language environment so the shared ``core/`` mount resolves
    # at import time without skeleton-level Dockerfile changes.
    if lang_lower == "python":
        # PYTHONPATH is prepended so ``python_core`` import resolves
        # before any same-named app-local package. The trailing /app
        # keeps the app's own imports working.
        entry["environment"] = {
            "PYTHONPATH": "/python_core:/app",
        }
    elif lang_lower in ("typescript", "javascript"):
        # Node resolves modules from NODE_PATH if the import isn't a
        # relative path. Mounting at /ts_core + setting NODE_PATH lets
        # ``import { TimeInstant } from "ts_core/data_types/time_instant"``
        # work without a separate tsconfig path alias.
        entry["environment"] = {
            "NODE_PATH": "/ts_core",
        }

    if service.port:
        # ``service.port`` is the CONTAINER port (the port the service
        # listens on inside its container). ``service.host_port`` is the
        # host-side mapping, set by the provisioner when collision-detect
        # forces a remap; None means "same as container port."
        from bizniz.architect.types import host_port_for
        container_port = _container_port_for(service)
        host_port = host_port_for(service) or container_port
        bind = getattr(service, "bind_host", None)
        entry["ports"] = [f"{bind}:{host_port}:{container_port}" if bind
                          else f"{host_port}:{container_port}"]

    # Resolve dependencies that exist in the architecture
    valid_deps = {s.name for s in architecture.services if s.name != service.name}
    deps = [d for d in service.depends_on if d in valid_deps]
    if deps:
        # If any dep is a database, use service_healthy condition where
        # postgres exposes a healthcheck.
        depends_block: dict = {}
        for d in deps:
            dep_svc = next((s for s in architecture.services if s.name == d), None)
            if dep_svc and dep_svc.service_type in {"database", "cache"}:
                depends_block[d] = {"condition": "service_healthy"}
            else:
                depends_block[d] = {"condition": "service_started"}
        entry["depends_on"] = depends_block

    return entry


def _container_port_for(service: ServiceDefinition) -> int:
    """Best guess for the in-container port a service exposes.

    Resolution order:
      1. Skeleton-declared ``container_port`` (most authoritative — the
         skeleton author knows which port their dev server binds).
      2. Framework default (covers generated boilerplate without a
         skeleton).
      3. Fallback to the host port, then 8000.
    """
    if service.skeleton and service.skeleton != "none":
        from bizniz.architect.skeletons import get_skeleton
        info = get_skeleton(service.skeleton)
        if info is not None and info.container_port is not None:
            return info.container_port
    framework_ports = {
        "fastapi": 8000,
        "flask": 5000,
        "django": 8000,
        "react": 5173,
        "angular": 4200,
        "vue": 5173,
    }
    p = framework_ports.get(service.framework)
    if p:
        return p
    return service.port or 8000
