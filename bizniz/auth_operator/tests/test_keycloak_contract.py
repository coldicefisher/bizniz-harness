"""The Keycloak auth contract: what generated code is told about the realm."""
from __future__ import annotations

import pytest

from bizniz.auth_operator.contract_renderer import render_auth_contract
from bizniz.auth_operator.manifest import (
    AuthManifest, RoleManifest, SigningKeyInfo, UserManifest,
)


def manifest(**kw) -> AuthManifest:
    base = dict(
        provider="keycloak",
        fa_url="http://keycloak:8080/realms/widgets",
        primary_app_id="widgets-api",
        tenant_id="widgets",
        issuer="http://keycloak:8080/realms/widgets",
        signing_key=SigningKeyInfo(key_id="abc", algorithm="RS256"),
        roles=[RoleManifest(name="admin", description="Full access")],
        users=[UserManifest(email="a@x.example.com", user_id="1", password="pw",
                            roles=["admin"], login_verified=True)],
    )
    base.update(kw)
    return AuthManifest(**base)


def test_a_keycloak_manifest_renders_a_keycloak_contract():
    text = render_auth_contract(manifest())
    assert "KeycloakOperator" in text
    assert "FusionAuth" not in text.replace("unlike FusionAuth's raw API key", "")


def test_a_fusionauth_manifest_still_renders_the_old_contract():
    """Projects cut before the cutover keep their contract."""
    text = render_auth_contract(manifest(provider="fusionauth"))
    assert "FusionAuth" in text


def test_the_contract_names_the_three_traps():
    text = render_auth_contract(manifest())
    assert "issuer follows the hostname" in text
    assert "audience is `account`" in text
    assert "realm_access.roles" in text


def test_it_distinguishes_the_two_apis_and_their_credentials():
    text = render_auth_contract(manifest())
    assert "grant_type=password" in text            # the user's own credentials
    assert "grant_type=client_credentials" in text  # this service's
    assert "Authorization: Bearer <service-account token>" in text


def test_it_says_role_assignment_needs_full_representations():
    """Posting `[{"name": "admin"}]` silently assigns nothing — worth stating."""
    text = render_auth_contract(manifest())
    assert "full role representations" in text


def test_it_forbids_handling_a_password_on_reset():
    text = render_auth_contract(manifest())
    assert "execute-actions-email" in text
    assert "Never accept" in text


def test_an_unverified_user_is_called_out_rather_than_listed_quietly():
    text = render_auth_contract(manifest(
        users=[UserManifest(email="a@x.example.com", user_id="1", password="pw",
                            roles=["admin"], login_verified=False)]))
    assert "**no**" in text


def test_the_contract_carries_the_live_signing_key():
    text = render_auth_contract(manifest(
        signing_key=SigningKeyInfo(key_id="key-9", algorithm="RS512")))
    assert "RS512" in text and "key-9" in text
