"""Mint a real token from the host's identity provider, for gating.

A hosted app's most valuable check is "an anonymous caller is refused and a valid
bearer is accepted", and that needs a token the host actually issued. This gets one
without a human: it drives ``kcadm.sh`` inside the running Keycloak container using
the bootstrap admin credentials already in that container's environment, ensures a
dedicated service-account client exists, and asks for a token with the app's roles.

Secrets are never read into this process except the token itself, and the token is
never logged — the client secret stays inside the container, and the access token is
returned to the caller and used in a header.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

GATE_CLIENT_ID = "bizniz-gate"
Log = Callable[[str], None]


class TokenError(RuntimeError):
    """No token could be minted. The gate says so rather than skipping the check."""


@dataclass(frozen=True)
class Realm:
    container: str          # the Keycloak container name
    realm: str              # "conduit"
    token_url: str          # reachable from *this* machine
    internal_url: str       # reachable from inside the container ("http://localhost:8080")
    network_url: str = ""   # how services on the network reach it ("http://keycloak:8080/realms/x")
    network: str = ""       # the docker network the host's services share


def realm_from_profile(profile, running: Optional[list[str]] = None) -> Realm:
    """Work out how to reach the realm, from a discovery profile."""
    realm_url = profile.identity.realm_url.value
    if not realm_url:
        raise TokenError("the profile has no realm URL; run `bizniz discover` with the stack up")
    realm = realm_url.rstrip("/").rsplit("/", 1)[-1]

    names = running if running is not None else (profile.stack.running.value or [])
    container = next((n for n in names if "keycloak" in n.lower()), None)
    if not container:
        raise TokenError("no running Keycloak container found; is the host stack up?")

    from bizniz.discovery.verify import _localize     # same host→localhost mapping
    token_url = _localize(realm_url, profile) + "/protocol/openid-connect/token"
    return Realm(container=container, realm=realm, token_url=token_url,
                 internal_url="http://localhost:8080",
                 network_url=realm_url.rstrip("/") + "/protocol/openid-connect/token",
                 network=profile.stack.network.value or "")


def _kcadm(realm: Realm, script: str, timeout: int = 60) -> tuple[int, str]:
    """Run a snippet inside the Keycloak container with kcadm already authenticated.

    The admin credentials are referenced as shell variables that exist in the
    container's environment; they are never passed through this process.
    """
    preamble = (
        'set -e; KC=/opt/keycloak/bin/kcadm.sh; '
        f'"$KC" config credentials --server {realm.internal_url} --realm master '
        '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" '
        '>/dev/null 2>&1 || '
        f'"$KC" config credentials --server {realm.internal_url} --realm master '
        '--user "$KEYCLOAK_ADMIN" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; '
    )
    proc = subprocess.run(
        ["docker", "exec", realm.container, "sh", "-c", preamble + script],
        capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _mapper_json(name: str, mapper: str, config: dict) -> str:
    return json.dumps({"name": name, "protocol": "openid-connect",
                       "protocolMapper": mapper, "config": config})


def ensure_gate_client(realm: Realm, roles: list[str], audiences: Optional[list[str]] = None,
                       roles_claim: str = "", log: Log = lambda _m: None) -> None:
    """Create (or update) the service-account client the gate authenticates as.

    Idempotent, and confined to this realm. The client is confidential with a service
    account and no interactive grants: it can mint a token for itself and nothing else.
    """
    quoted_roles = " ".join(shlex.quote(r) for r in roles)

    # A token is refused for two reasons that have nothing to do with being invalid:
    # it carries the wrong `aud` (client credentials gives "account"), and Keycloak puts
    # realm roles under realm_access.roles while an app may read a flat claim. Both are
    # fixed with protocol mappers on the gate client, created from JSON on stdin —
    # `-s config."included.client.audience"=x` does not apply reliably through kcadm.
    mappers: list[tuple[str, str]] = []
    for aud in audiences or []:
        mappers.append((f"bizniz-gate-aud-{aud}", _mapper_json(
            f"bizniz-gate-aud-{aud}", "oidc-audience-mapper",
            {"included.client.audience": aud, "access.token.claim": "true"})))
    if roles_claim:
        mappers.append((f"bizniz-gate-roles-{roles_claim}", _mapper_json(
            f"bizniz-gate-roles-{roles_claim}", "oidc-usermodel-realm-role-mapper",
            {"claim.name": roles_claim, "jsonType.label": "String", "multivalued": "true",
             "access.token.claim": "true", "id.token.claim": "false"})))

    mapper_block = "\n".join(
        f"""cat <<'MAPPER_{i}' | "$KC" create clients/$ID/protocol-mappers/models """
        f"""-r {realm.realm} -f - >/dev/null 2>&1 || true
{body}
MAPPER_{i}"""
        for i, (_name, body) in enumerate(mappers))
    script = f"""
