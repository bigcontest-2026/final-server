# Task 3 — 비교 근거의 원인 조사 전달

완료: 2026-10-25. 커밋·푸시·배포 없이 지정된 파일에만 구현했다. 실제 runtime DB, 외부 LLM, 외부 데이터베이스, 네트워크는 사용하지 않았다. 검증은 합성 제공자와 pytest 임시 SQLite 저장소를 사용한다.

## 구현과 계약

- `POST /api/model/analysis-reports/{id}/investigate`는 기존 `{}` 및 본문 생략과 함께 선택적 `comparison_report_ids: string[]`를 받는다. 최대 3개이며 엄격한 문자열 배열, 빈 값/공백 ID, 과도하게 긴 ID, 중복 ID를 거부한다. 임의 scope/provider/관측값은 기존처럼 입력할 수 없다.
- 서버가 각 ID의 실제 저장 보고서를 읽어 같은 관측월·성·연령·공간단위(sgg/administrative_dong)인지 확인한다. 전체 provenance, 분류 규칙, 임계값이 같아야 하며 activity/survey의 run_id와 manifest SHA가 있어야 한다. 실제 활동 관측이 없는 보고서도 비교할 수 없다.
- 누락 보고서는 404 `analysis_comparison_not_found`; 자기 계열 또는 동일 지역은 422 `analysis_comparison_self`; 같은 계열/지역의 중복은 422 `analysis_comparison_duplicate`; 조건·버전 불일치 또는 관측 부재는 422 `analysis_comparison_incompatible`이다. 검증 실패는 조사 제공자를 호출하지 않는다.
- 비교 없이 조사한 기존 결과는 기존 prompt-version lease 키를 유지한다. 비교가 있으면 정렬된 ID와 서버 소유 스냅샷의 SHA-256을 붙인 별도 lease 키를 사용한다. 선택 순서만 다르면 같은 결과를 재사용하고, 다른 비교 조합은 실행 중 lease와 완료 결과 모두 분리된다. 기존 store schema/lease 메서드는 바꾸지 않았다.
- 파생 보고서에서 명시적으로 비교 선택을 바꾸면 원본 관측 보고서를 부모로 하는 새 형제 보고서를 만든다. 기존 관측 보고서·다른 조사 보고서의 관측/컨텍스트/출처는 수정하지 않는다.
- 서버가 검증한 비교 관측을 제공자 입력에 추가한다. 제공자가 입력을 변경해도 별도 복사본만 바뀌며, 저장할 비교 스냅샷은 서버가 다시 고정한다. 비교 자료는 외부 공식 출처나 인과 입증으로 계산하지 않도록 조사 지시를 추가했다.
- Markdown에도 비교 지역·범위·저장 시각·자료/규칙 버전을 남긴다. 기존 외부 문구 escaping을 사용한다. 원래 scope/provenance/summary/assessments/context/rule_version/threshold_percent/품질·운영 경보 계약은 유지된다.

## Frontend 전달 형태

파생 보고서 최상위와 `context_investigation.comparison_context`, 제공자 payload의 `comparison_context`에 다음 스냅샷을 둔다. 비교가 없으면 새 조사 결과의 값은 `{}`이며, 기존 저장 보고서에 필드가 없어도 계속 사용할 수 있다.

```json
{
  "report_ids": ["정렬된 선택 보고서 ID"],
  "reports": [{
    "report_id": "선택한 저장 보고서 ID",
    "created_at": "원래 저장 시각",
    "scope": {"region_code": "...", "month": "...", "sex_code": "...", "age_code": "..."},
    "region_name": "지역명",
    "rule_version": "...",
    "threshold_percent": 5.0,
    "provenance": {"activity": {}, "survey": {}, "lifecycle_guide": {}},
    "observations": [],
    "context": {},
    "quality_status": "...",
    "operational_alert_allowed": false,
    "activity_availability": {}
  }]
}
```

`scope.dong_code` 및 `activity_availability`는 해당 저장 보고서에 있을 때만 포함한다. observations는 선택 보고서의 assessments를 그대로 복사한다. 비교 대상이 파생 보고서여도 선택한 문서 ID/저장 시각을 보존하되, 계열 중복 검사는 원본 조상 기준으로 수행한다.

Ruling: 파생 보고서에 `{}`로 재조사하면 그 보고서의 고정된 비교 선택을 유지한다. `comparison_report_ids: []`는 비교 없는 조사로 명시적으로 전환한다. 기존 비교 결과를 다시 열었을 때 조용히 비교를 지우지 않기 위한 판단이다. 잘못된 판단이면 UI가 빈 선택으로 전환할 때 명시적 빈 배열을 전송해야 하는 비용이 있다. 이 계약은 root 작업자에게 전달했다.

Ruling: 자료 버전은 activity/survey만 부분 비교하지 않고 저장된 전체 provenance를 동일 비교한다. 해석 가이드 등 다른 저장 근거의 버전도 섞이지 않기 위한 판단이다. 엄격한 비교로 인해 활동 자료가 같아도 가이드 버전이 다른 기존 보고서는 새로 저장해야 한다.

Ruling: 비교가 없는 기존 조사 결과의 재사용을 보존하기 위해 prompt version은 유지하고 비교 스냅샷이 있는 요청만 별도 lease namespace로 나눈다. 비교 관련 지시의 변경이 기존 비교 없는 조사 해석까지 바꿔야 한다면 추후 prompt version을 올려야 한다.

## TDD 증거

생산 코드 변경 전에 새 회귀 테스트를 작성하고 실행했다.

