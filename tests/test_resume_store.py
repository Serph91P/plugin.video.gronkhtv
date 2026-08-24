import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from resume_store import (  # noqa: E402
    is_completed,
    read_record,
    remove_record,
    write_record,
)


class FakeFile:
    def __init__(self, vfs, path, mode="r"):
        self.vfs = vfs
        self.path = path
        self.mode = mode
        self.content = "" if "w" in mode else vfs.files[path]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None and "w" in self.mode:
            self.vfs.files[self.path] = self.content
        return False

    def read(self):
        return self.content

    def write(self, content):
        self.content += content
        return True


class FakeVfs:
    def __init__(self, files=None, rename_succeeds=True, undeletable=None):
        self.files = dict(files or {})
        self.rename_succeeds = rename_succeeds
        self.undeletable = set(undeletable or [])
        self.directories = set()

    def File(self, path, mode="r"):
        return FakeFile(self, path, mode)

    def exists(self, path):
        return path in self.files or path in self.directories

    def mkdirs(self, path):
        self.directories.add(path)
        return True

    def rename(self, source, destination):
        if not self.rename_succeeds or destination in self.files:
            return False
        self.files[destination] = self.files.pop(source)
        return True

    def delete(self, path):
        if path in self.undeletable:
            return False
        return self.files.pop(path, None) is not None


def test_read_record_accepts_legacy_single_float_with_known_duration():
    vfs = FakeVfs({"resume/1105.txt": "123.5"})

    assert read_record(vfs, "resume/1105.txt", legacy_duration=300) == {
        "position": 123.5,
        "duration": 300.0,
    }


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not-json",
        "nan",
        '{"position": -1, "duration": 300}',
        '{"position": 10}',
        '{"position": true, "duration": 300}',
        '{"position": 10, "duration": "inf"}',
    ],
)
def test_read_record_treats_malformed_or_invalid_state_as_absent(content):
    vfs = FakeVfs({"resume/1105.txt": content})

    assert read_record(vfs, "resume/1105.txt", legacy_duration=300) is None


def test_write_record_atomically_replaces_complete_valid_json_record():
    vfs = FakeVfs({"resume/1105.txt": "old"})

    write_record(vfs, "resume", "resume/1105.txt", 123.5, 300)

    assert vfs.files == {
        "resume/1105.txt": '{"duration":300.0,"position":123.5}'
    }


def test_failed_atomic_replacement_preserves_previous_record_and_removes_temp():
    vfs = FakeVfs(
        {"resume/1105.txt": '{"duration":300.0,"position":100.0}'},
        rename_succeeds=False,
    )

    with pytest.raises(OSError, match="atomically replace"):
        write_record(vfs, "resume", "resume/1105.txt", 120, 300)

    assert vfs.files == {
        "resume/1105.txt": '{"duration":300.0,"position":100.0}'
    }


def test_read_record_recovers_backup_left_by_interrupted_replacement():
    vfs = FakeVfs(
        {"resume/1105.txt.bak": '{"duration":300.0,"position":100.0}'}
    )

    assert read_record(vfs, "resume/1105.txt") == {
        "position": 100.0,
        "duration": 300.0,
    }


def test_completed_threshold_and_cleanup_are_exactly_95_percent():
    vfs = FakeVfs({"resume/1105.txt": "state"})

    assert is_completed({"position": 284.9, "duration": 300}) is False
    assert is_completed({"position": 285, "duration": 300}) is True
    assert remove_record(vfs, "resume/1105.txt") is True
    assert "resume/1105.txt" not in vfs.files


def test_failed_backup_cleanup_keeps_current_record_available():
    vfs = FakeVfs(
        {
            "resume/1105.txt": '{"duration":300.0,"position":285.0}',
            "resume/1105.txt.bak": '{"duration":300.0,"position":100.0}',
        },
        undeletable={"resume/1105.txt.bak"},
    )

    assert remove_record(vfs, "resume/1105.txt") is False
    assert "resume/1105.txt" in vfs.files
    assert "resume/1105.txt.bak" in vfs.files
