# bizniz

A build harness. It turns a problem statement into a running, tested
application, and it gates every claim against a live stack.

## Start here

Paste this into a fresh Claude Code session. Any directory — you do not
need the repo yet.

```
Set up the bizniz build harness on this machine.

1. Clone git@github.com:coldicefisher/bizniz-harness.git into ~/bizniz.
   If ~/bizniz already exists, use it and pull instead of re-cloning.
2. Run ~/bizniz/scripts/bootstrap.sh and show me its full output.
3. If it reports failures, diagnose them and fix what you reasonably can,
   then re-run it. Report anything you cannot fix.
4. Once it prints READY, read ~/bizniz/README.md and give me a short
   summary of the gate commands, then tell me to restart you from inside
   the repo.

Two things not to do:

- Do not work around a skeleton clone or GitHub SSH failure by carrying on
  without them. The Provisioner catches a missing skeleton and generates
  the service from scratch instead, so the build then SUCCEEDS while
  producing services with none of the skeleton's auth, Docker or routing
  conventions. A red bootstrap is much cheaper than that.
- Do not edit the test suite or the bootstrap checks to make them pass.
  If the suite is red on a clean clone, that is the finding, and I want to
  hear it.
```

It clones the repo and the six skeleton repos, installs, and verifies the
result end to end. When it finishes, restart Claude Code from inside the
repo — `cd ~/bizniz && claude` — so the subagents, skills and MCP server
load. See [BOOTSTRAP.md](BOOTSTRAP.md) for what each step checks and how
to do it by hand.

**You need read access to all six `bizniz-skeleton-*` repositories, over
SSH.** `gh auth login` is not sufficient: it authenticates `gh`, not `git`
over SSH. Check with `ssh -T git@github.com` before you start.

---

## Why it works this way

Agent-written code does not fail loudly, it *reports success*. A model
will tell you the tests pass, the route is wired and the feature is done,
and be wrong about all three in a way that reads exactly like being right.
So every claim here has to survive a deterministic check that no model
runs — plain Python and shell that curl a live stack, walk an AST, and run
tests inside the container that will actually serve traffic. They exit
non-zero. Nothing talks its way past them.

Everything else in this repo exists to feed those gates or react to them.

---

## Contents

