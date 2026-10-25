# Phoenix 로컬 운영과 평가

구현 범위는 공개 자료 기반 개발·개인 시연의 관측과 코드 평가다. 기관 운영 인수나 고가용성 구축이 완료됐다는 의미는 아니다. Phoenix는 업무 DB 또는 감사 원장이 아니다.

## 설치와 실행

저장소 최상위에서 전용 Python 3.12 환경을 사용한다. 직접 의존성은 `pyproject.toml`에서 OTel 1.45.1, Phoenix 20.23.0, Phoenix client 3.5.0으로 고정한다.

```sh
python3 -m venv agent/.venv
agent/.venv/bin/pip install -e model -e 'agent[dev,observability,phoenix]'
agent/.venv/bin/python agent/evaluation/serve_phoenix_local.py --port 6006
```

개발용 실행기는 HTTP를 `127.0.0.1:6006`에만 연다. Phoenix 20.23.0의 gRPC는 `PHOENIX_HOST`와 별개로 `[::]:4317`에 바인딩하므로, 실행기에서 사용하지 않는 gRPC 구성요소를 프로세스 내부 어댑터로 비활성화한다. 설치된 패키지 파일은 수정하지 않는다. 버전이 달라지면 실행을 거부하며, 업그레이드 전에 생성자 계약과 실제 열린 소켓을 다시 검사해야 한다. 일반 `phoenix serve` 명령을 이 실행기의 대체로 사용하지 않는다.

기본 저장소는 `agent/runtime/phoenix/phoenix.sqlite`이며 업무·체크포인트 DB와 분리된다. 실행기는 외부 리소스·telemetry·내장 assistant·MCP 서버·평가 sandbox·Prometheus listener를 끄고 30일 trace 보존 기간을 설정한다. 서버가 상속받는 Phoenix/OTel/일부 모델 제공자 환경변수를 초기화하므로 기존 외부 collector나 모델 인증을 우연히 재사용하지 않는다. 사용자의 파일이나 Codex 로그인은 변경하지 않는다.

애플리케이션 실행 환경에는 다음을 설정한다.

```sh
export DONGHAENG_PHOENIX_ENABLED=true
export DONGHAENG_PHOENIX_ENDPOINT=http://127.0.0.1:6006/v1/traces
export DONGHAENG_PHOENIX_PROJECT=hermes-development
```

`observability.init_observability()`를 앱 시작에 한 번 호출하고 종료 시 `flush_observability()`와 `shutdown_observability()`를 호출한다. 환경변수만 설정하고 초기화를 생략하면 기록하지 않는다. 기본값은 비활성이다. API/worker가 별도 프로세스라면 각각 초기화한다.

## 기록 계약

- `trace_execution(case_id, execution_id, metadata=None)`는 AGENT span을 만들고 `session.id=case_id`로 사건 이력을 연결한다. 재개 작업은 같은 execution ID를 가진 새 root span이 될 수 있다.
- `trace_step(name, kind='CHAIN', input=None, metadata=None)`으로 모델·도구·검증을 수동 계측한다. 사용자 정의 Codex CLI 호출을 자동 계측에 맡기지 않는다.
- 반환 객체는 `trace_id`, `span_id`, `set_output`, `set_error`, `set_attributes`를 제공한다. 업무 실행에 ID를 저장하면 Phoenix 조회와 코드 점수를 정확한 실행에 연결할 수 있다.
- Codex는 `provider=codex_cli`, `auth_mode=chatgpt_subscription`, `billing_mode=subscription`을 기록한다. 반환된 usage만 기록하고 알 수 없는 값은 JSON metadata의 `null`로 유지한다.
- 실패한 LLM span의 사용량이 없으면 `token_usage_status=not_reported`, `usage=null`을 기록한다. Phoenix가 자체 token column에 기본값 0을 표시하더라도 실제 0토큰 사용의 증거가 아니다. 사용량 비교는 앱 metadata의 상태를 함께 읽는다.
- 구독의 `cost_usd`는 항상 `null`이다. 모델 이름은 metadata에 남기고 구독 호출에는 표준 `llm.model_name`을 쓰지 않아 API 단가 기반 자동 비용 추정을 피한다. 토큰 수는 실제 요금이나 구독 잔여율이 아니다.
- 자동 instrumentor와 전역 OTel tracer provider는 등록하지 않는다. 다른 라이브러리의 원문 로그·프롬프트·개인 설정이 섞이지 않도록 명시적 경계만 기록한다.

