"""The carve-off boundary: an app may use the host's interfaces, never its code."""
from __future__ import annotations

from pathlib import Path

import pytest

from bizniz.discovery.types import HostProfile, asserted, verified
from bizniz.gates.boundary import check


def make_profile() -> HostProfile:
    p = HostProfile(host="Host", root="/tmp", generated_at="2026-09-22")
    p.boundary.code_roots = asserted(["source-code"], "compose.yml")
    p.boundary.packages = verified(["core", "openscholar_schema"], "grep")
    p.boundary.build_internals = asserted(["source-code/", "host-base", "infra/build/"], "bake")
    p.boundary.integration_paths = asserted(["infra/"], "infra/")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A host with one hosted app that respects the boundary."""
    app = tmp_path / "chatty"
    (app / "api/app").mkdir(parents=True)
    (app / "api/app/main.py").write_text(
        "from app.config import settings\nimport httpx\n")
    (app / "web/src").mkdir(parents=True)
    (app / "web/src/app.ts").write_text("import { thing } from './thing';\n")
    (app / "web/src/thing.ts").write_text("export const thing = 1;\n")
    (app / "infra").mkdir()
    (app / "infra/compose.dev.yml").write_text("services: {}\n")

    (tmp_path / "source-code/core").mkdir(parents=True)
    (tmp_path / "source-code/core/util.py").write_text("VALUE = 1\n")
    (tmp_path / "infra/dev").mkdir(parents=True)
    # Host-side wiring names the app, and is expected to.
    (tmp_path / "infra/dev/docker-compose.yml").write_text(
        "include:\n  - path: ../../chatty/infra/compose.dev.yml\n")
    return tmp_path


def test_a_conforming_app_passes(repo):
    result = check(repo, "chatty", make_profile())
    assert result.passed
    assert result.checked_files > 0


def test_importing_a_host_package_is_a_violation(repo):
    (repo / "chatty/api/app/main.py").write_text("from core.services import thing\n")
    result = check(repo, "chatty", make_profile())
    assert not result.passed
    violation = result.violations[0]
    assert violation.rule == "host-import"
    assert violation.location == "chatty/api/app/main.py:1"
    assert "would not build once carved off" in violation.detail


def test_a_similarly_named_local_module_is_not_a_violation(repo):
    """`app.core` is the app's own; only the bare host package name is forbidden."""
    (repo / "chatty/api/app/main.py").write_text("from app.core import thing\nimport corelib\n")
    assert check(repo, "chatty", make_profile()).passed


def test_referencing_host_build_internals_is_a_violation(repo):
    (repo / "chatty/infra/api.Dockerfile").write_text("FROM host-base:latest\n")
    result = check(repo, "chatty", make_profile())
    assert [v.rule for v in result.violations] == ["host-internals"]


def test_frontend_reaching_outside_the_app_is_a_violation(repo):
    (repo / "chatty/web/src/app.ts").write_text(
        "import { shared } from '../../../source-code/frontend/shared';\n")
    result = check(repo, "chatty", make_profile())
    assert any(v.rule == "outside-app" for v in result.violations)


def test_the_host_naming_the_app_in_its_code_is_a_violation(repo):
    """The dependency points into the host. Host code calling the app ends the carve-off."""
    (repo / "source-code/core/util.py").write_text(
        "CHAT_URL = 'http://chatty-api:8010'\n")
    result = check(repo, "chatty", make_profile())
    violation = next(v for v in result.violations if v.rule == "wrong-direction")
    assert "must point into the host" in violation.detail


def test_host_side_wiring_may_name_the_app(repo):
    """infra/ is where the app is attached; naming it there is the integration, not a leak."""
    (repo / "infra/dev/docker-compose.yml").write_text(
        "include:\n  - path: ../../chatty/infra/compose.dev.yml\nservices:\n  chatty-api: {}\n")
    assert check(repo, "chatty", make_profile()).passed


def test_documentation_may_describe_both_sides(repo):
    (repo / "chatty/README.md").write_text("This app must never `from core import x`.\n")
    assert check(repo, "chatty", make_profile()).passed


def test_a_named_file_may_describe_the_boundary(repo):
    (repo / "chatty/tools/lint.py").parent.mkdir(parents=True)
    (repo / "chatty/tools/lint.py").write_text("FORBIDDEN = 'source-code/'\n")
    assert not check(repo, "chatty", make_profile()).passed
    assert check(repo, "chatty", make_profile(),
                 self_describing={"chatty/tools/lint.py"}).passed


def test_a_profile_without_packages_says_the_rule_did_not_run(repo):
    """Silence would read as "clean". It has to say the check was not performed."""
    profile = make_profile()
    profile.boundary.packages = asserted([], "none")
    result = check(repo, "chatty", profile)
    assert any("rule 1" in s for s in result.skipped)
    assert result.passed          # nothing was found, but the caller is told why


def test_a_missing_app_directory_is_an_error(repo):
    with pytest.raises(FileNotFoundError):
        check(repo, "nosuchapp", make_profile())


def test_vendored_directories_are_not_scanned(repo):
    (repo / "chatty/web/node_modules/pkg").mkdir(parents=True)
    (repo / "chatty/web/node_modules/pkg/index.js").write_text("require('../../../source-code/x')\n")
    assert check(repo, "chatty", make_profile()).passed
