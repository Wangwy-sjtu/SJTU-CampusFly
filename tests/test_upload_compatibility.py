import unittest
from unittest.mock import Mock, patch

from src.api_client import get_authorization_token_and_rules, upload_running_data
from src.config_manager import ConfigManager
from src.data_generator import generate_running_data_payload
from src.main import run_sports_upload
from src.utils import SportsUploaderError


def config():
    c = ConfigManager.get_default_config()
    c.update(API_MODE="real", COOKIE="fake-cookie", USER_ID="fake-user", START_TIME_EPOCH_MS=1700000000000)
    c["ROUTE"]["strokes"] = [[{"latitude": 31, "longitude": 121}, {"latitude": 31, "longitude": 121.001}]]
    return c


class UploadCompatibilityTests(unittest.TestCase):
    def test_missing_rule_id_matches_original_fallback_nine(self):
        payload, _, _ = generate_running_data_payload(config(), [], {"rules": {"spmin": 180, "spmax": 540}})
        self.assertEqual(payload[0]["id"], 9)

    def test_nonempty_live_rules_without_id_are_accepted(self):
        responses = [{"code": 0, "data": {"uid": "fake-token"}}, {"code": 0, "data": []},
                     {"code": 0, "data": {"rules": {"spmin": 180}, "points": []}}]
        with patch("src.api_client.make_request", side_effect=responses) as request:
            token, rules = get_authorization_token_and_rules(config())
        self.assertEqual(token, "fake-token")
        self.assertEqual(rules["rules"]["spmin"], 180)
        self.assertIn("TaskCenterApp/3.5.0", request.call_args_list[0].args[2]["User-Agent"])

    def test_rejected_or_empty_rules_stop_before_generation(self):
        for response in ({"code": 1, "data": {"rules": {"id": 9}}}, {"code": 0, "data": {}}, {"code": 0, "data": {"rules": {}}}):
            with self.subTest(response=response), patch("src.api_client.make_request", side_effect=[{"code": 0, "data": {"uid": "fake"}}, {"code": 0}, response]):
                with self.assertRaises(SportsUploaderError):
                    get_authorization_token_and_rules(config())

    def test_upload_preserves_original_transport_headers_and_one_post(self):
        with patch("src.api_client.make_request", return_value={"code": 0}) as request:
            upload_running_data(config(), "fake", [{"id": 9}])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", config()["UPLOAD_URL"]))
        self.assertEqual(request.call_args.args[2]["User-Agent"], "okhttp/4.10.0")
        self.assertEqual(request.call_args.args[2]["Host"], "pe.sjtu.edu.cn")

    def test_real_future_track_waits_even_with_old_false_setting(self):
        c = config()
        with patch("src.main.time.time", return_value=1700000000), patch("src.main.get_authorization_token_and_rules", return_value=("fake", {"rules": {"spmin": 180}})), patch("src.main._interruptible_wait", return_value=True) as wait, patch("src.main.upload_running_data", return_value={"code": 0, "data": {"accepted": True}}) as upload:
            success, message = run_sports_upload(c)
        self.assertTrue(success)
        wait.assert_called_once()
        self.assertGreater(wait.call_args.args[0], 0)
        upload.assert_called_once()
        self.assertIn("待核实", message)

    def test_cancel_during_wait_never_posts(self):
        with patch("src.main.time.time", return_value=1700000000), patch("src.main.get_authorization_token_and_rules", return_value=("fake", {"rules": {"id": 9}})), patch("src.main._interruptible_wait", return_value=False), patch("src.main.upload_running_data") as upload:
            success, _ = run_sports_upload(config())
        self.assertFalse(success)
        upload.assert_not_called()

    def test_completed_historical_track_does_not_wait(self):
        with patch("src.main.get_authorization_token_and_rules", return_value=("fake", {"rules": {"id": 9}})), patch("src.main._interruptible_wait") as wait, patch("src.main.upload_running_data", return_value={"code": 0}):
            run_sports_upload(config())
        wait.assert_not_called()

    def test_mock_never_claims_school_upload_or_calls_network(self):
        c = config()
        c["API_MODE"] = "mock"
        progress = Mock()
        with patch("src.api_client.make_request") as network:
            success, message = run_sports_upload(c, progress_callback=progress)
        network.assert_not_called()
        self.assertTrue(success)
        self.assertIn("未向学校", message)
        self.assertNotIn("上传成功", message)
        self.assertIn("未上传", progress.call_args.args[2])
