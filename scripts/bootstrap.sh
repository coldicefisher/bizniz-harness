#!/usr/bin/env bash
#
# Set up the bizniz harness on a fresh machine. Safe to re-run.
#
# Checks prerequisites, clones the skeleton repos, installs the package,
# and verifies the install actually works. Prints one summary at the end
# and exits non-zero if anything required is missing.
#
# The skeleton clone is the check that matters most. Skeletons are cloned
# over SSH on demand, and when that clone fails the Provisioner CATCHES
# the error and falls back to generating the service from scratch. The
# build then succeeds while producing a service with none of the
# skeleton's auth wiring, Docker setup, or conventions. Nothing errors.
# So this script clones them up front, where a failure is loud.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKELETONS_DIR="${BIZNIZ_SKELETONS_DIR:-$HOME}"
PROJECTS_ROOT="${BIZNIZ_PROJECTS_ROOT:-$HOME/bizniz_projects}"
GITHUB_ORG="coldicefisher"

SKELETON_REPOS=(
  bizniz-skeleton-fastapi
  bizniz-skeleton-react
  bizniz-skeleton-angular
  bizniz-skeleton-expo
  bizniz-skeleton-teams
  bizniz-skeleton-saas
)

FAILURES=()
WARNINGS=()

say()  { printf '%s\n' "$*"; }
head2() { printf '\n%s\n%s\n' "$*" "$(printf '%.0s-' $(seq 1 ${#1}))"; }
ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILURES+=("$*"); }
warn() { printf '  warn  %s\n' "$*"; WARNINGS+=("$*"); }

# ── 1. Prerequisites ──────────────────────────────────────────────────

head2 "Prerequisites"

PYBIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PYBIN="$(command -v "$candidate")"
      break
    fi
  fi
done

if [[ -n "$PYBIN" ]]; then
  ok "python $("$PYBIN" -c 'import platform; print(platform.python_version())') at $PYBIN"
else
  bad "no python 3.10 or newer on PATH"
fi

if command -v git >/dev/null 2>&1; then
  ok "git $(git --version | awk '{print $3}')"
else
  bad "git is not installed"
fi

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable"
  else
    bad "docker is installed but the daemon is not reachable (start it, or add yourself to the docker group)"
  fi
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin"
  else
    bad "docker compose plugin missing (the v1 'docker-compose' binary is not supported)"
  fi
else
  bad "docker is not installed"
fi

if command -v node >/dev/null 2>&1; then
  ok "node $(node --version) (needed by the Angular route guard and the screenshot sidecar)"
else
  warn "node not found; the Angular root-route guard and UX screenshot sidecar will not run"
fi

if command -v claude >/dev/null 2>&1; then
  ok "claude CLI on PATH (the default agent backend)"
else
  warn "claude CLI not found; agent work needs it, or an API key backend configured in bizniz.yaml"
fi

# ── 2. GitHub SSH access ──────────────────────────────────────────────

head2 "GitHub access"

# `ssh -T git@github.com` exits 1 on success, which is why this greps the
# banner instead of trusting the status code.
SSH_OUT="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1)"
if grep -q "successfully authenticated" <<<"$SSH_OUT"; then
  ok "ssh to github.com authenticates as $(grep -oP 'Hi \K[^!]+' <<<"$SSH_OUT" || echo '?')"
else
  bad "ssh to github.com failed: ${SSH_OUT%%$'\n'*}"
  say ""
  say "  Skeletons are cloned over SSH. Without this, the Provisioner does"
  say "  NOT fail: it catches the clone error and generates each service"
  say "  from scratch instead, producing a build that looks fine and has"
  say "  none of the skeleton's auth, Docker or route conventions."
fi

# ── 3. Skeletons ──────────────────────────────────────────────────────

head2 "Skeletons (in $SKELETONS_DIR)"

