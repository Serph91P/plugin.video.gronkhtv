import subprocess
from pathlib import Path

import pytest

from scripts.release_version import (
    bump_addon_version,
    next_patch_version,
    runtime_changed_since,
)


def test_next_patch_version_is_monotonic_for_multi_digit_patch():
    assert next_patch_version("2.3.9", ["v2.3.10", "v2.3.7"]) == "2.3.11"


@pytest.mark.parametrize("value", ["2.3", "v2.3.5", "2.3.5rc1", "2.-1.0", ""])
def test_next_patch_version_rejects_invalid_source_version(value):
    with pytest.raises(ValueError, match="Kodi patch version"):
        next_patch_version(value, [])


def test_bump_addon_version_changes_only_version_attribute(tmp_path):
    addon = tmp_path / "addon.xml"
    addon.write_text('<addon id="plugin.video.gronkhtv" version="2.3.5" name="GronkhTV"/>\n', encoding="utf-8")
    bump_addon_version(addon, "2.3.6")
    assert addon.read_text(encoding="utf-8") == '<addon id="plugin.video.gronkhtv" version="2.3.6" name="GronkhTV"/>\n'


def test_runtime_change_detection_ignores_docs_tests_and_version_only(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "addon.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="2.3.5"/>\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=tmp_path, check=True)
    released = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    (tmp_path / "addon.xml").write_text('<addon id="plugin.video.gronkhtv" version="2.3.6"/>\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "bookkeeping"], cwd=tmp_path, check=True)
    assert runtime_changed_since(tmp_path, released) is False

    (tmp_path / "resources" / "runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "runtime"], cwd=tmp_path, check=True)
    assert runtime_changed_since(tmp_path, released) is True