마스킹은 exporter에 도착하기 전에 수행한다. 인증 관련 키, reasoning/analysis/CLI 진단 필드, 주요 토큰 형식, 이메일·휴대전화·주민등록번호 형태를 제거하며 길이·깊이·항목 수를 제한한다. 원본 예외 메시지와 스택은 보내지 않고 짧은 오류 코드만 보낸다. 이 처리는 모든 개인정보를 식별하는 보안 분류기가 아니다. 호출자는 공개 근거 요약·ID·검증된 구조만 전달해야 한다. 기관 개인정보 원문 또는 전체 CLI JSONL을 넣으면 안 된다.

collector는 loopback HTTP 주소만 허용하고 인증이 섞인 URL·query·다른 경로를 거부한다. 프록시 환경변수와 redirect를 사용하지 않는다. 기관별 원격 collector 연결은 별도 보안 검토와 구현 범위다.

## 장애와 누락 확인

내보내기는 크기가 제한된 비동기 배치다. 기본 대기열은 512 span, 배치 64 span, 전송 제한 1초다. Phoenix 연결 실패로 업무 함수의 결과를 바꾸지 않는다. `observability_status()`의 `started_spans`, `finished_spans`, `exported_spans`, `failed_spans`, `queued_or_dropped_spans`, `last_error_code`로 누락 징후를 확인한다. `force_flush` 성공은 전송 대기열 처리 완료일 뿐 서버 저장 성공을 의미하지 않는다. 최종 증거는 Phoenix에서 해당 span을 다시 읽은 결과다.

현재 버퍼는 메모리 기반이다. 대기열 초과, 장기 장애 또는 프로세스 종료로 유실될 수 있으며 자동 디스크 재전송은 구현하지 않았다. 저장된 업무 기록과 별도 평가 결과는 남지만 유실된 원 trace를 재구성해 원래 실행인 것처럼 표시하지 않는다. 영속 collector queue 및 누락 경보는 운영 단계의 남은 과제다.

## 평가와 피드백 절차

`evaluation/phoenix_feedback.py`는 저장된 `repository.get()` 결과를 평가한다. 8개 CODE 검사는 실행·브리프 저장, 근거 ID, 조사 범위, 지원 조건, 구조화된 해석 제한, 실제 모델 호출 증거, 구독 비용 표현, 후속 결과 증거를 확인한다. 설명의 자연어 주장·사용성은 별도 사람 검토 대상이다.

```sh
agent/.venv/bin/python agent/evaluation/phoenix_feedback.py \
  --case agent/runtime/validation/case.json \
  --origin actual_model \
  --span-id 0123456789abcdef \
  --output agent/runtime/validation/feedback.json \
  --publish
```

예제의 파일과 span ID는 실제 저장 결과로 교체한다. `actual_model`은 성공한 제공자 실행이 있는 경우에만 사용한다. 규칙 기준선은 `rule_baseline`, 공식 캡처 재현은 `official_capture_replay`, 합성 자료는 `mock`으로 명시한다. 공개 정책 캡처로 실행한 실제 모델의 경우 실제 모델 호출 증거와 자료가 과거 캡처라는 한계를 함께 기록한다.

동기 annotation 전송 후 Phoenix SDK/API에서 해당 span과 평가 점수를 다시 읽어 확인한다. `publish_feedback`의 `submitted`만으로 저장 검증을 완료 처리하지 않는다. 데이터셋·평가 결과에는 입력 hash, rubric version, 실행 origin을 함께 기록한다.

