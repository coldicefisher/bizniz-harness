"""Tests for skeleton post-seed substitutions."""
from pathlib import Path

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.skeleton_substitutions import apply_substitutions


def _arch(services):
    return SystemArchitecture(
        project_name="t", project_slug="t",
        services=services, description="",
    )


def _backend(name: str = "backend", framework: str = "fastapi") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type="backend", framework=framework,
        language="python", description="", workspace_name=name, port=8000,
    )


def _frontend(name: str = "frontend") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type="frontend", framework="react",
        language="typescript", description="", workspace_name=name, port=5173,
    )


class TestReactViteProxySubstitution:
    def test_rewrites_default_target_to_actual_backend(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'export default defineConfig({\n'
            '  server: {\n'
            '    proxy: {\n'
            '      "/api": {\n'
            '        target: "http://api:8000",\n'
            '        changeOrigin: true,\n'
            '      },\n'
            '    },\n'
            '  },\n'
            '});\n'
        )
        arch = _arch([_backend("backend"), _frontend()])
        fe = _frontend()
        applied = apply_substitutions("react", ws, arch, fe)
        assert applied, "expected the vite proxy substitution to fire"
        new_content = (ws / "vite.config.ts").read_text()
        assert 'target: "http://backend:8000"' in new_content
        assert 'http://api:8000' not in new_content

    def test_uses_correct_backend_name_when_not_api(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        arch = _arch([_backend("core-service"), _frontend()])
        fe = _frontend()
        apply_substitutions("react", ws, arch, fe)
        assert 'http://core-service:8000' in (ws / "vite.config.ts").read_text()

    def test_skips_when_no_backend_in_arch(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        arch = _arch([_frontend()])  # frontend-only project
        fe = _frontend()
        applied = apply_substitutions("react", ws, arch, fe)
        # Substitution was tried but skipped (no backend service).
        assert applied == []
        # Original content unchanged.
        assert 'http://api:8000' in (ws / "vite.config.ts").read_text()

    def test_skips_when_file_missing(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        # No vite.config.ts at all.
        arch = _arch([_backend(), _frontend()])
        applied = apply_substitutions("react", ws, arch, _frontend())
        assert applied == []

    def test_skips_when_pattern_not_found(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        # vite.config.ts exists but doesn't have the default pattern
        # (e.g. someone already edited it).
        (ws / "vite.config.ts").write_text(
            'target: "http://something-else:9999",\n'
        )
        arch = _arch([_backend(), _frontend()])
        applied = apply_substitutions("react", ws, arch, _frontend())
        assert applied == []
        # Don't mangle a custom config.
        assert 'http://something-else:9999' in (ws / "vite.config.ts").read_text()

    def test_unknown_skeleton_is_noop(self, tmp_path):
        ws = tmp_path / "x"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        arch = _arch([_backend(), _frontend()])
        applied = apply_substitutions("totally-unknown", ws, arch, _frontend())
        assert applied == []

    def test_status_callback_logs_substitutions(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        msgs = []
        apply_substitutions(
            "react", ws, _arch([_backend(), _frontend()]),
            _frontend(),
            on_status=msgs.append,
        )
        assert any("applied skeleton substitution" in m for m in msgs)
        assert any("vite.config.ts" in m for m in msgs)


def _mobile(name: str = "mobile") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type="mobile", framework="expo",
        language="typescript", description="", workspace_name=name,
    )


class TestExpoIdentitySubstitutions:
    """Generated mobile apps must NOT share the skeleton's app identity
    (same Android package id ⇒ installs replace each other)."""

    def _seed(self, tmp_path: Path) -> Path:
        ws = tmp_path / "mobile"
        (ws / ".maestro").mkdir(parents=True)
        (ws / "app.json").write_text(
            '{\n  "expo": {\n'
            '    "name": "bizniz-skeleton-expo",\n'
            '    "slug": "bizniz-skeleton-expo",\n'
            '    "android": {\n'
            '      "package": "com.coldicefisher.biznizskeletonexpo"\n'
            '    }\n  }\n}\n'
        )
        (ws / ".maestro" / "smoke.yaml").write_text(
            "appId: com.coldicefisher.biznizskeletonexpo\n---\n- launchApp\n"
        )
        return ws

    def test_identity_rewritten_per_project(self, tmp_path):
        ws = self._seed(tmp_path)
        arch = SystemArchitecture(
            project_name="Flirpie", project_slug="flirpie",
            services=[_backend(), _mobile()], description="",
        )
        applied = apply_substitutions("expo", ws, arch, _mobile())
        app_json = (ws / "app.json").read_text()
        assert '"name": "Flirpie"' in app_json
        assert '"slug": "flirpie"' in app_json
        assert '"package": "com.coldicefisher.flirpie"' in app_json
        assert "biznizskeletonexpo" not in app_json
        smoke = (ws / ".maestro" / "smoke.yaml").read_text()
        assert "appId: com.coldicefisher.flirpie" in smoke
        assert len(applied) == 4

    def test_package_segment_sanitized(self, tmp_path):
        ws = self._seed(tmp_path)
        arch = SystemArchitecture(
            project_name="2Fast 4U", project_slug="2fast-4u",
            services=[_mobile()], description="",
        )
        apply_substitutions("expo", ws, arch, _mobile())
        app_json = (ws / "app.json").read_text()
        assert '"package": "com.coldicefisher.app2fast_4u"' in app_json


class TestAngularIdentitySubstitution:
    """The angular skeleton ships its own name in the two places a user
    reads a product's name from: the browser tab and the top bar.

    Nothing else rewrites them — they are not service references, so the
    react proxy substitution has no reason to touch them and no gate
    inspects rendered text. bizniz-tycoon's management console therefore
    shipped complete and fully tested with a tab reading "App". It was
    found by opening the site, which is the same way the root-route
    placeholder was found, and for the same reason: scaffolding that
    renders as success is invisible to everything except a pair of eyes.
    """

    def _angular_workspace(self, tmp_path):
        ws = tmp_path / "frontend"
        (ws / "src" / "app" / "shared" / "layout" / "topbar").mkdir(parents=True)
        (ws / "src" / "index.html").write_text(
            "<!doctype html>\n<html>\n<head>\n"
            "  <title>App</title>\n</head>\n<body></body>\n</html>\n")
        (ws / "src" / "app" / "shared" / "layout" / "topbar"
         / "topbar.component.html").write_text(
            '<mat-toolbar>\n'
            '    <span class="topbar-brand clickable" routerLink="/">App</span>\n'
            '</mat-toolbar>\n')
        return ws

    def _apply(self, ws, project_name="Bizniz Tycoon Console"):
        arch = SystemArchitecture(
            project_name=project_name, project_slug="console",
            services=[_frontend()], description="",
        )
        return apply_substitutions("angular", ws, arch, _frontend())

    def test_browser_tab_title_becomes_the_project_name(self, tmp_path):
        ws = self._angular_workspace(tmp_path)
        self._apply(ws)
        html = (ws / "src" / "index.html").read_text()
        assert "<title>Bizniz Tycoon Console</title>" in html
        assert "<title>App</title>" not in html

    def test_topbar_brand_becomes_the_project_name(self, tmp_path):
        ws = self._angular_workspace(tmp_path)
        self._apply(ws)
        bar = (ws / "src" / "app" / "shared" / "layout" / "topbar"
               / "topbar.component.html").read_text()
        assert ">Bizniz Tycoon Console</span>" in bar
        assert ">App</span>" not in bar

    def test_both_files_are_reported_as_applied(self, tmp_path):
        """A substitution that silently no-ops is the failure mode this
        module's own docstring warns about — the skeleton moves, the
        pattern stops matching, and nobody notices for a release."""
        ws = self._angular_workspace(tmp_path)
        applied = self._apply(ws)
        assert len(applied) == 2, applied

    def test_missing_file_is_survivable(self, tmp_path):
        """Skeleton layouts drift between versions; a missing file must
        skip, not crash a provision that is otherwise fine."""
        ws = tmp_path / "frontend"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "index.html").write_text("  <title>App</title>\n")
        applied = self._apply(ws)
        assert applied == ["src/index.html:<title>App</title>"] or len(applied) == 1

    def test_the_real_skeleton_still_matches_these_patterns(self):
        """Positive control against the shipped skeleton. If someone edits
        the skeleton's markup, these substitutions turn into silent no-ops
        and every future app is called "App" again."""
        from bizniz.architect.skeletons import skeletons_root
        root = skeletons_root() / "bizniz-skeleton-angular"
        if not root.exists():
            pytest.skip("angular skeleton not present")
        assert "<title>App</title>" in (root / "src" / "index.html").read_text()
        bar = (root / "src" / "app" / "shared" / "layout" / "topbar"
               / "topbar.component.html").read_text()
        assert 'routerLink="/">App</span>' in bar
