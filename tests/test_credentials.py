import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qtui import SportsUploaderUI
from src import config_manager
from src.config_manager import ConfigManager, ConfigError


class CredentialTests(unittest.TestCase):
    def test_restart_restores_credentials_without_route(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config_manager, "CONFIGS_DIR", folder):
            ConfigManager.save_credentials("tester", "keepalive=fake; JSESSIONID=fake-session", "fake-map")
            loaded = ConfigManager.load_config()
            self.assertEqual(loaded["USER_ID"], "tester")
            self.assertEqual(loaded["COOKIE"], "keepalive=fake; JSESSIONID=fake-session")
            self.assertEqual(loaded["TENCENT_MAP_KEY"], "fake-map")
            self.assertFalse(loaded["ROUTE"]["strokes"])

    def test_explicit_clear_overrides_legacy_values(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config_manager, "CONFIGS_DIR", folder):
            Path(folder, "default.json").write_text(json.dumps({"USER_ID": "old", "COOKIE": "old"}), encoding="utf-8")
            ConfigManager._save_private_map_key("old-map")
            ConfigManager.save_credentials("", "", "")
            loaded = ConfigManager.load_config()
            for name in ("USER_ID", "COOKIE", "TENCENT_MAP_KEY"):
                self.assertEqual(loaded[name], "")

    def test_corrupt_file_does_not_silently_discard_credentials(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config_manager, "CONFIGS_DIR", folder):
            Path(folder, "credentials.local.json").write_text("invalid", encoding="utf-8")
            with self.assertRaises(ConfigError):
                ConfigManager.load_config()

    def test_ui_save_does_not_require_valid_route_or_speed(self):
        view = SimpleNamespace(
            _credentials_dirty=True, _credentials_timer=Mock(),
            user_id_input=Mock(), keepalive_input=Mock(), jsessionid_input=Mock(),
            tencent_key_input=Mock(), log_output_text=Mock(),
        )
        for field, value in (("user_id_input", "tester"), ("keepalive_input", "fake"),
                             ("jsessionid_input", "session"), ("tencent_key_input", "map")):
            getattr(view, field).text.return_value = value
        with patch.object(ConfigManager, "save_credentials") as save:
            SportsUploaderUI._persist_credentials(view)
            save.assert_called_once_with("tester", "keepalive=fake; JSESSIONID=session", "map")
            self.assertFalse(view._credentials_dirty)
            SportsUploaderUI._persist_credentials(view)
            save.assert_called_once()

    def test_failed_atomic_replace_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config_manager, "CONFIGS_DIR", folder):
            ConfigManager.save_credentials("old", "old-cookie", "old-map")
            with patch.object(Path, "replace", side_effect=OSError("test failure")):
                with self.assertRaises(OSError):
                    ConfigManager.save_credentials("new", "new-cookie", "new-map")
            self.assertEqual(ConfigManager.load_config()["USER_ID"], "old")
