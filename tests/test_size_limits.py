"""변경 규모 측정 및 자동 리뷰 건너뛰기 테스트."""
import importlib
import unittest

run_review = importlib.import_module("run-review")
is_ignored_diff_file = run_review.is_ignored_diff_file
generate_oversize_report = run_review.generate_oversize_report
get_size_limits = run_review.get_size_limits


class TestSizeLimits(unittest.TestCase):
    def test_ignored_diff_files(self):
        # 락 파일 및 미니파이드 파일 필터링 검증
        self.assertTrue(is_ignored_diff_file("pnpm-lock.yaml"))
        self.assertTrue(is_ignored_diff_file("package-lock.json"))
        self.assertTrue(is_ignored_diff_file("yarn.lock"))
        self.assertTrue(is_ignored_diff_file("bun.lock"))
        self.assertTrue(is_ignored_diff_file("bun.lockb"))
        self.assertTrue(is_ignored_diff_file("Cargo.lock"))
        self.assertTrue(is_ignored_diff_file("poetry.lock"))
        self.assertTrue(is_ignored_diff_file("uv.lock"))
        self.assertTrue(is_ignored_diff_file("sub/dir/pnpm-lock.yaml"))
        self.assertTrue(is_ignored_diff_file("dist/bundle.min.js"))
        self.assertTrue(is_ignored_diff_file("bundle.min.css"))
        self.assertTrue(is_ignored_diff_file("bundle.js.map"))

        # 일반 소스 파일은 통과해야 함
        self.assertFalse(is_ignored_diff_file("src/auth.py"))
        self.assertFalse(is_ignored_diff_file("components/Button.tsx"))
        self.assertFalse(is_ignored_diff_file("README.md"))

    def test_generate_oversize_report(self):
        report = generate_oversize_report(
            total_lines=1500, total_files=50, max_lines=1200, max_files=40
        )
        self.assertIn("자동 리뷰 상한선을 초과하여", report["summary"])
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["severity"], "P0")
        self.assertIsNone(finding["file"])
        self.assertIsNone(finding["line"])
        self.assertIn("1,500줄", finding["detail"])
        self.assertIn("50개", finding["detail"])

    def test_get_size_limits_default(self):
        import os

        old_lines = os.environ.pop("MEERKIT_MAX_LINES", None)
        old_files = os.environ.pop("MEERKIT_MAX_FILES", None)
        try:
            max_lines, max_files = get_size_limits()
            self.assertEqual(max_lines, 1200)
            self.assertEqual(max_files, 40)
        finally:
            if old_lines is not None:
                os.environ["MEERKIT_MAX_LINES"] = old_lines
            if old_files is not None:
                os.environ["MEERKIT_MAX_FILES"] = old_files
