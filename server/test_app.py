import base64
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import re
import secrets
import tempfile
import threading
import unittest
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

try:
    from server.app import (
        ADMIN_PATH,
        API_PATH,
        MAX_EVENTS,
        CampusFlyApplication,
        RequestValidationError,
        create_http_server,
        validate_sync_payload,
    )
except ModuleNotFoundError:  # discovery with ``-s server`` from the repo root
    from app import (  # type: ignore[no-redef]
        ADMIN_PATH,
        API_PATH,
        MAX_EVENTS,
        CampusFlyApplication,
        RequestValidationError,
        create_http_server,
        validate_sync_payload,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl=None):
        return None


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.app = CampusFlyApplication(
            f"{self.tempdir.name}/campusfly.sqlite3",
            admin_user="admin",
            admin_password="test-password",
            origin="https://998223.xyz",
        )
        self.addCleanup(self.app.close)
        self.httpd = create_http_server(self.app, port=0)
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = build_opener(_NoRedirect)

    def tearDown(self):
        self.httpd.shutdown()

    @staticmethod
    def _payload(installation_id=None, token=None, version="1.0.3", events=None):
        return {
            "installation_id": installation_id or str(uuid.uuid4()),
            "token": token or secrets.token_hex(32),
            "version": version,
            "events": events or [],
        }

    @staticmethod
    def _event(kind="submission_attempt", event_id=None, occurred_at=None, **extra):
        value = {
            "id": event_id or str(uuid.uuid4()),
            "kind": kind,
            "occurred_at": occurred_at or "2026-09-19T12:00:00Z",
        }
        value.update(extra)
        return value

    def _request(self, method, path, payload=None, headers=None):
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
            else:
                data = payload.encode("utf-8")
                request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def _admin_headers(self):
        value = base64.b64encode(b"admin:test-password").decode("ascii")
        return {"Authorization": f"Basic {value}"}

    def _admin_page(self):
        status, headers, body = self._request(
            "GET", ADMIN_PATH, headers=self._admin_headers()
        )
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    @staticmethod
    def _nonce(page):
        match = re.search(r'name=csrf_token value="([^"]+)"', page)
        if not match:
            raise AssertionError("dashboard did not include a CSRF nonce")
        return match.group(1)

    def test_sync_registration_is_idempotent_and_durable(self):
        installation_id = str(uuid.uuid4())
        token = secrets.token_hex(32)
        event_id = str(uuid.uuid4())
        payload = self._payload(
            installation_id,
            token,
            events=[self._event(event_id=event_id), self._event("submission_success")],
        )
        status, _, body = self._request("POST", API_PATH, payload)
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response["acknowledged"], [event_id, payload["events"][1]["id"]])
        self.assertFalse(response["disabled"])
        self.assertIsNone(response["announcement"])

        status, _, body = self._request("POST", API_PATH, payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["acknowledged"], response["acknowledged"])
        snapshot = self.app.dashboard()
        self.assertEqual(snapshot["installations"], 1)
        self.assertEqual(snapshot["attempts"], 1)
        self.assertEqual(snapshot["successes"], 1)

        # Closing/reopening the SQLite store keeps both the registration and
        # event de-duplication durable.
        self.app.db.close()
        try:
            from server.app import Database
        except ModuleNotFoundError:
            from app import Database

        self.app.db = Database(f"{self.tempdir.name}/campusfly.sqlite3")
        status, _, _ = self._request("POST", API_PATH, payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.app.dashboard()["installations"], 1)
        self.assertEqual(self.app.dashboard()["attempts"], 1)

    def test_wrong_token_cannot_update_version_or_events(self):
        installation_id = str(uuid.uuid4())
        token = secrets.token_hex(32)
        payload = self._payload(
            installation_id,
            token,
            version="1.0.1",
            events=[self._event()],
        )
        self.assertEqual(self._request("POST", API_PATH, payload)[0], 200)
        forged = self._payload(
            installation_id,
            secrets.token_hex(32),
            version="9.9.9",
            events=[self._event("submission_success")],
        )
        status, _, body = self._request("POST", API_PATH, forged)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")
        row = self.app.dashboard()["rows"][0]
        self.assertEqual(row["version"], "1.0.1")
        self.assertEqual(self.app.dashboard()["successes"], 0)

    def test_global_disable_and_per_installation_override(self):
        first = self._payload(events=[])
        second = self._payload(events=[])
        self.assertEqual(self._request("POST", API_PATH, first)[0], 200)
        self.assertEqual(self._request("POST", API_PATH, second)[0], 200)
        self.app.db.set_global_disabled(True)
        self.assertTrue(self.app.sync(first)["disabled"])
        self.assertTrue(self.app.sync(second)["disabled"])

        # A per-installation restore clears its local flag but cannot bypass a
        # global pause.
        self.app.db.set_installation_override(second["installation_id"], -1)
        self.assertTrue(self.app.sync(first)["disabled"])
        self.assertTrue(self.app.sync(second)["disabled"])
        self.app.db.set_global_disabled(False)
        self.app.db.set_installation_override(first["installation_id"], 1)
        self.assertTrue(self.app.sync(first)["disabled"])
        self.assertFalse(self.app.sync(second)["disabled"])

    def test_validation_limits_and_success_code(self):
        base = self._payload()
        with self.assertRaises(RequestValidationError):
            validate_sync_payload({**base, "events": [self._event()] * (MAX_EVENTS + 1)})
        with self.assertRaises(RequestValidationError):
            validate_sync_payload({**base, "version": ""})
        with self.assertRaises(RequestValidationError):
            validate_sync_payload({**base, "events": [self._event(occurred_at="2026-09-19T12:00:00")]})
        with self.assertRaises(RequestValidationError):
            validate_sync_payload({**base, "token": "too-short"})

        status, _, _ = self._request("POST", API_PATH, {**base, "events": [self._event()]})
        self.assertEqual(status, 200)

    def test_admin_requires_basic_auth_exact_origin_and_toggles_state(self):
        status, headers, _ = self._request("GET", ADMIN_PATH)
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)

        page = self._admin_page()
        self.assertIn("CampusFly 管理", page)
        self.assertNotIn("test-password", page)

        form = f"csrf_token={self._nonce(page)}&scope=global&action=disable"
        headers = self._admin_headers()
        headers["Origin"] = "https://evil.example"
        status, _, _ = self._request("POST", ADMIN_PATH, form, headers)
        self.assertEqual(status, 403)
        self.assertFalse(self.app.dashboard()["global_disabled"])

        headers["Origin"] = "https://998223.xyz"
        status, _, _ = self._request("POST", ADMIN_PATH, form, headers)
        self.assertEqual(status, 303)
        self.assertTrue(self.app.dashboard()["global_disabled"])

        installation = self._payload(events=[])
        self.assertEqual(self._request("POST", API_PATH, installation)[0], 200)
        self.assertTrue(self.app.sync(installation)["disabled"])
        page = self._admin_page()
        form = (
            f"csrf_token={self._nonce(page)}&scope=installation&action=restore&"
            f"installation_id={installation['installation_id']}"
        )
        headers["Origin"] = "https://998223.xyz"
        status, _, _ = self._request("POST", ADMIN_PATH, form, headers)
        self.assertEqual(status, 303)
        self.assertTrue(self.app.sync(installation)["disabled"])
        page = self._admin_page()
        form = f"csrf_token={self._nonce(page)}&scope=global&action=restore"
        status, _, _ = self._request("POST", ADMIN_PATH, form, headers)
        self.assertEqual(status, 303)
        self.assertFalse(self.app.sync(installation)["disabled"])

    def test_admin_bad_nonce_and_unknown_installation_are_rejected(self):
        headers = self._admin_headers()
        headers["Origin"] = "https://998223.xyz"
        form = "csrf_token=not-a-real-nonce&scope=global&action=disable"
        self.assertEqual(self._request("POST", ADMIN_PATH, form, headers)[0], 403)
        form = (
            "scope=installation&action=disable&installation_id="
            f"{uuid.uuid4()}"
        )
        # Missing nonce is rejected before the installation lookup.
        self.assertEqual(self._request("POST", ADMIN_PATH, form, headers)[0], 403)

    def test_installation_note_is_admin_only_and_persists(self):
        installation = self._payload(events=[])
        self.assertEqual(self._request("POST", API_PATH, installation)[0], 200)
        headers = self._admin_headers()
        headers["Origin"] = "https://998223.xyz"
        note = "熟人测试机 <东南侧>"
        page = self._admin_page()
        form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "save_note",
                "installation_id": installation["installation_id"],
                "note": note,
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, form, headers)[0], 303)
        row = self.app.dashboard()["rows"][0]
        self.assertEqual(row["note"], note)
        self.assertNotIn("note", self.app.sync(installation))
        page = self._admin_page()
        self.assertIn("熟人测试机 &lt;东南侧&gt;", page)

        page = self._admin_page()
        invalid_form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "save_note",
                "installation_id": installation["installation_id"],
                "note": "不允许\n换行",
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, invalid_form, headers)[0], 400)
        self.assertEqual(self.app.dashboard()["rows"][0]["note"], note)

    def test_targeted_announcements_are_scoped_and_can_be_withdrawn(self):
        first = self._payload(events=[])
        second = self._payload(events=[])
        self.assertEqual(self._request("POST", API_PATH, first)[0], 200)
        self.assertEqual(self._request("POST", API_PATH, second)[0], 200)
        headers = self._admin_headers()
        headers["Origin"] = "https://998223.xyz"

        # Keep a global notice active, then publish a newer notice for only
        # the first installation. Each client receives the newest matching
        # notice without learning the target list.
        page = self._admin_page()
        global_form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "publish_announcement",
                "title": "所有人",
                "body": "全局维护提示",
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, global_form, headers)[0], 303)
        page = self._admin_page()
        targeted_form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "publish_announcement",
                "title": "只给第一台",
                "body": "请重新启动",
                "targets": first["installation_id"],
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, targeted_form, headers)[0], 303)
        first_announcement = self.app.sync(first)["announcement"]
        second_announcement = self.app.sync(second)["announcement"]
        self.assertEqual(first_announcement["title"], "只给第一台")
        self.assertEqual(second_announcement["title"], "所有人")

        snapshot = self.app.dashboard()
        targeted = next(item for item in snapshot["announcements"] if item["title"] == "只给第一台")
        self.assertEqual(targeted["target_ids"], [first["installation_id"]])

        page = self._admin_page()
        clear_targeted_form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "clear_announcement",
                "announcement_id": targeted["id"],
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, clear_targeted_form, headers)[0], 303)
        self.assertEqual(self.app.sync(first)["announcement"]["title"], "所有人")

        page = self._admin_page()
        unknown_form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "publish_announcement",
                "title": "错误目标",
                "body": "不应发布",
                "targets": str(uuid.uuid4()),
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, unknown_form, headers)[0], 400)

        page = self._admin_page()
        clear_all_form = urlencode(
            {"csrf_token": self._nonce(page), "action": "clear_announcement"}
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, clear_all_form, headers)[0], 303)
        self.assertIsNone(self.app.sync(first)["announcement"])

    def test_announcement_publish_edit_clear_persists_and_escapes_plain_text(self):
        headers = self._admin_headers()
        headers["Origin"] = "https://998223.xyz"
        title = "<script>alert(1)</script>"
        body = "第一行\n</textarea><script>location.href='https://evil.example'</script>"
        page = self._admin_page()
        form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "publish_announcement",
                "title": title,
                "body": body,
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, form, headers)[0], 303)

        installation = self._payload(events=[])
        status, _, response_body = self._request("POST", API_PATH, installation)
        self.assertEqual(status, 200)
        first = json.loads(response_body)["announcement"]
        self.assertEqual(first["title"], title)
        self.assertEqual(first["body"], body)
        first_id = first["id"]
        uuid.UUID(first_id)

        page = self._admin_page()
        escaped_page = page
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", escaped_page)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", escaped_page)
        self.assertNotIn("</textarea><script>", escaped_page)

        # Publishing an edit creates a new announcement UUID and survives a
        # database reopen. The API still returns raw JSON text for the client
        # to place into a text-only UI.
        page = self._admin_page()
        edited_form = urlencode(
            {
                "csrf_token": self._nonce(page),
                "action": "publish_announcement",
                "title": "已更新",
                "body": "维护完成",
            }
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, edited_form, headers)[0], 303)
        second = self.app.sync(installation)["announcement"]
        self.assertNotEqual(second["id"], first_id)
        self.assertEqual(second["title"], "已更新")
        self.app.db.close()
        try:
            from server.app import Database
        except ModuleNotFoundError:
            from app import Database
        self.app.db = Database(f"{self.tempdir.name}/campusfly.sqlite3")
        self.assertEqual(self.app.sync(installation)["announcement"]["id"], second["id"])

        page = self._admin_page()
        clear_form = urlencode(
            {"csrf_token": self._nonce(page), "action": "clear_announcement"}
        )
        self.assertEqual(self._request("POST", ADMIN_PATH, clear_form, headers)[0], 303)
        self.assertIsNone(self.app.sync(installation)["announcement"])

    def test_announcement_limits_are_checked_before_persisting(self):
        headers = self._admin_headers()
        headers["Origin"] = "https://998223.xyz"
        for title, body in (("x" * 81, "body"), ("title", "x" * 2001), ("", "body")):
            page = self._admin_page()
            form = urlencode(
                {
                    "csrf_token": self._nonce(page),
                    "action": "publish_announcement",
                    "title": title,
                    "body": body,
                }
            )
            self.assertEqual(self._request("POST", ADMIN_PATH, form, headers)[0], 400)
        self.assertIsNone(self.app.db.active_announcement())

    def test_no_request_address_is_written_to_handler_logs(self):
        # The handler overrides both default logging hooks; there is no access
        # log file or stdout/stderr side channel for peer addresses.
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.httpd.RequestHandlerClass.log_message(None, "%s", "127.0.0.1")
            self.httpd.RequestHandlerClass.log_error(None, "%s", "127.0.0.1")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
