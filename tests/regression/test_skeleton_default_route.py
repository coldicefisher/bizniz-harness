"""The Angular skeleton must not claim the application's root route.

REGRESSION
----------
The skeleton shipped ``{ path: '', loadChildren: () => HomeModule }`` —
its own "Your application is running" placeholder mounted at ``/``. Every
application cut from the factory inherited it, and every one had to be
un-wired by hand.

That is a slow bug rather than a loud one, because it fails *positively*.
A misrouted app 404s and you know in a second. This renders a cheerful
success page, so an application with a passing test suite, a live API and
a valid session still looks like a blank product. In bizniz-tycoon it
survived a full green frontend suite and cost two wrong diagnoses (a
cached tab, then an auth guard) before anyone opened a browser.

The fix moves the placeholder to ``/home`` and leaves ``/`` for the
application to claim in ``default-route.ts``, with a build guard that
refuses to start, build or test until it is claimed and points somewhere
real. These tests hold that arrangement in place.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bizniz.architect.skeletons import skeletons_root

SKELETON = skeletons_root() / "bizniz-skeleton-angular"
ROUTING = SKELETON / "src" / "app" / "app-routing.module.ts"
DEFAULT_ROUTE = SKELETON / "src" / "app" / "default-route.ts"
GUARD = SKELETON / "scripts" / "check-default-route.mjs"

pytestmark = pytest.mark.skipif(
    not SKELETON.exists(), reason=f"angular skeleton not present at {SKELETON}")


def _strip_comments(src: str) -> str:
    """Comments are not code. The guard script learned this the hard way:
    its first version matched the example ``DEFAULT_ROUTE = 'console'``
    inside its own documentation and reported a claimed route on a
    skeleton that had claimed nothing."""
    return re.sub(r"^\s*//.*$", "", re.sub(r"/\*[\s\S]*?\*/", "", src), flags=re.M)


# --- the arrangement itself -------------------------------------------

def test_root_is_a_redirect_not_a_module():
    """``''`` must not lazy-load anything. This is the actual regression."""
    routes = _strip_comments(ROUTING.read_text())
    root = re.search(r"\{[^}]*path:\s*''[^}]*\}", routes)
    assert root, "no route for '' found in app-routing.module.ts"
    entry = root.group(0)
    assert "redirectTo" in entry, f"root must redirect, got: {entry}"
    assert "loadChildren" not in entry, (
        f"root claims a module again — this is the original bug: {entry}")
    assert "component" not in entry, f"root claims a component: {entry}"


def test_root_redirect_is_full_match():
    """Without ``pathMatch: 'full'`` the redirect swallows every deep link,
    which trades a blank home page for an app you cannot navigate."""
    routes = _strip_comments(ROUTING.read_text())
    root = re.search(r"\{[^}]*path:\s*''[^}]*\}", routes).group(0)
    assert "pathMatch: 'full'" in root, f"root redirect not full-match: {root}"


def test_root_redirects_to_the_shared_constant():
    """Hardcoding the target here would put the decision in a file the
    skeleton owns, where the build guard cannot see it."""
    routes = _strip_comments(ROUTING.read_text())
    root = re.search(r"\{[^}]*path:\s*''[^}]*\}", routes).group(0)
    assert "DEFAULT_ROUTE" in root, (
        f"root should redirect to DEFAULT_ROUTE, got: {root}")
    assert "from './default-route'" in routes, "DEFAULT_ROUTE is not imported"


def test_placeholder_still_reachable_at_home():
    """The demo view is not deleted — just demoted. Removing it would
    break the skeleton's own smoke expectations."""
    routes = _strip_comments(ROUTING.read_text())
    assert re.search(r"path:\s*'home'[^}]*HomeModule", routes), (
        "the placeholder should still be served, at /home")


def test_skeleton_ships_unclaimed():
    """A skeleton that ships a working default has claimed the root on the
    application's behalf, which is the bug wearing a different hat."""
    src = _strip_comments(DEFAULT_ROUTE.read_text())
    match = re.search(r"DEFAULT_ROUTE\s*(?::[^=]+)?=\s*'([^']*)'", src)
    assert match, "default-route.ts must export a string DEFAULT_ROUTE"
    assert match.group(1) == "UNCLAIMED", (
        f"skeleton ships DEFAULT_ROUTE='{match.group(1)}'; it must ship "
        f"unclaimed so the guard fires until an application decides")


@pytest.mark.parametrize("script", ["start", "build", "test"])
def test_guard_runs_before_every_entry_point(script):
    """``build`` alone is not enough: the first thing anyone runs on a
    fresh workspace is ``start``, and the first thing CI runs is ``test``."""
    scripts = json.loads((SKELETON / "package.json").read_text())["scripts"]
    assert "check-default-route" in scripts[script], (
        f"npm run {script} does not invoke the root-route guard: {scripts[script]}")


# --- the guard's own behaviour ----------------------------------------

_NODE = shutil.which("node")


def _run_guard(tmp_path: Path, default_route_value: str):
    """Copy the skeleton's app + guard into a sandbox, set DEFAULT_ROUTE,
    and run it. Never mutates the real skeleton."""
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    shutil.copy(GUARD, tmp_path / "scripts")
    shutil.copy(ROUTING, app)
    (app / "default-route.ts").write_text(
        f"export const DEFAULT_ROUTE = '{default_route_value}';\n")
    return subprocess.run(
        [_NODE, str(tmp_path / "scripts" / "check-default-route.mjs")],
        capture_output=True, text=True)


@pytest.mark.skipif(not _NODE, reason="node not available")
def test_guard_rejects_the_sentinel(tmp_path):
    result = _run_guard(tmp_path, "UNCLAIMED")
    assert result.returncode != 0, "unclaimed root must fail the build"
    assert "default-route.ts" in result.stderr, (
        "the failure must name the file to edit, or it is just an error")


@pytest.mark.skipif(not _NODE, reason="node not available")
def test_guard_rejects_a_route_that_does_not_exist(tmp_path):
    """The second way to a blank front door, and the harder one to see:
    DEFAULT_ROUTE is set, so it looks decided, but it is misspelled."""
    result = _run_guard(tmp_path, "konsole")
    assert result.returncode != 0, "redirect to an undeclared route must fail"
    assert "konsole" in result.stderr


@pytest.mark.skipif(not _NODE, reason="node not available")
def test_guard_accepts_a_declared_route(tmp_path):
    result = _run_guard(tmp_path, "settings")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not _NODE, reason="node not available")
def test_guard_accepts_a_nested_route(tmp_path):
    """Only the first segment can be declared at the top level; the router
    resolves the rest through child routes the guard cannot see."""
    result = _run_guard(tmp_path, "docs/getting-started")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not _NODE, reason="node not available")
def test_guard_is_not_fooled_by_its_own_documentation(tmp_path):
    """Positive control for the comment stripper. A checker that reads
    comments is worse than no checker, because it is believed."""
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    shutil.copy(GUARD, tmp_path / "scripts")
    shutil.copy(ROUTING, app)
    (app / "default-route.ts").write_text(
        "/* Example: export const DEFAULT_ROUTE = 'settings'; */\n"
        "export const DEFAULT_ROUTE = 'UNCLAIMED';\n")
    result = subprocess.run(
        [_NODE, str(tmp_path / "scripts" / "check-default-route.mjs")],
        capture_output=True, text=True)
    assert result.returncode != 0, (
        "guard read the commented-out example as the real declaration")
