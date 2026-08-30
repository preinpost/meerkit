"""테스트 공용 도구."""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock as mocklib

TESTS_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(TESTS_DIR), str(TESTS_DIR.parent)]

from mock_forge import BASE_SHA, HEAD_SHA  # noqa: E402

import post_review  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "sample.json"
SIGNUP = "src/auth.py"
MAIL = "src/notify.py"
OUT_OF_DIFF = [(SIGNUP, 9999)]


def run_post(env):
    """환경변수를 통째로 갈아끼우고 post_review.main() 을 돌린다.

    clear=True 로 실제 CI 변수가 새어 들어오지 않게 한다.
    """
    with (
        mocklib.patch.dict(os.environ, env, clear=True),
        contextlib.redirect_stdout(io.StringIO()) as out,
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        code = post_review.main()
    return code, out.getvalue() + err.getvalue()


def gitlab_env(server, token="glpat-test"):
    env = {
        "CI_API_V4_URL": server.api_url,
        "CI_PROJECT_ID": "group/proj",
        "CI_MERGE_REQUEST_IID": "7",
        "MEERKIT_JSON": str(FIXTURE),
        "PATH": os.environ.get("PATH", ""),
    }
    if token:
        env["PI_GITLAB_TOKEN"] = token
    return env


def github_env(server, event_path, token="ghs-test"):
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_API_URL": server.api_url,
        "GITHUB_REPOSITORY": "acme/widget",
        "GITHUB_EVENT_PATH": event_path,
        "MEERKIT_JSON": str(FIXTURE),
        "PATH": os.environ.get("PATH", ""),
    }
    if token:
        env["GITHUB_TOKEN"] = token
    return env


def write_event(directory, number=7):
    """GitHub Actions 가 넣어주는 이벤트 페이로드를 흉내낸다."""
    path = Path(directory) / "event.json"
    path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": number,
                    "head": {"sha": HEAD_SHA},
                    "base": {"sha": BASE_SHA},
                }
            }
        ),
        encoding="utf-8",
    )
    return str(path)
