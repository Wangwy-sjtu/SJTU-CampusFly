import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src.client_management import ClientManagement


class ClientManagementTests(unittest.TestCase):
    def make_manager(self, folder, transport):
        return ClientManagement(
            state_path=Path(folder) / "configs" / "management.local.json",
            transport=transport,
            sync_interval_seconds=60,
        )

    def test_identity_persists_and_install_is_counted_once(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.make_manager(folder, lambda payload: {"disabled": False, "acknowledged": []})
            installation_id, token = first.installation_id, first.token
            second = self.make_manager(folder, lambda payload: {"disabled": False, "acknowledged": []})
            self.assertEqual(second.installation_id, installation_id)
            self.assertEqual(second.token, token)
            self.assertEqual(second.stats()["install_count"], 1)
            self.assertEqual(len(second.stats()["installation_id"]), 36)

    def test_duplicate_event_id_is_idempotent_and_queue_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = self.make_manager(folder, lambda payload: {"disabled": False, "acknowledged": []})
            event_id = str(uuid.uuid4())
            manager.record_event("submission_attempt", event_id=event_id)
            manager.record_event("submission_attempt", event_id=event_id)
            for _ in range(1100):
                manager.record_event("submission_success")
            state = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(state["events"]), 1000)
            self.assertEqual(manager.stats()["submission_attempt_count"], 1)
            self.assertEqual(len({event["id"] for event in state["events"]}), len(state["events"]))

    def test_offline_failure_keeps_last_valid_disabled_cache_and_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = self.make_manager(folder, lambda payload: {"disabled": True, "acknowledged": payload["events"] and [payload["events"][0]["id"]]})
            self.assertTrue(manager.sync_once().success)
            self.assertTrue(manager.disabled)
            manager.record_event("submission_attempt")
            manager._transport = lambda payload: (_ for _ in ()).throw(TimeoutError())
            result = manager.sync_once()
            self.assertFalse(result.success)
            self.assertTrue(manager.disabled)
            self.assertGreaterEqual(manager.stats()["queued_events"], 1)

    def test_malformed_response_does_not_ack_or_change_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            responses = iter((
                {"disabled": True, "acknowledged": []},
                {"disabled": False, "acknowledged": ["not-sent"]},
            ))
            manager = self.make_manager(folder, lambda payload: next(responses))
            self.assertTrue(manager.sync_once().success)
            self.assertTrue(manager.disabled)
            event_id = manager.record_event("submission_success")
            self.assertFalse(manager.sync_once().success)
            self.assertTrue(manager.disabled)
            self.assertIn(event_id, {event["id"] for event in json.loads(manager.state_path.read_text(encoding="utf-8"))["events"]})

    def test_valid_response_acknowledges_snapshot_and_restores_service(self):
        with tempfile.TemporaryDirectory() as folder:
            captured = []

            def transport(payload):
                captured.append(payload)
                return {"disabled": True, "acknowledged": [event["id"] for event in payload["events"]]}

            manager = self.make_manager(folder, transport)
            first = manager.sync_once()
            self.assertTrue(first.success)
            self.assertTrue(manager.disabled)
            self.assertEqual(manager.stats()["queued_events"], 0)
            manager._transport = lambda payload: {"disabled": False, "acknowledged": []}
            restored = manager.sync_once()
            self.assertTrue(restored.success)
            self.assertFalse(manager.disabled)
            self.assertEqual(set(captured[0]), {"installation_id", "token", "version", "events"})
            self.assertEqual(set(captured[0]["events"][0]), {"id", "kind", "occurred_at"})
            self.assertNotIn("route", captured[0])
            self.assertNotIn("COOKIE", captured[0])

    def test_concurrent_event_added_during_request_survives_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            request_started = threading.Event()
            release_request = threading.Event()
            sent = []

            def transport(payload):
                sent.append(payload)
                request_started.set()
                release_request.wait(2)
                return {"disabled": False, "acknowledged": [event["id"] for event in payload["events"]]}

            manager = self.make_manager(folder, transport)
            original_id = manager.record_event("submission_attempt")
            sync_thread = threading.Thread(target=manager.sync_once)
            sync_thread.start()
            self.assertTrue(request_started.wait(2))
            concurrent_id = manager.record_event("submission_success")
            release_request.set()
            sync_thread.join(2)
            queued_ids = {event["id"] for event in json.loads(manager.state_path.read_text(encoding="utf-8"))["events"]}
            self.assertNotIn(original_id, queued_ids)
            self.assertIn(concurrent_id, queued_ids)

    def test_transport_is_never_called_by_construction(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            manager = self.make_manager(folder, lambda payload: calls.append(payload))
            self.assertEqual(calls, [])
            manager.stop()

    def test_production_request_uses_https_without_following_redirects(self):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"disabled": False, "acknowledged": []}

        with tempfile.TemporaryDirectory() as folder, patch("src.client_management.requests.post", return_value=Response()) as post:
            manager = ClientManagement(state_path=Path(folder) / "management.local.json")
            result = manager.sync_once()
        self.assertTrue(result.success)
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], "https://998223.xyz/campusfly/api/v1/sync")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])
        self.assertLessEqual(kwargs["timeout"], 5)

    def test_redirect_or_malformed_status_is_not_a_valid_management_response(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ClientManagement(
                state_path=Path(folder) / "management.local.json",
                transport=lambda payload: (302, {"disabled": True, "acknowledged": []}),
            )
            self.assertFalse(manager.sync_once().success)
            self.assertFalse(manager.disabled)

    def test_announcement_is_emitted_once_then_persisted_after_display(self):
        announcement_id = str(uuid.uuid4())
        announcement = {"id": announcement_id, "title": "维护", "body": "请稍后重试。"}
        with tempfile.TemporaryDirectory() as folder:
            shown = []
            manager = ClientManagement(
                state_path=Path(folder) / "management.local.json",
                transport=lambda payload: {"disabled": False, "acknowledged": [], "announcement": announcement},
                on_announcement=shown.append,
            )
            first = manager.sync_once()
            self.assertTrue(first.success)
            self.assertEqual(shown, [announcement])
            manager.sync_once()
            self.assertEqual(shown, [announcement])
            self.assertTrue(manager.mark_announcement_seen(announcement_id))

            shown_after_restart = []
            restarted = ClientManagement(
                state_path=Path(folder) / "management.local.json",
                transport=lambda payload: {"disabled": False, "acknowledged": [], "announcement": announcement},
                on_announcement=shown_after_restart.append,
            )
            restarted.sync_once()
            self.assertEqual(shown_after_restart, [])

    def test_new_announcement_is_emitted_on_next_start_and_invalid_one_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            first_id = str(uuid.uuid4())
            first = {"id": first_id, "title": "第一条", "body": "内容"}
            manager = ClientManagement(
                state_path=Path(folder) / "management.local.json",
                transport=lambda payload: {"disabled": False, "acknowledged": [], "announcement": first},
            )
            manager.sync_once()
            manager.mark_announcement_seen(first_id)
            invalid = {"id": str(uuid.uuid4()), "title": "x" * 81, "body": "内容"}
            shown = []
            restarted = ClientManagement(
                state_path=Path(folder) / "management.local.json",
                transport=lambda payload: {"disabled": False, "acknowledged": [], "announcement": invalid},
                on_announcement=shown.append,
            )
            self.assertFalse(restarted.sync_once().success)
            self.assertEqual(shown, [])

            second_id = str(uuid.uuid4())
            second = {"id": second_id, "title": "第二条", "body": "新内容"}
            next_shown = []
            next_start = ClientManagement(
                state_path=Path(folder) / "management.local.json",
                transport=lambda payload: {"disabled": False, "acknowledged": [], "announcement": second},
                on_announcement=next_shown.append,
            )
            self.assertTrue(next_start.sync_once().success)
            self.assertEqual(next_shown, [second])


if __name__ == "__main__":
    unittest.main()
