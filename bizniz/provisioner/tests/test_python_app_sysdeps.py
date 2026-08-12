"""System-dependency injection in the generated Python Dockerfile."""
from bizniz.provisioner.templates.app_python import _generate_dockerfile


def test_pytesseract_requirement_installs_tesseract():
    df = _generate_dockerfile(8000, "worker", ["pytesseract", "pillow"])
    assert "apt-get install" in df
    assert "tesseract-ocr" in df and "tesseract-ocr-eng" in df
    # apt layer must come before pip so caching works and pip sees a
    # complete system.
    assert df.index("apt-get") < df.index("pip install")


def test_no_system_deps_no_apt_layer():
    df = _generate_dockerfile(8000, "backend", ["fastapi", "uvicorn"])
    assert "apt-get" not in df


def test_versioned_requirement_still_matches():
    df = _generate_dockerfile(8000, "worker", ["pytesseract>=0.3"])
    assert "tesseract-ocr" in df
