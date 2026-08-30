# 로컬 GitLab Runner (Meerkit 잡 테스트용)

프로젝트 전용 러너가 없는 환경(포크, 로컬 시험)에서 파이프라인을 돌리려면 러너가 따로 필요하다.

호스트에 gitlab-runner를 설치하지 않고 컨테이너로만 띄운다.

## 1. 러너 등록 토큰 발급

대상 프로젝트(Owner 권한 필요)에 러너를 만든다.

```bash
glab api --method POST user/runners \
  -f runner_type=project_type \
  -f project_id=<PROJECT_ID> \
  -f description=meerkit-local \
  -f 'tag_list=meerkit' \
  -f run_untagged=true
```

응답의 `token` (`glrt-...`) 을 쓴다.

`run_untagged=true` 인 이유: 태그가 없는 잡이 있으면 `false` 일 때 그 잡이 영원히 pending으로 남아 파이프라인이 끝나지 않는다. 로컬 러너 하나로 무거운 잡까지 돌릴 필요가 없으면, 테스트 브랜치의 `.gitlab-ci.yml` 에서 해당 잡을 `when: never` 로 막는다.

## 2. config.toml 작성

```bash
cd local-runner
mkdir -p config
sed 's|REPLACE_WITH_RUNNER_TOKEN|glrt-여기에토큰|' config.toml.example > config/config.toml
```

`config.toml.example` 의 `url` 을 실제 GitLab 주소로 바꾼다.

## 3. 이미지 준비

로컬 러너는 `pull_policy = if-not-present` 라 로컬에 태그만 있으면 레지스트리를 조회하지 않는다.
Apple Silicon에서는 네이티브로 빌드해야 job이 느려지지 않는다.

```bash
cd .. && task build:local
```

`.gitlab-ci.yml` 의 `meerkit` 잡이 참조하는 태그(`:latest`)와 이름이 같아야 한다.

## 4. 러너 기동

```bash
docker compose up -d
docker compose logs -f
```

프로젝트 Settings > CI/CD > Runners 에 online으로 뜨면 성공이다.

## 5. 정리

```bash
docker compose down
glab api --method DELETE runners/<runner_id>
```

## 주의

- `config/` 에는 러너 토큰이 들어간다. 커밋하지 않는다(`.gitignore` 처리됨).
- 포크에서 MR을 열면 다른 잡도 같이 큐에 들어간다. 로컬 러너로 다 돌리기 부담되면 테스트 브랜치에서만 해당 잡을 `when: never` 로 막는다.
