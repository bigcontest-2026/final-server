# Task 2 — 기존 대화 초안 범위 보호

## 결과와 원인

`setScope()`가 필터를 바꿔도 이전 `currentCase`를 유지하고, `ensureCase()`는 현재 사건이 없을 때 지역·월만 검사했다. 따라서 새 화면 조건과 이전 브리프를 합쳐 초안을 구성하고 이전 사건에 저장할 수 있었다.

선택한 분석의 최신 브리프 `model_evidence.scope`와 화면의 지역·월·성별·연령·공간단위를 모두 비교한다. 배열 순서·중복은 무시하며 전체 집단의 빈 배열과 전체 코드 목록은 동등하게 처리한다. 조건이 다르거나 분석 범위를 확인할 수 없으면 초안 구성과 연결 저장을 막는다. 저장 직전 사건을 다시 읽은 뒤에도 동일 조건 및 선택 사건을 재검사한다.

편집 화면에는 선택한 분석 조건이 보인다. 불일치 시 이유와 `선택한 분석 조건으로 돌아가기`를 표시하며, 분석을 임의로 바꾸거나 새 관측월로 옛 근거를 설명하지 않는다. 정상 분석 선택·재개·구성·저장은 유지한다.

필터나 분석 전환 전 입력을 분석별 `S.livePolicyDrafts`에 보존한다. 전역 `S.draft`를 다른 분석에 자동 이식하지 않는다. 기존 선택적 `persist()`와 이력 내보내기에도 새 보존 필드를 포함했고, 재로딩 복원 시험은 실제 `persist()` 함수를 실행한다. 성공적으로 연결 저장한 분석의 임시 보존본은 정리한다. 이는 레거시 초안 편집기 보호이며 Task 4의 공통 서버 대응안 작업공간 복구와 별개다.

## 변경 파일과 위치

- `service/shared/hermes-complete.html:317`: 분석별 입력 보존 필드 직렬화.
- `service/shared/hermes-complete.html:589`: 보존된 입력도 기존 이력 내보내기에 포함.
- `service/shared/hermes-complete.html:1448`: 범위 비교·검사·표시·입력 보존 helper.
- `service/shared/hermes-complete.html:1530`: 분석 선택 및 정확한 범위의 자동 재개.
- `service/shared/hermes-complete.html:1534`: 선택 분석 조건 안내와 보존 입력 표시.
- `service/shared/hermes-complete.html:1561`: 조건 복귀·생성·저장 guard.
- `service/frontend/scripts/hermes_draft_scope.py`: 정확한 기존 코드 경계를 검사하는 재생성 패치. 중복 실행은 같은 결과를 낸다.
- `service/frontend/scripts/hermes_analysis_reports.py:5,70`: 기존 가져오기 경로에 패치 연결.
- `service/frontend/tests/reportScope.test.ts`: 실제 통합 HTML 페이지 스크립트와 추적 소스에서 실제 패치로 재생성한 HTML 모두 실행.

## TDD와 검증

1. 최초 회귀 검사 실행: 40개 중 38개 실패, 정상 경로 2개 통과. 생성이 기존 편집 내용을 덮고, 필터 불일치 상태에서도 `/policy-drafts`로 쓰는 실패를 확인했다. 로그 `/private/tmp/task2-red.log`.
2. 수정 후 40/40 통과.
3. 실제 기존 `persist()`가 선택적 필드만 직렬화한다는 점을 확인하고 재로딩 보존 검사 추가: 42개 중 새 검사 2개 실패. 로그 `/private/tmp/task2-persist-red.log`.
4. 직렬화·복원 보완 후 `npm test -- --run tests/reportScope.test.ts`: **42/42 통과**. 로그 `/private/tmp/task2-green.log`.
5. 전체 `npm test` (23:55:53 KST): **362개 통과, 2개 실패**. 실패는 `analysisReports.test.ts > saved activity report boundary > keeps regeneration idempotent and the embedded adapter exactly synchronized`, `lifecycleGuide.test.ts > lifecycle interpretation view > keeps guide assets and frozen-guide checks after regeneration`. 병행 Task 4에서 바꾼 `hermi-overview.js`의 콘텐츠 해시가 현재 HTML 태그보다 새로워서 재생성 동등성 검사가 실패했다. 실제 diff는 해당 스크립트의 `?v=` 한 곳이다. 최종 통합 시 shared JS 수정 후 `patch_analysis_reports`를 한 번 적용해야 한다. 로그 `/private/tmp/task2-full-final.log`.
6. `npm run build`: **통과**. 로그 `/private/tmp/task2-build.log`.

기존 미커밋 변경은 초기 보존본과 대조했고, Task 2 변경 외에 되돌리거나 삭제하지 않았다. 외부 서비스 호출·LLM 호출·실제 runtime DB 쓰기·커밋·푸시·리셋은 실행하지 않았다. 본 담당 작업에서는 브라우저 시각 검증을 하지 않았으며 최종 통합 검증은 root 담당이다.
