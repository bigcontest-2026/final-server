# 에이전트 구현과 Phoenix 피드백 기록

2026-10-21 · 개발 검증 · 기관 운영 승인 아님

기존 ChatGPT 구독으로 실제 공개 집계·공식 지원 자료를 처리하는 에이전트를 연결했다. 서버가 숫자·근거·조건·허용 도구를 검증하고 LangGraph가 브리프 저장과 사람 검토 대기를 관리한다. UI는 수정하지 않았다.

## 실제 실행과 수정

Phoenix 20.23.0을 HTTP `127.0.0.1:6006`에만 실행했다. 기본 gRPC가 전체 인터페이스에 bind하는 것을 발견해 버전이 고정된 개발 실행기에서 사용하지 않는 listener를 비활성화했다. `lsof`로 HTTP loopback listener와 gRPC listener 부재를 확인했다. 업무·체크포인트·Phoenix는 별도 SQLite다.

| 실행 | 실제 모델 호출 | Phoenix 계약 검사 | 결과와 조치 |
| --- | ---: | ---: | --- |
| initial-subscription-2 | 성공 0 | 2/8 | CLI의 기능 비활성화 알림을 비정상 도구로 오인. 무시할 수 있는 알림을 검증한 CLI 버전에서만 좁게 허용하고 미인식 설정 제거 |
| subscription-parser-fix-1 | 1 | 8/8 | 실제 요약→브리프→검토 대기. 정상 GraphInterrupt가 오류 span으로 남는 문제 발견 |
| agent-tools-1 | 4 | 8/8 | 모델이 공식 검색→상세 취득→조건 검사→설명을 선택. 서버 검증 후 브리프 저장 |
| baseline-interrupt-fix-1 | 0 | 8/8 | 모델 없는 규칙 기준선. 검토 대기를 정상 제어 흐름으로 기록하고 오류 span이 없음을 확인 |
| privacy-prompt-21-1 | 1 | 8/8 | 개인 원문 입력을 요구하지 않고 담당 기관의 별도 확인 절차로 안내하도록 프롬프트 2.1 적용 |
| final-observed-summary-1 | 1 | 8/8 | 최종 코드의 공개 자료 요약. 답변·근거 ID·토큰·버전 해시를 Phoenix에 저장, 오류 span 0 |
| sanitized-agent-final-1 | 4 | 8/8 | 입력 마스킹·최소화가 적용된 도구 선택 실행. 검색·상세·조건 검증 후 설명, 22개 span 모두 저장, 오류 span 0 |

총 7개 trace, 134개 span, 성공한 구독 모델 span 11개를 Phoenix 저장 DB에서 재조회했다. 이 수치는 위 평가 실행에 한정하며 계정 전체 사용량이나 별도의 연결 진단 호출은 포함하지 않는다. 실패 실행을 삭제하거나 성공 수치에 포함하지 않았다.

마지막 실행의 trace ID는 `ba2423a4672689d8cdb473934828d59a`, root span ID는 `fbfdecddafdcbe15`다. 네 호출에 대해 Codex가 보고한 입력 합계 51,143·출력 630 토큰을 저장했다. 모든 모델 span에 정제된 설명·도구명·근거 ID가 저장됐고, 부모 span 누락이 없다. 구독 호출의 `cost_usd`는 `null`이며 API 단가 환산을 청구액으로 표시하지 않는다. 모델 이름은 계정 기본값을 사용했으므로 정확한 모델 snapshot을 고정한 잠금 평가가 아니다.

계약 검사 8개는 완료 상태, 근거 참조, 요청 범위, 지원 조건, 해석 제한, 실제 모델 실행 증거, 구독 비용 구분, 후속 조치 경계를 확인한다. 숫자·문장 전체의 정답성이나 현장 효용을 모두 측정하는 점수가 아니다.