KC=/opt/keycloak/bin/kcadm.sh
ID=$("$KC" get clients -r {realm.realm} -q clientId={GATE_CLIENT_ID} --fields id --format csv --noquotes 2>/dev/null | head -1)
if [ -z "$ID" ]; then
  "$KC" create clients -r {realm.realm} \
    -s clientId={GATE_CLIENT_ID} -s enabled=true -s publicClient=false \
    -s serviceAccountsEnabled=true -s standardFlowEnabled=false \
    -s directAccessGrantsEnabled=false \
    -s 'description=Service account used by bizniz gates to probe authenticated routes' \
    >/dev/null
  ID=$("$KC" get clients -r {realm.realm} -q clientId={GATE_CLIENT_ID} --fields id --format csv --noquotes | head -1)
fi
for ROLE in {quoted_roles}; do
  "$KC" add-roles -r {realm.realm} --uusername service-account-{GATE_CLIENT_ID} --rolename "$ROLE" \
    >/dev/null 2>&1 || true
done
{mapper_block}
echo "$ID"
"""
    code, out = _kcadm(realm, script)
    if code != 0 or not out.strip():
        raise TokenError(f"could not create the gate client in realm '{realm.realm}': "
                         f"{out.strip()[:200]}")
    extras = []
    if audiences:
        extras.append(f"aud {', '.join(audiences)}")
    if roles_claim:
        extras.append(f"roles in `{roles_claim}`")
    log(f"keycloak: {GATE_CLIENT_ID} ready with roles {', '.join(roles) or '(none)'}"
        + (f" ({'; '.join(extras)})" if extras else ""))


def _client_secret(realm: Realm) -> str:
    """Read the gate client's secret — inside the container, printed only to this pipe."""
    script = f"""
KC=/opt/keycloak/bin/kcadm.sh
ID=$("$KC" get clients -r {realm.realm} -q clientId={GATE_CLIENT_ID} --fields id --format csv --noquotes | head -1)
"$KC" get clients/$ID/client-secret -r {realm.realm} --fields value --format csv --noquotes
"""
    code, out = _kcadm(realm, script)
    secret = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or not secret:
        raise TokenError("could not read the gate client's secret")
    return secret


def mint_token(realm: Realm, roles: list[str], audiences: Optional[list[str]] = None,
               roles_claim: str = "", log: Log = lambda _m: None) -> str:
    """A bearer token carrying `roles` and `audiences`, issued by the host's provider.

    Minted from *inside* the shared network when possible. Keycloak stamps `iss` with
    the hostname it was reached on, and an app on the network is configured for the
    network hostname — so a token fetched through the published port carries the wrong
    issuer and is refused, however valid it otherwise is.
    """
    ensure_gate_client(realm, roles, audiences, roles_claim, log=log)
    secret = _client_secret(realm)
    form = {"grant_type": "client_credentials", "client_id": GATE_CLIENT_ID,
            "client_secret": secret}

    if realm.network_url and realm.network:
        token = _mint_on_network(realm, form, log=log)
        if token:
            return token
        log("keycloak: no in-network client available; falling back to the published port")

    try:
        resp = httpx.post(realm.token_url, timeout=15, data=form)
    except httpx.HTTPError as exc:
        raise TokenError(f"token endpoint {realm.token_url} unreachable: {exc}")
    if resp.status_code != 200:
        raise TokenError(f"token endpoint returned {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise TokenError("token response carried no access_token")
    log(f"keycloak: minted a token from {realm.token_url}")
    return token


def _mint_on_network(realm: Realm, form: dict, log: Log = lambda _m: None) -> Optional[str]:
    """Ask for the token from a container on the host's network, so `iss` matches.

    Uses whichever running container has curl or python3 — the request only needs to
    originate on the network, not from any particular service.
    """
    code, out = _run_host(["docker", "ps", "--filter", f"network={realm.network}",
                           "--format", "{{.Names}}"])
    if code != 0:
        return None
    body = "&".join(f"{k}={v}" for k, v in form.items())
    for name in [n for n in out.split() if n]:
        script = (
            f'if command -v curl >/dev/null 2>&1; then '
            f'  curl -s -X POST {shlex.quote(realm.network_url)} '
            f'    -H "Content-Type: application/x-www-form-urlencoded" -d {shlex.quote(body)}; '
            f'elif command -v python3 >/dev/null 2>&1; then '
            f'  python3 -c "import urllib.request as u;'
            f'print(u.urlopen(u.Request({realm.network_url!r}, data={body!r}.encode())).read().decode())"; '
            f'else exit 3; fi')
        proc = subprocess.run(["docker", "exec", name, "sh", "-c", script],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout.strip().startswith("{"):
            continue
        try:
            token = json.loads(proc.stdout).get("access_token")
        except ValueError:
            continue
        if token:
            log(f"keycloak: minted a token on {realm.network} (via {name}), "
                f"so `iss` matches what services expect")
            return token
    return None


def _run_host(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def token_roles(token: str) -> list[str]:
    """The realm roles a token carries, read from its payload without verifying it.

    The gate only needs to report what it is presenting; the app is what verifies.
    """
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return []
    roles = set(claims.get("roles") or [])
    roles |= set((claims.get("realm_access") or {}).get("roles") or [])
    return sorted(roles)
