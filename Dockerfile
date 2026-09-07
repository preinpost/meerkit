# Meerkit: pi 기반의 MR/PR 코드 리뷰 자동화를 수행하는 CI 이미지이다.
# pi 본체와 meridian 프록시, 관련 의존성을 이미지에 미리 구워둠으로써,
# 매 파이프라인 실행마다 npm 패키지나 런타임을 새로 설치하는 지연 시간을 방지한다.
# 이미지 빌드와 푸시 작업은 Taskfile 을 활용하여 로컬 개발 환경에서 수동으로 수행한다.
#
# 레이어 배치 순서: 아래로 내려갈수록 변경 빈도가 높은 명령어를 배치한다.
# 특히 스크립트와 프롬프트를 복사(COPY)하는 단계를 가장 마지막에 배치해야,
# 프롬프트 내용만 수정했을 때 약 220MB 크기의 claude 바이너리가 다시 다운로드되는 현상을 방지할 수 있다.
ARG UV_VERSION=0.12.5
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM node:22-bookworm-slim

ARG PI_VERSION=0.84.3
ARG MERIDIAN_VERSION=1.68.0
# Debian bookworm 의 기본 apt 저장소가 제공하는 python3 은 3.11 버전에 묶여 있어서,
# 로컬 개발 환경(3.14)과 버전 차이가 발생하여 런타임 불일치 문제가 생길 수 있다.
# 따라서 uv 를 활용하여 로컬과 동일한 Python 버전을 설치하고 일관성을 유지한다.
# 패치 버전까지 고정하여 관리하므로, 버전을 변경할 때는 .python-version 파일과 함께 수정해야 한다.
ARG PYTHON_VERSION=3.14.7
ARG PI_DEFAULT_PROVIDER=meridian
ARG PI_DEFAULT_MODEL=claude-opus-5

# Claude Code 바이너리는 root 권한 환경에서 --dangerously-skip-permissions 플래그의 사용을 제한한다.
# CI 잡 컨테이너는 매번 새롭게 격리된 상태로 실행된 뒤 폐기되는 환경이므로,
# 바이너리가 공식적으로 제공하는 IS_SANDBOX=1 환경변수를 지정하여 해당 제한을 우회한다.
# 일반 사용자로 전환하지 않는 이유는, GitLab Runner 등의 헬퍼 프로세스가
# 소스 체크아웃 경로(/builds)를 root 소유권으로 생성하여 파일 쓰기 권한 오류가 발생하기 때문이다.
ENV PI_SKIP_VERSION_CHECK=1 \
    GIT_TERMINAL_PROMPT=0 \
    IS_SANDBOX=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/*

# Meridian 프록시는 세션 락 및 소유권 격리(Process Incarnation)를 위해 유효한 32자리 16진수 machine-id 를 요구한다.
# 컨테이너 환경에서는 /etc/machine-id 가 비어 있거나 누락되어 500 오류가 발생하므로 이를 사전에 생성한다.
RUN node -e "process.stdout.write(require('node:crypto').randomBytes(16).toString('hex') + '\n')" > /etc/machine-id \
 && chmod 0444 /etc/machine-id

# 리뷰 게시 단계에서는 표준 라이브러리의 urllib 만 사용하므로 별도의 서드파티 패키지는 설치하지 않는다.
# 가상환경의 bin 디렉터리를 PATH 환경변수 맨 앞에 두어, 시스템 기본 python3 대신 uv 로 설치한 버전을 가리키도록 한다.
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv venv --python ${PYTHON_VERSION} /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# @rynfar/meridian 패키지 설치 시 postinstall 스크립트를 통해 현재 플랫폼(amd64/arm64)에 맞는
# 공식 claude 바이너리(~220MB)가 함께 다운로드되어 설치된다.
# pi-gitlab 과 같은 호스팅 플랫폼 확장은 이미지에 의도적으로 포함하지 않는다.
# 리뷰 결과 게시는 post_review.py 가 REST API 로 직접 안전하게 처리하며,
# --approve 옵션으로 실행되는 에이전트에게 토큰이 주입된 플랫폼 쓰기 도구를 노출하면 보안 위협이 생기기 때문이다.
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" "@rynfar/meridian@${MERIDIAN_VERSION}" \
 && npm cache clean --force \
 && pi --version \
 && meridian --version

# pi 및 meridian 설정 디렉터리와 확장 디렉터리를 생성하고 기본 프로바이더 설정을 기록한다.
RUN mkdir -p /root/.pi/agent/extensions /root/.config/meridian \
 && jq -n \
      --arg provider "${PI_DEFAULT_PROVIDER}" \
      --arg model "${PI_DEFAULT_MODEL}" \
      '{defaultProvider: $provider, defaultModel: $model, quietStartup: true}' \
      > /root/.pi/agent/settings.json

# config/models.json:
# Pi 가 로컬 Meridian 프록시(http://127.0.0.1:3456)를 모델 공급자로 인식하도록 구성한다.
# x-meridian-agent: pi 헤더를 전달하여 Meridian 의 Pi 전용 어댑터를 활성화하고,
# Claude Opus 5(1M), Fable 5.1(1M), Sonnet 5(200K)의 컨텍스트 창 크기와 매개변수를 명시한다.
#
# config/sdk-features.json:
# Team 플랜 구독 환경에서 Anthropic 분류기가 서드파티 프롬프트를 감지하여 발생하는 billing_error 를 방지한다.
# Pi 클라이언트의 시스템 프롬프트는 제외(clientSystemPrompt: false)하고,
# Claude Code 본래의 시스템 프롬프트만 업스트림으로 전송(codeSystemPrompt: true)하도록 사전에 설정한다.
#
# config/extensions/meridian-session.ts:
# 도구 실행 시 프롬프트 캐시 적중률 급락 및 토큰 과다 소모 현상을 방지하는 익스텐션이다 (Meridian 이슈 #734).
# Pi 는 도구 실행 결과(tool_result)를 전송할 때 기본적으로 세션 식별자를 포함하지 않아서,
# Meridian 이 매 도구 호출마다 독립 세션으로 판정하고 지금까지의 대화 히스토리 전체를 새 세션에 다시 전송한다.
# 이 익스텐션은 요청 직전에 세션 ID 를 metadata.user_id 에 주입하여 세션 연속성을 보장하며,
# 도구 실행 턴에서도 90% 이상의 프롬프트 캐시 적중률을 유지하도록 지원한다.
COPY config/models.json /root/.pi/agent/models.json
COPY config/sdk-features.json /root/.config/meridian/sdk-features.json
COPY config/extensions/meridian-session.ts /root/.pi/agent/extensions/meridian-session.ts

RUN pi --list-models meridian

# 리뷰 실행 로직과 프롬프트는 이미지 내부 경로(/opt/meerkit/)에 고정하여 관리한다.
# 체크아웃된 MR 저장소 경로에서 스크립트를 실행하게 되면,
# MR 작성자가 리뷰 스크립트나 지시문을 변조하여 잡에 주입된 인증 토큰을 탈취할 수 있기 때문이다.
COPY run-review.py post_review.py forge.py /opt/meerkit/
COPY prompt/ /opt/meerkit/prompt/
RUN chmod +x /opt/meerkit/run-review.py /opt/meerkit/post_review.py

WORKDIR /workspace
