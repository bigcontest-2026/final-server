# 통합 구현과 원본 근거 연결

V1 `read_skt/selected_skt`의 선택·집계, V2 `previous_change`의 Q 산식, V3 `robust_group_stats`의 중앙값/MAD 정의, C3 기존 일별 집계·기준선·월 요약 함수를 사용한다. 완전한 10집단에서는 upstream 강건 통계를 그대로 호출하고, 일부 소스 결측 시 정상 소스의 10집단을 같은 중앙값/MAD 산식으로 별도 처리한다.

사회조사 I5/MOIS는 확정 V3 집계로 별도 유지한다. 기존 model_mori의 설문 50:50 계약으로 변환하지 않는다. C3는 `historical_reference_id`로 연결하며 해당되는 키만 조회한다.

## 후처리 완료 상태

`pipeline`은 `core_ready`까지만 생성한다. `run`의 외부 맥락·스키마·저장 관측 재계산 검증을 모두 통과한 뒤 `completed`를 기록하고 최신 포인터를 게시한다. 후처리 예외는 `failed`로 남기며 공개 조회는 core_ready/validating/failed를 거부한다. manifest·포인터 JSON은 임시 파일 교체로 기록한다.

`run --bundle <완료 번들> --no-publish`는 별도 디렉터리로 복제해 재검증한다. `core_ready` 번들은 해당 경로에서 최종 검증한다. `--no-publish`는 기존 최신 포인터를 유지한다. 이 검수에서는 기존 번들 및 최신 포인터를 수정하지 않았다.

C3 비교 가능 합계가 null이면 검증 불가 상태를 유지하며 0으로 대체하지 않는다. V3 Q가 null인 경우 감소 판정도 null이다. 비교 가능 날짜가 있으면 합계·중복 날짜·원 일별 키를 검증하고, 합계/날짜의 모순은 오류로 처리한다. 일별 데이터가 있더라도 C3 월 coverage가 부족해 판정이 보류된 경우 원 변화율과 null 판정을 함께 보존한다.

## 기존 V3 설명·검증 근거 연결

[review_v3.py](../src/review_v3.py)는 전달 ZIP의 주 Step7_5 계보를 검증하고 별도 검수 디렉터리를 생성한다. current_rebase 계보는 사용하지 않는다. 기존 행동 JSONL의 계약·숫자·provenance는 바꾸지 않는다.

실행 결과: `outputs/v3_review_20261015_060929_123525/` (로컬, Git 제외).

- `validation_report.json`: 행동 100키·수치 1,600개·판정 1,300개·품질 300개, 동 SKT 2,820개·전월 변화 2,350개 대사.
- `original_v3_explanation_references.jsonl`: 관측 키별 원본 설명·원천별 품질·지역 상대 변화·방법 관계·reason codes·합성 반례·달력/카드 범위 민감도 100개. ZIP SHA256, 원본 member, 실제 줄 번호와 줄 바이트 해시를 포함한다.
- `manifest.json`: Step7_5 입력/출력, Step7_6 연결, 합성·달력 manifest와 상세 파일, 기존 보고서 등 검증한 참조 28개 및 생성 파일 해시를 기록한다. upstream dong CSV는 raw, 코드·설정은 명시한 LF 바이트로 확인한다.

원본 보고서는 ZIP의 `02_v3_behavior/v3/behavior/reports/` 아래 `STEP7_3_ANOMALY_COMPARISON.md`, `STEP7_4_FULL_VALIDATION.md`, `STEP7_5_FULL_PATTERN_ANALYSIS.md`, `STEP7_6_HANDOFF_REPORT.md`다. 상세 실험은 `02_v3_behavior/outputs/v3/behavior/step7_4_full/`의 injection/calendar manifest와 CSV로 연결된다.

원본 설명은 같은 관측값이 검증된 키에 대한 참조다. 새로운 원자료 결과가 달라지면 원본 비교가 실패하므로 그대로 재사용할 수 없다. 기존 1,024조건 합성 검증을 새 코드에서 다시 실행했다는 표시를 승계하지 않는다. 이는 공동 검수용 근거 연결이며 서비스·에이전트 구현이나 실제 인수인계가 아니다.
