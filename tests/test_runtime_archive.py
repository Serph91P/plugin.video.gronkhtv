"""Regression test for runtime-only deterministic release archive."""

import os
import stat
import struct
import subprocess
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
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REGULAR_FILE_MODE = stat.S_IFREG | 0o644


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


def _runtime_source(tmp_path: Path, addon_id: str = EXPECTED_ROOT) -> Path:
    source = tmp_path / "source"
    (source / "resources").mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{addon_id}" version="1.0.0"/>', encoding="utf-8"
    )
    (source / "addon.py").write_text("pass\n", encoding="utf-8")
    (source / "resources" / "runtime.txt").write_text(
        "runtime\n", encoding="utf-8"
    )
    return source


def _run_build(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "build_release.py"),
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_build_rejects_wrong_addon_id_without_writing_archive(tmp_path):
    source = _runtime_source(tmp_path, addon_id="plugin.video.impostor")
    output = tmp_path / "release.zip"

    result = _run_build(source, output)

    assert result.returncode != 0
    assert not output.exists()


def test_build_rejects_source_symlink_resolving_outside_root(tmp_path):
    source = _runtime_source(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (source / "escape.txt").symlink_to(outside)
    output = tmp_path / "release.zip"

    result = _run_build(source, output)

    assert result.returncode != 0
    assert "symlink" in (result.stdout + result.stderr).lower()
    assert not output.exists()


def test_build_rejects_unapproved_source_member(tmp_path):
    source = _runtime_source(tmp_path)
    (source / "README.md").write_text("not runtime\n", encoding="utf-8")
    output = tmp_path / "release.zip"

    result = _run_build(source, output)

    assert result.returncode != 0
    assert "unapproved" in (result.stdout + result.stderr).lower()
    assert not output.exists()


@pytest.mark.parametrize(
    "file_type",
    [stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK],
    ids=["fifo", "character-device", "block-device", "socket"],
)
def test_build_rejects_every_non_regular_source_mode(tmp_path, monkeypatch, file_type):
    from scripts.build_release import build_package

    source = _runtime_source(tmp_path)
    hostile = source / "resources" / "hostile"
    hostile.write_text("placeholder", encoding="utf-8")
    real_lstat = Path.lstat

    def forged_lstat(path):
        result = real_lstat(path)
        if path == hostile:
            fields = list(result)
            fields[0] = file_type | 0o644
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", forged_lstat)
    output = tmp_path / "release.zip"

    with pytest.raises(ValueError, match="regular file"):
        build_package(source, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "nested_path",
    [
        "resources/tests/test_runtime.py",
        "resources/workflows/release.yml",
        "resources/.github/workflows/release.yml",
        "resources/.pytest_cache/state",
        "resources/__pycache__/module.pyc",
        "resources/lib/module.PYO",
        "resources/docs/README.md",
        "resources/legal/license.TXT",
    ],
)
def test_build_rejects_nested_repository_cache_and_compiled_paths(
    tmp_path, nested_path
):
    from scripts.build_release import build_package

    source = _runtime_source(tmp_path)
    hostile = source / nested_path
    hostile.parent.mkdir(parents=True, exist_ok=True)
    hostile.write_text("not runtime", encoding="utf-8")
    output = tmp_path / "release.zip"

    with pytest.raises(ValueError, match="Forbidden|Dot path"):
        build_package(source, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "addon_xml",
    [
        '<metadata id="plugin.video.gronkhtv" version="1.0.0"/>',
        '<addon id="plugin.video.gronkhtv" version=""/>',
        '<addon id="plugin.video.gronkhtv" version="../1.0.0"/>',
        '<addon id="plugin.video.gronkhtv" version="1.0.0 bad"/>',
        '<addon id="plugin.video.gronkhtv" version="$GITHUB_OUTPUT"/>',
    ],
)
def test_build_rejects_invalid_addon_root_or_unsafe_version(tmp_path, addon_xml):
    from scripts.build_release import build_package

    source = _runtime_source(tmp_path)
    (source / "addon.xml").write_text(addon_xml, encoding="utf-8")
    output = tmp_path / "release.zip"

    with pytest.raises(ValueError, match="root|version"):
        build_package(source, output)
    assert not output.exists()


def _zip_info(
    name: str,
    *,
    mode: int = REGULAR_FILE_MODE,
    timestamp: tuple[int, int, int, int, int, int] = FIXED_ZIP_TIME,
    create_system: int = 3,
    compress_type: int = zipfile.ZIP_DEFLATED,
    extra: bytes = b"",
    comment: bytes = b"",
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, timestamp)
    info.create_system = create_system
    info.external_attr = mode << 16
    info.compress_type = compress_type
    info.extra = extra
    info.comment = comment
    return info


def _make_bad_zip(
    tmp_path: Path,
    base_members: dict,
    extra_members: dict,
    *,
    archive_comment: bytes = b"",
) -> Path:
    """Helper to create a ZIP with base members plus extra forged members.
    Extra members REPLACE base members with same name to avoid ZIP duplicates.
    """
    output = tmp_path / "bad.zip"
    # Merge: extra_members override base_members
    merged = {**base_members, **extra_members}
    with zipfile.ZipFile(output, "w") as zf:
        zf.comment = archive_comment
        for name in sorted(merged):
            content = merged[name]
            if isinstance(content, zipfile.ZipInfo):
                zf.writestr(content, "")
            else:
                zf.writestr(_zip_info(name), content)
    return output


def _set_first_central_directory_flags(path: Path, flags: int) -> None:
    data = bytearray(path.read_bytes())
    offset = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, offset + 8, flags)
    path.write_bytes(data)


