"""Host discovery — profile an existing system so new work can live inside it.

The harness provisions greenfield stacks well. It knows nothing about a system that
already exists: its conventions, its identity provider, its proxy, the gates it already
has. Discovery writes that down once, as a profile the host repository keeps, so the
planner and the coder work against the real thing.

    from bizniz.discovery import discover
    profile = discover(Path("~/MUSE/conduit").expanduser(), host="Conduit")

Every claim records how it was established — `verified` (a command ran, an endpoint
answered) or `asserted` (read from a file). See :mod:`bizniz.discovery.types`.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, Optional

from bizniz.discovery.collect import collect
from bizniz.discovery.render import render
from bizniz.discovery.types import HostProfile

__all__ = ["discover", "render", "HostProfile", "write_profile"]


def discover(root: Path, host: Optional[str] = None, *, verify: bool = True,
             log: Callable[[str], None] = lambda _m: None) -> HostProfile:
    """Profile the host rooted at `root`.

    With `verify` (the default), claims that can be checked against the running stack
    are checked; without it, everything stays `asserted`.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    profile = collect(root, host=host or root.name, generated_at=date.today().isoformat())
    if verify:
        from bizniz.discovery.verify import verify as run_verify
        profile = run_verify(profile, log=log)
    return profile


def write_profile(profile: HostProfile, out_dir: Path) -> tuple[Path, Path]:
    """Write `PROFILE.md` and `profile.json`, returning both paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "PROFILE.md", out_dir / "profile.json"
    md.write_text(render(profile))
    js.write_text(profile.model_dump_json(indent=2) + "\n")
    return md, js
