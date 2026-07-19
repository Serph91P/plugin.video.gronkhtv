#!/usr/bin/env python3
"""Deterministic release archive builder for plugin.video.gronkhtv.

Produces a deterministic ZIP archive with:
- Exactly one root directory: plugin.video.gronkhtv/
- Only allowed runtime files: addon.xml, addon.py, resources/**/*
- Rejects: symlinks, traversal, absolute paths, dot files, repo-only paths,
  case collisions, normalized path collisions, README, LICENSE, tests,
  workflows, .hermes, .pyc, __pycache__
- Deterministic: fixed member order, normalized timestamps (1980-01-01 00:00:00)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import List, Set, Tuple


EXPECTED_ADDON_ID = "plugin.video.gronkhtv"
ALLOWED_ROOTS = {"addon.xml", "addon.py", "resources"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REGULAR_FILE_MODE = stat.S_IFREG | 0o644
VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
FORBIDDEN_COMPONENTS = {
    ".git",
    ".github",
    ".hermes",
    "__pycache__",
    "tests",
    "workflows",
}
FORBIDDEN_BASENAMES = {
    ".gitattributes",
    ".gitignore",
    "license",
    "license.md",
    "license.txt",
    "readme",
    "readme.md",
    "readme.rst",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyd", ".pyo"}
WINDOWS_RESERVED_BASENAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def normalize_path_key(path: str) -> str:
    """Normalize path for collision detection: NFC + casefold."""
    return unicodedata.normalize("NFC", path).casefold()


def register_normalized_path(path: str, seen: Set[str]) -> None:
    parts = path.split("/")
    if "\\" in path or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Non-canonical path: {path}")

    normalized = normalize_path_key(path)
    for existing in seen:
        if (
            normalized == existing
            or normalized.startswith(existing + "/")
            or existing.startswith(normalized + "/")
        ):
            raise ValueError(f"Normalized path collision: {path}")
    seen.add(normalized)


def validate_runtime_path(path: str) -> None:
    """Reject repository-only, cache, hidden, and compiled runtime paths."""
    parts = path.split("/")
    folded = [part.casefold() for part in parts]
    if any(part.endswith((" ", ".")) for part in parts):
        raise ValueError(f"Windows-unsafe trailing dot or space: {path}")
    if any(":" in part for part in parts):
        raise ValueError(f"Windows-unsafe colon or alternate data stream: {path}")
    for part in folded:
        device_basename = part.split(".", 1)[0].rstrip(" .")
        if device_basename in WINDOWS_RESERVED_BASENAMES:
            raise ValueError(f"Windows-reserved device basename: {path}")
    if any(part.startswith(".") for part in parts):
        raise ValueError(f"Dot path component not allowed: {path}")
    if any(part in FORBIDDEN_COMPONENTS for part in folded):
        raise ValueError(f"Forbidden repository or cache path: {path}")
    if any(part in FORBIDDEN_BASENAMES for part in folded):
        raise ValueError(f"Forbidden repository file: {path}")
    if folded and any(folded[-1].endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise ValueError(f"Forbidden compiled file: {path}")


def require_regular_source_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"Source does not exist: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"Source must be a regular file: {path}")


def validate_addon_element(root: ET.Element) -> tuple[str, str]:
    if root.tag != "addon":
        raise ValueError(f"addon.xml root must be addon, got {root.tag}")
    addon_id = root.get("id")
    version = root.get("version")
    if addon_id != EXPECTED_ADDON_ID:
        raise ValueError(f"addon.xml id must be {EXPECTED_ADDON_ID}, got {addon_id}")
    if version is None or VERSION_TOKEN.fullmatch(version) is None:
        raise ValueError(f"addon.xml has unsafe version token: {version!r}")
    return addon_id, version


def is_allowed_root(name: str) -> bool:
    return name in ALLOWED_ROOTS


def is_allowed_under_resources(rel_path: Path) -> bool:
    try:
        rel_path.relative_to("resources")
        return True
    except ValueError:
        return False


def collect_runtime_files(root: Path) -> List[Tuple[str, Path]]:
    """Collect all runtime files that should be included in the release.

    Returns list of (archive_path, source_path) tuples sorted by archive_path.
    """
    files: List[Tuple[str, Path]] = []

    for item in sorted(root.iterdir()):
        if not is_allowed_root(item.name):
            kind = "symlink" if item.is_symlink() else "member"
            raise ValueError(f"Unapproved source {kind}: {item}")

        if item.name in {"addon.xml", "addon.py"}:
            require_regular_source_file(item)
            files.append((f"plugin.video.gronkhtv/{item.name}", item))
        elif item.name == "resources":
            if not stat.S_ISDIR(item.lstat().st_mode):
                raise ValueError(f"resources must be a directory: {item}")
            for res_file in sorted(item.rglob("*")):
                rel = res_file.relative_to(root)
                validate_runtime_path(rel.as_posix())
                mode = res_file.lstat().st_mode
                if stat.S_ISREG(mode):
                    files.append((f"plugin.video.gronkhtv/{rel.as_posix()}", res_file))
                elif stat.S_ISDIR(mode):
                    continue
                else:
                    raise ValueError(f"Source must be a regular file: {res_file}")

    files.sort(key=lambda x: x[0])
    return files


def validate_archive_members(files: List[Tuple[str, Path]]) -> None:
    """Validate all archive members conform to requirements."""
    roots: Set[str] = set()
    seen_normalized: Set[str] = set()

    for arc_path, src_path in files:
        # Must be under single root
        if not arc_path.startswith("plugin.video.gronkhtv/"):
            raise ValueError(f"Member {arc_path} not under plugin.video.gronkhtv/")

        # Extract path under root
        rel = arc_path[len("plugin.video.gronkhtv/") :]

        # Track root-level entries
        top_level = rel.split("/")[0]
        roots.add(top_level)

        # No dot files or dot directories
        parts = rel.split("/")
        for part in parts:
            if part.startswith("."):
                raise ValueError(f"Dot path component in {arc_path}: {part}")

        # Must be allowed root type
        if top_level not in ALLOWED_ROOTS:
            raise ValueError(f"Disallowed root entry: {top_level} in {arc_path}")

        # Exact root file validation - addon.xml and addon.py must be exact root files
        if top_level in {"addon.xml", "addon.py"}:
            if len(parts) != 1:
                raise ValueError(f"Invalid archive member (exact root file required): {arc_path}")

        # If under resources, must be under resources/
        if top_level == "resources":
            if not is_allowed_under_resources(Path(rel)):
                raise ValueError(f"Invalid resources path: {rel}")
            if ".." in rel.split("/"):
                raise ValueError(f"Traversal path component in {arc_path}")

        validate_runtime_path(rel)
        require_regular_source_file(src_path)

        register_normalized_path(rel, seen_normalized)

    # Must have exactly the three allowed roots
    required_roots = {"addon.xml", "addon.py", "resources"}
    if roots != required_roots:
        missing = required_roots - roots
        extra = roots - required_roots
        if missing:
            raise ValueError(f"Missing required roots: {missing}")
        if extra:
            raise ValueError(f"Extra unexpected roots: {extra}")

    if len({p.split("/")[0] for p in [f[0] for f in files]}) != 1:
        raise ValueError("Archive must have exactly one root directory")

    root_name = files[0][0].split("/")[0]
    if root_name != "plugin.video.gronkhtv":
        raise ValueError(f"Root directory must be plugin.video.gronkhtv, got {root_name}")


def build_deterministic_zip(files: List[Tuple[str, Path]], output_path: Path) -> None:
    """Build a deterministic ZIP archive."""
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc_path, src_path in files:
            info = zipfile.ZipInfo(arc_path)
            info.date_time = FIXED_ZIP_TIME
            info.create_system = 3
            info.external_attr = REGULAR_FILE_MODE << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.extra = b""
            info.comment = b""

            with open(src_path, "rb") as f:
                zf.writestr(info, f.read())


def verify_zip(output_path: Path, source_version: str | None = None) -> List[str]:
    """Verify ZIP contents and return member list.

    Enforces exact archive contract:
    - Every regular member under single root plugin.video.gronkhtv/
    - Only allowed members: addon.xml, addon.py, or resources/**/*
    - Reject: directory/symlink entries, traversal, absolute paths,
      case/normalized collisions, repo-only paths, tests/workflows/.hermes,
      .pyc, __pycache__, README, LICENSE
    - Require both root files (addon.xml, addon.py) + at least one resources file
    - Parse embedded addon.xml and verify id=plugin.video.gronkhtv and version=source_version
    """
    with zipfile.ZipFile(output_path, "r") as zf:
        if zf.comment:
            raise ValueError("Archive comment not allowed")
        infos = zf.infolist()
        members = [info.filename for info in infos]
        if members != sorted(members):
            raise ValueError("Archive members are not in canonical order")

        # Verify all members under single root (ignore absolute paths that create empty root)
        roots = {m.split("/")[0] for m in members if "/" in m}
        # Filter out empty root from absolute paths
        roots = {r for r in roots if r}
        if len(roots) != 1:
            raise ValueError(f"Multiple roots: {roots}")
        root = roots.pop()
        if root != "plugin.video.gronkhtv":
            raise ValueError(f"Root must be plugin.video.gronkhtv, got {root}")

        # Track required files
        has_addon_xml = False
        has_addon_py = False
        has_resources_file = False

        # For collision detection
        seen_normalized: Set[str] = set()

        for info in infos:
            m = info.filename
            if info.date_time != FIXED_ZIP_TIME:
                raise ValueError(f"Invalid timestamp for archive member: {m}")
            if info.create_system != 3:
                raise ValueError(f"Archive member must use Unix metadata: {m}")
            mode = info.external_attr >> 16
            if not stat.S_ISREG(mode):
                raise ValueError(f"Archive member must be a regular file: {m}")
            if stat.S_IMODE(mode) != 0o644 or info.external_attr & 0xFFFF:
                raise ValueError(f"Archive member must have mode 0644: {m}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"Archive member must use deflate compression: {m}")
            if info.extra:
                raise ValueError(f"Archive member extra data not allowed: {m}")
            if info.comment:
                raise ValueError(f"Archive member comment not allowed: {m}")
            try:
                m.encode("ascii")
                expected_flags = 0
            except UnicodeEncodeError:
                expected_flags = 0x800
            if info.flag_bits != expected_flags:
                raise ValueError(f"Unsafe archive member flags for {m}: {info.flag_bits:#x}")

            rel = m[len(root) + 1 :] if m.startswith(root + "/") else m

            # Reject absolute paths
            if rel.startswith("/") or (os.name == "nt" and len(rel) >= 3 and rel[1:3] == ":/"):
                raise ValueError(f"Absolute path in archive: {m}")

            # Reject traversal (including synthetic paths like resources/../evil)
            if ".." in rel.split("/"):
                raise ValueError(f"Traversal path in archive: {m}")

            validate_runtime_path(rel)

            # Validate allowed structure - EXACT root file matching
            parts = rel.split("/")
            if parts[0] == "addon.xml":
                if len(parts) != 1:
                    raise ValueError(f"Invalid archive member (addon.xml must be exact root file): {m}")
                has_addon_xml = True
            elif parts[0] == "addon.py":
                if len(parts) != 1:
                    raise ValueError(f"Invalid archive member (addon.py must be exact root file): {m}")
                has_addon_py = True
            elif parts[0] == "resources" and len(parts) > 1:
                has_resources_file = True
            else:
                raise ValueError(f"Invalid archive member (not under allowed paths): {m}")

            register_normalized_path(rel, seen_normalized)

        # Require both root files
        if not has_addon_xml:
            raise ValueError("Missing required addon.xml at root")
        if not has_addon_py:
            raise ValueError("Missing required addon.py at root")

        # Require at least one resources file
        if not has_resources_file:
            raise ValueError("Missing required files under resources/")

        # Reading through EOF verifies local headers, decompression, sizes, and CRCs.
        for info in infos:
            with zf.open(info, "r") as member:
                while member.read(1024 * 1024):
                    pass

        # Parse and verify embedded addon.xml
        with zf.open(f"{root}/addon.xml") as f:
            addon_content = f.read().decode("utf-8")
        addon_root = ET.fromstring(addon_content)
        _, addon_version = validate_addon_element(addon_root)

        if source_version is not None and addon_version != source_version:
            raise ValueError(f"Version mismatch: embedded={addon_version}, source={source_version}")

        return members


def parse_addon_xml(addon_xml_path: Path) -> tuple[str, str]:
    """Parse addon.xml and return (id, version)."""
    tree = ET.parse(addon_xml_path)
    return validate_addon_element(tree.getroot())


def verify_checksum(output_path: Path, expected: str) -> tuple[bool, str]:
    checksum = hashlib.sha256(Path(output_path).read_bytes()).hexdigest()
    return checksum == expected, checksum


def validate_package(output_path: Path, source: Path | None = None) -> list[str]:
    try:
        source_version = None
        if source is not None:
            _, source_version = parse_addon_xml(Path(source) / "addon.xml")
        verify_zip(Path(output_path), source_version=source_version)
    except (
        OSError,
        UnicodeError,
        ValueError,
        ET.ParseError,
        zipfile.BadZipFile,
        zlib.error,
    ) as exc:
        return [str(exc)]
    return []


def build_package(
    source: Path, output: Path, expected_version: str | None = None
) -> dict[str, object]:
    source = Path(source).resolve()
    output = Path(output).resolve()
    addon_id, version = parse_addon_xml(source / "addon.xml")
    if expected_version and version != expected_version:
        raise ValueError(
            f"Version mismatch: source={version}, expected={expected_version}"
        )

    files = collect_runtime_files(source)
    validate_archive_members(files)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary_output = Path(temporary_name)
    try:
        build_deterministic_zip(files, temporary_output)
        members = verify_zip(temporary_output, source_version=version)
        checksum = hashlib.sha256(temporary_output.read_bytes()).hexdigest()
        temporary_output.replace(output)
    finally:
        temporary_output.unlink(missing_ok=True)

    return {
        "addon_id": addon_id,
        "addon_version": version,
        "checksum": checksum,
        "filename": f"{addon_id}-{version}.zip",
        "members": members,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic release archive")
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="Source directory")
    parser.add_argument("--output", type=Path, required=True, help="Output ZIP path")
    parser.add_argument("--verify-only", action="store_true", help="Only verify existing archive")
    parser.add_argument("--expected-version", type=str, help="Expected addon version (for validation)")
    args = parser.parse_args()

    if args.verify_only:
        members = verify_zip(args.output, source_version=args.expected_version)
        print(f"Verified {len(members)} members in {args.output}")
        for m in members:
            print(f"  {m}")
        return 0

    output = args.output.resolve()
    info = build_package(args.source, output, expected_version=args.expected_version)

    print(f"Built {output}")
    print(f"Addon ID: {info['addon_id']}, Version: {info['addon_version']}")
    print(f"SHA256: {info['checksum']}")
    print(f"Members: {len(info['members'])}")
    for m in info["members"]:
        print(f"  {m}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