def _corrupt_last_central_directory_crc(path: Path) -> None:
    data = bytearray(path.read_bytes())
    offset = data.rindex(b"PK\x01\x02")
    crc = struct.unpack_from("<I", data, offset + 16)[0]
    struct.pack_into("<I", data, offset + 16, crc ^ 0xFFFFFFFF)
    path.write_bytes(data)


def _corrupt_first_local_header_name(path: Path) -> None:
    data = bytearray(path.read_bytes())
    offset = data.index(b"PK\x03\x04")
    name_length = struct.unpack_from("<H", data, offset + 26)[0]
    name_offset = offset + 30
    data[name_offset + name_length - 1] ^= 1
    path.write_bytes(data)


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

    info = _zip_info(
        "plugin.video.gronkhtv/resources/symlink.txt",
        mode=stat.S_IFLNK | 0o777,
    )
    output = _make_bad_zip(tmp_path, _base_members(), {info.filename: info})

    with pytest.raises(ValueError) as exc:
        verify_zip(output)
    assert "regular file" in str(exc.value).lower()


@pytest.mark.parametrize(
    "file_type",
    [stat.S_IFDIR, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK],
    ids=["directory", "fifo", "character-device", "block-device", "socket"],
)
def test_verify_zip_rejects_every_non_regular_archive_mode(tmp_path, file_type):
    from scripts.build_release import verify_zip

    info = _zip_info(
        "plugin.video.gronkhtv/resources/hostile",
        mode=file_type | 0o644,
    )
    output = _make_bad_zip(tmp_path, _base_members(), {info.filename: info})

    with pytest.raises(ValueError, match="regular file"):
        verify_zip(output)


@pytest.mark.parametrize(
    "nested_path",
    [
        "resources/tests/test_runtime.py",
        "resources/workflows/release.yml",
        "resources/.github/workflows/release.yml",
        "resources/.ruff_cache/state",
        "resources/__pycache__/module.pyc",
        "resources/lib/module.PYD",
        "resources/docs/README.rst",
        "resources/legal/LICENSE.md",
    ],
)
def test_verify_zip_rejects_nested_repository_cache_and_compiled_paths(
    tmp_path, nested_path
):
    from scripts.build_release import verify_zip

    member = f"plugin.video.gronkhtv/{nested_path}"
    output = _make_bad_zip(tmp_path, _base_members(), {member: "not runtime"})

    with pytest.raises(ValueError, match="Forbidden|Dot path"):
        verify_zip(output)


@pytest.mark.parametrize(
    "addon_xml",
    [
        '<metadata id="plugin.video.gronkhtv" version="1.0.0"/>',
        '<addon id="plugin.video.other" version="1.0.0"/>',
        '<addon id="plugin.video.gronkhtv" version=""/>',
        '<addon id="plugin.video.gronkhtv" version="../1.0.0"/>',
        '<addon id="plugin.video.gronkhtv" version="1.0.0 bad"/>',
    ],
)
def test_verify_zip_rejects_invalid_embedded_identity_or_version(
    tmp_path, addon_xml
):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(
        tmp_path,
        _base_members(),
        {"plugin.video.gronkhtv/addon.xml": addon_xml},
    )

    with pytest.raises(ValueError, match="root|id|version"):
        verify_zip(output)


@pytest.mark.parametrize(
    "metadata",
    [
        {"timestamp": (1981, 1, 1, 0, 0, 0)},
        {"create_system": 0},
        {"mode": stat.S_IFREG | 0o600},
        {"compress_type": zipfile.ZIP_STORED},
        {"extra": b"\x01\x00\x00\x00"},
        {"comment": b"member comment"},
    ],
    ids=["timestamp", "platform", "permissions", "compression", "extra", "comment"],
)
def test_verify_zip_rejects_non_deterministic_member_metadata(tmp_path, metadata):
    from scripts.build_release import verify_zip

    name = "plugin.video.gronkhtv/resources/hostile.txt"
    info = _zip_info(name, **metadata)
    output = _make_bad_zip(tmp_path, _base_members(), {name: info})

    with pytest.raises(ValueError):
        verify_zip(output)


