#!/usr/bin/env python3
"""Allocate and apply monotonic patch versions for release candidates."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


PATCH_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$", re.ASCII)
RUNTIME_PATHS = ("addon.py", "resources")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = PATCH_VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid Kodi patch version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def next_patch_version(source_version: str, tags: list[str]) -> str:
    """Return one patch above the greatest valid source/tag version."""
    versions = [_version_tuple(source_version)]
    for tag in tags:
        candidate = tag[1:] if tag.startswith("v") else tag
        try:
            versions.append(_version_tuple(candidate))
        except ValueError:
            continue
    major, minor, patch = max(versions)
    return f"{major}.{minor}.{patch + 1}"


def bump_addon_version(path: Path, version: str) -> None:
    """Replace exactly one addon version attribute without reformatting XML."""
    _version_tuple(version)
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(<addon\b[^>]*\bversion=")[^"]+("[^>]*>)',
        rf"\g<1>{version}\g<2>",
        text,
        count=1,
    )
    if count != 1:
        raise ValueError("addon.xml must contain exactly one addon version attribute")
    path.write_text(updated, encoding="utf-8")


def runtime_changed_since(repo: Path, released_ref: str) -> bool:
    """Compare all runtime bytes to a release, ignoring metadata/docs/tests."""
    result = subprocess.run(
        ["git", "diff", "--quiet", released_ref, "HEAD", "--", *RUNTIME_PATHS],
        cwd=repo,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("git diff failed while classifying runtime changes")
    return result.returncode == 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--released-ref", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    repo = args.repo.resolve()
    if not runtime_changed_since(repo, args.released_ref):
        print("runtime_changed=false")
        return 0
    from build_release import parse_addon_xml

    _, source_version = parse_addon_xml(repo / "addon.xml")
    tags = subprocess.check_output(
        ["git", "tag", "--list", "v*"], cwd=repo, text=True
    ).splitlines()
    version = next_patch_version(source_version, tags)
    if args.apply:
        bump_addon_version(repo / "addon.xml", version)
    print("runtime_changed=true")
    print(f"version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
