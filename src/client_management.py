"""Private, background client management telemetry.

The client keeps a small durable queue of coarse application events.  This
module deliberately has no Qt dependency: the UI starts one worker thread at
an explicit point, while tests can use :meth:`sync_once` with an injected
transport and never touch the network.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests

from src.utils import get_user_data_path


APP_VERSION = "1.1.0"
SYNC_URL = "https://998223.xyz/campusfly/api/v1/sync"
STATE_FILE_NAME = "management.local.json"
MAX_QUEUE_SIZE = 1000
SYNC_BATCH_SIZE = 100
SYNC_INTERVAL_SECONDS = 60.0
SYNC_TIMEOUT_SECONDS = 5.0
MAX_SYNC_PAYLOAD_BYTES = 64 * 1024
EVENT_KINDS = frozenset({"install", "submission_attempt", "submission_success"})
_TOKEN_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# All instances in one process use the same lock.  This matters for tests
# that create two managers against one temporary state file and also prevents
# a background flush from racing a submission callback.
_STATE_LOCK = threading.RLock()


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one management request.

    ``success`` means the response passed every shape and acknowledgement
    check.  A failed request never changes the cached disabled state or queue.
    """

    success: bool
    disabled: bool
    acknowledged: tuple[str, ...] = ()
    error: str | None = None
    announcement: dict[str, str] | None = None


