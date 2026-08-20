# Getting started on a new machine

**The prompt to paste lives at the top of [README.md](README.md).** Paste
it into a fresh Claude Code session from any directory and it will clone
the repo, run `scripts/bootstrap.sh`, and report.

This file is the detail behind it: what each step checks, why the GitHub
SSH check is separate, and how to do the whole thing by hand.

It is deliberately not a second copy of the prompt. Two copies drift, and
the one someone pastes would be whichever they found first.

## Why the restart matters

The repo's subagents (`.claude/agents/`), skills (`.claude/skills/`), and
MCP server (`.mcp.json`) are only loaded for a Claude Code session whose
working directory is the repo. A session started elsewhere can run the
`bizniz` CLI perfectly well but will not have `/bizniz-fix`,
`/bizniz-review`, or the MCP tools.

So after the bootstrap passes:

```bash
cd ~/bizniz && claude
```

## What the script actually does

Everything below is idempotent, so re-run it whenever a machine looks
wrong.

| Step | Checks |
|---|---|
| Prerequisites | Python 3.10+, git, a reachable Docker daemon, the compose **plugin** (not the v1 binary). Warns on missing `node` and `claude` |
| GitHub access | `ssh -T git@github.com` actually authenticates. This is checked separately because skeletons clone over SSH, not through `gh` |
| Skeletons | Clones all six `bizniz-skeleton-*` repos into `$BIZNIZ_SKELETONS_DIR` (default `~/`). Existing clones are left alone |
| Install | Creates `.venv`, installs the package editable |
| Verify | Runs the CLI, imports the core subpackages, runs the full test suite, creates the projects root |

It exits non-zero if anything required failed, and prints one summary
listing every problem rather than stopping at the first.

## Doing it by hand

```bash
git clone git@github.com:coldicefisher/bizniz-harness.git ~/bizniz
cd ~/bizniz
python3 -m venv .venv
.venv/bin/pip install -e .
./scripts/bootstrap.sh
```

## If SSH to GitHub is the blocker

The skeleton repos are private and cloned over SSH. `gh auth login` alone
is not enough — it authenticates the `gh` tool, not `git` over SSH.

```bash
ssh-keygen -t ed25519 -C "you@example.com"     # if you have no key
gh ssh-key add ~/.ssh/id_ed25519.pub           # or paste it into GitHub settings
ssh -T git@github.com                          # should greet you by username
```

You also need read access to all six `bizniz-skeleton-*` repositories.
