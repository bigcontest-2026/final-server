# Step 03: 실제 I5·활동신호 → 임시 ERD DB 추천

2026-10-25 구현. 실행 모듈: `donghaeng_agent.welfare_pipeline`.
독립 Python 함수와 CLI로 연결했다. LangGraph·서비스 API·웹 연결은 다음 단계다.

## 전체 흐름

1. 모델 공개 query 함수로 실제 I5와 활동 번들을 조회한다. 번들 완료 상태·실데이터 표식·출력 해시를 검증한다.
2. 같은 시군구·관측 월인지 확인한다. 활동자료는 요청한 성·연령 집단, I5는 지역 전체 참고값이다.
3. 기존 Rule / Robust / 연속 감소 값을 사용해 조사 경로를 선택한다.
4. 연구용 제안 규칙으로 관련 I5 영역을 선택하고 실제 영역 점수와 record_id를 남긴다.
5. 새 DB의 program_i5_domains를 검색하고 공식 분류·연결 사유·규칙 버전을 함께 가져온다.
6. 지역·연령·기간·사업 내용 검토 상태를 확인한다. 후보·제외·보류로 나누고 이유와 미확인 조건을 반환한다.

## 현재 규칙: research-context-v1, draft

`--research-context`를 명시해야 실행된다. 기본값은 mapping held이며 자동 검색하지 않는다.
이는 구현 검증용 제안이며 I5 임계값을 확정한 것이 아니다.

| 활동 근거 | 조사 경로 | 시연 검색 영역 |
|---|---|---|
| 통신·카드 동시 감소 | 공통 요인 검토 | P/C/U, 카드 감소 관측 시 E 추가 |
| Robust 또는 연속 감소 | 집중 조사 | P/C/U, 카드 감소 관측 시 E 추가 |
| 통신 단일 감소 | 단일 자료 검토 | O/P |
| 카드 단일 감소 | 단일 자료 검토 | E |
| 신호 없음·자료 부족 | 자동 검색 안 함 | 없음 |

영역별 실제 값이 유효할 때만 연결한다. 점수 크기에 따른 순위·발동 기준은 없다.
따라서 점수가 달라져도 유효한 값이면 동일 분야를 검색한다. **I5 고저에 반응하는 규칙은 아직 미구현**이다.
카드 감소가 경제적 곤란을 입증하거나 P가 실업을 의미하는 것은 아니다.
직업 관련 후보는 가상 사업에 별도로 저장된 P/E 내용 연결을 통해 검색된다.

공식 분류 기반 검색 기능은 Step 02에 구현돼 있다. 이번 자동 경로는 분류를 임의로 추정하지 않고
ERD의 별도 I5-사업 연결을 사용한다. 공식 분류는 결과에 보존된다.

## 실제 실행

상위 bigcontest-2026 폴더에서:

```powershell
$env:PYTHONPATH='agent/src;model'
python -m donghaeng_agent.welfare_pipeline --i5-bundle model/data/i5/national_2025 --activity-bundle model/final_model/outputs/revalidated_20261016_172441_931042 --db-path agent/runtime/welfare_demo/welfare_erd_v2.sqlite --publication-id erd-demo-v2 --region-code 11680 --month 202508 --sex-code MAN --age-code 20G --as-of 2026-10-25 --research-context --allow-synthetic --output agent/runtime/welfare_demo/real_evidence_recommendations.json
python -m unittest discover -s agent/tests -p 'test_welfare_*standalone.py' -v
```

임시 DB가 없다면 Step 02의 welfare_erd 생성 명령을 먼저 실행한다.
latest 포인터에 암묵적으로 의존하지 않고 번들 경로와 publication_id를 고정한다.
입력 검증 실패는 예외/비정상 종료이며 ‘검색 결과 없음’으로 숨기지 않는다.

검증 사례: 강남구 2025-08 MAN 20G. 통신 -4.5123%, 카드 -0.3399%, Rule=true,
Robust=false, 연속 감소=null. P/C/U/E를 검색해 추천 후보 2건, 기간 제외 1건, 보류 1건.
실제 관측자료 + 가상 복지 DB의 결합이며 실제 운영사업을 추천했다는 의미는 아니다.

## 결과 및 남은 조건

- model_inputs_synthetic=false와 welfare_data_synthetic=true를 구분한다.
- 추천 이유에 활동·I5 record_id, 실제 점수, 검색 연결 사유와 규칙 버전을 남긴다.
- I5는 해당 지역의 직접 조사나 활동자료와 같은 성·연령 집단 점수가 아니다.
- 활동 관측 월과 사업 판단 기준일은 분리한다. 과거 신호를 현재 위험으로 해석하지 않는다.
- 지역은 KR_SGG 정확 코드 매칭만 사용한다. 매핑이 없으면 지역 미확인으로 보류 조건을 남긴다.
- 사업 원문·내용 확인은 DB에 입력된 검토값을 사용한다. 실시간 조회나 LLM 심사는 아직 없다.
- 성별·소득·가구 등 일반 자격조건의 자동 판정은 미구현이며 개인 자격은 미확인으로 유지한다.
- 모든 결과 quality_status=hold, operational_alert_allowed=false.

다음: 확정 조합 규칙 교체, 실제 복지 데이터 어댑터, LangGraph/서비스 API/웹 연결.
추가 맥락으로 원인·실제 필요를 조사하는 C 확장은 계속 후속이다.
