"""Meridian 프록시 데몬 및 다중 OAuth 프로필 관리 모듈.

Meridian 데몬 수명주기(기동, 헬스체크, 종료), machine-id 생성,
다중 OAuth 토큰 파싱, 실시간 low_usage 사용량 분석, 프로필 정렬 및 등록을 담당한다.
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

CREDENTIAL_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKENS",
    "ANTHROPIC_API_KEY",
    "GITLAB_TOKEN",
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
    strategy: str = "low_usage",
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
        if len(pairs) > 1:
            if strategy in ("low_usage", "low-usage", "usage", "least-used"):
                info = f"[active: {active_profile} (low-usage quota)]"
            elif strategy == "random":
                info = f"[active: {active_profile} (random load balancing)]"
            else:
                info = f"[active: {active_profile}]"
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

    for name in ("ANTHROPIC_API_KEY", "GITLAB_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if name == "GITLAB_TOKEN" and not value and os.environ.get("PI_GITLAB_TOKEN"):
            value = os.environ.get("PI_GITLAB_TOKEN")
            name = "GITLAB_TOKEN (PI_GITLAB_TOKEN 호환)"
        print(f"{name}: set ({len(value)} chars)" if value else f"{name}: unset")


def fetch_oauth_usage(token: str, timeout: float = 3.0) -> float | None:
    """Anthropic OAuth 엔드포인트에서 5시간 윈도우 사용률(0.0 ~ 1.0)을 조회한다.

    조회 실패 또는 Rate Limit 발생 시 None 을 반환한다.
    """
    url = "https://api.anthropic.com/api/oauth/usage"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "meerkit",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            windows = data.get("windows") or []
            for w in windows:
                if w.get("type") == "five_hour" and w.get("utilization") is not None:
                    return float(w["utilization"])
            for w in windows:
                if w.get("type") == "seven_day" and w.get("utilization") is not None:
                    return float(w["utilization"])
            return 0.0
    except Exception:
        return None


def select_and_order_profiles(
    token_pairs: list[tuple[str, str]],
    strategy: str = "low_usage",
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

    # 사용량이 가장 여유 있는 계정을 우선 선택하는 정책 (기본값)
    if strategy in ("low_usage", "low-usage", "usage", "least-used"):
        scored_pairs = []
        for name, token in pairs:
            util = fetch_oauth_usage(token)
            # 조회 실패 시 패널티(1.5)를 부여하여 정상 조회된 계정보다 후순위로 배치한다.
            score = util if util is not None else 1.5
            scored_pairs.append((score, name, token, util))

        scored_pairs.sort(key=lambda x: x[0])
        _, best_name, _, best_util = scored_pairs[0]
        if best_util is not None:
            print(
                f"OAuth 계정 사용량 분석 완료: '{best_name}' 선택됨 "
                f"(5시간 사용률: {best_util * 100:.1f}%)",
                flush=True,
            )
        return [(name, token) for _, name, token, _ in scored_pairs]

    # 무작위 분산 정책인 경우 시작 프로필을 랜덤하게 선택하고 순환 배치한다.
    if strategy == "random":
        import secrets

        chosen_idx = secrets.randbelow(len(pairs))
        return pairs[chosen_idx:] + pairs[:chosen_idx]

    return pairs


def setup_meridian_profiles(
    token_pairs: list[tuple[str, str]],
    strategy: str = "low_usage",
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


def fetch_telemetry_summary(
    url: str = "http://127.0.0.1:3456/telemetry/summary", timeout: float = 2.0
) -> dict | None:
    """Meridian 텔레메트리 엔드포인트에서 잡 수행 중 발생한 토큰 사용량 요약을 조회한다."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "meerkit"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def print_token_usage_summary(summary: dict | None = None):
    """수집된 토큰 사용량과 프롬프트 캐시 적중률 통계를 콘솔에 정갈하게 출력한다."""
    data = summary or fetch_telemetry_summary()
    if not data:
        return

    token_usage = data.get("tokenUsage") or {}
    cost_est = data.get("costEstimate") or {}

    total_in = token_usage.get("totalInputTokens", 0)
    total_out = token_usage.get("totalOutputTokens", 0)
    cache_read = token_usage.get("totalCacheReadTokens", 0)
    cache_write = token_usage.get("totalCacheCreationTokens", 0)
    cache_rate = token_usage.get("avgCacheHitRate", 0.0)
    cost_usd = cost_est.get("totalUsd", 0.0)

    if total_in == 0 and total_out == 0:
        return

    print("\n--- 토큰 사용량 및 프롬프트 캐시 요약 ---", flush=True)
    print(
        f"• 입력 토큰: {total_in:,}줄 (캐시 적중: {cache_read:,}, 신규 생성: {cache_write:,})",
        flush=True,
    )
    print(f"• 출력 토큰: {total_out:,}", flush=True)
    print(f"• 프롬프트 캐시 적중률: {cache_rate * 100:.1f}%", flush=True)
    if cost_usd > 0:
        print(f"• 추정 비용: ${cost_usd:.4f}", flush=True)
    print("------------------------------------------\n", flush=True)


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
        strategy = os.environ.get("MEERKIT_PROFILE_STRATEGY", "low_usage")
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
