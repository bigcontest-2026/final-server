# 최종 통합 모델 — 동행 관측소

## 전국 I5 실제 값 생성·에이전트 전달 진행 (2026-10-17, Asia/Seoul)

이번 우선순위는 서울 417개 복구가 아니라 전국 I5 값 생성과 실제 조회 연결이다. 기존 I5 산식과 전국 설문 6집단 평균을 유지한다. 아래의 과거 계획에서 '전국 인구 미확보'로 적힌 상태는 이번 자료 확보로 갱신한다.

### 전국 Step 1 — 원본·전처리 경로 추적: 완료

- 기존 `isolation-index/v3/mois/src/population_reference.py`는 팀 ZIP의 MOIS master와 2025-07~12 월별 공식 원본을 대사한 뒤 강남·춘천 47개 읍면동만 추출했다. 원 팀 ZIP은 현재 로컬에서 확인되지 않았다.
- `data-set/sk_merge_csv_with_hangjangdong.csv`는 SKT 유동·행정동 공간 매핑이다. 주민등록 인구를 대신 추정하지 않고 공식 주민등록 통계를 별도로 확보했다.

### 전국 Step 2 — 공식 전국 인구 확보·원자료 대사: 완료

- 출처: [행정안전부 행정동별 연령별 인구현황](https://jumin.mois.go.kr/ageStatMonth.do). 등록구분 전체(거주자·거주불명자·재외국민 포함, 외국인 제외), 남녀 구분, 10세 간격, 0~100세 이상, 전체 읍면동 다운로드를 사용했다.
- 2025년 7~12월 6개월 CSV·요청 파라미터·응답 헤더·취득시각·SHA256은 `data-set/mois_national/`에 보존했다. 다운로드는 `final_model.src.download_population`, 파싱·대사는 `final_model.src.national_population`로 재현한다.
- 각 월 17개 시도의 모든 읍면동 합계와 공식 시도 총계, 277개 5자리 지역 총계 행의 자식 읍면동 합계를 대사했다. 계·남·여의 총인구/연령합/11개 연령구간, 총 39개 수치 필드가 일치해야 진행한다. 비자치구가 있는 상위 시와 관할 구를 구분하고, 두 레벨을 더해 인구를 중복 계산하지 않는다.
- 공식 원표에 인구 0으로 게시된 출장소를 누락 데이터나 0점으로 취급하지 않는다. 공식 사이트가 미게시하는 무인 지역도 임의 생성하지 않는다.

### 전국 Step 3 — 전국 I5 실제 산출: 완료

- 산출 번들: `outputs/survey_area_index_20261016_180747_534427/`. `outputs/survey_area_index_latest.json`이 이 번들을 가리킨다. 실행 ID는 UTC이며 보고 날짜는 Asia/Seoul이다.
- 실제 값: 읍면동×월 **21,698행**, 상위 시·비자치구를 포함한 5자리 지역×월 **1,584행(264개 코드×6개월)**. 상위 시와 하위 구 점수는 각각 제공하되 인구 합산 시 중복 집계하지 않는다.
- 성인 주민등록 인구가 0인 읍면동·월 **16개**는 점수를 만들지 않고 `unscored_areas.json`에 사유를 보존했다. 해당 조회는 `zero_registered_adult_population`을 반환한다.
- 전국 실제 인구로 성×연령 6집단 비중을 만들고 기존 전국 사회조사 I5·E/P/O/C/U 평균을 가중했다. 전국 지역별 인구구성 참고지수이며 지역 설문 실측·고립률·개인 확률이 아니다. `hold`, `operational_alert_allowed=false`를 유지한다.
- 원표 기반 지역·월 목록은 `area_registry.json`, 월별 원자료 총계 대사와 SHA256은 `national_validation_report.json`에 포함한다. 기존 강남·춘천 동결 결과와 서울 417개 번들은 덮어쓰지 않는다.
- 재현: model 루트에서 `python -B -m final_model.src.download_population --output ../data-set/mois_national` 후 `python -B -m final_model.src.national_population --raw-root ../data-set/mois_national --national-source ../isolation-index/v3/exports/2025_final_v1 --output-root final_model/outputs`.

### 전국 Step 4 — 실제 agent/service 연결: 로컬 HTTP·그래프 검증 완료

- 제공된 GitHub 조직의 `agent`, `service` main을 별도 로컬 checkout으로 받아 실제 경로를 확인했다. 에이전트 도구는 `agent/src/donghaeng_agent/tools.py`, 조사 그래프는 `graph.py`, 서비스 설정은 `service/backend/app/config.py`와 `main.py`다.
- `InvestigationTools.get_survey_area_isolation_index()`와 서비스 `GET /api/research/i5`를 추가했다. 서비스가 명시적인 `I5_BUNDLE_PATH`로 검증된 전국 번들을 주입한다. 기존 `isolation_snapshot.json`을 교체하지 않는다.
- 기존 조사 요청은 서울·춘천만 허용했으므로 도시 이름 계약을 확장하고, I5 원표의 정확한 동 코드·기준월·공식 지역명으로 전국 지역을 확인한다. 도시 이름과 코드가 다르면 거부한다.
- 실제 `measurements()`에 `survey_i5_population_reference` 근거를 별도로 추가해 graph의 observations/evidence/brief에 전달한다. 요청 연령이 20대/30대여도 I5는 20세 이상 전체 인구구성 점수임을 명시한다. 점수를 해당 연령 전용 값으로 복제하지 않는다.
- 실행 버전에 I5 manifest SHA256을 추가하고, 대기/실행 도중 번들이 바뀌면 거부한다. 근거 재검사에도 I5 무결성·버전 검사를 포함했다.
- 현재 model 저장소에 설치용 패키지 설정이 없어 agent/service가 요구하던 `donghaeng-model` 설치가 불가능했다. `pyproject.toml`을 추가하고, 기존 `model_mori`의 ResearchSnapshot 계약 두 파일을 그대로 보존해 기존 서비스 호환성과 새 I5 조회 패키지를 함께 제공한다.
- 코드·HTTP·실제 LangGraph rule_baseline 조사에서 부산의 I5 값이 최종 brief와 evidence에 동일하게 저장됨을 확인했다. 외부 LLM 호출이나 원격 운영 배포를 수행한 것으로 표시하지 않는다.

### 전국 Step 5 — 전국 실측·회귀·연결 검증: 완료

- 모델·agent·service 통합 검사: **129 passed / 0 failed / 18 skipped**. 17개는 Git에서 제외된 기존 공식 지원 원문 캡처 자료가 없어 제외됐고, 1개는 Windows symlink 권한이 없어 제외됐다. 경로 탈출 거부 검사는 별도로 통과했다.
- 최종 I5 HTTP·실제 조사 그래프 검사 **5개 모두 통과**: 전국 지역 조회/입력 오류, 부산 동의 실제 I5와 최종 brief/evidence 일치, 도시·코드 불일치 거부, 미완료 번들 503, 대기 중 I5 버전 변경 거부. I5는 '인구구성 가중 참고지수'로 표기한다.
- 전국 산출값 전체를 JSON Schema로 검증했다. 기존 두 지역의 282개 동·월 인구가 정확히 일치하고 I5 최대 부동소수 오차는 **7.11e-15점**이다. 기존 산식을 바꾸거나 지역별 점수를 임의 보정하지 않았다.
- 결과와 실행 코드 SHA256: `reports/NATIONAL_I5_DELIVERY_REPORT.json`. 테스트 XML은 outputs에 있다. 통합 런타임(FastAPI 0.143.3/LangGraph 1.2.15/Pydantic 2.14.1)으로 검증했으며 기존 서비스 잠금 파일의 모든 버전 조합에서 검증했다고 주장하지 않는다.
- model 패키지 wheel 빌드 통과. 기존 snapshot 계약과 새 final_model 조회·스키마를 함께 설치할 수 있다.

### 전국 Step 6 — 에이전트팀에 실제 값·연결 코드 인계

- 인계 ZIP: `outputs/national_i5_agent_handoff_survey_area_index_20261016_180747_534427.zip`.
- 전국 실제 결과 번들·latest 포인터·model wheel·3개 저장소의 변경 파일과 diff·기준 커밋·공식 취득 manifest·테스트 결과를 함께 전달한다. 원자료 CSV·API 키·로컬 DB·개별 응답은 넣지 않는다.
- 재생성: model 루트에서 `python -B -m final_model.src.package_national_i5`. ZIP CRC와 번들 manifest의 각 파일 해시를 대조한다.
- 서버에 model/agent/service 변경을 반영하고 `I5_BUNDLE_PATH`를 해당 전국 번들의 절대 경로로 설정한다. 기존 `RESEARCH_PATH`는 유지한다. 소스코드만 전달하고 outputs를 생략하면 전국 값은 전달되지 않는다.
- 현재 추가로 필요한 팀원 원자료는 없다. 원격 실행 환경/배포 권한과 배포 경로는 아직 확인되지 않았으며, 이번 완료 범위는 전국 값 생성·검증·인계 및 로컬 실제 agent/service 경로 연결이다. GitHub 푸시·원격 배포·외부 LLM 실행은 이번 단계에서 수행하지 않았다.

## 후속 실행 계획: 전국 실측 확장·실제 에이전트 연결 (2026-10-17)

현재 완료 범위는 서울 417개 별도 참고지표 복구, 서울 C1 25개 구 전달, 두 지역 행동/I5 보존, 지역 입력 확장 코드다. 아래는 아직 실행하지 않은 후속 계획이다. 서울 복구를 먼저 에이전트에 연결하고, 전국 원자료가 검증된 지역부터 실측 제공 범위를 넓힌다.

### A. 전국 실측 확장

1. **기준월별 행정구역·원천 제공 범위 확정.** 공식 시군구/행정동 코드 목록과 유효기간을 확보한다. 현재 `regions` 설정과 5자리 코드 형식 검증만으로 전국 행정구역 존재나 전체 관측 여부를 인정하지 않는다. 자료별 지역×월×성×연령 제공 목록, 누락·비공개 규칙, 관측 지점/공간매핑 버전을 기록한다. SKT의 경계 인접 소수 관측은 지역 전체 제공과 구분한다.
2. **주민등록 인구 입력 확인.** 현재 merge CSV의 66개 컬럼에는 SKT 유동·행정동 매핑이 있고 I5용 성×연령 주민등록 인구는 확인되지 않았다. 생산 단계의 원 merge 또는 MOIS 주민등록 집계에서 필요한 6집단 인구를 추출한다. 행정동 인구가 SKT 블록마다 반복 결합돼 있다면 월×행정동별 값의 일치 여부를 검증한 뒤 한 번만 사용하고, 블록별로 합산하지 않는다. 유동인구로 주민등록 인구를 대체하지 않는다.
3. **전국 I5 생성.** 검증한 주민등록 인구를 `population_inputs` 형식으로 변환한다. 동결한 전국 설문 6집단 평균·5영역 산식을 유지하고, 기준월별 공식 동 목록으로 `--expected-dongs` 누락/초과 검사를 수행한다. 시군구 합계와 행정동 합계, 분모·가중치·원 표본 정보를 대사한다. 기존 2지역과 서울 417개 결과는 덮어쓰지 않는다.
4. **전국 행동 입력 연결.** 현재 신한 data2는 강남·춘천만 있으므로 목표 지역을 덮는 가맹지 기준 카드 자료와 SKT 자료가 필요하다. `regions`/`region_aliases`를 명시하고 중복·기간·연령 대응·달력·관측 지점·경계 버전을 검증한다. 자료가 한 원천에만 있으면 해당 원천 근거만 제공하고 공동 감소 판정은 미산출로 둔다. 기존 지역 내 10집단 Q/H/T 및 C3 기준선 산식을 유지한다.
5. **권역별 실측 검증 후 확대.** 기존 강남·춘천 회귀 → 확보한 서울 자료 → 타 시도 표본 → 전체 입력 순으로 실행한다. 각 단계에서 코드·입력·출력 해시, 실제 제공 키, 결측/보류 사유, 원자료 독립 합계 대사, 처리 시간·메모리를 기록한다. `available`은 실제 해당 조회 범위의 자료 존재를 뜻하며, 전국 제공이나 공간 완전성을 자동 뜻하지 않는다.

전국 완료 조건: 기준월별 공식 대상 목록과 제공 범위 대사 통과, 필요한 전국 원천 자료 확보, 실제 전체 산출 및 조회 검증 완료. 현재 전국 실측 완료를 선언할 수 없으며, 데이터 확보 대기는 코드 수정 시간과 별도다. C1은 서울 전용 자료로 유지하고 서울 417개는 원래 2025-12·20~39세 참고지표로 유지한다.

### B. 실제 에이전트·서비스 연결

| 제안 도구 | 모델 함수/번들 | 제공 의미 |
| --- | --- | --- |
| `get_region_availability` | `coverage.get_region_availability` | 해당 번들에 실제 있는 근거와 제공 기간. I5 번들은 별도 가용성 검사 필요 |
| `get_legacy_seoul_reference` | `external_query(bundle, "legacy_seoul", ...)` | 기존 서울 417개 동별 2025-12·20~39세 참고지표 |
| `get_commercial_context` | `external_query(bundle, "c1", ...)` | 서울 25개 구의 분기 상권 참고자료 |
| `get_final_behavior_evidence` | `query.query(bundle, ...)` | 시군구×월×성×연령별 SKT·카드 행동 근거 |
| `get_survey_area_isolation_index` | `survey_area_index.query(i5_bundle, ...)` | 별도 I5 번들의 인구구성 가중 참고지수 |

1. **읽기 전용 번들 설치.** Git에는 코드·스키마·테스트·보고서만 올린다. 실측 번들 ZIP은 허용된 팀 내부 배포 경로로 따로 전달한다. 서비스에 버전 디렉터리로 풀고 ZIP SHA256·manifest·모든 출력 해시·계약 버전·완료 상태를 검증한다. 기존 `isolation_snapshot.json`은 유지한다. 배포 설정에 행동 번들 경로, 별도 I5 번들 경로와 코드 버전을 명시한다.
2. **에이전트 도구 등록.** agent의 실제 도구 정의/그래프 라우팅과 service의 호출 경로를 확인한 후 위 함수들을 얇은 어댑터로 연결한다. 단일 조회 인터페이스가 필요해도 근거 종류·공간단위·기준월·성연령을 응답에 보존한다. 10자리 동 코드와 5자리 시군구 코드를 구분하고, 동명만으로 추측 매핑하지 않는다. 기존 20/30대 도구와 원래 20~39세 참고지표를 구분한다.
3. **응답·화면 계약 적용.** 자료 없음, 조건에 해당하는 후보 없음, 자료 손상/미완료를 서로 구분한다. 근거 종류·관측기간·기준선·원 품질·버전·보류 사유를 표시한다. I5·417개 점수·C1·월별 행동을 합산하거나 서로 누락값을 채우는 데 쓰지 않는다. `hold`, `operational_alert_allowed=false`를 유지한다.
4. **실제 호출 검증.** 에이전트→도구→번들→응답까지 실제 요청을 실행한다. 종로의 기존 동 점수, 강남 행동, 춘천의 서울 참고지표 없음, C1 분기 요청, 다른 월/연령 요청, 페이지네이션, 변조·미완료 번들 거부를 포함한다. 서울 417개를 원 서비스 snapshot과 재대사하고, 테스트한 코드/데이터 버전과 결과를 기록한다. GitHub snapshot 일치를 운영 API 검증으로 대신하지 않는다.
5. **성능·배포·복구.** 현재 조회는 파일 전체 읽기/해시 검사를 반복하므로 운영 전 지연시간을 측정한다. 불변·읽기 전용 번들은 로딩 시 전체 검증하고, 검증된 manifest 버전별로 자료와 공간/기간 인덱스를 캐시하는 구조를 검토한다. 버전 변경 시 캐시를 무효화한다. staging 검증 후 활성 버전을 원자적으로 교체하고, 이전 검증 버전으로 복구할 수 있게 보관한다. health/log에 코드·번들 버전과 오류율·지연을 남긴다.

운영 연결 완료 조건: 실제 agent/service 경로의 호출 성공, 원본 결과 일치, 미보유/손상 응답 검증, 읽기 전용 배포와 버전 복구 확인. 담당 경계는 모델팀의 번들·산식·해시 검증, 데이터 담당의 전국 제공 범위·정의 확인, 에이전트/서비스 담당의 도구 등록·화면·배포 검증으로 나눈다.

## 지역 지원 복구·전국 입력 확장 진행 보고 (2026-10-17, Asia/Seoul)

작업 기준은 `model/final_model`이다. 아래는 이번 작업 결과이며, 아래쪽의 과거 검수 기록과 구분한다.
기존 I5·행동 산식, hold 및 `operational_alert_allowed=false`를 유지한다. 전국 원자료와 실제 서비스 연결이 검증됐다는 의미는 아니다.

사용자와 방향 확정: 서울 지원 누락은 기존 417개 결과를 별도 참고지표로 복원해 먼저 해결한다. 전국 확장 코드는 선택적 입력 확장으로 두며, 기존 점수를 전국 점수로 바꾸거나 다른 모델과 합산하지 않는다.

### Step 1 — 서울 C1 25개 구 전달 복구: 재계산 검증 완료

- `context.py`에서 인계용 C1 설정을 25개 자치구로 확장했다. 외부 `C1_commercial_baseline`의 연구 설정 파일은 수정하지 않았다.
- `config_used.json` 및 유효 설정 해시를 저장하며, 인계 지역 목록을 실제 결과에서 생성한다.
- 별도 실행 `outputs/revalidated_20261016_171111_028809/`에서 25개 구 인계 목록을 확인했다. 기존 완료 번들과 latest는 보존했다.
- 행동 100행과 C3 880행의 저장 관측 기반 재계산 통과. C1은 분기별 상권 참고자료이며 월별 성×연령 행동 모델에 합산하지 않는다.

### Step 2 — 지역 입력·조회 일반화: 코드 및 회귀 테스트 완료

- `geography.py`에서 설정의 `regions`와 `region_aliases`를 검증한다. 기존 두 지역은 과거 실행 호환용 기본값이며 지역 확장은 명시 설정으로 수행한다.
- pipeline의 카드 집계, C3 지역 매핑, 지역 내 Q/H/T 계산, 행정동 집계, 시군구 스키마를 확장했다. 외부 C3 원본 코드를 수정하지 않고 실행 시 명시 별칭을 전달한다.
- 패널 행 수는 설정된 지역×기간×성×연령 키로 검증한다. 지역 내 10집단 강건 통계 산식은 유지한다.
- 행동 조회와 I5 조회는 형식이 맞는 미보유 지역에 빈 records와 `availability.status=data_unavailable`을 반환한다. 코드 형식 검증은 실제 행정구역 존재·전국 제공 범위 검증을 뜻하지 않는다.
- `coverage.py`는 검증된 번들에 실제 포함된 지역별 행동·C1 제공 기간을 조회한다. 지리적 완전성은 미검증으로 표시한다.
- 종로·강남·부산 중구를 포함한 합성 150행을 지역별 독립 계산과 대조했다. 동명 지역을 이름만으로 추정하지 않는 별칭 검증도 추가했다.
- 기존 41개와 확장 6개, 총 47개 테스트 통과. 합성 확장 테스트를 전국 실측 검증으로 해석하지 않는다.

### Step 3 — 전국 I5 입력 생성 경로: 구현 완료, 전국 주민등록 인구 입력 미확인

- `population_inputs.py`가 월·행정동별 성×연령 6집단 주민등록 인구를 받아 기존 전국 설문 평균으로 가중한 I5 입력과 결과 번들을 만든다.
- 기존 동결 설문 파일의 해시를 검증하고 원 평균을 유지한다. 지역 수에 고정된 12/282/1692 검증은 실제 지역·행정동·6집단 키 대사로 대체했다.
- 입력 컬럼: `month,region_code,region,dong_code,dong,population_total,population_20plus,pop_남_20-39,pop_남_40-59,pop_남_60+,pop_여_20-39,pop_여_40-59,pop_여_60+`.
- 중복 행정동·월, 음수/결측/비정수 인구, 분모 불일치를 거부한다. `--expected-dongs`의 월별 동 목록과 일치 여부를 검사할 수 있으며 입력 목록만으로 전국 완전성을 주장하지 않는다.
- 실행: `python -B -m final_model.src.population_inputs --population <인구CSV> --national-source ../isolation-index/v3/exports/2025_final_v1 --output-root final_model/outputs [--expected-dongs <month,dong_code CSV>]`.

### Step 4 — 원자료 범위와 merge CSV 재확인: 감사 완료, 전국 행동 실측은 미완료

- 지정된 `../data-set`의 ZIP 내부와 원자료를 확인했다. 재현 명령: `python -B -m final_model.src.data_audit --data-root ../data-set --output final_model/reports/DATA_SCOPE_AUDIT.json`.
- 신한 data2는 강남 1,366,189행·춘천 440,611행이다. 서울 C1 입력은 25개 구다. SKT merge는 11개 시군구 코드가 있지만 행 수만으로 지역 전체 제공을 인정하지 않는다.
- 사용자가 MOIS와 결합된 파일로 알려준 `sk_merge_csv_with_hangjangdong.csv`의 66개 컬럼을 직접 확인했다. SKT 유동·행정동 공간 매핑은 있으나 주민등록 성×연령 인구 컬럼은 확인되지 않았다. `*_FLOW_POP_CNT_*`를 주민등록 인구로 대체하지 않는다.
- 기존 두 지역 I5 확정 입력은 계속 사용 가능하다. 전국 I5 실측 생성에는 전국 주민등록 인구 입력 확인이, 전국 공동 행동 계산에는 전국 SKT·카드의 검증된 제공 범위가 필요하다.

### Step 5 — 기존 서울 417개 별도 참고지표 복구: 구현·현재 저장소 snapshot 대조 완료

- 새 지역 가용성 조회: `python -B -m final_model.src.coverage --bundle <검증완료 번들> [--region-code 26110]`. 에이전트/service 저장소의 실제 도구 등록·운영 반영은 미검증이다.
- 팀원 제안의 기존 417개는 `model_mori/outputs/paper_isolation_index_20261003/service_payload.json`에서 확인했다. 계산 424개·공간 연결 417개·보류 7개이며, 2025-12 서울 20~39세의 통신 기반 행동 대리지수다.
- 현재 확장 작업은 새 모델의 지역 입력·조회 확장이다. 팀원 안은 기존 417개 점수·공간매핑·검증 결과를 보존해 별도 참고지표 조회로 복구하는 작업이다. 서울 C1 25개 구 복구만으로 417개 연결이 복원되는 것은 아니다.
- GitHub `bigcontest-2026/service`의 `shared/research/isolation_snapshot.json` 본문을 blob `12ca3d1039a4ef3d7f9c1eac9c85da2c9e59047f`로 확보했다. 원 Git blob 해시와 SHA256을 검증했다. 출처는 `reports/LEGACY_SERVICE_SNAPSHOT_SOURCE.json`에 기록했다.
- `legacy_seoul.py`에서 기존 `ResearchSnapshot` 계약을 적용하고, 현재 snapshot의 `paper_behavior_index`와 로컬 payload 전체가 동일함을 확인했다. 417개 점수·4영역·품질·설명·공간코드·동명은 그대로다. 원 점수 CSV와 매핑 CSV도 독립 대사했다. 보류 7개를 추정 연결하지 않는다.
- 새 번들 `outputs/revalidated_20261016_172441_931042/`의 `external_context/legacy_seoul/`에 독립 계약 `legacy_seoul_behavior_reference_v1`로 연결했고 검증 완료 후 행동 latest를 이 번들로 변경했다. 기존 번들은 보존했다.
- 에이전트에서 `external_query(bundle, "legacy_seoul", region_code="11110", period="2025-12")` 또는 `legacy_seoul.query(reference_bundle, dong_code="1111053000")`로 호출한다. 원 연령은 `20~39세`이며 월별·성별 점수로 복제하지 않는다. 응답의 `quality_status=hold`, `operational_alert_allowed=false`, `main_model_join=false`를 유지한다.
- 기존 로컬 `service_verification.json`의 과거 TestClient 검증과 이번 GitHub snapshot 내용 일치 검증은 구분한다. 운영 API·배포·실제 에이전트 실행은 이번에 검증하지 않았다.

### Step 6 — 보존 회귀검증과 인계

- 최종 검증: `python -B -m pytest final_model/tests -q -p no:cacheprovider --basetemp=<작업폴더내 새 임시경로>`로 53 passed / 0 failed / 0 skipped (61.31초). 이전 날짜의 tests.xml은 새 인계 ZIP에서 제외한다.
- `reports/REGIONAL_RESTORATION_VALIDATION.json`에 실제 결과 대사를 기록했다. 기존 행동 100행은 실행 provenance 외 모든 필드가 같고, 동 SKT 2,820행 파일과 I5 동결 CSV는 해시가 같다. C1은 서울 25개 구를 제공한다.
- 확장 인구 입력 경로로 기존 282개 동·월 I5를 재생성했다. 최대 부동소수 오차는 7.11e-15이며 기존 I5 latest는 원래 번들로 복원했다. 이 검증을 전국 인구 실측 검증으로 표시하지 않는다.
- 417개 전체 페이지 조회, 원 코드·기간·연령 필터, 타지역/다른 월의 자료 없음, snapshot/점수 변조 거부, 최종 모델 외부 조회 연결을 테스트한다.
- 실사용 인계 시 `outputs/latest.json`이 가리키는 새 번들과 현재 코드가 함께 필요하다. 기존 `agent_handoff_final_20261014_224511_915286.zip`만 사용하면 417개 참고지표를 조회할 수 없다.
- 새 인계 ZIP: `outputs/agent_handoff_revalidated_20261016_172441_931042.zip`. 원자료·로컬 설정은 제외하고 집계 결과·조회 코드·출처·검증 보고를 포함한다.
- 재생성 시 `configs/local.json`의 `legacy_reference_source`에 원 `paper_isolation_index_20261003` 디렉터리, `legacy_service_snapshot`에 확인할 실제 snapshot 파일을 지정하고 `run --bundle <기존 완료 번들>`을 실행한다. 이 설정은 예제에서 null로 두어 미보유 환경의 자동 연결을 방지한다. 전용 생성은 `python -B -m final_model.src.legacy_seoul build --source <원결과폴더> --snapshot <실제snapshot>`으로 가능하다.


사회조사 I5는 V3 확정 집계를 유지합니다. 행동 모델은 V3 전월 Q/H/T·양원천 감소·강건 통계 + C3 과거 같은 요일 기준의 별도 근거입니다.
원천 품질은 hold이며 자동 운영 경보를 허용하지 않습니다. 개인 고립 확률·정답 분류 모델이 아닙니다.

## 실행 및 조회

Python 3.14.7 전용 .venv에서 실행·검증했습니다. requirements.lock.txt에 실제 환경을 기록합니다.
configs/example.json을 참고해 Git 제외 configs/local.json에 index_root, data_root, c3_root를 지정하세요.
원자료를 코드 폴더로 복사하지 않습니다. model 루트에서 실행합니다.

    .\.venv\Scripts\python.exe -X utf8 -m final_model.src.run --config final_model/configs/local.json
    .\.venv\Scripts\python.exe -X utf8 -m final_model.src.query --bundle final_model/outputs/final_20261014_224511_915286 --region-code 11680 --month 202510 --sex-code MAN --age-code 20G
    .\.venv\Scripts\python.exe -X utf8 -m pytest final_model/tests tests -q

run 명령은 주 모델 생성, 외부 참고자료 검증, 재계산 검증을 순서대로 수행합니다. pipeline 명령은 주 모델만 생성합니다.
기간 조회는 YYYYMM, 지역은 5자리 시군구입니다. survey_query는 6집단 집계를 별도로 반환합니다.
external_query는 C1 원 분기·M1/M2 원 주차 차원을 유지합니다. M3 지역 조회는 차단합니다.
query는 완료된 실측 manifest와 전체 출력 해시를 먼저 검사합니다. 실패 실행·해시 불일치 자료는 인계하지 않습니다.

## 생성 결과

성공 실행: outputs/final_20261014_224511_915286/ (실측이며 Git 제외).
행동 100행·월 관측 120행·C3 880행·공통 연결 24행·동 SKT 2,820행·I5/MOIS 6파일을 생성했습니다.
양원천 감소 후보 47, 강건 후보 2입니다. V3 카드와 C3의 공통 24셀은 V3만 감소 17 / 양쪽 비후보 7입니다.
최종 테스트 21개 통과. 미래 월 누수, 증가 반례, null/0 구분, MAD=0, 무검증 공간 조인, 외부 월별 복제, hash 변경을 검사합니다.

## 폴더 및 인계

- src/: 기존 모듈 로딩·실측 파이프라인·참고정보·조회·검증 실행기
- configs/: 경로 예시 및 Git 제외 로컬 설정
- schemas/: 기존 V3 정의와 경량 행동 계약, C3 원 전달 계약 검사
- tests/: 경계·원천 무결성·실측 조회 검증
- reports/: 선정, 통합, 검증, 한계, 인계, 데이터 사전, 원천 목록
- outputs/: 성공/실패 실행 manifest 및 팀 내부 실측 전달 데이터

에이전트 상세 인계는 reports/AGENT_HANDOFF.md, 결과는 reports/VALIDATION_REPORT.md를 보세요.
기존 model의 50/50 설문 지수·행정동 스냅샷은 다른 계약이므로 덮어쓰지 않습니다.
agent/service에 신규 도구 등록과 manifest 버전 검사가 필요합니다. 실제 연동·HTTP·LLM은 미구현입니다.
사회조사 미시자료 추가 요청은 없으며 기존 확정 V3 집계를 사용합니다.
SKT 공식 경계·CRS·공급 규칙 확인, M3 공간 정렬, 과거 공표시점 복원은 남았습니다.
원자료와 실측 출력은 Git 제외입니다. 최초 실측 생성 단계에는 커밋·푸시가 없었으며, 신규 저장소 게시 기록은 아래에 남깁니다.

## 단계별 진행 기록


- Step 1: model main 4d1a7a6, index main cd1acaca, index2 main 19b0ec61, agent main fd108b78, service main 2ccb9297 확인. 원자료 4개와 SKT merge, 코드 4종 확보. 팀원의 상세 출력은 없음.
- Step 2: 시군구 월×성×연령 100 전월 비교를 기본으로 한다. C3 공통 20/30대 10~12월 24셀만 연결. I5/MOIS 6집단 별도 조회. M3 공식 공간 매핑 미검증으로 직접 조인하지 않음. 기존 서비스 스냅샷은 다른 산식·공간 계약이므로 덮어쓰지 않음.
- Step 3: final_model 폴더 생성. 기존 실험 파일은 읽기 전용으로 사용. 커밋·푸시 없음.


- Step 4 [2026-10-14T22:42:47.889114+00:00]: V1 SKT 선택·집계, V2 전월 산식, V3 강건 통계 재사용. 원자료 집계 완료.

- Step 5 [2026-10-14T22:44:00.411356+00:00]: C3 실제 data2 재실행 완료. 원 월건수 48셀 독립 대사 통과. 실행 final_20261014_222939_188951

- Step 5 [2026-10-14T22:44:18.636175+00:00]: 실패 실행은 인계 불가: ValueError: survey_frozen_hash_mismatch:dong_month_reference.csv

- Step 4 [2026-10-14T22:46:06.427038+00:00]: V1 SKT 선택·집계, V2 전월 산식, V3 강건 통계 재사용. 원자료 집계 완료.

- Step 5 [2026-10-14T22:55:57.352650+00:00]: C3 실제 data2 재실행 완료. 원 월건수 48셀 독립 대사 통과. 실행 final_20261014_224511_915286

- Step 6 [2026-10-14T22:56:16.692924+00:00]: 스키마·Q/H/T 검증 통과. 행동 100행, C3 연결 24행, I5/MOIS 동결 6개 파일, 동 SKT 2820행 생성.

- Step 7 [2026-10-14T22:56:16.693191+00:00]: 실측 실행 final_20261014_224511_915286 완료. hold 유지. agent/service 실제 연결·기존 팀 출력 동일성은 미검증.

- Step 7 [2026-10-15T08:11:11.676460+09:00]: 외부 C1 재실행·M1/M2 산출물 및 원자료 해시·M3 18원파일 해시 검증 완료. 21 테스트 통과. 보고서·조회 예시·팀 인계 자료 생성 완료.

- Step 7 [2026-10-14T23:57:12.002033+00:00]: 팀 내부 인계 ZIP 생성·CRC/해시 검증: agent_handoff_final_20261014_224511_915286.zip. 원자료·실패 실행·로컬 설정 제외.

- Step 7 [2026-10-14T23:58:31.041617+00:00]: 팀 내부 인계 ZIP 생성·CRC/해시 검증: agent_handoff_final_20261014_224511_915286.zip. 원자료·실패 실행·로컬 설정 제외.

## 신규 model 저장소 분리

- [2026-10-15T09:23:25.587751+09:00] 기존 연구 저장소를 model_mori로 보존하고 신규 bigcontest-2026/model에 최종 통합 폴더만 분리했습니다.
- 분리 후 최종 모델 테스트 14개 통과. 이전 21개는 기존 model 연구 스냅샷 테스트 7개를 포함한 당시 검증 결과입니다.
- 신규 저장소에는 기존 research/indices/export/src/donghaeng_model 코드·이력을 복사하지 않았습니다. 필요한 이전 모듈은 설정 경로로 읽습니다.
- 테스트 설정은 FINAL_MODEL_CONFIG, 실측 번들 위치는 FINAL_MODEL_BUNDLE 환경변수로 지정할 수 있습니다. 외부 코드·실측 번들 미확보 검사는 skip으로 구분합니다.
- 원자료·상세 출력·로컬 설정·가상환경·테스트 XML은 Git 제외입니다. 실측 번들은 로컬 outputs에 보존합니다.

- 저장소 게시 [2026-10-15T09:24:47.177238+09:00]: 신규 비공개 bigcontest-2026/model의 main에 초기 커밋 0ac71ac를 푸시하고 원격 SHA 일치를 확인했습니다. 코드·스키마·테스트·보고서만 포함하며 원자료·실측 출력·로컬 설정은 제외했습니다. model_mori의 원격 데이터는 변경하지 않았습니다.

## 시군구·행정동별 설문 기반 I5 추가 (2026-10-15T14:01:30.036543+09:00)

기존 행동 출력은 그대로 보존하고 별도 survey_area_isolation_index_v1 계약을 추가했습니다.

- Step 1: V3 지역별 i5_reference와 기존 기본 조회의 별도 경로를 확인했습니다.
- Step 2: 사회조사 I5를 지역 MOIS 인구구성으로 가중한 참고지수를 그대로 사용합니다. 지역 직접 조사·행동 결합 점수는 생성하지 않습니다.
- Step 3~4: src/survey_area_index.py, 전용 JSON Schema와 tests/test_survey_area_index.py를 추가했습니다. 기존 pipeline/query 코드는 수정하지 않았습니다.
- Step 5: 지역 12행·행정동 282행의 산식 및 MOIS 1,692집단을 검증했고 전체 27 테스트가 통과했습니다. 기존 행동 번들 47파일의 해시가 그대로임을 확인했습니다.
- Step 6: outputs/survey_area_index_20261015_045910_201236/에 별도 JSONL·manifest·검증 보고서를 생성했습니다. 기존 outputs/latest.json은 유지했습니다.
- Step 7: reports/SURVEY_AREA_INDEX.md에 조회법·지수 의미·에이전트 도구 연결을 정리했습니다.

## 최종 병합 검수 체크포인트 (2026-10-15)

- 팀원 V3 검증 ZIP의 원본 evidence/manifest를 대조했다. 행동 100키, 수치 1,600개(최대 오차 8.89e-16), 범주·판정 1,300개, 품질 300개가 일치한다.
- 동 SKT 2,820행·전월 변화 2,350개 및 기존 합성/달력 검증 해시 연결을 확인했다. 원자료 실험을 새로 실행한 것으로 해석하지 않는다.
- [검수 현황과 남은 작업](reports/MODEL_FINAL_REVIEW.md)에 완료 상태·C3 null·소스별 강건 통계 보완 3건과 설명 근거 연결 작업을 기록했다.
- 이 검수 기록까지 먼저 커밋·푸시하고 이후 보완은 로컬에서 진행한다. 기존 실측 출력은 유지하며 공동 최종 확정은 대기 상태다.

- Step 7 [2026-10-15T06:11:34.601293+00:00]: 후처리 검증 통과: revalidated_20261015_061107_437481. hold 유지. 공동 확정·실제 서비스 연동은 대기.

## 최종 검수 로컬 보완 (2026-10-15, 체크포인트 이후 미푸시)

- Review Step 1: 검수 체크포인트 382839f를 model/main에 커밋·푸시했다.
- Review Step 2: 완료 상태·C3 null·소스별 강건 통계의 경계조건 3건을 보완했다. 품질 hold와 운영 경보 불허는 유지한다.
- Review Step 3: 기존 검사와 경계조건 테스트 41개가 통과했다. 외부 입력으로 인한 skip은 없다.
- Review Step 4: review_v3를 실제 원본 ZIP에 실행했다. 행동 100키·동 SKT 2,820키가 일치하며 설명 근거 100개·검증 참조 28개를 별도 outputs/v3_review_20261015_060929_123525/에 생성했다.
- Review Step 5: revalidated_20261015_061107_437481에서 저장 행동 100행·C3 월요약 880행 재계산과 연결 24셀 회귀·조회가 통과했다. 원본 번들 47개 파일과 latest는 그대로다. 새 전체 원자료 반복은 아니다.
- Review Step 6: 선정·통합·검증·한계·최종 검수 문서를 갱신했다. P0 완료, P1은 명시한 검증 범위 내 확인, 공동 승인·버전 동결은 대기다. 추가 커밋·푸시 및 실제 에이전트 전달은 하지 않았다.

### 외부 입력과 실행 순서

깨끗한 checkout만으로 실측 전체를 실행할 수 없다. configs/local.json에 index_root/data_root/c3_root를 지정하며 다음 자료가 필요하다.

- isolation-index: V1 선택·집계, V2 변화 산식, V3 강건 통계 코드·설정 및 v3/exports/2025_final_v1 동결 자료. 원본 검수에는 v1/outputs/final_index_research/dong_skt_indicators.csv도 필요하다.
- data_root: SKT merge CSV, 신한 ZIP, SK 원자료 ZIP, 모바일 인구이동 ZIP, 서울 추정매출 JSON.
- C3_Shinhan_baseline: c3_baseline.py, config_c3.json, calendar_2025_H2.csv.
- data_root의 형제 디렉터리: C1_commercial_baseline 코드·설정, Mobile_M1_M2_v3_code/outputs/v3_20261014_101514_927324의 완료 산출물, M3_SK_v2_code/outputs/m3_20261013_201106_662229의 manifest·상태·full_results.zip. 현재 context.py는 이 실행 디렉터리 이름을 사용한다.
- 원본 설명 검수: 별도로 전달받은 V3_behavior_검증본_20261015.zip. 모델 집계의 필수 원자료와 구분한다.

저장된 완료 번들을 별도 복사본에서 검증하고 latest를 유지하려면:

    python -B -X utf8 -m final_model.src.run --config final_model/configs/local.json --bundle final_model/outputs/final_20261014_224511_915286 --no-publish

원본 설명을 별도 검수 출력으로 연결하려면 실제 ZIP 경로와 index 경로를 지정한다:

    python -B -X utf8 -m final_model.src.review_v3 --bundle final_model/outputs/final_20261014_224511_915286 --archive "<V3 ZIP 경로>" --index-root "<isolation-index 경로>"

원본 ZIP의 고정 SHA256을 확인하려면 --expected-archive-sha256 옵션을 함께 지정할 수 있다. 신규 전체 실측 생성은 앞의 run --config 명령을 사용한다. pipeline 단독 실행은 core_ready까지만 만들므로 run --bundle로 후처리 검증을 완료해야 조회할 수 있다.

[최신 검수 상태](reports/MODEL_FINAL_REVIEW.md)와 [실제 검증 범위·C3 사례](reports/VALIDATION_REPORT.md)를 함께 확인한다. 기존 전달 ZIP은 이번 보완 코드를 포함하지 않으며 공동 확정 후 별도 패키징한다.

- Step 7 [2026-10-16T17:12:07.475617+00:00]: 후처리 검증 통과: revalidated_20261016_171111_028809. hold 유지. 공동 확정·실제 서비스 연동은 대기.

- Step 4 [2026-10-16T17:17:11.521195+00:00]: V1 SKT 선택·집계, V2 전월 산식, V3 강건 통계 재사용. 원자료 집계 완료.

- Step 5 [2026-10-16T17:18:02.978502+00:00]: C3 실제 data2 재실행 완료. 원 월건수 48셀 독립 대사 통과. 실행 final_20261016_171617_226469

- Step 6 [2026-10-16T17:18:20.193603+00:00]: 스키마·Q/H/T 검증 통과. 행동 100행, C3 연결 24행, 동 SKT 2820행 생성.

- Step 7 [2026-10-16T17:18:20.193843+00:00]: 실측 집계 final_20261016_171617_226469 core_ready. 후처리 검증 전에는 조회·최신 번들 게시 불가.

- Step 7 [2026-10-16T17:19:06.997853+00:00]: 후처리 검증 통과: final_20261016_171617_226469. hold 유지. 공동 확정·실제 서비스 연동은 대기.

- Step 7 [2026-10-16T17:25:31.498874+00:00]: 후처리 검증 통과: revalidated_20261016_172441_931042. hold 유지. 공동 확정·실제 서비스 연동은 대기.

- Step 7 [2026-10-16T17:29:41.151598+00:00]: 팀 내부 인계 ZIP 생성·CRC/해시 검증: agent_handoff_revalidated_20261016_172441_931042.zip. 원자료·실패 실행·로컬 설정 제외.

### 전국 I5 Git 전달 및 기본 경로 연결 — 2026-10-17

1. **데이터 정리 완료:** 검증된 실행 `survey_area_index_20261016_180747_534427`의 7개 파일을 `../data/i5/national_2025/`에 복사했습니다. 합계 47,999,789바이트이며 manifest가 지정한 6개 산출물 해시를 대조했습니다. 읍면동·월 21,698행, 시군구/시·월 1,584행, 성인 인구 0명인 미산출 16행의 사유를 포함합니다. 상위 시와 하위 구는 중복되므로 합산하지 않습니다.
2. **Git 관리 적용:** 원자료·일반 outputs 제외는 유지하고 이 전국 I5 경로만 추적합니다. `.gitattributes`의 `-text`로 checkout 줄바꿈 변환에 따른 해시 손상을 방지합니다.
3. **서비스 기본 연결 완료:** 형제 `model/data/i5/national_2025`를 기본값으로 지정했습니다. `I5_BUNDLE_PATH`는 위치 변경 시에만 필요합니다. 기존 에이전트 I5 조회·조사 근거 연결과 `hold`/경보 불허는 유지합니다.
4. **검증:** 기본 설정에서 실제 전국 I5를 HTTP 및 LangGraph 조사 경로로 확인하고 환경변수 재정의도 검사합니다. 최종 검사 결과와 Git 전달 정보는 아래에 기록합니다.
5. **전달:** 저장소에서 데이터까지 받을 수 있도록 커밋·푸시합니다. 별도 전달용 ZIP은 GitHub Release에 첨부합니다. 실제 운영 서버의 업데이트·재시작은 서버 환경에서 별도로 수행해야 합니다.

최종 검증 결과: 모델·에이전트·서비스 전체 **131 passed, 18 skipped** (467.69초). 건너뛴 검사는 기존 공식 지원 원문 미보유 17개와 Windows 심볼릭 링크 권한 1개입니다. 기본 경로의 실제 데이터로 HTTP 조회·LangGraph 조사 브리프·근거 값을 대조했습니다. Git index에 저장된 6개 산출물 SHA256도 manifest와 일치했습니다. ZIP CRC 및 내부 SHA256 검증을 통과했습니다.

별도 전달: [전국 I5 Release](https://github.com/bigcontest-2026/model/releases/tag/national-i5-2025-20261017), 파일 `national_i5_2025_20261017.zip` (4,357,789바이트), SHA256 `764e74822fbde28e77274b0d5be8dede7515e6cdd394b2f5179ff8acbae276b1`. 최신 Git checkout 사용자는 ZIP이 필요 없습니다. 원격 운영 서버 배포·재시작은 이번 로컬 검증에 포함하지 않았습니다.
