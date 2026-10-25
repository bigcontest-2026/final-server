# Task 1 — 분석 계열 대응안 연결

## Result

원본 현황 보고서와 파생 원인 조사 보고서가 같은 대응안·수정 이력·완성 보고서 목록을 사용한다. URL로 요청한 분석 ID와 응답 `analysis_report_id`의 일치 계약은 유지한다. 공통 저장 기준은 추가 필드 `workspace_analysis_report_id`로 제공한다.

## Files changed

- `agent/src/donghaeng_agent/analysis_report_store.py`: 저장된 부모 관계로 원본·자손을 찾는 `family(report_id)` 추가. 하나의 읽기 스냅샷으로 해석하며 끊어진 관계나 순환은 오류로 처리한다. 기존 조사 임대·저장·fingerprint 메서드 변경 없음.
- `agent/src/donghaeng_agent/response_workspace.py`: 계열 구성원 매핑과 일회성 비파괴 병합, 계열 공통 버전·수정 이력·보고서 조회, 생성 보고서의 실제 요청 분석 스냅샷과 공통 작업공간 링크 유지.
- `service/backend/app/response_api.py`: 검증된 계열 정보를 저장소에 전달하고 기존 요청 URL 계약 유지. 기존 대응안 수 제한은 저장소에서 검사하여 병합 결과가 20개를 넘더라도 수정·보고서 생성 가능.
- `service/backend/tests/test_response_api.py`: 조사 전후 공유, 기존 양쪽 저장 내용·이력·완성 보고서, 충돌, 재시작, 같은 대응안 ID의 서로 다른 분기, 20개 초과 병합, 파생 보고서 재사용, 새 형제·후속 조사 회귀 검사.

## Root cause and interface

기존 저장소는 현황 분석과 파생 원인 분석의 서로 다른 ID를 그대로 작업공간 키로 사용했다. `AnalysisReportStore.family()`가 원본 ID와 모든 파생 ID를 반환하고, API가 이를 작업공간 저장소에 전달한다. 작업공간 응답은 요청 ID를 반환하되 저장·버전·수정 이력은 원본 ID로 공유한다. 보고서 목록의 `analysis_report_id` 필터도 같은 계열 전체를 조회한다.

생성 보고서의 `analysis_report_id`와 `analysis_snapshot`은 실제 요청한 원본 또는 파생 보고서 그대로다. `workspace_analysis_report_id`는 공통 작업공간을 가리킨다. 기존 완성 보고서와 분석 보고서는 수정하지 않는다.

## Migration and rulings

- Ruling: 기존 두 작업공간이 모두 채워져 있으면 한쪽을 선택하지 않고 대응안을 모두 합친다. 자식의 기존 행과 모든 기존 완성 보고서는 그대로 보존하고 수정 이력을 공통 공간에 복사한다. 구성원 매핑을 같은 SQLite 쓰기 트랜잭션에 저장하여 재시작 뒤 중복 병합을 막는다. 같은 대응안 ID의 다른 분기는 결정적인 새 대응안 ID로 보존한다. 비용: 기존에 사실상 같은 대응안을 양쪽에서 따로 저장했다면 둘 다 표시된다.
- Ruling: 기존 내용이 병합되면 작업공간 버전을 모든 이전 버전보다 크게 올린다. 먼저 열린 원본·파생 편집기의 저장은 409로 거부한다. 비용: 내용이 한쪽에만 있던 기존 편집기도 한 번 새로 불러와야 할 수 있다.
- Ruling: 신규 대응안은 기존 20개 제한을 유지하되, 이전 작업공간을 병합하여 이미 20개를 넘은 경우 그 수만큼의 수정·선택·보고서 생성을 허용한다. 기존 정적 요청 배열 제한은 보존된 내용을 저장할 수 없게 하므로 저장소의 현재 대응안 수 기준 검사로 옮겼다. 비용: 대규모 기존 계열의 저장 요청 크기는 실제 보존된 대응안 수에 따라 커질 수 있다.
- Ruling: 생성 보고서 fingerprint에 공통 작업공간 ID를 추가한다. 이전 자식 작업공간 ID가 붙은 완성 보고서를 수정하거나 잘못 재사용하지 않는다. 비용: 업그레이드 이후 최초의 명시적 생성은 내용이 같아도 새 보고서를 만들 수 있다. 이후 같은 요청의 재사용은 유지한다.

