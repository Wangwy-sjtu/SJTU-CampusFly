"""Bounded CampusFly statistics and remote-disable service.

The service intentionally has a very small surface area.  It accepts batches of
anonymous, client-generated events, stores only installation/event identifiers
and coarse timestamps, and exposes a Basic-authenticated dashboard for the
single remote control this application needs: disabling or restoring the
CampusFly client.

This module uses only the Python standard library.  Access logging is disabled
at the request-handler boundary because the default ``http.server`` logger
would include the peer address.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from time import time
from typing import Any, Iterable, Mapping
import uuid
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_PATH = "/campusfly/api/v1/sync"
ADMIN_PATH = "/campusfly/admin"
DEFAULT_ORIGIN = "https://998223.xyz"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
MAX_BODY_BYTES = 64 * 1024
MAX_EVENTS = 100
MAX_VERSION_LENGTH = 64
MAX_ADMIN_ROWS = 500
MAX_ANNOUNCEMENT_TITLE = 80
MAX_ANNOUNCEMENT_BODY = 2000
MAX_ADMIN_BODY_BYTES = 32 * 1024
NONCE_TTL_SECONDS = 15 * 60

EVENT_KINDS = frozenset({"install", "submission_attempt", "submission_success"})
TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class RequestValidationError(ValueError):
    """Raised when an API request does not satisfy the public contract."""


class UnauthorizedError(Exception):
    """Raised when an existing installation presents the wrong token."""


class NotFoundError(Exception):
    """Raised when an admin operation names an unknown installation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalise_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RequestValidationError(f"{field} must be a UUID")
    # Accept the canonical hyphenated form and the compact form so older
    # clients can be upgraded without creating a second installation row.
    if len(value) not in (32, 36):
        raise RequestValidationError(f"{field} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise RequestValidationError(f"{field} must be a UUID") from None
    return str(parsed)


def _normalise_time(value: Any, field: str = "occurred_at") -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise RequestValidationError(f"{field} must be an ISO-8601 time")
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise RequestValidationError(f"{field} must be an ISO-8601 time") from None
    if parsed.tzinfo is None:
        raise RequestValidationError(f"{field} must include a timezone")
    # Store a single UTC representation. It keeps dashboard comparisons
    # predictable while retaining sub-second precision supplied by clients.
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalise_version(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_VERSION_LENGTH:
        raise RequestValidationError("version is invalid")
    if not VERSION_RE.fullmatch(value):
        raise RequestValidationError("version is invalid")
    return value


def _normalise_token(value: Any) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise RequestValidationError("token is invalid")
    return value.lower()


def _normalise_announcement_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise RequestValidationError(f"{field} is invalid")
    # Keep announcements plain text. Tabs and line breaks are useful in a
    # notice; other C0 controls are rejected before they reach the database or
    # a client UI.
    if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
        raise RequestValidationError(f"{field} contains unsupported control characters")
    return value


def _token_from_payload(payload: Mapping[str, Any]) -> str:
    """Read and validate the installation token from the canonical field."""

    if "token" not in payload:
        raise RequestValidationError("token is required")
    return _normalise_token(payload["token"])


def validate_sync_payload(payload: Any) -> dict[str, Any]:
    """Validate and canonicalise the sync JSON object.

    Unknown top-level/event fields are deliberately ignored and are never
    written to SQLite. This leaves room for a client to add non-sensitive
    transport metadata without changing the storage contract.
    """

    if not isinstance(payload, Mapping):
        raise RequestValidationError("request must be a JSON object")
    installation_id = _normalise_uuid(payload.get("installation_id"), "installation_id")
    version = _normalise_version(payload.get("version"))
    token = _token_from_payload(payload)
    events = payload.get("events")
    if not isinstance(events, list):
        raise RequestValidationError("events must be an array")
    if len(events) > MAX_EVENTS:
        raise RequestValidationError(f"at most {MAX_EVENTS} events are allowed")

    canonical_events: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in events:
        if not isinstance(item, Mapping):
            raise RequestValidationError("each event must be an object")
        event_id = _normalise_uuid(item.get("id"), "event id")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise RequestValidationError("event kind is invalid")
        occurred_at = _normalise_time(item.get("occurred_at"))
        if event_id in seen_ids:
            # Duplicate IDs in one batch are harmless and receive one ack.
            continue
        seen_ids.add(event_id)
        canonical_events.append({"id": event_id, "kind": kind, "occurred_at": occurred_at})

    return {
        "installation_id": installation_id,
        "token": token,
        "version": version,
        "events": canonical_events,
    }


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration, with secrets supplied only through the env."""

    db_path: Path
    admin_user: str
    admin_password: str
    origin: str = DEFAULT_ORIGIN
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls) -> "AppConfig":
        db_value = os.environ.get("CAMPUSFLY_DB", "").strip()
        if not db_value:
            raise RuntimeError("CAMPUSFLY_DB is required")
        db_path = Path(db_value).expanduser()
        if not db_path.is_absolute():
            db_path = db_path.resolve()

        origin = os.environ.get("CAMPUSFLY_ORIGIN", "").strip()
        if not origin:
            raise RuntimeError("CAMPUSFLY_ORIGIN is required")
        parsed_origin = urlsplit(origin)
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.netloc
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.username
            or parsed_origin.password
        ):
            raise RuntimeError("CAMPUSFLY_ORIGIN must be an HTTPS origin without a trailing slash")

        host = os.environ.get("CAMPUSFLY_BIND", DEFAULT_HOST).strip() or DEFAULT_HOST
        if host not in LOOPBACK_HOSTS:
            raise RuntimeError("CAMPUSFLY_BIND must be a loopback address")
        try:
            port = int(os.environ.get("CAMPUSFLY_PORT", str(DEFAULT_PORT)))
        except ValueError:
            raise RuntimeError("CAMPUSFLY_PORT must be an integer") from None
        if not (1 <= port <= 65535):
            raise RuntimeError("CAMPUSFLY_PORT is outside the valid range")

        admin_user = os.environ.get("CAMPUSFLY_ADMIN_USER", "")
        admin_password = os.environ.get("CAMPUSFLY_ADMIN_PASSWORD", "")
        if not admin_user or not admin_password:
            raise RuntimeError("CAMPUSFLY_ADMIN_USER and CAMPUSFLY_ADMIN_PASSWORD are required")

        return cls(
            db_path=db_path,
            admin_user=admin_user,
            admin_password=admin_password,
            origin=origin,
            host=host,
            port=port,
        )


class Database:
    """SQLite store with one short transaction per API/admin operation."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        if self.path != ":memory:":
            path_obj = Path(self.path).expanduser()
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(path_obj)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 10000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO settings(key, value)
                    VALUES ('global_disabled', '0');

                CREATE TABLE IF NOT EXISTS installations (
                    installation_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    version TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    disabled_override INTEGER NOT NULL DEFAULT -1
                        CHECK (disabled_override IN (-1, 0, 1))
                );

                CREATE TABLE IF NOT EXISTS events (
                    installation_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('install', 'submission_attempt', 'submission_success')
                    ),
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (installation_id, event_id),
                    FOREIGN KEY (installation_id) REFERENCES installations(installation_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind);
                CREATE INDEX IF NOT EXISTS events_installation_idx
                    ON events(installation_id, kind);

                CREATE TABLE IF NOT EXISTS announcements (
                    announcement_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS announcements_active_idx
                    ON announcements(active, published_at DESC);
                """
            )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def sync(
        self,
        installation_id: str,
        token: str,
        version: str,
        events: Iterable[Mapping[str, str]],
    ) -> dict[str, Any]:
        """Register/authenticate an installation and insert events idempotently."""

        now = _utc_now()
        event_items = list(events)
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT token_hash, disabled_override FROM installations "
                    "WHERE installation_id = ?",
                    (installation_id,),
                ).fetchone()
                token_hash = self._hash_token(token)
                if row is None:
                    self._connection.execute(
                        "INSERT INTO installations "
                        "(installation_id, token_hash, version, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (installation_id, token_hash, version, now, now),
                    )
                    override = -1
                else:
                    if not hmac.compare_digest(str(row["token_hash"]), token_hash):
                        raise UnauthorizedError()
                    override = int(row["disabled_override"])
                    self._connection.execute(
                        "UPDATE installations SET version = ?, last_seen = ? "
                        "WHERE installation_id = ?",
                        (version, now, installation_id),
                    )

                for event in event_items:
                    self._connection.execute(
                        "INSERT OR IGNORE INTO events "
                        "(installation_id, event_id, kind, occurred_at, received_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            installation_id,
                            event["id"],
                            event["kind"],
                            event["occurred_at"],
                            now,
                        ),
                    )
                global_row = self._connection.execute(
                    "SELECT value FROM settings WHERE key = 'global_disabled'"
                ).fetchone()
                global_disabled = bool(global_row and global_row["value"] == "1")
                announcement = self._active_announcement_unlocked()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

        return {
            # A global pause always wins.  The per-installation flag only
            # permits an individual installation to be disabled while the
            # service remains available to the rest of the fleet.
            "disabled": bool(global_disabled or override == 1),
            "acknowledged": [item["id"] for item in event_items],
            "announcement": announcement,
        }

    def _active_announcement_unlocked(self) -> dict[str, str] | None:
        row = self._connection.execute(
            "SELECT announcement_id, title, body FROM announcements "
            "WHERE active = 1 ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["announcement_id"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
        }

    def active_announcement(self) -> dict[str, str] | None:
        with self._lock:
            return self._active_announcement_unlocked()

    def publish_announcement(self, title: str, body: str) -> str:
        announcement_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                self._connection.execute("UPDATE announcements SET active = 0 WHERE active = 1")
                self._connection.execute(
                    "INSERT INTO announcements "
                    "(announcement_id, title, body, published_at, active) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (announcement_id, title, body, now),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return announcement_id

    def clear_announcement(self) -> None:
        with self._lock:
            self._begin()
            try:
                self._connection.execute("UPDATE announcements SET active = 0 WHERE active = 1")
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def set_global_disabled(self, disabled: bool) -> None:
        with self._lock:
            self._begin()
            try:
                self._connection.execute(
                    "UPDATE settings SET value = ? WHERE key = 'global_disabled'",
                    ("1" if disabled else "0",),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def set_installation_override(self, installation_id: str, override: int) -> None:
        if override not in (-1, 0, 1):
            raise ValueError("invalid installation override")
        with self._lock:
            self._begin()
            try:
                cursor = self._connection.execute(
                    "UPDATE installations SET disabled_override = ? WHERE installation_id = ?",
                    (override, installation_id),
                )
                if cursor.rowcount != 1:
                    raise NotFoundError()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def dashboard(self, limit: int = MAX_ADMIN_ROWS) -> dict[str, Any]:
        with self._lock:
            global_row = self._connection.execute(
                "SELECT value FROM settings WHERE key = 'global_disabled'"
            ).fetchone()
            counts = self._connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM installations) AS installations, "
                "(SELECT COUNT(*) FROM events WHERE kind = 'submission_attempt') AS attempts, "
                "(SELECT COUNT(*) FROM events WHERE kind = 'submission_success') AS successes"
            ).fetchone()
            rows = self._connection.execute(
                "SELECT i.installation_id, i.version, i.first_seen, i.last_seen, "
                "i.disabled_override, "
                "COALESCE(SUM(CASE WHEN e.kind = 'submission_attempt' THEN 1 ELSE 0 END), 0) AS attempts, "
                "COALESCE(SUM(CASE WHEN e.kind = 'submission_success' THEN 1 ELSE 0 END), 0) AS successes "
                "FROM installations AS i LEFT JOIN events AS e "
                "ON e.installation_id = i.installation_id "
                "GROUP BY i.installation_id "
                "ORDER BY i.last_seen DESC LIMIT ?",
                (max(1, min(int(limit), MAX_ADMIN_ROWS)),),
            ).fetchall()
        return {
            "global_disabled": bool(global_row and global_row["value"] == "1"),
            "installations": int(counts["installations"]),
            "attempts": int(counts["attempts"]),
            "successes": int(counts["successes"]),
            "announcement": self._dashboard_announcement(),
            "rows": [dict(row) for row in rows],
        }

    def _dashboard_announcement(self) -> dict[str, str] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT announcement_id, title, body, published_at FROM announcements "
                "WHERE active = 1 ORDER BY published_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["announcement_id"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
            "published_at": str(row["published_at"]),
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class CampusFlyApplication:
    """Application services shared by the HTTP handler and tests."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | Database,
        *,
        admin_user: str = "",
        admin_password: str = "",
        origin: str = DEFAULT_ORIGIN,
    ):
        self.db = db_path if isinstance(db_path, Database) else Database(db_path)
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.origin = origin
        self._nonce_lock = threading.Lock()
        self._nonces: dict[str, float] = {}

    @classmethod
    def from_env(cls) -> "CampusFlyApplication":
        config = AppConfig.from_env()
        return cls(
            config.db_path,
            admin_user=config.admin_user,
            admin_password=config.admin_password,
            origin=config.origin,
        )

    def sync(self, payload: Any) -> dict[str, Any]:
        request = validate_sync_payload(payload)
        return self.db.sync(
            request["installation_id"],
            request["token"],
            request["version"],
            request["events"],
        )

    def authenticate_basic(self, header: str | None) -> bool:
        supplied_user = ""
        supplied_password = ""
        if isinstance(header, str) and header.startswith("Basic "):
            encoded = header[6:].strip()
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeError, binascii.Error):
                decoded = ""
            supplied_user, separator, supplied_password = decoded.partition(":")
            if not separator:
                supplied_user = decoded
                supplied_password = ""
        # Compare bytes so a malformed/missing credential takes the same
        # compare path as a regular credential. Empty configured credentials
        # never enable admin access.
        user_ok = hmac.compare_digest(
            supplied_user.encode("utf-8"), self.admin_user.encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            supplied_password.encode("utf-8"), self.admin_password.encode("utf-8")
        )
        return bool(self.admin_user and self.admin_password and user_ok and password_ok)

    def issue_nonce(self) -> str:
        nonce = secrets.token_urlsafe(24)
        now = time()
        with self._nonce_lock:
            self._nonces = {
                key: expires for key, expires in self._nonces.items() if expires > now
            }
            if len(self._nonces) >= 256:
                oldest = min(self._nonces, key=self._nonces.get)
                del self._nonces[oldest]
            self._nonces[nonce] = now + NONCE_TTL_SECONDS
        return nonce

    def consume_nonce(self, nonce: str) -> bool:
        if not nonce or len(nonce) > 128:
            return False
        now = time()
        with self._nonce_lock:
            expires = self._nonces.pop(nonce, None)
        return bool(expires and expires > now)

    def admin_post(self, form: Mapping[str, list[str]]) -> None:
        action = form.get("action", [""])[0]
        scope = form.get("scope", [""])[0]
        installation_id = form.get("installation_id", [""])[0]
        if action == "publish_announcement":
            if scope or installation_id:
                raise RequestValidationError("announcement action cannot name an installation")
            title = _normalise_announcement_text(
                form.get("title", [""])[0], "title", MAX_ANNOUNCEMENT_TITLE
            )
            body = _normalise_announcement_text(
                form.get("body", [""])[0], "body", MAX_ANNOUNCEMENT_BODY
            )
            self.db.publish_announcement(title, body)
            return
        if action == "clear_announcement":
            if scope or installation_id:
                raise RequestValidationError("announcement action cannot name an installation")
            self.db.clear_announcement()
            return
        if action not in {"disable", "restore", "inherit"}:
            raise RequestValidationError("unknown admin action")
        if scope == "global":
            if installation_id:
                raise RequestValidationError("global action cannot name an installation")
            if action == "inherit":
                raise RequestValidationError("global action cannot inherit")
            self.db.set_global_disabled(action == "disable")
            return
        if scope not in {"installation", "install"}:
            raise RequestValidationError("unknown admin scope")
        installation_id = _normalise_uuid(installation_id, "installation_id")
        # Restoring an installation clears its local disable flag. Global
        # disable remains authoritative until an administrator restores it.
        override = {"disable": 1, "restore": -1, "inherit": -1}[action]
        self.db.set_installation_override(installation_id, override)

    def dashboard(self) -> dict[str, Any]:
        return self.db.dashboard()

    def close(self) -> None:
        self.db.close()


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RequestValidationError("request body must be valid JSON") from None


def _html_text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_dashboard(snapshot: Mapping[str, Any], nonce: str) -> bytes:
    """Render the complete dashboard without external resources or secrets."""

    global_disabled = bool(snapshot["global_disabled"])
    global_state = "已禁用" if global_disabled else "运行中"
    active_announcement = snapshot.get("announcement")
    announcement_title = _html_text(active_announcement["title"]) if active_announcement else ""
    announcement_body = _html_text(active_announcement["body"]) if active_announcement else ""
    announcement_meta = (
        f"当前公告 ID {_html_text(active_announcement['id'])}，发布时间 {_html_text(active_announcement['published_at'])} UTC。"
        if active_announcement
        else "当前没有活动公告。"
    )
    clear_announcement_form = (
        "<form method=post action=/campusfly/admin class=announcement-clear>"
        f"<input type=hidden name=csrf_token value=\"{_html_text(nonce)}\">"
        "<button name=action value=clear_announcement type=submit>撤回公告</button></form>"
        if active_announcement
        else ""
    )
    announcement_html = (
        "<section class=\"panel announcement\" aria-label=\"启动公告\">"
        "<h2>启动公告</h2>"
        "<p class=subtle>客户端显示纯文本；每次发布都会生成新的公告 ID。</p>"
        f"<p class=subtle>{announcement_meta}</p>"
        "<form method=post action=/campusfly/admin>"
        f"<input type=hidden name=csrf_token value=\"{_html_text(nonce)}\">"
        "<label for=announcement-title>标题（最多 80 字）</label>"
        f"<input id=announcement-title name=title maxlength={MAX_ANNOUNCEMENT_TITLE} value=\"{announcement_title}\" required>"
        "<label for=announcement-body>正文（最多 2000 字）</label>"
        f"<textarea id=announcement-body name=body maxlength={MAX_ANNOUNCEMENT_BODY} rows=7 required>{announcement_body}</textarea>"
        "<div class=announcement-actions><button name=action value=publish_announcement type=submit>发布/更新</button></div></form>"
        f"{clear_announcement_form}</section>"
    )
    rows_html: list[str] = []
    for row in snapshot["rows"]:
        override = int(row["disabled_override"])
        if override == 1:
            state = "单独禁用"
        elif global_disabled:
            state = "全局禁用"
        else:
            state = "运行中"
        installation_id = _html_text(row["installation_id"])
        rows_html.append(
            "<tr>"
            f"<td><code>{installation_id}</code></td>"
            f"<td>{_html_text(row['version'])}</td>"
            f"<td>{_html_text(row['first_seen'])}</td>"
            f"<td>{_html_text(row['last_seen'])}</td>"
            f"<td>{int(row['attempts'])}</td>"
            f"<td>{int(row['successes'])}</td>"
            f"<td>{_html_text(state)}</td>"
            "<td class=actions>"
            "<form method=post action=/campusfly/admin>"
            f"<input type=hidden name=csrf_token value=\"{_html_text(nonce)}\">"
            "<input type=hidden name=scope value=installation>"
            f"<input type=hidden name=installation_id value=\"{installation_id}\">"
            "<button name=action value=disable type=submit>禁用</button>"
            "<button name=action value=restore type=submit>恢复</button>"
            "</form></td></tr>"
        )
    if not rows_html:
        rows_html.append('<tr><td colspan="8" class=empty>还没有匿名安装记录。</td></tr>')
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CampusFly 管理</title>
<style>
:root {{ color-scheme: light; font-family: system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif; background:#f3f6fb; color:#172033; }}
body {{ margin:0; }}
main {{ max-width:1180px; margin:0 auto; padding:32px 20px 48px; }}
h1 {{ margin:0 0 6px; letter-spacing:-.03em; }}
.subtle {{ color:#596579; margin:0 0 24px; }}
.cards {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:20px 0; }}
.card,.panel {{ background:#fff; border:1px solid #dfe5ef; border-radius:14px; box-shadow:0 8px 24px #27364d0d; }}
.card {{ padding:16px 18px; }}
.card strong {{ display:block; font-size:1.7rem; margin-top:4px; }}
.label {{ color:#66748a; font-size:.85rem; }}
.panel {{ padding:18px; overflow:auto; }}
.global {{ display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }}
.state {{ font-weight:700; color:#156b4b; }}
.state.off {{ color:#a54130; }}
h2 {{ margin:0 0 8px; font-size:1.05rem; }}
label {{ display:block; margin:10px 0 5px; color:#596579; font-size:.85rem; }}
input,textarea {{ box-sizing:border-box; width:100%; border:1px solid #b8c4d6; border-radius:8px; padding:9px 10px; font:inherit; }}
textarea {{ resize:vertical; min-height:120px; }}
.announcement {{ margin:18px 0; }}
.announcement-actions {{ display:flex; align-items:center; gap:10px; margin-top:12px; }}
.announcement-clear {{ display:inline; }}
button {{ border:1px solid #b8c4d6; border-radius:8px; background:#fff; color:#172033; padding:7px 11px; cursor:pointer; }}
button:hover {{ background:#edf3fc; }}
button[value=disable] {{ color:#a54130; }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; font-size:.9rem; }}
th,td {{ padding:10px 8px; text-align:left; border-bottom:1px solid #e7ebf2; white-space:nowrap; }}
th {{ color:#596579; font-weight:600; font-size:.8rem; }}
code {{ font-size:.78rem; }}
.actions form {{ display:flex; gap:6px; }}
.empty {{ text-align:center; color:#66748a; padding:28px; }}
@media (max-width:700px) {{ main {{ padding:22px 12px 32px; }} .cards {{ grid-template-columns:1fr; }} .panel {{ padding:12px; }} }}
</style>
</head>
<body><main>
<h1>CampusFly 管理</h1>
<p class=subtle>匿名同步统计与客户端远程开关。这里不显示账号、Cookie、地图 Key 或网络地址。</p>
<section class=cards aria-label="统计">
<div class=card><span class=label>安装数量</span><strong>{int(snapshot['installations'])}</strong></div>
<div class=card><span class=label>提交尝试</span><strong>{int(snapshot['attempts'])}</strong></div>
<div class=card><span class=label>接口成功码事件</span><strong>{int(snapshot['successes'])}</strong></div>
</section>
{announcement_html}
<section class="panel global" aria-label="全局控制">
<div><strong>全局状态：</strong><span class="state{' off' if global_disabled else ''}">{_html_text(global_state)}</span><br><span class=subtle>全局禁用会暂停所有安装；单独恢复只清除该安装的本地禁用标记。</span></div>
<form method=post action=/campusfly/admin>
<input type=hidden name=csrf_token value="{_html_text(nonce)}">
<input type=hidden name=scope value=global>
<button name=action value={'restore' if global_disabled else 'disable'} type=submit>{'恢复全局' if global_disabled else '禁用全局'}</button>
</form>
</section>
<section class=panel aria-label="安装列表">
<table><caption class=subtle>时间均为 UTC</caption><thead><tr><th>安装 ID</th><th>版本</th><th>首次同步</th><th>最近同步</th><th>提交尝试</th><th>接口成功码事件</th><th>状态</th><th>操作</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<p class=subtle>最多显示最近 {MAX_ADMIN_ROWS} 个安装；统计按客户端生成的 UUID 去重。</p>
</section>
</main></body></html>"""
    return page.encode("utf-8")


class CampusFlyHandler(BaseHTTPRequestHandler):
    """HTTP boundary with deliberately quiet logging and bounded reads."""

    protocol_version = "HTTP/1.1"
    server_version = "CampusFlyService"
    sys_version = ""

    @property
    def application(self) -> CampusFlyApplication:
        return self.server.application  # type: ignore[attr-defined]

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(20)
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        # Never emit default access logs: BaseHTTPRequestHandler includes the
        # remote address, which this service intentionally does not collect.
        return

    def log_error(self, format: str, *args: Any) -> None:
        # Keep malformed requests and handler failures free of peer addresses.
        return

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _send_json(self, status: int, payload: Mapping[str, Any], *, extra: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self._send_bytes(status, "application/json; charset=utf-8", body, headers)

    def _send_error_json(self, status: int, code: str, detail: str | None = None) -> None:
        payload: dict[str, Any] = {"error": code}
        if detail:
            payload["detail"] = detail
        self._send_json(status, payload)

    def _read_body(self, limit: int) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
        if transfer_encoding and transfer_encoding != "identity":
            raise RequestValidationError("chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestValidationError("content length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestValidationError("content length is invalid") from None
        if length < 0:
            raise RequestValidationError("content length is invalid")
        if length > limit:
            raise RequestValidationError("request body is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestValidationError("request body is incomplete")
        return body

    def _check_admin_with_challenge(self) -> bool:
        if self.application.authenticate_basic(self.headers.get("Authorization")):
            return True
        self._send_bytes(
            401,
            "application/json; charset=utf-8",
            json.dumps(
                {"error": "admin_auth_required", "detail": "请输入管理员凭据。"},
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Cache-Control": "no-store",
                "WWW-Authenticate": 'Basic realm="CampusFly admin", charset="UTF-8"',
            },
        )
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path != ADMIN_PATH:
            self._send_error_json(404, "not_found")
            return
        if not self._check_admin_with_challenge():
            return
        nonce = self.application.issue_nonce()
        try:
            body = render_dashboard(self.application.dashboard(), nonce)
        except (sqlite3.Error, OSError):
            self._send_error_json(503, "temporarily_unavailable")
            return
        self._send_bytes(
            200,
            "text/html; charset=utf-8",
            body,
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
                "Referrer-Policy": "same-origin",
            },
        )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == API_PATH:
            self._post_sync()
        elif path == ADMIN_PATH:
            self._post_admin()
        else:
            self._send_error_json(404, "not_found")

    def _post_sync(self) -> None:
        try:
            body = self._read_body(MAX_BODY_BYTES)
            payload = _parse_json(body)
            response = self.application.sync(payload)
        except RequestValidationError as exc:
            detail = str(exc)
            status = 413 if "too large" in detail else 400
            self._send_error_json(status, "invalid_request", detail)
            return
        except UnauthorizedError:
            self._send_error_json(401, "unauthorized")
            return
        except (sqlite3.Error, OSError):
            self._send_error_json(503, "temporarily_unavailable")
            return
        self._send_json(200, response)

    def _post_admin(self) -> None:
        if not self._check_admin_with_challenge():
            return
        if self.headers.get("Origin") != self.application.origin:
            self._send_error_json(403, "origin_required")
            return
        try:
            body = self._read_body(16 * 1024)
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/x-www-form-urlencoded", ""}:
                raise RequestValidationError("form encoding is required")
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
            nonce = form.get("csrf_token", form.get("nonce", [""]))[0]
            # Same-origin and a one-use nonce are both required. Basic auth is
            # intentionally not treated as CSRF protection.
            if not nonce or not self.application.consume_nonce(nonce):
                self._send_error_json(403, "csrf_failed")
                return
            self.application.admin_post(form)
        except UnicodeDecodeError:
            self._send_error_json(400, "invalid_request", "form encoding is invalid")
            return
        except RequestValidationError as exc:
            self._send_error_json(400, "invalid_request", str(exc))
            return
        except NotFoundError:
            self._send_error_json(404, "installation_not_found")
            return
        except (sqlite3.Error, OSError):
            self._send_error_json(503, "temporarily_unavailable")
            return
        self._send_bytes(
            303,
            "text/plain; charset=utf-8",
            b"",
            {"Location": ADMIN_PATH, "Cache-Control": "no-store"},
        )

    def do_HEAD(self) -> None:
        self._send_error_json(405, "method_not_allowed")


class CampusFlyHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], application: CampusFlyApplication):
        self.application = application
        self._request_slots = threading.BoundedSemaphore(16)
        super().__init__(address, CampusFlyHandler)

    def process_request(self, request, client_address):
        # A bounded service should not create an unbounded number of threads
        # when a reverse proxy or a broken client opens many connections.
        if not self._request_slots.acquire(blocking=False):
            try:
                request.close()
            finally:
                return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def handle_error(self, request, client_address):
        # socketserver's default traceback may expose request details. The
        # endpoint deliberately has no server-side access/error logging.
        return


def create_http_server(
    application: CampusFlyApplication,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> CampusFlyHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("server must bind to a loopback address")
    if not (0 <= int(port) <= 65535):
        raise ValueError("port is outside the valid range")
    return CampusFlyHTTPServer((host, int(port)), application)


def make_server(
    db_path: str | os.PathLike[str],
    *,
    admin_user: str = "",
    admin_password: str = "",
    origin: str = DEFAULT_ORIGIN,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> CampusFlyHTTPServer:
    """Convenience factory used by deployments and tests."""

    app = CampusFlyApplication(
        db_path,
        admin_user=admin_user,
        admin_password=admin_password,
        origin=origin,
    )
    return create_http_server(app, host=host, port=port)


def main() -> None:
    config = AppConfig.from_env()
    application = CampusFlyApplication(
        config.db_path,
        admin_user=config.admin_user,
        admin_password=config.admin_password,
        origin=config.origin,
    )
    server = create_http_server(application, host=config.host, port=config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application.close()


if __name__ == "__main__":
    main()
