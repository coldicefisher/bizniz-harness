"""Tests for the ``bizniz`` CLI (bizniz/cli.py).

Covers project resolution, run-state discovery, architecture loading,
and the docker-free subcommands (projects/status/validate). Compose-
backed commands (up/down/smoke/test) are exercised only at the parser
level — their bodies are thin wrappers over ``docker compose``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bizniz import cli


# ── fixtures ────────────────────────────────────────────────────────────


ARCH = {
    "project_name": "Demo",
    "project_slug": "demo",
    "description": "demo project",
    "services": [
        {
            "name": "backend",
            "service_type": "backend",
            "framework": "fastapi",
            "language": "python",
            "description": "api",
            "workspace_name": "backend",
            "port": 8000,
        }
    ],
}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    """A minimal generated-project layout under a fake projects root."""
    root = tmp_path / "projects"
    proj = root / "demo"
    run = proj / ".bizniz" / "runs" / "20260101_000000"
    run.mkdir(parents=True)
    (run / "architect.json").write_text(json.dumps(ARCH))
    (run / "run_status.json").write_text(json.dumps(
        {"top_completed": ["plan", "architect", "provision"]}
    ))
    m1 = run / "m1"
    m1.mkdir()
    (m1 / "status.json").write_text(json.dumps(
        {"completed": ["enrich", "implement"], "current": "smoke"}
    ))
    monkeypatch.setenv("BIZNIZ_PROJECTS_ROOT", str(root))
    return proj


# ── project resolution ──────────────────────────────────────────────────


def test_resolve_project_by_slug(project: Path):
    assert cli.resolve_project("demo") == project.resolve()


def test_resolve_project_by_path(project: Path):
    assert cli.resolve_project(str(project)) == project.resolve()


def test_resolve_project_missing_exits(project: Path):
    with pytest.raises(SystemExit):
        cli.resolve_project("nope")


# ── run-state discovery ─────────────────────────────────────────────────


def test_latest_run_dir_picks_newest(project: Path):
    runs = project / ".bizniz" / "runs"
    (runs / "20260102_000000").mkdir()
    latest = cli.latest_run_dir(project)
    assert latest is not None and latest.name == "20260102_000000"


def test_latest_run_dir_none_without_runs(tmp_path: Path):
    assert cli.latest_run_dir(tmp_path) is None


def test_load_architecture_bare(project: Path):
    arch = cli.load_architecture(project)
    assert arch.project_slug == "demo"
    assert arch.services[0].name == "backend"


def test_load_architecture_payload_wrapped(project: Path):
    arch_path = (
        project / ".bizniz" / "runs" / "20260101_000000" / "architect.json"
    )
    arch_path.write_text(json.dumps({"payload": ARCH}))
    arch = cli.load_architecture(project)
    assert arch.project_slug == "demo"


def test_load_architecture_no_runs_exits(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    (root / "empty").mkdir(parents=True)
    monkeypatch.setenv("BIZNIZ_PROJECTS_ROOT", str(root))
    with pytest.raises(SystemExit):
        cli.load_architecture(root / "empty")


# ── subcommands (docker-free) ───────────────────────────────────────────


def test_cmd_projects_lists_runs(project: Path, capsys):
    assert cli.main(["projects"]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "20260101_000000" in out


def test_cmd_status_reports_phases(project: Path, capsys):
    assert cli.main(["status", "demo"]) == 0
    out = capsys.readouterr().out
    assert "plan, architect, provision" in out
    assert "m1: 2 completed (last=implement), current=smoke" in out


def test_cmd_status_done_milestone(project: Path, capsys):
    m1_status = (
        project / ".bizniz" / "runs" / "20260101_000000" / "m1" / "status.json"
    )
    m1_status.write_text(json.dumps({"completed": ["enrich", "done"]}))
    cli.main(["status", "demo"])
    assert "DONE" in capsys.readouterr().out


def test_cmd_status_no_runs_exits_nonzero(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "projects"
    (root / "bare").mkdir(parents=True)
    monkeypatch.setenv("BIZNIZ_PROJECTS_ROOT", str(root))
    assert cli.main(["status", "bare"]) == 1


def test_cmd_validate_clean_workspace(tmp_path: Path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.py").write_text("import json\nprint(json.dumps({}))\n")
    assert cli.main(["validate", str(ws)]) == 0
    assert "PASSED" in capsys.readouterr().out


def test_cmd_validate_bad_import_fails(tmp_path: Path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "bad.py").write_text("import definitely_not_a_real_module_xyz\n")
    assert cli.main(["validate", str(ws)]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_cmd_validate_empty_workspace_fails(tmp_path: Path, capsys):
    ws = tmp_path / "empty_ws"
    ws.mkdir()
    assert cli.main(["validate", str(ws)]) == 1


def test_cmd_validate_project_defaults_to_backend(project: Path, capsys):
    backend = project / "backend"
    backend.mkdir()
    (backend / "app.py").write_text("import json\n")
    assert cli.main(["validate", "demo"]) == 0


# ── parser wiring ───────────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [
    ["projects"],
    ["status", "x"],
    ["up", "x"],
    ["down", "x"],
    ["smoke", "x", "--timeout", "2"],
    ["test", "x", "--service", "frontend"],
    ["validate", "x"],
    ["perf", "analyze", "some.log"],
    ["mcp"],
])
def test_parser_accepts_all_subcommands(argv):
    args = cli.build_parser().parse_args(argv)
    assert callable(args.fn)


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_test_command_default_and_override():
    args = cli.build_parser().parse_args(["test", "proj"])
    assert args.service == "backend" and args.cmd == []
    args = cli.build_parser().parse_args(
        ["test", "proj", "--service", "frontend", "npm", "test"]
    )
    assert args.cmd == ["npm", "test"]