## RED → GREEN evidence

모든 명령의 기준 디렉터리는 `hermes-complete`다. 외부 제공자를 호출하지 않는 TestClient·합성 분석 자료와 pytest 임시 SQLite DB를 사용했다.

1. `.venv/bin/python -m pytest -q service/backend/tests/test_response_api.py -k 'researched_child or family_migration'`
   - RED: 2 failed. 파생 응답의 공통 ID 필드가 없고, 원본 조회에서 기존 자식 대응안이 누락됨.
   - 계열 해석·연결·병합 구현 후 전체 대응 API: 39 passed.
2. `.venv/bin/python -m pytest -q service/backend/tests/test_response_api.py -k 'over_twenty or colliding_legacy'`
   - RED: 1 failed, 1 passed. 보존된 24개 대응안의 저장을 정적 20개 요청 제한이 422로 거부함. ID 충돌 분기 보존 검사는 통과.
   - 현재 저장 개수 기준 제한으로 수정 후 전체 대응 API: 41 passed.
3. `.venv/bin/python -m pytest -q service/backend/tests/test_response_api.py -k legacy_child_report_reuse`
   - RED: 1 failed. 이전 자식 보고서를 재사용하여 공통 작업공간 ID가 자식 ID로 남음.
   - fingerprint에 공통 작업공간 ID 추가 후 통과. 직접 저장 반환의 일회성 `reused` 필드는 저장된 분석 스냅샷이 아니므로 검사는 실제 GET 분석 스냅샷과 대조하도록 정정.
4. 형제·후속 조사와 원본/파생 양방향 보고서 목록 검사 포함 최종 대응 API: 43 passed.

## Final verification

- `.venv/bin/python -m pytest -q service/backend/tests/test_response_api.py` → **43 passed**.
- `RUN_WELFARE_POSTGRES_TESTS=0 PYTHONPATH=agent/src:model/src:model:service/backend .venv/bin/python -m pytest -q service/backend/tests` → **374 passed, 17 skipped, 14 subtests passed** (32.95s).
- `RUN_WELFARE_POSTGRES_TESTS=0 PYTHONPATH=agent/src:model/src:model:service/backend .venv/bin/python -m pytest -q agent/tests` → **403 passed, 13 skipped, 65 subtests passed** (7.14s).
- 기준 스냅샷과 네 파일의 diff를 직접 검토했다. 작업 diff: `/private/tmp/hermes-task1.patch`; 전체 시험 출력: `/private/tmp/hermes-task1-backend-tests.log`, `/private/tmp/hermes-task1-agent-tests.log`.

첫 전체 시험 시도는 실행 경로 설정 문제로 실패했다. backend 하위 폴더의 `.venv/bin/python` 경로는 없었고, 그 폴더에서 실행한 대체 명령은 `donghaeng_model`을 못 찾아 네 모듈(`test_investigation_evidence`, `test_observability_lifecycle`, `test_security`, `test_security_negative_review`) 수집 오류가 났다. 프로젝트 루트의 초기 agent 전체 시험은 `final_model` 경로 누락으로 `test_welfare_pipeline_standalone.IntentTests`의 confirmed needs/local/remote/publication/catalog/threshold 관련 8개 검사와 하위 사례가 실패했다(19 failed 집계). 명시적 `PYTHONPATH`로 같은 전체 시험을 다시 실행하여 모두 통과했다. 제품 코드는 이 실행 환경 문제 때문에 수정하지 않았다.

## Remaining concerns

Task 1 범위에서 남은 실패는 없다. 실제 운영 DB를 열거나 마이그레이션하지 않았으며 외부 LLM·PostgreSQL·네트워크 요청, 커밋·푸시·재설정은 수행하지 않았다. 프런트 브라우저 임시 저장 키와 UI 연결은 Task 4가 추가 필드를 소비하도록 별도 진행한다. 마이그레이션은 이후 저장 요청이 새 저장소를 통한다는 일반적인 배포 전제를 사용한다. 이전 버전 프로세스가 동시에 자식 DB 행을 계속 쓰는 혼합 버전 운용은 검증하지 않았다.
