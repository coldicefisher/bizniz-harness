"""Types for host discovery.

A *host* is an existing system a new app is going to live inside — Conduit, say.
Discovery writes down what that host requires of anything hosted in it, so the
planner and the coder work against real conventions instead of inventing their own.

Everything discovery says is a :class:`Claim`, and a claim knows how it was
established. ``verified`` means a command ran or an endpoint answered; ``asserted``
means it was read out of a file and nothing has confirmed it is true at runtime.
That distinction is the whole point: a profile that confidently states the wrong
test command is exactly the failure the gates exist to catch, so the profile holds
itself to the same standard.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Evidence = Literal["verified", "asserted", "absent"]


class Claim(BaseModel):
    """One fact about the host, with how it was established."""

    value: Any = None
    evidence: Evidence = "asserted"
    #: The command run, endpoint probed, or file read.
    how: str = ""
    #: Repo-relative path the claim came from, when it came from a file.
    source: Optional[str] = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.evidence == "verified"

    def __bool__(self) -> bool:      # `if profile.identity.jwks_url:`
        return self.value not in (None, "", [], {})


def asserted(value: Any, source: str, note: str = "") -> Claim:
    return Claim(value=value, evidence="asserted", how=f"read {source}",
                 source=source, note=note)


def verified(value: Any, how: str, source: Optional[str] = None, note: str = "") -> Claim:
    return Claim(value=value, evidence="verified", how=how, source=source, note=note)


def absent(how: str, note: str = "") -> Claim:
    return Claim(value=None, evidence="absent", how=how, note=note)


class HostedApp(BaseModel):
    """An app already hosted in this host — the working examples to conform to."""

    name: str
    path_prefix: Claim = Field(default_factory=Claim)     # "/chat/"
    services: Claim = Field(default_factory=Claim)        # container names
    compose_files: Claim = Field(default_factory=Claim)
    nginx_snippet: Claim = Field(default_factory=Claim)
    bake_targets: Claim = Field(default_factory=Claim)
    reachable: Claim = Field(default_factory=Claim)       # HTTP status through the proxy


class Stack(BaseModel):
    compose_dev: Claim = Field(default_factory=Claim)
    compose_prod: Claim = Field(default_factory=Claim)
    services: Claim = Field(default_factory=Claim)
    network: Claim = Field(default_factory=Claim)         # the shared external network
    running: Claim = Field(default_factory=Claim)         # containers up right now
    includes: Claim = Field(default_factory=Claim)        # compose `include:` entries


class Proxy(BaseModel):
    service: Claim = Field(default_factory=Claim)
    dev_base_url: Claim = Field(default_factory=Claim)
    locations_dir: Claim = Field(default_factory=Claim)   # where a hosted app drops its snippet
    mount_convention: Claim = Field(default_factory=Claim)


class Identity(BaseModel):
    """What a hosted app must do to authenticate a caller."""

    provider: Claim = Field(default_factory=Claim)
    realm_url: Claim = Field(default_factory=Claim)
    jwks_url: Claim = Field(default_factory=Claim)
    issuer: Claim = Field(default_factory=Claim)
    algorithms: Claim = Field(default_factory=Claim)
    audiences: Claim = Field(default_factory=Claim)         # aud values apps accept
    roles_claim: Claim = Field(default_factory=Claim)       # where roles are read from
    roles: Claim = Field(default_factory=Claim)
    session_endpoint: Claim = Field(default_factory=Claim)  # host endpoint apps reuse
    reference_impl: Claim = Field(default_factory=Claim)    # a working verifier in-tree


class Frontend(BaseModel):
    framework: Claim = Field(default_factory=Claim)
    version: Claim = Field(default_factory=Claim)
    styles_entry: Claim = Field(default_factory=Claim)
    design_tokens: Claim = Field(default_factory=Claim)     # CSS custom properties
    nav_wiring: Claim = Field(default_factory=Claim)        # where a sidebar link is added


class Boundary(BaseModel):
    """What separates a hosted app from the host, so it can still be carved off."""

    code_roots: Claim = Field(default_factory=Claim)      # where the host's own code lives
    packages: Claim = Field(default_factory=Claim)        # importable names a hosted app must not use
    build_internals: Claim = Field(default_factory=Claim)  # shared base images, build context
    integration_paths: Claim = Field(default_factory=Claim)  # where naming the app IS allowed


class Build(BaseModel):
    bake_file: Claim = Field(default_factory=Claim)
    targets: Claim = Field(default_factory=Claim)
    ci_scripts: Claim = Field(default_factory=Claim)
    gates: Claim = Field(default_factory=Claim)             # gate commands the host already has


class HostProfile(BaseModel):
    """What a new app must conform to in order to live inside this host."""

    host: str
    root: str
    generated_at: str
    stack: Stack = Field(default_factory=Stack)
    proxy: Proxy = Field(default_factory=Proxy)
    identity: Identity = Field(default_factory=Identity)
    frontend: Frontend = Field(default_factory=Frontend)
    build: Build = Field(default_factory=Build)
    boundary: Boundary = Field(default_factory=Boundary)
    hosted_apps: list[HostedApp] = Field(default_factory=list)
    #: Things discovery looked for and could not establish — the honest gaps.
    gaps: list[str] = Field(default_factory=list)

    def claims(self) -> list[tuple[str, Claim]]:
        """Every claim in the profile, as dotted paths."""
        out: list[tuple[str, Claim]] = []

        def walk(prefix: str, model: BaseModel) -> None:
            for name, value in model:
                if isinstance(value, Claim):
                    out.append((f"{prefix}{name}", value))
                elif isinstance(value, BaseModel):
                    walk(f"{prefix}{name}.", value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, HostedApp):
                            walk(f"{prefix}{name}.{item.name}.", item)

        walk("", self)
        return out

    def coverage(self) -> tuple[int, int]:
        """(verified, total) over claims that found something.

        A claim that was never populated is neither found nor missing — it is simply
        not part of this profile, so it stays out of the denominator. Only `absent`
        means discovery looked and came back empty.
        """
        found = [c for _, c in self.claims()
                 if c.evidence == "verified" or (c.evidence == "asserted" and bool(c))]
        return sum(1 for c in found if c.ok), len(found)
