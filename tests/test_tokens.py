"""OAuth 토큰 파싱 및 Meridian 다중 프로필 등록 테스트."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path

import meridian_runner

run_review = importlib.import_module("run-review")
parse_oauth_tokens = meridian_runner.parse_oauth_tokens
setup_meridian_profiles = meridian_runner.setup_meridian_profiles


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

    def test_setup_meridian_profiles_usage_strategy(self):
        from unittest.mock import patch

        usage_map = {
            "sk-ant-oat01-busy": 0.85,
            "sk-ant-oat01-free": 0.15,
            "sk-ant-oat01-med": 0.50,
        }

        def mock_fetch(token, timeout=3.0):
            return usage_map.get(token)

        with tempfile.TemporaryDirectory() as tmpdir:
            import os

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmpdir
            try:
                pairs = [
                    ("busy_acc", "sk-ant-oat01-busy"),
                    ("free_acc", "sk-ant-oat01-free"),
                    ("med_acc", "sk-ant-oat01-med"),
                ]
                with patch.object(meridian_runner, "fetch_oauth_usage", side_effect=mock_fetch):
                    default_id, ordered = setup_meridian_profiles(pairs, strategy="low_usage")

                # 가장 사용률이 낮은(15%) free_acc 가 1순위로 선택되어야 한다.
                self.assertEqual(default_id, "free_acc")
                self.assertEqual(
                    [p[0] for p in ordered],
                    ["free_acc", "med_acc", "busy_acc"],
                )

                config_dir = Path(tmpdir) / ".config" / "meridian"
                settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["activeProfile"], "free_acc")
                self.assertEqual(
                    settings["profileOrder"],
                    ["free_acc", "med_acc", "busy_acc"],
                )
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)

    def test_setup_meridian_profiles_usage_fallback_on_error(self):
        from unittest.mock import patch

        # err_acc 는 에러(None), valid_acc 는 30% 사용률
        def mock_fetch(token, timeout=3.0):
            return None if "err" in token else 0.30

        with tempfile.TemporaryDirectory() as tmpdir:
            import os

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmpdir
            try:
                pairs = [
                    ("err_acc", "sk-ant-oat01-err"),
                    ("valid_acc", "sk-ant-oat01-valid"),
                ]
                with patch.object(meridian_runner, "fetch_oauth_usage", side_effect=mock_fetch):
                    default_id, ordered = setup_meridian_profiles(pairs, strategy="low_usage")

                # 조회가 실패한 계정보다 정상 조회된 계정(valid_acc)이 우선 선택되어야 한다.
                self.assertEqual(default_id, "valid_acc")
                self.assertEqual([p[0] for p in ordered], ["valid_acc", "err_acc"])
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)

    def test_print_token_usage_summary(self):
        import io
        from contextlib import redirect_stdout

        summary_data = {
            "tokenUsage": {
                "totalInputTokens": 12500,
                "totalOutputTokens": 350,
                "totalCacheReadTokens": 11000,
                "totalCacheCreationTokens": 1500,
                "avgCacheHitRate": 0.88,
            },
            "costEstimate": {
                "totalUsd": 0.0245,
            },
        }

        buf = io.StringIO()
        with redirect_stdout(buf):
            meridian_runner.print_token_usage_summary(summary_data)

        output = buf.getvalue()
        self.assertIn("12,500", output)
        self.assertIn("350", output)
        self.assertIn("88.0%", output)
        self.assertIn("$0.0245", output)
