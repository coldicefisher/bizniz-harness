"""Deterministic gates for device-type (Expo mobile) workspaces.

The mobile analogues of the web pipeline's gates, all hard exit codes:

  validate  →  tsc --noEmit + expo lint
  test      →  jest (host-side; mobile workspaces aren't containerized)
  smoke     →  gradle release build → adb install → maestro flows

Hard-won rules encoded here (2026-08-10 loop-proof, see
docs + bizniz-skeleton-expo SKELETON.md):
  - Smoke uses the RELEASE variant: debug builds need a Metro dev
    server, and dev-server ports collide across concurrent projects.
  - The install device is ALWAYS chosen explicitly via ``adb -s`` —
    the expo CLI ignores ANDROID_SERIAL and installs to the first
    adb device it sees.
  - The emulator is resolved by AVD NAME (``BIZNIZ_AVD``, default
    "bizniz"), never "whatever is running": other projects' emulators
    may be up.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

Log = Callable[[str], None]

_DEFAULT_AVD = "bizniz"
_BOOT_TIMEOUT_S = 180
_RELEASE_APK_REL = Path("android/app/build/outputs/apk/release/app-release.apk")


def is_expo_workspace(workspace: Path) -> bool:
    """A workspace is an Expo mobile workspace iff its package.json
    declares the ``expo`` dependency."""
    pkg = workspace / "package.json"
    if not pkg.exists():
        return False
    try:
        import json
        data = json.loads(pkg.read_text())
    except Exception:
        return False
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    return "expo" in deps


def _run(cmd: List[str], cwd: Optional[Path] = None, log: Optional[Log] = None,
         env: Optional[dict] = None, timeout: int = 3600) -> int:
    if log:
        log(f"$ {' '.join(cmd)}")
    merged_env = {**os.environ, **(env or {})}
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged_env,
                          timeout=timeout)
    return proc.returncode


# ── validate ────────────────────────────────────────────────────────────


def run_validate(workspace: Path, log: Optional[Log] = None) -> int:
    """tsc --noEmit + expo lint. Non-zero on the first failing gate."""
    rc = _run(["npx", "tsc", "--noEmit"], cwd=workspace, log=log)
    if rc != 0:
        return rc
    return _run(["npm", "run", "lint"], cwd=workspace, log=log)


# ── test ────────────────────────────────────────────────────────────────


def run_tests(workspace: Path, extra_args: Optional[List[str]] = None,
              log: Optional[Log] = None) -> int:
    """jest on the host. Mobile workspaces have no container to exec in."""
    return _run(["npx", "jest", *(extra_args or [])], cwd=workspace, log=log)


# ── emulator management ─────────────────────────────────────────────────


def _adb(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", *args], capture_output=True, text=True,
                          timeout=timeout)


def _booted_serials() -> List[str]:
    out = _adb(["devices"]).stdout
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _avd_name(serial: str) -> str:
    out = _adb(["-s", serial, "emu", "avd", "name"]).stdout
    # Response is "<name>\r\nOK"; first non-empty line is the name.
    for line in out.splitlines():
        line = line.strip()
        if line and line != "OK":
            return line
    return ""


def ensure_emulator(avd: Optional[str] = None,
                    log: Optional[Log] = None) -> str:
    """Return the serial of a booted emulator running ``avd``,
    booting one headless if none is. Raises RuntimeError on timeout.

    Resolution is BY AVD NAME so concurrent projects' emulators are
    never hijacked.
    """
    avd = avd or os.environ.get("BIZNIZ_AVD", _DEFAULT_AVD)
    for serial in _booted_serials():
        if serial.startswith("emulator-") and _avd_name(serial) == avd:
            if log:
                log(f"emulator: reusing {serial} (avd={avd})")
            return serial

    emulator_bin = (
        Path(os.environ.get("ANDROID_HOME", str(Path.home() / "Android/Sdk")))
        / "emulator" / "emulator"
    )
    if log:
        log(f"emulator: booting avd '{avd}' headless…")
    before = set(_booted_serials())
    subprocess.Popen(
        [str(emulator_bin), "-avd", avd, "-no-window", "-no-audio",
         "-no-boot-anim", "-no-snapshot-save", "-gpu", "swiftshader_indirect"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + _BOOT_TIMEOUT_S
    while time.time() < deadline:
        for serial in _booted_serials():
            if serial in before or not serial.startswith("emulator-"):
                continue
            if _avd_name(serial) != avd:
                continue
            boot = _adb(["-s", serial, "shell", "getprop",
                         "sys.boot_completed"]).stdout.strip()
            if boot == "1":
                if log:
                    log(f"emulator: {serial} booted")
                return serial
        time.sleep(3)
    raise RuntimeError(f"emulator '{avd}' did not boot within {_BOOT_TIMEOUT_S}s")


# ── smoke ───────────────────────────────────────────────────────────────


def _app_id(workspace: Path) -> Optional[str]:
    import json
    app_json = workspace / "app.json"
    if not app_json.exists():
        return None
    try:
        data = json.loads(app_json.read_text())
        return data.get("expo", {}).get("android", {}).get("package")
    except Exception:
        return None


def run_smoke(workspace: Path, avd: Optional[str] = None,
              skip_build: bool = False, log: Optional[Log] = None) -> int:
    """Release build → explicit adb install → maestro flows.

    ``skip_build`` reuses an existing release APK (fast re-gate when
    only maestro flows changed).
    """
    flows = workspace / ".maestro"
    if not flows.is_dir() or not any(flows.glob("*.yaml")):
        if log:
            log(f"smoke: no maestro flows under {flows} — nothing to gate")
        return 1

    apk = workspace / _RELEASE_APK_REL
    if not skip_build or not apk.exists():
        if not (workspace / "android").is_dir():
            rc = _run(["npx", "expo", "prebuild", "--platform", "android",
                       "--no-install"], cwd=workspace, log=log, env={"CI": "1"})
            if rc != 0:
                return rc
        # Via bash: expo prebuild does not reliably set gradlew's exec
        # bit, and the gate must not depend on file modes.
        rc = _run(["bash", "gradlew", "assembleRelease"],
                  cwd=workspace / "android", log=log)
        if rc != 0:
            return rc
    if not apk.exists():
        if log:
            log(f"smoke: release APK not found at {apk}")
        return 1

    serial = ensure_emulator(avd, log=log)
    rc = _run(["adb", "-s", serial, "install", "-r", str(apk)], log=log)
    if rc != 0:
        return rc

    maestro = str(Path.home() / ".maestro" / "bin" / "maestro")
    return _run([maestro, "--device", serial, "test", str(flows)],
                cwd=workspace, log=log)
