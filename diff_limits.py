"""Git diff 분석 및 대규모 변경 제한 모듈.

락 파일이나 번들 파일 등 기계적으로 대량 생성되는 변경분을 제외하고,
순수 변경 라인 수와 파일 수를 측정하여 상한선 초과 시 조기 종료 리포트를 생성한다.
"""
import os
import re
import subprocess

IGNORED_DIFF_PATTERNS = (
    r"(^|/).*package-lock\.json$",
    r"(^|/).*pnpm-lock\.yaml$",
    r"(^|/).*yarn\.lock$",
    r"(^|/).*bun\.lockb?$",
    r"(^|/).*Cargo\.lock$",
    r"(^|/).*poetry\.lock$",
    r"(^|/).*uv\.lock$",
    r"(^|/).*Pipfile\.lock$",
    r"(^|/).*go\.sum$",
    r"(^|/).*composer\.lock$",
    r"(^|/).*\.min\.(js|css)$",
    r"(^|/).*\.map$",
)


def is_ignored_diff_file(file_path: str) -> bool:
    """락 파일이나 압축 번들 등 기계적으로 대량 생성되는 파일인지 판별한다."""
    normalized = file_path.strip().replace("\\", "/")
    return any(re.search(pattern, normalized) for pattern in IGNORED_DIFF_PATTERNS)


def calculate_diff_size(diff_range: str) -> tuple[int, int]:
    """자동 생성 파일을 제외한 순수 변경 라인 수(추가+삭제)와 파일 수를 계산한다."""
    result = subprocess.run(
        ["git", "diff", "--numstat", diff_range],
        capture_output=True,
        text=True,
        check=True,
    )
    total_lines = 0
    total_files = 0

    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_str, deleted_str, file_path = parts
        if is_ignored_diff_file(file_path):
            continue
        # 바이너리 변경 표시('-')는 라인 수 계산에서 제외한다.
        if added_str == "-" or deleted_str == "-":
            continue
        try:
            added = int(added_str)
            deleted = int(deleted_str)
            total_lines += added + deleted
            total_files += 1
        except ValueError:
            continue

    return total_lines, total_files


def get_size_limits() -> tuple[int, int]:
    """환경변수에서 리뷰 건너뛰기 상한선 설정값(최대 라인 수, 최대 파일 수)을 읽어온다."""
    try:
        max_lines = int(os.environ.get("MEERKIT_MAX_LINES", "1200"))
    except ValueError:
        max_lines = 1200

    try:
        max_files = int(os.environ.get("MEERKIT_MAX_FILES", "40"))
    except ValueError:
        max_files = 40

    return max_lines, max_files


def generate_oversize_report(
    total_lines: int, total_files: int, max_lines: int, max_files: int
) -> dict:
    """변경 규모 초과 시 모델 호출을 건너뛰고 게시할 대체 결과 리포트를 생성한다."""
    exceeded = []
    if max_lines > 0 and total_lines > max_lines:
        exceeded.append(f"라인 수 {total_lines:,}줄 (상한: {max_lines:,}줄)")
    if max_files > 0 and total_files > max_files:
        exceeded.append(f"파일 수 {total_files:,}개 (상한: {max_files:,}개)")

    reason = ", ".join(exceeded)
    summary = (
        f"코드 변경 규모가 자동 리뷰 상한선을 초과하여 세부 코드 리뷰를 생략합니다: {reason}. "
        "효과적인 코드 리뷰와 변경 추적을 위해 검토 가능한 크기로 나누어 주십시오."
    )
    detail = (
        f"자동 생성 파일(락 파일 등)을 제외한 순수 변경 규모가 설정된 상한선을 초과했습니다.\n\n"
        f"- 변경 라인: {total_lines:,}줄 (설정 상한: {max_lines:,}줄)\n"
        f"- 변경 파일: {total_files:,}개 (설정 상한: {max_files:,}개)\n\n"
        "변경 범위가 너무 넓으면 중요한 결함이나 부작용을 놓치기 쉬우므로 모델 호출을 생략합니다."
    )
    finding = {
        "file": None,
        "line": None,
        "severity": "P0",
        "title": f"변경 규모 초과로 자동 리뷰 생략 ({reason})",
        "detail": detail,
        "suggestion": "독립적인 기능 단위나 계층별로 MR/PR 을 분할하여 다시 제출해 주십시오.",
    }
    return {
        "summary": summary,
        "findings": [finding],
    }