```sh
.venv/bin/python -m pytest service/backend/tests/test_analysis_research_api.py agent/tests/test_analysis_research.py -q
```

RED: **15 failed, 78 passed** (`/private/tmp/hermes-task3-red.log`). API가 새 필드를 거부하고, 비교 제공자 payload가 존재하지 않아 새 동작에 도달하지 못했다.

실패한 새 행동 시험:

- `test_comparison_saved_observations_and_provenance_are_frozen_without_changing_base`
- `test_comparison_incompatible_saved_report_never_calls_model`의 month, sex_code, age_code, spatial_unit, rule_version, threshold_percent, activity_version, survey_version, missing_version, no_observations 10개 경우
- `test_comparison_missing_self_and_duplicate_family_are_rejected`
- `test_changed_comparisons_do_not_reuse_completed_report_and_child_can_change_selection`
- `test_distinct_comparison_requests_have_independent_running_leases`
- `test_server_verified_comparison_observations_reach_provider_and_survive_mutation`

GREEN: 같은 명령 **93 passed** (`/private/tmp/hermes-task3-green.log`). 새 요청 형식의 잘못된 자료형·빈 값·최대 개수·중복 검증 7개 경우, 기존 조사 불변/실패 후 재시도/잠금/보안/생애주기/출처 시험도 함께 통과했다. 별도 fake provider로 실제 에이전트의 payload 전달·결과 고정·입력 변형 방어를 검증했다.

## 전체 회귀

패키지의 모든 시험을 실행했다. 현재 저장소는 모듈 검색 경로를 명시해야 한다.

```sh
# cwd: hermes-complete/agent
PYTHONPATH=../model:../model/src:src ../.venv/bin/python -m pytest -q
# 403 passed, 13 skipped, 65 subtests passed in 7.05s

# cwd: hermes-complete/service/backend
PYTHONPATH=../../model:../../model/src:../../agent/src:. ../../.venv/bin/python -m pytest -q
# 372 passed, 17 skipped, 14 subtests passed in 35.38s
```

최종 로그: `/private/tmp/hermes-task3-agent-full-configured.log`, `/private/tmp/hermes-task3-backend-full-configured.log`.

초기 실행의 경로 설정 문제도 기록한다. 루트에서 두 suite를 합친 최초 명령은 `app`을 찾지 못해 conftest 수집 단계에서 종료했다. 패키지 폴더에서 환경 변수 없이 실행한 agent 전체 시험은 `final_model` import 부재로 `IntentTests.test_confirmed_needs_are_echoed_when_research_mode_is_not_enabled`, `test_invalid_confirmed_needs_fail_before_model_or_database_queries`, `test_local_confirmed_needs_hold_instead_of_falling_back_to_i5_search`, `test_local_empty_confirmed_needs_preserves_existing_erd_search`, `test_publication_switch_between_pages_never_mixes_candidates`, `test_real_catalog_candidates_keep_service_and_observation_evidence_across_pages`, `test_remote_confirmed_needs_searches_only_confirmed_topics_with_or_without_decline`, `test_run_below_threshold_returns_without_searching_welfare`, `test_run_queries_exact_previous_month_and_preserves_remote_mapping_hold`에서 실패했다(하위 경우 포함 19 failed; 일부 정리 코드 UnboundLocalError도 함께 발생). 같은 초기 backend 실행은 `donghaeng_model` import 부재로 `test_investigation_evidence.py`, `test_observability_lifecycle.py`, `test_security.py`, `test_security_negative_review.py` 수집 오류 4개가 발생했다. 위 명시적 PYTHONPATH 재실행에서 모두 해소되었으며 생산 코드로 경로 문제를 우회하지 않았다.

## 변경 파일과 확인

1. `service/backend/app/analysis_api.py`: 요청 모델, 저장 근거/계열/범위/버전 검증, 비교별 lease 키, 서버 스냅샷 고정.
2. `agent/src/donghaeng_agent/analysis_research.py`: 서버 비교 근거 payload·조사 지시·결과 보존.
3. `agent/src/donghaeng_agent/analysis_reports.py`: 파생 보고서 최상위 비교 스냅샷과 Markdown.
4. `service/backend/tests/test_analysis_research_api.py`: 위 API/불변/재사용/잠금 회귀.
5. `agent/tests/test_analysis_research.py`: 실제 연구 payload 전달 및 변형 방어.
6. 이 보고서.

`analysis_report_store.py`는 Task 1 소유자에게 현재 lease API/지문을 보존하도록 협의했으며 직접 수정하지 않았다. 지정한 생산 파일의 변경은 `/private/tmp/hermes-report-flow-kftxc4s_/baseline`과 비교해 확인했다. 파일들이 작업 시작 전부터 untracked라 `git diff`만으로는 변경 검토가 되지 않는 점도 확인했다.

## 한계

- 실제 외부 모델의 설명 품질, 공식 사이트 네트워크 접근, 실제 runtime DB 업그레이드/통합은 이번 검증 범위 밖이다.
- 프런트 선택·렌더링·실제 브라우저 시험은 root Task 4가 맡는다. 본 Task는 계약과 후단 동작을 구현·검증했다.
- 기존 모델 입력 크기 제한을 유지하며 비교 관측을 조용히 자르지 않는다. 최대 3개라도 입력이 기존 한도를 넘으면 기존 실패 계약으로 중단한다.
- 전체 시험의 skip은 기존 선택적 통합/환경 의존 항목이며 실행됐다고 주장하지 않는다.
