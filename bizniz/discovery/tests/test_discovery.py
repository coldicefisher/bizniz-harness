"""Discovery reads a host's conventions, and is honest about what it checked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bizniz.discovery import discover, render, write_profile
from bizniz.discovery.collect import collect
from bizniz.discovery.types import HostProfile, absent, asserted, verified

DEV_COMPOSE = """
include:
  - path: ../../chatty/infra/compose.dev.yml
  - path: ../../worker/infra/compose.dev.yml
services:
  proxy:
    image: nginx:1.27
    ports: ["48088:80"]
    volumes:
      - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
      - ../../chatty/infra/nginx/dev:/etc/nginx/hosted-apps/chatty:ro
  keycloak:
    image: quay.io/keycloak/keycloak:26.0
  api:
    image: host-api:dev
    environment:
      APP_KEYCLOAK_REALM_URL: http://keycloak:8080/realms/hostrealm
networks:
  hostnet:
    name: host-dev-net
    external: true
"""

APP_COMPOSE = """
services:
  chatty-api:
    image: chatty-api:dev
  chatty-web:
    image: node:22
"""

NGINX = """
location = /chatty { return 301 /chatty/; }
location /chatty/api/ { proxy_pass http://chatty-api:8010; }
location /chatty/ { proxy_pass http://chatty-web:4300; }
"""

STYLES = """
:root {
  --brand-primary: #002d72;
  --brand-accent: #f1c400;
  --surface: #ffffff;
}
"""

AUTH_PY = '''
"""Bearer verification against the host realm."""
ALGORITHMS = ["RS256"]
jwks_url = "..."
'''


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A miniature host repo with one proxied app and one internal app."""
    (tmp_path / "infra/dev").mkdir(parents=True)
    (tmp_path / "infra/dev/docker-compose.yml").write_text(DEV_COMPOSE)
    (tmp_path / "infra/prod").mkdir(parents=True)
    (tmp_path / "infra/prod/docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "infra/ci").mkdir(parents=True)
    (tmp_path / "infra/ci/docker-bake.hcl").write_text(
        'target "host-api" {\n}\ntarget "chatty-api" {\n}\n')
    (tmp_path / "infra/ci/smoke.sh").write_text("#!/bin/sh\n")
    (tmp_path / "infra/ci/wait_healthy.sh").write_text("#!/bin/sh\n")

    for app, compose in (("chatty", APP_COMPOSE), ("worker", "services:\n  worker: {}\n")):
        (tmp_path / app / "infra").mkdir(parents=True)
        (tmp_path / app / "infra/compose.dev.yml").write_text(compose)
    (tmp_path / "chatty/infra/nginx/dev").mkdir(parents=True)
    (tmp_path / "chatty/infra/nginx/dev/chatty.conf").write_text(NGINX)
    (tmp_path / "chatty/api/app").mkdir(parents=True)
    (tmp_path / "chatty/api/app/auth.py").write_text(AUTH_PY)
    (tmp_path / "chatty/infra/keycloak").mkdir(parents=True)
    (tmp_path / "chatty/infra/keycloak/register-roles.sh").write_text(
        'create roles -r "$REALM" -s name=chatty_user\ncreate roles -s name=system_admin\n')

    (tmp_path / "source-code/frontend/src").mkdir(parents=True)
    (tmp_path / "source-code/frontend/package.json").write_text(
        json.dumps({"dependencies": {"@angular/core": "^20.0.0"}}))
    (tmp_path / "source-code/frontend/src/styles.scss").write_text(STYLES)
    return tmp_path


def profile_of(host: Path) -> HostProfile:
    return discover(host, host="TestHost", verify=False)


def test_finds_the_stack_and_its_shared_network(host):
    p = profile_of(host)
    assert p.stack.compose_dev.value == "infra/dev/docker-compose.yml"
    assert p.stack.network.value == "host-dev-net"
    assert "proxy" in p.stack.services.value


def test_finds_where_hosted_apps_attach(host):
    p = profile_of(host)
    assert p.stack.includes.value == [
        "../../chatty/infra/compose.dev.yml", "../../worker/infra/compose.dev.yml"]


def test_finds_the_proxy_and_its_snippet_mount(host):
    p = profile_of(host)
    assert p.proxy.service.value == "proxy"
    assert p.proxy.dev_base_url.value == "http://localhost:48088"
    assert p.proxy.locations_dir.value == "/etc/nginx/hosted-apps/chatty"


def test_derives_the_auth_contract_from_configuration(host):
    p = profile_of(host)
    assert p.identity.provider.value == "keycloak"
    assert p.identity.realm_url.value == "http://keycloak:8080/realms/hostrealm"
    assert p.identity.jwks_url.value.endswith("/protocol/openid-connect/certs")
    assert p.identity.reference_impl.value == "chatty/api/app/auth.py"
    assert "chatty_user" in p.identity.roles.value


def test_takes_the_path_prefix_from_the_shortest_location(host):
    """`/chatty/` is the app's prefix; `/chatty/api/` is a route inside it."""
    chatty = next(a for a in profile_of(host).hosted_apps if a.name == "chatty")
    assert chatty.path_prefix.value == "/chatty/"


def test_an_app_with_no_route_is_internal_not_broken(host):
    worker = next(a for a in profile_of(host).hosted_apps if a.name == "worker")
    assert worker.path_prefix.evidence == "absent"
    assert "internal" in worker.path_prefix.how


def test_harvests_design_tokens_so_a_new_app_matches(host):
    p = profile_of(host)
    assert p.frontend.framework.value == "angular"
    assert p.frontend.design_tokens.value["--brand-primary"] == "#002d72"


def test_points_at_the_hosts_own_gates(host):
    gates = profile_of(host).build.gates.value
    assert sorted(gates) == ["infra/ci/smoke.sh", "infra/ci/wait_healthy.sh"]


def test_without_verification_nothing_claims_to_be_verified(host):
    p = profile_of(host)
    assert all(c.evidence != "verified" for _n, c in p.claims())
    ok, total = p.coverage()
    assert ok == 0 and total > 0


def test_coverage_counts_only_claims_that_found_something():
    p = HostProfile(host="H", root="/tmp", generated_at="2026-09-22")
    p.stack.network = verified("net", "docker network inspect net")
    p.stack.services = asserted(["a"], "compose.yml")
    p.stack.running = absent("docker not available")
    assert p.coverage() == (1, 2)


def test_missing_host_directory_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover(tmp_path / "nope", verify=False)


def test_render_marks_each_claim_with_how_it_was_established(host):
    p = profile_of(host)
    p.stack.network = verified("host-dev-net", "docker network inspect host-dev-net")
    text = render(p)
    assert "| ✓ | **shared network**" in text
    assert "| · | **dev compose**" in text
    assert "`✓` verified" in text          # the legend, so a reader knows what the marks mean


def test_write_profile_emits_both_forms(host, tmp_path):
    md, js = write_profile(profile_of(host), tmp_path / "out")
    assert md.exists() and js.exists()
    assert json.loads(js.read_text())["host"] == "TestHost"
    assert md.read_text().startswith("# TestHost — host profile")


def test_collect_survives_a_repo_that_is_nothing_like_a_host(tmp_path):
    """Pointed at an unrelated directory it reports absence, it does not crash."""
    (tmp_path / "README.md").write_text("hello")
    p = collect(tmp_path, host="Empty", generated_at="2026-09-22")
    assert p.stack.compose_dev.evidence == "absent"
    assert p.identity.provider.evidence == "absent"
    assert p.hosted_apps == []
