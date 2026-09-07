#!/usr/bin/env python3
"""MR/PR 변경분을 pi 로 리뷰하고 결과를 인라인 코멘트로 게시한다.

GitLab CI 의 merge_request_event 파이프라인이나 GitHub Actions 의 pull_request
워크플로에서 실행된다. 실행 중인 플랫폼의 종류는 forge.py 가 환경변수를 바탕으로 판별한다.

리뷰 로직은 컨테이너 이미지 내부에 고정되어 있으므로, 리뷰 대상 저장소의 체크아웃 상태나
임의의 변경에 영향을 받지 않는다.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
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
MERIDIAN_LOG_PATH = Path("/tmp/meridian.log")


def ensure_machine_id():
    """Linux 환경에서 Meridian 프로세스 소유권 추적을 위한 machine-id 를 보장한다."""
    if sys.platform != "linux":
        return

    machine_id_path = Path("/etc/machine-id")
    dbus_id_path = Path("/var/lib/dbus/machine-id")
    for path in (machine_id_path, dbus_id_path):
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-fA-F]{32}", content):
                    return
            except OSError:
                pass

    # 유효한 32자리 16진수 machine-id 가 없으면 새로 생성하여 기록한다.
    import secrets

    new_id = f"{secrets.token_hex(16)}\n"
    try:
        machine_id_path.write_text(new_id, encoding="utf-8")
        machine_id_path.chmod(0o444)
    except OSError:
        pass


def parse_oauth_tokens(raw_value: str) -> list[tuple[str, str]]:
    """환경변수로 주입된 문자열에서 하나 이상의 OAuth 토큰이나 (프로필명, 토큰) 쌍을 추출한다.

    지원하는 형식:
    1. 단일 토큰: 'sk-ant-oat01-...'
    2. 구분자(콤마, 줄바꿈)로 나열된 토큰 목록: 'sk-ant-oat01-a, sk-ant-oat01-b'
    3. 명시적 이름 쌍: 'main:sk-ant-oat01-a, sub:sk-ant-oat01-b'
    4. JSON 배열 또는 객체:
       - '["sk-ant-oat01-a", "sk-ant-oat01-b"]'
       - '{"main": "sk-ant-oat01-a", "sub": "sk-ant-oat01-b"}'
    """
    raw = raw_value.strip()
    if not raw:
        return []

    # JSON 형식으로 전달된 문자열인지 확인하고 파싱을 시도한다.
    if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith("[") and raw.endswith("]")):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return [
                    (str(k).strip(), str(v).strip())
                    for k, v in parsed.items()
                    if str(v).strip()
                ]
            if isinstance(parsed, list):
                result = []
                for idx, item in enumerate(parsed):
                    if isinstance(item, dict) and "id" in item and "oauthToken" in item:
                        result.append((str(item["id"]).strip(), str(item["oauthToken"]).strip()))
                    elif isinstance(item, str) and item.strip():
                        name = "default" if idx == 0 else f"profile-{idx}"
                        result.append((name, item.strip()))
                if result:
                    return result
        except json.JSONDecodeError:
            pass

    # 줄바꿈이나 콤마를 구분자로 사용하여 토큰 목록을 분리한다.
    tokens = [t.strip() for t in re.split(r"[\n,]+", raw) if t.strip()]
    result = []
    for idx, token in enumerate(tokens):
        # 'name:sk-ant-oat01-...' 형식인 경우 콜론 앞부분을 프로필 식별자로 지정한다.
        if ":" in token and not token.startswith("sk-"):
            name, tok = token.split(":", 1)
            result.append((name.strip(), tok.strip()))
        else:
            name = "default" if idx == 0 else f"profile-{idx}"
            result.append((name, token))
    return result


def report_credentials(
    token_pairs: list[tuple[str, str]] | None = None,
    active_profile: str | None = None,
    strategy: str = "random",
):
    """인증에 필요한 자격증명 값은 노출하지 않고 주입 여부만 기록한다."""
    pairs = token_pairs
    if pairs is None:
        raw_tokens = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKENS")
        )
        pairs = parse_oauth_tokens(raw_tokens) if raw_tokens else []

    if pairs:
        names = [p[0] for p in pairs]
        if len(pairs) > 1 and strategy == "random":
            info = f"[active: {active_profile} (random load balancing)]"
        else:
            info = f"[default: {active_profile or names[0]}]"
        print(
            f"CLAUDE_CODE_OAUTH_TOKEN: {len(pairs)} profile(s) configured "
            f"({', '.join(names)}) {info}"
        )
    else:
        raw = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKENS")
        )
        print(
            "CLAUDE_CODE_OAUTH_TOKEN: set (empty tokens)"
            if raw
            else "CLAUDE_CODE_OAUTH_TOKEN: unset"
        )

    for name in ("ANTHROPIC_API_KEY", "PI_GITLAB_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        print(f"{name}: set ({len(value)} chars)" if value else f"{name}: unset")


def select_and_order_profiles(
    token_pairs: list[tuple[str, str]],
    strategy: str = "random",
    pinned_name: str | None = None,
) -> list[tuple[str, str]]:
    """로드 밸런싱 정책에 따라 프로필 순서를 조정하고 시작 프로필을 결정한다."""
    if len(token_pairs) <= 1:
        return token_pairs

    pairs = list(token_pairs)

    # 특정 프로필이 명시적으로 지정된 경우 해당 프로필을 맨 앞으로 배치한다.
    if pinned_name:
        for idx, (name, _) in enumerate(pairs):
            if name == pinned_name:
                return pairs[idx:] + pairs[:idx]

    # 무작위 분산 정책인 경우 시작 프로필을 랜덤하게 선택하고 순환 배치한다.
    if strategy == "random":
        import secrets

        chosen_idx = secrets.randbelow(len(pairs))
        return pairs[chosen_idx:] + pairs[:chosen_idx]

    return pairs


def setup_meridian_profiles(
    token_pairs: list[tuple[str, str]],
    strategy: str = "random",
    pinned_name: str | None = None,
) -> tuple[str | None, list[tuple[str, str]]]:
    """Meridian 프로필 목록을 등록하고, 로드 밸런싱 정책에 따라 기본 프로필을 지정한다."""
    if not token_pairs:
        return None, []

    ordered_pairs = select_and_order_profiles(
        token_pairs, strategy=strategy, pinned_name=pinned_name
    )
    default_profile_id = ordered_pairs[0][0]

    home = Path(os.environ.get("HOME", "/root"))
    config_dir = home / ".config" / "meridian"
    config_dir.mkdir(parents=True, exist_ok=True)

    profiles_file = config_dir / "profiles.json"
    profiles_data = [
        {
            "id": profile_id,
            "type": "oauth-token",
            "oauthToken": token,
        }
        for profile_id, token in ordered_pairs
    ]
    profiles_file.write_text(json.dumps(profiles_data, indent=2), encoding="utf-8")

    # 선택된 프로필을 기본 활성 프로필로 지정한다.
    settings_file = config_dir / "settings.json"
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = {}

    settings["activeProfile"] = default_profile_id
    settings["profileOrder"] = [p[0] for p in ordered_pairs]
    settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    return default_profile_id, ordered_pairs


def is_meridian_ready(url="http://127.0.0.1:3456/health", timeout=1.0) -> bool:
    """Meridian 헬스체크 엔드포인트의 응답 상태를 확인한다."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "meerkit"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_meridian(
    proc: subprocess.Popen,
    log_path: Path,
    url="http://127.0.0.1:3456/health",
    timeout=30.0,
):
    """Meridian 프록시 데몬이 요청을 수신할 준비가 될 때까지 대기한다."""
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Meridian 프로세스가 비정상 종료되었습니다 (종료 코드: {proc.returncode})."
            )
        if is_meridian_ready(url):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Meridian 서버가 {timeout}초 안에 준비되지 않았습니다.")


