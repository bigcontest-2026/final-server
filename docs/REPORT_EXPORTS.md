# 복지 대응계획 작성·출력

표준템플릿 v2를 반영한 새 보고서는 `welfare-plan-2.0` 문서 모델을 저장합니다. 화면·Markdown·PDF·HWPX가 같은 문서 내용을 사용하며, 이전 보고서는 저장 당시 Markdown만 변환합니다. 기존 보고서를 새 초안으로 덮어쓰지 않습니다.

## 작성 항목

`PlanInput.planning_details`는 선택적 항목 사전입니다. 각 항목은 `status`, `text`, `evidence`, `owner`, `next_action`으로 구성됩니다. 지원 키는 `planning_details.py`에 정의합니다. 기존 사업 활용에는 서비스 조건·공급·이용 경로를, 보완·신규안에는 대안·사업량을 보여주고 비용·재원은 공통 선택 항목으로 제공합니다.

확인 상태에는 내용과 근거, 해당 없음에는 사유가 필요합니다. 새 항목을 생략한 구형 요청은 기존 값을 보존합니다. AI는 미확인 제안만 반환하며 사용자가 항목을 골라 초안에 반영합니다. 사용자 확인 기록은 시스템 검증·기관 승인·예산 확보가 아닙니다.

## 다운로드 API

`GET /api/model/{analysis|response}-reports/{report_id}/export?format=md|pdf|hwpx`

- 기존 보고서 조회와 같은 읽기 권한을 적용합니다.
- 저장본만 사용하며 모델 호출·외부 변환 전송은 없습니다.
- PDF는 A4, 포함된 OFL 나눔고딕 폰트, Unicode 매핑과 페이지 번호를 사용합니다. 지원하지 않는 글리프는 422 `pdf_unsupported_glyph`로 거부하고 대체 형식을 안내합니다.
- 한글 다운로드는 편집 가능한 HWPX입니다. HWP 확장자로 이름만 바꾸지 않습니다.
- 성공은 첨부 파일, 변환 실패는 503 JSON 오류입니다. 파일 응답은 `private, no-store`를 사용합니다.
- `export_assets` 폰트와 라이선스는 Python 패키지 데이터에 포함됩니다. 추가 변환 프로그램·API 키가 필요하지 않습니다.

## 적용과 검증 범위

실행 중인 API는 Python 소스를 자동 재로딩하지 않으므로 변경 적용에는 재시작이 필요합니다. 로컬 실행은 서버 폴더에서 `.venv/bin/python scripts/dev.py stop` 후 `.venv/bin/python scripts/dev.py start`를 사용합니다.

PDF는 독립 렌더러의 페이지 렌더링·한글 추출·여백 검사까지 검증했습니다. HWPX는 패키지·XML·문서 내부 참조와 본문 보존을 검사하고 한컴 저장 문서의 기본 구성을 대조했습니다. 실제 한글 앱의 열기·편집·재저장 검증은 별도로 필요합니다. 관련 회귀 테스트는 `test_planning_details.py`, `test_report_exports.py`입니다.
