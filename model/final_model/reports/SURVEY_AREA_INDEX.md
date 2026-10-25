# 시군구·행정동별 설문 기반 고립지수 추가

기존 행동 모델 출력·manifest·latest.json과 기존 에이전트 ZIP은 수정하지 않았다.
V3 확정 I5 및 MOIS 지역 인구구성 참고점수를 별도 계약으로 조회할 수 있게 추가했다.

## 제공 범위와 의미

- 기간: 2025년 7~12월
- 시군구: 강남구 11680, 춘천시 51110 — 월별 12행
- 행정동: 강남구 22동, 춘천시 25개 읍면동 — 월별 282행
- 지수: isolation_index (0~100점). i5_reference는 V3 원 필드와 동일한 값
- E/P/O/C/U 5개 영역점수, 성·연령 6집단 인구비중, 20세 이상 주민등록인구 포함
- 전국 사회조사 I5 완전응답자 31,817명은 전국 표본 수이며 지역 표본 수는 null

지역 점수 = Σ(지역 20세 이상 성·연령집단 인구비중 × 2025 전국 해당 집단 I5 평균).
사회조사 V3 산식과 점수를 변경하지 않았다. SKT·카드 활동변화는 이 점수에 포함하지 않는다.
이는 전국 설문을 지역 인구구성으로 가중한 고립취약성 참고지수이며, 지역 설문 실측·고립률·개인 고립확률이 아니다.
월별 변화는 고정된 전국 설문점수에 대한 MOIS 인구구성 변화에서 나온다.

## 생성 및 조회

model 루트에서 실행한다. 기본 입력은 configs/local.json의 index_root 아래 V3 확정 export이다.
기존 행동 번들의 survey_mois_reference/도 --source로 지정할 수 있다.

    python -X utf8 -m final_model.src.survey_area_index build
    python -X utf8 -m final_model.src.survey_area_index build --source ../isolation-index/v3/exports/2025_final_v1

생성 위치는 outputs/survey_area_index_<run_id>/이다.
기존 outputs/latest.json 대신 신규 outputs/survey_area_index_latest.json으로 이 결과만 찾는다.

    python -X utf8 -m final_model.src.survey_area_index query --bundle <신규결과폴더> --level sgg --region-code 11680 --month 202512
    python -X utf8 -m final_model.src.survey_area_index query --bundle <신규결과폴더> --level administrative_dong --region-code 11680 --month 202512
    python -X utf8 -m final_model.src.survey_area_index query --bundle <신규결과폴더> --level administrative_dong --dong-code 1168051000 --month 202512

행정동 전체 282행은 total_matched/next_offset 또는 --limit 500으로 조회한다.
이 신규 모듈의 조회에는 jsonschema 패키지가 필요하며 저장소 requirements에 포함돼 있다.

## 파일과 에이전트 연결

- sgg_isolation_index.jsonl: 시군구별 월별 I5
- dong_isolation_index.jsonl: 행정동별 월별 I5
- manifest.json: 출처·코드·스키마·출력 해시
- validation_report.json: V3 원 점수·MOIS 성연령 인구·산식 대사 결과

별도 도구 get_survey_area_isolation_index(spatial_unit, month, region_code, dong_code)로 등록한다.
기존 행동 조회 응답이나 점수를 변경할 필요가 없다.
화면·보고서 명칭은 '설문 기반 고립취약성 참고지수(인구구성 가중)'를 사용하고 직접 지역 설문 결과라고 표시하지 않는다.

## 검증

- V3 동결 원본 4파일 SHA256 확인 (Windows LF 표준화 후 대조)
- 시군구·행정동 294개 원 I5와 5영역 점수 재계산 일치
- MOIS 1,692개 집단 인구 대사 및 시군구/행정동 인구합 일치
- 신규 JSON Schema 294행 통과
- null 지역 표본·지역 실측 아님·경보 불허 상태 고정
- 기존 행동 번들 모든 파일의 추가 전후 해시 일치
- 전체 테스트 27개 통과 (기존 14개 + 신규 13개); 합성 경계 테스트와 실제 집계 검증 구분

2025년 12월 시군구 참고지수는 강남구 34.12395682037197점, 춘천시 34.74343912705784점이다.
