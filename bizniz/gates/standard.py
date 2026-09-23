"""The Press standard as a bizniz gate.

The harness generates applications; `press` decides whether they meet the standard those
applications are supposed to meet. Keeping the two apart matters: the layout belongs to
the standard, not to this provisioner, so the definition lives in one place and the
generator is held to it like anything a person writes.

This is the seam. `bizniz standard <project>` runs the same check a reviewer and CI run —
no model, no negotiation — and a milestone that leaves a project failing it is a milestone
that is not done.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

Log = Callable[[str], None]


class StandardUnavailable(RuntimeError):
    """press-standard is not installed. Said out loud rather than skipped silently."""


def check(project: Path, stage: Optional[str] = None, profile: Optional[str] = None):
    """Run the standard against a generated project. Returns a press Report."""
    try:
        from press.repo import Repo
        from press.rules import run
    except ImportError as exc:                      # pragma: no cover - environment issue
        raise StandardUnavailable(
            "press-standard is not installed in this environment; "
            "`pip install -e ~/MUSE/press-standard`") from exc

    return run(Repo(Path(project)), stage=stage, profile=profile)


def gate(project: Path, stage: Optional[str] = None, profile: Optional[str] = None,
         log: Log = lambda _m: None) -> bool:
    """True when the project meets the standard. Logs each failure with its fix."""
    report = check(project, stage=stage, profile=profile)
    for finding in report.failures:
        log(f"FAIL {finding.rule.id} — {finding.detail}")
        if finding.fix:
            log(f"     fix: {finding.fix}")
    satisfied, decided = report.score()
    log(f"standard: {'PASSED' if report.passed else 'FAILED'} "
        f"({satisfied}/{decided} required rules, profile {report.profile})")
    return report.passed
