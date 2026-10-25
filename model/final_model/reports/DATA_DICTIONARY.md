# 에이전트 입력 데이터 사전

| 파일/필드 | 자료형·단위 | 정의 |
|---|---|---|
| behavior_evidence.key | 문자열 | month=YYYYMM, 시군구 11680/51110, MAN/WMAN, 20G/30G/40G/50G/60GU |
| sources.skt.activity_daily | 수치 또는 null | 월 일평균 장소 기반 유동량의 선택 격자 합. 월 일수로 다시 나누지 않음 |
| sources.card_a.activity_daily | 수치 또는 null | 가맹점 결제 월건수 / 해당 월 달력 일수 |
| sources.*.previous_activity_daily | 수치 또는 null | 전월 같은 관측집단 활동량 |
| sources.*.change_percent | 수치 또는 null, % | 100*(현재/전월-1). 음수=감소 |
| sources.*.Q | 수치 또는 null | -ln(현재/전월). 양수=감소 |
| sources.*.H | 수치 또는 null | 같은 시군구·월 10집단 Q 중앙값. 부족하면 null |
| sources.*.T | 수치 또는 null | Q-H. 지역 공통 변화 대비 상대 변화 |
| sources.*.robust | 객체 | 동일 10집단 T의 median/MAD 기준 z. Q>0 및 z>=3.5 연구 후보. MAD=0이면 null |
| rule.candidate | bool 또는 null | SKT·카드 모두 Q>0. 고립 확정 아님 |
| rule.two_month_streak | bool 또는 null | 이전 비교월도 양원천 감소. 8월은 이전 비교 기록 없어 null |
| historical_reference_id | 문자열 또는 null | 별도 C3 record_id. 공통 10~12월 남녀×20/30대만 연결 |
| historical_reference.count | 객체, transactions | reported_sum은 전체 날짜, comparable_sum/baseline_sum은 동일 비교 가능 비휴일 날짜 집합 |
| historical_reference.count.decline_flag | bool 또는 null | 기준 대비 10% 이상 감소. false와 null을 구별 |
| historical_reference.amount | 객체, source_amount_unit | 금액 단위 미확인. 원자료 숫자 유지 |
| historical_reference.daily_support | 객체 | 일별 MAD·IF 보조 건수. 동일 원천의 독립 증거 아님 |
| coverage.skt_coverage_ratio | null | 기대 격자 모집단 미검증으로 비율 생성 안 함 |
| spatial_context.status | not_comparable | M3 격자와 지역별 신호의 공식 공간 정렬 미검증 |
| quality.status | hold | 계산 검증 통과와 공급 품질 승인 구분 |
| operational_alert_allowed | false | 자동 운영 경보 불허 |
| provenance | 객체 | 실행ID, 코드·설정·입력 해시, 분석기간. 과거 공표시점 미복원 |
| dong_skt_evidence | 객체 | 행정동 SKT 단독. card_observation=null. 시군구 카드 복제 금지 |
| survey_mois_reference/ | V3 CSV 6종 | survey_mois_reference_v1 동결 집계. 전국 6집단과 행동 10집단 자동 1:1 조인 없음 |
| region_summary | 객체 배열 | 같은 지역·월 10집단의 후보 수. 사람 수·고립률 아님 |

관측 0은 숫자 0이고 결측은 null이다. 비양수 분모·기초량의 로그 변화는 unavailable/null로 보류한다.
