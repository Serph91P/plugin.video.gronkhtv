"""Non-blocking, crash-released serialization of local progress operations.

The file is just a kernel-lock handle. Its presence/content never represents
progress, ownership epochs, a lease expiry or a completed operation.
"""
import os
import threading
from contextlib import contextmanager
from pathlib import Path

_HELD = threading.local()


@contextmanager
def progress_operation(database_path):
    key = os.path.abspath(str(database_path))
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = _HELD.paths = set()
    if key in held:
        yield True
        return
    handle = None
    try:
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        handle = open(key + ".operation.lock", "a+b")
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if handle is not None:
            handle.close()
        yield False
        return
    held.add(key)
    try:
        yield True
    finally:
        held.remove(key)
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