평가 코드 검토에서 실제 I5 유형의 보류 상태 검사와 실행별 근거 소유권 검사가 빠진 것을 발견했다. 기준 1.1은 SQL이 확인한 `case_id`·`execution_id`에 속한 근거만 허용하고 모델 인용·관측 인용까지 검사한다. 실제 I5와 기존 I5 모두 명시적 `hold`를 요구한다. 원래 실행 파일을 유지하고 마지막 사건을 DB에서 다시 내보내 재평가했으며, 추가 모델 호출 없이 8/8을 통과했다. Phoenix에 갱신된 기준 1.1·8개 점수가 저장된 것도 재조회했다.

합성 회귀 데이터셋은 1.0의 7개를 유지하고 1.1의 12개를 별도로 등록했다. 실제 저장된 입력·메타데이터·해시와 예상 실패 판정이 12/12 일치했다. 이는 잘못된 입력을 평가 코드가 거부하는 검사이며, 모델 답변 12개가 모두 옳다는 결과가 아니다.

개인정보 요청 표현은 개발 중인 Codex의 보조 의미 검토에서 문제를 발견했다. 수정 전 0·수정 후 1을 별도 `LLM` annotation으로 기록했다. `evaluator=codex_assistant_review`, `human_validated=false`, `independent_evaluation=false`, `release_gate=false`이며 CODE 점수와 합산하지 않는다. 사람·기관 전문가 평가를 대신하지 않는다.

## 구현한 기반

- **모델 실행:** Codex CLI 0.163.0 구독 인증, 공통 JSON 결정 계약, 명시적 API 제공자 선택, 자동 유료 전환 금지, 입력·출력 크기 제한, 모델 동시 실행 1, 타임아웃·취소 시 자식 프로세스 종료와 회수.
- **입력 최소화:** 전송 전에 인증정보·개인 원문·불필요한 자유문을 제거하고 개인정보 패턴을 마스킹. JSON 내부도 재귀 처리하며 수치·근거 ID·버전을 보존하고, 크기·깊이 초과는 거절한다. 임의 문장이 공개 자료임을 판정하는 분류기는 아니므로 공개 집계·승인된 자료 계약을 함께 적용한다.
- **업무 검증:** 모델 설정·프롬프트·schema·자료 버전 고정, 미등록 도구·후보·근거 거부, 상세 확인 전 조건 판정 거부, 호출·검색·전체 작업 시간 제한.
- **자료 연결:** 전국 I5의 실제 값·원본 해시·정확한 행정동 범위, 시군구 활동 근거의 별도 계약, `hold`·`operational_alert_allowed=false` 유지. 실제 활동 번들이 없으면 명시적으로 사용 불가를 반환.
- **작업 복구:** 갱신 가능한 lease, 소유권 토큰, 업무와 체크포인트의 늦은 쓰기 차단, 멱등성·검토 재개·종료 중 복구. 두 DB의 일관된 백업, 해시·무결성·체크포인트 소속 검사와 새 디렉터리 복원 CLI를 추가했다. 같은 호스트 SQLite 범위다.
- **관측:** 실행→그래프→도구→모델 계층, 캐시와 실제 호출 구분, 토큰 미보고 상태 구분, 원본 진단·인증·내부 추론 전문 제외, 한정된 비동기 큐와 전송 실패 계량.
- **기관 접근 기반:** RS256 JWT 검증, 기관·읽기·조사·검토·관리 권한, 실제 검토자/후속 기록 행위자 저장, 업무·체크포인트 DB 기관 고정. 기관 모드는 승인된 모델이 없으면 LLM 전략을 거부한다.
- **개발 접근:** loopback 접속·Host 검증과 proxy 헤더 거부. 개인 구독 실행은 이 개발 범위에서만 허용한다.
- **회귀 검사:** agent/model 필수 설치, 필수 suite 누락 및 예상 밖 skip 거부, 모델을 호출하지 않는 CI 실행 경로와 JUnit 근거 저장.

