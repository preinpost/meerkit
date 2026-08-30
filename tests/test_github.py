"""GitHub 게시 동작. 라우트와 상태 코드는 REST API 2022-11-28 문서 기준이다."""
import tempfile
import unittest
from unittest import mock as mocklib

from mock_forge import MockGitHub, bot_comment, comment
from support import MAIL, OUT_OF_DIFF, SIGNUP, github_env, post_review, run_post, write_event

from forge import GitHubForge

RATE_LIMITED = [(SIGNUP, 108)]


class GitHubCase(unittest.TestCase):
    def setUp(self):
        # 실제 간격은 1초다. 여기서는 꺼두고 간격 자체는 ThrottleTest 에서 따로 본다.
        patcher = mocklib.patch.object(GitHubForge, "mutation_interval", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event = write_event(tmp.name)

    def post(self, server, **kwargs):
        return run_post(github_env(server, self.event, **kwargs))


class InlinePostingTest(GitHubCase):
    def test_심각도_순서대로_리뷰_코멘트를_단다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            code, _ = self.post(server)

        self.assertEqual(code, 0)
        self.assertEqual(
            [(b["path"], b["line"]) for b in server.inline], [(SIGNUP, 108), (MAIL, 42)]
        )

    def test_페이로드는_head_sha_와_RIGHT_면을_쓴다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            self.post(server)
            first = server.inline[0]

        self.assertEqual(first["commit_id"], "cccc000000000000000000000000000000000000")
        self.assertEqual(first["side"], "RIGHT")
        self.assertIsInstance(first["line"], int)

    def test_요청_헤더가_문서_규격을_따른다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            self.post(server)
            posts = [r for r in server.requests if r["method"] == "POST"]

        self.assertTrue(posts)
        for request in posts:
            self.assertEqual(request["headers"]["authorization"], "Bearer ghs-test")
            self.assertEqual(request["headers"]["accept"], "application/vnd.github+json")
            self.assertEqual(request["headers"]["x-github-api-version"], "2022-11-28")
            self.assertEqual(request["headers"]["content-type"], "application/json")

    def test_422_는_요약으로_내려간다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            code, _ = self.post(server)
            summary = server.summaries[0]["body"]

        self.assertEqual(code, 0)
        self.assertIn(f"`{SIGNUP}:9999`", summary)

    def test_403_은_422_와_구분되어_실패로_남는다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF, forbidden_lines=RATE_LIMITED) as server:
            code, out = self.post(server)
            summary = server.summaries[0]["body"]

        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", out)
        # 실패해도 요약은 올라가고, 밀려난 지적이 거기 남는다.
        self.assertIn(f"`{SIGNUP}:108`", summary)
        self.assertEqual([b["path"] for b in server.inline], [MAIL])

    def test_파일이_없는_지적은_PR_전체로_찍힌다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            self.post(server)
            summary = server.summaries[0]["body"]

        self.assertIn("`PR 전체` · **P1** · 변경 규모가 리뷰 한계를 넘는다", summary)

    def test_요약은_이슈_코멘트로_올라간다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            self.post(server)
            issue_posts = [
                r for r in server.requests
                if r["method"] == "POST" and "/issues/" in r["path"]
            ]

        self.assertEqual(len(issue_posts), 1)
        self.assertTrue(issue_posts[0]["body"]["body"].startswith(post_review.MARKER))

    def test_물결표_처리는_GitLab_과_같다(self):
        with MockGitHub(invalid_lines=OUT_OF_DIFF) as server:
            self.post(server)

        self.assertIn("108\\~118행", server.inline[0]["body"])
        self.assertIn("`~/.pi`", server.summaries[0]["body"])


class ClearPreviousTest(GitHubCase):
    def test_리뷰_코멘트와_이슈_코멘트를_모두_지운다(self):
        with MockGitHub(
            seed_review_comments=[bot_comment(1), comment(2, "사람이 쓴 리뷰 코멘트")],
            seed_issue_comments=[bot_comment(3), comment(4, "사람이 쓴 요약 코멘트")],
            invalid_lines=OUT_OF_DIFF,
        ) as server:
            self.post(server)

        self.assertEqual(sorted(server.deleted), [1, 3])
        self.assertEqual([c["id"] for c in server.review_comments], [2])
        self.assertEqual([c["id"] for c in server.issue_comments], [4])

    def test_삭제_권한이_없으면_넘어간다(self):
        """GITHUB_TOKEN 은 GET /user 가 막혀 있어 작성자를 미리 못 거른다.

        마커가 붙은 남의 코멘트는 지워봐야 403 인 줄 안다.
        """
        with MockGitHub(
            seed_review_comments=[bot_comment(1), bot_comment(2)],
            forbidden_ids=[2],
            invalid_lines=OUT_OF_DIFF,
        ) as server:
            code, out = self.post(server)

        self.assertEqual(code, 0)
        self.assertEqual(server.deleted, [1])
        self.assertIn("이전 코멘트 삭제 실패 id=2 403", out)

    def test_100건이_넘어도_전부_지운다(self):
        with MockGitHub(
            seed_review_comments=[bot_comment(i) for i in range(1, 102)],
            invalid_lines=OUT_OF_DIFF,
        ) as server:
            self.post(server)

        self.assertEqual(sorted(server.deleted), list(range(1, 102)))
        self.assertEqual(server.review_comments, [])


class NoTokenTest(GitHubCase):
    def test_토큰이_없으면_아무_요청도_하지_않는다(self):
        with MockGitHub() as server:
            code, out = self.post(server, token=None)

        self.assertEqual(code, 0)
        self.assertEqual(server.requests, [])
        self.assertIn("GITHUB_TOKEN 이 없어", out)


if __name__ == "__main__":
    unittest.main()
