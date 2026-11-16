# 신이어 통화 서버

65세 이상 시니어의 AI 안부전화를 실제 동네 모임 참가로 연결하는 서비스의 T 서버다.
Twilio–Gemini Live 음성 연결, 통화 상태·원문·요약·RAG를 담당한다.
회원·모임·신청·미응답 정책은 C 서버인 `siniear-service`가 담당한다.
현재는 개발 환경과 상태 확인 API만 구현되어 있다.

## 작업 기록과 커밋

작업마다 관련 GitHub Issue를 확인하고 없으면 생성한다. [작업 템플릿](.github/ISSUE_TEMPLATE/task.md)과
[버그 템플릿](.github/ISSUE_TEMPLATE/bug_report.md)에 배경·범위·완료 조건을 적고, 완료 시 검증 결과를 남긴다.
커밋은 `type(scope): 한국어 요약` 형식으로 작성하며 scope는 선택, 제목은 72자 이내를 권장한다.
유형은 `feat`, `refactor`, `fix`, `docs`, `rm`, `test`, `chore`, `revert`를 사용하고,
footer에 `Refs: #번호`를 기록한다. 다른 저장소의 Issue는 전체 URL로 연결한다.
호환성을 깨는 변경에는 `!`와 `BREAKING CHANGE:`를 함께 쓴다.
공통 작업공간의 상세 규칙은 루트 `docs/COMMIT_CONVENTION.md`에 있다. 이 파일은 이 저장소에 포함되지 않는다.
[규칙 확정 및 템플릿 도입 기록](https://github.com/CreatePath/siniear-service/issues/1)을 참고한다.

## 개발 환경 실행

두 저장소를 같은 부모 폴더 아래 두고, `siniear-service`의 `compose.yaml`로 함께 실행한다.
Docker Desktop의 Linux 엔진과 Docker Compose가 필요하다.

```text
workspace/
  siniear-service/  # https://github.com/CreatePath/siniear-service.git
  siniear-call/     # 이 저장소
```

```powershell
cd ../siniear-service
# 최초 한 번만 실행. 기존 .env가 있으면 복사하지 않는다.
Copy-Item .env.example .env
# .env의 DB_PASSWORD를 로컬 개발용 값으로 설정한 뒤 실행한다.
docker compose up --build --detach --wait --wait-timeout 120
docker compose ps
```

Compose는 **서비스 저장소의 `.env`만 사용**해 두 서버와 PostgreSQL을 설정한다.
이 저장소의 `.env.example`은 별도로 실행할 때 사용할 DB 설정 예제이며 Compose 입력이 아니다.

- Swagger: http://localhost:8001/api/docs
- 프로세스 상태: http://localhost:8001/api/health
- 실제 DB 연결 상태: http://localhost:8001/api/health/ready
- Compose 내부 주소: T는 `http://call:8000`, C는 `http://service:8000`, DB는 `db:5432`

프로세스 상태는 DB 장애에도 200, DB 연결 상태는 `SELECT 1` 성공 시 200,
DB 오류·시간 초과 시 상세 정보를 숨기고 503을 반환한다. 업무 API는 아직 없다.
소스 수정은 자동 반영되며 의존성 수정은 잠금 파일 갱신 후 재빌드가 필요하다.

서비스 저장소 폴더에서 운영·검증 명령을 실행한다.

```powershell
docker compose logs --tail 100 call
docker compose exec call ruff check .
docker compose exec call ruff format --check .
docker compose exec call pytest
docker compose stop
docker compose start
```

`docker compose down`도 DB 볼륨은 보존한다. `down --volumes`는 DB까지 삭제한다.
공통 포트·DB 설정과 실행 방법은 서비스 저장소 README에서 관리한다.

## 독립 저장소의 도구 설정

Python 3.12, uv 0.13.8, FastAPI/Uvicorn, Psycopg 3를 사용한다.
이 저장소에서 호스트 검사만 실행하려면 다음을 사용한다. 테스트는 실제 DB나 외부 API 키가 필요 없다.

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

Windows에서 `uv`를 찾지 못하면 새 터미널을 열거나 `python -m uv`로 호출한다.
의존성은 `uv add <package>`로 추가하거나 `pyproject.toml` 수정 후 `uv lock`으로 갱신한다.
`pyproject.toml`과 `uv.lock`은 함께 커밋한다. GitHub Actions에도 동일한 검사 절차를 설정했다.

```powershell
docker build --target runtime -t siniear-call:local .
```

기본 runtime 이미지는 비루트 사용자로 실행한다. Compose에서만 development 단계의
검사 도구와 reload를 사용한다. 앱 DB 설정은 `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
필수 `DB_PASSWORD`이며 자세한 예제는 `.env.example`에 있다.

## 구현 경계

[AGENTS.md](AGENTS.md)의 서버 책임을 따른다. 통화 중 신청·취소는 동의 발화를 저장한 뒤
C API로 요청하고, 성공을 확인한 뒤 완료 안내를 한다. 미응답 판정과 비상연락 대상 선정은 C의 책임이다.
대화 내용 기반 위험 탐지는 MVP에 포함하지 않는다.

Twilio·Gemini SDK/키, 공개 HTTPS/WSS 주소, 음성 변환, 도구 호출, RAG, 후처리,
운영 배포는 아직 설정하지 않았다. 개발 환경을 실행해도 실제 발신이나 모델 호출은 발생하지 않는다.
개발 문서·원본 PDF는 루트 [docs/](../docs/)에 별도로 두며 이 저장소에 포함하지 않는다.
업무 작업 전 [공통 지침](../AGENTS.md)과
[프로젝트 맥락](../docs/PROJECT_CONTEXT.md)을 읽는다.
단독 체크아웃에는 공통 지침·개발 문서가 포함되지 않으며 환경 실행은 이 README를 따른다.

## 프로젝트 구조

현재 파일 배치다. Python 패키지의 `__init__.py`와 일부 보조 설정 파일은 생략했다.

```text
siniear-call/
├── app/
│   ├── main.py                 # 앱 생성·시작/종료 처리·라우터 등록
│   ├── api/
│   │   ├── router.py           # /api 접두사와 기능 라우터 조립
│   │   └── health.py           # 상태 확인·DB 준비 상태 API
│   ├── core/
│   │   └── config.py           # 환경 변수·설정 검증
│   ├── db/
│   │   └── health.py           # 읽기 전용 DB 연결 검사
│   ├── modules/                # 기능별 업무 코드 위치
│   ├── integrations/           # 외부 서비스 연결 코드 위치
│   └── workers/                # 백그라운드 작업 실행 진입점 위치
├── tests/
│   ├── conftest.py             # 테스트 클라이언트·공통 fixture
│   ├── unit/
│   │   └── test_config.py      # 설정 검증·비밀 값 노출 방지
│   └── integration/
│       └── test_health.py      # HTTP 응답 검증, DB 연결 함수는 mock
├── .github/workflows/ci.yml    # 린트·포맷·테스트 CI
├── .env.example               # 비밀 값 없는 환경 변수 예제
├── Dockerfile                 # development·runtime 이미지
├── pyproject.toml             # 의존성·개발 도구 설정
├── uv.lock                    # 의존성 버전 고정
├── AGENTS.md                  # 서버별 작업 지침
└── README.md                  # 실행·검증·구조 안내
```

실행 진입점은 `app.main:app`이다. `main.py`가 `api/router.py`를 등록하고,
상태 API는 `db/health.py`를 통해 DB 연결을 검사한다.
`modules`, `integrations`, `workers`는 현재 `__init__.py`만 있으며 업무 구현은 없다.

### 기능 구현 시 추가할 구조

아래 모듈과 파일은 구현 예정이며 현재 디렉터리에는 없다. 필요한 기능부터 추가한다.

| `app/modules/` 하위 모듈 | 책임 |
| --- | --- |
| `calls` | 안부·비상전화 접수·실행·상태·원문 |
| `realtime` | 음성 스트림·Live 세션 제어 |
| `tools` | 모델 도구 실행·동의 발화 저장 후 C 호출 |
| `memory` | 통화 문맥·회원별 RAG 검색 |
| `processing` | 종료 처리·요약·임베딩·색인 |

각 모듈은 `router.py`(API), `schemas.py`(입출력), `service.py`(업무 규칙·트랜잭션),
`repository.py`(DB 접근)로 나눈다. 내부 API가 필요하면 `internal_router.py`,
정책이 복잡해지면 `policies.py`를 추가한다.
업무 트랜잭션은 service가 관리하고 repository가 임의로 commit하지 않는다.

C API 호출은 `integrations/service_server.py`, 공급자 연결은 `integrations/twilio/`·`gemini/`에 둔다.
Twilio 웹훅·WebSocket 라우터는 해당 기능 모듈에 두고 `api/router.py`에서 조립한다.
후처리·재전송 실행 진입점은 `workers/`에 두며 작업 실행기는 아직 정하지 않았다.
SQLAlchemy 매핑은 `app/db/models/`에 추가하고 C가 생성한 스키마에서 호환성을 확인한다.
공유 `migrations/`·`alembic.ini`는 C에서만 관리한다. T의 모임 신청·취소는 C API로 처리한다.

개발 문서는 두 서버 바깥의 루트 `docs/`에 별도로 유지한다.
자세한 C/T 경계와 코드 관리 규칙은 [프로젝트 구조](../docs/PROJECT_STRUCTURE.md)를 따른다.
