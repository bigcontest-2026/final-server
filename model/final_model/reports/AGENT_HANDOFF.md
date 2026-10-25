> 최신: 전국 I5 실제 값 생성과 로컬 agent/service 조사 경로 검증 완료. [전국 단계별 README](../README.md)와 [전국 인계 검증 결과](NATIONAL_I5_DELIVERY_REPORT.json)를 따른다. 아래 서울 복구 안내는 이전 독립 참고지표 계약이다. 원격 운영 배포와 외부 LLM 실행은 별도다.

## 전국 I5 실제 값 인계

- 전국 번들: 저장소에 포함된 `model/data/i5/national_2025/`. 생성 원본은 `outputs/survey_area_index_20261016_180747_534427/`이며 바이트 단위로 같다. 기존 행동 번들과 다른 경로다.
- 별도 전달 ZIP: GitHub Release `national-i5-2025-20261017`의 `national_i5_2025_20261017.zip`. 최신 Git checkout에는 데이터가 포함되어 ZIP을 추가로 풀 필요 없다. 이전 인계 ZIP의 패치는 당시 코드이므로 최신 서비스 코드에 덮어쓰지 않는다.
- 전국 읍면동·월 21,698개 점수와 5자리 지역·월 1,584개 점수가 실제 포함된다. 성인 인구 0인 16개 동·월은 0점으로 만들지 않는다. 상위 시와 하위 구의 인구를 합산하면 중복되므로 주의한다.
- 설치: 최신 model/agent/service를 같은 부모 폴더에 받고 패키지를 갱신한 후 백엔드를 재시작한다. 서비스는 형제 `model/data/i5/national_2025`를 기본 사용한다. 별도 위치에 둘 때만 `I5_BUNDLE_PATH`를 지정한다. 기존 snapshot은 유지한다.
- 직접 조회: `from final_model.src.survey_area_index import query`; `query(i5_bundle, region_code="26110", month="202512")`.
- HTTP: `GET /api/research/i5?dong_code=2611051000&month=202512`.
- 실제 조사 경로: `InvestigationTools(..., i5_bundle=...)`의 `measurements()` → `survey_i5_population_reference` observation/evidence → graph brief. 부산 실값이 최종 brief에 보존되는 테스트를 통과했다.
- I5는 지역의 20세 이상 성·연령 인구구성으로 가중한 전국 설문 참고점수다. 요청 연령의 전용 점수·지역 설문 실측·고립률·개인 확률이 아니다. 417개 지수와 합산하지 않으며 hold/경보 불허를 유지한다.

## 서울 참고지표 복구 인계 (2026-10-17)

- 최신 번들: `outputs/revalidated_20261016_172441_931042/` (`outputs/latest.json` 참조).
- 기존 서울 417개: `external_context/legacy_seoul/`. 2025-12·20~39세 통신 기반 행동 대리지수이며 새 I5·월별 성×연령 행동 모델과 별개다.
- 호출: `from final_model.src.query import external_query` 후 `external_query(bundle, "legacy_seoul", region_code="11110", period="2025-12", offset=0, limit=100)`.
- 원 동 코드 조회: `external_query(bundle, "legacy_seoul", dong_code="1111053000")`. `next_offset`이 있으면 이어서 조회한다. 원 records의 점수·4영역·품질·설명을 그대로 전달한다.
- 응답 외피의 `quality_status=hold`, `operational_alert_allowed=false`, `main_model_join=false`를 유지한다. 자료 없는 동/월/지역을 0으로 대체하거나 20~39세를 성별·10세 단위로 복제하지 않는다.
- 가용성: `from final_model.src.coverage import get_region_availability`; `get_region_availability(bundle, region_code="11110")`.
- 서울 C1 25개 구: `external_query(bundle, "c1", district_code="11110", quarter=20254)`. 분기 상권 참고자료이며 417개 동 점수와 다른 계약이다.
- 현재 GitHub service snapshot blob `12ca3d1039a4ef3d7f9c1eac9c85da2c9e59047f`와 417개 전체 payload 동일. 계산 424개·공간매핑 417개·보류 7개를 보존했다. 서비스 저장소 본문 대조는 완료했지만 실제 도구 등록·운영 배포 검증은 별도다.
- 과거 `final_20261014_224511_915286` 번들과 ZIP은 당시 결과다. 새 참고지표가 포함된 최신 인계 ZIP을 사용한다.

# 에이전트팀 인계
행동 모델 실측 bundle은 outputs/<run_id>/에 있다. 원자료·행동 모델 세부 결과는 Git 제외이며 팀 내부 파일 전달로 사용한다. 전국 I5 집계 결과는 위 Git 경로에 포함한다.
from final_model.src.query import query, survey_query
query(bundle, region_code='11680', month='202510', sex_code='MAN', age_code='20G')
조회 전에 manifest와 모든 출력 해시를 검증한다. mismatch/failed 실행은 제공하지 않는다.
국가 설문 6집단은 survey_query로 별도 조회하며 행동 10집단에 점수를 복제하지 않는다.
agent tools.py의 기존 measurements는 ResearchSnapshot과 10자리 행정동·20/30대 계약이다.
신규 도구 get_final_behavior_evidence(5자리 시군구, YYYYMM, 성, 연령)와 get_survey_mois_reference를 별도로 등록해야 한다.
graph.py의 source version 검사에는 final bundle manifest 해시를 추가해야 한다. 기존 isolation_snapshot.json을 덮어쓰지 않는다.
service /api/investigations 요청은 10자리 행정동이다. 같은 행정동 관측으로 변환하지 말고 별도 시군구 컨텍스트로 표시하거나 새로운 조회 API를 추가한다.
정책 검색·자격 조건 검사는 기존 agent 기능을 유지한다. 현재 실제 연동과 HTTP 경로 추가는 미수행이다.

## 팀 전달 ZIP과 실행

outputs/agent_handoff_final_20261014_224511_915286.zip을 팀원에게 전달합니다. 원자료·실패 실행·로컬 설정은 포함하지 않습니다. 전체 해시 검사를 위해 성공 출력 폴더 전체를 함께 보존하세요.

압축을 푼 위치에서 Python 3.11 이상으로 실행합니다. 조회만 할 때는 표준 라이브러리만 필요합니다.

    python -X utf8 -m final_model.src.query --bundle final_model/outputs/final_20261014_224511_915286 --region-code 11680 --month 202510 --sex-code MAN --age-code 20G

C1·M1·M2는 external_query로 원래 차원을 조회합니다. 반환에 hold와 operational_alert_allowed=false가 유지됩니다. 신규 시군구 관측은 기존 행정동 스냅샷과 분리해 도구에 등록하세요.

service 기존 snapshot은 경로·blob·파일 크기를 확인했으나 GitHub 도구가 본문을 반환하지 않아 전체 데이터 내용 대조는 미수행입니다. 계약 비교는 실제 model의 Pydantic 스키마, agent 도구, 조사 API 문서로 수행했습니다.

## 추가: 시군구·행정동별 설문 I5 조회

기존 행동 번들과 별도로 survey_area_isolation_index_v1을 제공합니다. sgg_isolation_index.jsonl 12행, dong_isolation_index.jsonl 282행입니다. 기존 출력은 변경하지 않았습니다. 원 V3 i5_reference를 isolation_index와 함께 전달하며 전국 설문을 지역 인구구성으로 가중한 참고지수임을 표시합니다. 상세 조회 명령·필드·에이전트 연결은 [SURVEY_AREA_INDEX.md](SURVEY_AREA_INDEX.md)를 참고하세요.
