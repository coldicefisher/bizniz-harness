# Getting started on a new machine

Paste the block below into a fresh Claude Code session. It works from any
directory — the session does not need to be in this repo yet.

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
