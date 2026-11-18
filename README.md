# 신이어 통화 서버

T는 C의 안부전화·비상전화 실행 요청, 한국AI통신–Gemini Live 음성 연결, 상태·원문·요약·검색을 담당한다.
회원·예약·미응답 정책·모임 신청/취소는 C(`siniear-service`)가 소유한다.
팀러너스의 한국AI통신 국내 회선을 사용한다. 기본 설정에서 발신은 꺼져 있다.

## 실행과 공유 DB

Python 3.12, uv 0.13.8을 사용한다. 두 저장소를 형제 폴더로 두고 C에서 실행한다.
Compose는 C의 `.env`만 읽는다. 기존 `.env`를 덮어쓰지 않는다.

```powershell
cd ../siniear-service
docker compose up --build --detach --wait --wait-timeout 120
docker compose exec service alembic upgrade head
```

C는 127.0.0.1:8000, T는 127.0.0.1:8001, PostgreSQL은 127.0.0.1:5432다.
`/api/docs`, `/api/openapi.json`, `/api/health`, `/api/health/ready`를 제공한다.
공유 마이그레이션은 C에서만 관리한다. T에는 필요한 테이블 매핑만 두며 자동 DDL을 실행하지 않는다.
업무 테이블 16개와 운영 테이블 6개를 적용했다. DB volume은 재시작 후 보존하며 `down --volumes`를 사용하지 않는다.

## 구현한 경계

| 경로 | 역할 |
| --- | --- |
| POST `/api/internal/calls` | E01, C의 발신 요청 접수 |
| POST `/api/internal/emergency-calls` | E05, C의 비상전화 요청 접수 |
| POST `/api/webhooks/koreaaitel/voice` | 서명·번호·연결 검증 후 VoiceML |
| POST `/api/webhooks/koreaaitel/status` | 서명·계정·번호·CallId 검증 후 상태와 이벤트 저장 |
| WSS `/api/webhooks/koreaaitel/media` | start에서 통화별 토큰·계정·CallId를 검증하는 양방향 스트림 |

내부 POST에는 Bearer 서비스 인증·`calls:execute` scope·`Idempotency-Key`가 필요하다.
앱 토큰은 거부한다. C가 저장한 명령 및 회원 관계와 일치해야 하며 202는 실행 접수다.
같은 키·내용의 결과를 재사용하고 같은 키의 다른 내용은 409다. 같은 통화의 다른 키도 재발신하지 않는다.
발신 직전 동의·번호·예약과 같은 회원의 진행 중 안부/비상전화를 다시 확인한다.
외부 요청 결과가 불명확하면 `unknown`으로 보존한다. 자동 재발신하지 않으며 공급자 기록 대조가 필요하다.
유효한 서명 콜백으로 CallId를 결합해 복구할 수 있다. 수동 복구 관리 API는 아직 없다.

한국AI통신 μ-law 8kHz를 Gemini 입력 PCM 16kHz로, Gemini 출력 PCM 24kHz를 μ-law 8kHz로 변환한다.
오디오 원본은 저장하지 않는다. 텍스트 원문은 turn ID로 중복을 막고 최종 텍스트 수집 후 후처리한다.
끊긴 연결과 최종 턴을 받지 못한 기록은 partial/unavailable로 구분한다.
비상전화는 C가 정한 번호와 고정 한국어 안내문을 사용한다. 통신상 completed는 사람의 확인을 뜻하지 않는다.

통화 도구는 C의 검색·내 신청·신청·취소 API를 호출한다. 모델이 회원 ID나 임의 대상 ID를 지정하지 못한다.
서버가 만든 대상·작업 안내 발화와 한국AI통신 재생 확인(mark), 이후 사용자의 명시적 긍정 발화를 확인한다.
관련 원문을 DB에 커밋한 뒤 C에 요청하고, C 성공 전에는 완료 결과를 반환하지 않는다.
응답 유실 시 같은 키·본문을 유지하고 다른 대상으로 전환하지 않는다. C는 동의 턴 재사용과 버전 충돌도 검증한다.

