"""OAuth 토큰 파싱 및 Meridian 다중 프로필 등록 테스트."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path

run_review = importlib.import_module("run-review")
parse_oauth_tokens = run_review.parse_oauth_tokens
setup_meridian_profiles = run_review.setup_meridian_profiles


class TestOAuthTokens(unittest.TestCase):
    def test_single_token(self):
        raw = "sk-ant-oat01-single-token-12345"
        pairs = parse_oauth_tokens(raw)
        self.assertEqual(pairs, [("default", "sk-ant-oat01-single-token-12345")])

    def test_comma_separated_tokens(self):
        raw = "sk-ant-oat01-tok1, sk-ant-oat01-tok2, sk-ant-oat01-tok3"
        pairs = parse_oauth_tokens(raw)
        self.assertEqual(
            pairs,
            [
                ("default", "sk-ant-oat01-tok1"),
                ("profile-1", "sk-ant-oat01-tok2"),
                ("profile-2", "sk-ant-oat01-tok3"),
            ],
        )

    def test_newline_separated_tokens(self):
        raw = "sk-ant-oat01-tok1\nsk-ant-oat01-tok2"
        pairs = parse_oauth_tokens(raw)
        self.assertEqual(
            pairs,
            [
                ("default", "sk-ant-oat01-tok1"),
                ("profile-1", "sk-ant-oat01-tok2"),
            ],
        )

    def test_named_pairs(self):
        raw = "work:sk-ant-oat01-work, personal:sk-ant-oat01-personal"
        pairs = parse_oauth_tokens(raw)
        self.assertEqual(
            pairs,
            [
                ("work", "sk-ant-oat01-work"),
                ("personal", "sk-ant-oat01-personal"),
            ],
        )

    def test_json_list(self):
        raw = json.dumps(["sk-ant-oat01-a", "sk-ant-oat01-b"])
        pairs = parse_oauth_tokens(raw)
        self.assertEqual(
            pairs,
            [
                ("default", "sk-ant-oat01-a"),
                ("profile-1", "sk-ant-oat01-b"),
            ],
        )

    def test_json_dict(self):
        raw = json.dumps({"main": "sk-ant-oat01-main", "sub": "sk-ant-oat01-sub"})
        pairs = parse_oauth_tokens(raw)
        self.assertEqual(
            pairs,
            [
                ("main", "sk-ant-oat01-main"),
                ("sub", "sk-ant-oat01-sub"),
            ],
        )

    def test_setup_meridian_profiles_fixed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import os

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmpdir
            try:
                pairs = [("default", "sk-ant-oat01-first"), ("backup", "sk-ant-oat01-second")]
                default_id, ordered = setup_meridian_profiles(pairs, strategy="first")
                self.assertEqual(default_id, "default")
                self.assertEqual(len(ordered), 2)

                config_dir = Path(tmpdir) / ".config" / "meridian"
                profiles_path = config_dir / "profiles.json"
                settings_path = config_dir / "settings.json"

                self.assertTrue(profiles_path.exists())
                self.assertTrue(settings_path.exists())

                profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
                self.assertEqual(len(profiles), 2)
                self.assertEqual(profiles[0]["id"], "default")
                self.assertEqual(profiles[0]["oauthToken"], "sk-ant-oat01-first")
                self.assertEqual(profiles[0]["type"], "oauth-token")
                self.assertEqual(profiles[1]["id"], "backup")
                self.assertEqual(profiles[1]["oauthToken"], "sk-ant-oat01-second")

                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(settings["activeProfile"], "default")
                self.assertEqual(settings["profileOrder"], ["default", "backup"])
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)

    def test_setup_meridian_profiles_random_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import os

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmpdir
            try:
                pairs = [
                    ("acc1", "sk-ant-oat01-1"),
                    ("acc2", "sk-ant-oat01-2"),
                    ("acc3", "sk-ant-oat01-3"),
                ]
                default_id, ordered = setup_meridian_profiles(pairs, strategy="random")
                self.assertIn(default_id, ["acc1", "acc2", "acc3"])
                self.assertEqual(ordered[0][0], default_id)
                self.assertEqual(len(ordered), 3)

                config_dir = Path(tmpdir) / ".config" / "meridian"
                settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["activeProfile"], default_id)
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)
