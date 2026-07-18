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


def test_zip_contains_no_bytecode():
    """Built ZIP must contain no .pyc or __pycache__ entries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output = Path(tmpdir) / "release.zip"
        build_archive(ROOT, output)

        with zipfile.ZipFile(output, "r") as zf:
            members = zf.namelist()

        pyc_members = [m for m in members if m.endswith(".pyc") or "__pycache__" in m]
        assert not pyc_members, f"ZIP contains bytecode entries: {pyc_members}"


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


# ===== Adversarial regression tests (RED first) =====

def test_collect_rejects_symlinked_addon_xml(tmp_path):
    """collect_runtime_files must reject symlinked addon.xml with nonzero exit."""
    from scripts.build_release import collect_runtime_files
    source = tmp_path / "source"
    source.mkdir()
    real_xml = source / "real_addon.xml"
    real_xml.write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    (source / "addon.xml").symlink_to(real_xml)
    (source / "addon.py").write_text("pass")
    resources = source / "resources"
    resources.mkdir()
    (resources / "test.txt").write_text("test")

    try:
        collect_runtime_files(source)
        assert False, "Expected ValueError for symlinked addon.xml"
    except ValueError as e:
        assert "symlink" in str(e).lower() or "not a regular file" in str(e).lower()


def test_collect_rejects_symlinked_addon_py(tmp_path):
    """collect_runtime_files must reject symlinked addon.py with nonzero exit."""
    from scripts.build_release import collect_runtime_files
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    real_py = source / "real_addon.py"
    real_py.write_text("pass")
    (source / "addon.py").symlink_to(real_py)
    resources = source / "resources"
    resources.mkdir()
    (resources / "test.txt").write_text("test")

    try:
        collect_runtime_files(source)
        assert False, "Expected ValueError for symlinked addon.py"
    except ValueError as e:
        assert "symlink" in str(e).lower() or "not a regular file" in str(e).lower()


def test_collect_rejects_symlinked_resources_dir(tmp_path):
    """collect_runtime_files must reject symlinked resources directory."""
    from scripts.build_release import collect_runtime_files
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    (source / "addon.py").write_text("pass")
    real_resources = source / "real_resources"
    real_resources.mkdir()
    (real_resources / "test.txt").write_text("test")
    (source / "resources").symlink_to(real_resources)

    try:
        collect_runtime_files(source)
        assert False, "Expected ValueError for symlinked resources directory"
    except ValueError as e:
        assert "symlink" in str(e).lower() or "not a regular file" in str(e).lower()


def test_collect_rejects_symlinked_file_under_resources(tmp_path):
    """collect_runtime_files must reject symlinked file under resources."""
    from scripts.build_release import collect_runtime_files
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    (source / "addon.py").write_text("pass")
    resources = source / "resources"
    resources.mkdir()
    real_file = source / "real_file.txt"
    real_file.write_text("test")
    (resources / "test.txt").symlink_to(real_file)

    try:
        collect_runtime_files(source)
        assert False, "Expected ValueError for symlinked file under resources"
    except ValueError as e:
        assert "symlink" in str(e).lower() or "not a regular file" in str(e).lower()


def test_collect_rejects_symlinked_dir_under_resources(tmp_path):
    """collect_runtime_files must reject symlinked directory under resources."""
    from scripts.build_release import collect_runtime_files
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    (source / "addon.py").write_text("pass")
    resources = source / "resources"
    resources.mkdir()
    real_dir = source / "real_dir"
    real_dir.mkdir()
    (real_dir / "nested.txt").write_text("test")
    (resources / "symlink_dir").symlink_to(real_dir)

    try:
        collect_runtime_files(source)
        assert False, "Expected ValueError for symlinked directory under resources"
    except ValueError as e:
        assert "symlink" in str(e).lower() or "not a regular file" in str(e).lower()


# ===== Path collision normalization tests (RED first) =====

def test_collect_rejects_unicode_normalization_collision(tmp_path):
    """collect_runtime_files + validate must reject paths that collide after Unicode NFC normalization + casefold."""
    from scripts.build_release import collect_runtime_files, validate_archive_members
    import unicodedata
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    (source / "addon.py").write_text("pass")
    resources = source / "resources"
    resources.mkdir()
    # Create two files that normalize to same path: "café.txt" (NFC) vs "cafe\u0301.txt" (NFD)
    file1 = resources / "café.txt"  # NFC
    file1.write_text("test1")
    file2 = resources / "cafe\u0301.txt"  # NFD
    file2.write_text("test2")

    try:
        files = collect_runtime_files(source)
        validate_archive_members(files)
        assert False, "Expected ValueError for Unicode normalization collision"
    except ValueError as e:
        assert "collision" in str(e).lower() or "normalize" in str(e).lower()


def test_collect_rejects_casefold_collision(tmp_path):
    """collect_runtime_files + validate must reject paths that collide after casefold()."""
    from scripts.build_release import collect_runtime_files, validate_archive_members
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
    (source / "addon.py").write_text("pass")
    resources = source / "resources"
    resources.mkdir()
    # Case-insensitive collision: "ReadMe.txt" vs "README.TXT"
    (resources / "ReadMe.txt").write_text("test1")
    (resources / "README.TXT").write_text("test2")

    try:
        files = collect_runtime_files(source)
        validate_archive_members(files)
        assert False, "Expected ValueError for casefold collision"
    except ValueError as e:
        assert "collision" in str(e).lower()


# ===== verify_zip adversarial tests (RED first) =====

def test_verify_zip_rejects_forged_repo_only_member(tmp_path):
    """verify_zip must reject ZIP with forged repository-only member (e.g., .github/workflows/xxx)."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        # Forged repo-only member
        zf.writestr("plugin.video.gronkhtv/.github/workflows/ci.yml", "fake")

    try:
        verify_zip(output)
        assert False, "Expected ValueError for forged repo-only member"
    except ValueError as e:
        assert "forbidden" in str(e).lower() or "repo" in str(e).lower() or "workflow" in str(e).lower()


