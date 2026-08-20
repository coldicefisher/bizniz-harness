"""Workspace adoption: hooking a generated stack into an existing one.

Covers `ServiceDefinition.external` and the configurable infra directory.

The failure these guard against is quiet and expensive: declaring a
database the host already runs used to produce a SECOND database, with its
own empty volume, that the generated app then talked to instead of the real
one. Nothing errors; the data is just missing.

Half of these are regression checks that a project WITHOUT any external
service still renders byte-identically, because this module builds every
other bizniz project too.
"""
import yaml

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.compose_builder import build_compose


def svc(**kw):
    base = dict(service_type="backend", framework="fastapi", language="python",
                description="d", workspace_name="w")
    base.update(kw)
    return ServiceDefinition(**base)


def _arch(services):
    return SystemArchitecture(project_name="P", project_slug="p",
                              description="d", services=services)


PLAIN = _arch([
    svc(name="db", service_type="database", framework="postgres",
        language="sql", workspace_name=""),
    svc(name="api", workspace_name="api", depends_on=["db"]),
])

ADOPTING = _arch([
    svc(name="postgres", service_type="database", framework="postgres",
        language="sql", workspace_name="", external=True,
        external_network="bizniz-net", external_host="bizniz-postgres"),
    svc(name="api", workspace_name="api", depends_on=["postgres"]),
    svc(name="web", service_type="frontend", framework="angular",
        language="typescript", workspace_name="web", depends_on=["api"]),
])


# ── regression: projects with no external service are unchanged ─────────

def test_app_service_still_emitted():
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert "api" in y["services"]


def test_app_service_still_only_on_app_network():
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert y["services"]["api"]["networks"] == ["app-network"]


def test_dockerfile_path_still_defaults_to_development():
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert "infra/development/" in y["services"]["api"]["build"]["dockerfile"]


def test_no_external_network_block_when_nothing_adopted():
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert all(v is None for v in (y.get("networks") or {}).values())


def test_external_defaults_are_off():
    s = svc(name="x")
    assert s.external is False
    assert s.external_network is None
    assert s.external_host is None


# ── adoption ────────────────────────────────────────────────────────────

def test_adopted_service_gets_no_compose_block():
    """The whole point. A block here means a duplicate database."""
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p"))
    assert "postgres" not in y["services"]


def test_adopted_service_does_not_suppress_the_others():
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p"))
    assert "api" in y["services"] and "web" in y["services"]


def test_adopted_network_is_declared_external():
    """Compose must not create or tear down a network the host stack owns:
    `compose down` would take the host's containers offline with it."""
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p"))
    assert y["networks"]["bizniz-net"] == {"external": True}


def test_dependent_joins_the_adopted_network():
    """Without this the backend cannot resolve the host database by name,
    which is the entire reason for adopting it."""
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p"))
    assert "bizniz-net" in y["services"]["api"]["networks"]


def test_non_dependent_does_not_join_it():
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p"))
    assert "bizniz-net" not in y["services"]["web"]["networks"]


def test_own_network_still_created_normally():
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p"))
    assert y["networks"]["app-network"] is None


# ── configurable infra directory ────────────────────────────────────────

def test_dockerfile_path_follows_infra_dirname():
    y = yaml.safe_load(build_compose(ADOPTING, {}, "p", infra_dirname="dev"))
    assert "infra/dev/" in y["services"]["api"]["build"]["dockerfile"]


def test_project_dev_root_follows_infra_dirname(tmp_path):
    from bizniz.project.project import Project
    assert Project(tmp_path / "a", "a").dev_root.name == "development"
    assert Project(tmp_path / "b", "b", infra_dirname="dev").dev_root.name == "dev"


# ── adopted services must not leak into depends_on or leave templated
#    services stranded off the network ─────────────────────────────────

TEMPLATED = {
    "fusionauth": type("T", (), {
        "compose_service": {
            "image": "fusionauth/fusionauth-app:latest",
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "networks": ["app-network"],
        },
        "compose_volumes": [], "compose_networks": ["app-network"],
    })(),
}

ADOPT_TEMPLATED = _arch([
    svc(name="postgres", service_type="database", framework="postgres",
        language="sql", workspace_name="", external=True,
        external_network="bizniz-net", external_host="bizniz-postgres"),
    svc(name="fusionauth", service_type="auth", framework="fusionauth",
        language="java", workspace_name="", depends_on=["postgres"]),
    svc(name="api", workspace_name="api",
        depends_on=["postgres", "fusionauth"]),
])


