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
import os
import stat
import sys
import zipfile
from pathlib import Path
from typing import List, Set, Tuple


ALLOWED_ROOTS = {"addon.xml", "addon.py", "resources"}
EXCLUDED_NAMES = {
    "README.md",
    "README.rst",
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
    ".gitignore",
    ".gitattributes",
    ".github",
    ".hermes",
    "tests",
    "__pycache__",
    ".pyc",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
EXCLUDED_PREFIXES = {".", "__pycache__"}


def is_excluded(name: str) -> bool:
    if name in EXCLUDED_NAMES:
        return True
    for suffix in EXCLUDED_SUFFIXES:
        if name.endswith(suffix):
            return True
    for prefix in EXCLUDED_PREFIXES:
        if name == prefix or name.startswith(prefix + "/"):
            return True
    return False


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
        if is_excluded(item.name):
            continue

        if not is_allowed_root(item.name):
            continue

        if item.name in {"addon.xml", "addon.py"}:
            if item.is_file() and not item.is_symlink():
                files.append((f"plugin.video.gronkhtv/{item.name}", item))
        elif item.name == "resources" and item.is_dir():
            for res_file in sorted(item.rglob("*")):
                if res_file.is_symlink():
                    continue
                if is_excluded(res_file.name):
                    continue
                if res_file.is_file():
                    rel = res_file.relative_to(root)
                    files.append((f"plugin.video.gronkhtv/{rel.as_posix()}", res_file))

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

        # No case collisions (case-insensitive check)
        norm = rel.lower()
        if norm in seen_normalized:
            raise ValueError(f"Case-insensitive collision: {arc_path}")
        seen_normalized.add(norm)

        # Must be allowed root type
        if top_level not in ALLOWED_ROOTS:
            raise ValueError(f"Disallowed root entry: {top_level} in {arc_path}")

        # If under resources, must be under resources/
        if top_level == "resources":
            if not is_allowed_under_resources(Path(rel)):
                raise ValueError(f"Invalid resources path: {rel}")

        # Source must exist and be a regular file
        if not src_path.exists():
            raise ValueError(f"Source does not exist: {src_path}")
        if not src_path.is_file() or src_path.is_symlink():
            raise ValueError(f"Source not a regular file: {src_path}")

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
    # Fixed timestamp: 1980-01-01 00:00:00 (DOS epoch)
    fixed_time = (1980, 1, 1, 0, 0, 0)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc_path, src_path in files:
            info = zipfile.ZipInfo(arc_path)
            info.date_time = fixed_time
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED

            with open(src_path, "rb") as f:
                zf.writestr(info, f.read())


def verify_zip(output_path: Path) -> List[str]:
    """Verify ZIP contents and return member list."""
    with zipfile.ZipFile(output_path, "r") as zf:
        members = sorted(zf.namelist())

        # Verify all members under single root
        roots = {m.split("/")[0] for m in members if "/" in m}
        if len(roots) != 1:
            raise ValueError(f"Multiple roots: {roots}")
        root = roots.pop()
        if root != "plugin.video.gronkhtv":
            raise ValueError(f"Root must be plugin.video.gronkhtv, got {root}")

        # Verify no forbidden entries
        for m in members:
            if m.endswith("/"):
                continue  # directory entry
            rel = m[len(root) + 1 :] if m.startswith(root + "/") else m
            if rel.startswith(".") or "/." in rel:
                raise ValueError(f"Dot path in archive: {m}")
            if any(part.lower() in {"readme.md", "license", "license.txt", "license.md"} for part in rel.split("/")):
                raise ValueError(f"Forbidden file in archive: {m}")

        return members


def parse_addon_xml(addon_xml_path: Path) -> tuple[str, str]:
    """Parse addon.xml and return (id, version)."""
    import xml.etree.ElementTree as ET
    tree = ET.parse(addon_xml_path)
    root = tree.getroot()
    addon_id = root.get("id")
    version = root.get("version")
    if not addon_id or not version:
        raise ValueError("addon.xml missing id or version")
    return addon_id, version


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic release archive")
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="Source directory")
    parser.add_argument("--output", type=Path, required=True, help="Output ZIP path")
    parser.add_argument("--verify-only", action="store_true", help="Only verify existing archive")
    args = parser.parse_args()

    if args.verify_only:
        members = verify_zip(args.output)
        print(f"Verified {len(members)} members in {args.output}")
        for m in members:
            print(f"  {m}")
        return 0

    source = args.source.resolve()
    output = args.output.resolve()

    # Collect runtime files
    files = collect_runtime_files(source)

    # Validate
    validate_archive_members(files)

    # Build deterministic ZIP
    build_deterministic_zip(files, output)

    # Verify
    members = verify_zip(output)

    # Parse addon.xml from source and verify version/id
    addon_id, version = parse_addon_xml(source / "addon.xml")
    print(f"Built {output}")
    print(f"Addon ID: {addon_id}, Version: {version}")
    print(f"Members: {len(members)}")
    for m in members:
        print(f"  {m}")

    return 0


if __name__ == "__main__":
    sys.exit(main())