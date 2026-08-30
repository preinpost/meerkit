# Meerkit

MR/PR 코드 리뷰를 자동화하는 CI 컨테이너.

pi(코딩 에이전트)를 헤드리스로 돌려 변경분을 리뷰하고, 결과를 GitLab MR 인라인
discussion 또는 GitHub PR 리뷰 코멘트로 남긴다. 어느 쪽인지는 환경변수로 판별한다.

이름은 보초 서는 미어캣(meerkat)에서 가져왔다.

## 구성

| 경로 | 역할 |
|---|---|
| `Dockerfile` | pi + 확장 + 리뷰 스크립트를 구운 CI 이미지 |
| `run-review.py` | 잡 진입점. diff 범위 계산 → pi 실행 → 게시 |
| `prompt/review.md` | 리뷰 지시문. 결과를 JSON 스키마로 쓰게 한다 |
| `prompt/fluent-korean.md` | 한국어 문장 지침. 시스템 프롬프트에 붙는다 |
| `post_review.py` | JSON 을 인라인 코멘트로 게시. 본문 렌더와 순서 |
| `forge.py` | GitLab / GitHub REST 어댑터. 플랫폼 차이는 전부 여기 |
| `tests/` | 가짜 포지 서버에 대고 도는 게시 로직 테스트 |
| `Taskfile.yaml` | 빌드/푸시/검증/테스트/러너 관리 태스크 |
| `local-runner/` | 테스트용 로컬 GitLab Runner |

리뷰 로직은 전부 이미지 안(`/opt/meerkit/`)에 있다. 리뷰 대상 저장소의 체크아웃에서
실행하지 않는다 — 변경 작성자가 스크립트나 프롬프트를 고쳐 잡에 주입된 토큰을 빼낼 수 있기 때문이다.

같은 이유로 `pi-gitlab` 같은 호스팅 플랫폼 확장은 이미지에 넣지 않는다. 게시는
`post_review.py` 가 REST 로 직접 하므로 쓸 데가 없고, `--approve` 로 도는 에이전트에게
토큰 쥔 쓰기 도구를 노출하게 된다.

## 사용하는 쪽 설정

이 저장소를 참조할 필요 없다. 결합점은 이미지 태그 하나이다.

### GitLab CI

쓰는 프로젝트의 `.gitlab-ci.yml` 에 아래를 넣는다.

```yaml
meerkit:
  stage: test
  needs: []
  image: ghcr.io/<owner>/meerkit:latest
  # tags:
  #   - your-runner-tag
  allow_failure: true
  variables:
    # CI_MERGE_REQUEST_DIFF_BASE_SHA 를 찾으려면 얕은 클론으로는 부족하다.
    GIT_DEPTH: "0"
  script:
    - /opt/meerkit/run-review.py
  artifacts:
    when: always
    paths:
      - meerkit.json
    expire_in: 7 days
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
      when: manual
```

### GitHub Actions

`.github/workflows/meerkit.yml` 에 넣는다.

```yaml
name: meerkit
on: pull_request

permissions:
  contents: read
  pull-requests: write   # 없으면 GITHUB_TOKEN 이 읽기 전용이라 게시가 실패한다

jobs:
  review:
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/<owner>/meerkit:latest
    steps:
      - uses: actions/checkout@v4
        with:
          # GIT_DEPTH: "0" 과 같은 이유. 베이스 커밋이 있어야 한다.
          fetch-depth: 0
      - run: /opt/meerkit/run-review.py
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: meerkit
          path: meerkit.json
```

주의할 점:

- **이미지가 러너에서 닿아야 한다.** 프라이빗 레지스트리는 GitHub 호스티드 러너에서
  안 보인다. GHCR 같은 곳으로 푸시하고
  `task push REGISTRY=ghcr.io/<owner> IMAGE=meerkit` 로 태그를 맞춘다.
- **amd64 여야 한다.** 호스티드 러너는 x86_64 다. `task build` 기본값이 맞고,
  `task build:local` 로 만든 arm64 이미지는 못 쓴다.
- **포크에서 온 PR 은 게시가 안 된다.** `GITHUB_TOKEN` 이 읽기 전용으로 내려온다.
  `pull_request_target` 으로 우회할 수 있지만 그건 PR 코드를 신뢰된 컨텍스트에서
  돌리는 것이라, 위에 적은 위협 모델과 정면으로 부딪친다. 포크를 쓸 거면 별도 설계가 필요하다.
- GitLab 의 `when: manual` 에 해당하는 게 없다. 수동 실행이 필요하면 `workflow_dispatch`
  나 라벨 트리거를 따로 붙인다.

