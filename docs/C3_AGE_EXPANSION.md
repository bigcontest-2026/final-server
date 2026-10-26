# C3 연령 확장 및 재계산

C3 계산 소스와 조회 스키마는 20·30·40·50대·60세 이상을 지원한다. `60+`는 V3의 `60GU`에 연결한다. 60·70·80·90대는 원본 중복 검사 후 일별 건수·금액을 합산하고 같은 요일 기준선을 계산한다.

`hermes-complete/model/final_model`의 기존 V3/C3 구조를 유지하며, 확장 계산은 `submission/code/vendor/C3_Shinhan_baseline`의 1.1.0 구현을 사용한다. I5와 활동 분류 규칙은 변경하지 않는다.

## 실제 데이터 반영 상태

2026-10-26 작업 시 신한 원자료 ZIP과 확장 산출물은 로컬에 없었다. 따라서 포함된 `bundles/activity`는 **20·30대 C3 880행·24셀 연결 상태**이며, 코드 수정만으로 40·50대·60세 이상 값을 생성하지 않는다. 신규 연령의 실제 서비스 반영은 다음 재계산과 게시 검증 후 완료된다.

필요한 ZIP의 SHA256은 `5dd537389bbda489e8ac7c771e1ab56ed2f7d9d0fb325ecd300768f2061aa8b6`이며, 기본 번들 입력 해시와 일치해야 한다.

## 별도 번들 생성

저장소 루트에서 실행한다. 오프라인 재계산 의존성은 서비스 실행 환경과 별도 가상환경에 설치해도 된다.

```bash
python3 -m venv .venv-c3
.venv-c3/bin/python -m pip install -r model/C3_Shinhan_baseline/requirements_c3.txt jsonschema
.venv-c3/bin/python scripts/expand_c3.py \
  --card-zip /실제/경로/2026빅콘테스트_신한카드_데이터_레이아웃.zip \
  --output runtime/c3-20plus
```

출력 폴더는 새 경로여야 한다. 원자료 해시, 기존 C3 건수·금액·감소 판정, 10~12월 기대 키, V3 원 월건수, 스키마와 파일 해시를 검증한다. V3 100셀에서는 C3 참조 ID만 바뀌고, 8~9월 40셀은 미연결 상태를 유지한다. 현재 두 지역에서는 10~12월 60셀에 연결된다. 기존 번들과 실행 설정은 이 명령으로 바뀌지 않는다.

생성 번들로 별도 API를 실행하려면 `FINAL_MODEL_BUNDLE`을 해당 절대경로로 지정한다. `/api/model/behavior?month=202510&limit=100`에서 모든 20행에 C3가 연결되는지, 각 연령 필터가 자기 연령을 반환하는지 확인한다. 운영 프로세스에는 검증 후 이 경로를 적용한다.

## 회귀검사

```bash
PYTHONPATH=model:service/backend PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest model/final_model/tests/test_c3_expansion.py \
  service/backend/tests/test_c3_age_api.py -q
```

테스트의 임시 원자료와 번들은 테스트 디렉터리에만 생성된다. 서비스 자료로 게시하지 않는다.
