# Step 04 — 추천 그래프·API·웹 연결

2026-10-25. 기본 final 화면에서 지역·월·성별·연령 선택 → API → 공통 LangGraph → 실제 모델 자료와 가상 DB → 추천 표시를 연결했다.

## 에이전트

`welfare_graph.py`: recommend → verify_research_output. Step 03 파이프라인을 실행하고 운영 경보 금지를 확인한다.
`graph.py`: 기존 investigate 다음 recommend_welfare 노드 추가. 선택적 welfare_request 어댑터를 받는다.
결과는 도구 실행 이력과 brief.welfare_recommendations.groups에 남긴다.
`worker.py`: 어댑터를 그래프에 전달한다.

legacy_main은 로컬 개발 + 가상 DB 존재 시 어댑터를 연결한다. 공개 I5 행으로 행정동의 상위 시군구를 확인한다.
기존 조사 요청에는 성별이 없으므로 MAN/WMAN을 각각 실행하고 그룹별 결과를 보존한다.
기관 모드에서 이 레거시 가상 데이터 연결을 자동 활성화하지 않는다.
이번 실증 검증은 final 화면의 공통 추천 그래프 경로이다. 기존 조사 사건의 LLM·승인·체크포인트 전체 회귀시험은 실행하지 않았다.
추천 DB 변경 시 기존 조사 실행의 자료 버전 고정 계약까지 확장하는 작업은 후속이다.

## API

GET `/api/model/welfare?region_code=11680&month=202508&sex_code=MAN&age_code=20G&as_of=2026-10-25`

- 기존 보안 미들웨어 적용. 서버 경로는 클라이언트에서 지정할 수 없다.
- 지원하지 않는 조건/잘못된 지역·월·집단은 422, 자료·실행 장애는 503.
- as_of 생략 시 Asia/Seoul 오늘 날짜. 관측 월과 사업 기준일 구분.
- 기본 DB: agent/runtime/welfare_demo/welfare_erd_v2.sqlite, publication erd-demo-v2.
- `WELFARE_DB_PATH`, `WELFARE_PUBLICATION_ID`로 서버 설정 가능.
- 로컬 개발은 연구 모드·가상 자료 허용. 기관 모드는 WELFARE_RESEARCH_ENABLED=true를 별도로 요구한다.
  기관에서 가상 자료 사용은 WELFARE_ALLOW_SYNTHETIC=true도 필요하다.
- 응답에서 서버 파일 경로를 포함한 provenance를 제거한다.

## 웹

`WelfarePanel.tsx`를 기존 FinalModelApp에 연결했다. 선택된 행정동의 상위 시군구 기준으로 조회한다.
후보·제외·보류, 추천 근거 점수, 관측 ID, 미확인 조건과 가상 DB 표시를 제공한다.
요청 취소와 선택 키 비교로 이전 응답이 새 선택을 덮지 않게 했다. 오류는 빈 추천과 구별한다.
I5 고저 임계값은 여전히 적용하지 않는 context 연구 모드다.

## 실행

상위 bigcontest-2026에서 두 터미널:

```powershell
$env:PYTHONPATH='service/backend;agent/src;model'
service/backend/.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
cd service/frontend
npm run dev
```

환경 의존성: backend/requirements.txt (langgraph 포함). 임시 DB 생성은 Step 02 명령 참조.
검증용 세션은 API 8011, 웹 5175와 MODEL_API_PROXY=http://127.0.0.1:8011을 사용했다.

## 검증 기록

- 실제 자료 API 응답 200: 강남구 2025-08 MAN 20G → 후보 2 / 제외 1 / 보류 1.
- API 회귀 4개 통과: 잘못된 지역, 임의 경로 파라미터 차단, 그래프 응답/경로 비공개, DB 장애 503.
- TypeScript 검사 및 Vite production build 통과.
- 브라우저에서 12월 → 8월 변경 후 SKT -4.51%, 카드 -0.34%, 추천 2/제외1/보류1 표시 확인.
- 화면 기록: agent/runtime/welfare_demo/web_recommendations.png.

가상 복지사업과 제안 규칙을 사용한 로컬 연구 기능이다. 실제 복지 DB·고저 판단 규칙·원문 자동 확인은 후속이다.
