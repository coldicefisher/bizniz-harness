"""Deterministic Keycloak applier. No LLM.

Most of what the FusionAuth operator had to do at build time, Keycloak does at container
start: the realm import creates the realm, the clients, the protocol mappers, the roles
and the seed user before anything asks. So this is much smaller, and its job is different
in kind — it **reconciles the plan against what is actually running**, and reports what it
found rather than what it intended.

What it does, in order:

1. waits for the realm to answer its discovery document (not just for the port to open —
   Keycloak serves HTTP well before the realm exists);
2. ensures every role the plan asks for exists, creating what is missing;
3. ensures every user the plan asks for exists with those roles;
4. logs each user in, so "this account works" is a fact rather than an assumption;
5. reads the live realm into the manifest.

Step 4 is the one that earns its keep. Every other step can succeed against a realm that
still refuses to issue a token — a missing direct-access grant, a disabled user, a
password policy the seed violates — and the failure would otherwise surface much later as
a generated test that cannot log in.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

import httpx

from bizniz.auth_operator.manifest import (
    ApplicationManifest, AuthManifest, RoleManifest, SigningKeyInfo, UserManifest,
)


class KeycloakOperatorError(Exception):
    """Keycloak could not be brought to the state the plan asked for."""


class KeycloakOperator:
    """Applies an AuthSpec to a running Keycloak realm."""

    def __init__(
        self,
        *,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str = "",
        admin_username: str = "",
        admin_password: str = "",
        on_status: Optional[Callable[[str], None]] = None,
        readiness_deadline_s: float = 600.0,
        readiness_poll_s: float = 5.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.admin_username = admin_username
        self.admin_password = admin_password
        self._on_status = on_status
        self._readiness_deadline_s = readiness_deadline_s
        self._readiness_poll_s = readiness_poll_s
        self._admin_token: Optional[str] = None

    # ── URLs ───────────────────────────────────────────────────────────

    @property
    def realm_url(self) -> str:
        return f"{self.base_url}/realms/{self.realm}"

    @property
    def token_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/token"

    @property
    def admin_url(self) -> str:
        return f"{self.base_url}/admin/realms/{self.realm}"

    # ── Apply ──────────────────────────────────────────────────────────

    def apply(self, *, spec, primary_app_id: str = "", tenant_id: str = "") -> AuthManifest:
        """Reconcile the realm with `spec`, and return what is actually there."""
        self._log("KeycloakOperator: apply starting")

        if not self._wait_for_realm():
            self._log("KeycloakOperator: the realm did not answer before the deadline; "
                      "continuing so the manifest reflects reality")

        roles = self._ensure_roles(self._spec_roles(spec))
        users = self._ensure_users(spec)
        for user in users:
            user.login_verified = self._smoke_login(user.email, user.password)

        manifest = AuthManifest(
            fa_url=self.realm_url,          # the manifest's field name predates Keycloak
            primary_app_id=self.client_id,
            tenant_id=self.realm,
            issuer=self._read_issuer(),
            signing_key=self._read_signing_key(),
            applications=[ApplicationManifest(
                name=self.client_id, application_id=self.client_id,
                role_names=[r.name for r in roles])],
            roles=roles,
            users=users,
        )
        verified = sum(1 for u in manifest.users if u.login_verified)
        self._log(f"KeycloakOperator: apply done — {len(manifest.users)} user(s) "
                  f"({verified} login-verified), {len(manifest.roles)} role(s), "
                  f"signing key {manifest.signing_key.algorithm}")
        return manifest

    # ── Readiness ──────────────────────────────────────────────────────

    def _wait_for_realm(self) -> bool:
        """Wait for the realm's discovery document, not merely for the port.

        Keycloak answers HTTP long before the import finishes, so polling the port
        reports ready while every realm request still 404s.
        """
        deadline = time.monotonic() + self._readiness_deadline_s
        url = f"{self.realm_url}/.well-known/openid-configuration"
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(url, timeout=10)
                if resp.status_code == 200 and resp.json().get("issuer"):
                    self._log(f"KeycloakOperator: realm '{self.realm}' is serving")
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(self._readiness_poll_s)
        return False

    # ── Admin access ───────────────────────────────────────────────────

    def _token(self) -> Optional[str]:
        """An admin-capable token: the client's service account, or the bootstrap admin."""
        if self._admin_token:
            return self._admin_token
        attempts = []
        if self.client_secret:
            attempts.append(("client_credentials", {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, self.token_url))
        if self.admin_username and self.admin_password:
            attempts.append(("bootstrap admin", {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": self.admin_username,
                "password": self.admin_password,
            }, f"{self.base_url}/realms/master/protocol/openid-connect/token"))

        for label, form, url in attempts:
            try:
                resp = httpx.post(url, data=form, timeout=15)
            except httpx.HTTPError as exc:
                self._log(f"KeycloakOperator: {label} token request failed: {exc}")
                continue
            if resp.status_code == 200:
                self._admin_token = resp.json().get("access_token")
                return self._admin_token
            self._log(f"KeycloakOperator: {label} refused ({resp.status_code})")
        return None

    def _admin(self, method: str, path: str, **kwargs) -> Optional[httpx.Response]:
        token = self._token()
        if not token:
            return None
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            return httpx.request(method, f"{self.admin_url}{path}", headers=headers,
                                 timeout=20, **kwargs)
        except httpx.HTTPError as exc:
            self._log(f"KeycloakOperator: admin {method} {path} failed: {exc}")
            return None

    # ── Roles ──────────────────────────────────────────────────────────

    @staticmethod
    def _spec_roles(spec) -> List[str]:
        names: List[str] = []
        for role in getattr(spec, "roles", []) or []:
            name = getattr(role, "name", None) or (role if isinstance(role, str) else None)
            if name:
                names.append(name)
        return names

    def _ensure_roles(self, wanted: List[str]) -> List[RoleManifest]:
        existing = {}
        resp = self._admin("GET", "/roles")
        if resp is not None and resp.status_code == 200:
            existing = {r["name"]: r for r in resp.json()}

        for name in wanted:
            if name in existing:
                continue
            created = self._admin("POST", "/roles", json={"name": name})
            if created is not None and created.status_code in (201, 409):
                self._log(f"KeycloakOperator: role '{name}' ensured")
            else:
                code = created.status_code if created is not None else "no admin access"
                self._log(f"KeycloakOperator: could not create role '{name}' ({code})")

        resp = self._admin("GET", "/roles")
        if resp is None or resp.status_code != 200:
            return [RoleManifest(name=n) for n in wanted]
        return [RoleManifest(name=r["name"], description=r.get("description") or "")
                for r in resp.json()
                # Keycloak's built-ins are noise in a manifest about this application.
                if not r["name"].startswith(("default-roles", "offline_access",
                                             "uma_authorization"))]

    # ── Users ──────────────────────────────────────────────────────────

    def _ensure_users(self, spec) -> List[UserManifest]:
        out: List[UserManifest] = []
        for want in getattr(spec, "users", []) or []:
            email = getattr(want, "email", "")
            password = getattr(want, "password", "")
            roles = list(getattr(want, "roles", []) or [])
            if not email:
                continue
            user_id = self._find_user(email) or self._create_user(want)
            if user_id:
                self._assign_roles(user_id, roles)
            out.append(UserManifest(
                email=email, user_id=user_id or "", password=password,
                first_name=getattr(want, "first_name", "") or "",
                last_name=getattr(want, "last_name", "") or "",
                roles=roles, registered=bool(user_id)))
        return out

    def _find_user(self, email: str) -> Optional[str]:
        resp = self._admin("GET", "/users", params={"email": email, "exact": "true"})
        if resp is None or resp.status_code != 200:
            return None
        found = resp.json()
        return found[0]["id"] if found else None

    def _create_user(self, want) -> Optional[str]:
        email = getattr(want, "email", "")
        resp = self._admin("POST", "/users", json={
            "username": email,
            "email": email,
            "firstName": getattr(want, "first_name", "") or "",
            "lastName": getattr(want, "last_name", "") or "",
            "enabled": True,
            "emailVerified": True,
            "credentials": [{"type": "password",
                             "value": getattr(want, "password", ""),
                             "temporary": False}],
        })
        if resp is not None and resp.status_code == 201:
            self._log(f"KeycloakOperator: created {email}")
            return self._find_user(email)
        if resp is not None and resp.status_code == 409:
            return self._find_user(email)
        code = resp.status_code if resp is not None else "no admin access"
        self._log(f"KeycloakOperator: could not create {email} ({code})")
        return None

    def _assign_roles(self, user_id: str, roles: List[str]) -> None:
        if not roles:
            return
        resp = self._admin("GET", "/roles")
        if resp is None or resp.status_code != 200:
            return
        available = {r["name"]: r for r in resp.json()}
        payload = [available[name] for name in roles if name in available]
        missing = [name for name in roles if name not in available]
        if missing:
            self._log(f"KeycloakOperator: realm has no role(s) {missing}; not assigned")
        if payload:
            self._admin("POST", f"/users/{user_id}/role-mappings/realm", json=payload)

    # ── Verification ───────────────────────────────────────────────────

    def _smoke_login(self, email: str, password: str) -> bool:
        """Log the user in for real. The only step that proves the realm works."""
        if not password:
            return False
        form = {"grant_type": "password", "client_id": self.client_id,
                "username": email, "password": password, "scope": "openid"}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        try:
            resp = httpx.post(self.token_url, data=form, timeout=15)
        except httpx.HTTPError as exc:
            self._log(f"KeycloakOperator: login for {email} failed to reach Keycloak: {exc}")
            return False
        if resp.status_code == 200 and resp.json().get("access_token"):
            return True
        self._log(f"KeycloakOperator: {email} could not log in ({resp.status_code}) — "
                  f"check that the client has direct access grants enabled")
        return False

    # ── Reading live state ─────────────────────────────────────────────

    def _read_issuer(self) -> str:
        try:
            resp = httpx.get(f"{self.realm_url}/.well-known/openid-configuration", timeout=10)
            if resp.status_code == 200:
                return resp.json().get("issuer", self.realm_url)
        except (httpx.HTTPError, ValueError):
            pass
        return self.realm_url

    def _read_signing_key(self) -> SigningKeyInfo:
        """The realm's active signing key, read from the JWKS it publishes.

        Keycloak is RS256 by default and has no equivalent of the HS256 trap that made
        this a whole stage for FusionAuth — but it is read rather than assumed, because
        the manifest is supposed to describe reality.
        """
        try:
            resp = httpx.get(f"{self.realm_url}/protocol/openid-connect/certs", timeout=10)
            keys = resp.json().get("keys", []) if resp.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            keys = []
        signing = [k for k in keys if k.get("use") == "sig"] or keys
        if not signing:
            return SigningKeyInfo(key_id="", algorithm="unknown")
        key = signing[0]
        return SigningKeyInfo(key_id=key.get("kid", ""),
                              algorithm=key.get("alg", "RS256"),
                              bound_to_app=True, bound_to_tenant=True)

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)
