"""
FusionAuth infrastructure template.

FusionAuth is the project's default OAuth/OIDC provider. The template
emits:

  - docker-compose service entry (depends on postgres)
  - kickstart.json that pre-configures a tenant, application, roles, an
    initial admin user, and OAuth redirect URLs derived from the project's
    frontend service
  - .env entries for FusionAuth admin password, API key, app id, and
    issuer URL the application services use

The kickstart file is FusionAuth's bootstrapping format — it executes
the listed REST requests once on first start, creating the realm
configuration without any manual UI clicks. Kickstart docs:
https://fusionauth.io/docs/v1/tech/installation-guide/kickstart
"""
from __future__ import annotations

import json
import re
import secrets
import string
import uuid

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


#: Digest-pinned rather than ``:latest``.
#:
#: ``:latest`` means an image can change under a project between two runs
#: of the same commit, which is the one thing a reproducible build cannot
#: tolerate -- and FusionAuth carries a database schema, so a silent major
#: bump is a migration nobody asked for. This digest is a known-good
#: build that has run every stack in this repo.
#:
#: To update: pull the new tag, `docker inspect --format='{{index
#: .RepoDigests 0}}' fusionauth/fusionauth-app:<tag>`, paste it here.
FUSIONAUTH_IMAGE = (
    "fusionauth/fusionauth-app@sha256:"
    "fe44e9aba57b5343ef8645a346ddcc85b870e2f7b66c1e88c28c6f7c5641d517"
)


def _existing_env_value(project_root: Path, key: str) -> "str | None":
    """A value already written to this project's .env, or None.

    Generated secrets must be READ BACK before they are minted again, and
    the reason is not tidiness. FusionAuth applies kickstart.json exactly
    once, on the first boot of an empty volume, and stores a password
    HASH. `.env` is what the generated application authenticates with.
    Regenerating on a re-provision therefore desynchronises the two, and
    `_merge_env` pinning the old value in `.env` does not save you: the
    next time the volume is recreated, kickstart seeds the NEW password
    while `.env` still carries the OLD one, and the only symptom is a 401
    on the one account every project logs in with.

    Globbed rather than hardcoded to ``infra/development``: the infra
    directory name is configurable for adopted workspaces.
    """
    infra = project_root / "infra"
    if not infra.is_dir():
        return None
    for env_path in sorted(infra.glob("*/.env")):
        try:
            text = env_path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key and v.strip():
                return v.strip()
    return None


def _generate_admin_password() -> str:
    """A password that satisfies FusionAuth's default policy.

    The policy wants length plus mixed character classes, so the classes
    are placed explicitly rather than hoped for: a rejected password does
    not fail loudly at provision time, it fails later as a kickstart error
    buried in the FusionAuth container log.

    No ``!`` or other shell-active characters. This value lands in a
    ``.env`` read by docker compose, where ``$`` would interpolate and a
    quote would truncate.
    """
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(20))
    return (f"Bz{secrets.choice(string.ascii_uppercase)}"
            f"{secrets.choice(string.ascii_lowercase)}"
            f"{secrets.choice(string.digits)}-{body}")


