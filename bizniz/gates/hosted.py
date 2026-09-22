"""Gate an app hosted inside an existing system.

The standalone smoke gate curls a stack it owns. A hosted app has no stack of its
own: it answers behind the host's proxy, under a path prefix, and authenticates with
the host's tokens. The failures that matter are correspondingly different —

  * the prefix is not routed, or the rewrite drops a path segment;
  * a route that should require a bearer answers anonymously;
  * a valid host-issued token is rejected, because the app pins the wrong issuer
    or audience.

Each is checked against the running host with a real token. No model is involved.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

from bizniz.discovery.types import HostProfile
from bizniz.gates.keycloak import TokenError, mint_token, realm_from_profile, token_roles

Log = Callable[[str], None]

#: Paths a hosted app must answer without a bearer, or the host cannot health-check it.
PUBLIC_SUFFIXES = ("/health", "/healthz", "/ready", "/readyz", "/livez")
#: Paths that may be either public or protected — putting API docs behind auth is a
#: legitimate choice, so the gate reports what it found and does not judge it.
EITHER_SUFFIXES = ("/openapi.json", "/docs", "/redoc")


@dataclass
class Check:
    category: str
    target: str
    passed: bool
    status: Optional[int] = None
    detail: str = ""

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        code = f" [{self.status}]" if self.status is not None else ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"{mark} {self.category:12s} {self.target}{code}{detail}"


@dataclass
class Result:
    app: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        return not self.failed


def load_profile(repo: Path) -> HostProfile:
    path = Path(repo) / ".bizniz" / "host" / "profile.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no host profile at {path}. Run `bizniz discover {repo}` first.")
    return HostProfile.model_validate_json(path.read_text())


def _get(url: str, token: Optional[str] = None, timeout: float = 10.0):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        return httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False), None
    except httpx.HTTPError as exc:
        return None, str(exc)[:160]


def _openapi_gets(base_api: str) -> tuple[list[str], Optional[str]]:
    """GET paths with no path parameters, from the app's own OpenAPI document."""
    resp, err = _get(base_api.rstrip("/") + "/openapi.json")
    if resp is None:
        return [], f"openapi.json unreachable: {err}"
    if resp.status_code != 200:
        return [], f"openapi.json returned {resp.status_code}"
    try:
        paths = json.loads(resp.text).get("paths", {})
    except ValueError:
        return [], "openapi.json was not JSON"
    return sorted(p for p, ops in paths.items() if "get" in ops and "{" not in p), None


def gate(repo: Path, app_name: Optional[str] = None, *, roles: Optional[list[str]] = None,
         with_auth: bool = True, log: Log = lambda _m: None) -> Result:
    profile = load_profile(repo)

    apps = [a for a in profile.hosted_apps if a.path_prefix.value]
    if app_name:
        apps = [a for a in profile.hosted_apps if a.name == app_name]
        if not apps:
            raise KeyError(f"no hosted app named '{app_name}' in the profile")
        if not apps[0].path_prefix.value:
            raise KeyError(f"'{app_name}' has no proxy route: {apps[0].path_prefix.how}")
    if not apps:
        raise KeyError("the profile holds no proxied app to gate")
    app = apps[0]

    base = (profile.proxy.dev_base_url.value or "").rstrip("/")
    if not base:
        raise KeyError("the profile has no proxy base URL; re-run discover with the stack up")

    prefix = app.path_prefix.value
    result = Result(app=app.name)

    # ── the host itself is up, or nothing below means anything ──
    resp, err = _get(base + "/")
    result.checks.append(Check("host", base + "/", resp is not None and resp.status_code < 500,
                               resp.status_code if resp else None, err or ""))
    if resp is None:
        return result

    # ── the app's prefix is routed ──
    app_url = base + prefix
    resp, err = _get(app_url)
    routed = resp is not None and resp.status_code < 500
    detail = err or ("proxy reached no upstream — is the app running?"
                     if resp is not None and resp.status_code in (502, 503, 504) else "")
    result.checks.append(Check("route", app_url, routed,
                               resp.status_code if resp else None, detail))

    # ── its API answers under the prefix, and tells us its routes ──
    api_base = f"{base}{prefix}api"
    paths, why = _openapi_gets(api_base)
    if why:
        result.notes.append(f"no route list: {why}; probing the documented prefix only")
    else:
        log(f"{app.name}: {len(paths)} GET route(s) from openapi.json")

    token, presented = None, []
    if with_auth:
        try:
            wanted = roles or _roles_for(profile, app.name)
            token = mint_token(realm_from_profile(profile), wanted,
                               audiences=profile.identity.audiences.value or None,
                               roles_claim=profile.identity.roles_claim.value or "",
                               log=log)
            presented = token_roles(token)
            result.notes.append(f"token presents roles: {', '.join(presented) or '(none)'}")
        except TokenError as exc:
            # Not being able to mint is itself a finding: the auth contract is unproven.
            result.checks.append(Check("token", "keycloak", False, None, str(exc)))

    for path in paths:
        url = api_base + path
        public = path.endswith(PUBLIC_SUFFIXES)
        either = path.endswith(EITHER_SUFFIXES)
        resp, err = _get(url)
        if resp is None:
            result.checks.append(Check("anon", url, False, None, err or "no response"))
            continue

        if either:
            state = "public" if resp.status_code == 200 else "protected"
            result.notes.append(f"{path} is {state} ({resp.status_code})")
            continue
        if public:
            result.checks.append(Check("public", url, resp.status_code == 200,
                                       resp.status_code,
                                       "" if resp.status_code == 200 else "should answer anonymously"))
        else:
            # The whole point of hosting behind the host's identity: no bearer, no data.
            enforced = resp.status_code in (401, 403)
            result.checks.append(Check(
                "anon", url, enforced, resp.status_code,
                "" if enforced else "answered without a bearer token"))

        if token and not public:
            resp, err = _get(url, token=token)
            if resp is None:
                result.checks.append(Check("auth", url, False, None, err or "no response"))
                continue
            accepted = resp.status_code not in (401, 403)
            detail = ""
            if not accepted:
                detail = ("a token this host issued was refused — check the accepted issuers, "
                          "audience and required roles")
            elif resp.status_code >= 500:
                accepted, detail = False, "server error with a valid token"
            result.checks.append(Check("auth", url, accepted, resp.status_code, detail))

    return result


def _roles_for(profile: HostProfile, app_name: str) -> list[str]:
    """The roles this app's own routes are likely to require.

    Hosted apps here name their roles after themselves (`<app>_user`, `<app>_engineer`),
    so prefer those, and fall back to every role discovery found.
    """
    known = list(profile.identity.roles.value or [])
    stem = app_name.replace("-", "_")
    mine = [r for r in known if r.startswith(stem)]
    return mine or known