## 필요한 변수

| 변수 | 필수 | 용도 |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 예 | 모델 호출. 없으면 잡이 실패한다 |
| `PI_GITLAB_TOKEN` | GitLab | MR 게시. 없으면 게시를 건너뛰고 아티팩트만 남긴다 |
| `GITHUB_TOKEN` | GitHub | PR 게시. 위와 같다 |
| `MEERKIT_MODEL` | 아니오 | 예: `claude-bridge/claude-sonnet-5` |
| `MEERKIT_THINKING` | 아니오 | `off`~`max` |
| `MEERKIT_JSON` | 아니오 | 산출물 경로. 기본 `meerkit.json` |
| `MEERKIT_FORGE` | 아니오 | `gitlab` / `github` 강제 지정. 보통 자동 판별된다 |
| `MEERKIT_DIFF_BASE` | 아니오 | 리뷰 범위 수동 지정. CI 밖 시험 실행용 |

GitLab 에서는 모두 **Masked ✅ / Protect ❌** 로 등록한다.
Protect 를 켜면 source와 target 브랜치가 **둘 다** protected 일 때만 주입되므로,
일반 기능 브랜치에서 여는 MR 에서는 변수가 비어 있게 된다.

### CLAUDE_CODE_OAUTH_TOKEN 발급

로컬에서 한 번만 받으면 된다. 장기 토큰이라 CI 컨테이너는 브라우저 승인을 거치지 않는다.

```bash
claude setup-token   # 브라우저 승인 → sk-ant-oat01-... 출력
```

만료되면 잡이 `401 OAuth access token is invalid` 로 실패한다. 다시 발급해 변수만 갱신한다.

### PI_GITLAB_TOKEN 발급

`CI_JOB_TOKEN` 으로는 MR 노트를 작성할 수 없어 별도 토큰이 필요하다.

리뷰 대상 프로젝트에서: **Settings → Access tokens → Add new token**

| 항목 | 값 |
|---|---|
| Name | `meerkit` |
| Role | `Developer` |
| Scopes | `api` |

발급된 값을 **Settings → CI/CD → Variables** 에 `PI_GITLAB_TOKEN` 으로 넣는다
(Masked ✅, Protect ❌).

프로젝트 액세스 토큰은 해당 프로젝트로 범위가 제한되므로 개인 액세스 토큰보다 안전하다.

### GITHUB_TOKEN

따로 발급하지 않는다. Actions 가 잡마다 주입하는 값을 그대로 쓰되, 워크플로에
`permissions: pull-requests: write` 를 줘야 한다.

이 토큰은 설치 토큰이라 `GET /user` 가 막혀 있다. 그래서 GitLab 과 달리 이전 코멘트를
작성자로 미리 거르지 못하고, 마커(`<!-- meerkit-bot -->`)로 고른 뒤 삭제가 403 으로
떨어지는 것을 받아 넘긴다.

## 두 플랫폼의 차이

`forge.py` 가 흡수하는 차이는 대체로 이렇다.

| | GitLab | GitHub |
|---|---|---|
| 인라인 게시 | `POST .../discussions` + `position[*]` | `POST .../pulls/{n}/comments` |
| 요약 | `POST .../notes` | `POST .../issues/{n}/comments` |
| 인증 | `PRIVATE-TOKEN` + form-urlencoded | `Bearer` + JSON |
| diff 밖 라인 거부 | 400 | 422 |
| 게시 주체 확인 | `GET /user` | 불가 (설치 토큰) |
| 리뷰 범위 | `CI_MERGE_REQUEST_DIFF_BASE_SHA` (머지 베이스) | 이벤트의 `base.sha` (브랜치 끝) → `git merge-base` 로 정규화 |

GitHub 은 리뷰 코멘트 생성에 2차 레이트 리밋을 건다. 지적 하나당 요청 하나를 쏘므로
게시 요청 사이에 1초 간격을 둔다(GitLab 은 0).

같은 403 이 레이트 리밋과 권한 부족 양쪽에서 나므로, diff 밖 라인(422)과 반드시 구분해야
한다. 422 는 조용히 요약으로 내리고 403 은 stderr 에 남기고 종료 코드 1 로 끝낸다.

애초에 그 상황을 줄이려고, 게시 전에 `git diff --unified=0` 을 파싱해 실제로 바뀐 라인
집합을 만들고 거기 없는 지적은 요청조차 하지 않는다.

## 한국어 문장 품질

리뷰 본문은 사람이 읽는 글이다. 모델은 조사와 어미를 빼먹고 명사를 나열하는 압축된 문장을
쓰는 경향이 있어서, 지적이 정확해도 읽히지 않는다.

