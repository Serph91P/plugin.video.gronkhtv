"""Process-safe, account-isolated persistent queue for progress snapshots."""

import json
import os
import sqlite3
import time
from pathlib import Path


class ContextLostError(RuntimeError):
    """A durable mutation no longer belongs to the active account epoch."""


class SyncStore:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self._clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    active_account_id TEXT,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    sync_enabled INTEGER NOT NULL DEFAULT 0,
                    fence INTEGER NOT NULL DEFAULT 0,
                    lease_account_id TEXT,
                    lease_fence INTEGER,
                    lease_expires REAL
                );
                INSERT OR IGNORE INTO control(singleton) VALUES (1);
                CREATE TABLE IF NOT EXISTS progress (
                    account_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    offset REAL NOT NULL,
                    duration REAL NOT NULL,
                    revision INTEGER NOT NULL,
                    pending INTEGER NOT NULL,
                    confirmed_json TEXT,
                    observed_json TEXT,
                    PRIMARY KEY(account_id, video_id)
                );
                """
            )

    def activate(self, account_id, enabled):
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account id is required")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = db.execute("SELECT epoch FROM control WHERE singleton=1").fetchone()[0] + 1
            db.execute(
                """UPDATE control SET active_account_id=?, epoch=?, sync_enabled=?,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL WHERE singleton=1""",
                (account_id, epoch, int(bool(enabled))),
            )
            db.execute("COMMIT")
            return epoch

    def reconcile(self, account_id, expected_epoch):
        """Restore a validated account only if no newer auth event won the race."""
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account id is required")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute(
                "SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1"
            ).fetchone()
            if control["epoch"] != expected_epoch:
                db.execute("ROLLBACK")
                return None
            if control["active_account_id"] == account_id and control["sync_enabled"]:
                db.execute("ROLLBACK")
                return control["epoch"]
            epoch = control["epoch"] + 1
            db.execute(
                """UPDATE control SET active_account_id=?, epoch=?, sync_enabled=1,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL WHERE singleton=1""",
                (account_id, epoch),
            )
            db.execute("COMMIT")
            return epoch

    def deactivate(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = db.execute("SELECT epoch FROM control WHERE singleton=1").fetchone()[0] + 1
            db.execute(
                """UPDATE control SET active_account_id=NULL, epoch=?, sync_enabled=0,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL WHERE singleton=1""",
                (epoch,),
            )
            db.execute("COMMIT")
            return epoch

    def context(self):
        with self._connection() as db:
            row = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            return dict(row)

    def is_active_context(self, account_id, epoch):
        """Check a job's immutable identity immediately before network I/O."""
        with self._connection() as db:
            row = db.execute(
                "SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1"
            ).fetchone()
            return (
                row["active_account_id"] == account_id
                and row["epoch"] == epoch
                and bool(row["sync_enabled"])
            )

    def display_status(self):
        """Return a UI-safe summary derived only from durable local state."""
        context = self.context()
        account_id = context["active_account_id"]
        if not account_id or not context["sync_enabled"]:
            return "authentication_required"
        with self._connection() as db:
            rows = db.execute(
                "SELECT confirmed_json, observed_json FROM progress WHERE account_id=? AND pending=1",
                (account_id,),
            ).fetchall()
        for row in rows:
            try:
                confirmed = json.loads(row["confirmed_json"]) if row["confirmed_json"] else None
                observed = json.loads(row["observed_json"]) if row["observed_json"] else None
            except (TypeError, ValueError):
                return "pending"
            if (
                confirmed
                and observed
                and observed.get("status") == "valid"
                and confirmed.get("offset") != observed.get("offset")
            ):
                return "conflict"
        return "pending" if rows else "synchronized"

    def record_local(self, account_id, video_id, offset, duration=0):
        _non_negative(offset, "offset")
        _non_negative(duration, "duration")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, sync_enabled FROM control WHERE singleton=1").fetchone()
            if control["active_account_id"] != account_id or not control["sync_enabled"]:
                db.execute("ROLLBACK")
                raise RuntimeError("account is not the active sync context")
            row = db.execute("SELECT revision FROM progress WHERE account_id=? AND video_id=?", (account_id, video_id)).fetchone()
            revision = (row["revision"] if row else 0) + 1
            db.execute(
                """INSERT INTO progress(account_id, video_id, offset, duration, revision, pending)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(account_id, video_id) DO UPDATE SET
                     offset=excluded.offset, duration=excluded.duration,
                     revision=excluded.revision, pending=1""",
                (account_id, video_id, offset, duration, revision),
            )
            db.execute("COMMIT")
            return {"account_id": account_id, "video_id": video_id, "offset": offset, "duration": duration, "revision": revision}

    def pending(self, account_id, video_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM progress WHERE account_id=? AND video_id=? AND pending=1", (account_id, video_id)).fetchone()
            return dict(row) if row else None

    def observe_remote(self, account_id, epoch, video_id, remote):
        """Persist only a successful remote read; failures must not look absent."""
        if not isinstance(remote, dict) or remote.get("status") not in {"valid", "absent"}:
            raise ValueError("only successful remote observations are persistable")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            if (
                control["active_account_id"] != account_id
                or control["epoch"] != epoch
                or not control["sync_enabled"]
            ):
                db.execute("ROLLBACK")
                raise ContextLostError("account is not the active sync context")
            row = db.execute("SELECT revision FROM progress WHERE account_id=? AND video_id=?", (account_id, video_id)).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO progress(account_id, video_id, offset, duration, revision, pending, observed_json)
                       VALUES (?, ?, 0, 0, 0, 0, ?)""",
                    (account_id, video_id, json.dumps(remote, sort_keys=True)),
                )
            else:
                db.execute(
                    "UPDATE progress SET observed_json=? WHERE account_id=? AND video_id=?",
                    (json.dumps(remote, sort_keys=True), account_id, video_id),
                )
            db.execute("COMMIT")

    def observed_remote(self, account_id, video_id):
        with self._connection() as db:
            row = db.execute("SELECT observed_json FROM progress WHERE account_id=? AND video_id=?", (account_id, video_id)).fetchone()
            if not row or row["observed_json"] is None:
                return None
            return json.loads(row["observed_json"])

    def rebase_pending_on_observed_remote(self, account_id, video_id):
        """Record an explicit Kodi-choice against the current cloud baseline."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, sync_enabled FROM control WHERE singleton=1").fetchone()
            row = db.execute(
                "SELECT offset, pending, observed_json FROM progress WHERE account_id=? AND video_id=?",
                (account_id, video_id),
            ).fetchone()
            if (
                control["active_account_id"] != account_id
                or not control["sync_enabled"]
                or row is None
                or not row["pending"]
                or row["observed_json"] is None
            ):
                db.execute("ROLLBACK")
                return None
            observed = json.loads(row["observed_json"])
            if observed.get("status") != "valid":
                db.execute("ROLLBACK")
                return None
            db.execute(
                "UPDATE progress SET confirmed_json=? WHERE account_id=? AND video_id=?",
                (json.dumps(observed, sort_keys=True), account_id, video_id),
            )
            db.execute("COMMIT")
            return row["offset"]

    def accept_observed_remote(self, account_id, video_id):
        """Discard a pending Kodi choice only after the user selects account."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, sync_enabled FROM control WHERE singleton=1").fetchone()
            row = db.execute(
                "SELECT pending, observed_json FROM progress WHERE account_id=? AND video_id=?",
                (account_id, video_id),
            ).fetchone()
            if (
                control["active_account_id"] != account_id
                or not control["sync_enabled"]
                or row is None
                or not row["pending"]
                or row["observed_json"] is None
            ):
                db.execute("ROLLBACK")
                return None
            observed = json.loads(row["observed_json"])
            if observed.get("status") != "valid":
                db.execute("ROLLBACK")
                return None
            db.execute(
                """UPDATE progress SET offset=?, pending=0, confirmed_json=?
                   WHERE account_id=? AND video_id=?""",
                (
                    observed["offset"],
                    json.dumps(observed, sort_keys=True),
                    account_id,
                    video_id,
                ),
            )
            db.execute("COMMIT")
            return observed["offset"]

    def claim_upload(self, account_id, lease_seconds):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            if control["active_account_id"] != account_id or not control["sync_enabled"]:
                db.execute("ROLLBACK")
                return None
            if control["lease_expires"] is not None and control["lease_expires"] > now:
                db.execute("ROLLBACK")
                return None
            fence = control["fence"] + 1
            db.execute("UPDATE control SET fence=?, lease_account_id=?, lease_fence=?, lease_expires=? WHERE singleton=1", (fence, account_id, fence, now + lease_seconds))
            db.execute("COMMIT")
            return {"account_id": account_id, "epoch": control["epoch"], "fence": fence}

    def confirm_upload(self, account_id, epoch, lease, video_id, revision, confirmed=None, observed_before_write=None):
        if not lease:
            return False
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            valid = (
                control["active_account_id"] == account_id
                and control["epoch"] == epoch
                and control["lease_account_id"] == account_id
                and control["lease_fence"] == lease.get("fence")
                and control["lease_expires"] is not None
                and control["lease_expires"] > self._clock()
            )
            row = db.execute("SELECT revision FROM progress WHERE account_id=? AND video_id=?", (account_id, video_id)).fetchone()
            if not valid or not row:
                db.execute("ROLLBACK")
                return False
            if row["revision"] == revision:
                observed_json = json.dumps(observed_before_write, sort_keys=True) if observed_before_write is not None else None
                current = db.execute(
                    "SELECT observed_json FROM progress WHERE account_id=? AND video_id=?",
                    (account_id, video_id),
                ).fetchone()["observed_json"]
                update_observed = current == observed_json
                db.execute(
                    """UPDATE progress SET pending=0, confirmed_json=?,
                       observed_json=CASE WHEN ? THEN ? ELSE observed_json END
                       WHERE account_id=? AND video_id=?""",
                    (
                        json.dumps(confirmed) if confirmed is not None else None,
                        update_observed,
                        json.dumps({"status": "valid", "offset": confirmed["offset"]}, sort_keys=True) if confirmed is not None else None,
                        account_id,
                        video_id,
                    ),
                )
            else:
                # The POST succeeded for an older snapshot. Preserve the newer
                # pending position but retain its real server confirmation.
                db.execute(
                    "UPDATE progress SET confirmed_json=? WHERE account_id=? AND video_id=?",
                    (json.dumps(confirmed) if confirmed is not None else None, account_id, video_id),
                )
            db.execute(
                """UPDATE control SET lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL
                   WHERE singleton=1 AND lease_account_id=? AND lease_fence=?""",
                (account_id, lease.get("fence")),
            )
            db.execute("COMMIT")
            return row["revision"] == revision


def _non_negative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be non-negative")
