"""Dropping a reference to an execution environment must not run Docker.

REGRESSION
----------
Both Docker execution environments defined ``__del__`` as ``self.stop()``,
and ``stop()`` runs ``docker cp`` (syncing files OUT of the container) and
``docker rm``. That is blocking I/O, executed at whatever moment the
garbage collector happens to drop the last reference.

It surfaced as an unrelated-looking flake. Every unit test for these
classes patches ``subprocess.run`` with a fixed list of side effects, so an
environment constructed in an EARLIER test and collected during a LATER one
consumed the later test's side effects, shifted every subsequent call by
one, and failed it with an empty container id. Which test broke moved with
collection size, so the same commit passed and failed depending on what
else ran.

The production hazard is the larger one: dropping a reference should not
write to the workspace, and at interpreter shutdown ``__del__`` may run
against half-torn-down module globals.

Cleanup now happens at two known moments instead: an atexit hook over a
registry of started containers, and ``_cleanup_stale_containers`` on the
next start. Containers are also started with ``--rm``.
"""
from __future__ import annotations

import gc
from unittest.mock import patch

import pytest

from bizniz.environment import docker_jest_environment, docker_pytest_environment
from bizniz.environment.docker_jest_environment import DockerJestEnvironment
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment

MODULES = [docker_pytest_environment, docker_jest_environment]
CLASSES = [DockerPytestEnvironment, DockerJestEnvironment]
PAIRS = list(zip(MODULES, CLASSES))
IDS = ["pytest-env", "jest-env"]


@pytest.mark.parametrize("cls", CLASSES, ids=IDS)
def test_no_del_hook(cls):
    """The actual regression. A `__del__` on these classes is a docker call
    scheduled by the garbage collector."""
    assert "__del__" not in vars(cls), (
        f"{cls.__name__} defines __del__ again; cleanup must stay on the "
        f"atexit registry so it happens at a known moment")


@pytest.mark.parametrize("mod,cls", PAIRS, ids=IDS)
def test_collecting_an_environment_runs_no_subprocess(mod, cls, tmp_path):
    """The failure as it actually presented: a collected environment
    reaching into a live test's mock."""
    env = cls(workspace_root=tmp_path, image="img:latest")
    env._container_id = "container123"
    with patch.object(mod, "subprocess") as sp:
        del env
        gc.collect()
        assert sp.run.call_count == 0, sp.run.call_args_list
    mod._LIVE_CONTAINERS.discard("container123")


@pytest.mark.parametrize("mod,cls", PAIRS, ids=IDS)
def test_stop_still_removes_the_container(mod, cls, tmp_path):
    """Explicit cleanup is unchanged. Only the implicit path went away."""
    env = cls(workspace_root=tmp_path, image="img:latest")
    env._container_id = "container123"
    mod._LIVE_CONTAINERS.add("container123")
    with patch.object(mod, "subprocess") as sp:
        env.stop()
    assert any("rm" in list(c.args[0]) for c in sp.run.call_args_list if c.args)
    assert env._container_id is None
    assert "container123" not in mod._LIVE_CONTAINERS, (
        "a stopped container must leave the registry, or the atexit hook "
        "tries to remove it again")


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_atexit_hook_removes_registered_containers(mod):
    """The replacement safety net. Runs once, at a known moment, whether or
    not the environment object is still referenced."""
    mod._LIVE_CONTAINERS.add("leaked-container")
    try:
        with patch.object(mod, "subprocess") as sp:
            mod._remove_live_containers()
        cmds = [list(c.args[0]) for c in sp.run.call_args_list if c.args]
        assert any("leaked-container" in c for c in cmds), cmds
        assert "leaked-container" not in mod._LIVE_CONTAINERS
    finally:
        mod._LIVE_CONTAINERS.discard("leaked-container")


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_removal_never_raises(mod):
    """Best effort by contract: this runs during interpreter shutdown,
    where a raised exception is printed and ignored but the remaining
    containers are then never cleaned up."""
    mod._LIVE_CONTAINERS.add("boom")
    try:
        with patch.object(mod, "subprocess") as sp:
            sp.run.side_effect = OSError("docker daemon gone")
            mod._remove_live_containers()
        assert "boom" not in mod._LIVE_CONTAINERS
    finally:
        mod._LIVE_CONTAINERS.discard("boom")
