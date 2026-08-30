# Meerkit

Meerkit 은 MR/PR 코드 리뷰를 자동화하는 CI 컨테이너이다.

코딩 에이전트인 pi 를 헤드리스 모드로 실행하여 변경분을 리뷰하고, 그 결과를 GitLab MR 의
인라인 discussion 이나 GitHub PR 의 리뷰 코멘트로 게시한다. 두 플랫폼 중 어느 쪽에서
실행되고 있는지는 환경변수를 통해 판별한다.

이름은 무리를 위해 보초를 서는 동물인 미어캣(meerkat)에서 가져왔다.

## 구성

| 경로 | 역할 |
|---|---|
| `Dockerfile` | pi 와 확장, 리뷰 스크립트를 함께 빌드하는 CI 이미지이다 |
| `run-review.py` | 잡 진입점이다. diff 범위를 계산하고 pi 를 실행한 뒤 결과를 게시한다 |
| `prompt/review.md` | 리뷰 지시문이다. 결과를 JSON 스키마에 맞추어 작성하게 한다 |
| `prompt/fluent-korean.md` | 한국어 문장 지침이다. 시스템 프롬프트에 덧붙는다 |
| `post_review.py` | JSON 을 인라인 코멘트로 게시한다. 본문 렌더링과 게시 순서를 담당한다 |
| `forge.py` | GitLab 과 GitHub 의 REST 어댑터이다. 플랫폼 차이를 모두 흡수한다 |
| `tests/` | 가짜 포지 서버를 대상으로 실행하는 게시 로직 테스트이다 |
| `Taskfile.yaml` | 빌드와 푸시, 검증, 테스트, 러너 관리 태스크를 정의한다 |
| `local-runner/` | 테스트용 로컬 GitLab Runner 이다 |

리뷰 로직은 모두 이미지 내부(`/opt/meerkit/`)에 존재한다. 리뷰 대상 저장소를 체크아웃한
경로에서는 실행하지 않는데, 변경 작성자가 스크립트나 프롬프트를 수정하여 잡에 주입된 토큰을
탈취할 수 있기 때문이다.

같은 이유로 `pi-gitlab` 과 같은 호스팅 플랫폼 확장은 이미지에 포함하지 않는다. 게시 작업은
`post_review.py` 가 REST API 를 직접 호출하여 처리하므로 확장이 필요하지 않고, 확장을 포함하면
`--approve` 옵션으로 실행되는 에이전트에게 토큰을 쥔 쓰기 도구를 노출하게 된다.

## 사용하는 쪽 설정

이 저장소를 참조할 필요는 없다. 두 저장소를 연결하는 지점은 이미지 태그 하나뿐이다.

### GitLab CI

도입하려는 프로젝트의 `.gitlab-ci.yml` 에 아래 내용을 추가한다.

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

`.github/workflows/meerkit.yml` 에 아래 내용을 추가한다.

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

주의할 점은 아래와 같다.

- **러너가 이미지를 내려받을 수 있어야 한다.** 프라이빗 레지스트리는 GitHub 호스티드
  러너에서 접근할 수 없다. GHCR 과 같은 레지스트리로 푸시한 뒤
  `task push REGISTRY=ghcr.io/<owner> IMAGE=meerkit` 으로 태그를 일치시킨다.
- **이미지 아키텍처는 amd64 여야 한다.** 호스티드 러너는 x86_64 환경이다. `task build` 의
  기본값이 여기에 해당하며, `task build:local` 로 빌드한 arm64 이미지는 사용할 수 없다.
- **포크에서 생성된 PR 에는 게시할 수 없다.** `GITHUB_TOKEN` 이 읽기 전용 권한으로
  주입되기 때문이다. `pull_request_target` 이벤트를 사용하면 우회할 수 있으나, 이 방식은
  PR 의 코드를 신뢰된 컨텍스트에서 실행하는 것이므로 앞에서 설명한 위협 모델과 정면으로
  충돌한다. 포크를 지원해야 한다면 별도의 설계가 필요하다.
- GitLab 의 `when: manual` 에 대응하는 기능이 없다. 수동 실행이 필요하다면
  `workflow_dispatch` 나 라벨 트리거를 별도로 설정한다.

## 필요한 변수

| 변수 | 필수 | 용도 |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 예 | 모델을 호출한다. 없으면 잡이 실패한다 |
| `PI_GITLAB_TOKEN` | GitLab | MR 에 게시한다. 없으면 게시를 건너뛰고 아티팩트만 남긴다 |
| `GITHUB_TOKEN` | GitHub | PR 에 게시한다. 동작은 위와 같다 |
| `MEERKIT_MODEL` | 아니오 | 예: `claude-bridge/claude-sonnet-5` |
| `MEERKIT_THINKING` | 아니오 | `off` 부터 `max` 까지 지정한다 |
| `MEERKIT_JSON` | 아니오 | 산출물 경로이다. 기본값은 `meerkit.json` 이다 |
| `MEERKIT_FORGE` | 아니오 | `gitlab` 이나 `github` 로 강제 지정한다. 보통은 자동으로 판별된다 |
| `MEERKIT_DIFF_BASE` | 아니오 | 리뷰 범위를 수동으로 지정한다. CI 외부에서 시험 실행할 때 사용한다 |

