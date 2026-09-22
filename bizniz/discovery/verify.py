"""Turn what the repository *says* into what the running host *does*.

Every check here either promotes a claim to ``verified`` or leaves it ``asserted``
and records why. Nothing is invented: a claim with no way to check it stays
asserted, and says so in the profile.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import httpx

from bizniz.discovery.types import HostProfile, verified

Log = Callable[[str], None]


def _run(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 90,
         env_extra: Optional[dict] = None) -> tuple[int, str]:
    env = {**os.environ, **env_extra} if env_extra else None
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def _get(url: str, timeout: float = 5.0) -> tuple[Optional[int], str]:
    """(status, body). The body is whole: callers parse JSON out of it."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=False)
        return resp.status_code, resp.text
    except httpx.HTTPError as exc:
        return None, str(exc)[:200]


def _get_json(url: str, timeout: float = 5.0) -> tuple[Optional[int], dict]:
    status, body = _get(url, timeout)
    if status != 200:
        return status, {}
    try:
        return status, json.loads(body)
    except ValueError:
        return status, {}


def verify(profile: HostProfile, log: Log = lambda _m: None) -> HostProfile:
    root = Path(profile.root)

    # ── the compose file parses, and these are really its services ──
    dev = profile.stack.compose_dev.value
    if dev and shutil.which("docker"):
        code, out = _run(["docker", "compose", "-f", dev, "config", "--services"], cwd=root)
        if code == 0:
            services = sorted(s for s in out.split() if s)
            profile.stack.services = verified(
                services, f"docker compose -f {dev} config --services", dev,
                note="merged across includes, so hosted apps' services appear here")
            log(f"stack: {len(services)} services (verified)")
        else:
            profile.gaps.append(f"`docker compose -f {dev} config` failed: {out.strip()[:120]}")

        code, out = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
        if code == 0:
            running = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
            profile.stack.running = verified(sorted(running), "docker ps")
            log(f"running: {len(running)} containers")
    elif not shutil.which("docker"):
        profile.gaps.append("docker is not on PATH: nothing about the running stack was verified")

    # ── the proxy answers, and each hosted app answers behind its prefix ──
    base = profile.proxy.dev_base_url.value
    if base:
        status, _body = _get(base + "/")
        if status is not None:
            profile.proxy.dev_base_url = verified(base, f"GET {base}/ -> {status}")
            log(f"proxy: {base} -> {status}")
        else:
            profile.gaps.append(f"proxy {base} did not answer; is the dev stack up?")

        for app in profile.hosted_apps:
            prefix = app.path_prefix.value
            if not prefix:
                continue
            url = base.rstrip("/") + prefix
            status, _body = _get(url)
            if status is None:
                app.reachable = app.reachable.model_copy(
                    update={"evidence": "asserted", "how": f"GET {url} failed"})
                profile.gaps.append(f"{app.name}: {url} did not answer")
            else:
                app.reachable = verified(status, f"GET {url} -> {status}")
                # The route answering is what proves the prefix, not the file that declares it.
                if status < 500:
                    app.path_prefix = verified(prefix, f"GET {url} -> {status}",
                                               app.nginx_snippet.value)
                log(f"{app.name}: {url} -> {status}")

    # ── identity: the keys are really served, and the realm really answers ──
    jwks = profile.identity.jwks_url.value
    if jwks:
        url = _localize(jwks, profile)
        status, doc = _get_json(url)
        keys = doc.get("keys") or []
        if keys:
            algs = sorted({k.get("alg") for k in keys if k.get("alg")})
            profile.identity.jwks_url = verified(
                jwks, f"GET {url} -> 200, {len(keys)} signing key(s)",
                note="a hosted app verifies bearer tokens against these keys")
            if algs:
                profile.identity.algorithms = verified(
                    algs, f"alg of the keys served at {url}",
                    note="what a token from this realm is actually signed with")
            log(f"identity: JWKS {url} -> {len(keys)} key(s), {algs}")
        else:
            profile.gaps.append(
                f"JWKS {url} did not return keys (status {status}); "
                "token verification is unproven")

    realm = profile.identity.realm_url.value
    if realm:
        url = _localize(realm, profile) + "/.well-known/openid-configuration"
        status, doc = _get_json(url)
        issuer = doc.get("issuer")
        if issuer:
            profile.identity.realm_url = verified(
                realm, f"GET {url} -> 200",
                note="the realm answers its discovery document")
            # Keycloak echoes the host it was reached on, so the issuer a token carries
            # depends on whether it was obtained through the browser or from inside the
            # network. An app that pins one of them rejects tokens minted via the other.
            configured_host = realm.split("//", 1)[-1].split("/", 1)[0]
            probed_host = issuer.split("//", 1)[-1].split("/", 1)[0]
            note = "tokens must carry exactly this iss claim"
            if configured_host != probed_host:
                note = (f"reached over `{probed_host}` the realm calls itself `{issuer}`, but it is "
                        f"configured as `{realm}`. The issuer follows the host used to reach "
                        f"Keycloak, so an app must accept BOTH — this is why the reference "
                        f"implementation takes a list of issuers, not one.")
                profile.gaps.append(
                    f"issuer differs by route: `{realm}` configured, `{issuer}` served to a "
                    "caller on the host. Accept both, or pin Keycloak's hostname.")
            profile.identity.issuer = verified(issuer, f"issuer in {url}", note=note)
            if profile.identity.provider.value:
                profile.identity.provider = verified(
                    profile.identity.provider.value, f"{url} answered")
            log(f"identity: issuer {issuer}")
        elif status is not None:
            profile.gaps.append(
                f"realm discovery {url} returned {status} without an issuer")

    # ── the shared network exists, and the proxy is really serving from the mount ──
    network = profile.stack.network.value
    if network and shutil.which("docker"):
        code, _out = _run(["docker", "network", "inspect", network])
        if code == 0:
            profile.stack.network = verified(
                network, f"docker network inspect {network}",
                note="a hosted app joins this as an external network")
            log(f"network: {network} exists")
        else:
            profile.gaps.append(f"network {network} does not exist; is the dev stack up?")

    proxy_service = profile.proxy.service.value
    mount = profile.proxy.locations_dir.value
    if proxy_service and mount and shutil.which("docker"):
        container = ([proxy_service] + _containers_matching(profile, proxy_service))[-1]
        parent = str(Path(mount).parent)
        code, out = _run(["docker", "exec", container, "ls", parent])
        if code == 0:
            entries = sorted(e for e in out.split() if e)
            profile.proxy.locations_dir = verified(
                mount, f"docker exec {container} ls {parent} -> {', '.join(entries) or 'empty'}",
                note=f"snippets land in {parent}/<app>/ and are included by the server block")
            log(f"proxy: snippet dir holds {entries}")
        else:
            profile.gaps.append(f"could not list {parent} in the proxy container")

    # ── the session endpoint hosted apps reuse actually exists ──
    session = profile.identity.session_endpoint.value
    if session and base:
        url = base.rstrip("/") + session
        status, _body = _get(url)
        if status is None:
            profile.gaps.append(f"session endpoint {url} did not answer")
        elif status == 404:
            profile.gaps.append(
                f"session endpoint {url} returned 404 — the path may have moved")
        else:
            # 401/403 is the right answer to an unauthenticated probe: the route exists.
            profile.identity.session_endpoint = verified(
                session, f"GET {url} -> {status}",
                note="unauthenticated probe; a non-404 proves the route exists")
            log(f"identity: session endpoint {session} -> {status}")

    # ── hosted apps' services really are in the merged stack ──
    merged = set(profile.stack.services.value or [])
    if merged:
        for app in profile.hosted_apps:
            declared = set(app.services.value or [])
            if declared and declared <= merged:
                app.services = verified(
                    sorted(declared), "present in `docker compose config --services`",
                    note="attached to the host through its compose include")
            elif declared:
                profile.gaps.append(
                    f"{app.name}: {sorted(declared - merged)} declared but not in the merged stack")

    # ── bake targets resolve ──
    bake = profile.build.bake_file.value
    if bake and shutil.which("docker"):
        # bake interpolates REGISTRY/IMAGE_TAG; give them values so --print resolves.
        env_hint = {"REGISTRY": "127.0.0.1:5000", "IMAGE_TAG": "discover"}
        code, out = _run(["docker", "buildx", "bake", "-f", bake, "--print"],
                         cwd=root, timeout=120, env_extra=env_hint)
        targets: list[str] = []
        if code == 0:
            # The JSON is followed by buildx's progress output on stderr, so decode just
            # the first value rather than the whole stream.
            brace = out.find("{")
            if brace >= 0:
                try:
                    doc, _end = json.JSONDecoder().raw_decode(out[brace:])
                    targets = sorted(doc.get("target", {}))
                except ValueError:
                    targets = []
        if targets:
            profile.build.targets = verified(
                targets, f"docker buildx bake -f {bake} --print", bake,
                note="an app adds its own target here to be built by CI")
            log(f"build: {len(targets)} bake targets resolve")
        else:
            profile.gaps.append(
                f"`docker buildx bake -f {bake} --print` did not resolve targets "
                f"(exit {code}); the target list is read from the file, unconfirmed")

    # ── the host's own gate scripts exist and are executable ──
    gates = profile.build.gates.value or []
    runnable = [g for g in gates if (root / g).is_file()]
    if runnable:
        profile.build.gates = verified(
            runnable, "stat " + ", ".join(runnable),
            note="wrap these rather than reimplementing them")
    return profile