def test_depends_on_drops_adopted_services():
    """Compose refuses to start on a dependency it cannot see, and an
    adopted service is by definition not in this file."""
    y = yaml.safe_load(build_compose(ADOPT_TEMPLATED, TEMPLATED, "p"))
    for name, block in y["services"].items():
        assert "postgres" not in (block.get("depends_on") or {}), name


def test_real_dependencies_survive():
    y = yaml.safe_load(build_compose(ADOPT_TEMPLATED, TEMPLATED, "p"))
    assert "fusionauth" in y["services"]["api"]["depends_on"]


def test_templated_service_joins_the_adopted_network():
    """fusionauth renders its own compose block and so bypasses the app
    path; without this its database hostname never resolves."""
    y = yaml.safe_load(build_compose(ADOPT_TEMPLATED, TEMPLATED, "p"))
    assert "bizniz-net" in y["services"]["fusionauth"]["networks"]


def test_depends_on_removed_entirely_when_only_adopted():
    """An empty depends_on mapping is not valid compose."""
    arch = _arch([
        svc(name="postgres", service_type="database", framework="postgres",
            language="sql", workspace_name="", external=True,
            external_network="bizniz-net"),
        svc(name="api", workspace_name="api", depends_on=["postgres"]),
    ])
    y = yaml.safe_load(build_compose(arch, {}, "p"))
    assert "depends_on" not in y["services"]["api"]


# ── image pinning and opt-in loopback binding ───────────────────────────

def test_fusionauth_image_is_digest_pinned():
    """`:latest` can change under a project between two runs of the same
    commit, and FusionAuth carries a database schema — a silent major bump
    is an unrequested migration."""
    from bizniz.provisioner.templates.fusionauth import FUSIONAUTH_IMAGE
    assert "@sha256:" in FUSIONAUTH_IMAGE
    assert ":latest" not in FUSIONAUTH_IMAGE


def test_ports_bind_all_interfaces_by_default():
    """MUST stay the default: an Expo app on a phone or emulator reaches
    the API over the LAN, so a global loopback bind would break every
    mobile project."""
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    ports = y["services"]["api"].get("ports") or []
    assert all(not str(p).startswith("127.0.0.1") for p in ports), ports


def test_bind_host_is_opt_in_per_service():
    arch = _arch([svc(name="api", workspace_name="api", port=8000,
                      host_port=8081, bind_host="127.0.0.1")])
    y = yaml.safe_load(build_compose(arch, {}, "p"))
    assert y["services"]["api"]["ports"] == ["127.0.0.1:8081:8000"]


def test_bind_host_defaults_to_none():
    assert svc(name="x").bind_host is None


# ── declared host mounts ────────────────────────────────────────────────

def test_declared_mounts_are_emitted_verbatim():
    """The companion to `external`: a stack that adopts a running service
    usually needs to import the modules defining it, and copying them
    instead is how two sources of truth start."""
    arch = _arch([svc(name="api", workspace_name="api",
                      mounts=["../../:/pipeline:ro"])])
    y = yaml.safe_load(build_compose(arch, {}, "p"))
    assert "../../:/pipeline:ro" in y["services"]["api"]["volumes"]


def test_mounts_default_empty_and_change_nothing():
    y_plain = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert svc(name="x").mounts == []
    assert all(":/pipeline" not in v
               for v in y_plain["services"]["api"]["volumes"])


def test_mounts_are_appended_after_convention_volumes():
    """Last, so a declared mount cannot be shadowed by a convention one."""
    arch = _arch([svc(name="api", workspace_name="api",
                      mounts=["../../:/pipeline:ro"])])
    y = yaml.safe_load(build_compose(arch, {}, "p"))
    vols = y["services"]["api"]["volumes"]
    assert vols[-1] == "../../:/pipeline:ro"


def test_declared_environment_overrides_the_language_default():
    """A mount the service cannot import is a mount that silently does
    nothing, so PYTHONPATH has to be reachable from the architecture."""
    arch = _arch([svc(name="api", workspace_name="api",
                      mounts=["../../:/pipeline:ro"],
                      environment={"PYTHONPATH": "/pipeline:/python_core:/app"})])
    y = yaml.safe_load(build_compose(arch, {}, "p"))
    assert y["services"]["api"]["environment"]["PYTHONPATH"] == \
        "/pipeline:/python_core:/app"


