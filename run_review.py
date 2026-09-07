#!/usr/bin/env python3
"""MR/PR 변경분을 pi 로 리뷰하고 결과를 인라인 코멘트로 게시한다.

GitLab CI 의 merge_request_event 파이프라인이나 GitHub Actions 의 pull_request
워크플로에서 실행된다. 실행 중인 플랫폼의 종류는 forge.py 가 환경변수를 바탕으로 판별한다.

리뷰 로직은 컨테이너 이미지 내부에 고정되어 있으므로, 리뷰 대상 저장소의 체크아웃 상태나
임의의 변경에 영향을 받지 않는다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_DIR = SCRIPT_DIR / "prompt"
sys.path.insert(0, str(SCRIPT_DIR))

from diff_limits import (  # noqa: F401, E402
    IGNORED_DIFF_PATTERNS,
    calculate_diff_size,
    generate_oversize_report,
    get_size_limits,
    is_ignored_diff_file,
)
from forge import detect_forge  # noqa: E402
from meridian_runner import (  # noqa: F401, E402
    CREDENTIAL_VARS,
    MERIDIAN_LOG_PATH,
    dump_meridian_log,
    ensure_machine_id,
    fetch_oauth_usage,
    is_meridian_ready,
    parse_oauth_tokens,
    print_token_usage_summary,
    report_credentials,
    run_meridian,
    select_and_order_profiles,
    setup_meridian_profiles,
    wait_for_meridian,
)


def load_korean_guideline() -> str:
    """한국어 지침 파일에서 상단 주석을 제외한 본문을 읽어온다."""
    text = (PROMPT_DIR / "fluent-korean.md").read_text(encoding="utf-8")
    if "-->" in text:
        text = text.split("-->", 1)[1].strip()
    return text


def build_prompt(diff_range: str, output_json: str, change_request: str) -> str:
    # Team 플랜 환경에서 clientSystemPrompt 가 비활성화되어도 지침이 누락되지 않도록,
    # 유저 프롬프트 본문 뒤에 한국어 작성 지침을 직접 결합한다.
    template = (PROMPT_DIR / "review.md").read_text(encoding="utf-8")
    korean_guideline = load_korean_guideline()
    prompt = (
        template.replace("__DIFF_RANGE__", diff_range)
        .replace("__OUTPUT_JSON__", output_json)
        .replace("__CR__", change_request)
    )
    return f"{prompt}\n\n---\n\n## 한국어 문장 작성 지침\n\n{korean_guideline}"


def pi_command(prompt: str) -> list[str]:
    command = [
        "pi", "-p", "--no-session", "--approve",
    ]
    model = os.environ.get("MEERKIT_MODEL")
    if model:
        command += ["--model", model]
    thinking = os.environ.get("MEERKIT_THINKING")
    if thinking:
        command += ["--thinking", thinking]
    return command + ["--", prompt]


def resolve_range(forge):
    """외부 시험 실행 시 리뷰 기준 커밋을 수동으로 지정하기 위한 MEERKIT_DIFF_BASE 를 확인한다."""
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

    print(f"호스팅 플랫폼: {forge.name if forge else '없음 (로컬 시험 실행)'}", flush=True)
    print(f"리뷰 범위: {diff_range}", flush=True)
    subprocess.run(["git", "--no-pager", "diff", "--stat", diff_range], check=True)

    raw_tokens = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKENS")
    )
    token_pairs = parse_oauth_tokens(raw_tokens) if raw_tokens else []
    strategy = os.environ.get("MEERKIT_PROFILE_STRATEGY", "low_usage")
    pinned = os.environ.get("MEERKIT_PROFILE")
    active_profile, ordered_pairs = setup_meridian_profiles(
        token_pairs, strategy=strategy, pinned_name=pinned
    )
    report_credentials(token_pairs, active_profile, strategy=strategy)

    report = Path(output_json)
    report.unlink(missing_ok=True)

    # 자동 생성 파일(락 파일 등)을 제외한 순수 변경 규모를 측정한다.
    total_lines, total_files = calculate_diff_size(diff_range)
    max_lines, max_files = get_size_limits()
    is_oversized = (max_lines > 0 and total_lines > max_lines) or (
        max_files > 0 and total_files > max_files
    )

    if is_oversized:
        print(
            f"변경 규모 초과 감지: {total_lines:,}줄, {total_files:,}개 파일 "
            f"(상한: {max_lines:,}줄, {max_files:,}개 파일) -> 자동 리뷰 생략 (토큰 소모 방지)",
            flush=True,
        )
        report_data = generate_oversize_report(
            total_lines, total_files, max_lines, max_files
        )
        report.write_text(
            json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"--- {output_json} ---")
        print(report.read_text(encoding="utf-8"))
        import post_review

        return post_review.main()

    # 호스팅 플랫폼이 감지되지 않는 로컬 실행 환경에서는 프롬프트의 기본 용어로 MR 을 사용한다.
    change_request = forge.change_request if forge else "MR"
    prompt = build_prompt(diff_range, output_json, change_request)

    with run_meridian(ordered_pairs=ordered_pairs):
        exit_code = subprocess.run(pi_command(prompt), env=os.environ).returncode
        print_token_usage_summary(active_pair=ordered_pairs[0] if ordered_pairs else None)
        if exit_code != 0:
            dump_meridian_log()
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
