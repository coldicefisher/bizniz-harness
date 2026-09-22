"""Check that a hosted app can still be carved off.

An app hosted inside another system is supposed to depend on it through public
interfaces only — the network, the identity provider, the proxy. The coupling that
ends a carve-off does not announce itself: one import of the host's shared library,
one Dockerfile built FROM its base image, one host component that starts calling the
app directly, and the app is now part of the host whatever the README says.

The rules, all driven by the discovery profile rather than hard-coded names:

  1. the app must not import the host's own packages;
  2. the app must not reference the host's source or build internals;
  3. the app's frontend must not import from outside the app;
  4. the host must not reference the app — the dependency points one way. Host-side
     wiring (compose include, proxy snippet, CI) is where naming the app is expected,
     so those paths are exempt.

A violation is reported with file and line. Nothing is inferred: if the profile does
not say what the host's packages are, rule 1 reports that it could not run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from bizniz.discovery.types import HostProfile

SKIP_DIRS = {"node_modules", ".angular", "dist", "build", "__pycache__", ".pytest_cache",
             ".venv", "venv", "coverage", ".git", ".mypy_cache"}
TEXT_SUFFIXES = {".py", ".ts", ".js", ".json", ".yml", ".yaml", ".sh", ".conf", ".ini",
                 ".toml", ".html", ".scss", ".css", ".txt", ".cfg", ".hcl", ""}
TS_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]""")
#: Documentation describes the boundary, so it necessarily names both sides.
DOC_SUFFIXES = {".md", ".rst"}


@dataclass
class Violation:
    rule: str
    location: str
    detail: str

    def line(self) -> str:
        return f"{self.location}: {self.detail}  [{self.rule}]"


@dataclass
class BoundaryResult:
    app: str
    violations: list[Violation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    checked_files: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations


def _files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts):
            yield path


def _readable(path: Path) -> Optional[str]:
    if path.suffix not in TEXT_SUFFIXES and not path.name.endswith("Dockerfile"):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def check(repo: Path, app_name: str, profile: HostProfile,
          self_describing: Optional[set[str]] = None) -> BoundaryResult:
    repo = Path(repo)
    app_dir = repo / app_name
    if not app_dir.is_dir():
        raise FileNotFoundError(f"no app directory at {app_dir}")

    result = BoundaryResult(app=app_name)
    exempt = set(self_describing or set())

    packages = profile.boundary.packages.value or []
    internals = profile.boundary.build_internals.value or []
    code_roots = profile.boundary.code_roots.value or []
    integration = profile.boundary.integration_paths.value or []

    if not packages:
        result.skipped.append(
            "rule 1 (host imports): the profile lists no host packages — "
            "re-run `bizniz discover` against the host")
    if not code_roots:
        result.skipped.append(
            "rule 4 (dependency direction): the profile lists no host code roots")

    py_import = (re.compile(r"^\s*(?:from|import)\s+(" + "|".join(re.escape(p) for p in packages)
                            + r")(?:\.|\s|$)", re.M) if packages else None)
    internal_ref = (re.compile("|".join(re.escape(i) for i in internals))
                    if internals else None)

    # The app's own frontend directory, so rule 3 knows what "outside" means.
    web_dirs = [d for d in (app_dir / "web", app_dir / "frontend", app_dir / "ui") if d.is_dir()]

    # ── rules 1-3: the app must not reach into the host ──
    for path in _files(app_dir):
        rel = path.relative_to(repo).as_posix()
        if path.suffix in DOC_SUFFIXES or rel in exempt:
            continue
        text = _readable(path)
        if text is None:
            continue
        result.checked_files += 1

        if py_import and path.suffix == ".py":
            for m in py_import.finditer(text):
                result.violations.append(Violation(
                    "host-import", f"{rel}:{_line_of(text, m.start())}",
                    f"imports the host package '{m.group(1)}' — the app would not build "
                    "once carved off"))

        if internal_ref:
            for m in internal_ref.finditer(text):
                result.violations.append(Violation(
                    "host-internals", f"{rel}:{_line_of(text, m.start())}",
                    f"references host build internals '{m.group(0)}'"))

        for web in web_dirs:
            if path.suffix in {".ts", ".js"} and web in path.parents:
                for m in TS_IMPORT.finditer(text):
                    spec = m.group(1)
                    if not spec.startswith("."):
                        continue
                    target = (path.parent / spec).resolve()
                    if web.resolve() != target and web.resolve() not in target.parents:
                        result.violations.append(Violation(
                            "outside-app", f"{rel}:{_line_of(text, m.start())}",
                            f"imports '{spec}', which resolves outside {web.name}/"))

    # ── rule 4: the host must not depend on the app ──
    app_names = {app_name, app_name.replace("_", "-"), app_name.replace("-", "_")}
    for code_root in code_roots:
        base = repo / code_root
        if not base.is_dir():
            continue
        for path in _files(base):
            rel = path.relative_to(repo).as_posix()
            if any(rel.startswith(p) for p in integration) or path.suffix in DOC_SUFFIXES:
                continue
            if path.suffix not in {".py", ".ts", ".js", ".json", ".html"}:
                continue
            text = _readable(path)
            if text is None:
                continue
            for name in app_names:
                index = text.find(name)
                if index >= 0:
                    result.violations.append(Violation(
                        "wrong-direction", f"{rel}:{_line_of(text, index)}",
                        f"host code names '{name}' — the dependency must point into the "
                        "host, never out of it"))
                    break
    return result
