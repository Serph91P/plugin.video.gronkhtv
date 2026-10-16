"""Process-safe, account-isolated persistent queue for progress snapshots."""

from contextlib import closing
import json
import os
import sqlite3
import time
from pathlib import Path


class ContextLostError(RuntimeError):
    """A durable mutation no longer belongs to the active account epoch."""


class SyncStore:
    def __init__(self, path, clock=time.time, timeout=3, initialize=True):
        self.path = str(path)
        self._clock = clock
        self._timeout = timeout
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._initialize()
            self.apply_observed_opt_out()

    def _opt_out_epoch(self):
        # Append-only invalidation journal: an observed off event must survive
        # a busy SQLite writer AND a subsequent process exit/re-enable. Entries
        # contain epochs only, never account/session/progress data.
        try:
            with open(self.path + ".optout", encoding="ascii") as journal:
                return max((int(line) for line in journal), default=-1)
        except FileNotFoundError:
            return -1

    def _is_opted_out_epoch(self, epoch):
        """Return whether a journaled off event fences this context generation."""
        # One successor can bind after an observer reads its cutoff and before
        # its append is durable, so the journal must fence both generations.
        cutoff = self._opt_out_epoch()
        return cutoff >= 0 and epoch <= cutoff + 1

    def observe_opt_out(self):
        """Journal the cutoff without waiting for a SQLite writer."""
        fast = SyncStore(self.path, self._clock, timeout=0, initialize=False)
        epoch = fast.context()["epoch"]
        if fast._opt_out_epoch() < epoch:
            fd = os.open(self.path + ".optout", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (str(epoch) + "\n").encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
        try:
            fast.apply_observed_opt_out()
        except sqlite3.Error:
            # The journal fences the captured generation and its successor
            # before SQLite can materialize the disabled state.
            pass

    def apply_observed_opt_out(self):
        cutoff = self._opt_out_epoch()
        if cutoff < 0:
            return
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            # A validation may bind the next epoch after this observer read the
            # disabled context but before the cutoff reached the journal. Fence
            # that one successor too; a completed cutoff advances past it so a
            # genuinely fresh validation may reconcile afterwards.
            db.execute(
                """UPDATE control SET active_account_id=NULL, epoch=MAX(epoch+1, ?), sync_enabled=0,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL
                   WHERE singleton=1 AND epoch<=?""", (cutoff + 2, cutoff + 1),
            )
            db.execute("COMMIT")

    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with closing(self._connection()) as db:
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
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = max(db.execute("SELECT epoch FROM control WHERE singleton=1").fetchone()[0], self._opt_out_epoch()) + 1
            db.execute(
                """UPDATE control SET active_account_id=?, epoch=?, sync_enabled=?,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL WHERE singleton=1""",
                (account_id, epoch, int(bool(enabled))),
            )
            db.execute("COMMIT")
            return epoch

    def reconcile(self, account_id, expected_epoch, allowed=None):
        """Restore a validated account only if no newer auth event won the race."""
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account id is required")
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute(
                "SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1"
            ).fetchone()
            if control["epoch"] != expected_epoch or (allowed is not None and not allowed()):
                db.execute("ROLLBACK")
                return None
            # A cutoff invalidates in-flight validation even when already disabled.
            if self._is_opted_out_epoch(control["epoch"]):
                db.execute("ROLLBACK")
                return None
            if control["active_account_id"] == account_id and control["sync_enabled"]:
                db.execute("ROLLBACK")
                return control["epoch"]
            epoch = max(control["epoch"], self._opt_out_epoch()) + 1
            db.execute(
                """UPDATE control SET active_account_id=?, epoch=?, sync_enabled=1,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL WHERE singleton=1""",
                (account_id, epoch),
            )
            db.execute("COMMIT")
            return epoch

    def disable(self):
        """Persist observed opt-out, idempotently, without deleting pending data."""
        return self._deactivate(only_enabled=True)

    def deactivate_if_epoch(self, expected_epoch):
        return self._deactivate(expected_epoch=expected_epoch)

    def deactivate(self):
        return self._deactivate()

    def _deactivate(self, expected_epoch=None, only_enabled=False):
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            if ((expected_epoch is not None and control["epoch"] != expected_epoch)
                    or (only_enabled and not control["sync_enabled"])):
                db.execute("ROLLBACK")
                return None
            epoch = control["epoch"] + 1
            db.execute(
                """UPDATE control SET active_account_id=NULL, epoch=?, sync_enabled=0,
                   lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL WHERE singleton=1""",
                (epoch,),
            )
            db.execute("COMMIT")
            return epoch

    def context(self):
        with closing(self._connection()) as db:
            row = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            return dict(row)

    def is_active_context(self, account_id, epoch):
        """Check a job's immutable identity immediately before network I/O."""
        with closing(self._connection()) as db:
            row = db.execute(
                "SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1"
            ).fetchone()
            return (
                row["active_account_id"] == account_id
                and row["epoch"] == epoch
                and bool(row["sync_enabled"])
                and not self._is_opted_out_epoch(epoch)
            )

    def display_status(self):
        """Return a UI-safe summary derived only from durable local state."""
        context = self.context()
        account_id = context["active_account_id"]
        if not account_id or not context["sync_enabled"]:
            return "authentication_required"
        with closing(self._connection()) as db:
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

    def record_local(self, account_id, epoch, video_id, offset, duration=0):
        _non_negative(offset, "offset")
        _non_negative(duration, "duration")
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            if (control["active_account_id"] != account_id or control["epoch"] != epoch
                    or not control["sync_enabled"] or self._is_opted_out_epoch(epoch)):
                db.execute("ROLLBACK")
                raise ContextLostError("account is not the active sync context")
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
        with closing(self._connection()) as db:
            row = db.execute("SELECT * FROM progress WHERE account_id=? AND video_id=? AND pending=1", (account_id, video_id)).fetchone()
            return dict(row) if row else None

    def observe_remote(self, account_id, epoch, video_id, remote):
        """Persist only a successful remote read; failures must not look absent."""
        if not isinstance(remote, dict) or remote.get("status") not in {"valid", "absent"}:
            raise ValueError("only successful remote observations are persistable")
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            if (
                control["active_account_id"] != account_id
                or control["epoch"] != epoch
                or not control["sync_enabled"]
                or self._is_opted_out_epoch(epoch)
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
        with closing(self._connection()) as db:
            row = db.execute("SELECT observed_json FROM progress WHERE account_id=? AND video_id=?", (account_id, video_id)).fetchone()
            if not row or row["observed_json"] is None:
                return None
            return json.loads(row["observed_json"])

    def rebase_pending_on_observed_remote(self, account_id, epoch, video_id):
        """Record an explicit Kodi-choice against the current cloud baseline."""
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            row = db.execute(
                "SELECT offset, pending, observed_json FROM progress WHERE account_id=? AND video_id=?",
                (account_id, video_id),
            ).fetchone()
            if (
                control["active_account_id"] != account_id
                or control["epoch"] != epoch
                or not control["sync_enabled"]
                or self._is_opted_out_epoch(epoch)
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

    def accept_observed_remote(self, account_id, epoch, video_id):
        """Discard a pending Kodi choice only after the user selects account."""
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT active_account_id, epoch, sync_enabled FROM control WHERE singleton=1").fetchone()
            row = db.execute(
                "SELECT pending, observed_json FROM progress WHERE account_id=? AND video_id=?",
                (account_id, video_id),
            ).fetchone()
            if (
                control["active_account_id"] != account_id
                or control["epoch"] != epoch
                or not control["sync_enabled"]
                or self._is_opted_out_epoch(epoch)
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

    def claim_upload(self, account_id, lease_seconds, epoch=None):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            now = self._clock()
            control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            if (control["active_account_id"] != account_id or not control["sync_enabled"]
                    or self._is_opted_out_epoch(control["epoch"])
                    or (epoch is not None and control["epoch"] != epoch)):
                db.execute("ROLLBACK")
                return None
            if control["lease_expires"] is not None and control["lease_expires"] > now:
                db.execute("ROLLBACK")
                return None
            fence = control["fence"] + 1
            db.execute("UPDATE control SET fence=?, lease_account_id=?, lease_fence=?, lease_expires=? WHERE singleton=1", (fence, account_id, fence, now + lease_seconds))
            db.execute("COMMIT")
            return {"account_id": account_id, "epoch": control["epoch"], "fence": fence}

    def confirm_upload(self, account_id, epoch, lease, video_id, revision, confirmed=None):
        if not lease:
            return False
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            valid = self._owns_lease(control, account_id, epoch, lease)
            row = db.execute("SELECT revision FROM progress WHERE account_id=? AND video_id=?", (account_id, video_id)).fetchone()
            if not valid or not row or row["revision"] != revision:
                db.execute("ROLLBACK")
                return False
            db.execute(
                """UPDATE progress SET pending=0, confirmed_json=? WHERE account_id=? AND video_id=?""",
                (json.dumps(confirmed) if confirmed is not None else None, account_id, video_id),
            )
            db.execute(
                """UPDATE control SET lease_account_id=NULL, lease_fence=NULL, lease_expires=NULL
                   WHERE singleton=1 AND lease_account_id=? AND lease_fence=?""",
                (account_id, lease.get("fence")),
            )
            db.execute("COMMIT")
            return True

    def owns_lease(self, account_id, epoch, lease):
        with closing(self._connection()) as db:
            control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            return self._owns_lease(control, account_id, epoch, lease)

    def _owns_lease(self, control, account_id, epoch, lease):
        return bool(
            lease
            and lease.get("account_id") == account_id
            and lease.get("epoch") == epoch
            and control["active_account_id"] == account_id
            and control["epoch"] == epoch
            and control["sync_enabled"]
            and not self._is_opted_out_epoch(epoch)
            and control["lease_account_id"] == account_id
            and control["lease_fence"] == lease.get("fence")
            and control["lease_expires"] is not None
            and control["lease_expires"] > self._clock()
        )


def _non_negative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be non-negative")
