"""테스트용 가짜 포지 서버 (GitLab / GitHub).

post_review.py 가 실제로 어떤 요청을 보내는지 받아 적는다.

응답 규칙은 API 문서를 보고 넣은 가정이다. 여기를 통과했다고 실물이 받아준다는
뜻은 아니다 — 그건 실제 프로젝트에 대고 돌려봐야 안다. 이 서버의 쓸모는
게시 로직을 리팩터링할 때 동작이 그대로인지 확인하는 것이다.

GitHub 라우트는 REST API 버전 2022-11-28 문서 기준이다.
  POST   /repos/{o}/{r}/pulls/{n}/comments      201 / 403 / 422
  GET    /repos/{o}/{r}/pulls/{n}/comments      200            (per_page 최대 100)
  DELETE /repos/{o}/{r}/pulls/comments/{id}     204 / 404
  POST   /repos/{o}/{r}/issues/{n}/comments     201 / 403 / 404 / 410 / 422
  GET    /repos/{o}/{r}/issues/{n}/comments     200 / 404 / 410
  DELETE /repos/{o}/{r}/issues/comments/{id}    204
"""
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "<!-- meerkit-bot -->"
BASE_SHA = "aaaa000000000000000000000000000000000000"
START_SHA = "bbbb000000000000000000000000000000000000"
HEAD_SHA = "cccc000000000000000000000000000000000000"

BOT_USER_ID = 42
BOT_LOGIN = "github-actions[bot]"