[fluent-korean](https://github.com/snflkd/fluent-korean) 의 지침을 `--append-system-prompt` 로
붙여 이를 완화한다. 원본은 Claude Code output-style 플러그인이고,
코딩 지침이 빠진 `fluent-korean-not-coding` 변형을 가져와 YAML 프런트매터만 걷어냈다
(MIT 라이선스). 이 리뷰 에이전트는 코드를 고치지 않으므로 해당 변형이 맞다.

지침을 갱신하려면 상류 저장소에서 파일을 다시 받아 프런트매터를 걷어내면 된다.
시스템 프롬프트가 약 6KB 늘어나므로 토큰을 조금 더 쓴다.

## 개발

### 환경

[uv](https://docs.astral.sh/uv/) 를 쓴다. 런타임 서드파티 의존성은 없고(게시는 stdlib
`urllib` 로 직접 호출한다), uv 는 Python 버전 고정과 개발 도구 관리용이다.

```bash
uv sync        # .venv 생성 + ruff 설치
task check     # 린트 + 테스트
```

**Python 버전을 바꿀 때는 두 곳을 같이 옮긴다** — `.python-version` 과 `Dockerfile` 의
`ARG PYTHON_VERSION`. 한쪽만 바꾸면 로컬에서는 통과하고 CI 에서만 깨지는 문법이 들어온다.
이미지도 uv 로 Python 을 깐다(apt 의 `python3` 은 bookworm 기준 3.11 에 묶여 있다).

포매터는 켜지 않았다. `ruff format` 이 pi 호출 인자처럼 의미 단위로 묶어둔 줄을 풀어헤쳐
오히려 읽기 나빠지는 곳이 있다.

### 테스트

```bash
task test     # 또는 uv run pytest
```

테스트는 `unittest.TestCase` 로 쓰고 pytest 는 실행기로만 쓴다. VS Code 를 쓴다면
권장 확장(`ms-python.python`, `charliermarsh.ruff`)을 깔고 `uv sync` 만 하면
테스트 패널에서 바로 돌리거나 중단점을 잡을 수 있다.

`tests/mock_forge.py` 가 표준 라이브러리 HTTP 서버로 가짜 GitLab/GitHub 을 띄운다.
네트워크도 토큰도 도커도 필요 없고 몇 초에 끝난다. API 주소가 원래 환경변수
(`CI_API_V4_URL`, `GITHUB_API_URL`)라 프로덕션 코드에 테스트용 훅이 없다.

**목이 증명하는 것은 "우리가 보내려는 걸 보내고 있다" 까지다.** 응답 규칙은 API 문서를
보고 넣은 가정이라, 실물이 그 페이로드를 받아준다는 증명은 아니다. 그건 실제 프로젝트에
대고 돌려봐야 안다.

목이 잡아주는 것: 게시 순서, 인라인 실패 시 요약 폴백, 남의 코멘트·시스템 노트를 안
건드리는지, 100건 초과 페이지네이션, 물결표가 코드 스팬 밖에서만 escape 되는지,
403 과 422 를 구분하는지.

### 이미지 빌드

```bash
task                 # 태스크 목록
task build           # amd64 빌드 (GitHub 호스티드 러너용)
task build:local     # arm64 네이티브 빌드 (Apple Silicon 로컬 러너용)
task push            # 빌드 후 레지스트리 푸시
task verify          # 빌드된 이미지 점검
```

`Dockerfile` 의 레이어 순서는 의도적이다. 스크립트 `COPY` 가 맨 끝에 있어 프롬프트만
고치면 재빌드가 1초대로 끝난다. `apt`/`uv`/`npm`/`pi install` 을 건드리면 claude 바이너리
(~220MB) 재설치로 7분대가 된다.

### 로컬 시험 실행

실제 저장소를 대상으로 리뷰만 돌려본다(게시는 하지 않는다).

```bash
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
task review REPO=/path/to/repo BASE=HEAD~2
```

게시까지 확인하려면 스크래치 프로젝트에 PR/MR 을 하나 열고 `post_review.py` 를 직접
돌리면 된다. CI 도 러너도 필요 없다.

```bash
CI_API_V4_URL=https://gitlab.com/api/v4 \
CI_PROJECT_ID=12345678 \
CI_MERGE_REQUEST_IID=1 \
PI_GITLAB_TOKEN=glpat-... \
MEERKIT_JSON=tests/fixtures/sample.json \
uv run python post_review.py
```

로컬 러너는 `task runner:up` / `runner:logs` / `runner:down` 으로 다룬다.
