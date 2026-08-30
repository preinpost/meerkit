"""플랫폼과 무관한 부분 — diff 라인 맵, 상류 필터, 레이트 리미터, 물결표."""
import contextlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from mock_forge import MockGitLab
from support import MAIL, SIGNUP, gitlab_env, post_review, run_post

from forge import Forge


def git(cwd, *args):
    return subprocess.run(
        ("git",) + args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(root, path, lines):
    target = Path(root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{n}\n" for n in lines), encoding="utf-8")


@contextlib.contextmanager
def repo(build):
    """임시 저장소를 만들고 그 안에서 실행한다. build(root) 가 두 번째 커밋을 만든다."""
    with tempfile.TemporaryDirectory() as root:
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "tester")
        base = build(root)
        with contextlib.chdir(root):
            yield base


class DiffLinesTest(unittest.TestCase):
    def test_변경된_라인만_파일별로_모은다(self):
        def build(root):
            write(root, "keep.txt", range(1, 6))
            write(root, "gone.txt", ["사라질 파일"])
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")
            write(root, "keep.txt", [1, 2, "X", 4, 5, 6])
            write(root, "added.txt", ["new"])
            os.remove(Path(root) / "gone.txt")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "change")

        with repo(build):
            changed = post_review.diff_lines("HEAD~1...HEAD")

        self.assertEqual(changed["keep.txt"], {3, 6})
        self.assertEqual(changed["added.txt"], {1})
        self.assertNotIn("gone.txt", changed)

    def test_범위를_모르면_거르지_않는다(self):
        self.assertIsNone(post_review.diff_lines(None))

    def test_git_이_실패하면_거르지_않는다(self):
        def build(root):
            write(root, "a.txt", ["a"])
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")

        with repo(build):
            self.assertIsNone(post_review.diff_lines("존재하지않는sha...HEAD"))


class SplitFindingsTest(unittest.TestCase):
    def test_diff_밖_라인은_보내기_전에_제외된다(self):
        findings = [
            {"file": "a.py", "line": 10},
            {"file": "a.py", "line": 99},
            {"file": "b.py", "line": 1},
            {"file": None, "line": None},
        ]
        inline, unpositioned = post_review.split_findings(findings, {"a.py": {10, 11}})

        self.assertEqual(inline, findings[:1])
        self.assertEqual(unpositioned, findings[1:])

    def test_맵이_없으면_전부_시도한다(self):
        findings = [{"file": "a.py", "line": 99}, {"file": None, "line": None}]
        inline, unpositioned = post_review.split_findings(findings, None)

        self.assertEqual(inline, findings[:1])
        self.assertEqual(unpositioned, findings[1:])


class UpstreamFilterTest(unittest.TestCase):
    """diff 를 알면 거부당할 요청을 아예 보내지 않는다."""

    def build(self, root):
        write(root, SIGNUP, range(1, 121))
        write(root, MAIL, range(1, 51))
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        base = git(root, "rev-parse", "HEAD")
        write(root, SIGNUP, [n if n != 108 else "변경" for n in range(1, 121)])
        write(root, MAIL, [n if n != 42 else "변경" for n in range(1, 51)])
        git(root, "add", "-A")
        git(root, "commit", "-qm", "change")
        return base

    def test_diff_밖_지적은_요청조차_하지_않는다(self):
        with repo(self.build) as base, MockGitLab() as server:
            env = gitlab_env(server)
            env["CI_MERGE_REQUEST_DIFF_BASE_SHA"] = base
            code, out = run_post(env)
            summary = server.summaries[0]["body"]

        self.assertEqual(code, 0)
        # 목에 invalid_lines 를 주지 않았는데도 9999 는 인라인으로 시도되지 않는다.
        self.assertEqual([b["position[new_line]"] for b in server.inline], ["108", "42"])
        self.assertIn("diff 밖 라인이라 인라인에서 제외: 1건", out)
        # 빠진 것이 아니라 요약으로 옮겨간다.
        self.assertIn(f"`{SIGNUP}:9999`", summary)


class ThrottleTest(unittest.TestCase):
    def test_변이_요청_사이에_간격을_둔다(self):
        forge = Forge("token")
        forge.mutation_interval = 0.05

        start = time.monotonic()
        for _ in range(3):
            forge._throttle()

        self.assertGreaterEqual(time.monotonic() - start, 0.1)

    def test_간격이_0이면_기다리지_않는다(self):
        forge = Forge("token")

        start = time.monotonic()
        for _ in range(200):
            forge._throttle()

        self.assertLess(time.monotonic() - start, 0.05)


class TildeEscapeTest(unittest.TestCase):
    def test_코드_스팬_밖의_물결표만_막는다(self):
        self.assertEqual(
            post_review.escape_tildes("108~118행을 보라. 설정은 `~/.pi` 아래다."),
            "108\\~118행을 보라. 설정은 `~/.pi` 아래다.",
        )


if __name__ == "__main__":
    unittest.main()
