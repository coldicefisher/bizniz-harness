"""KeycloakOperator: reconciles a realm, and proves the accounts actually work."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import httpx
import pytest

from bizniz.auth_operator.keycloak_operator import KeycloakOperator

BASE = "http://keycloak:8080"
REALM = "widgets"


@dataclass
class _User:
    email: str
    password: str = "pw"
    first_name: str = ""
    last_name: str = ""
    roles: List[str] = field(default_factory=list)


@dataclass
class _Role:
    name: str


@dataclass
class _Spec:
    roles: List[_Role] = field(default_factory=list)
    users: List[_User] = field(default_factory=list)


@pytest.fixture
def realm(monkeypatch):
    """A Keycloak that answers like the real one, and records what was asked of it."""
    state = {
        "roles": [{"name": "default-roles-widgets"}],
        "users": {},                 # email -> id
        "assigned": {},              # id -> [role names]
        "requests": [],
        "login_ok": True,
        "admin_ok": True,
    }
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        state["requests"].append(f"{method} {url}")

        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": f"{BASE}/realms/{REALM}"})
        if url.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json={"keys": [
                {"kid": "abc", "alg": "RS256", "use": "sig"}]})
        if url.endswith("/protocol/openid-connect/token"):
            body = request.content.decode()
            if "grant_type=client_credentials" in body:
                return httpx.Response(200 if state["admin_ok"] else 401,
                                      json={"access_token": "admin-token"})
            if not state["login_ok"]:
                return httpx.Response(401, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "user-token"})

        if url.endswith(f"/admin/realms/{REALM}/roles") and method == "GET":
            return httpx.Response(200, json=state["roles"])
        if url.endswith(f"/admin/realms/{REALM}/roles") and method == "POST":
            import json as _json
            state["roles"].append({"name": _json.loads(request.content)["name"]})
            return httpx.Response(201)

        if "/admin/realms/%s/users" % REALM in url and method == "GET":
            email = dict(request.url.params).get("email", "")
            found = state["users"].get(email)
            return httpx.Response(200, json=[{"id": found}] if found else [])
        if url.endswith(f"/admin/realms/{REALM}/users") and method == "POST":
            import json as _json
            counter["n"] += 1
            state["users"][_json.loads(request.content)["email"]] = f"id-{counter['n']}"
            return httpx.Response(201)
        if "/role-mappings/realm" in url and method == "POST":
            import json as _json
            user_id = url.split("/users/")[1].split("/")[0]
            state["assigned"][user_id] = [r["name"] for r in _json.loads(request.content)]
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_request(method, url, **kwargs):
        with real_client(transport=transport) as c:
            return c.request(method, url, **{k: v for k, v in kwargs.items()
                                             if k != "timeout"})

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: fake_request("GET", url, **kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: fake_request("POST", url, **kw))
    return state


def operator(**kw) -> KeycloakOperator:
    return KeycloakOperator(base_url=BASE, realm=REALM, client_id="widgets-api",
                            client_secret="s3cret", **kw)


def test_a_clean_realm_yields_a_manifest_of_what_is_there(realm):
    spec = _Spec(roles=[_Role("admin"), _Role("user")],
                 users=[_User("a@x.example.com", roles=["admin"])])
    manifest = operator().apply(spec=spec)

    assert manifest.issuer == f"{BASE}/realms/{REALM}"
    assert manifest.signing_key.algorithm == "RS256"
    assert manifest.signing_key.is_rs_family
    assert sorted(manifest.role_names()) == ["admin", "user"]
    assert manifest.users[0].registered and manifest.users[0].login_verified


def test_missing_roles_are_created_and_existing_ones_left_alone(realm):
    realm["roles"].append({"name": "admin"})
    operator().apply(spec=_Spec(roles=[_Role("admin"), _Role("editor")]))
    created = [r for r in realm["requests"] if r.startswith("POST") and r.endswith("/roles")]
    assert len(created) == 1                       # only `editor`


def test_keycloaks_built_in_roles_are_not_reported_as_the_applications(realm):
    manifest = operator().apply(spec=_Spec(roles=[_Role("admin")]))
    assert "default-roles-widgets" not in manifest.role_names()


def test_a_user_that_cannot_log_in_is_reported_not_assumed(realm):
    """Every other step can pass against a realm that still refuses to issue a token."""
    realm["login_ok"] = False
    manifest = operator().apply(spec=_Spec(users=[_User("a@x.example.com")]))
    assert manifest.users[0].registered is True
    assert manifest.users[0].login_verified is False
    assert manifest.all_users_login_verified is False


def test_an_existing_user_is_not_created_twice(realm):
    realm["users"]["a@x.example.com"] = "id-existing"
    operator().apply(spec=_Spec(users=[_User("a@x.example.com")]))
    assert not [r for r in realm["requests"]
                if r.startswith("POST") and r.endswith(f"/realms/{REALM}/users")]


def test_roles_are_assigned_to_the_user(realm):
    operator().apply(spec=_Spec(roles=[_Role("admin")],
                                users=[_User("a@x.example.com", roles=["admin"])]))
    assert list(realm["assigned"].values()) == [["admin"]]


def test_a_role_the_realm_does_not_have_is_reported_rather_than_assigned(realm):
    logs: List[str] = []
    operator(on_status=logs.append).apply(
        spec=_Spec(users=[_User("a@x.example.com", roles=["nonexistent"])]))
    assert any("no role(s) ['nonexistent']" in line for line in logs)


def test_without_admin_access_it_still_returns_a_manifest(realm):
    """No admin token is a degraded run, not a crash: the manifest shows what is real."""
    realm["admin_ok"] = False
    logs: List[str] = []
    manifest = operator(on_status=logs.append).apply(
        spec=_Spec(roles=[_Role("admin")], users=[_User("a@x.example.com")]))
    assert manifest.users[0].registered is False
    assert manifest.issuer == f"{BASE}/realms/{REALM}"
