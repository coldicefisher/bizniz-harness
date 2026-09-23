"""Keycloak: the identity provider the Press actually runs.

Emits the compose service and a realm import, so a generated stack comes up with a realm,
a confidential client, roles and a seeded admin already in place — no console clicking, no
first-run wizard.

The realm import is deliberately small. A full realm export is thousands of lines, goes
stale the moment anyone touches the console, and hides what matters in noise. This imports
the few objects an application actually needs and lets Keycloak default the rest.

Three things are configured that look optional and are not. Each of them makes Keycloak
issue a token the generated backend then refuses:

* an **audience mapper**, because a token otherwise carries ``aud: account`` and an API
  checking its own audience rejects every request;
* a **realm-roles mapper** writing a flat ``roles`` claim, because Keycloak puts roles in
  ``realm_access.roles`` and most backends look for ``roles``;
* **direct access grants**, because the generated `/auth/login` exchanges a username and
  password for tokens, which is that grant by name.

Credentials are generated per project and read back from `.env` on re-provision, so a
second run does not desynchronise the file from what Keycloak imported.
"""
from __future__ import annotations

import json
import re
import secrets
import string
from pathlib import Path

from bizniz.provisioner.templates.base import InfraTemplate, TemplateContext, TemplateOutput

# Pinned: a realm import written for one major version can fail to import on another.
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.0"


def _existing_env_value(project_root: Path, key: str) -> "str | None":
    """Read a value already written to `.env`, so re-provisioning is idempotent.

    Minting a fresh secret on every run leaves `.env` disagreeing with the realm Keycloak
    imported the first time, and every token exchange fails with `invalid_client`.
    """
    env_path = Path(project_root) / ".env"
    if not env_path.is_file():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    except OSError:
        return None
    return None


def _password() -> str:
    """A password that satisfies the usual policies: upper, lower, digit, length."""
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    return (f"Bz{secrets.choice(string.ascii_uppercase)}"
            f"{secrets.choice(string.digits)}-{body}")