class _MockForge:
    """with 문으로 띄운다. 라우팅은 하위 클래스의 _route 가 맡는다."""

    def __init__(self):
        self.requests = []
        self.deleted = []
        self.inline = []
        self.summaries = []
        self._next_id = 9000

    def __enter__(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                mock._handle(self, "GET")

            def do_POST(self):
                mock._handle(self, "POST")

            def do_DELETE(self):
                mock._handle(self, "DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def paths(self, method):
        return [r["path"] for r in self.requests if r["method"] == method]

    def _handle(self, handler, method):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        self.requests.append(
            {
                "method": method,
                "path": handler.path,
                # urllib 이 헤더 이름을 capitalize 해서 보낸다(PRIVATE-TOKEN -> Private-token).
                "headers": {k.lower(): v for k, v in handler.headers.items()},
                "body": _parse_body(handler.headers.get("Content-Type"), raw),
            }
        )

        status, payload = self._route(method, handler.path, self.requests[-1]["body"])
        data = b"" if payload is None else json.dumps(payload).encode()
        handler.send_response(status)
        if data:
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        if data:
            handler.wfile.write(data)

    def _issue_id(self):
        self._next_id += 1
        return self._next_id


MR = r"/projects/[^/]+/merge_requests/\d+"
GL_USER = re.compile(r"^/api/v4/user$")
GL_VERSIONS = re.compile(rf"^/api/v4{MR}/versions$")
GL_DISCUSSIONS = re.compile(rf"^/api/v4{MR}/discussions$")
GL_NOTES = re.compile(rf"^/api/v4{MR}/notes$")
GL_NOTE = re.compile(rf"^/api/v4{MR}/notes/(\d+)$")


def note(note_id, body="", author_id=BOT_USER_ID, system=False):
    return {"id": note_id, "body": body, "author": {"id": author_id}, "system": system}


def bot_note(note_id):
    return note(note_id, f"{MARKER}\n**P1** · 이전 실행이 남긴 지적")


class MockGitLab(_MockForge):
    """api_url 을 CI_API_V4_URL 에 넣어 쓴다.

    invalid_lines: 인라인 게시를 400 으로 거부할 (파일, 라인) 집합.
                   실제 MR 에서는 지적한 라인이 diff 에 없을 때 나는 응답이다.
    seed_notes:    clear_previous 가 훑을 기존 노트. 하나당 discussion 하나로 싼다.
    """

    def __init__(self, seed_notes=(), invalid_lines=()):
        super().__init__()
        self.notes = [dict(n) for n in seed_notes]
        self.invalid_lines = {(f, int(line)) for f, line in invalid_lines}

    @property
    def api_url(self):
        return f"{self.base_url}/api/v4"

    def _route(self, method, raw_path, body):
        path, _, query = raw_path.partition("?")

        if method == "GET" and GL_USER.match(path):
            return 200, {"id": BOT_USER_ID}

        if method == "GET" and GL_VERSIONS.match(path):
            return 200, [
                {
                    "base_commit_sha": BASE_SHA,
                    "start_commit_sha": START_SHA,
                    "head_commit_sha": HEAD_SHA,
                }
            ]

        if method == "GET" and GL_DISCUSSIONS.match(path):
            window = _page(self.notes, query, default_per_page=20)
            return 200, [{"notes": [n]} for n in window]

        if method == "POST" and GL_DISCUSSIONS.match(path):
            target = (body.get("position[new_path]"), int(body.get("position[new_line]", 0)))
            if target in self.invalid_lines:
                return 400, {"message": {"base": ["line_code 를 찾을 수 없습니다"]}}
            self.inline.append(body)
            return 201, {"id": self._issue_id()}

        if method == "POST" and GL_NOTES.match(path):
            self.summaries.append(body)
            return 201, {"id": self._issue_id()}

        match = GL_NOTE.match(path)
        if method == "DELETE" and match:
            note_id = int(match.group(1))
            remaining = [n for n in self.notes if n["id"] != note_id]
            if len(remaining) == len(self.notes):
                return 404, {"message": "404 Not found"}
            self.notes = remaining
            self.deleted.append(note_id)
            return 204, None

        return 404, {"message": f"라우트 없음: {method} {path}"}


GH_REPO = r"/repos/[^/]+/[^/]+"
GH_REVIEW_COMMENTS = re.compile(rf"^{GH_REPO}/pulls/\d+/comments$")
GH_REVIEW_COMMENT = re.compile(rf"^{GH_REPO}/pulls/comments/(\d+)$")
GH_ISSUE_COMMENTS = re.compile(rf"^{GH_REPO}/issues/\d+/comments$")
GH_ISSUE_COMMENT = re.compile(rf"^{GH_REPO}/issues/comments/(\d+)$")

REQUIRED_REVIEW_FIELDS = ("body", "commit_id", "path", "line")


def comment(comment_id, body="", login=BOT_LOGIN, user_type="Bot"):
    return {"id": comment_id, "body": body, "user": {"login": login, "type": user_type}}


def bot_comment(comment_id):
    return comment(comment_id, f"{MARKER}\n**P1** · 이전 실행이 남긴 지적")


class MockGitHub(_MockForge):
    """api_url 을 GITHUB_API_URL 에 넣어 쓴다.

    GitLab 과 달리 게시 주체를 미리 알 수 없다. GITHUB_TOKEN 은 설치 토큰이라
    GET /user 가 막혀 있어서, 남의 코멘트인지는 삭제를 시도해봐야 안다.
    forbidden_ids 가 그 403 을 흉내낸다.
    """

    def __init__(
        self,
        seed_review_comments=(),
        seed_issue_comments=(),
        invalid_lines=(),
        forbidden_lines=(),
        forbidden_ids=(),
    ):
        super().__init__()
        self.review_comments = [dict(c) for c in seed_review_comments]
        self.issue_comments = [dict(c) for c in seed_issue_comments]
        self.invalid_lines = {(f, int(line)) for f, line in invalid_lines}
        # 레이트 리밋이나 권한 부족으로 거부되는 자리. 422 와 구별되어야 한다.
        self.forbidden_lines = {(f, int(line)) for f, line in forbidden_lines}
        self.forbidden_ids = set(forbidden_ids)

    @property
    def api_url(self):
        return self.base_url

    def _route(self, method, raw_path, body):
        path, _, query = raw_path.partition("?")

        if method == "GET" and GH_REVIEW_COMMENTS.match(path):
            return 200, _page(self.review_comments, query, default_per_page=30)

        if method == "POST" and GH_REVIEW_COMMENTS.match(path):
            missing = [f for f in REQUIRED_REVIEW_FIELDS if body.get(f) in (None, "")]
            if missing:
                return 422, _validation_failed(
                    "PullRequestReviewComment", missing[0], "missing_field"
                )
            target = (body["path"], int(body["line"]))
            if target in self.forbidden_lines:
                return 403, {"message": "You have exceeded a secondary rate limit"}
            if target in self.invalid_lines:
                return 422, _validation_failed(
                    "PullRequestReviewComment", "line", "custom", "line must be part of the diff"
                )
            self.inline.append(body)
            return 201, comment(self._issue_id(), body["body"])

        if method == "GET" and GH_ISSUE_COMMENTS.match(path):
            return 200, _page(self.issue_comments, query, default_per_page=30)

        if method == "POST" and GH_ISSUE_COMMENTS.match(path):
            self.summaries.append(body)
            return 201, comment(self._issue_id(), body.get("body", ""))

        for pattern, store in (
            (GH_REVIEW_COMMENT, "review_comments"),
            (GH_ISSUE_COMMENT, "issue_comments"),
        ):
            match = pattern.match(path)
            if method == "DELETE" and match:
                return self._delete(store, int(match.group(1)))

        return 404, {"message": f"라우트 없음: {method} {path}"}

    def _delete(self, store, comment_id):
        if comment_id in self.forbidden_ids:
            return 403, {"message": "Resource not accessible by integration"}
        current = getattr(self, store)
        remaining = [c for c in current if c["id"] != comment_id]
        if len(remaining) == len(current):
            return 404, {"message": "Not Found"}
        setattr(self, store, remaining)
        self.deleted.append(comment_id)
        return 204, None


def _validation_failed(resource, field, code, message=None):
    error = {"resource": resource, "field": field, "code": code}
    if message:
        error["message"] = message
    return {"message": "Validation Failed", "errors": [error]}


def _page(items, query, default_per_page):
    params = urllib.parse.parse_qs(query)
    per_page = min(int(params.get("per_page", [default_per_page])[0]), 100)
    page = int(params.get("page", ["1"])[0])
    return items[(page - 1) * per_page : page * per_page]


def _parse_body(content_type, raw):
    if not raw:
        return {}
    text = raw.decode()
    if content_type and "json" in content_type:
        return json.loads(text)
    return {k: v[0] for k, v in urllib.parse.parse_qs(text, keep_blank_values=True).items()}
