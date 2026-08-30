"""GitLab 게시 동작. forge.py 로 쪼개기 전에 박아둔 기준선이다."""
import unittest

from mock_forge import MockGitLab, bot_note, note
from support import MAIL, OUT_OF_DIFF, SIGNUP, gitlab_env, post_review, run_post


class InlinePostingTest(unittest.TestCase):
    def test_심각도_순서대로_인라인을_단다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            code, _ = run_post(gitlab_env(server))

        self.assertEqual(code, 0)
        posted = [(b["position[new_path]"], b["position[new_line]"]) for b in server.inline]
        self.assertEqual(posted, [(SIGNUP, "108"), (MAIL, "42")])

    def test_diff_밖_라인은_요약으로_내려간다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))
            summary = server.summaries[0]["body"]

        self.assertIn(f"`{SIGNUP}:9999`", summary)
        self.assertNotIn("9999", str(server.inline))

    def test_파일이_없는_지적은_MR_전체로_찍힌다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))
            summary = server.summaries[0]["body"]

        self.assertIn("`MR 전체` · **P1** · 변경 규모가 리뷰 한계를 넘는다", summary)

    def test_요약에_집계가_들어간다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))
            summary = server.summaries[0]["body"]

        self.assertIn("지적 4건 (P0 1건, P1 2건, P2 1건) · 인라인 2건", summary)
        self.assertTrue(summary.startswith(post_review.MARKER))

    def test_인라인_위치는_MR_최신_버전을_따른다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))
            first = server.inline[0]

        self.assertEqual(first["position[base_sha]"], "aaaa000000000000000000000000000000000000")
        self.assertEqual(first["position[head_sha]"], "cccc000000000000000000000000000000000000")
        self.assertEqual(first["position[position_type]"], "text")
        self.assertEqual(first["position[old_path]"], first["position[new_path]"])

    def test_요청은_토큰_헤더와_폼_인코딩을_쓴다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))
            posts = [r for r in server.requests if r["method"] == "POST"]

        self.assertTrue(posts)
        for request in posts:
            self.assertEqual(request["headers"]["private-token"], "glpat-test")
            self.assertEqual(
                request["headers"]["content-type"], "application/x-www-form-urlencoded"
            )

    def test_인라인_본문의_물결표를_막는다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))

        self.assertIn("108\\~118행", server.inline[0]["body"])

    def test_요약_본문의_코드_스팬은_보존된다(self):
        with MockGitLab(invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))

        self.assertIn("`~/.pi`", server.summaries[0]["body"])


class ClearPreviousTest(unittest.TestCase):
    def test_이전_봇_노트만_지운다(self):
        seed = [
            bot_note(1),
            note(2, f"{post_review.MARKER}\n남이 쓴 마커 노트", author_id=99),
            note(3, "사람이 쓴 일반 코멘트"),
            note(4, f"{post_review.MARKER} 시스템 노트", system=True),
        ]
        with MockGitLab(seed_notes=seed, invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))

        self.assertEqual(server.deleted, [1])
        self.assertEqual([n["id"] for n in server.notes], [2, 3, 4])

    def test_100건이_넘어도_전부_지운다(self):
        seed = [bot_note(i) for i in range(1, 102)]
        with MockGitLab(seed_notes=seed, invalid_lines=OUT_OF_DIFF) as server:
            run_post(gitlab_env(server))

        self.assertEqual(sorted(server.deleted), list(range(1, 102)))
        self.assertEqual(server.notes, [])


class NoTokenTest(unittest.TestCase):
    def test_토큰이_없으면_아무_요청도_하지_않는다(self):
        with MockGitLab() as server:
            code, out = run_post(gitlab_env(server, token=None))

        self.assertEqual(code, 0)
        self.assertEqual(server.requests, [])
        self.assertIn("PI_GITLAB_TOKEN 이 없어", out)


if __name__ == "__main__":
    unittest.main()