def test_verify_zip_rejects_directory_entries(tmp_path):
    """verify_zip must reject ZIP with directory entries (trailing slash)."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/", "")  # directory entry
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")

    try:
        verify_zip(output)
        assert False, "Expected ValueError for directory entry"
    except ValueError as e:
        assert "director" in str(e).lower() or "invalid" in str(e).lower()


def test_verify_zip_rejects_traversal_paths(tmp_path):
    """verify_zip must reject ZIP with traversal paths (..)."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        # Traversal attempt
        zf.writestr("plugin.video.gronkhtv/../evil.txt", "evil")

    try:
        verify_zip(output)
        assert False, "Expected ValueError for traversal path"
    except ValueError as e:
        assert "traversal" in str(e).lower() or ".." in str(e).lower() or "invalid" in str(e).lower()


def test_verify_zip_rejects_absolute_paths(tmp_path):
    """verify_zip must reject ZIP with absolute paths."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        # Absolute path
        zf.writestr("/etc/passwd", "evil")

    try:
        verify_zip(output)
        assert False, "Expected ValueError for absolute path"
    except ValueError as e:
        assert "absolute" in str(e).lower() or "invalid" in str(e).lower()


def test_verify_zip_rejects_case_collision_in_zip(tmp_path):
    """verify_zip must reject ZIP with case-insensitive collision."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/ReadMe.txt", "test1")
        zf.writestr("plugin.video.gronkhtv/resources/README.TXT", "test2")  # case collision

    try:
        verify_zip(output)
        assert False, "Expected ValueError for case collision in ZIP"
    except ValueError as e:
        assert "collision" in str(e).lower()


def test_verify_zip_rejects_unicode_normalization_collision_in_zip(tmp_path):
    """verify_zip must reject ZIP with Unicode normalization collision."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/café.txt", "test1")  # NFC
        zf.writestr("plugin.video.gronkhtv/resources/cafe\u0301.txt", "test2")  # NFD

    try:
        verify_zip(output)
        assert False, "Expected ValueError for Unicode normalization collision in ZIP"
    except ValueError as e:
        assert "collision" in str(e).lower() or "normalize" in str(e).lower()


def test_verify_zip_requires_both_root_files(tmp_path):
    """verify_zip must require both addon.xml and addon.py at root."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        # Missing addon.py

    try:
        verify_zip(output)
        assert False, "Expected ValueError for missing addon.py"
    except ValueError as e:
        assert "missing" in str(e).lower() or "required" in str(e).lower()


def test_verify_zip_requires_at_least_one_resources_file(tmp_path):
    """verify_zip must require at least one file under resources/."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        # No resources files

    try:
        verify_zip(output)
        assert False, "Expected ValueError for missing resources files"
    except ValueError as e:
        assert "resource" in str(e).lower() or "required" in str(e).lower()


def test_verify_zip_rejects_forbidden_names(tmp_path):
    """verify_zip must reject ZIP with forbidden names (README, LICENSE, tests, workflows, .hermes, .pyc, __pycache__)."""
    from scripts.build_release import verify_zip
    import zipfile
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        # Forbidden names
        zf.writestr("plugin.video.gronkhtv/README.md", "readme")
        zf.writestr("plugin.video.gronkhtv/LICENSE", "license")
        zf.writestr("plugin.video.gronkhtv/tests/test.py", "test")
        zf.writestr("plugin.video.gronkhtv/.github/workflows/ci.yml", "ci")
        zf.writestr("plugin.video.gronkhtv/.hermes/config.json", "config")
        zf.writestr("plugin.video.gronkhtv/module.pyc", "bytecode")
        zf.writestr("plugin.video.gronkhtv/__pycache__/module.cpython-313.pyc", "bytecode")

    try:
        verify_zip(output)
        assert False, "Expected ValueError for forbidden names"
    except ValueError as e:
        assert "forbidden" in str(e).lower() or "invalid" in str(e).lower()


def test_verify_zip_verifies_embedded_addon_xml(tmp_path):
    """verify_zip must parse embedded addon.xml and verify id and version match source."""
    from scripts.build_release import verify_zip
    import zipfile
    import xml.etree.ElementTree as ET
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        # Wrong addon id
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.other" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")

    try:
        verify_zip(output)
        assert False, "Expected ValueError for wrong addon id"
    except ValueError as e:
        assert "id" in str(e).lower() or "addon" in str(e).lower()


def test_verify_zip_verifies_version_matches_source(tmp_path):
    """verify_zip must verify embedded addon.xml version matches source version."""
    from scripts.build_release import verify_zip
    import zipfile
    import xml.etree.ElementTree as ET
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        # Wrong version
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="9.9.9"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")

    try:
        verify_zip(output, source_version="1.0.0")
        assert False, "Expected ValueError for version mismatch"
    except ValueError as e:
        assert "version" in str(e).lower() or "mismatch" in str(e).lower()