# Step 01 — 조합 매핑 및 사업 검토 코어

- 코드: `src/donghaeng_agent/welfare_mapping.py`
- 시험: `tests/test_welfare_mapping_standalone.py`
- 현재 런타임 graph에는 아직 연결하지 않았다.

## 입력과 처리

MappingRule은 활동 조건·I5 조건을 모두 요구한다. 한 규칙 안에서는 AND로 평가하고 OR 후보는 별도 규칙으로 표현할 수 있다. 이것은 규칙 표현 방식이며 실제 어떤 조합을 쓸지는 미확정이다. 조건 또는 근거가 없으면 보류한다. draft는 research_mode에서만 평가한다.

검색 함수는 선택 태그 OR 검색 결과·코퍼스 버전·coverage와 전체 페이지 결과를 제공한다. 빈 태그는 검색하지 않는다. 사업 내용 검사는 어댑터가 원문 검증한 태그별 match/mismatch/unknown 및 근거를 받는다. 단순 제목 키워드나 태그 일치만으로 내용 적합성을 확정하지 않는다.

지역·연령·신청기간의 명백한 불일치는 제외한다. 연령 일부 겹침과 개인 조건은 unknown이다. 출처 또는 내용 근거 미확인은 source_hold다. 후보에는 규칙 이유·원천 근거·사업 내용 이유·미확인 조건을 보존한다.

## 검증 명령

프로젝트 루트 PowerShell:

```powershell
$env:PYTHONPATH='agent/src'
python -m unittest discover -s agent/tests -p test_welfare_mapping_standalone.py -v
```

합성 시험이며 실제 DB·LLM·추천 효과 검증이 아니다. Step 02에서 source_verified 및 content_matches를 실제 원문 검증 어댑터와 연결해야 한다. 요청 지역에는 검증된 상위 코드만 포함해야 하며 코드 접두사 추측은 사용하지 않는다.

2026-10-25 위 명령 실행 결과: 8개 시험 통과. I5 조건 누락, draft 모드, 개인 조건 unknown, 지역/기간/연령 불일치, 내용 근거 누락, 연령 일부 겹침, 빈 태그, 검색 실패를 확인했다.
