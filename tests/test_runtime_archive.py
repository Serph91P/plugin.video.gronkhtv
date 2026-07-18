"""Regression test for runtime-only deterministic release archive."""

import subprocess
import tempfile
import zipfile
from pathlib import Path
import pytest


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


def _make_bad_zip(tmp_path: Path, base_members: dict, extra_members: dict) -> Path:
    """Helper to create a ZIP with base members plus extra forged members.
    Extra members REPLACE base members with same name to avoid ZIP duplicates.
    """
    output = tmp_path / "bad.zip"
    # Merge: extra_members override base_members
    merged = {**base_members, **extra_members}
    with zipfile.ZipFile(output, "w") as zf:
        for name, content in merged.items():
            if isinstance(content, zipfile.ZipInfo):
                zf.writestr(content, "")
            else:
                zf.writestr(name, content)
    return output


@pytest.fixture
def base_zip_members():
    """Base valid ZIP members for testing."""
    return {
        "plugin.video.gronkhtv/addon.xml": '<addon id="plugin.video.gronkhtv" version="1.0.0"/>',
        "plugin.video.gronkhtv/addon.py": "pass",
        "plugin.video.gronkhtv/resources/test.txt": "test",
    }


# Helper to get base members dict in parametrized tests
def _base_members():
    return {
        "plugin.video.gronkhtv/addon.xml": '<addon id="plugin.video.gronkhtv" version="1.0.0"/>',
        "plugin.video.gronkhtv/addon.py": "pass",
        "plugin.video.gronkhtv/resources/test.txt": "test",
    }


def test_verify_zip_requires_both_root_files(tmp_path, base_zip_members):
    """verify_zip must require both addon.xml and addon.py at root."""
    from scripts.build_release import verify_zip

    # Create ZIP without addon.py
    members = {k: v for k, v in base_zip_members.items() if "addon.py" not in k}
    output = _make_bad_zip(tmp_path, members, {})
    with pytest.raises(ValueError) as exc:
        verify_zip(output)
    assert "missing" in str(exc.value).lower() or "required" in str(exc.value).lower()


def test_verify_zip_verifies_embedded_addon_xml(tmp_path, base_zip_members):
    """verify_zip must parse embedded addon.xml and verify id."""
    from scripts.build_release import verify_zip

    output = _make_bad_zip(
        tmp_path,
        base_zip_members,
        {"plugin.video.gronkhtv/addon.xml": '<addon id="plugin.video.other" version="1.0.0"/>'},
    )
    with pytest.raises(ValueError) as exc:
        verify_zip(output)
    assert "id" in str(exc.value).lower() or "addon" in str(exc.value).lower()


def test_verify_zip_verifies_version_matches_source(tmp_path, base_zip_members):
    """verify_zip must verify embedded addon.xml version matches source version."""
    from scripts.build_release import verify_zip

    output = _make_bad_zip(
        tmp_path,
        base_zip_members,
        {"plugin.video.gronkhtv/addon.xml": '<addon id="plugin.video.gronkhtv" version="9.9.9"/>'},
    )
    with pytest.raises(ValueError) as exc:
        verify_zip(output, source_version="1.0.0")
    assert "version" in str(exc.value).lower() or "mismatch" in str(exc.value).lower()


def test_verify_zip_rejects_forged_symlink_entry(tmp_path):
    """verify_zip must reject ZIP with forged Unix symlink entry (external_attr indicates symlink)."""
    from scripts.build_release import verify_zip
    import zipfile
    import stat

    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("plugin.video.gronkhtv/addon.xml", '<addon id="plugin.video.gronkhtv" version="1.0.0"/>')
        zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
        zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        info = zipfile.ZipInfo("plugin.video.gronkhtv/resources/symlink.txt")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "target")

    with pytest.raises(ValueError) as exc:
        verify_zip(output)
    assert "symlink" in str(exc.value).lower() or "external_attr" in str(exc.value).lower() or "invalid" in str(exc.value).lower()


@pytest.mark.parametrize(
    "evil_path",
    [
        "plugin.video.gronkhtv/addon.xml/evil",
        "plugin.video.gronkhtv/addon.py/evil",
    ],
)
def test_verify_zip_rejects_exact_root_file_evil(tmp_path, evil_path):
    """verify_zip must reject entries like plugin.video.gronkhtv/addon.xml/evil (exact root file validation)."""
    from scripts.build_release import verify_zip

    output = _make_bad_zip(tmp_path, _base_members(), {evil_path: "evil"})
    with pytest.raises(ValueError) as exc:
        verify_zip(output)
    assert "invalid" in str(exc.value).lower() or "exact" in str(exc.value).lower() or "root" in str(exc.value).lower()