class KeycloakTemplate(InfraTemplate):

    DEFAULT_CONTAINER_PORT = 8080

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        from bizniz.architect.types import host_port_for

        host_port = host_port_for(ctx.service) or self.DEFAULT_CONTAINER_PORT
        slug = ctx.project_slug
        own_name = ctx.service.name
        root = ctx.project_root

        # The architect names the database service whatever it likes; find it rather than
        # assuming "postgres", so this template works under any naming convention.
        pg = ctx.find_by_framework("postgres")
        pg_name = pg.name if pg is not None else "postgres"

        realm = re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-") or "app"
        client_id = f"{realm}-api"
        spa_client_id = f"{realm}-spa"

        admin_password = (_existing_env_value(root, "KEYCLOAK_ADMIN_PASSWORD")
                          or _password())
        client_secret = (_existing_env_value(root, "KEYCLOAK_CLIENT_SECRET")
                         or secrets.token_urlsafe(32))
        seed_password = (_existing_env_value(root, "KEYCLOAK_SEED_PASSWORD")
                         or _password())
        db_password = (_existing_env_value(root, "POSTGRES_PASSWORD")
                       or _existing_env_value(root, "DB_PASSWORD") or "app_dev")

        # `.example.com` is reserved for exactly this (RFC 2606) and passes the email
        # validation generated backends apply; `.local` and `.example` do not, and seed a
        # user the API then refuses to accept.
        email_slug = re.sub(r"[^a-zA-Z0-9-]", "-", slug).strip("-") or "bizniz"
        seed_email = f"admin@{email_slug}.example.com"

        internal_url = f"http://{own_name}:{self.DEFAULT_CONTAINER_PORT}"
        public_url = f"http://localhost:{host_port}"

        compose_service = {
            "image": KEYCLOAK_IMAGE,
            "container_name": f"{slug}-{own_name}",
            # start-dev keeps the developer experience simple; a production compose
            # swaps this for `start` with a hostname and TLS configured.
            "command": ["start-dev", "--import-realm"],
            "environment": {
                "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
                "KC_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
                "KC_DB": "postgres",
                "KC_DB_URL": f"jdbc:postgresql://{pg_name}:5432/keycloak",
                "KC_DB_USERNAME": "${POSTGRES_USER:-app}",
                "KC_DB_PASSWORD": db_password,
                "KC_HEALTH_ENABLED": "true",
                # Tokens are stamped with the hostname the caller used. Telling Keycloak
                # its public address keeps browser-issued tokens consistent, and the
                # backend accepts both anyway (KEYCLOAK_ISSUERS).
                "KC_HOSTNAME": public_url,
                "KC_HOSTNAME_STRICT": "false",
            },
            "ports": [f"{host_port}:{self.DEFAULT_CONTAINER_PORT}"],
            "volumes": [f"./infra/development/{own_name}/realm:/opt/keycloak/data/import:ro"],
            "depends_on": [pg_name],
            "networks": ["app-network"],
            "restart": "unless-stopped",
            "healthcheck": {
                # The management port serves health; curl is absent from the image, so
                # this uses the shell's own /dev/tcp rather than adding a dependency.
                "test": ["CMD-SHELL",
                         "exec 3<>/dev/tcp/127.0.0.1/9000 && echo -e "
                         "'GET /health/ready HTTP/1.1\\r\\nHost: localhost\\r\\n\\r\\n' >&3 "
                         "&& head -1 <&3 | grep -q 200"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 30,
                "start_period": "30s",
            },
        }

        realm_json = {
            "realm": realm,
            "enabled": True,
            "registrationAllowed": True,
            "registrationEmailAsUsername": True,
            "loginWithEmailAllowed": True,
            "verifyEmail": False,          # no SMTP in development; enable with one
            "resetPasswordAllowed": True,
            "accessTokenLifespan": 1800,
            "roles": {
                "realm": [
                    {"name": "admin", "description": "Full access"},
                    {"name": "user", "description": "A signed-in user"},
                ]
            },
            "clients": [
                {
                    "clientId": client_id,
                    "name": f"{slug} API",
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": False,
                    "secret": client_secret,
                    "serviceAccountsEnabled": True,      # for the admin API calls
                    "directAccessGrantsEnabled": True,   # for /auth/login
                    "standardFlowEnabled": True,         # for the authorization code flow
                    "redirectUris": ["*"],
                    "webOrigins": ["*"],
                    "protocolMappers": [
                        {
                            "name": "audience",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.client.audience": client_id,
                                "access.token.claim": "true",
                            },
                        },
                        {
                            "name": "realm-roles-flat",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-usermodel-realm-role-mapper",
                            "config": {
                                "claim.name": "roles",
                                "jsonType.label": "String",
                                "multivalued": "true",
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        },
                    ],
                },
                {
                    # The browser client: public, no secret to leak into a bundle.
                    "clientId": spa_client_id,
                    "name": f"{slug} SPA",
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": True,
                    "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": False,
                    "redirectUris": ["*"],
                    "webOrigins": ["*"],
                    "protocolMappers": [
                        {
                            "name": "audience-api",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.client.audience": client_id,
                                "access.token.claim": "true",
                            },
                        },
                        {
                            "name": "realm-roles-flat",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-usermodel-realm-role-mapper",
                            "config": {
                                "claim.name": "roles",
                                "jsonType.label": "String",
                                "multivalued": "true",
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        },
                    ],
                },
            ],
            "users": [
                {
                    "username": seed_email,
                    "email": seed_email,
                    "emailVerified": True,
                    "enabled": True,
                    "firstName": "Admin",
                    "lastName": "User",
                    "credentials": [
                        {"type": "password", "value": seed_password, "temporary": False}
                    ],
                    "realmRoles": ["admin", "user"],
                },
                {
                    # The service account's roles: without manage-users, every admin-API
                    # call the backend makes (register, password-reset email) answers 403.
                    "username": f"service-account-{client_id}",
                    "enabled": True,
                    "serviceAccountClientId": client_id,
                    "clientRoles": {"realm-management": ["manage-users", "view-users"]},
                },
            ],
        }

        env_vars = {
            "KEYCLOAK_URL": internal_url,
            "KEYCLOAK_BASE_URL": internal_url,
            "KEYCLOAK_PUBLIC_URL": public_url,
            "KEYCLOAK_EXTERNAL_URL": public_url,
            "KEYCLOAK_REALM": realm,
            "KEYCLOAK_CLIENT_ID": client_id,
            "KEYCLOAK_SPA_CLIENT_ID": spa_client_id,
            "KEYCLOAK_CLIENT_SECRET": client_secret,
            "KEYCLOAK_ADMIN_PASSWORD": admin_password,
            "KEYCLOAK_SEED_EMAIL": seed_email,
            "KEYCLOAK_SEED_PASSWORD": seed_password,
            # Both, always: the same realm calls itself by whichever host reached it, and
            # an app pinning one rejects tokens minted through the other.
            "KEYCLOAK_ISSUERS": (f"{internal_url}/realms/{realm},"
                                 f"{public_url}/realms/{realm}"),
            "KEYCLOAK_AUDIENCES": client_id,
        }

        return TemplateOutput(
            compose_service=compose_service,
            compose_networks=["app-network"],
            infra_files={
                f"{own_name}/realm/{realm}-realm.json":
                    json.dumps(realm_json, indent=2) + "\n",
            },
            env_vars=env_vars,
            # Host-perspective only: `localhost` inside a container is the container.
            host_env_vars={
                "KEYCLOAK_HOST_URL": public_url,
                "KEYCLOAK_HOST_REALM_URL": f"{public_url}/realms/{realm}",
            },
            depends_on_services=[pg_name],
        )
