"""Coordinator for durable account-progress snapshots.

Network work is deliberately separate from Kodi playback hooks: hooks call
``record_position`` only; a bounded worker may call ``flush`` afterwards.
"""

import json

from account.progress import wire_offset
from account.session import AuthenticationError, RateLimitError, SessionError, ValidationError
from account.sync_store import ContextLostError


class _ContextLostError(Exception):
    """The immutable account/epoch no longer owns this queued job."""


class ProgressSync:
    def __init__(self, store, api, account_id, epoch, lease_seconds=5, max_write_attempts=2):
        self._store = store
        self._api = api
        self._account_id = account_id
        self._epoch = epoch
        self._lease_seconds = lease_seconds
        self._max_write_attempts = max_write_attempts

    def prepare_playback(self, video_id):
        # Fence before the GET as well as the upload: a stale background job
        # must not even read account A progress after a logout/account switch.
        if not self._store.is_active_context(self._account_id, self._epoch):
            return {"state": "pending", "offset": None}
        snapshot_before_read = self._store.pending(self._account_id, video_id)
        read_fence = self._store.read_fence()
        try:
            remote = self._api.read(video_id)
        except AuthenticationError:
            return {"state": "authentication_required", "offset": None}
        except Exception:
            return {"state": "unavailable", "offset": None}
        # Do not persist or act on a response received after the context was
        # invalidated.  Keep its durable local snapshot pending instead.
        if not self._store.is_active_context(self._account_id, self._epoch):
            return {"state": "pending", "offset": None}
        if snapshot_before_read is not None:
            current = self._store.pending(self._account_id, video_id)
            if current is None or current["revision"] != snapshot_before_read["revision"]:
                return {"state": "pending", "offset": None}
        if remote.status == "absent":
            try:
                accepted = self._store.observe_remote(self._account_id, self._epoch, video_id, {"status": "absent", "_read_fence": read_fence})
            except ContextLostError:
                return {"state": "pending", "offset": None}
            if not accepted:
                return {"state": "pending", "offset": None}
            return {"state": "absent", "offset": None}
        if remote.status != "valid":
            return {"state": "unavailable", "offset": None}
        observed = {
            "status": "valid",
            "offset": remote.offset,
            "updated_at": remote.updated_at,
            "marked_as_watched": remote.marked_as_watched,
            "_read_fence": read_fence,
        }
        snapshot = self._store.pending(self._account_id, video_id)
        preflight = {"state": "valid", "offset": remote.offset}
        try:
            accepted = self._store.observe_remote(self._account_id, self._epoch, video_id, observed)
        except ContextLostError:
            return {"state": "pending", "offset": None}
        if not accepted:
            return {"state": "pending", "offset": None}
        if snapshot is not None and _remote_changed_since_confirmed(snapshot, preflight):
            return {
                "state": "conflict",
                "offset": snapshot["offset"],
                "remote_offset": remote.offset,
            }
        return preflight

    def record_position(self, video_id, offset, duration, event="tick"):
        # Event is retained at the boundary to make callers explicit; the store
        # coalesces by account/video and accepts legitimate reverse seeks and 0.
        return self._store.record_local(self._account_id, video_id, offset, duration)

    def begin_playback_writer(self, video_id):
        """Reserve a short cross-invoker lease while a monitor can commit."""
        return self._store.claim_playback_writer(
            self._account_id, self._epoch, video_id, self._lease_seconds
        )

    def renew_playback_writer(self, video_id, lease):
        return self._store.renew_playback_writer(
            self._account_id, self._epoch, video_id, lease, self._lease_seconds
        )

    def finish_playback_writer(self, video_id, lease):
        return self._store.release_playback_writer(self._account_id, video_id, lease)

    def reset(self, video_id):
        """Synchronously publish an explicit selected-video reset to the account.

        Unlike regular samples, an explicit reset intentionally writes zero and
        bypasses the conflict preflight.  It still shares the account epoch,
        lease and revision fencing with every background uploader.  A busy
        lease leaves the zero intent pending instead of racing a second POST.
        """
        try:
            snapshot = self._store.record_reset_if_idle(
                self._account_id, self._epoch, video_id
            )
        except Exception:
            return "pending"
        if snapshot is None:
            return "pending"
        lease = self._store.claim_upload(
            self._account_id, self._lease_seconds, video_id, snapshot["revision"]
        )
        if lease is None or lease["epoch"] != self._epoch:
            return "pending"
        if not self._store.is_active_context(self._account_id, self._epoch):
            return "pending"
        observed_before_write = self._store.observed_remote(self._account_id, video_id)
        try:
            self._write_bounded(video_id, 0, lease, snapshot["revision"])
        except _ContextLostError:
            return "pending"
        except AuthenticationError:
            return "authentication_required"
        except ValidationError:
            return "invalid"
        except RateLimitError:
            return "rate_limited"
        except Exception:
            return "pending"
        if self._store.confirm_upload(
            self._account_id,
            self._epoch,
            lease,
            video_id,
            snapshot["revision"],
            confirmed={"offset": 0},
            observed_before_write=observed_before_write,
        ):
            return "synchronized"
        return "pending"

    def resolve_conflict(self, video_id, choice):
        """Apply a foreground user's conflict choice without a network call."""
        if choice == "kodi":
            offset = self._store.rebase_pending_on_observed_remote(self._account_id, video_id)
        elif choice == "account":
            offset = self._store.accept_observed_remote(self._account_id, video_id)
        else:
            raise ValueError("conflict choice must be kodi or account")
        if offset is None:
            raise RuntimeError("conflict can no longer be resolved in this account context")
        return offset

    def flush(self, video_id):
        snapshot = self._store.pending(self._account_id, video_id)
        if snapshot is None:
            return "synchronized"
        # A durable offline snapshot is never replayed blindly.  Re-read first;
        # failed or malformed reads remain pending and do not become "absent".
        preflight = self.prepare_playback(video_id)
        if preflight["state"] == "authentication_required":
            return "authentication_required"
        if preflight["state"] == "unavailable":
            return "pending"
        if preflight["state"] == "pending":
            return "pending"
        if preflight["state"] == "conflict":
            return "conflict"
        if _remote_changed_since_confirmed(snapshot, preflight):
            return "conflict"
        if not self._store.is_current_revision(
            self._account_id, self._epoch, video_id, snapshot["revision"]
        ):
            return "pending"
        observed_before_write = self._store.observed_remote(self._account_id, video_id)
        lease = self._store.claim_upload(
            self._account_id, self._lease_seconds, video_id, snapshot["revision"]
        )
        if lease is None or lease["epoch"] != self._epoch:
            return "pending"
        # An epoch change between lease acquisition and request dispatch fences
        # the job before it can send account A's record with a newer session.
        if not self._store.is_active_context(self._account_id, self._epoch):
            return "pending"
        sent_offset = wire_offset(snapshot["offset"])
        try:
            self._write_bounded(video_id, sent_offset, lease, snapshot["revision"])
        except _ContextLostError:
            return "pending"
        except AuthenticationError:
            return "authentication_required"
        except ValidationError:
            return "invalid"
        except RateLimitError:
            return "rate_limited"
        except Exception:
            return "pending"
        if self._store.confirm_upload(
            self._account_id,
            self._epoch,
            lease,
            video_id,
            snapshot["revision"],
            confirmed={"offset": sent_offset},
            observed_before_write=observed_before_write,
        ):
            return "synchronized"
        return "pending"

    def _write_bounded(self, video_id, offset, lease, revision):
        """Retry only transient session failures; never sleep or loop in Kodi UI."""
        attempts = max(1, self._max_write_attempts)
        last_error = None
        for _ in range(attempts):
            if not self._store.owns_upload(
                self._account_id, self._epoch, lease, video_id, revision
            ):
                raise _ContextLostError()
            try:
                return self._api.write(video_id, offset)
            except (AuthenticationError, ValidationError, RateLimitError):
                raise
            except SessionError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("no upload attempt was made")


def _remote_changed_since_confirmed(snapshot, preflight):
    """Detect a remote divergence without comparing local clocks."""
    raw_confirmed = snapshot.get("confirmed_json")
    if not raw_confirmed or preflight.get("state") != "valid":
        return False
    try:
        confirmed = json.loads(raw_confirmed)
    except (TypeError, ValueError):
        return True
    return confirmed.get("offset") != preflight.get("offset")
