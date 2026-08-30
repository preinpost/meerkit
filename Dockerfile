# Meerkit — pi 기반 MR 리뷰용 CI 이미지.
# pi 본체와 확장 설치를 이미지에 구워두어 매 파이프라인마다 npm/git 설치를 하지 않는다.
# 빌드/푸시는 Taskfile 로 로컬에서 수동 수행한다.
#
# 레이어 순서 주의: 아래로 갈수록 자주 바뀐다.
# 스크립트 COPY 를 맨 끝에 두어야 프롬프트 수정이 claude 바이너리(~220MB) 재설치를 유발하지 않는다.
ARG UV_VERSION=0.12.5
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM node:22-bookworm-slim

ARG PI_VERSION=0.84.3
# apt 의 python3 은 bookworm 기준 3.11 에 묶여 있어 로컬 개발 환경과 벌어진다.
# uv 로 깔아 로컬과 같은 버전을 쓴다. 패치까지 박는다 — .python-version 과 같이 옮긴다.
ARG PYTHON_VERSION=3.14.7
# pi-claude-bridge 는 @anthropic-ai/claude-agent-sdk 를 끌고 오고, 그 SDK가
# 플랫폼별 optionalDependency 로 claude 실행파일(~220MB)을 함께 설치한다.
# Claude Code를 따로 깔 필요가 없다.
# pi-gitlab 같은 호스팅 플랫폼 확장은 넣지 않는다. 게시는 post_review.py 가 REST 로 직접 하므로
# 쓸 데가 없고, --approve 로 도는 에이전트에게 토큰 쥔 쓰기 도구를 노출하게 된다.
ARG PI_PACKAGES="npm:pi-claude-bridge"
ARG PI_DEFAULT_PROVIDER=claude-bridge
ARG PI_DEFAULT_MODEL=claude-opus-5
# Team Premium 은 bridge 기준으로 max 로 취급해야 Opus 1M 컨텍스트가 켜진다.
ARG CLAUDE_BRIDGE_PLAN=max

# Claude Code는 root에서 --dangerously-skip-permissions 를 거부하고, pi-claude-bridge는
# 항상 그 모드로 동작한다. 잡 컨테이너는 매번 버려지는 격리 환경이므로
# 바이너리가 제공하는 IS_SANDBOX 탈출구를 쓴다.
# 비root 사용자 전환은 러너 helper가 /builds 를 root로 생성해 쓰기가 깨진다.
ENV PI_SKIP_VERSION_CHECK=1 \
    GIT_TERMINAL_PROMPT=0 \
    IS_SANDBOX=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl jq \
 && rm -rf /var/lib/apt/lists/*

# 의존성이 없어 설치할 패키지는 없다. venv 를 PATH 앞에 두어 python3 가 이쪽을 가리키게만 한다.
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv venv --python ${PYTHON_VERSION} /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force \
 && pi --version

RUN mkdir -p /root/.pi/agent \
 && jq -n \
      --arg provider "${PI_DEFAULT_PROVIDER}" \
      --arg model "${PI_DEFAULT_MODEL}" \
      '{defaultProvider: $provider, defaultModel: $model, quietStartup: true, packages: []}' \
      > /root/.pi/agent/settings.json \
 && jq -n --arg plan "${CLAUDE_BRIDGE_PLAN}" \
      '{provider: {plan: $plan}}' \
      > /root/.pi/agent/claude-bridge.json

RUN for pkg in ${PI_PACKAGES}; do pi install "$pkg"; done \
 && pi list

# 리뷰 로직은 이미지에 고정한다. 체크아웃된 MR 소스에서 실행하면
# MR 작성자가 스크립트/프롬프트를 고쳐 잡에 주입된 토큰을 빼낼 수 있다.
COPY run-review.py post_review.py forge.py /opt/meerkit/
COPY prompt/ /opt/meerkit/prompt/
RUN chmod +x /opt/meerkit/run-review.py /opt/meerkit/post_review.py

WORKDIR /workspace