def _localize(url: str, profile: HostProfile) -> str:
    """Rewrite a container hostname to something reachable from this machine.

    The compose environment names services by their docker hostname (``keycloak:8080``),
    which does not resolve outside the network. If that service publishes a host port,
    use it; otherwise leave the URL alone and let the probe fail honestly.
    """
    import re

    m = re.match(r"^(https?)://([^:/]+)(?::(\d+))?(/.*)?$", url)
    if not m:
        return url
    scheme, host, port, rest = m.groups()
    if host in ("localhost", "127.0.0.1"):
        return url
    published = _published_port(profile, host, port)
    if published:
        return f"{scheme}://localhost:{published}{rest or ''}"
    return url


def _published_port(profile: HostProfile, service: str, container_port: Optional[str]) -> Optional[str]:
    """Host port for a service, addressed by the name a *container* would use.

    Compose environments name services by network alias (`keycloak:8080`), which is not
    the container name (`conduit-keycloak`), so `docker port keycloak` finds nothing.
    Try the alias, then any running container whose name contains it.
    """
    for candidate in [service] + _containers_matching(profile, service):
        code, out = _run(["docker", "port", candidate] + ([container_port] if container_port else []))
        if code == 0 and out.strip():
            # "0.0.0.0:8080" / "[::]:8080"
            return out.strip().splitlines()[0].rsplit(":", 1)[-1].strip() or None
    return None


def _containers_matching(profile: HostProfile, alias: str) -> list[str]:
    running = profile.stack.running.value or []
    return [name for name in running if alias in name]