로컬 개발 환경에서 API 연결을 사용할 수 없을 때는 다음으로 이미 수집된 SQLite를 읽기 전용으로 확인할 수 있다. 지정한 프로젝트의 span 구조·모델 metadata·CODE 점수만 요약하고 원문 입력은 내보내지 않는다. 이 방식은 운영 API의 권한 검사를 대신하지 않는다.

```sh
agent/.venv/bin/python agent/evaluation/read_phoenix_feedback.py \
  --project hermes-enterprise-development \
  --output agent/runtime/enterprise-feedback/feedback_readback.json
```

현재 `phoenix_regression_cases_v1_1.json`에는 합성 사례 12개가 있다. 기존 만료 지원·없는 근거·인구집단 변경·가짜 모델 성공·자동 경보·구독의 API 요금화 검사에 실제 `survey_i5_population_reference` 유형의 명시적 hold, 누락된 품질 상태, 다른 실행의 근거 인용, 누락된 근거 소유권, 모델 근거 참조를 추가했다. `run_regression_dataset()`으로 기대한 실패가 발생하는지 검사하고 `publish_regression_dataset()`으로 새 이름 `hermes-guardrail-fixtures-1.1`에 등록한다. 기존 `phoenix_regression_cases.json`과 Phoenix의 7개 사례 데이터셋 1.0은 수정하지 않는다. 합성 사례가 통과한 결과는 실제 모델 성능 점수가 아니다.

```sh
agent/.venv/bin/python agent/evaluation/phoenix_feedback.py \
  --regression-dataset --publish \
  --output agent/runtime/enterprise-feedback/regression_dataset_1_1.json
```

`--publish`를 빼면 로컬 검사만 한다. 게시 전에 실제로 전송할 마스킹된 입력을 다시 평가하여 기대 실패 목록이 변하지 않았는지 확인한다. 전체 fixture 계약은 span 요약보다 깊어 최대 12단계까지 허용하며 길이·항목 제한과 비밀정보 제거는 유지한다. 원본과 전송 입력의 hash를 구별해 저장한다. 정제 과정이 평가 의미를 바꾸면 게시하지 않는다. `synthetic=true`·`execution_origin=mock`을 각 예제에 붙인다.

평가 규칙 `hermes-contract-evaluation-1.1`은 `Repository.get()`이 DB 행에서 확인한 근거의 `case_id`·`execution_id`를 요구한다. 두 키가 없던 과거 case JSON은 소속을 ID 문자열로 추정하지 않고 실패로 처리한다. 새 규칙으로 과거 실행을 평가하려면 저장된 업무 DB에서 case를 새 파일로 내보내고, 모델을 다시 호출하지 않은 재평가임을 기록한다. 기존 1.0 평가 파일과 원본 case는 유지한다. 근거의 실제 수집 이력과 내용은 신뢰할 수 있는 업무 저장소를 기준으로 하며, JSON의 필드 검사만으로 원문 진위를 증명하지 않는다.

대화 중 Codex가 직접 검토한 설명의 문제는 `annotate_semantic_review.py`로 별도 보조 점수에 기록한다. 이 도구는 심사기 호출을 수행하지 않고 제공받은 판정과 짧은 사유 코드만 저장한다. `annotator_kind=LLM`, `evaluator=codex_assistant_review`, `human_validated=false`, `independent_evaluation=false`, `release_gate=false`, `separate_judge_call=false`를 고정한다. 사람 검토로 표시하거나 CODE 점수 8개와 합산하지 않는다. 예를 들어 `no_personal_data_request`는 '생년월일' 단어 유무 대신 실제 문맥에서 개발 채팅에 개인정보를 요청하는지를 보조 검토하는 기준이다.

피드백은 다음 순서로 반복한다.

