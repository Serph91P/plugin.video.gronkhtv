import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import pytest

from watched_store import read_marker, write_marker  # noqa: E402


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
    def __init__(self, files=None, rename_succeeds=True):
        self.files = dict(files or {})
        self.directories = set()
        self.rename_succeeds = rename_succeeds

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
        return self.files.pop(path, None) is not None


def test_write_marker_persists_a_watched_episode_for_a_new_plugin_instance():
    vfs = FakeVfs()

    write_marker(vfs, "watched", "watched/1105.json")

    assert read_marker(vfs, "watched/1105.json") is True


def test_read_marker_treats_malformed_persisted_data_as_unwatched():
    vfs = FakeVfs({"watched/1105.json": '{"watched": true}'})

    assert read_marker(vfs, "watched/1105.json") is False


def test_failed_marker_replacement_preserves_previous_watched_state():
    vfs = FakeVfs({"watched/1105.json": "true"}, rename_succeeds=False)

    with pytest.raises(OSError, match="atomically replace"):
        write_marker(vfs, "watched", "watched/1105.json")

    assert read_marker(vfs, "watched/1105.json") is True
