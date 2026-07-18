"""Regression test for runtime-only deterministic release archive."""

import subprocess
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESOURCES_ROOT = ROOT / "resources"
ADDON_XML = ROOT / "addon.xml"
ADDON_PY = ROOT / "addon.py"


EXPECTED_ROOT = "plugin.video.gronkhtv"
ALLOWED_ROOT_MEMBERS = {"addon.xml", "addon.py", "resources"}
FORBIDDEN_PATTERNS = [
    "README",
    "LICENSE",
    "test",
    "tests",
    ".github",
    "workflows",
    ".hermes",
    ".pyc",
    "__pycache__",
    ".git",
    ".gitignore",
]


def build_archive(source_dir: Path, output_zip: Path) -> None:
    """Build release archive using the deterministic build script."""
    script = ROOT / "scripts" / "build_release.py"
    result = subprocess.run(
        ["python3", str(script), "--source", str(source_dir), "--output", str(output_zip)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Build failed: {result.stderr}")


def test_archive_members_are_runtime_only():
    """RED test: Archive must contain ONLY runtime files under single root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output = Path(tmpdir) / "release.zip"
        build_archive(ROOT, output)

        with zipfile.ZipFile(output, "r") as zf:
            members = zf.namelist()

        # Must have exactly one root directory
        roots = {m.split("/")[0] for m in members if "/" in m}
        assert roots == {EXPECTED_ROOT}, f"Expected single root {EXPECTED_ROOT}, got {roots}"

        # Root members must be only allowed ones
        root_members = {m.split("/")[1] for m in members if m.startswith(f"{EXPECTED_ROOT}/") and m.count("/") == 1}
        forbidden_at_root = root_members - ALLOWED_ROOT_MEMBERS
        assert not forbidden_at_root, f"Forbidden root members: {forbidden_at_root}"

        # No forbidden patterns anywhere
        for pattern in FORBIDDEN_PATTERNS:
            matches = [m for m in members if pattern.lower() in m.lower()]
            assert not matches, f"Forbidden pattern '{pattern}' found in: {matches}"

        # Must have addon.xml and addon.py at root
        assert f"{EXPECTED_ROOT}/addon.xml" in members
        assert f"{EXPECTED_ROOT}/addon.py" in members

        # Must have resources/ directory
        assert any(m.startswith(f"{EXPECTED_ROOT}/resources/") for m in members)


def test_archive_deterministic():
    """Archive must be byte-identical across two independent builds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output1 = Path(tmpdir) / "release1.zip"
        output2 = Path(tmpdir) / "release2.zip"
        build_archive(ROOT, output1)
        build_archive(ROOT, output2)

        hash1 = output1.read_bytes()
        hash2 = output2.read_bytes()
        assert hash1 == hash2, "Archive builds are not byte-identical"


def test_embedded_addon_xml_matches():
    """Embedded addon.xml must have correct id and version."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output = Path(tmpdir) / "release.zip"
        build_archive(ROOT, output)

        with zipfile.ZipFile(output, "r") as zf:
            with zf.open(f"{EXPECTED_ROOT}/addon.xml") as f:
                content = f.read().decode("utf-8")

        assert 'id="plugin.video.gronkhtv"' in content
        assert 'version="2.3.1"' in content


def test_no_bytecode_in_source_tree():
    """Verify no .pyc or __pycache__ in tracked source."""
    pyc_files = list(ROOT.rglob("*.pyc"))
    pycache_dirs = list(ROOT.rglob("__pycache__"))
    assert not pyc_files, f"Found .pyc files: {pyc_files}"
    assert not pycache_dirs, f"Found __pycache__ dirs: {pycache_dirs}"


def test_workflow_yaml_parses():
    """All workflow YAML files must parse with PyYAML."""
    import yaml

    workflows = ROOT / ".github" / "workflows"
    for wf in workflows.glob("*.yml"):
        with open(wf, "r") as f:
            yaml.safe_load(f)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))