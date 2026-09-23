"""Gates for work that lives inside an existing host system.

``bizniz smoke`` gates a stack the harness owns. :mod:`bizniz.gates.hosted` gates an
app that answers behind someone else's proxy and authenticates with their tokens,
which is a different set of failures — see that module.
"""
from __future__ import annotations

from bizniz.gates.boundary import BoundaryResult, Violation
from bizniz.gates.boundary import check as boundary_check
from bizniz.gates.hosted import Check, Result, gate, load_profile

__all__ = ["gate", "load_profile", "Check", "Result",
           "boundary_check", "BoundaryResult", "Violation"]
