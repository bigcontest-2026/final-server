# HERMES Report Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 감사에서 확인한 분석 연결·저장·비교 공백을 해결하고 공통 보고서 UX를 완성한다.
**Architecture:** 기존 통합 HTML과 별도 JS, FastAPI 저장 API를 보완한다. 분석 계열의 작업공간과 불변 보고서 스냅샷을 구분한다.
**Tech Stack:** JavaScript, Vitest, Python, FastAPI, SQLite, pytest, Vite.
**Spec:** docs/superpowers/specs/2026-10-25-report-flow-design.md

## Global Constraints
- 기존 미커밋 변경과 실제 runtime DB를 덮어쓰거나 삭제하지 않는다.
- 원본 보고서와 완성 예방 보고서는 불변이며, I5·활동 단위·품질 보류·운영 경보 불허 계약을 유지한다.
- 외부 LLM 호출·DB 쓰기·기관 연락 없이 합성 제공자와 임시 DB로 검증한다.
- 의존성을 추가하지 않고 현재 스택과 테스트 도구를 사용한다.
- UI에 내부 식별자·DB·구현 상세를 불필요하게 노출하지 않는다.

## Review Focus
- 원본과 파생 보고서 양쪽에 기존 대응안이 있거나 여러 탭이 열렸을 때 내용 보존.
- 원인 조사 이후 생성 보고서가 실제로 사용한 분석 스냅샷 보존.
- 화면 조건만 변경하거나 오래된 대화를 재개할 때 잘못된 연결 방지.
- 비교 지역의 월·집단·공간단위·자료 버전 불일치 및 서로 다른 비교 캐시.
- 임시 초안이 다른 분석이나 최신 서버 저장본을 덮어쓰지 않으며 재가져오기에도 수정 유지.

### Task 1: 분석 계열 대응안 연결
**Files:** agent/src/donghaeng_agent/{analysis_report_store,response_workspace}.py; service/backend/app/response_api.py; service/backend/tests/test_response_api.py.
**Interfaces:** 기존 요청 URL과 클라이언트의 요청 analysis_report_id 일치 계약을 보존한다. 필요한 공통 작업공간 ID는 추가 필드로 제공하고 불변 스냅샷은 실제 요청한 분석을 유지한다.
- [x] 조사 전후 저장안 유지·기존 양쪽 작업공간·버전 충돌 회귀 검사 작성 및 실패 확인.
- [x] 공통 계열 해석과 비파괴 저장·조회 구현.
- [x] 관련 API·저장 시험 통과 및 변경 diff 검토.

### Task 2: 기존 대화 초안 범위 보호
**Files:** service/shared/hermes-complete.html; service/frontend/scripts/hermes_analysis_reports.py (또는 전용 패치 모듈); service/frontend/tests/reportScope.test.ts.
**Interfaces:** 기존 HERMES_PAGES API와 import_hermes.py 재생성 결과를 유지한다. root 작업자는 이 Task 완료 전 HTML을 수정하지 않는다.
- [x] 실제 지역·월·성·연령 변경 후 이전 사건에 저장하지 못하는 회귀 검사 작성·실패 확인.
- [x] 조건 검사·분석 재선택 UI와 재생성 패치 구현.
- [x] 기존 정상 초안·대화 재개 동작 시험 및 diff 검토.

### Task 3: 비교 근거의 원인 조사 전달
**Files:** service/backend/app/analysis_api.py; agent/src/donghaeng_agent/{analysis_research,analysis_reports,analysis_report_store}.py; service/backend/tests/test_analysis_research_api.py; agent/tests/test_analysis_research.py.
**Interfaces:** POST /api/model/analysis-reports/{id}/investigate는 선택적 comparison_report_ids 문자열 배열(최대 3개)을 받는다. 서버가 저장된 보고서와 같은 월·집단·공간단위·자료 버전을 확인해 조사 입력과 결과의 comparison_context에 보존한다. 비교 없음은 기존 {} 호환. Task 1과 겹치는 analysis_report_store.py 변경은 별도 협의한다.
- [x] 비교 입력 전달·잘못된 조건·서로 다른 비교의 재사용 분리 검사 작성·실패 확인.
- [x] 검증·출처 보존·조사 저장 구현.
- [x] 기존/새 조사 시험과 diff 검토.

### Task 4: 공통 보고서 UX·초안 복구
**Files:** service/shared/hermi-{analysis-reports,response-workspace,overview,desktop}.js와 관련 CSS; service/frontend/tests/{reportBridge,responseWorkspace,contextAnalysisReports,hermiWelfareChat}.test.ts; 필요 시 별도 순수 helper 파일.
**Interfaces:** Task 1의 기존 클라이언트 호환 계약, Task 3의 comparison_report_ids와 comparison_context를 사용한다. 복지 보고서 CTA는 기존 attachProgramToReport 흐름을 재사용한다.
- [x] 사업 CTA 연결·분석별 복구/충돌·비교 선택 전달 회귀 검사 실패 확인.
- [x] 사업 설명 대화와 예방 보고서 CTA 구분, 공통 이력·분석 조건·선택적 다음 단계 UI 구현.
- [x] 임시 저장·복구 선택·이탈 보호·저장 완료 정리 구현.
- [x] 비교 보고서 선택·원인 조사 연결과 비교 근거 표시 구현.
- [x] 전체 프런트 시험·빌드, 백엔드 시험, 실제 로컬 화면과 최종 리뷰.