GitLab 에서는 위 변수를 모두 **Masked ✅ / Protect ❌** 로 등록한다.
Protect 를 활성화하면 source 브랜치와 target 브랜치가 **둘 다** protected 상태일 때만
변수가 주입되므로, 일반 기능 브랜치에서 생성한 MR 에서는 변수가 비어 있게 된다.

### CLAUDE_CODE_OAUTH_TOKEN 발급

로컬에서 한 번만 발급받으면 된다. 유효 기간이 긴 토큰이므로 CI 컨테이너에서는 브라우저
승인 절차를 거치지 않는다.

```bash
claude setup-token   # 브라우저 승인 → sk-ant-oat01-... 출력
```

토큰이 만료되면 잡이 `401 OAuth access token is invalid` 오류로 실패한다. 이 경우 토큰을
다시 발급하여 변수 값만 갱신하면 된다.

### PI_GITLAB_TOKEN 발급

`CI_JOB_TOKEN` 으로는 MR 노트를 작성할 수 없으므로 별도의 토큰이 필요하다.

리뷰 대상 프로젝트에서 **Settings → Access tokens → Add new token** 으로 이동하여 아래와
같이 발급한다.

| 항목 | 값 |
|---|---|
| Name | `meerkit` |
| Role | `Developer` |
| Scopes | `api` |

발급된 값을 **Settings → CI/CD → Variables** 에 `PI_GITLAB_TOKEN` 이라는 이름으로
등록한다(Masked ✅, Protect ❌).

프로젝트 액세스 토큰은 유효 범위가 해당 프로젝트로 제한되므로 개인 액세스 토큰보다 안전하다.

### GITHUB_TOKEN

이 토큰은 따로 발급하지 않는다. Actions 가 잡마다 주입하는 값을 그대로 사용하되,
워크플로에 `permissions: pull-requests: write` 를 명시해야 한다.

이 토큰은 설치 토큰이므로 `GET /user` 호출이 차단되어 있다. 그래서 GitLab 과 달리 이전
코멘트를 작성자 기준으로 미리 선별하지 못하고, 마커(`<!-- meerkit-bot -->`)로 대상을 고른 뒤
삭제 요청이 403 으로 실패하면 그대로 무시하고 진행한다.

## 두 플랫폼의 차이

`forge.py` 가 흡수하는 차이는 대체로 아래와 같다.

| | GitLab | GitHub |
|---|---|---|
| 인라인 게시 | `POST .../discussions` + `position[*]` | `POST .../pulls/{n}/comments` |
| 요약 | `POST .../notes` | `POST .../issues/{n}/comments` |
| 인증 | `PRIVATE-TOKEN` + form-urlencoded | `Bearer` + JSON |
| diff 밖 라인 거부 | 400 | 422 |
| 게시 주체 확인 | `GET /user` | 불가 (설치 토큰) |
| 리뷰 범위 | `CI_MERGE_REQUEST_DIFF_BASE_SHA` (머지 베이스) | 이벤트의 `base.sha` (브랜치 끝) → `git merge-base` 로 정규화 |

GitHub 은 리뷰 코멘트 생성에 2차 레이트 리밋을 적용한다. 지적 하나마다 요청을 하나씩
보내므로 게시 요청 사이에 1초 간격을 둔다(GitLab 에서는 간격을 두지 않는다).

레이트 리밋과 권한 부족 양쪽에서 동일한 403 이 발생하므로, diff 범위 밖의 라인에서 발생하는
422 와 반드시 구분해야 한다. 422 는 별도의 메시지 없이 요약으로 처리하고, 403 은 stderr 에
기록한 뒤 종료 코드 1 로 종료한다.

애초에 그러한 상황을 줄이기 위해, 게시하기 전에 `git diff --unified=0` 을 파싱하여 실제로
변경된 라인 집합을 만들고, 그 집합에 포함되지 않는 지적은 요청조차 보내지 않는다.

## 한국어 문장 품질

리뷰 본문은 사람이 읽는 글이다. 모델은 조사와 어미를 생략하고 명사를 나열하는 압축된 문장을
작성하는 경향이 있어서, 지적 내용이 정확하더라도 잘 읽히지 않는다.

