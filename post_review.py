#!/usr/bin/env python3
"""Meerkit 리뷰 결과(JSON)를 MR/PR 코멘트로 게시한다.

라인에 달 수 없는 리뷰는 요약으로 모아서 올린다.
재실행 시 이전에 남긴 Meerkit 코멘트를 먼저 지워 중복을 막는다.

플랫폼별 REST 호출은 forge.py 에 있다. 여기에는 본문 렌더와 순서만 남는다.

필요 환경 변수:
  GitLab  CI_API_V4_URL, CI_PROJECT_ID, CI_MERGE_REQUEST_IID (CI 기본 제공)
          GITLAB_TOKEN 또는 PI_GITLAB_TOKEN (api 스코프 토큰)
  GitHub  GITHUB_REPOSITORY, GITHUB_EVENT_PATH (Actions 기본 제공)
          GITHUB_TOKEN (pull-requests: write 권한)

토큰이 없으면 게시를 건너뛰고 JSON 아티팩트만 남긴다.

GITLAB_TOKEN 발급:
  CI_JOB_TOKEN 으로는 MR 노트를 작성할 수 없어 별도 토큰이 필요하다.
  리뷰 대상 프로젝트 Settings > Access tokens > Add new token
    Name   meerkit
    Role   Developer
    Scopes api
  발급값을 Settings > CI/CD > Variables 에 GITLAB_TOKEN 으로 등록한다.
  (기존의 PI_GITLAB_TOKEN 도 호환성을 위해 동일하게 지원된다.)
  Masked 는 켜고 Protect 는 끈다 — Protect 를 켜면 source/target 브랜치가
  둘 다 protected 일 때만 주입되어 일반 기능 브랜치 MR 에서는 값이 비어 있다.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error

from forge import TOKEN_VARS, NotOnDiff, detect_forge

MARKER = "<!-- meerkit-bot -->"
CODE_SPAN = re.compile(r"(`+[^`]*?`+)")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# diff 에 함께 실을 컨텍스트 줄 수. GitLab 과 GitHub 이 기본으로 그려주는 폭이 3줄이라
# 그 안쪽 라인은 변경되지 않은 줄이어도 코멘트가 붙는다. 더 넓히려면 환경변수로 올린다.
DIFF_CONTEXT = os.environ.get("MEERKIT_DIFF_CONTEXT", "3")
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
SEVERITY_LABEL = {"P0": "**P0**", "P1": "**P1**", "P2": "P2"}


def load_findings(path):
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
    findings = report.get("findings") or []
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))
    return report.get("summary", ""), findings


def escape_tildes(text):
    """한 문단에 물결표가 둘 이상이면 GitLab 과 GitHub 모두 그 사이를 취소선으로 그어버린다.

    `108~118행` 같은 범위 표기가 지적 전체를 지워버리는 사고를 막는다.
    코드 스팬 안은 건드리지 않는다 — `~/.pi` 같은 경로에 백슬래시가 드러나면 안 된다.
    """
    parts = CODE_SPAN.split(text)
    return "".join(
        part if index % 2 else part.replace("~", "\\~")
        for index, part in enumerate(parts)
    )


def render_body(finding):
    severity = SEVERITY_LABEL.get(finding.get("severity"), finding.get("severity", ""))
    lines = [MARKER, f"{severity} · {escape_tildes(finding.get('title', ''))}", ""]
    if finding.get("detail"):
        lines.append(escape_tildes(finding["detail"]))
    if finding.get("suggestion"):
        lines += ["", f"**제안** {escape_tildes(finding['suggestion'])}"]
    return "\n".join(lines)


def diff_lines(diff_range):
    """코멘트를 붙일 수 있는 라인을 파일별로 모은다.

    `{경로: {변경 후 라인: 변경 전 라인}}` 이고, 추가된 라인은 변경 전 번호가 없어 None 이다.
    변경된 라인만이 아니라 hunk 안의 컨텍스트 라인도 담는다. 변경 주변의 기존 코드에 대한
    지적이 흔한데, 그 줄들도 diff 에 그려지므로 인라인으로 붙는다.

    다만 GitLab 은 컨텍스트 라인에 old_line 을 함께 주지 않으면 400 이다. 그래서 라인이
    diff 안인지만 보지 않고 변경 전 번호까지 같이 들고 나온다.

    diff 밖 라인을 지적하면 GitLab 은 400, GitHub 은 422 로 거부한다. GitHub 의 거부는
    레이트 리밋·권한 문제와 같은 자리에서 나 구분이 어려우므로, 보내기 전에 걸러
    그 상황 자체를 줄인다.

    범위를 모르거나 저장소 밖에서 돌면 None 을 돌려 거르지 않는다.
    """
    if not diff_range:
        return None
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            f"--unified={DIFF_CONTEXT}",
            diff_range,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    changed = {}
    path = None
    old_no = new_no = old_left = new_left = 0
    for line in result.stdout.splitlines():
        if old_left <= 0 and new_left <= 0:
            # hunk 밖이다. 파일 헤더와 hunk 헤더만 읽는다.
            if line.startswith("+++ "):
                target = line[4:].strip()
                path = None if target == "/dev/null" else target.split("/", 1)[-1]
                continue
            match = HUNK.match(line)
            if match:
                old_no = int(match.group(1))
                old_left = 1 if match.group(2) is None else int(match.group(2))
                new_no = int(match.group(3))
                new_left = 1 if match.group(4) is None else int(match.group(4))
            continue

        # hunk 안이다. 남은 줄 수를 세며 읽어야 본문에 든 `+++` 같은 줄을 헤더로 오인하지 않는다.
        if line.startswith("+"):
            if path:
                changed.setdefault(path, {})[new_no] = None
            new_no += 1
            new_left -= 1
        elif line.startswith("-"):
            old_no += 1
            old_left -= 1
        elif not line.startswith("\\"):  # "\ No newline at end of file"
            if path:
                changed.setdefault(path, {})[new_no] = old_no
            new_no += 1
            old_no += 1
            new_left -= 1
            old_left -= 1
    return changed


def on_diff(finding, changed):
    if not finding.get("file") or not finding.get("line"):
        return False
    if changed is None:
        return True
    return finding["line"] in changed.get(finding["file"], {})


def old_line_of(finding, changed):
    """컨텍스트 라인이면 변경 전 라인 번호를, 추가된 라인이면 None 을 준다."""
    if changed is None:
        return None
    return changed.get(finding["file"], {}).get(finding["line"])


def split_findings(findings, changed):
    inline, unpositioned = [], []
    for finding in findings:
        (inline if on_diff(finding, changed) else unpositioned).append(finding)
    return inline, unpositioned


def render_summary(forge, summary, findings, unpositioned, posted):
    body = [MARKER, "## Meerkit 코드 리뷰", ""]
    if summary:
        body += [escape_tildes(summary), ""]

    counts = {}
    for finding in findings:
        counts[finding.get("severity")] = counts.get(finding.get("severity"), 0) + 1
    if findings:
        tally = ", ".join(f"{k} {v}건" for k, v in counts.items() if k)
        body.append(f"지적 {len(findings)}건 ({tally}) · 인라인 {posted}건")
    else:
        body.append("지적 사항 없음")

    if unpositioned:
        body += ["", "### 라인에 달 수 없는 리뷰", ""]
        for finding in unpositioned:
            # 파일이 없는 지적은 변경 전체에 대한 것이다(예: 변경 규모).
            location = finding.get("file") or f"{forge.change_request} 전체"
            if finding.get("file") and finding.get("line"):
                location = f"{location}:{finding['line']}"
            severity = SEVERITY_LABEL.get(finding.get("severity"), "")
            body.append(f"- `{location}` · {severity} · {escape_tildes(finding.get('title', ''))}")
            if finding.get("detail"):
                body.append(f"  {escape_tildes(finding['detail'])}")
    return "\n".join(body)


def main():
    report_path = os.environ.get("MEERKIT_JSON", "meerkit.json")

    forge = detect_forge()
    if forge is None:
        print("MR/PR 환경이 아니어서 게시를 건너뜁니다.")
        return 0
    if not forge.token:
        print(f"{TOKEN_VARS[forge.name]} 이 없어 게시를 건너뜁니다.")
        return 0
    if not os.path.exists(report_path):
        print(f"{report_path} 이 없어 게시할 내용이 없습니다.", file=sys.stderr)
        return 1

    summary, findings = load_findings(report_path)
    forge.prepare()

    print(f"이전 Meerkit 코멘트 {forge.clear_previous(MARKER)}건 정리")

    changed = diff_lines(forge.diff_range())
    inline, unpositioned = split_findings(findings, changed)
    unplaced = sum(1 for f in findings if not f.get("file") or not f.get("line"))
    if len(unpositioned) > unplaced:
        print(f"diff 밖 라인이라 인라인에서 제외: {len(unpositioned) - unplaced}건")

    posted = 0
    failed = False
    for finding in inline:
        try:
            forge.post_inline(finding, render_body(finding), old_line_of(finding, changed))
            posted += 1
        except NotOnDiff:
            print(f"  인라인 거부 {finding['file']}:{finding['line']} → 요약으로 이동")
            unpositioned.append(finding)
        except urllib.error.HTTPError as error:
            # 레이트 리밋이나 권한 문제다. diff 밖 라인과 섞이면 안 되므로 따로 남긴다.
            error.close()
            failed = True
            print(
                f"  인라인 게시 실패 {finding['file']}:{finding['line']} HTTP {error.code}",
                file=sys.stderr,
            )
            unpositioned.append(finding)

    forge.post_summary(render_summary(forge, summary, findings, unpositioned, posted))
    print(f"게시 완료: 인라인 {posted}건, 요약 이동 {len(unpositioned)}건")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
