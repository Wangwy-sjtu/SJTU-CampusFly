import os
import sys
import unittest
from unittest.mock import patch

from src.utils import get_base_path, get_user_data_path


class InstallPathsTests(unittest.TestCase):
    def test_installed_settings_are_outside_program_directory(self):
        with patch.object(sys, "frozen", True, create=True), patch.dict(os.environ, {"LOCALAPPDATA": "C:/test-user"}):
            self.assertEqual(get_user_data_path(), os.path.join("C:/test-user", "SJTU-CampusFly"))
            self.assertNotEqual(get_user_data_path(), get_base_path())

    def test_source_settings_keep_existing_location(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(get_user_data_path(), get_base_path())
