"""Durable account-isolated queue of failed progress POSTs, never a resume cache."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from account.progress import _offset


class SyncStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            # Additive upgrade: old progress/control tables are deliberately not
            # read, imported or deleted. They are not failed-POST records.
            db.execute("""CREATE TABLE IF NOT EXISTS pending_uploads (
                account_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                offset REAL NOT NULL,
                upload_id TEXT NOT NULL,
                PRIMARY KEY(account_id, video_id)
            )""")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, account_id, video_id, offset):
        offset = _offset(offset)
        if not account_id or not video_id:
            raise ValueError("account and video identifiers are required")
        record = {"account_id": account_id, "video_id": video_id,
                  "offset": offset, "upload_id": str(uuid4())}
        with self._connection() as db:
            db.execute("""INSERT INTO pending_uploads VALUES (:account_id, :video_id, :offset, :upload_id)
                ON CONFLICT(account_id, video_id) DO UPDATE SET
                offset=excluded.offset, upload_id=excluded.upload_id""", record)
        return record

    def pending(self, account_id, video_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM pending_uploads WHERE account_id=? AND video_id=?",
                             (account_id, video_id)).fetchone()
        return dict(row) if row else None

    def pending_uploads(self, account_id):
        with self._connection() as db:
            rows = db.execute("SELECT * FROM pending_uploads WHERE account_id=? ORDER BY video_id",
                              (account_id,)).fetchall()
        return [dict(row) for row in rows]

    def remove(self, record):
        if record is None:
            return True
        with self._connection() as db:
            deleted = db.execute("""DELETE FROM pending_uploads
                WHERE account_id=:account_id AND video_id=:video_id AND upload_id=:upload_id""", record)
            return deleted.rowcount == 1

    def display_status(self, account_id):
        return "pending" if self.pending_uploads(account_id) else "synchronized"