class FusionAuthTemplate(InfraTemplate):

    DEFAULT_CONTAINER_PORT = 9011

    # Stable UUIDs so kickstart is idempotent across re-runs of a project.
    # Customize per-project by deriving from project_slug if you want.
    APPLICATION_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
    ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
    # Stable RSA signing key ID. Kickstart generates an RS256 keypair
    # at this ID and binds it as the tenant's accessTokenSigningKey
    # (see comment on the PATCH /api/tenant request below). Without
    # this, FusionAuth defaults to HS256 → JWKS exposes no public
    # keys → backend's RS256 + JWKS validation fails on every JWT.
    ACCESS_TOKEN_KEY_ID = "12345678-1234-1234-1234-123456789012"
    # FusionAuth ships with this default tenant ID built-in. We do NOT
    # set this as a kickstart variable — kickstart treats `defaultTenantId`
    # specially (a "rename the default tenant" trigger), and renaming it
    # to itself fails with a tenants_pkey unique-constraint violation.
    # Instead, reference the UUID literally in PATCH URLs.
    DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"
    # FusionAuth ships a built-in "FusionAuth" application with this
    # ID — it's the system admin app. To bypass the first-run setup
    # wizard (``/admin/setup-wizard``), the kickstart must register
    # the bootstrap admin against this app with role=admin. Without
    # the registration, FA shows the setup wizard on every fresh
    # boot regardless of how many app users exist — surfaced
    # 2026-05-15 when ``localhost:9024/`` redirected to the wizard
    # despite a successful kickstart that created the project app
    # admin.
    FUSIONAUTH_SYSTEM_APP_ID = "3c219e58-ed0e-4b18-ad48-f4f92793ae32"

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        from bizniz.architect.types import host_port_for
        host_port = host_port_for(ctx.service) or self.DEFAULT_CONTAINER_PORT
        slug = ctx.project_slug
        # The architect can name the postgres service anything ("db",
        # "postgres", "data") — look up the actual name and use it for
        # depends_on + the JDBC hostname so this template works
        # regardless of naming convention.
        pg = ctx.find_by_framework("postgres")
        pg_name = pg.name if pg is not None else "postgres"
        own_name = ctx.service.name
        # Dev defaults. Credentials below are generated per project; the
        # rest of these values are structural and follow the architecture.
        # Email validator in FA rejects underscores in the domain part
        # (RFC 5321 requires the domain be a valid hostname; underscores
        # aren't legal in hostnames). Slugs like ``recipe_box`` produced
        # an invalid email and kickstart failed with
        # ``[notEmail]user.email`` until 2026-05-15. Sanitize to a
        # hostname-safe form (letters, digits, hyphens) before building
        # the email.
        email_safe_slug = re.sub(r"[^a-zA-Z0-9-]", "-", slug).strip("-") or "bizniz"
        # ``.example`` -> the seeded admin CANNOT LOG IN.
        #
        # `.local` is a special-use TLD (RFC 6762, mDNS). The FastAPI
        # skeleton validates login bodies with pydantic `EmailStr`, whose
        # email-validator backend rejects special-use names outright:
        #   "The part after the @-sign is a special-use or reserved name"
        # So FusionAuth seeds a user the generated API then refuses to
        # accept — a 422 on the one account every new project logs in with.
        #
        # `example.com` is reserved for exactly this (RFC 2606) and passes
        # validation, which is why the shipped muvnit project uses it.
        admin_email = f"admin@{email_safe_slug}.example.com"
        # Generated per project, never shared.
        #
        # These were a hardcoded password and a hardcoded API key GUID,
        # identical in every project ever cut from this factory. The API
        # key is the worse half: it is FusionAuth's ADMIN key, it grants
        # full tenant control, and one value in a source file meant every
        # stack on a host answered to the same credential -- including any
        # stack built by anyone who had read this file.
        #
        # Read-back before mint, so a re-provision does not desynchronise
        # `.env` from the hash FusionAuth stored at kickstart. See
        # `_existing_env_value`.
        root = ctx.project_root
        admin_password = (_existing_env_value(root, "FUSIONAUTH_ADMIN_PASSWORD")
                          or _generate_admin_password())
        api_key = (_existing_env_value(root, "FUSIONAUTH_API_KEY")
                   or str(uuid.uuid4()))
        issuer = f"http://{own_name}:{self.DEFAULT_CONTAINER_PORT}"

        kickstart = {
            "variables": {
                "applicationId": self.APPLICATION_ID,
                "adminUserId": self.ADMIN_USER_ID,
                "adminEmail": admin_email,
                "adminPassword": admin_password,
                "apiKey": api_key,
                "appName": slug,
                "accessTokenKeyId": self.ACCESS_TOKEN_KEY_ID,
                "systemAppId": self.FUSIONAUTH_SYSTEM_APP_ID,
            },
            "apiKeys": [
                {
                    "key": "#{apiKey}",
                    "description": "Bizniz bootstrap key",
                }
            ],
            "requests": [
                # Generate an RSA-2048 keypair for the tenant's access
                # tokens. Without this, FA defaults to an HMAC SHA-256
                # key, JWKS endpoint exposes no public keys, and the
                # skeleton's RS256 + JWKS validation fails on every JWT
                # ("Signature verification failed"). RS256 is the
                # standard for JWKS-based service-to-service token
                # validation; HS256 would require shared-secret
                # distribution.
                {
                    "method": "POST",
                    "url": "/api/key/generate/#{accessTokenKeyId}",
                    "body": {
                        "key": {
                            "algorithm": "RS256",
                            "name": "Access Token Signing Key",
                            "length": 2048,
                        }
                    },
                },
                # NOTE: we deliberately do NOT PATCH the default
                # tenant here. FusionAuth's PATCH validator on a
                # freshly-bootstrapped default tenant is broken in
                # both directions:
                #   - body without name → 400 [blank]tenant.name
                #   - body with name="Default" → 400 [duplicate]tenant.name
                # The application-level jwtConfiguration below
                # overrides the tenant's defaults and IS accepted on
                # PATCH/POST. JWT signing config goes there.
                #
                # Tenant.issuer would be nice to set (so the JWT's
                # ``iss`` claim matches the placeholder value we
                # write to ``FUSIONAUTH_ISSUER`` in .env), but the
                # same validator quirk blocks it on a fresh tenant.
                # Instead, the FA agent reconciles this after a
                # successful smoke test: decodes the JWT body, reads
                # the actual ``iss``, and rewrites ``FUSIONAUTH_ISSUER``
                # in .env to match (see fusionauth_agent.py
                # _reconcile_issuer_in_env). The skeleton's auth.py
                # then validates against the correct issuer.
                # Roles for the application
                {
                    "method": "POST",
                    "url": "/api/application/#{applicationId}",
                    "body": {
                        "application": {
                            "name": "#{appName}",
                            "roles": [
                                {"name": "admin", "isSuperRole": True},
                                {"name": "user", "isDefault": True},
                            ],
                            "oauthConfiguration": {
                                "authorizedRedirectURLs": [
                                    "http://localhost:5173/auth/callback",
                                    "http://localhost:4200/auth/callback",
                                ],
                                "logoutURL": "http://localhost:5173/logout",
                                "requireRegistration": True,
                                "generateRefreshTokens": True,
                                "enabledGrants": [
                                    "authorization_code",
                                    "refresh_token",
                                ],
                            },
                            "jwtConfiguration": {
                                "enabled": True,
                                "timeToLiveInSeconds": 3600,
                                "refreshTokenTimeToLiveInMinutes": 43200,
                                # Bind the RSA key here at the
                                # APPLICATION level (not tenant) —
                                # FA's tenant PATCH validator is
                                # broken on fresh tenants. Application
                                # JWT config overrides tenant defaults
                                # and the validator is happy here.
                                "accessTokenKeyId": "#{accessTokenKeyId}",
                                "idTokenKeyId": "#{accessTokenKeyId}",
                            },
                        }
                    },
                },
                # Admin user with admin role in the project's application.
                {
                    "method": "POST",
                    "url": "/api/user/registration/#{adminUserId}",
                    "body": {
                        "user": {
                            "email": "#{adminEmail}",
                            "password": "#{adminPassword}",
                        },
                        "registration": {
                            "applicationId": "#{applicationId}",
                            "roles": ["admin"],
                        },
                    },
                },
                # Second registration: bind the same admin to the
                # FusionAuth built-in system application so FA
                # recognizes them as a system admin and skips the
                # setup-wizard redirect on /admin/. Without this,
                # ``localhost:9024/`` 302s to ``/admin/setup-wizard``
                # on every fresh boot even though kickstart created
                # the project's admin successfully — FA's wizard
                # gate is "any user registered against
                # FUSIONAUTH_SYSTEM_APP_ID", not "any user with an
                # admin role anywhere".
                {
                    "method": "POST",
                    "url": "/api/user/registration/#{adminUserId}",
                    "body": {
                        "registration": {
                            "applicationId": "#{systemAppId}",
                            "roles": ["admin"],
                        },
                    },
                },
            ],
        }

        _bind = getattr(ctx.service, "bind_host", None)
        compose_service = {
            "image": FUSIONAUTH_IMAGE,
            "depends_on": {
                pg_name: {"condition": "service_healthy"},
            },
            "environment": {
                "DATABASE_URL": f"jdbc:postgresql://{pg_name}:5432/fusionauth",
                "DATABASE_ROOT_USERNAME": "${POSTGRES_USER}",
                "DATABASE_ROOT_PASSWORD": "${POSTGRES_PASSWORD}",
                "DATABASE_USERNAME": "${POSTGRES_USER}",
                "DATABASE_PASSWORD": "${POSTGRES_PASSWORD}",
                "FUSIONAUTH_APP_RUNTIME_MODE": "development",
                "FUSIONAUTH_APP_KICKSTART_FILE":
                    "/usr/local/fusionauth/kickstart/kickstart.json",
            },
            "ports": [f"{_bind}:{host_port}:9011" if _bind
                      else f"{host_port}:9011"],
            "volumes": [
                "./fusionauth/kickstart:/usr/local/fusionauth/kickstart:ro",
            ],
            "networks": ["app-network"],
        }

        env_vars = {
            "FUSIONAUTH_URL": issuer,  # internal Docker URL for backend → FusionAuth
            "FUSIONAUTH_ADMIN_EMAIL": admin_email,
            "FUSIONAUTH_ADMIN_PASSWORD": admin_password,
            "FUSIONAUTH_API_KEY": api_key,
            "FUSIONAUTH_APPLICATION_ID": self.APPLICATION_ID,
            "FUSIONAUTH_ISSUER": issuer,
        }

        return TemplateOutput(
            compose_service=compose_service,
            compose_networks=["app-network"],
            infra_files={
                "fusionauth/kickstart/kickstart.json":
                    json.dumps(kickstart, indent=2) + "\n",
            },
            env_vars=env_vars,
            # Host-perspective URL for driver/smoke/debugger tooling.
            # NOT container-valid — localhost inside a container is
            # the container itself.
            host_env_vars={
                "FUSIONAUTH_HOST_URL": f"http://localhost:{host_port}",
            },
            depends_on_services=[pg_name],
        )