def dump_meridian_log():
    """오류 원인을 분석하기 위해 Meridian 로그의 마지막 부분을 표준 에러로 출력한다."""
    print(f"--- {MERIDIAN_LOG_PATH} (tail) ---", file=sys.stderr)
    if not MERIDIAN_LOG_PATH.exists():
        print("(로그 파일 없음)", file=sys.stderr)
        return
    lines = MERIDIAN_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-50:]), file=sys.stderr)


@contextmanager
def run_meridian(ordered_pairs: list[tuple[str, str]] | None = None):
    """Meridian 프록시 데몬을 백그라운드로 실행하고 라이프사이클을 안전하게 관리한다."""
    ensure_machine_id()
    health_url = "http://127.0.0.1:3456/health"
    if is_meridian_ready(health_url):
        print("기존 Meridian 프록시(http://127.0.0.1:3456)를 사용합니다.", flush=True)
        yield None
        return

    pairs = ordered_pairs
    if pairs is None:
        raw_tokens = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKENS")
        )
        token_pairs = parse_oauth_tokens(raw_tokens) if raw_tokens else []
        strategy = os.environ.get("MEERKIT_PROFILE_STRATEGY", "random")
        pinned = os.environ.get("MEERKIT_PROFILE")
        _, pairs = setup_meridian_profiles(
            token_pairs, strategy=strategy, pinned_name=pinned
        )

    meridian_env = {**os.environ}
    if pairs:
        # [0]번째 토큰을 주입하여 사전 인증 상태(preflight auth status) 검사를 통과시킨다.
        meridian_env["CLAUDE_CODE_OAUTH_TOKEN"] = pairs[0][1]
        meridian_env["MERIDIAN_DEFAULT_PROFILE"] = pairs[0][0]
        meridian_env["MERIDIAN_PROFILE_ORDER"] = ",".join(p[0] for p in pairs)
        if len(pairs) > 1 and "MERIDIAN_ROUTING" not in meridian_env:
            meridian_env["MERIDIAN_ROUTING"] = "priority"

    MERIDIAN_LOG_PATH.unlink(missing_ok=True)
    print("Meridian 프록시 데몬을 기동합니다...", flush=True)

    with open(MERIDIAN_LOG_PATH, "wb") as log_file:
        proc = subprocess.Popen(
            ["meridian"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=meridian_env,
        )
        try:
            wait_for_meridian(proc, MERIDIAN_LOG_PATH, health_url)
            print("Meridian 프록시가 정상 준비되었습니다.", flush=True)
            yield proc
        except Exception as exc:
            print(f"Meridian 시작 실패: {exc}", file=sys.stderr)
            dump_meridian_log()
            raise
        finally:
            if proc.poll() is None:
                print("Meridian 프록시를 종료합니다...", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


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
    strategy = os.environ.get("MEERKIT_PROFILE_STRATEGY", "random")
    pinned = os.environ.get("MEERKIT_PROFILE")
    active_profile, ordered_pairs = setup_meridian_profiles(
        token_pairs, strategy=strategy, pinned_name=pinned
    )
    report_credentials(token_pairs, active_profile, strategy=strategy)

    report = Path(output_json)
    report.unlink(missing_ok=True)

    # 호스팅 플랫폼이 감지되지 않는 로컬 실행 환경에서는 프롬프트의 기본 용어로 MR 을 사용한다.
    change_request = forge.change_request if forge else "MR"
    prompt = build_prompt(diff_range, output_json, change_request)

    with run_meridian(ordered_pairs=ordered_pairs):
        if subprocess.run(pi_command(prompt), env=os.environ).returncode != 0:
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
