"""GitLab / GitHub REST 어댑터.

게시에서 플랫폼 차이를 여기로 몰아둔다. 본문 렌더·정렬·요약 조립은 post_review.py 에
남으며 플랫폼과 무관하다.

glab/gh 같은 CLI 를 쓰지 않고 직접 호출한다. 상태 코드를 그대로 봐야 하기 때문이다.
인라인 거부는 GitLab 400, GitHub 422 이고, GitHub 은 같은 자리에서 레이트 리밋 403 도 낸다.
CLI 는 이걸 종료 코드 하나로 뭉갠다.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API_VERSION = "2022-11-28"


class NotOnDiff(Exception):
    """지적한 라인이 diff 에 없다. 인라인 대신 요약으로 내린다."""


class Forge:
    name = ""
    change_request = ""
    # 게시 요청 사이 최소 간격(초). GitHub 은 연속 생성에 2차 레이트 리밋을 건다.
    mutation_interval = 0.0

    def __init__(self, token):
        self.token = token
        self._last_mutation = 0.0

    def request(self, path, method="GET", payload=None):
        if method != "GET":
            self._throttle()
        headers = dict(self.headers())
        data = None
        if payload is not None:
            data, headers["Content-Type"] = self.encode(payload)
        request = urllib.request.Request(
            self.url(path), data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(request) as response:
            body = response.read()
        return json.loads(body) if body else None

    def _throttle(self):
        if not self.mutation_interval:
            return
        gap = self.mutation_interval - (time.monotonic() - self._last_mutation)
        if gap > 0:
            time.sleep(gap)
        self._last_mutation = time.monotonic()


class GitLabForge(Forge):
    name = "gitlab"
    change_request = "MR"

    @classmethod
    def detect(cls):
        if not os.environ.get("CI_MERGE_REQUEST_IID"):
            return None
        return cls(os.environ.get("PI_GITLAB_TOKEN"))

    def __init__(self, token):
        super().__init__(token)
        project = urllib.parse.quote(os.environ["CI_PROJECT_ID"], safe="")
        self.base = f"/projects/{project}/merge_requests/{os.environ['CI_MERGE_REQUEST_IID']}"
        self.position = None

    def url(self, path):
        return f"{os.environ['CI_API_V4_URL']}{path}"

    def headers(self):
        return {"PRIVATE-TOKEN": self.token}

    def encode(self, payload):
        body = urllib.parse.urlencode(payload, doseq=True).encode()
        return body, "application/x-www-form-urlencoded"

    def diff_range(self):
        base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
        return f"{base}...HEAD" if base else None

    def prepare(self):
        versions = self.request(f"{self.base}/versions")
        if not versions:
            raise RuntimeError("MR 버전 정보를 가져오지 못했습니다.")
        latest = versions[0]
        self.position = {
            "position[base_sha]": latest["base_commit_sha"],
            "position[start_sha]": latest["start_commit_sha"],
            "position[head_sha]": latest["head_commit_sha"],
            "position[position_type]": "text",
        }

    def clear_previous(self, marker):
        """훑는 중에 지우면 다음 페이지가 밀려 마지막 묶음을 통째로 건너뛴다.

        대상을 먼저 다 모은 뒤에 지운다.
        """
        author_id = self.request("/user")["id"]
        targets = []
        page = 1
        while True:
            discussions = self.request(f"{self.base}/discussions?per_page=100&page={page}")
            if not discussions:
                break
            for discussion in discussions:
                for note in discussion.get("notes", []):
                    if marker not in (note.get("body") or ""):
                        continue
                    if note.get("author", {}).get("id") != author_id:
                        continue
                    if note.get("system"):
                        continue
                    targets.append(note["id"])
            if len(discussions) < 100:
                break
            page += 1

        removed = 0
        for note_id in targets:
            try:
                self.request(f"{self.base}/notes/{note_id}", method="DELETE")
                removed += 1
            except urllib.error.HTTPError as error:
                error.close()
                print(f"  이전 노트 삭제 실패 note={note_id} {error.code}")
        return removed

    def post_inline(self, finding, body):
        payload = {
            "body": body,
            "position[new_path]": finding["file"],
            "position[old_path]": finding["file"],
            "position[new_line]": str(finding["line"]),
            **self.position,
        }
        try:
            self.request(f"{self.base}/discussions", method="POST", payload=payload)
        except urllib.error.HTTPError as error:
            if error.code == 400:
                error.close()
                raise NotOnDiff from None
            raise

    def post_summary(self, body):
        self.request(f"{self.base}/notes", method="POST", payload={"body": body})


class GitHubForge(Forge):
    name = "github"
    change_request = "PR"
    # 리뷰 코멘트 생성은 2차 레이트 리밋 대상이다. 지적 하나당 요청 하나라 간격을 둔다.
    mutation_interval = 1.0

    @classmethod
    def detect(cls):
        if not os.environ.get("GITHUB_ACTIONS"):
            return None
        return cls(os.environ.get("GITHUB_TOKEN"))

    def __init__(self, token):
        super().__init__(token)
        self.repo = os.environ["GITHUB_REPOSITORY"]
        pull = _event().get("pull_request") or {}
        self.number = pull.get("number")
        self.head_sha = (pull.get("head") or {}).get("sha")
        self.base_sha = (pull.get("base") or {}).get("sha")

    def url(self, path):
        root = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        return f"{root}{path}"

    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def encode(self, payload):
        return json.dumps(payload).encode(), "application/json"

    def diff_range(self):
        """GitLab 의 CI_MERGE_REQUEST_DIFF_BASE_SHA 는 머지 베이스지만
        GitHub 의 pull_request.base.sha 는 베이스 브랜치의 현재 끝이다.

        git diff 는 세 점 표기가 알아서 머지 베이스를 잡지만 git log 는 대칭 차집합이
        되어 베이스 쪽 커밋까지 딸려 나온다. 여기서 정규화해 양쪽 의미를 맞춘다.
        """
        if not self.base_sha:
            return None
        found = subprocess.run(
            ["git", "merge-base", self.base_sha, "HEAD"],
            capture_output=True,
            text=True,
        )
        base = found.stdout.strip() if found.returncode == 0 else self.base_sha
        return f"{base}...HEAD"

    def prepare(self):
        if not self.number or not self.head_sha:
            raise RuntimeError("GITHUB_EVENT_PATH 에서 PR 번호와 head sha 를 찾지 못했습니다.")

    def clear_previous(self, marker):
        """GitLab 과 달리 작성자로 미리 거를 수 없다.

        GITHUB_TOKEN 은 설치 토큰이라 GET /user 가 막혀 있다. 마커로만 고르고,
        남의 코멘트면 삭제가 403 으로 떨어지는 것을 그대로 받아 넘긴다.
        """
        pulls = f"/repos/{self.repo}/pulls"
        issues = f"/repos/{self.repo}/issues"
        removed = 0
        for listing, delete in (
            (f"{pulls}/{self.number}/comments", f"{pulls}/comments"),
            (f"{issues}/{self.number}/comments", f"{issues}/comments"),
        ):
            for comment_id in self._marked(listing, marker):
                try:
                    self.request(f"{delete}/{comment_id}", method="DELETE")
                    removed += 1
                except urllib.error.HTTPError as error:
                    error.close()
                    print(f"  이전 코멘트 삭제 실패 id={comment_id} {error.code}")
        return removed

    def _marked(self, listing, marker):
        targets = []
        page = 1
        while True:
            comments = self.request(f"{listing}?per_page=100&page={page}")
            if not comments:
                break
            targets += [c["id"] for c in comments if marker in (c.get("body") or "")]
            if len(comments) < 100:
                break
            page += 1
        return targets

    def post_inline(self, finding, body):
        payload = {
            "body": body,
            "commit_id": self.head_sha,
            "path": finding["file"],
            "line": finding["line"],
            "side": "RIGHT",
        }
        try:
            self.request(
                f"/repos/{self.repo}/pulls/{self.number}/comments",
                method="POST",
                payload=payload,
            )
        except urllib.error.HTTPError as error:
            if error.code == 422:
                error.close()
                raise NotOnDiff from None
            raise

    def post_summary(self, body):
        self.request(
            f"/repos/{self.repo}/issues/{self.number}/comments",
            method="POST",
            payload={"body": body},
        )


FORGES = (GitLabForge, GitHubForge)


def detect_forge():
    """환경변수로 어느 포지인지 고른다. MEERKIT_FORGE 로 강제할 수 있다."""
    forced = os.environ.get("MEERKIT_FORGE")
    if forced:
        for forge in FORGES:
            if forge.name == forced:
                return forge(os.environ.get(TOKEN_VARS[forced]))
        raise RuntimeError(f"모르는 MEERKIT_FORGE 값: {forced}")
    for forge in FORGES:
        found = forge.detect()
        if found:
            return found
    return None


TOKEN_VARS = {"gitlab": "PI_GITLAB_TOKEN", "github": "GITHUB_TOKEN"}


def _event():
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
