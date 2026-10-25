# 동행 관측소 — 최종 통합 모델

전국 I5 후속 작업: 공식 2025-07~12 주민등록 인구를 확보해 전국 읍면동·월 21,698행과 5자리 지역·월 1,584행을 생성했습니다. agent/service의 별도 I5 조회와 실제 조사 근거 연결 코드를 추가했습니다. 최신 단계별 결과는 [전국 I5 진행 보고](final_model/README.md)를 참고하세요. 아래 서울 복구 기록은 이전 단계이며 전국 I5를 서울 417개 지수로 대체한 것이 아닙니다.

2026-10-17: 기존 서울 417개 행정동 참고지표를 별도 조회로 복구하고 현재 서비스 저장소 snapshot과 전체 내용 일치를 검증했습니다. 서울 C1은 25개 구로 전달 범위를 넓혔습니다. 새 I5·행동 산식과 hold/경보 불허는 유지합니다. 단계별 결과는 [지역 지원 복구 보고](final_model/README.md), 조회 방법은 [최신 인계 안내](final_model/reports/AGENT_HANDOFF.md)를 참고하세요. 전국 자료 제공 완료나 운영 배포 완료를 뜻하지 않습니다.

이 저장소는 V3 행동변화와 C3 과거 동일요일 보조 근거를 통합하고, AI 에이전트에 전달할 구조화된 근거를 생성합니다. 사회조사 I5·MOIS는 확정된 V3 집계를 별도 계약으로 유지합니다.

기존 개별 연구는 [model_mori](https://github.com/bigcontest-2026/model_mori)에 보존합니다. 이 저장소는 최종 통합 코드·계약·테스트·보고서와 검증된 전국 I5 집계 번들을 관리합니다.

- [구조·실행 방법·단계별 진행](final_model/README.md)
- [검증 결과](final_model/reports/VALIDATION_REPORT.md)
- [에이전트팀 인계](final_model/reports/AGENT_HANDOFF.md)
- [데이터 사전](final_model/reports/DATA_DICTIONARY.md)

## 설치

실측 검증 환경은 Python 3.14.7입니다. 저장소 루트에서 전용 가상환경을 만들고 설치합니다.

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

configs/example.json을 참고해 final_model/configs/local.json에 실제 입력·코드 위치를 지정합니다. 재계산에는 isolation-index의 기존 모듈, C3/C1 전체 코드와 원자료, M1/M2/M3의 보존 결과가 필요합니다. 파일 구성은 원천 감사표와 인계 문서를 참고하세요.

    .\.venv\Scripts\python.exe -X utf8 -m final_model.src.run --config final_model/configs/local.json
    .\.venv\Scripts\python.exe -X utf8 -m pytest final_model/tests -q

원자료·행동 모델 세부 출력·로컬 설정은 Git에 포함하지 않습니다. 검증된 전국 I5 집계 결과는 아래 경로에 포함합니다. 실제 agent/service 도구 연결은 구현·로컬 검증했으며, 공급 품질 hold와 운영 경보 불허 상태를 유지합니다. 실행 중인 서버 반영은 최신 코드·데이터를 받은 뒤 재시작해야 합니다.

## 지역별 설문 기반 I5 추가

[시군구·행정동별 참고지수](final_model/reports/SURVEY_AREA_INDEX.md): 기존 행동 출력과 별도로 전국 2025년 7~12월 I5 조회 결과를 제공합니다. 해당 문서의 강남·춘천 기록은 초기 검증 범위입니다.

- [최종 병합 검수 현황](final_model/reports/MODEL_FINAL_REVIEW.md): 원본 V3 대조 완료 및 남은 보완사항.

## 전국 I5 데이터 포함 및 서비스 기본 연결 (2026-10-17)

- [검증된 번들](data/i5/national_2025/): 읍면동·월 21,698행, 시군구/시·월 1,584행, 행정구역 사전, manifest, 검증 보고서 및 미산출 사유를 포함한 7개 파일입니다. 원천 MOIS CSV는 포함하지 않습니다.
- `model`, `agent`, `service`를 같은 부모 폴더에 두고 최신 버전을 받으면 서비스는 `model/data/i5/national_2025`를 기본 사용합니다. `I5_BUNDLE_PATH`는 데이터를 다른 위치에 둘 때만 지정합니다. 환경변수 값은 파일이 아니라 폴더의 경로입니다.
- Git checkout에서도 manifest 해시가 유지되도록 이 데이터 경로는 `.gitattributes`의 `-text`를 적용합니다. 원본 검증 번들과 바이트 단위로 같습니다.
- I5는 전국 설문 평균에 지역 성별·연령별 인구를 가중한 참고지수입니다. 지역 직접 설문값·개인 고립확률이 아니며 `hold`, `operational_alert_allowed=false`를 유지합니다.
- GitHub Release의 ZIP은 별도 전달용입니다. Git으로 받는 환경에서는 추가 압축 해제가 필요 없습니다. 서버 운영자는 저장소 업데이트, 패키지 업데이트 및 백엔드 재시작 후 `/api/research/i5?region_code=26110&month=202512`와 실제 조사 근거를 확인합니다.