def test_verify_zip_rejects_archive_comment(tmp_path):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(
        tmp_path,
        _base_members(),
        {},
        archive_comment=b"archive comment",
    )

    with pytest.raises(ValueError, match="Archive comment"):
        verify_zip(output)


def test_verify_zip_rejects_unsafe_member_flags(tmp_path):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(tmp_path, _base_members(), {})
    _set_first_central_directory_flags(output, 0x1)

    with pytest.raises(ValueError, match="Unsafe archive member flags"):
        verify_zip(output)


def test_verify_zip_rejects_noncanonical_physical_member_order(tmp_path):
    from scripts.build_release import verify_zip

    output = tmp_path / "bad.zip"
    members = _base_members()
    physical_order = [
        "plugin.video.gronkhtv/resources/test.txt",
        "plugin.video.gronkhtv/addon.xml",
        "plugin.video.gronkhtv/addon.py",
    ]
    with zipfile.ZipFile(output, "w") as archive:
        for name in physical_order:
            archive.writestr(_zip_info(name), members[name])

    with pytest.raises(ValueError, match="canonical order"):
        verify_zip(output)


def test_verify_zip_fully_reads_every_member_for_crc(tmp_path):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(tmp_path, _base_members(), {})
    _corrupt_last_central_directory_crc(output)

    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        verify_zip(output)


def test_verify_zip_opens_every_member_to_check_local_headers(tmp_path):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(tmp_path, _base_members(), {})
    _corrupt_first_local_header_name(output)

    with pytest.raises(zipfile.BadZipFile, match="header"):
        verify_zip(output)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "resources/trailing.",
        "resources/trailing ",
        "resources/dir./runtime.txt",
        "resources/stream:metadata",
        "resources/CON",
        "resources/con.txt",
        "resources/AUX.py",
        "resources/COM1.log",
        "resources/lpt9",
        "resources/NUL.json",
    ],
)
def test_build_rejects_windows_unsafe_source_components(tmp_path, unsafe_path):
    from scripts.build_release import build_package

    source = _runtime_source(tmp_path)
    hostile = source / unsafe_path
    hostile.parent.mkdir(parents=True, exist_ok=True)
    hostile.write_text("unsafe", encoding="utf-8")
    output = tmp_path / "release.zip"

    with pytest.raises(ValueError, match="Windows"):
        build_package(source, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "resources/trailing.",
        "resources/trailing ",
        "resources/dir./runtime.txt",
        "resources/stream:metadata",
        "resources/PRN",
        "resources/prn.txt",
        "resources/COM9.log",
        "resources/LPT1",
        "resources/NUL.json",
    ],
)
def test_verify_zip_rejects_windows_unsafe_archive_components(
    tmp_path, unsafe_path
):
    from scripts.build_release import verify_zip

    member = f"plugin.video.gronkhtv/{unsafe_path}"
    output = _make_bad_zip(tmp_path, _base_members(), {member: "unsafe"})

    with pytest.raises(ValueError, match="Windows"):
        verify_zip(output)


def test_build_is_byte_identical_and_emits_exact_archive_metadata(tmp_path):
    from scripts.build_release import build_package

    source = _runtime_source(tmp_path)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.gronkhtv" version="1.0.0+omega.1"/>',
        encoding="utf-8",
    )
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_info = build_package(source, first)
    second_info = build_package(source, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_info["checksum"] == second_info["checksum"]
    assert first_info["filename"] == "plugin.video.gronkhtv-1.0.0+omega.1.zip"
    with zipfile.ZipFile(first) as archive:
        assert archive.comment == b""
        for info in archive.infolist():
            assert info.date_time == FIXED_ZIP_TIME
            assert info.create_system == 3
            assert info.external_attr == REGULAR_FILE_MODE << 16
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.extra == b""
            assert info.comment == b""
            assert info.flag_bits == 0


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


def test_verify_zip_rejects_lexically_noncanonical_member(tmp_path):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(
        tmp_path,
        _base_members(),
        {"plugin.video.gronkhtv/resources//alias.txt": "alias"},
    )

    with pytest.raises(ValueError, match="[Nn]on-canonical"):
        verify_zip(output)


def test_verify_zip_rejects_casefolded_file_directory_collision(tmp_path):
    from scripts.build_release import verify_zip

    output = _make_bad_zip(
        tmp_path,
        _base_members(),
        {"plugin.video.gronkhtv/resources/TEST.txt/child": "collision"},
    )

    with pytest.raises(ValueError, match="[Cc]ollision"):
        verify_zip(output)


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
        with zipfile.ZipFile(output, "w") as zf:
            zf.writestr(_zip_info("plugin.video.gronkhtv/addon.py"), "pass")
            zf.writestr(
                _zip_info("plugin.video.gronkhtv/addon.xml"),
                f'<addon id="plugin.video.gronkhtv" version="{source_version}"/>',
            )
            zf.writestr(
                _zip_info("plugin.video.gronkhtv/resources/test.txt"), "test"
            )
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