1. 동일 자료·프롬프트·schema 버전의 기준선과 모델 실행을 각각 보존한다.
2. Phoenix에서 root/model/tool span, 오류, 지연, 토큰, CODE 점수를 읽는다.
3. `feedback_actions()`가 모은 실패 원인 중 하나를 수정한다. 실패 입력은 회귀 사례에 추가한다.
4. 같은 입력으로 재실행하고 이전 실행과 비교한다. 합격 기준을 낮춰 이전 실패를 지우지 않는다.
5. 개발자가 설명과 근거의 대응을 검토한다. 기관 담당자 검증 또는 독립 잠금 평가와 구별한다.

## 검증 명령과 운영 전 남은 과제

업무 DB와 LangGraph 체크포인트의 같은 호스트 복구에는 별도 CLI를 사용한다. API/worker 입력을 멈추고 진행 중인 작업의 종료를 확인한 뒤 실행한다. 실행 중인 작업이 있으면 백업을 거부하며, 두 DB에 대한 쓰기를 막은 동안 SQLite backup API로 복사한다. 두 파일이 같은 경로이거나 검토 중인 실행의 체크포인트가 없는 조합도 거부한다.

```sh
agent/.venv/bin/python -m donghaeng_agent.backup backup \
  --business-db agent/runtime/investigations.sqlite3 \
  --checkpoint-db agent/runtime/checkpoints.sqlite3 \
  --output agent/runtime/recovery-snapshot
agent/.venv/bin/python -m donghaeng_agent.backup restore \
  --backup agent/runtime/recovery-snapshot \
  --output agent/runtime/recovery-restored
```

실제 설정된 DB 경로로 바꾸고 출력 디렉터리는 아직 존재하지 않는 경로를 지정한다. 복원 시 SHA256·SQLite 무결성·DB 쌍을 확인하고 기존 파일은 덮어쓰지 않는다. 복원된 `business.sqlite3`와 `checkpoints.sqlite3`를 별도 개발 인스턴스에 연결해 검토 재개를 확인한 뒤 전환한다. 해시는 손상 검출용이며 공격자의 파일·manifest 동시 변조를 인증하는 서명은 아니다. 백업 접근 제한·암호화·외부 보관은 배포 환경의 별도 운영 항목이다. 이 CLI는 Phoenix 저장소를 포함하지 않는다.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 agent/.venv/bin/python -m pytest \
  agent/tests/test_observability.py agent/tests/test_phoenix_feedback.py \
  agent/tests/test_phoenix_launcher.py agent/tests/test_phoenix_readback.py \
  agent/tests/test_semantic_review_annotation.py -q
```

Phoenix 패키지는 pytest 플러그인을 자동 등록하며 import 시 기본 홈 디렉터리를 생성할 수 있어 이 단위검사에서는 플러그인 자동 로딩을 끈다. Phoenix 서버/SDK를 직접 사용하는 명령에는 `PHOENIX_WORKING_DIR`를 업무 폴더로 명시한다.

기관 배포 전에 인증·권한·조직 격리, PostgreSQL, 영속 collector와 누락 경보, DB 백업·복원, 보존·삭제, TLS·egress 제한, 장애·부하·복구 시험, 세부 권한과 라이선스 검토를 완료해야 한다. 로컬 무인증 개발 실행기는 기관 공용 서버에 노출하지 않는다. SQLite 복사 백업은 서버를 정지하거나 SQLite backup API로 일관된 snapshot을 만든 뒤 별도 경로에서 복원 검증한다.

확인한 근거: [Phoenix OTel 설정](https://arize.com/docs/phoenix/tracing/how-to-tracing/setup-tracing/setup-using-phoenix-otel), [Phoenix 서버 설정](https://arize.com/docs/phoenix/self-hosting/configuration), [평가 결과 저장](https://arize.com/docs/phoenix/tracing/how-to-tracing/feedback-and-annotations/llm-evaluations). gRPC 비활성화 어댑터와 버전별 설정은 설치된 Phoenix 20.23.0의 `server/app.py`, `server/grpc_server.py`, `config.py`를 확인했다.