마지막 실제 공개 자료 실행의 업무 DB와 체크포인트를 백업하고 다른 디렉터리에 복원했다. 복원본에서 `reviewer_type=developer_verification`으로 추가 확인(`request_info`)을 요청한 뒤 그래프가 `succeeded`, 사건이 `awaiting_info`에 도달했다. 브리프는 1개로 유지됐고 검토 1개만 저장됐다. 이 과정의 모델 호출은 0회이며 원본 사건은 검토 대기 상태로 그대로 남았다. 기관 담당자의 승인이나 실제 지원 결과로 기록하지 않았다. Phoenix 자체 DB 복원·다중 호스트 장애·기관 RPO/RTO는 이 검사에 포함하지 않는다.

## 최종 로컬 검사

`agent/.venv/bin/python service/scripts/check_backend_ci.py`로 에이전트와 서비스의 오프라인 통합 회귀 검사 **311개 통과, 실패 0, skip 0**을 확인했다. 제공자 부정 입력·취소·타임아웃, 자료 범위, 기관 인증·권한, 실행 소유권, 근거 소유권, 마스킹, 관측 장애, 평가 반례, 백업·복원·검토 재개가 포함된다. 외부 모델 호출을 차단한 검사이며 GitHub Actions 원격 실행 결과는 아니다.

근거는 `service/.artifacts/backend-ci/junit.xml`과 `summary.json`에 저장했다. `pip check`는 의존성 충돌 없음, agent/service의 `git diff --check`는 통과했다. 사용자 구독의 한도 소진이 발생하지 않아 초기화권을 사용하지 않았다. 코드·문서는 로컬 수정 상태이며 commit·push·기관 배포는 수행하지 않았다.

## 재현과 근거 위치

- [최신 계획](AGENT_ENTERPRISE_PLAN.md)
- [Phoenix 실행·보존·장애 경계](PHOENIX_OPERATIONS.md)
- [통합 검사 실행기](../../service/scripts/check_backend_ci.py)
- [실제 평가 실행기](../evaluation/run_feedback_iteration.py)
- [Phoenix 저장 결과 재조회기](../evaluation/read_phoenix_feedback.py)
- [업무·체크포인트 백업/복원 CLI](../src/donghaeng_agent/backup.py)
- 로컬 실행 원장: `agent/runtime/enterprise-feedback/<iteration>/case.json`, `feedback.json`
- 로컬 재조회 근거: `agent/runtime/enterprise-feedback/feedback_readback.json`
- 평가 기준 1.1 재검사: `agent/runtime/enterprise-feedback/sanitized-agent-final-1/feedback-rubric-1.1.json`, `annotation-readback-1.1.json`
- 합성 데이터셋 저장 검증: `agent/runtime/enterprise-feedback/regression_dataset_1_1_readback.json`
- 실제 복원·검토 재개: `agent/runtime/enterprise-feedback/sanitized-agent-final-1-restored/recovery-verification.json`
- 관측 DB: `agent/runtime/phoenix/phoenix.sqlite`

실제 평가 원장은 Git에 포함하지 않는다. 공개 데이터 개발 실험이며 내부망의 민감 업무 데이터 사용 승인을 의미하지 않는다.

## 남은 필수 단계

1. 실제 활동 번들 연결, 공식 검색 범위 확대·외부 맥락·집단 비교·정책 공백·정책 초안 등 미완성 업무 기능.
2. 사전 기준을 고정한 독립 평가와 담당자 효용 검증. 현재 성공 사례 1개 지역의 반복은 전국 업무 품질 검증이 아니다.
3. 승인된 내부 모델 제공자와 실제 기관 SSO, 사건별 접근 범위, 키 순환·폐기·감사 보존 운영.
4. PostgreSQL 업무·체크포인트·Phoenix 전환과 다중 호스트 worker, 내구성 있는 관측 재전송, 부하·장애·복원·롤백 시험.
5. 기관별망·자료 반입·보존 삭제·운영 인수 기준 검증. 외부 동작은 아직 비활성화이며 담당자 수락은 실제 지원 완료가 아니다.

현재 결과는 로컬에서 실제 모델과 Phoenix 피드백을 연결한 개발 기반이다. 엔터프라이즈 운영 완료·무오류·성능 보증으로 표현하지 않는다.