def test_language_default_survives_when_nothing_is_declared():
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert y["services"]["api"]["environment"]["PYTHONPATH"] == "/python_core:/app"


# ── declared environment reaches templated services too ─────────────────

_TEMPLATED_ENV = {
    "fusionauth": type("T", (), {
        "compose_service": {
            "image": "fa:pinned",
            "environment": {
                "DATABASE_ROOT_USERNAME": "${POSTGRES_USER}",
                "DATABASE_ROOT_PASSWORD": "${POSTGRES_PASSWORD}",
                "DATABASE_USERNAME": "${POSTGRES_USER}",
                "DATABASE_PASSWORD": "${POSTGRES_PASSWORD}",
            },
            "networks": ["app-network"],
        },
        "compose_volumes": [], "compose_networks": ["app-network"],
    })(),
}


def _fa_arch(env):
    return _arch([svc(name="fusionauth", service_type="auth",
                      framework="fusionauth", language="java",
                      workspace_name="", environment=env)])


def test_declared_environment_overrides_a_templated_service():
    """Without this, a generated FusionAuth necessarily holds the database
    superuser's password — the template names ${POSTGRES_USER} for its
    application credentials and nothing could say otherwise."""
    y = yaml.safe_load(build_compose(
        _fa_arch({"DATABASE_USERNAME": "fusionauth"}), _TEMPLATED_ENV, "p"))
    assert y["services"]["fusionauth"]["environment"]["DATABASE_USERNAME"] \
        == "fusionauth"


def test_a_none_value_removes_the_key_entirely():
    """Stronger isolation than a different password: the service never
    receives root credentials at all."""
    y = yaml.safe_load(build_compose(
        _fa_arch({"DATABASE_ROOT_USERNAME": None,
                  "DATABASE_ROOT_PASSWORD": None}), _TEMPLATED_ENV, "p"))
    env = y["services"]["fusionauth"]["environment"]
    assert "DATABASE_ROOT_USERNAME" not in env
    assert "DATABASE_ROOT_PASSWORD" not in env
    assert env["DATABASE_USERNAME"] == "${POSTGRES_USER}"   # untouched


def test_templated_environment_is_untouched_when_nothing_is_declared():
    y = yaml.safe_load(build_compose(_fa_arch({}), _TEMPLATED_ENV, "p"))
    assert y["services"]["fusionauth"]["environment"]["DATABASE_ROOT_USERNAME"] \
        == "${POSTGRES_USER}"


def test_building_twice_does_not_leak_one_projects_config_into_the_next():
    """`build_compose` used to store the TemplateOutput's dict by reference
    and then edit it, so a second build with the same rendered template
    inherited the first project's credentials. Deep-copied now."""
    shared = _TEMPLATED_ENV
    first = yaml.safe_load(build_compose(
        _fa_arch({"DATABASE_USERNAME": "fusionauth"}), shared, "p1"))
    second = yaml.safe_load(build_compose(_fa_arch({}), shared, "p2"))
    assert first["services"]["fusionauth"]["environment"]["DATABASE_USERNAME"] \
        == "fusionauth"
    assert second["services"]["fusionauth"]["environment"]["DATABASE_USERNAME"] \
        == "${POSTGRES_USER}", "second build inherited the first's credentials"


# ── operator-added .env keys survive a re-provision ─────────────────────

def test_hand_added_env_keys_are_preserved(tmp_path):
    """`write_text` used to erase them, and the failure did not look like an
    erased secret — compose refused a `${VAR:?}` interpolation later and
    named the variable, not the overwrite."""
    from bizniz.provisioner.provisioner import _merge_env
    env = tmp_path / ".env"
    env.write_text("PROJECT_NAME=old\nFUSIONAUTH_DB_PASSWORD=s3cret\n")
    merged = _merge_env(env, "PROJECT_NAME=new\nDATABASE_URL=x\n")
    assert "FUSIONAUTH_DB_PASSWORD=s3cret" in merged


def test_generated_keys_win_over_stale_copies(tmp_path):
    from bizniz.provisioner.provisioner import _merge_env
    env = tmp_path / ".env"
    env.write_text("PROJECT_NAME=old\n")
    merged = _merge_env(env, "PROJECT_NAME=new\n")
    assert "PROJECT_NAME=new" in merged
    assert "PROJECT_NAME=old" not in merged