class ManagementWorker(threading.Thread):
    """One daemon worker that polls and wakes after a newly queued event."""

    def __init__(self, manager: "ClientManagement") -> None:
        super().__init__(name="SJTU-CampusFly-management", daemon=True)
        self.manager = manager
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    def wake(self) -> None:
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def run(self) -> None:
        # An immediate request records an installation without making the UI
        # wait for the first 60-second interval.  The request is still made
        # off the Qt thread.
        if self._stop_event.is_set():
            return
        self.manager.sync_once()
        while not self._stop_event.is_set():
            self._wake_event.wait(self.manager.sync_interval_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self.manager.sync_once()


class ClientManagement:
    """Durable event queue and remote service-state client.

    ``transport`` is an injection seam for tests.  It accepts one payload
    mapping and may return a mapping, ``(status_code, mapping)``, or a
    requests-like response.  The production transport always uses HTTPS,
    normal certificate verification, and redirects disabled.
    """

    def __init__(
        self,
        *,
        state_path: str | os.PathLike[str] | None = None,
        sync_url: str = SYNC_URL,
        version: str = APP_VERSION,
        transport: Callable[..., Any] | None = None,
        on_state_changed: Callable[[bool], None] | None = None,
        on_announcement: Callable[[dict[str, str]], None] | None = None,
        max_queue_size: int = MAX_QUEUE_SIZE,
        batch_size: int = SYNC_BATCH_SIZE,
        sync_interval_seconds: float = SYNC_INTERVAL_SECONDS,
        timeout_seconds: float = SYNC_TIMEOUT_SECONDS,
    ) -> None:
        self.state_path = Path(state_path) if state_path is not None else (
            Path(get_user_data_path()) / "configs" / STATE_FILE_NAME
        )
        self.sync_url = str(sync_url)
        self.version = str(version)
        self._transport = transport
        self._on_state_changed = on_state_changed
        self._on_announcement = on_announcement
        self.max_queue_size = max(1, int(max_queue_size))
        self.batch_size = max(1, min(int(batch_size), SYNC_BATCH_SIZE))
        self.sync_interval_seconds = max(1.0, float(sync_interval_seconds))
        self.timeout_seconds = min(max(float(timeout_seconds), 0.1), SYNC_TIMEOUT_SECONDS)
        self._worker: ManagementWorker | None = None
        self._closed = False
        self._startup_sync_done = False
        self._state = self._load_or_create_state()

        parsed = urlsplit(self.sync_url)
        if parsed.scheme.lower() != "https":
            raise ValueError("管理同步地址必须使用 HTTPS。")
        if self._transport is None and self.sync_url != SYNC_URL:
            raise ValueError("生产管理同步地址不可更改。")

    @property
    def worker(self) -> ManagementWorker | None:
        return self._worker

    @property
    def disabled(self) -> bool:
        with _STATE_LOCK:
            return bool(self._state["disabled"])

    @property
    def installation_id(self) -> str:
        with _STATE_LOCK:
            return str(self._state["installation_id"])

    @property
    def token(self) -> str:
        """Return the local token for diagnostics/tests; never log it."""
        with _STATE_LOCK:
            return str(self._state["token"])

    @property
    def last_seen_announcement_id(self) -> str | None:
        with _STATE_LOCK:
            return self._state.get("last_seen_announcement_id")

    def stats(self) -> dict[str, Any]:
        with _STATE_LOCK:
            counters = dict(self._state["counters"])
            return {
                "installation_id": self._state["installation_id"],
                "disabled": bool(self._state["disabled"]),
                "queued_events": len(self._state["events"]),
                "install_count": int(counters.get("install", 0)),
                "submission_attempt_count": int(counters.get("submission_attempt", 0)),
                "submission_success_count": int(counters.get("submission_success", 0)),
            }

    def start(self) -> ManagementWorker:
        """Start the one background worker; construction itself never starts it."""
        with _STATE_LOCK:
            if self._closed:
                raise RuntimeError("管理同步客户端已关闭。")
            if self._worker is None or not self._worker.is_alive():
                self._worker = ManagementWorker(self)
                self._worker.start()
            return self._worker

    def stop(self, timeout: float = 6.0) -> None:
        """Stop the worker and wait a bounded time for a short request timeout."""
        with _STATE_LOCK:
            self._closed = True
            worker = self._worker
        if worker is not None:
            worker.stop()
            if worker is not threading.current_thread():
                worker.join(max(0.0, float(timeout)))

    def record_event(
        self,
        kind: str,
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        """Durably enqueue one idempotent event and wake the worker."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unsupported management event kind: {kind!r}")
        event = {
            "id": self._validate_event_id(event_id or str(uuid.uuid4())),
            "kind": kind,
            "occurred_at": occurred_at or _utc_now(),
        }
        self._validate_event(event)
        changed = False
        with _STATE_LOCK:
            if not any(item["id"] == event["id"] for item in self._state["events"]):
                self._state["events"].append(event)
                counters = self._state["counters"]
                counters[kind] = int(counters.get(kind, 0)) + 1
                self._trim_queue_locked()
                self._write_state_locked()
                changed = True
        if changed:
            worker = self._worker
            if worker is not None:
                worker.wake()
        return event["id"]

    def mark_announcement_seen(self, announcement_id: str) -> bool:
        """Persist an announcement ID after the UI has displayed it."""
        canonical = self._validate_event_id(announcement_id)
        with _STATE_LOCK:
            self._state["last_seen_announcement_id"] = canonical
            return self._write_state_locked()

    def sync_once(self) -> SyncResult:
        """Send one immutable queue snapshot and apply a valid response."""
        with _STATE_LOCK:
            snapshot = copy.deepcopy(self._state["events"][: self.batch_size])
            installation_id = str(self._state["installation_id"])
            token = str(self._state["token"])
        payload = {
            "installation_id": installation_id,
            "token": token,
            "version": self.version,
            "events": snapshot,
        }
        try:
            encoded_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if encoded_size > MAX_SYNC_PAYLOAD_BYTES:
                raise ValueError("management payload exceeds the size limit")
            raw_response = self._call_transport(payload)
            status_code, response_payload = _unpack_transport_response(raw_response)
            disabled, acknowledged, announcement = _validate_sync_response(
                response_payload, status_code, snapshot
            )
        except Exception as exc:
            # This service is best-effort.  Avoid exposing request bodies or
            # exception strings because either can contain the private token.
            return SyncResult(False, self.disabled, error=type(exc).__name__)

        acknowledged_set = set(acknowledged)
        with _STATE_LOCK:
            # Remove only IDs from this immutable snapshot.  Events queued
            # while the request was in flight remain in the current queue.
            if acknowledged_set:
                self._state["events"] = [
                    item for item in self._state["events"]
                    if item["id"] not in acknowledged_set
                ]
            previous_disabled = bool(self._state["disabled"])
            self._state["disabled"] = disabled
            self._write_state_locked()
        if previous_disabled != disabled and not self._closed:
            self._notify_state_changed(disabled)
        with _STATE_LOCK:
            first_startup_sync = not self._startup_sync_done
            if first_startup_sync:
                self._startup_sync_done = True
            last_seen_announcement_id = self._state.get("last_seen_announcement_id")
        if (
            first_startup_sync
            and announcement is not None
            and announcement["id"] != last_seen_announcement_id
            and not self._closed
        ):
            self._notify_announcement(announcement)
        return SyncResult(True, disabled, tuple(acknowledged), announcement=announcement)

    def _call_transport(self, payload: Mapping[str, Any]) -> Any:
        if self._transport is None:
            # Do not follow redirects: a redirect could send the installation
            # token to another host.  ``verify=True`` keeps normal TLS checks.
            return requests.post(
                self.sync_url,
                json=dict(payload),
                timeout=self.timeout_seconds,
                allow_redirects=False,
                verify=True,
            )
        return self._transport(dict(payload))

    def _load_or_create_state(self) -> dict[str, Any]:
        with _STATE_LOCK:
            try:
                state_file_exists = self.state_path.exists()
            except OSError:
                state_file_exists = True
            corrupt_file = False
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                state = _normalise_state(payload, self.max_queue_size)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                state = _new_state()
                corrupt_file = state_file_exists
                if state_file_exists:
                    # A damaged or unreadable management file must never make
                    # the UI unusable, but silently falling back to an
                    # enabled fresh identity could bypass a prior stop state.
                    # Keep the safe local state disabled and preserve the
                    # original file where possible.
                    state["disabled"] = True
            self._state = state
            # A newly generated state queues exactly one install event.  An
            # existing valid state is never counted again on restart.
            if not state["events"] and state["counters"].get("install", 0) == 0:
                self._append_install_locked(state)
            if corrupt_file:
                try:
                    backup = self.state_path.with_name(self.state_path.name + ".corrupt")
                    if not backup.exists():
                        shutil.copy2(self.state_path, backup)
                except OSError:
                    pass
            self._write_state_locked()
            return state

    def _append_install_locked(self, state: dict[str, Any]) -> None:
        state["events"].append({
            "id": str(uuid.uuid4()),
            "kind": "install",
            "occurred_at": _utc_now(),
        })
        state["counters"]["install"] = 1
        self._trim_queue_locked(state)

    def _trim_queue_locked(self, state: dict[str, Any] | None = None) -> None:
        target = state if state is not None else self._state
        if len(target["events"]) <= self.max_queue_size:
            return
        # Keep the first install marker when possible, then retain the newest
        # events.  Counters remain cumulative even when old queue entries are
        # discarded under a sustained offline backlog.
        install = next((item for item in target["events"] if item["kind"] == "install"), None)
        newest = target["events"][-self.max_queue_size :]
        if install is not None and install not in newest:
            newest = [install, *newest[: self.max_queue_size - 1]]
        target["events"] = newest[: self.max_queue_size]

    def _write_state_locked(self) -> bool:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.state_path.name}.", suffix=".tmp", dir=str(self.state_path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.state_path)
                return True
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        except OSError:
            # Management is best-effort.  Keep the in-memory state and let
            # future attempts retry persistence without breaking uploads/UI.
            return False

    def _notify_state_changed(self, disabled: bool) -> None:
        callback = self._on_state_changed
        if callback is None:
            return
        try:
            callback(disabled)
        except Exception:
            # Telemetry must never terminate its worker or affect uploads/UI.
            pass

    def _notify_announcement(self, announcement: dict[str, str]) -> None:
        callback = self._on_announcement
        if callback is None:
            return
        try:
            callback(copy.deepcopy(announcement))
        except Exception:
            # A popup failure must not terminate management polling.
            pass

    @staticmethod
    def _validate_event_id(value: str) -> str:
        parsed = uuid.UUID(str(value))
        canonical = str(parsed)
        if canonical != str(value):
            raise ValueError("event id must be a canonical UUID")
        return canonical

    @staticmethod
    def _validate_event(event: Mapping[str, Any]) -> None:
        if set(event) != {"id", "kind", "occurred_at"}:
            raise ValueError("management event shape is invalid")
        ClientManagement._validate_event_id(str(event["id"]))
        if event["kind"] not in EVENT_KINDS:
            raise ValueError("management event kind is invalid")
        occurred_at = event["occurred_at"]
        if not isinstance(occurred_at, str) or not occurred_at.endswith("Z"):
            raise ValueError("management event timestamp must be UTC")
        try:
            parsed = _datetime.datetime.fromisoformat(occurred_at[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("management event timestamp must be ISO-8601 UTC") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != _datetime.timedelta(0):
            raise ValueError("management event timestamp must be UTC")


def _new_state() -> dict[str, Any]:
    return {
        "version": 1,
        "installation_id": str(uuid.uuid4()),
        "token": secrets.token_hex(32),
        "disabled": False,
        "last_seen_announcement_id": None,
        "counters": {"install": 0, "submission_attempt": 0, "submission_success": 0},
        "events": [],
    }


def _normalise_state(payload: Any, max_queue_size: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("management state must be an object")
    installation_id = str(payload.get("installation_id", ""))
    if str(uuid.UUID(installation_id)) != installation_id:
        raise ValueError("invalid installation id")
    token = payload.get("token")
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ValueError("invalid management token")
    disabled = payload.get("disabled", False)
    if type(disabled) is not bool:
        raise ValueError("invalid disabled cache")
    raw_events = payload.get("events", [])
    if not isinstance(raw_events, list):
        raise ValueError("invalid event queue")
    events: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_events:
        if not isinstance(item, dict):
            raise ValueError("invalid queued event")
        event = {key: item.get(key) for key in ("id", "kind", "occurred_at")}
        if set(item) != set(event):
            raise ValueError("invalid queued event shape")
        ClientManagement._validate_event(event)
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        events.append(event)
    counters = payload.get("counters", {})
    if not isinstance(counters, dict):
        raise ValueError("invalid management counters")
    clean_counters = {}
    for kind in EVENT_KINDS:
        value = counters.get(kind, 0)
        if type(value) is not int or value < 0:
            raise ValueError("invalid management counter")
        clean_counters[kind] = value
    state = {
        "version": 1,
        "installation_id": installation_id,
        "token": token,
        "disabled": disabled,
        "last_seen_announcement_id": _normalise_announcement_id(
            payload.get("last_seen_announcement_id")
        ),
        "counters": clean_counters,
        "events": events,
    }
    # Reuse the same bounded-queue policy used for newly appended events.
    if len(events) > max_queue_size:
        install = next((item for item in events if item["kind"] == "install"), None)
        events = events[-max_queue_size:]
        if install is not None and install not in events:
            events = [install, *events[: max_queue_size - 1]]
        state["events"] = events[:max_queue_size]
    return state


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unpack_transport_response(raw_response: Any) -> tuple[int, Any]:
    if isinstance(raw_response, tuple) and len(raw_response) == 2:
        status_code, payload = raw_response
        return int(status_code), payload
    if isinstance(raw_response, Mapping):
        return 200, raw_response
    status_code = int(getattr(raw_response, "status_code"))
    if 300 <= status_code < 400:
        return status_code, None
    payload = raw_response.json()
    return status_code, payload


def _validate_sync_response(
    payload: Any,
    status_code: int,
    snapshot: list[dict[str, str]],
) -> tuple[bool, list[str], dict[str, str] | None]:
    if status_code != 200 or not isinstance(payload, dict):
        raise ValueError("invalid management response status or object")
    if set(payload) not in (
        {"disabled", "acknowledged"},
        {"disabled", "acknowledged", "announcement"},
    ):
        raise ValueError("invalid management response fields")
    disabled = payload["disabled"]
    acknowledged = payload["acknowledged"]
    if type(disabled) is not bool or not isinstance(acknowledged, list):
        raise ValueError("invalid management response types")
    sent_ids = {item["id"] for item in snapshot}
    if any(type(item) is not str for item in acknowledged):
        raise ValueError("invalid acknowledged event id")
    if len(set(acknowledged)) != len(acknowledged) or not set(acknowledged) <= sent_ids:
        raise ValueError("acknowledgement is not a subset of the request")
    return disabled, list(acknowledged), _validate_announcement(payload.get("announcement"))


def _normalise_announcement_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid last-seen announcement id")
    parsed = uuid.UUID(value)
    canonical = str(parsed)
    if canonical != value:
        raise ValueError("invalid last-seen announcement id")
    return canonical


def _validate_announcement(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"id", "title", "body"}:
        raise ValueError("invalid management announcement")
    announcement_id = _normalise_announcement_id(value["id"])
    title = value["title"]
    body = value["body"]
    if announcement_id is None or not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("invalid management announcement fields")
    if not title or len(title) > 80 or len(body) > 2000:
        raise ValueError("management announcement is too long")
    return {"id": announcement_id, "title": title, "body": body}


__all__ = [
    "APP_VERSION",
    "SYNC_URL",
    "STATE_FILE_NAME",
    "MAX_QUEUE_SIZE",
    "SYNC_BATCH_SIZE",
    "ClientManagement",
    "ManagementWorker",
    "SyncResult",
]
