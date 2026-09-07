"""플랫폼과 무관한 부분 — diff 라인 맵, 상류 필터, 레이트 리미터, 물결표."""
import contextlib
import json
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
    def test_변경_라인은_변경_전_번호가_없다(self):
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

        # 3번은 고칬 줄, 6번은 말밌에 붙은 줄 — 둘 다 변경 전 번호가 없다.
        # 나머지는 그대로 남은 컨텍스트 줄이라 변경 전 번호를 든다.
        self.assertEqual(
            changed["keep.txt"], {1: 1, 2: 2, 3: None, 4: 4, 5: 5, 6: None}
        )
        self.assertEqual(changed["added.txt"], {1: None})
        self.assertNotIn("gone.txt", changed)

    def test_변경에서_멀리_떨어진_줄은_담지_않는다(self):
        def build(root):
            write(root, "a.txt", range(1, 21))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")
            write(root, "a.txt", [n if n != 10 else "X" for n in range(1, 21)])
            git(root, "add", "-A")
            git(root, "commit", "-qm", "change")

        with repo(build):
            changed = post_review.diff_lines("HEAD~1...HEAD")

        # 기본 컨텍스트 3줄 — GitLab 과 GitHub 이 그려주는 폭 만큼만 연다.
        self.assertEqual(sorted(changed["a.txt"]), [7, 8, 9, 10, 11, 12, 13])

    def test_본문에_들어있는_diff_모양_줄을_헤더로_오인하지_않는다(self):
        def build(root):
            write(root, "patch.md", ["a", "b"])
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")
            write(root, "patch.md", ["a", "+++ /dev/null", "@@ -1 +1 @@", "b"])
            git(root, "add", "-A")
            git(root, "commit", "-qm", "change")

        with repo(build):
            changed = post_review.diff_lines("HEAD~1...HEAD")

        self.assertEqual(sorted(changed), ["patch.md"])
        self.assertEqual(changed["patch.md"], {1: 1, 2: None, 3: None, 4: 2})

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
        inline, unpositioned = post_review.split_findings(
            findings, {"a.py": {10: None, 11: 7}}
        )

        self.assertEqual(inline, findings[:1])
        self.assertEqual(unpositioned, findings[1:])

    def test_맵이_없으면_전부_시도한다(self):
        findings = [{"file": "a.py", "line": 99}, {"file": None, "line": None}]
        inline, unpositioned = post_review.split_findings(findings, None)

        self.assertEqual(inline, findings[:1])
        self.assertEqual(unpositioned, findings[1:])


class OldLineTest(unittest.TestCase):
    def setUp(self):
        self.changed = {"a.py": {10: None, 11: 7}}

    def test_추가된_라인은_변경_전_번호가_없다(self):
        finding = {"file": "a.py", "line": 10}
        self.assertIsNone(post_review.old_line_of(finding, self.changed))

    def test_컨텍스트_라인은_변경_전_번호를_돌려준다(self):
        finding = {"file": "a.py", "line": 11}
        self.assertEqual(post_review.old_line_of(finding, self.changed), 7)

    def test_맵이_없으면_모른다고_한다(self):
        finding = {"file": "a.py", "line": 11}
        self.assertIsNone(post_review.old_line_of(finding, None))


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


class ContextLineTest(unittest.TestCase):
    """변경 주변의 기존 코드에 대한 지적도 인라인으로 붙는다."""

    def build(self, root):
        write(root, SIGNUP, range(1, 121))
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        base = git(root, "rev-parse", "HEAD")
        write(root, SIGNUP, [n if n != 108 else "변경" for n in range(1, 121)])
        git(root, "add", "-A")
        git(root, "commit", "-qm", "change")
        return base

    def report(self, root, line):
        path = Path(root) / "report.json"
        path.write_text(
            json.dumps(
                {
                    "summary": "",
                    "findings": [
                        {
                            "file": SIGNUP,
                            "line": line,
                            "severity": "P1",
                            "title": "변경 주변 기존 코드에 대한 지적",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def run_with(self, line, **mock_kwargs):
        with repo(self.build) as base, MockGitLab(**mock_kwargs) as server:
            env = gitlab_env(server)
            env["CI_MERGE_REQUEST_DIFF_BASE_SHA"] = base
            env["MEERKIT_JSON"] = self.report(os.getcwd(), line)
            code, out = run_post(env)
            return code, out, server.inline, server.summaries[0]["body"]

    def test_컨텍스트_라인에는_변경_전_번호를_함께_보낸다(self):
        code, _, inline, _ = self.run_with(110, context_lines=[(SIGNUP, 110)])

        self.assertEqual(code, 0)
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0]["position[new_line]"], "110")
        self.assertEqual(inline[0]["position[old_line]"], "110")

    def test_추가된_라인에는_변경_전_번호를_보내지_않는다(self):
        code, _, inline, _ = self.run_with(108)

        self.assertEqual(code, 0)
        self.assertEqual(len(inline), 1)
        self.assertNotIn("position[old_line]", inline[0])

    def test_컨텍스트_밖_라인은_요약으로_내려간다(self):
        code, out, inline, summary = self.run_with(60)

        self.assertEqual(code, 0)
        self.assertEqual(inline, [])
        self.assertIn("diff 밖 라인이라 인라인에서 제외: 1건", out)
        self.assertIn("### 라인에 달 수 없는 리뷰", summary)
        self.assertIn(f"`{SIGNUP}:60`", summary)


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


class AdditionalSystemPromptTest(unittest.TestCase):
    def test_환경변수가_없을_때_추가_지시문_미포함(self):
        from unittest.mock import patch

        import run_review

        with patch.dict(os.environ, {}, clear=True):
            prompt = run_review.build_prompt("HEAD~1...HEAD", "out.json", "MR")
            self.assertNotIn("## 추가 프로젝트 리뷰 지침", prompt)
            self.assertIn("## 한국어 문장 작성 지침", prompt)

    def test_텍스트_문자열_주입_시_프롬프트에_정상_결합(self):
        from unittest.mock import patch

        import run_review

        custom_guide = "보안상 SQL 인젝션 취약점을 최우선으로 검토할 것."
        with patch.dict(os.environ, {"ADD_SYSTEM_PROMPT": custom_guide}):
            prompt = run_review.build_prompt("HEAD~1...HEAD", "out.json", "MR")
            self.assertIn("## 추가 프로젝트 리뷰 지침", prompt)
            self.assertIn(custom_guide, prompt)
            self.assertIn("## 한국어 문장 작성 지침", prompt)

    def test_파일_경로_주입_시_파일_본문_로드(self):
        from unittest.mock import patch

        import run_review

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write("파일에서 로드된 커스텀 프롬프트 지침")
            f.flush()
            temp_path = f.name

        try:
            with patch.dict(os.environ, {"ADD_SYSTEM_PROMPT": temp_path}):
                prompt = run_review.build_prompt("HEAD~1...HEAD", "out.json", "MR")
                self.assertIn("파일에서 로드된 커스텀 프롬프트 지침", prompt)
        finally:
            os.unlink(temp_path)


if __name__ == "__main__":
    unittest.main()
