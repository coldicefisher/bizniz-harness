"""The Press standard, as a bizniz gate."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bizniz.gates.standard import check, gate


@pytest.fixture
def conforming(tmp_path: Path) -> Path:
    """A project the standard is happy with, built by the scaffold it ships."""
    from press.cli import main

    target = tmp_path / "widgets"
    assert main(["init", str(target), "--no-git"]) == 0
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    return target


def test_a_conforming_project_passes(conforming):
    lines: list[str] = []
    assert gate(conforming, log=lines.append) is True
    assert any("PASSED" in line for line in lines)


def test_a_missing_namespace_fails_with_its_fix(conforming):
    import shutil

    shutil.rmtree(conforming / "ops")      # the scaffold leaves a README in it
    lines: list[str] = []
    assert gate(conforming, log=lines.append) is False
    assert any("LAYOUT-003" in line for line in lines)
    assert any("create `ops/`" in line for line in lines)


def test_the_report_carries_the_profile(conforming):
    assert check(conforming).profile == "standalone"