- [Install by hand](#install-by-hand)
- [Quickstart](#quickstart)
- [The `bizniz` CLI](#the-bizniz-cli) — the gate surface
- [The Claude-native layer](#the-claude-native-layer) — agents, skills, MCP
- [The autonomous pipeline](#the-autonomous-pipeline) — batch builds
- [Skeletons](#skeletons)
- [Environment variables](#environment-variables)
- [Repository layout](#repository-layout)
- [Running the tests](#running-the-tests)

---

## Install by hand

What the bootstrap above does, if you would rather do it yourself.

Requires Python 3.10+, Docker with the compose plugin, and `git`.

```bash
git clone git@github.com:coldicefisher/bizniz-harness.git ~/bizniz
cd ~/bizniz
python3 -m venv .venv
.venv/bin/pip install -e .
```

That puts a `bizniz` executable in `.venv/bin/`. Inside this repo, call it
as `.venv/bin/bizniz`; add the venv to your `PATH` if you want it bare.

Model backends read their keys from `.env` in this directory
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`). The Claude CLI
backend uses your local `claude` login instead of a key, and is the
default for agent work.

**Optional, for mobile projects:** Node with `npx`, the Android SDK
(`ANDROID_HOME`), an emulator AVD, and `maestro` on `PATH`. Nothing else
needs them; the web path is unaffected if they are absent.

### Verifying the install

```bash
./scripts/bootstrap.sh   # prerequisites, skeletons, install, full verify
```

Idempotent, and it exits non-zero listing every problem rather than
stopping at the first. Or check by hand:

```bash
bizniz --help          # nine subcommands
bizniz projects        # lists generated projects, or nothing on a fresh machine
```

### Skeleton access is not optional

The skeleton repos are private and are cloned **over SSH, on demand**.
Confirm `ssh -T git@github.com` greets you by name before your first
build, because a failed skeleton clone does not stop a build: the
Provisioner catches it and generates the service from scratch instead, so
you get a stack that comes up cleanly with none of the skeleton's auth
wiring, Docker setup, or routing conventions. `scripts/bootstrap.sh`
checks this up front for exactly that reason.

---

## Quickstart

### I have a generated project and I want to know if it works

This is the common case, and it needs no model at all.

```bash
bizniz up      my_project    # docker compose up -d
bizniz smoke   my_project    # curl every route on the live stack
bizniz test    my_project    # run the suite inside the container
bizniz validate my_project   # AST check for hallucinated symbols
bizniz down    my_project
```

Each one exits non-zero on failure, so they chain:

```bash
bizniz up p && bizniz smoke p && bizniz test p && echo GREEN
```

### I have a broken project and I want it fixed

From a Claude Code session in this repo:

```
/bizniz-review my_project     # read-only: one clustered defect report
/bizniz-fix    my_project     # gate → cluster → dispatch → re-gate until green
```

### I want to build something from nothing

```bash
set -a && source .env && set +a
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_project --plan-only "A tool that does X for Y"
```

`--plan-only` is cheap and shows you the milestone breakdown before you
spend anything. Drop it and add `--auto` to run the whole thing.

---

## The `bizniz` CLI

Nine subcommands. No LLM is involved in any of them. Every one that can
fail exits non-zero when it does, which is what makes them usable as gates
by a script, a CI job, or an agent.

`<project>` accepts either a **slug** (resolved under
`$BIZNIZ_PROJECTS_ROOT`, default `~/bizniz_projects`) or a **path**.

### `bizniz projects`

Lists every generated project and the timestamp of its last recorded run.

```bash
$ bizniz projects
bizniz_console                   no runs
muvnit                           last run 20260812_154244
recipe_v4_v16                    last run 20260520_142004
```

"no runs" means no *driver* run. A project provisioned through the
Provisioner API directly still shows here and still works.

### `bizniz status <project>`

Where a project got to.

```bash
$ bizniz status muvnit
project:  /home/jamey/muvnit
run:      20260812_154244
top phases: plan, architect, provision, auth
```

For a project with no driver run it falls back to the architecture
snapshot in the project database and reports the services and the resolved
compose file instead. Exits 1 only when the project genuinely is not
provisioned.

### `bizniz up <project>` / `bizniz down <project>`

`docker compose up -d` and `docker compose down` against the project's
stack. The compose file is discovered by *content* — the one declaring
this project's services — not by assuming `infra/development/`. That
matters on a host running several stacks: picking the wrong file means
`down` tears down someone else's containers.

### `bizniz smoke <project>`

**The gate that catches "tests pass but the app 500s."** Pure curl against
the live stack, over the host ports compose actually bound:

- backend `/health`
- the public login flow, over the same unauthenticated path the frontend
  uses
- a GET probe on every route registered in the OpenAPI schema

Any 5xx fails the gate.

```bash
bizniz smoke my_project
bizniz smoke my_project --timeout 10
```

| Flag | Meaning |
|---|---|
| `--timeout N` | Per-probe timeout in seconds (default 5) |
| `--service NAME` | Gate a **mobile** workspace instead: release build → `adb install` → Maestro flow |
| `--avd NAME` | Emulator AVD to boot (default `$BIZNIZ_AVD`, else `bizniz`) |
| `--skip-build` | Reuse the existing release APK instead of rebuilding |

Mobile example:

```bash
bizniz smoke muvnit --service mobile --avd pixel7 --skip-build
```

### `bizniz test <project> [cmd...]`

Runs the test suite **inside the running container**, not against a mock
of it. On failure it automatically appends the container logs of the
target service and its dependencies, because the log is almost always
where the real cause is.

```bash
bizniz test my_project                              # default: python -m pytest -q
bizniz test my_project --service backend
bizniz test my_project -- python -m pytest tests/unit -x
```

| Flag | Meaning |
|---|---|
| `--service NAME` | Compose service to run in (default `backend`) |

Expo workspaces are not containerised, so this runs `jest` on the host
instead. It detects that automatically.

### `bizniz validate <path-or-project>`

Static validation that catches the two things models get wrong most often:
**imports that do not resolve** and **attribute access on classes that do
not define the attribute** (`settings.foo_bar` when the config only has
`foo_baz`).

```bash
$ bizniz validate ~/muvnit/backend
SYMBOL VALIDATION PASSED  (41 file(s), 218 import(s) resolved)
validate: PASSED (41 files, 218 resolved, 0 unresolved imports, ...)
```

Accepts a workspace directory, a project slug, or a project path. Given a
project root it defaults to `backend/`. On an Expo workspace it runs `tsc`
and lint instead of the Python AST walker. A workspace with nothing to
check exits 1 rather than reporting a vacuous pass.

### `bizniz perf <args...>`

Delegates to the performance-log analyser (`bizniz.perf_log`): parses run
logs into per-call timing and token counts, renders Markdown or JSON, and
compares two runs for A/B work.

```bash
bizniz perf --help
```

### `bizniz mcp`

Launches the MCP server on stdio. You normally do not run this by hand —
`.mcp.json` starts it per Claude Code session. See below.

---

## The Claude-native layer

This is the primary way the harness is driven: Claude Code runs the loop
and uses the CLI above as its instrument.

### Skills

Invoked as slash commands from a Claude Code session in this repo.

**`/bizniz-fix <project>`** — a convergence loop. Bring the stack up, run
every gate, cluster the failures **by root cause** rather than by symptom,
dispatch a `bizniz-coder` subagent per cause, restart the affected
services, re-gate. It diff-audits every dispatch and stops on stall after
two iterations with no progress, so it cannot spin.

**`/bizniz-review <project>`** — read-only. Runs code review and quality
engineering per service *in parallel*, plus the deterministic gates, and
merges everything into one clustered defect report. Feed that report to
`/bizniz-fix` to act on it.

### Subagents

In `.claude/agents/`. These are the pipeline's role prompts, ported to
Claude Code subagents so they can be dispatched directly.

| Agent | Role |
|---|---|
| `bizniz-coder` | Implements or fixes **one** issue in one workspace. Dispatch with project path, workspace, the issue, files in scope, and the gate command. Pinned to Haiku. |
| `bizniz-architect` | Decomposes a problem statement into a service architecture as JSON. Advisory only — the Provisioner still materialises. |
| `bizniz-code-reviewer` | Cold-reads source for hallucinated symbols, ungated auth, and missing error handling. Returns findings, never edits. |
| `bizniz-quality-engineer` | Checks that **tests** cover the spec, and never reads source. That bias firewall is deliberate: a reviewer who has read the implementation grades the tests against what the code does rather than against what was asked for. |

### MCP server

`.mcp.json` starts `bizniz mcp` per session. Five tools, each taking an
optional `project` argument so they work session-wide:

| Tool | Returns |
|---|---|
| `get_prior_issues` | What has already been implemented in this project |
| `get_issue_test_output` | The last test run for a given issue |
| `validate_python_imports` | The AST validator, callable mid-task |
| `read_audit_findings` | Recorded review findings |
| `read_auth_contract` | The project's auth contract |

---

## The autonomous pipeline

The batch path. Runs the whole agent chain unattended: Planner →
Architect → Provisioner (plus the FusionAuth agent) → per-service Engineer
→ Coder/Tester/Debugger loops → quality-engineering and code-review repair
→ integration tests against the live stack → UX review → refactor.

**Canonical entry point: `examples/v2_build.py`.**

```bash
cd ~/bizniz && set -a && source .env && set +a

# Plan only — cheap dry run, shows the milestone breakdown
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_project --plan-only "<problem statement>"

# Full build
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_project --auto "<problem statement>"

# One milestone
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_project --milestone 2 --auto "<problem statement>"

# One phase over existing run state
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_project --milestone 2 --phase integration_api

# Resume the most recent run
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_project --resume --auto
```

Useful flags:

| Flag | Effect |
|---|---|
| `--plan-only` | Run the Planner and stop |
| `--milestone N` | Run through milestone N, 1-indexed, inclusive |
| `--phase NAME` | Run a single phase and exit. Top: `plan`, `architect`, `provision`, `auth`. Per-milestone (needs `--milestone`): `enrich`, `implement`, `review_initial`, `review_final`, `repair_iter_0..2`, `integration_api`, `integration_web` |
| `--resume` / `--resume-job-id ID` | Continue an existing run |
| `--auto` | Push through soft gates with a warning |
| `--interactive` | Halt at every gate for review |
| `--retry-failed` | Reset failed issues to pending and re-attempt, without paying to re-plan |
| `--retry-service NAME` | Restrict `--retry-failed` to one service |
| `--use-v3` / `--use-v4` / `--use-v5` | Opt into newer dispatch strategies, for A/B runs |
| `--decompose` | Opt into per-unit issue decomposition (off by default: measured 3-4× slower with no quality gain) |

Output lands in `$BIZNIZ_PROJECTS_ROOT/<slug>/` — running code, tests,
`SKELETON.md` contracts, and run state under `.bizniz/`.

---

## Skeletons

Projects are seeded from contract-bearing starter repos, cloned to
`$BIZNIZ_SKELETONS_DIR` (default `~/`):

| Skeleton | Stack |
|---|---|
| `bizniz-skeleton-fastapi` | FastAPI backend, auth delegated to FusionAuth, pytest |
| `bizniz-skeleton-react` | React + Vite frontend |
| `bizniz-skeleton-angular` | Angular + Material |
| `bizniz-skeleton-expo` | Expo / React Native, gated by `tsc`, `jest`, lint, Maestro |
| `bizniz-skeleton-teams` | Multi-service team template |
| `bizniz-skeleton-saas` | SaaS starter |

Each ships a `SKELETON.md` **hard contract**: the auto-discovery
conventions, which files may be edited, and the accumulated field lessons.
Two rules matter most and the whole system depends on them:

1. **Auto-discovery.** FastAPI auto-mounts `app/api/routes/*.py` exporting
   a `router`; React auto-mounts `src/routes/*.tsx` exporting a
   `RouteEntry`. Both warn loudly on a malformed module rather than
   skipping it silently.
2. **Non-destructive editing.** Files outside a skeleton's declared
   extension points are not to be rewritten. A generated app that edits
   shipped files is a generated app nobody can regenerate.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BIZNIZ_PROJECTS_ROOT` | `~/bizniz_projects` | Where generated projects live, and how slugs resolve |
| `BIZNIZ_SKELETONS_DIR` | `~/` | Where skeleton repos are cloned |
| `BIZNIZ_AVD` | `bizniz` | Default Android emulator for mobile smoke |
| `ANDROID_HOME` | — | Android SDK, for mobile gates only |
| `BIZNIZ_CLAUDE_FALLBACK_MODEL` | unset | Appends `--fallback-model` to every Claude CLI call, so a long build survives an overloaded primary |
| `BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S` | 6h | How long to sleep when a rate-limit response names a reset time. Set to 0 to fail fast instead |

---

## Repository layout

```
bizniz/
  cli.py                 the gate surface
  architect/             problem statement → SystemArchitecture
  provisioner/           architecture → project on disk (skeletons,
                         compose, Dockerfiles, FusionAuth kickstart)
  driver/                phase orchestration: smoke, integration,
                         review/repair, refactor, UX
  coder/ tester/ agents/ the implement and debug loops
  integration/           API, worker and web-UI integration testers
  ux_designer/           screenshot capture + vision evaluation
  mcp_server/            the MCP tools listed above
  clients/               pluggable LLM backends (Claude CLI, Claude API,
                         OpenAI, Gemini)
  perf_log/              run timing and token analysis

.claude/agents/          role subagents
.claude/skills/          /bizniz-fix, /bizniz-review
examples/                v2_build.py and friends
tests/regression/        cross-cutting regressions with the story attached
docs/changes/            session narratives, newest last
CLAUDE.md                current state, invariants, and what not to do
```

---

## Running the tests

```bash
# Everything (functional tests, which call real APIs, are excluded)
.venv/bin/python -m pytest bizniz/ tests/ -q

# One module
.venv/bin/python -m pytest bizniz/provisioner/tests/ -q

# One test by name
.venv/bin/python -m pytest bizniz/engineer/tests/ -k "cycle" -q

# The functional tests, which do call real APIs and cost money
.venv/bin/python -m pytest -m functional -q
```

Unit tests live beside the module they cover, in `bizniz/<module>/tests/`.
Cross-cutting regressions live in `tests/regression/`, and each one opens
with the story of the bug it prevents — if you are about to change
behaviour those files describe, read the docstring first. It will tell you
what broke last time.

---

## Where to read next

`CLAUDE.md` carries the current session state, the working method, and the
list of things not to do and why. `docs/changes/` holds the narrative of
how the system got here, newest last.