[fluent-korean](https://github.com/snflkd/fluent-korean) 의 지침을 `--append-system-prompt` 로
덧붙여 이 문제를 완화한다. 원본은 Claude Code 의 output-style 플러그인이며, 코딩 지침이 빠진
`fluent-korean-not-coding` 변형을 가져와 YAML 프런트매터만 제거하였다(MIT 라이선스).
이 리뷰 에이전트는 코드를 수정하지 않으므로 해당 변형이 적합하다.

지침을 갱신하려면 상류 저장소에서 파일을 다시 내려받아 프런트매터를 제거하면 된다.
시스템 프롬프트가 약 6KB 늘어나므로 토큰을 조금 더 소비한다.

## 개발

### 환경

[uv](https://docs.astral.sh/uv/) 를 사용한다. 런타임 서드파티 의존성은 없으며(게시는 stdlib 의
`urllib` 로 직접 호출한다), uv 는 Python 버전을 고정하고 개발 도구를 관리하는 용도로 쓴다.

```bash
uv sync        # .venv 생성 + ruff 설치
task check     # 린트 + 테스트
```

**Python 버전을 변경할 때는 `.python-version` 과 `Dockerfile` 의 `ARG PYTHON_VERSION` 을
함께 수정한다.** 한쪽만 변경하면 로컬에서는 통과하지만 CI 에서만 실패하는 문법이 들어올 수
있다. 이미지에서도 uv 로 Python 을 설치하는데, apt 가 제공하는 `python3` 은 bookworm 기준으로
3.11 에 고정되어 있기 때문이다.

포매터는 활성화하지 않았다. `ruff format` 이 pi 호출 인자처럼 의미 단위로 묶어 둔 줄을
재배치하여 오히려 가독성이 떨어지는 부분이 있기 때문이다.

### 테스트

```bash
task test     # 또는 uv run pytest
```

테스트는 `unittest.TestCase` 로 작성하고 pytest 는 실행기로만 사용한다. VS Code 를 사용한다면
권장 확장(`ms-python.python`, `charliermarsh.ruff`)을 설치하고 `uv sync` 를 실행하는 것만으로
테스트 패널에서 곧바로 실행하거나 중단점을 설정할 수 있다.

`tests/mock_forge.py` 가 표준 라이브러리 HTTP 서버를 이용하여 가짜 GitLab 과 GitHub 을
실행한다. 네트워크와 토큰, 도커가 모두 필요하지 않으며 수 초 만에 끝난다. API 주소가
원래부터 환경변수(`CI_API_V4_URL`, `GITHUB_API_URL`)로 지정되므로 프로덕션 코드에는 테스트
전용 훅을 넣지 않았다.

**목이 검증하는 범위는 "의도한 요청을 실제로 보내고 있다" 까지이다.** 응답 규칙은 API 문서를
참고하여 작성한 가정이므로, 실제 서버가 그 페이로드를 수용한다는 사실까지 증명하지는 못한다.
그 부분은 실제 프로젝트를 대상으로 실행해 보아야 확인할 수 있다.

목이 검증해 주는 항목은 게시 순서, 인라인 게시가 실패했을 때의 요약 폴백, 다른 사용자의
코멘트와 시스템 노트를 수정하지 않는지 여부, 100건을 초과하는 경우의 페이지네이션, 물결표가
코드 스팬 밖에서만 escape 되는지 여부, 403 과 422 를 구분하는지 여부이다.

### 이미지 빌드

```bash
task                 # 태스크 목록
task build           # amd64 빌드 (GitHub 호스티드 러너용)
task build:local     # arm64 네이티브 빌드 (Apple Silicon 로컬 러너용)
task push            # 빌드 후 레지스트리 푸시
task verify          # 빌드된 이미지 점검
```

`Dockerfile` 의 레이어 순서는 의도적으로 배치한 것이다. 스크립트를 `COPY` 하는 단계가 맨 끝에
있으므로 프롬프트만 수정하면 재빌드가 1초대에 끝난다. 반면 `apt` 나 `uv`, `npm`,
`pi install` 단계를 수정하면 claude 바이너리(약 220MB)를 다시 설치하게 되어 7분대까지 늘어난다.

### 로컬 시험 실행

실제 저장소를 대상으로 리뷰만 실행해 본다(게시는 수행하지 않는다).

```bash
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
task review REPO=/path/to/repo BASE=HEAD~2
```

게시 동작까지 확인하려면 임시 프로젝트에 PR 이나 MR 을 하나 생성한 뒤 `post_review.py` 를
직접 실행하면 된다. CI 와 러너는 모두 필요하지 않다.

```bash
CI_API_V4_URL=https://gitlab.com/api/v4 \
CI_PROJECT_ID=12345678 \
CI_MERGE_REQUEST_IID=1 \
PI_GITLAB_TOKEN=glpat-... \
MEERKIT_JSON=tests/fixtures/sample.json \
uv run python post_review.py
```

로컬 러너는 `task runner:up` 과 `runner:logs`, `runner:down` 명령으로 관리한다.