요약·임베딩은 별도 상태와 영속 작업으로 재시도한다. 빈 통화에 요약을 만들지 않는다.
gemini-embedding-001/768차원, RETRIEVAL_DOCUMENT/RETRIEVAL_QUERY, L2 정규화·코사인 유사도를 사용한다.
물리 저장은 double precision[]이며 pgvector는 도입하지 않았다. 회원별 최신 후보 200개에서 기본 3개/최대 10개를 검색한다.
현재 동의·요약 해시·모델·차원을 검사하고 다른 회원 데이터는 제외한다. 임계값 0.75는 품질 평가 전 구현값이다.
관심사는 T outbox에서 C로 보내며 T가 seniors를 직접 수정하지 않는다.

## 설정과 작업자

기본 `CALL_PROVIDER=disabled`이며 작업자를 API 시작 시 자동 실행하지 않는다.
개발/test의 `fake`는 외부 호출 없이 동작한다. fake 발신만으로 수신 이벤트가 자동 발생하지 않으며 테스트가 상태를 주입한다.
Compose에서는 C `.env`의 `C_TO_T_TOKEN`과 `T_TO_C_TOKEN`을 방향별로 설정한다. 빈 토큰은 내부 API 접근을 거부한다.
실제 비밀 값이나 전화번호를 저장소에 기록하지 않는다.

| 설정 | 의미 |
| --- | --- |
| DB_HOST/PORT/NAME/USER/PASSWORD | DB 접속. 비밀번호 필수, 값 출력 금지 |
| APP_ENV | development/test/production. production에서 fake 금지 |
| INTERNAL_INBOUND_TOKEN / INTERNAL_OUTBOUND_TOKEN | 단독 실행 시 C→T / T→C 서비스 비밀 |
| INTERNAL_SCOPES | T 수신 기본 calls:execute |
| PEER_BASE_URL | C 주소, Compose http://service:8000 |
| CALL_PROVIDER | disabled/fake/koreaaitel |
| ALLOW_PAID_CALLS | 기본 false. 실제 모드에 명시적 true 필요 |
| PUBLIC_BASE_URL | 한국AI통신이 접근하는 HTTPS 기준 주소, 서명 검증에 사용 |
| KOREAAITEL_ACCOUNT_ID / KOREAAITEL_API_KEY | 한국AI통신 계정 ID·발신용 Bearer API 키 |
| KOREAAITEL_SIGNING_KEY / KOREAAITEL_FROM_NUMBER | 대시보드의 Webhook Signing Secret·계정에 등록한 국내 발신번호 |
| GEMINI_API_KEY / GEMINI_LIVE_MODEL / GEMINI_SUMMARY_MODEL | API 키와 별도 선택할 실제 모델 ID |
| EMBEDDING_MODEL / EMBEDDING_DIMENSIONS | gemini-embedding-001 / 768 고정 계약 |
| MEMORY_MIN_SCORE | 기본 0.75, 실제 한국어 검색 평가 전 값 |

실제 발신에는 `CALL_PROVIDER=koreaaitel`, `ALLOW_PAID_CALLS=true`, 위 한국AI통신·Gemini 설정과 외부에서 접근할 수 있는 HTTPS/WSS가 필요하다. 국내 번호는 발신 요청에서 0으로 시작하는 형식으로 변환한다. 기존 Twilio 회선에 진행 중이거나 결과 불명인 통화가 있다면 전환 전에 기존 콜백 처리를 마치고 공급자 기록과 대조한다.

