"""``bizniz`` — command-line surface over the deterministic pipeline tooling.

This is the harness-integration entry point: every subcommand wraps an
existing deterministic phase (no LLM calls) so that an outer agent —
Claude Code, a skill, a script — can drive builds, gates, and probes
without the ``PYTHONPATH=. .venv/bin/python`` incantations.

Subcommands
-----------
projects            list generated projects under the projects root
status <project>    latest run's phase/milestone progress
up / down <project> compose the generated stack up or down
smoke <project>     run the deterministic SmokePhase gate (exit 1 on fail)
test <project>      run tests inside a running service container
validate <path>     AST symbol/import validation over a workspace
perf ...            delegate to bizniz.perf_log CLI
mcp                 launch the Bizniz MCP server (stdio)

``<project>`` accepts either a slug (resolved under
``$BIZNIZ_PROJECTS_ROOT``, default ``~/bizniz_projects``) or a path.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


DEFAULT_PROJECTS_ROOT = Path.home() / "bizniz_projects"
COMPOSE_REL = Path("infra") / "development" / "docker-compose.yml"
#: Legacy default above; use discover_compose() for anything that
#: may run against an adopted workspace.


# ── project / state resolution ──────────────────────────────────────────


def discover_compose(project_root: Path, service_names=None) -> Path:
    """The project's compose file, whatever its infra directory is called.

    ``infra/development/`` is the generated default, but a stack adopted
    into an existing workspace uses that workspace's convention instead
    (this repo's console lives in ``infra/management/`` so it does not
    collide with the pipeline's own ``infra/dev/``).

    Guessing "development" and falling back silently is worse than it
    looks: the smoke gate then cannot ask compose for the real host port
    bindings, defaults to the CONTAINER ports, and probes
    ``localhost:8000`` — which on a machine running several bizniz
    projects lands on a DIFFERENT project's API and reports its routes as
    passes.
    """
    preferred = project_root / "infra" / "development" / "docker-compose.yml"
    if preferred.exists():
        return preferred
    infra = project_root / "infra"
    if not infra.is_dir():
        return preferred
    found = sorted(infra.glob("*/docker-compose.yml"))
    if not found:
        return preferred
    if len(found) == 1:
        return found[0]

    # More than one infra directory -- an adopted workspace has its own
    # stack beside the host's. Pick by CONTENT, not by name: the compose
    # that declares this project's services. Sorting alphabetically would
    # pick the host's `infra/dev/` over the console's `infra/management/`
    # and gate the wrong stack.
    wanted = {str(n) for n in (service_names or [])}
    if wanted:
        best, best_score = None, 0
        for candidate in found:
            try:
                import yaml
                declared = set(
                    (yaml.safe_load(candidate.read_text()) or {}).get("services") or {}
                )
            except Exception:
                continue
            score = len(declared & wanted)
            if score > best_score:
                best, best_score = candidate, score
        if best is not None:
            return best
    return found[0]


def projects_root() -> Path:
    return Path(os.environ.get("BIZNIZ_PROJECTS_ROOT", str(DEFAULT_PROJECTS_ROOT)))


def find_project(arg: str) -> Optional[Path]:
    """Resolve a slug or path to a project root, or None if not found."""
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    candidate = projects_root() / arg
    if candidate.is_dir():
        return candidate.resolve()
    return None


def resolve_project(arg: str) -> Path:
    """Like :func:`find_project` but exits with an error message."""
    found = find_project(arg)
    if found is None:
        raise SystemExit(
            f"error: project '{arg}' not found (not a directory, and "
            f"not a slug under {projects_root()})"
        )
    return found


def compose_path(project_root: Path) -> Path:
    """The project's compose file.

    Goes through `discover_compose` so `up`, `down` and `test` work against
    a stack adopted into an existing workspace, whose infra directory is
    named for that workspace's convention rather than "development".

    Service names come from the persisted architecture when one is
    available: a repo can hold more than one compose file (this project's
    console lives beside the pipeline's own stack), and picking the wrong
    one means `bizniz down` tears down the host's containers.
    """
    names = None
    arch = _architecture_from_project_db(project_root)
    if arch is not None:
        names = [svc.name for svc in arch.services]
    path = discover_compose(project_root, service_names=names)
    if not path.exists():
        raise SystemExit(
            f"error: no compose file at {path} "
            f"(looked under {project_root / 'infra'})")
    return path


def latest_run_dir(project_root: Path) -> Optional[Path]:
    from bizniz.driver.runs_paths import resolve_runs_root

    runs_root = resolve_runs_root(project_root)
    if not runs_root.is_dir():
        return None
    runs = sorted(d for d in runs_root.iterdir() if d.is_dir())
    return runs[-1] if runs else None


def _architecture_from_project_db(project_root: Path):
    """Latest architecture snapshot from the project DB, or None.

    Read-only and deliberately forgiving: this is a fallback, so any
    failure to open or parse the snapshot means "no snapshot" rather than
    an error that masks the real one.
    """
    from bizniz.architect.types import SystemArchitecture

    if not (project_root / ".bizniz" / "project.db").exists():
        return None
    try:
        from bizniz.project.project import Project

        snap = Project(project_root, project_root.name).db.get_latest_architecture()
        if not snap:
            return None
        # sqlite3.Row from the project DB; the payload lives in
        # `snapshot_json`. Mapping-style access rather than indexing so
        # this survives a column being added before it.
        if hasattr(snap, "keys"):
            snap = snap["snapshot_json"]
        data = snap if isinstance(snap, dict) else json.loads(snap)
        for key in ("architecture", "payload"):
            if "services" not in data and key in data:
                data = data[key]
                if isinstance(data, str):
                    data = json.loads(data)
        return SystemArchitecture.model_validate(data)
    except Exception:
        return None


def load_architecture(project_root: Path):
    """Load the persisted SystemArchitecture from the latest run."""
    from bizniz.architect.types import SystemArchitecture

    run_dir = latest_run_dir(project_root)
    if run_dir is None:
        # No driver run directory. That does NOT mean the project is
        # unprovisioned: the Provisioner records an architecture snapshot in
        # the project DB on every run, and a project provisioned through the
        # Provisioner API directly (or adopted into an existing workspace)
        # has the snapshot without ever having had a driver run.
        #
        # Falling back here is what lets the gates work against such a
        # project. Without it, `smoke` reports "has the pipeline provisioned
        # this project?" for a project that is fully provisioned and running.
        arch = _architecture_from_project_db(project_root)
        if arch is not None:
            return arch
        raise SystemExit(
            f"error: no runs recorded under {project_root} and no "
            f"architecture snapshot in its project DB — has the pipeline "
            f"provisioned this project?"
        )
    arch_path = run_dir / "architect.json"
    if not arch_path.exists():
        raise SystemExit(f"error: {arch_path} not found")
    data = json.loads(arch_path.read_text())
    # Artifacts may be stored bare or wrapped in a payload envelope.
    if "services" not in data and "payload" in data:
        data = data["payload"]
    return SystemArchitecture.model_validate(data)


# ── subcommands ─────────────────────────────────────────────────────────


def cmd_projects(args: argparse.Namespace) -> int:
    root = projects_root()
    if not root.is_dir():
        print(f"(no projects root at {root})")
        return 0
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        run_dir = latest_run_dir(proj)
        marker = f"last run {run_dir.name}" if run_dir else "no runs"
        print(f"{proj.name:32s} {marker}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    run_dir = latest_run_dir(project)
    if run_dir is None:
        print(f"{project.name}: no runs recorded")
        return 1
    print(f"project:  {project}")
    print(f"run:      {run_dir.name}")
    status_path = run_dir / "run_status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text())
        phases = status.get("top_completed") or []
        print(f"top phases: {', '.join(map(str, phases)) or '(none)'}")
    for m_dir in sorted(run_dir.glob("m*")):
        if not m_dir.is_dir():
            continue
        m_status_path = m_dir / "status.json"
        if not m_status_path.exists():
            continue
        m_status = json.loads(m_status_path.read_text())
        completed = m_status.get("completed") or []
        current = m_status.get("current")
        done = " DONE" if "done" in [str(c).lower() for c in completed] else ""
        tail = f", current={current}" if (current and not done) else ""
        print(f"{m_dir.name}: {len(completed)} completed"
              f" (last={completed[-1] if completed else 'none'}){tail}{done}")
    return 0


def _compose(project_root: Path, *compose_args: str) -> int:
    cmd = ["docker", "compose", "-f", str(compose_path(project_root)), *compose_args]
    return subprocess.call(cmd)


def cmd_up(args: argparse.Namespace) -> int:
    return _compose(resolve_project(args.project), "up", "-d")


def cmd_down(args: argparse.Namespace) -> int:
    return _compose(resolve_project(args.project), "down")


def cmd_smoke(args: argparse.Namespace) -> int:
    from bizniz.driver.smoke_phase import SmokePhase
    from bizniz.planner.types import Milestone

    project = resolve_project(args.project)
    if getattr(args, "service", None):
        # Per-service smoke: device-type services gate via Maestro on
        # an emulator, not curl against compose.
        from bizniz import mobile_gates

        workspace = project / args.service
        if mobile_gates.is_expo_workspace(workspace):
            return mobile_gates.run_smoke(
                workspace,
                avd=getattr(args, "avd", None),
                skip_build=getattr(args, "skip_build", False),
                log=lambda m: print(m, file=sys.stderr),
            )
        raise SystemExit(
            f"error: --service smoke only supports mobile workspaces; "
            f"'{args.service}' is not an Expo workspace"
        )
    architecture = load_architecture(project)
    auth_contract = None
    contract_path = project / "AUTH_CONTRACT.md"
    if contract_path.exists():
        auth_contract = contract_path.read_text()

    milestone = Milestone(name="cli-smoke", problem_slice="CLI-invoked smoke gate")
    phase = SmokePhase(
        timeout_s=args.timeout,
        on_status=lambda msg: print(msg, file=sys.stderr),
    )
    result = phase.run(milestone, architecture, project, auth_contract=auth_contract)

    for check in result.checks:
        mark = "PASS" if check.passed else "FAIL"
        code = f" [{check.status_code}]" if check.status_code is not None else ""
        detail = f" — {check.detail}" if (check.detail and not check.passed) else ""
        print(f"{mark} {check.category:10s} {check.target}{code}{detail}")
    print(
        f"smoke: {'PASSED' if result.passed else 'FAILED'} "
        f"({len(result.checks)} checks, "
        f"{len(result.failed_checks)} failed, {result.duration_s:.1f}s)"
    )
    for failure in result.critical_failures:
        print(f"critical: {failure}")
    return 0 if result.passed else 1


def cmd_test(args: argparse.Namespace) -> int:
    from bizniz import mobile_gates

    project = resolve_project(args.project)
    workspace = project / args.service
    if mobile_gates.is_expo_workspace(workspace):
        # Mobile workspaces aren't containerized — jest runs host-side.
        return mobile_gates.run_tests(
            workspace, args.cmd or None,
            log=lambda m: print(m, file=sys.stderr),
        )
    test_cmd: List[str] = args.cmd or ["python", "-m", "pytest", "-q"]
    return _compose(project, "exec", "-T", args.service, *test_cmd)


def cmd_validate(args: argparse.Namespace) -> int:
    from bizniz import mobile_gates
    from bizniz.coder.symbol_validator import validate_files

    root = Path(args.path).expanduser()
    if not root.is_dir():
        root = resolve_project(args.path)
    if mobile_gates.is_expo_workspace(root):
        # Expo workspaces validate via tsc + lint, not the Python AST
        # walker.
        return mobile_gates.run_validate(
            root, log=lambda m: print(m, file=sys.stderr),
        )
    # A project root isn't itself a Python workspace — default to backend/.
    if not any(root.glob("*.py")) and (root / "backend").is_dir():
        root = root / "backend"
    skip_dirs = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    files = [
        f for f in root.rglob("*.py")
        if not (skip_dirs & set(f.relative_to(root).parts[:-1]))
    ]
    if not files:
        print(f"no .py files under {root}")
        return 1
    report = validate_files(files, root)
    print(report.render())
    print(
        f"validate: {'PASSED' if report.passed else 'FAILED'} "
        f"({report.file_count} files, {report.resolved_count} resolved, "
        f"{len(report.unresolved)} unresolved imports, "
        f"{len(report.unresolved_attributes)} unresolved attributes, "
        f"{len(report.syntax_errors)} syntax errors)"
    )
    return 0 if report.passed else 1


def cmd_perf(args: argparse.Namespace) -> int:
    from bizniz.perf_log.cli import main as perf_main

    return perf_main(args.perf_args)


def cmd_mcp(args: argparse.Namespace) -> int:
    from bizniz.mcp_server.server import main as mcp_main

    return mcp_main()


# ── parser ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bizniz",
        description="Deterministic tooling for the Bizniz pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("projects", help="list generated projects")
    p.set_defaults(fn=cmd_projects)

    p = sub.add_parser("status", help="latest run's phase progress")
    p.add_argument("project", help="project slug or path")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("up", help="docker compose up -d the generated stack")
    p.add_argument("project")
    p.set_defaults(fn=cmd_up)

    p = sub.add_parser("down", help="docker compose down the generated stack")
    p.add_argument("project")
    p.set_defaults(fn=cmd_down)

    p = sub.add_parser("smoke", help="run the deterministic smoke gate")
    p.add_argument("project")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="per-probe timeout seconds (default 5)")
    p.add_argument("--service", default=None,
                   help="mobile workspace name: gate via release build "
                        "+ adb install + maestro instead of stack curl")
    p.add_argument("--avd", default=None,
                   help="emulator AVD name (default $BIZNIZ_AVD or 'bizniz')")
    p.add_argument("--skip-build", action="store_true",
                   help="reuse the existing release APK")
    p.set_defaults(fn=cmd_smoke)

    p = sub.add_parser("test", help="run tests inside a running service container")
    p.add_argument("project")
    p.add_argument("--service", default="backend",
                   help="compose service name (default: backend)")
    p.add_argument("cmd", nargs="*",
                   help="test command (default: python -m pytest -q)")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("validate", help="AST symbol/import validation")
    p.add_argument("path", help="workspace dir, project slug, or project path")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("perf", help="perf-log analysis (delegates to bizniz.perf_log)")
    p.add_argument("perf_args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_perf)

    p = sub.add_parser("mcp", help="launch the Bizniz MCP server (stdio)")
    p.set_defaults(fn=cmd_mcp)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
