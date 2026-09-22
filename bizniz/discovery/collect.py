"""Harvest the host's conventions from its files.

Everything here is `asserted`: it is what the repository says. :mod:`verify` is
what turns the checkable parts into `verified`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional

import yaml

from bizniz.discovery.types import (
    Build, Frontend, HostProfile, HostedApp, Identity, Proxy, Stack, absent, asserted,
)

COMPOSE_CANDIDATES = [
    ("dev", ["infra/dev/docker-compose.yml", "infra/development/docker-compose.yml",
             "docker-compose.dev.yml", "docker-compose.yml"]),
    ("prod", ["infra/prod/docker-compose.yml", "infra/production/docker-compose.yml",
              "docker-compose.prod.yml"]),
]

# `location /chat/ {` and `location = /chat {`
LOCATION_RE = re.compile(r"^\s*location\s+(?:=\s+)?(/[^\s{]*)", re.M)
BAKE_TARGET_RE = re.compile(r'^target\s+"([^"]+)"', re.M)
CSS_VAR_RE = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.M | re.I)


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _first_existing(root: Path, candidates: Iterable[str]) -> Optional[Path]:
    for rel in candidates:
        path = root / rel
        if path.is_file():
            return path
    return None


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


def collect_stack(root: Path) -> Stack:
    stack = Stack()
    files: dict[str, Path] = {}
    for kind, candidates in COMPOSE_CANDIDATES:
        found = _first_existing(root, candidates)
        if found:
            files[kind] = found
            claim = asserted(_rel(root, found), _rel(root, found))
            setattr(stack, f"compose_{kind}", claim)
        else:
            setattr(stack, f"compose_{kind}",
                    absent(f"looked for {', '.join(candidates)}"))

    dev = files.get("dev")
    if not dev:
        stack.services = absent("no dev compose file found")
        return stack

    doc = _load_yaml(dev)
    stack.services = asserted(sorted((doc.get("services") or {}).keys()), _rel(root, dev))

    # Hosted apps arrive through compose `include:` — the seam a carved-off app uses.
    includes = doc.get("include") or []
    paths = [i.get("path") if isinstance(i, dict) else i for i in includes]
    stack.includes = asserted([p for p in paths if p], _rel(root, dev),
                              note="hosted apps are attached here")

    # The shared external network every hosted app joins.
    networks = doc.get("networks") or {}
    external = [n.get("name", key) for key, n in networks.items()
                if isinstance(n, dict) and (n.get("external") or n.get("name"))]
    stack.network = (asserted(external[0], _rel(root, dev)) if external
                     else absent("no named network in the dev compose"))
    return stack


def _proxy_service(doc: dict) -> Optional[str]:
    for name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image", ""))
        if "nginx" in image or "proxy" in name:
            return name
    return None


def collect_proxy(root: Path, stack: Stack) -> Proxy:
    proxy = Proxy()
    dev_rel = stack.compose_dev.value
    if not dev_rel:
        proxy.service = absent("no dev compose file found")
        return proxy

    dev = root / dev_rel
    doc = _load_yaml(dev)
    name = _proxy_service(doc)
    if not name:
        proxy.service = absent("no nginx-like service in the dev compose")
        return proxy
    proxy.service = asserted(name, dev_rel)

    svc = (doc.get("services") or {}).get(name) or {}
    ports = [str(p) for p in (svc.get("ports") or [])]
    host_port = None
    for spec in ports:
        parts = spec.split(":")
        if len(parts) >= 2 and parts[0].isdigit():
            host_port = parts[0]
            break
    proxy.dev_base_url = (asserted(f"http://localhost:{host_port}", dev_rel)
                          if host_port else absent("proxy publishes no host port"))

    # A hosted app drops a routing snippet into a mounted directory; find the mount.
    mounts = [str(v) for v in (svc.get("volumes") or [])]
    hosted = [m for m in mounts if "hosted" in m or "locations" in m]
    if hosted:
        target = hosted[0].split(":")[1] if ":" in hosted[0] else hosted[0]
        proxy.locations_dir = asserted(target, dev_rel)
        proxy.mount_convention = asserted(
            [m for m in hosted], dev_rel,
            note="a hosted app mounts its own nginx snippet directory here")
    else:
        proxy.locations_dir = absent("no hosted-app snippet mount on the proxy service")
    return proxy


def collect_identity(root: Path, stack: Stack) -> Identity:
    ident = Identity()
    dev_rel = stack.compose_dev.value
    doc = _load_yaml(root / dev_rel) if dev_rel else {}
    services = doc.get("services") or {}

    kc = next((n for n in services if "keycloak" in n.lower()), None)
    if kc:
        ident.provider = asserted("keycloak", dev_rel)
    else:
        ident.provider = absent("no keycloak service in the dev compose")

    # The realm URL is configured, not guessed: find it in any service's environment.
    realm = None
    for svc in services.values():
        env = svc.get("environment") if isinstance(svc, dict) else None
        items = env.items() if isinstance(env, dict) else (
            [tuple(e.split("=", 1)) for e in env if "=" in e] if isinstance(env, list) else [])
        for key, value in items:
            if "REALM" in str(key).upper() and "realms/" in str(value):
                realm = str(value)
                break
        if realm:
            break
    if realm and "${" not in realm:
        ident.realm_url = asserted(realm, dev_rel)
        ident.jwks_url = asserted(f"{realm}/protocol/openid-connect/certs", dev_rel)
        ident.issuer = asserted(realm, dev_rel)
    else:
        # Fall back to env files, which is where a ${VAR} indirection resolves.
        for env_file in sorted(root.glob("**/*.env"))[:40] + list(root.glob("infra/*/.env")):
            try:
                text = env_file.read_text()
            except Exception:
                continue
            m = re.search(r"^[A-Z_]*REALM[A-Z_]*_?URL?=(\S+)$", text, re.M)
            if m and "realms/" in m.group(1):
                realm = m.group(1)
                ident.realm_url = asserted(realm, _rel(root, env_file))
                ident.jwks_url = asserted(f"{realm}/protocol/openid-connect/certs",
                                          _rel(root, env_file))
                ident.issuer = asserted(realm, _rel(root, env_file))
                break
        else:
            ident.realm_url = absent("no realm URL found in compose or env files")

    # A working verifier in the tree beats a specification of one.
    for candidate in sorted(root.glob("*/api/app/auth.py")) + sorted(root.glob("**/auth/jwt_verify.py")):
        text = candidate.read_text(errors="replace")
        if "jwks" in text.lower() or "JWKS" in text:
            ident.reference_impl = asserted(_rel(root, candidate), _rel(root, candidate),
                                            note="a hosted app already verifies tokens this way")
            algs = re.findall(r'"(RS\d{3})"', text)
            if algs:
                ident.algorithms = asserted(sorted(set(algs)), _rel(root, candidate))
            break
    else:
        ident.reference_impl = absent("no in-tree token verifier found")

    # Roles a hosted app registers, and the host endpoint apps reuse for a session.
    roles: list[str] = []
    for script in root.glob("*/infra/keycloak/*.sh"):
        roles += re.findall(r"[\"']?([a-z][a-z0-9_]*_(?:user|admin|engineer))[\"']?", script.read_text())
    if roles:
        ident.roles = asserted(sorted(set(roles)),
                               _rel(root, next(root.glob("*/infra/keycloak/*.sh"))))
    else:
        ident.roles = absent("no role-registration script found")

    for pattern in ("**/auth/refresh", "/api/auth/refresh"):
        hits = _grep(root, re.escape("/api/auth/refresh"), limit=1)
        if hits:
            ident.session_endpoint = asserted("/api/auth/refresh", hits[0],
                                              note="hosted apps reuse the host session here")
            break
    else:
        ident.session_endpoint = absent("no shared session endpoint found")
    return ident


def _grep(root: Path, pattern: str, limit: int = 5,
          globs: tuple[str, ...] = ("**/*.ts", "**/*.py", "**/*.conf")) -> list[str]:
    rx = re.compile(pattern)
    out: list[str] = []
    for glob in globs:
        for path in root.glob(glob):
            if "node_modules" in path.parts or ".git" in path.parts:
                continue
            try:
                if rx.search(path.read_text(errors="replace")):
                    out.append(_rel(root, path))
                    if len(out) >= limit:
                        return out
            except Exception:
                continue
    return out


def collect_frontend(root: Path) -> Frontend:
    fe = Frontend()
    pkg = _first_existing(root, ["source-code/frontend/package.json", "frontend/package.json",
                                 "web/package.json", "src/frontend/package.json"])
    if not pkg:
        fe.framework = absent("no frontend package.json found")
        return fe
    try:
        data = json.loads(pkg.read_text())
    except Exception:
        data = {}
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    for name, label in (("@angular/core", "angular"), ("react", "react"), ("vue", "vue")):
        if name in deps:
            fe.framework = asserted(label, _rel(root, pkg))
            fe.version = asserted(deps[name], _rel(root, pkg))
            break
    else:
        fe.framework = absent("no known framework in the frontend package.json")

    styles = _first_existing(root, [str(Path(_rel(root, pkg)).parent / "src/styles.scss"),
                                    str(Path(_rel(root, pkg)).parent / "src/styles.css")])
    if styles:
        fe.styles_entry = asserted(_rel(root, styles), _rel(root, styles))
        tokens = CSS_VAR_RE.findall(styles.read_text(errors="replace"))
        fe.design_tokens = (asserted({k: v.strip() for k, v in tokens[:60]},
                                     _rel(root, styles),
                                     note="reuse these rather than inventing colours")
                            if tokens else absent("no CSS custom properties in the styles entry"))
    else:
        fe.styles_entry = absent("no styles entry found next to the frontend package.json")

    nav = _grep(root, r"(sidenav|sidebar)\.component\.ts$|NavItem", limit=1)
    fe.nav_wiring = (asserted(nav[0], nav[0], note="where a hosted app's link is added")
                     if nav else absent("no sidebar/nav component found"))
    return fe


def collect_build(root: Path) -> Build:
    build = Build()
    bake = _first_existing(root, ["infra/ci/docker-bake.hcl", "docker-bake.hcl"])
    if bake:
        build.bake_file = asserted(_rel(root, bake), _rel(root, bake))
        build.targets = asserted(BAKE_TARGET_RE.findall(bake.read_text()), _rel(root, bake))
    else:
        build.bake_file = absent("no docker-bake.hcl found")

    ci_dir = _first_existing(root, ["infra/ci/ci_stage.sh"])
    scripts = sorted(p.name for p in (root / "infra/ci").glob("*.sh")) if (root / "infra/ci").is_dir() else []
    build.ci_scripts = (asserted(scripts, "infra/ci/") if scripts
                        else absent("no infra/ci shell scripts found"))
    gates = [f"infra/ci/{name}" for name in scripts if "smoke" in name or "healthy" in name]
    build.gates = (asserted(gates, "infra/ci/",
                            note="the host already gates itself with these; wrap, do not replace")
                   if gates else absent("the host has no smoke/health scripts"))
    if ci_dir is None and not scripts:
        build.ci_scripts = absent("no infra/ci directory")
    return build


def collect_hosted_apps(root: Path, stack: Stack, build: Build) -> list[HostedApp]:
    """Apps already living in this host — the conforming examples to copy."""
    apps: list[HostedApp] = []
    for include in (stack.includes.value or []):
        # "../../jhup_chat/infra/compose.dev.yml" -> jhup_chat
        parts = Path(include).parts
        name = next((p for p in parts if p not in ("..", ".", "infra")), None)
        if not name or any(a.name == name for a in apps):
            continue
        app_dir = root / name
        app = HostedApp(name=name)
        app.compose_files = asserted(
            sorted(_rel(root, p) for p in app_dir.glob("infra/compose*.yml")), name)

        snippets = sorted(app_dir.glob("infra/nginx/*/*.conf"))
        if snippets:
            text = snippets[0].read_text(errors="replace")
            prefixes = [p for p in LOCATION_RE.findall(text) if p not in ("/",)]
            root_prefix = min(prefixes, key=len) if prefixes else None
            app.nginx_snippet = asserted(_rel(root, snippets[0]), _rel(root, snippets[0]))
            app.path_prefix = (asserted(root_prefix if root_prefix.endswith("/") else root_prefix + "/",
                                        _rel(root, snippets[0]))
                               if root_prefix else absent("no location prefix in the snippet"))
        else:
            app.nginx_snippet = absent(f"no nginx snippet under {name}/infra/nginx")
            app.path_prefix = absent(
                "no proxy route: this app is internal to the network, reached by hostname")
            app.reachable = absent("internal-only app; nothing to probe through the proxy")

        targets = [t for t in (build.targets.value or []) if t.startswith(name.replace("_", "-"))]
        app.bake_targets = (asserted(targets, build.bake_file.value)
                            if targets else absent("no bake targets named for this app"))

        dev = next((p for p in app_dir.glob("infra/compose.dev.yml")), None)
        if dev:
            doc = _load_yaml(dev)
            app.services = asserted(sorted((doc.get("services") or {}).keys()), _rel(root, dev))
        apps.append(app)
    return apps


def collect(root: Path, host: str, generated_at: str) -> HostProfile:
    stack = collect_stack(root)
    build = collect_build(root)
    profile = HostProfile(
        host=host, root=str(root), generated_at=generated_at,
        stack=stack,
        proxy=collect_proxy(root, stack),
        identity=collect_identity(root, stack),
        frontend=collect_frontend(root),
        build=build,
    )
    profile.hosted_apps = collect_hosted_apps(root, stack, build)
    return profile
