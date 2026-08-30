#!/usr/bin/env python3
"""MR/PR 변경분을 pi 로 리뷰하고 결과를 인라인 코멘트로 게시한다.

GitLab CI 의 merge_request_event 파이프라인이나 GitHub Actions 의 pull_request
워크플로에서 실행된다. 어느 쪽인지는 forge.py 가 환경변수로 판별한다.

리뷰 로직은 이미지 안에 고정되어 있어 리뷰 대상 저장소의 체크아웃과 무관하다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_DIR = SCRIPT_DIR / "prompt"
sys.path.insert(0, str(SCRIPT_DIR))

from forge import detect_forge  # noqa: E402

CREDENTIAL_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "PI_GITLAB_TOKEN",
    "GITHUB_TOKEN",
)


def report_credentials():
    """브리지가 Claude Code 의 에러를 삼켜 인증 실패와 그 외를 구분할 수 없다.

    값은 찍지 않고 주입 여부만 남긴다.
    """
    for name in CREDENTIAL_VARS:
        value = os.environ.get(name)
        print(f"{name}: set ({len(value)} chars)" if value else f"{name}: unset")


def build_prompt(diff_range, output_json, change_request):
    template = (PROMPT_DIR / "review.md").read_text(encoding="utf-8")
    return (
        template.replace("__DIFF_RANGE__", diff_range)
        .replace("__OUTPUT_JSON__", output_json)
        .replace("__CR__", change_request)
    )


def dump_bridge_log():
    log = Path(os.environ.get("HOME", "/root")) / ".pi/agent/claude-bridge.log"
    print("--- claude-bridge.log (tail) ---", file=sys.stderr)
    if not log.exists():
        print("(로그 없음)", file=sys.stderr)
        return
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-50:]), file=sys.stderr)


def pi_command(prompt):
    # 리뷰 본문은 사람이 읽는 한국어다. 모델이 기본적으로 쓰는 압축된 번역체를 막는다.
    command = [
        "pi", "-p", "--no-session", "--approve",
        "--append-system-prompt", str(PROMPT_DIR / "fluent-korean.md"),
    ]
    model = os.environ.get("MEERKIT_MODEL")
    if model:
        command += ["--model", model]
    thinking = os.environ.get("MEERKIT_THINKING")
    if thinking:
        command += ["--thinking", thinking]
    return command + ["--", prompt]


def resolve_range(forge):
    """MEERKIT_DIFF_BASE 는 CI 밖에서 시험 실행할 때 쓰는 수동 지정이다."""
    base = os.environ.get("MEERKIT_DIFF_BASE")
    if base:
        return f"{base}...HEAD"
    return forge.diff_range() if forge else None


def main():
    forge = detect_forge()
    diff_range = resolve_range(forge)
    if not diff_range:
        sys.exit(
            "리뷰 범위를 찾지 못했습니다. MR/PR 파이프라인이 아니거나 "
            "얕은 클론이라 베이스 커밋이 없습니다."
        )

    output_json = os.environ.get("MEERKIT_JSON", "meerkit.json")

    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", os.getcwd()], check=True
    )

    print(f"포지: {forge.name if forge else '없음 (로컬 시험 실행)'}", flush=True)
    print(f"리뷰 범위: {diff_range}", flush=True)
    subprocess.run(["git", "--no-pager", "diff", "--stat", diff_range], check=True)
    report_credentials()

    report = Path(output_json)
    report.unlink(missing_ok=True)

    # 포지가 없으면 프롬프트 용어를 MR 로 둔다. 어차피 게시하지 않는다.
    change_request = forge.change_request if forge else "MR"
    prompt = build_prompt(diff_range, output_json, change_request)

    env = {**os.environ, "CLAUDE_BRIDGE_DEBUG": "1"}
    if subprocess.run(pi_command(prompt), env=env).returncode != 0:
        dump_bridge_log()
        return 1

    if not report.exists():
        print(f"{output_json} 이 생성되지 않았습니다.", file=sys.stderr)
        return 1

    raw = report.read_text(encoding="utf-8")
    try:
        json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"{output_json} 이 올바른 JSON 이 아닙니다: {error}", file=sys.stderr)
        print(raw[:2000], file=sys.stderr)
        return 1

    print(f"--- {output_json} ---")
    print(raw)

    import post_review

    return post_review.main()


if __name__ == "__main__":
    sys.exit(main())