상태 콜백의 `Timestamp`는 ISO 8601이다. 공급자 순번이 없어 T는 진행 단계를 내부 순번으로 쓰고 같은 통화·상태의 재전송을 한 번만 처리한다. 수신 거절(`rejected`)은 기존 앱 상태 `canceled`로 표시하며, 안부전화 미응답 집계에는 `no-answer`만 포함한다. [발신 API](https://platform.koreaaitel.com/docs/api/calls/createCall) · [상태 콜백](https://platform.koreaaitel.com/docs/webhooks/status) · [서명](https://platform.koreaaitel.com/docs/webhooks/signature-verification) · [VoiceML 스트림](https://platform.koreaaitel.com/docs/build/stream)

발신/후처리/이벤트 전달 작업자와 C 스케줄러는 각각 한 번 실행한 뒤 종료한다.
주기 실행·프로세스 감독은 배포 단계에서 연결한다. 실제 전화는 허용된 대상과 범위를 별도로 확인한 뒤 실행한다.

```powershell
# siniear-service에서 실행
docker compose exec service python -m app.workers.calls
docker compose exec call python -m app.workers.calls
```

## 검증과 의존성

```powershell
# siniear-service에서 실행
docker compose exec call ruff check .
docker compose exec call ruff format --check .
docker compose exec -e RUN_DB_TESTS=1 call pytest
```

2026-11-18 한국AI통신 전환 후 T 전체 32개 통과, Ruff 검사·포맷 통과. DB 업무 테스트는 RUN_DB_TESTS=1 없으면 skip된다.
테스트는 격리 스키마에 C 마이그레이션을 적용하고 종료 시 해당 스키마만 제거한다.
C 서버를 별도 프로세스로 실행하는 실제 HTTP 계약 테스트도 포함한다. 외부 공급자는 fake다.
Python 3.12의 audioop 사용에 폐기 예정 경고 1개가 있다. Python 3.13으로 올릴 때 변환기 교체가 필요하다.

호스트는 `python -m uv sync --locked` 후 `python -m uv run ruff check .` 등으로 실행할 수 있다.
호스트 DB 테스트에는 C 형제 체크아웃과 DB 환경 변수가 필요하다.
CI는 PostgreSQL 및 C 소스를 함께 체크아웃한다. `SERVICE_CONTRACT_REF` 변수로 C ref를 고정할 수 있다.
C가 비공개 저장소이면 `SERVICE_REPO_TOKEN` secret에 읽기 권한이 필요하다.
공유 DB 마이그레이션은 C 저장소에서 관리하며, 두 서버를 같은 스키마 버전으로 실행한다.

주요 추가 의존성은 SQLAlchemy, HTTPX, google-genai다. 버전은 uv.lock을 따른다.
의존성 변경 시 pyproject.toml과 uv.lock을 함께 변경한다. 이미지 빌드는 uv sync --locked, runtime은 비루트 사용자다.

## 남은 작업과 기록

한국AI통신 국내 안부전화 1회에서 공개 HTTPS 콜백, WSS 음성 스트림, Gemini Live 양쪽 발화의 전사 저장을 확인했다. 수신자는 본인 단말에 국내 전화로 표시됐고 별도 안내 멘트 없이 AI 대화가 시작됐다고 확인했다. 스팸 표시, 음질·지연, 전사 정확도, Gemini 도구·요약/RAG 품질과 운영 부하는 별도 검증 대상이다.
실제 로그인·실명 인증·회원/비상연락처 관리·탈퇴 API와 알림 발송, 운영 배포는 후속 범위다.
현재 call_agree=false는 발신·검색·후처리 저장을 차단한다.
`memory.service.invalidate_search`는 철회 시 임베딩/진행 작업을 무효화하는 훅이다.
P1의 동의 변경 경로에 연결해야 재동의 시 과거 검색 데이터가 되살아나지 않는다. 탈퇴의 원문·요약·운영 payload 삭제도 P1에서 연결해야 한다.

[T Issue #2](https://github.com/CreatePath/siniear-call/issues/2), [C Issue #5](https://github.com/CreatePath/siniear-service/issues/5).
[전체 구현 기록](../docs/implementation-plan/CALL_IMPLEMENTATION.md)은 루트 docs에 별도 유지하며 두 저장소 추적 범위 밖이다.
커밋은 `[#번호] type(scope): 한국어 요약`, footer `Refs: #번호`를 사용한다.
