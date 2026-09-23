"""The hosted gate: routing, anonymous enforcement, and a real token being accepted."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from bizniz.discovery.types import HostedApp, HostProfile, asserted, verified
from bizniz.gates import hosted
from bizniz.gates.hosted import Result, gate, load_profile
from bizniz.gates.keycloak import _mapper_json, token_roles

OPENAPI = {
    "paths": {
        "/health": {"get": {}},
        "/me": {"get": {}},
        "/models": {"get": {}},
        "/docs": {"get": {}},
        "/conversations": {"get": {}, "post": {}},
        "/conversations/{id}": {"get": {}},          # path parameters are not probed
        "/upload": {"post": {}},                      # not a GET
    }
}


def make_profile(tmp_path: Path) -> HostProfile:
    p = HostProfile(host="Host", root=str(tmp_path), generated_at="2026-09-22")
    p.proxy.dev_base_url = verified("http://host.invalid:8080", "GET -> 200")
    p.identity.realm_url = asserted("http://keycloak:8080/realms/r", "compose.yml")
    p.identity.roles = asserted(["chatty_user", "other_user"], "roles.sh")
    p.identity.audiences = asserted(["spa"], "config.py")
    p.identity.roles_claim = asserted("roles", "config.py")
    p.stack.running = verified(["keycloak"], "docker ps")
    app = HostedApp(name="chatty")
    app.path_prefix = verified("/chatty/", "GET -> 200")
    p.hosted_apps = [app]
    return p


@pytest.fixture
def profile_on_disk(tmp_path):
    def _write(profile: HostProfile) -> Path:
        out = tmp_path / ".bizniz" / "host"
        out.mkdir(parents=True)
        (out / "profile.json").write_text(profile.model_dump_json())
        return tmp_path
    return _write


@pytest.fixture
def fake_http(monkeypatch):
    """Serve scripted responses, and record whether each request carried a bearer."""
    state = {"routes": {}, "seen": []}

    def handler(request: httpx.Request) -> httpx.Response:
        authed = "authorization" in {k.lower() for k in request.headers}
        state["seen"].append((str(request.url), authed))
        key = (request.url.path, authed)
        status, body = state["routes"].get(key, state["routes"].get(request.url.path, (200, "{}")))
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    real_client = httpx.Client

    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        with real_client(transport=httpx.MockTransport(handler)) as c:
            return c.get(url, headers=headers or {})

    monkeypatch.setattr(hosted.httpx, "get", fake_get)
    return state


def route_map(overrides: dict | None = None) -> dict:
    """Scripted responses, keyed by path or by (path, authenticated)."""
    base = {
        "/chatty/": (200, "<html></html>"),
        "/": (200, "<html></html>"),
        "/chatty/api/openapi.json": (200, OPENAPI),
        ("/chatty/api/health", False): (200, "{}"),
        ("/chatty/api/docs", False): (401, "{}"),
        ("/chatty/api/me", False): (401, "{}"),
        ("/chatty/api/me", True): (200, "{}"),
        ("/chatty/api/models", False): (401, "{}"),
        ("/chatty/api/models", True): (200, "{}"),
        ("/chatty/api/conversations", False): (401, "{}"),
        ("/chatty/api/conversations", True): (200, "{}"),
    }
    base.update(overrides or {})
    return base


def run(tmp_path, fake_http, profile_on_disk, *, routes=None, profile=None) -> Result:
    profile = profile or make_profile(tmp_path)
    repo = profile_on_disk(profile)
    fake_http["routes"] = routes or route_map()
    return gate(repo, "chatty", with_auth=False)


def test_missing_profile_says_to_run_discover(tmp_path):
    with pytest.raises(FileNotFoundError, match="bizniz discover"):
        load_profile(tmp_path)


def test_probes_only_parameterless_get_routes(tmp_path, fake_http, profile_on_disk):
    result = run(tmp_path, fake_http, profile_on_disk)
    probed = {url for url, _authed in fake_http["seen"]}
    assert not any("{id}" in u for u in probed)
    assert not any(u.endswith("/upload") for u in probed)
    assert any(u.endswith("/api/me") for u in probed)


def test_a_protected_route_answering_anonymously_fails(tmp_path, fake_http, profile_on_disk):
    routes = route_map({("/chatty/api/me", False): (200, "{}")})
    result = run(tmp_path, fake_http, profile_on_disk, routes=routes)
    failure = next(c for c in result.failed if c.target.endswith("/api/me"))
    assert failure.category == "anon"
    assert "without a bearer" in failure.detail


def test_health_must_answer_anonymously(tmp_path, fake_http, profile_on_disk):
    routes = route_map({("/chatty/api/health", False): (401, "{}")})
    result = run(tmp_path, fake_http, profile_on_disk, routes=routes)
    assert any(c.category == "public" and not c.passed for c in result.checks)


def test_protected_api_docs_is_reported_not_failed(tmp_path, fake_http, profile_on_disk):
    """Putting the docs behind auth is a choice; the gate records it and moves on."""
    result = run(tmp_path, fake_http, profile_on_disk)
    assert any("/docs is protected" in n for n in result.notes)
    assert not any(c.target.endswith("/docs") for c in result.checks)


def test_an_unroutable_prefix_fails_the_route_check(tmp_path, fake_http, profile_on_disk):
    routes = route_map({"/chatty/": (502, "bad gateway")})
    result = run(tmp_path, fake_http, profile_on_disk, routes=routes)
    failure = next(c for c in result.failed if c.category == "route")
    assert "is the app running" in failure.detail


def test_a_host_that_is_down_stops_the_gate_early(tmp_path, fake_http, profile_on_disk):
    routes = route_map({"/": (503, "down")})
    result = run(tmp_path, fake_http, profile_on_disk, routes=routes)
    assert not result.passed
    assert result.checks[0].category == "host"


def test_clean_stack_passes(tmp_path, fake_http, profile_on_disk):
    assert run(tmp_path, fake_http, profile_on_disk).passed


def test_an_app_without_a_route_is_not_gateable(tmp_path, fake_http, profile_on_disk):
    profile = make_profile(tmp_path)
    profile.hosted_apps[0].path_prefix = asserted(None, "none")
    repo = profile_on_disk(profile)
    fake_http["routes"] = route_map()
    with pytest.raises(KeyError):
        gate(repo, "chatty", with_auth=False)


def test_roles_prefer_the_apps_own(tmp_path):
    assert hosted._roles_for(make_profile(tmp_path), "chatty") == ["chatty_user"]


def test_roles_fall_back_to_everything_known(tmp_path):
    assert hosted._roles_for(make_profile(tmp_path), "unrelated") == ["chatty_user", "other_user"]


def test_audience_mapper_requests_the_claim_the_app_checks():
    body = json.loads(_mapper_json("m", "oidc-audience-mapper",
                                   {"included.client.audience": "spa",
                                    "access.token.claim": "true"}))
    assert body["protocolMapper"] == "oidc-audience-mapper"
    assert body["config"]["included.client.audience"] == "spa"


def test_token_roles_reads_both_placements():
    def encode(claims: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    assert token_roles(encode({"roles": ["a"], "realm_access": {"roles": ["b"]}})) == ["a", "b"]
    assert token_roles("not-a-token") == []
