# 2025 최종 I5·MOIS 집계 공유 데이터

데이터 버전 `2025_final_v1_export_1`, 지수 버전 `I5_2025_final_v1`. 이 폴더는 [Step 6 보고서](../../mois/reports/STEP6_SURVEY_MOIS_INTEGRATION.md)의 **검증된 집계값**을 팀원에게 전달한다. 사회조사 개인 응답, 가구 식별자, 원본 ZIP, MOIS 단일 연령 열은 포함하지 않는다. 전체 컬럼은 [데이터 사전](DATA_DICTIONARY.md), 해시·검증 결과는 [manifest](manifest.json), 사용 한계는 [점수 해석 규칙](../../docs/SCORE_INTERPRETATION_RULES.md)을 따른다.

| CSV | 행 수 | 기본 키 | 기간·지역 | 용도 |
|---|---:|---|---|---|
| `national_six_group_profile.csv` | 6 | `(sex, age_group)` | 2025 전국 사회조사, 남/여×20–39/40–59/60+ | **전국 설문** I5·E/P/O/C/U 집단 평균, OR 비율, 표본·가중치 합 조회 |
| `national_item_profile.csv` | 72 | `(sex, age_group, item)` | 2025 전국, 6집단×최종 11문항+P3 참고 | 문항별 취약응답 비율·평균 점수·코드 방향·P3 총점 제외 여부 확인 |
| `dong_month_six_group_population.csv` | 1,692 | `(month, dong_code, sex, age_group)` | 2025.07–12, 강남구 22동·춘천시 25개 읍면동×6집단 | **MOIS 실측** 20세 이상 집단 인구수·비중 조회 |
| `dong_month_reference.csv` | 282 | `(month, dong_code)` | 같은 47개 읍면동×6개월 | 실제 주민등록 인구·세대·1인 세대와 **인구구성 참고** I5·영역별 점수·전월 변화 조회 |
| `region_month_summary.csv` | 12 | `(month, region_code)` | 강남구·춘천시×6개월 | 시군구의 실제 인구 집계와 20세 이상 구성비로 다시 계산한 참고점수 비교 |
| `national_overall_reference.csv` | 8 | `(survey_year, metric)` | 2025 전국 | 동일 산식 20세 이상 전체 I5·영역 평균·개인 점수 가중 분산/표준편차. 19세 이상 공식 OR은 별도 행 |

`month`는 `YYYYMM`이며 인구·세대는 해당 월말 주민등록 **실측 집계**다. `population_total`에는 20세 미만이 포함되고 `population_20plus`는 결합 분모다. `one_person_household_share`는 **전체 세대 중 1인 세대 비율(0~1)**이다. 성연령 1인 세대 세부 수는 작은 셀 때문에 공유하지 않는다. 공개된 여섯 집단 인구수는 모두 10명 이상이다. 2025 전국 설문 점수는 모든 월에 같은 값이므로 행정동 참고점수의 월별 변화는 MOIS 성연령 인구비중 변화에서 나온다.

`i5_reference`, `E_reference`…`U_reference`는 지역에서 측정한 설문 점수가 아니다. 전국 성연령집단별 평균에 해당 지역 20세 이상 인구비중을 곱한 **인구구성 기반 참고점수(점)**다. 공식 OR은 설문 비율(%)이고 U1/U3와 I5의 U가 정의상 겹친다. 참고점수와 OR을 지역 고립률·고립자 수·확률 또는 활동 이상탐지의 독립 증거로 쓰지 않는다.

## 조회 예시 — 원자료 없이 가능

이 폴더의 CSV만 받은 팀원은 Python과 pandas로 아래 조회를 실행할 수 있다. `region_code`·`dong_code`는 식별 코드 문자열로 읽는다.

```python
import pandas as pd
from pathlib import Path

share = Path("v3/exports/2025_final_v1")
national = pd.read_csv(share / "national_overall_reference.csv")
dong = pd.read_csv(share / "dong_month_reference.csv", dtype={"region_code": "string", "dong_code": "string"})
baseline_e = national.loc[national.metric.eq("E"), "weighted_mean"].item()
gangnam_dec = dong.loc[dong.month.eq(202512) & dong.region_code.eq("11680")]
print("전국 E 평균(점):", baseline_e)
print(gangnam_dec[["dong", "population_20plus", "E_reference", "i5_reference"]].head())
```

팀원의 로컬 환경에 사회조사 원자료·MOIS 팀 ZIP이 없어도 **6개 CSV의 전국 프로필·문항별 집계·2025.07–12 인구·인구구성 참고점수·전월 변화**는 조회할 수 있다. 개인 응답 분포 재계산, 다른 연령 구간, 새 기간·지역, 공식 복합표본 신뢰구간, 실제 지역 고립률은 이 집계 CSV만으로 산출할 수 없다.

## 재현

원자료가 허용된 로컬 환경에서는 저장소 루트에서 다음 순서로 재현한다. 기존 지수·MOIS 산식 코드는 변경하지 않았다.

```sh
.venv/bin/python v3/survey/src/final_2025.py
.venv/bin/python v3/mois/src/population_reference.py
.venv/bin/python v3/exports/build_2025_final_v1.py
.venv/bin/python -m unittest discover -s v3/survey/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s v3/mois/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s v3/exports/tests -p 'test_*.py'
```

생성 코드는 기존 4개 CSV의 셀 값을 그대로 재사용하고, 민감한 성연령별 1인 세대 열만 제거한다. 없는 두 전국 표(`national_item_profile`, `national_overall_reference`)만 기존 Step 3/최종 I5 코드를 이용해 같은 완전응답자 31,817명에서 추가 집계한다. 기존 `outputs/`와 원본 데이터는 Git 공유 범위에 포함하지 않는다.

## 자료 제공 범위

팀 ZIP의 README에는 공유 드라이브 자료 구조가 설명되어 있으며 명시적인 재배포 금지 문구는 확인되지 않았다. 이 폴더는 **프로젝트 팀의 GitHub 공유를 위한 파생 집계**만 담는다. 팀 외부 재배포 전에는 행정안전부 원천자료와 대회 제공조건을 다시 확인한다. 성연령별 1인 세대 수 중 3~4세대 셀이 있어 전부 제외했고, 공개 인구·세대 집계의 최소값은 14, 문항별 취약/비취약 응답 사례수의 최소값은 16이다. 원본 ZIP·개인응답·가구키는 포함하지 않는다.
