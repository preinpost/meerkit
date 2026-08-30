#!/usr/bin/env python3
"""Meerkit 리뷰 결과(JSON)를 MR/PR 코멘트로 게시한다.

라인에 붙일 수 없는 지적은 요약으로 모아서 올린다.
재실행 시 이전에 남긴 Meerkit 코멘트를 먼저 지워 중복을 막는다.

플랫폼별 REST 호출은 forge.py 에 있다. 여기에는 본문 렌더와 순서만 남는다.

필요 환경 변수:
  GitLab  CI_API_V4_URL, CI_PROJECT_ID, CI_MERGE_REQUEST_IID (CI 기본 제공)
          PI_GITLAB_TOKEN (api 스코프 토큰)
  GitHub  GITHUB_REPOSITORY, GITHUB_EVENT_PATH (Actions 기본 제공)
          GITHUB_TOKEN (pull-requests: write 권한)

토큰이 없으면 게시를 건너뛰고 JSON 아티팩트만 남긴다.

PI_GITLAB_TOKEN 발급:
  CI_JOB_TOKEN 으로는 MR 노트를 작성할 수 없어 별도 토큰이 필요하다.
  리뷰 대상 프로젝트 Settings > Access tokens > Add new token
    Name   meerkit
    Role   Developer
    Scopes api
  발급값을 Settings > CI/CD > Variables 에 PI_GITLAB_TOKEN 으로 등록한다.
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
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
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
    """변경 후 파일 기준으로 실제 바뀐 라인을 파일별로 모은다.

    diff 밖 라인을 지적하면 GitLab 은 400, GitHub 은 422 로 거부한다. GitHub 의 거부는
    레이트 리밋·권한 문제와 같은 자리에서 나 구분이 어려우므로, 보내기 전에 걸러
    그 상황 자체를 줄인다.

    범위를 모르거나 저장소 밖에서 돌면 None 을 돌려 거르지 않는다.
    """
    if not diff_range:
        return None
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--unified=0", diff_range],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    changed = {}
    path = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target.split("/", 1)[-1]
            continue
        if not path:
            continue
        match = HUNK.match(line)
        if match:
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            changed.setdefault(path, set()).update(range(start, start + count))
    return changed


def on_diff(finding, changed):
    if not finding.get("file") or not finding.get("line"):
        return False
    if changed is None:
        return True
    return finding["line"] in changed.get(finding["file"], ())


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
        body += ["", "### 라인에 달지 않은 지적", ""]
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

    inline, unpositioned = split_findings(findings, diff_lines(forge.diff_range()))
    unplaced = sum(1 for f in findings if not f.get("file") or not f.get("line"))
    if len(unpositioned) > unplaced:
        print(f"diff 밖 라인이라 인라인에서 제외: {len(unpositioned) - unplaced}건")

    posted = 0
    failed = False
    for finding in inline:
        try:
            forge.post_inline(finding, render_body(finding))
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
