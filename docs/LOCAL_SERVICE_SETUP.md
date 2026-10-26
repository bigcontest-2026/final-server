# 로컬 서비스 연결과 실행

`final-server/`와 `final-client/`를 같은 상위 폴더에 둔다. Python 3.12 이상, Node.js와 npm,
ChatGPT로 로그인한 Codex CLI **0.163.0**, 읽기 가능한 Supabase 복지 DB가 필요하다.

## 준비

서버 루트에서 가상환경을 만들고 런타임 의존성을 설치한다. 프런트엔드는 별도로 `npm ci`를 실행한다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r service/backend/requirements.txt
cd ../final-client/frontend
npm ci
```

`service/backend/.env.example`을 참고해 서버 `.env`에 다음을 설정한다. 이 파일은 Git 제외 대상이며
DB 비밀번호와 접속 문자열을 프런트 환경변수나 화면에 전달하지 않는다.

- `APP_MODE=final`, `ACCESS_MODE=local_development`
- `WELFARE_DATABASE_URL`, `WELFARE_DATABASE_PASSWORD`, `WELFARE_DATABASE_SCHEMA=welfare`
- `DONGHAENG_PROVIDER=codex_subscription`
- `DONGHAENG_CODEX_BIN`: 설치된 0.163.0 실행 파일의 절대 경로
- `DONGHAENG_CODEX_TIMEOUT_SECONDS=90`, `DONGHAENG_ANALYSIS_RESEARCH_TIMEOUT_SECONDS=120`
- `DONGHAENG_PHOENIX_ENABLED=false`

`DONGHAENG_MODEL`을 생략하면 계정 기본 모델을 사용한다. 현재 모델 어댑터는 검증 버전을 정확히 검사하므로
서비스 전용 실행 파일을 지정한다. 로그인 파일을 복사하거나 시스템 Codex 버전을 변경할 필요는 없다.
구독 모델이 실패해도 API 키 방식으로 자동 전환하지 않는다.

지역 조사에서는 검증 CLI의 네이티브 웹 도구 실행에 필요한 호스트만 켠다. 일반 코드 모드·셸·MCP·앱은
계속 끄고, 실제 웹 검색 이벤트와 공식 원문 인용을 검증한 결과만 저장한다. 웹 실행 실패는 근거 없음과 구분한다.

Supabase는 기존 공개 자료와 `welfare-service-actions-v3`의 완료된 제공서비스 연결을 읽는다.
실행기는 원격 URL과 준비된 연결 규칙을 요구한다. DB 구조나 원격 사업 자료를 수정하지 않는다.

## 실행

`final-server`에서 실행한다.

```bash
.venv/bin/python scripts/dev.py start
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
```

- 화면: <http://127.0.0.1:5173>
- API: <http://127.0.0.1:8011>, 문서: <http://127.0.0.1:8011/docs>
- 실행 로그와 프로세스 기록: `runtime/local-services/`
- 보고서·대응안: 기본 `service/backend/runtime/`의 SQLite

포트가 다른 프로세스에 점유돼 있으면 시작을 중단한다. `stop`은 기록된 PID의 시작 시각·그룹을
다시 확인한 뒤 해당 실행기가 시작한 그룹만 종료한다. 별도로 실행한 서버는 자동 종료하지 않는다.
`status`는 DB 연결·모델 인증·자료 번들·저장소 및 포트 상태를 출력하며, LLM 생성은 실행하지 않는다.

## 자동 활동 요약

화면은 보고서를 열 때 `POST /api/model/analysis-reports/{report_id}/summary`를 호출한다.
추가 본문·조회 조건은 받지 않으며 서버에 저장된 관측만 요약한다. 성공 응답은
`report_id`, `text`, `created_at`, `reused`다.

캐시는 보고서 입력·프롬프트·모델 설정별로 저장되고 서버 재시작 후에도 유지된다.
새 `analysis_report_summaries` 테이블을 추가하며 기존 보고서·대응안을 수정하지 않는다.
생성 잠금은 180초 뒤 회수할 수 있고 모델 호출 중에는 SQLite 쓰기 트랜잭션을 열어 두지 않는다.

없는 보고서는 404, 같은 요약 생성 중이나 다른 구독 AI 작업 진행 중이면 409,
모델 연결·생성·출력 검증 실패는 503이다. 실패 결과는 캐시하지 않으며 화면에서 다시 시도할 수 있다.
원인 조사·사업 대화·대응안 작성과 동시 실행하지 않고 순차 사용한다.

LLM은 핵심 관측 1~3개를 선택하고 생활상 의미·가능한 배경·살펴볼 대응을 작성한다.
수치·집단·활동 집계 지역은 서버 문장으로 표시하며 해석문에는 새 숫자나 링크를 받지 않는다.
자료가 없으면 모델을 호출하지 않고 비교 자료가 없다는 안내만 반환한다.
내부 품질·경보 설정과 출처 검증은 유지하되 생성 본문에 반복 안내를 붙이지 않는다.
프롬프트 버전 변경 시 기존 요약 캐시는 재사용하지 않으며 저장된 보고서 원문은 수정하지 않는다.
기관 모드에서는 개인 구독 모델 호출을 허용하지 않는다.

## 검증

개발 검사에는 `service/backend/requirements-agent-dev.txt`를 설치한다.

```bash
.venv/bin/python service/scripts/check_backend_ci.py
```

클라이언트에서는 `npm test`와 `npm run build`를 실행한다. 오프라인 검사와 실제 모델·원격 DB 검증을
구분한다. 구독 호출의 `cost_usd=null`은 호출별 API 비용을 계산하지 않았다는 뜻이다.

### 2026-10-26 로컬 연결 확인

- Supabase 활성 공개 자료 16,244건의 목록 조회와 실제 자료 기반 복지 추천: HTTP 200.
- ChatGPT 구독 추론, 공개 사업 AI 대화, 활동 요약, 대응안 AI 작성: 실제 모델 응답 확인.
- 대응안 저장, 선택 버전의 통합 보고서 생성·재조회: HTTP 200.
- 자동 요약의 중복 요청·보고서 전환·화면 해제·재시도: 브라우저 검사 통과.
- 지역 원인 조사: 실제 웹 검색 실행은 확인했으나 시험 사례의 출처 검증에 실패해 HTTP 503으로 보류.
  검증되지 않은 출처나 원인 설명을 저장하지 않았으며 원본 관측 보고서는 보존했다.

실행 증거는 Git 제외 `runtime/integration/`, 오프라인 검사 결과는
`service/.artifacts/backend-ci/`에 있다. 위 확인은 로컬 사용 범위이며 기관 운영 검증이나 인과 입증이 아니다.