@pytest.mark.parametrize(
    "traversal_path",
    [
        "plugin.video.gronkhtv/resources/../evil.txt",
        "plugin.video.gronkhtv/addon.xml/../evil.txt",
    ],
)
def test_verify_zip_rejects_synthetic_traversal(tmp_path, traversal_path):
    """verify_zip must reject synthetic traversal paths like plugin.video.gronkhtv/resources/../evil."""
    from scripts.build_release import verify_zip

    output = _make_bad_zip(tmp_path, _base_members(), {traversal_path: "evil"})
    with pytest.raises(ValueError) as exc:
        verify_zip(output)
    assert "traversal" in str(exc.value).lower() or ".." in str(exc.value).lower() or "invalid" in str(exc.value).lower()


@pytest.mark.parametrize(
    "repository_only_path",
    [
        "resources/README",
        "resources/docs/readme.MD",
        "resources/docs/ReadMe.rSt",
        "resources/LICENSE",
        "resources/legal/license.TXT",
        "resources/legal/License.Md",
    ],
)
def test_verify_zip_rejects_nested_readme_and_license_basenames(tmp_path, repository_only_path):
    """verify_zip must reject README and LICENSE basenames anywhere below the add-on root."""
    from scripts.build_release import verify_zip

    member = f"plugin.video.gronkhtv/{repository_only_path}"
    output = _make_bad_zip(tmp_path, _base_members(), {member: "repository metadata"})

    with pytest.raises(ValueError, match="[Ff]orbidden"):
        verify_zip(output)


@pytest.mark.parametrize(
    "mode,source_version,expected_version,should_pass",
    [
        ("build", "1.0.0", "2.0.0", False),   # mismatch on build
        ("verify", "1.0.0", "2.0.0", False),  # mismatch on verify
        ("build", "1.0.0", "1.0.0", True),    # match on build
        ("verify", "1.0.0", "1.0.0", True),   # match on verify
    ],
)
def test_cli_expected_version(tmp_path, mode, source_version, expected_version, should_pass):
    """CLI --expected-version must enforce version match on build and verify."""
    script = ROOT / "scripts" / "build_release.py"
    output = tmp_path / "out.zip"

    if mode == "build":
        source = tmp_path / "source"
        source.mkdir()
        (source / "addon.xml").write_text(f'<addon id="plugin.video.gronkhtv" version="{source_version}"/>')
        (source / "addon.py").write_text("pass")
        (source / "resources").mkdir()
        (source / "resources" / "test.txt").write_text("test")
        cmd = ["python3", str(script), "--source", str(source), "--output", str(output), "--expected-version", expected_version]
    else:
        import zipfile
        with zipfile.ZipFile(output, "w") as zf:
            zf.writestr("plugin.video.gronkhtv/addon.xml", f'<addon id="plugin.video.gronkhtv" version="{source_version}"/>')
            zf.writestr("plugin.video.gronkhtv/addon.py", "pass")
            zf.writestr("plugin.video.gronkhtv/resources/test.txt", "test")
        cmd = ["python3", str(script), "--verify-only", "--output", str(output), "--expected-version", expected_version]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if should_pass:
        assert result.returncode == 0, f"Expected pass but got: {result.stderr}"
    else:
        assert result.returncode != 0
        assert "version" in (result.stderr + result.stdout).lower() or "mismatch" in (result.stderr + result.stdout).lower()


def test_workflow_passes_expected_version_to_build_and_verify():
    """Workflow must pass --expected-version to both build and verify steps."""
    import yaml
    workflow_path = ROOT / ".github" / "workflows" / "make-release.yml"
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)

    steps = workflow["jobs"]["release"]["steps"]

    # Find Create Zip step
    create_zip_step = next(s for s in steps if s.get("name") == "Create Zip")
    create_zip_run = create_zip_step["run"]
    assert "--expected-version" in create_zip_run
    assert "${{ steps.version.outputs.version }}" in create_zip_run

    # Find Verify Zip step
    verify_zip_step = next(s for s in steps if s.get("name") == "Verify Zip")
    verify_zip_run = verify_zip_step["run"]
    assert "--expected-version" in verify_zip_run
    assert "${{ steps.version.outputs.version }}" in verify_zip_run