def test_no_existing_file_is_just_the_generated_text(tmp_path):
    from bizniz.provisioner.provisioner import _merge_env
    text = "PROJECT_NAME=new\n"
    assert _merge_env(tmp_path / "absent.env", text) == text


def test_generated_secrets_are_not_reminted_on_reprovision(tmp_path):
    """A regenerated password desynchronises from state that already
    consumed it: FusionAuth kickstarts an admin user with
    FUSIONAUTH_ADMIN_PASSWORD and stores the hash, so a fresh value leaves
    the .env and the running service disagreeing — and the only symptom is
    a 401 with a password that looks right in the file."""
    from bizniz.provisioner.provisioner import _merge_env
    env = tmp_path / ".env"
    env.write_text("FUSIONAUTH_ADMIN_PASSWORD=original\nDATABASE_URL=old\n")
    merged = _merge_env(env, "FUSIONAUTH_ADMIN_PASSWORD=freshly-minted\n"
                             "DATABASE_URL=new\n")
    assert "FUSIONAUTH_ADMIN_PASSWORD=original" in merged
    assert "freshly-minted" not in merged
    assert "DATABASE_URL=new" in merged, "structural values must still update"


def test_a_secret_absent_from_the_file_is_still_generated(tmp_path):
    from bizniz.provisioner.provisioner import _merge_env
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=old\n")
    merged = _merge_env(env, "FUSIONAUTH_ADMIN_PASSWORD=minted\nDATABASE_URL=new\n")
    assert "FUSIONAUTH_ADMIN_PASSWORD=minted" in merged


def test_declared_command_overrides_the_image_cmd():
    """Needed when a service must start differently from its skeleton's
    Dockerfile — the alternative is editing a skeleton-shipped file."""
    arch = _arch([svc(name="web", service_type="frontend", framework="angular",
                      language="typescript", workspace_name="web",
                      command="npx ng serve --host 0.0.0.0 --proxy-config p.json")])
    y = yaml.safe_load(build_compose(arch, {}, "p"))
    assert y["services"]["web"]["command"].endswith("--proxy-config p.json")


def test_no_command_key_when_none_declared():
    y = yaml.safe_load(build_compose(PLAIN, {}, "p"))
    assert "command" not in y["services"]["api"]


# ── the seeded admin must be able to log in ─────────────────────────────

def test_generated_admin_email_is_not_a_special_use_tld():
    """`.local` is special-use (RFC 6762). pydantic `EmailStr` — which the
    FastAPI skeleton uses on its login body — rejects it outright, so the
    seeded admin got a 422 on the one account every new project uses."""
    import inspect
    from bizniz.provisioner.templates import fusionauth
    src = inspect.getsource(fusionauth)
    assert 'admin@{email_safe_slug}.local"' not in src
    assert "example.com" in src


def test_generated_admin_email_actually_validates():
    """Skipped where email-validator is absent; it is a dependency of the
    generated service, not of the factory."""
    import pytest
    validate_email = pytest.importorskip("email_validator").validate_email
    validate_email("admin@bizniz-console.example.com", check_deliverability=False)
    with pytest.raises(Exception):
        validate_email("admin@bizniz-console.local", check_deliverability=False)


# ── the gates must target the adopted stack, not the host's ─────────────

def test_compose_discovery_prefers_the_matching_stack(tmp_path):
    """`bizniz down` picking the wrong compose file tears down the HOST's
    containers. A repo can hold more than one stack — this project's
    console lives beside the pipeline's own infra/dev/."""
    from bizniz.cli import discover_compose
    (tmp_path / "infra" / "dev").mkdir(parents=True)
    (tmp_path / "infra" / "management").mkdir(parents=True)
    (tmp_path / "infra" / "dev" / "docker-compose.yml").write_text(
        "services:\n  postgres: {}\n  sync: {}\n")
    (tmp_path / "infra" / "management" / "docker-compose.yml").write_text(
        "services:\n  management-api: {}\n  fusionauth: {}\n")
    # Alphabetically "dev" wins; by content, "management" must.
    chosen = discover_compose(
        tmp_path, service_names=["management-api", "fusionauth"])
    assert chosen.parent.name == "management"


def test_compose_discovery_falls_back_without_a_hint(tmp_path):
    from bizniz.cli import discover_compose
    (tmp_path / "infra" / "development").mkdir(parents=True)
    f = tmp_path / "infra" / "development" / "docker-compose.yml"
    f.write_text("services: {}\n")
    assert discover_compose(tmp_path) == f
