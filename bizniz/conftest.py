"""Package-level pytest configuration.

perf_tests fixture workspaces contain deliberately-broken test files —
the breakage IS the fixture (BatchFixDebugger repairs them). Collecting
them fails the ``pytest bizniz/`` sweep with import errors, so they are
excluded here. Paths are relative to this conftest, so the exclusion
holds regardless of invocation CWD.
"""

collect_ignore = ["perf_tests/fixtures"]