mkdir -p "$SKELETONS_DIR"
for repo in "${SKELETON_REPOS[@]}"; do
  target="$SKELETONS_DIR/$repo"
  if [[ -d "$target/.git" ]]; then
    ok "$repo present"
  elif [[ -e "$target" ]]; then
    bad "$repo exists at $target but is not a git repo"
  else
    printf '  ....  cloning %s\n' "$repo"
    if git clone --depth 1 "git@github.com:$GITHUB_ORG/$repo.git" "$target" >/dev/null 2>&1; then
      ok "$repo cloned"
    else
      rm -rf "$target"
      bad "$repo could not be cloned (no access, or the repo is gone)"
    fi
  fi
done

# ── 4. Install ────────────────────────────────────────────────────────

head2 "Install"

if [[ -n "$PYBIN" ]]; then
  if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
    printf '  ....  creating virtualenv\n'
    "$PYBIN" -m venv "$REPO_ROOT/.venv" || bad "could not create $REPO_ROOT/.venv"
  fi
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    ok "virtualenv at $REPO_ROOT/.venv"
    printf '  ....  installing (editable)\n'
    if "$REPO_ROOT/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 \
       && "$REPO_ROOT/.venv/bin/pip" install -q -e "$REPO_ROOT" >/dev/null 2>&1; then
      ok "bizniz installed"
    else
      bad "pip install -e . failed; re-run it by hand to see the error"
    fi
  fi
else
  bad "skipping install: no usable python"
fi

# ── 5. Verify ─────────────────────────────────────────────────────────

head2 "Verify"

BIZNIZ="$REPO_ROOT/.venv/bin/bizniz"
PY="$REPO_ROOT/.venv/bin/python"

if [[ -x "$BIZNIZ" ]] && "$BIZNIZ" --help >/dev/null 2>&1; then
  ok "bizniz CLI runs ($("$BIZNIZ" --help | grep -oP '\{\K[a-z,]+' | head -1 | tr ',' ' ' | wc -w) subcommands)"
else
  bad "the bizniz CLI does not run"
fi

if [[ -x "$PY" ]] && "$PY" - <<'EOF' >/dev/null 2>&1
import importlib
for m in ("bizniz.cli", "bizniz.provisioner.provisioner", "bizniz.driver.pipeline",
          "bizniz.mcp_server.server", "bizniz.architect.skeletons"):
    importlib.import_module(m)
EOF
then
  ok "core subpackages import"
else
  bad "core subpackages do not import; the install is incomplete"
fi

if [[ -x "$PY" ]]; then
  printf '  ....  running the test suite\n'
  SUITE="$("$PY" -m pytest "$REPO_ROOT/bizniz" "$REPO_ROOT/tests" -q 2>&1 | tail -1)"
  if grep -qE '^[0-9]+ passed' <<<"$SUITE" && ! grep -q 'failed' <<<"$SUITE"; then
    ok "test suite: $SUITE"
  else
    bad "test suite did not come back clean: $SUITE"
  fi
fi

mkdir -p "$PROJECTS_ROOT" && ok "projects root $PROJECTS_ROOT"

# ── Summary ───────────────────────────────────────────────────────────

head2 "Summary"

if ((${#WARNINGS[@]})); then
  say "Optional pieces missing:"
  for w in "${WARNINGS[@]}"; do say "  - $w"; done
  say ""
fi

if ((${#FAILURES[@]})); then
  say "NOT READY. ${#FAILURES[@]} problem(s):"
  for f in "${FAILURES[@]}"; do say "  - $f"; done
  say ""
  say "Fix these before running a build. Do not work around a skeleton or"
  say "SSH failure by continuing: the Provisioner treats a missing skeleton"
  say "as a reason to generate the service itself, and says nothing."
  exit 1
fi

say "READY."
say ""
say "Next: start Claude Code from inside this repo, so its subagents,"
say "skills and MCP server load:"
say ""
say "    cd $REPO_ROOT && claude"
say ""
say "Then try:"
say "    bizniz projects                 # what already exists here"
say "    bizniz validate <path>          # AST gate, works on any Python tree"
say "    /bizniz-review <project>        # read-only defect report"
say ""
say "Full command reference: $REPO_ROOT/README.md"
