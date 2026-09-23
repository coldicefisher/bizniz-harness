"""Render a host profile as the document a person (or an agent) reads."""
from __future__ import annotations

from bizniz.discovery.types import Claim, HostProfile

MARK = {"verified": "✓", "asserted": "·", "absent": "—"}


def _line(label: str, claim: Claim) -> str:
    mark = MARK[claim.evidence]
    if claim.evidence == "absent":
        return f"| {mark} | **{label}** | — | {claim.how} |"
    value = claim.value
    if isinstance(value, dict):
        shown = ", ".join(f"`{k}`" for k in list(value)[:6]) + (" …" if len(value) > 6 else "")
    elif isinstance(value, list):
        shown = ", ".join(f"`{v}`" for v in value[:8]) + (" …" if len(value) > 8 else "")
    else:
        shown = f"`{value}`"
    note = f" {claim.note}" if claim.note else ""
    return f"| {mark} | **{label}** | {shown} | {claim.how}.{note} |"


def _table(rows: list[str]) -> str:
    return "\n".join(["| | What | Value | How it was established |", "|---|---|---|---|", *rows])


def render(profile: HostProfile) -> str:
    ok, total = profile.coverage()
    out: list[str] = []
    a = out.append

    a(f"# {profile.host} — host profile\n")
    a(f"What an app must conform to in order to live inside **{profile.host}**, "
      "written from the repository and checked against the running stack.\n")
    a(f"Generated {profile.generated_at} from `{profile.root}`. "
      f"**{ok} of {total}** claims verified against something running; the rest are read "
      "from files and not confirmed.\n")
    a("`✓` verified — a command ran or an endpoint answered. "
      "`·` asserted — read from a file, unconfirmed. `—` looked for, not found.\n")

    a("## Stack\n")
    a(_table([
        _line("dev compose", profile.stack.compose_dev),
        _line("prod compose", profile.stack.compose_prod),
        _line("services", profile.stack.services),
        _line("shared network", profile.stack.network),
        _line("hosted-app includes", profile.stack.includes),
        _line("running now", profile.stack.running),
    ]))

    a("\n## Proxy — how a hosted app is reached\n")
    a(_table([
        _line("proxy service", profile.proxy.service),
        _line("dev base URL", profile.proxy.dev_base_url),
        _line("snippet mount", profile.proxy.locations_dir),
        _line("mount convention", profile.proxy.mount_convention),
    ]))

    a("\n## Identity — the auth contract\n")
    a("A hosted app does not stand up its own identity provider. It verifies bearer tokens "
      "issued by the host's, against the keys below.\n")
    a(_table([
        _line("provider", profile.identity.provider),
        _line("realm", profile.identity.realm_url),
        _line("JWKS", profile.identity.jwks_url),
        _line("issuer", profile.identity.issuer),
        _line("algorithms", profile.identity.algorithms),
        _line("accepted audiences", profile.identity.audiences),
        _line("roles claim", profile.identity.roles_claim),
        _line("roles in use", profile.identity.roles),
        _line("session endpoint", profile.identity.session_endpoint),
        _line("reference implementation", profile.identity.reference_impl),
    ]))

    a("\n## Frontend conventions\n")
    a(_table([
        _line("framework", profile.frontend.framework),
        _line("version", profile.frontend.version),
        _line("styles entry", profile.frontend.styles_entry),
        _line("design tokens", profile.frontend.design_tokens),
        _line("navigation wiring", profile.frontend.nav_wiring),
    ]))

    a("\n## Build and gates\n")
    a(_table([
        _line("bake file", profile.build.bake_file),
        _line("bake targets", profile.build.targets),
        _line("CI scripts", profile.build.ci_scripts),
        _line("existing gates", profile.build.gates),
    ]))

    a("\n## Carve-off boundary\n")
    a("A hosted app depends on the host through the network, the identity provider and the "
      "proxy — never by importing its code. `bizniz boundary` enforces this.\n")
    a(_table([
        _line("host code roots", profile.boundary.code_roots),
        _line("host packages", profile.boundary.packages),
        _line("build internals", profile.boundary.build_internals),
        _line("integration paths", profile.boundary.integration_paths),
    ]))

    if profile.hosted_apps:
        a("\n## Apps already hosted here\n")
        a("These conform to the contract above. Copy them rather than inventing a new shape.\n")
        for app in profile.hosted_apps:
            a(f"\n### {app.name}\n")
            a(_table([
                _line("path prefix", app.path_prefix),
                _line("reachable", app.reachable),
                _line("services", app.services),
                _line("compose files", app.compose_files),
                _line("nginx snippet", app.nginx_snippet),
                _line("bake targets", app.bake_targets),
            ]))

    if profile.gaps:
        a("\n## What could not be established\n")
        a("Each of these is a question, not a finding of absence.\n")
        for gap in profile.gaps:
            a(f"- {gap}")

    a("\n## Using this profile\n")
    a("A new hosted app conforms by: joining the shared network rather than publishing ports, "
      "mounting its own nginx snippet into the proxy's snippet directory, verifying bearer "
      "tokens against the host's JWKS rather than standing up an identity provider, adding its "
      "own bake targets, and keeping every dependency on the host to network hostnames so it "
      "can be carved off later.\n")
    return "\n".join(out) + "\n"
