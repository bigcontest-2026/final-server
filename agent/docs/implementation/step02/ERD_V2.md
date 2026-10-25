# Step 02 수정: ERD 기반 임시 DB v2

2026-10-25 사용자 선택: **공식 API 분류 + 별도 I5 연결로 전환**.

근거: [Notion ERD 1차 수정안](https://app.notion.com/p/4033719c0448802bacfef71e0c8285f8).
하위 ‘초안’에는 9태그가 남아 있으나 본문의 1차 수정안과 이번 사용자 선택을 우선한다.
본문 표·설명으로 논리 관계를 확인했다. 이미지의 세부 컬럼을 전부 복제한 물리 스키마가 아니다.

## 이전 DB와 차이

이전 welfare_db.py는 corpus / programs(JSON) / program_tags 3테이블이었다.
그 DB와 16개 가상 사업은 회귀시험용으로 보존한다. 9태그를 공식 분류로 자동 변환하지 않는다.
새 실행 경로는 welfare_erd.py이며 기존 그래프에 자동 연결되지는 않았다.

| 그룹 | 새 SQLite 테이블 | 역할 |
|---|---|---|
| 사업 | sources, programs, program_versions | 출처별 원본 ID와 사업 버전 분리 |
| 분류 | source_categories, program_source_categories, category_relations | 출처별 분류, 다중 연결, 검토된 동등·관련 관계 |
| I5 | i5_domains, program_i5_domains | E/P/O/C/U 연결 사유·검토 상태·규칙 버전 |
| 지역 | administrative_regions, program_regions | 코드체계 및 nationwide/specific/unknown/conflicting 구분 |
| 조건·근거 | eligibility_rules, evidence | 연령·성별·개인조건과 근거 |
| 공개 | publications, publication_members | 지정된 공개 묶음의 버전만 검색 |

총 14테이블. PostgreSQL 운영 DB는 구축하지 않았다. source_documents, ingestion_runs,
fetch_events, active_publications, 코드체계 매핑, 대상 라벨, 중복 검토·장기 보존은 후속이다.
publication_id를 호출자가 명시한다. 공개 전환·실제 수집 검증은 아직 구현하지 않았다.

## 검색 및 검토

공식 분류 ID 정확 일치 → 승인된 관련 분류 → 내용 키워드 순서로 검색한다.
I5 영역 관련 사업도 별도로 검색할 수 있고, 사업 버전별로 중복을 제거한다.
현재 내용 검색은 부분문자열 검색이며 의미 검색이나 LLM 심사는 아니다.
사업 내용 확인 상태는 입력 데이터의 검토값이다. 자동 원문 확인 기능은 미구현이다.
지역·연령·기간의 명백한 불일치는 제외하고, 미확인은 별도 조건으로 유지한다.
지역 계층은 호출자가 검증한 region_ids로 전달하며 코드 접두사 추정은 하지 않는다.
성별 제한 등의 일반 조건 비교는 후속 구현이며 현재 합성 자료는 미확인으로 반환한다.

## 실행

저장소 상위 bigcontest-2026에서 PowerShell:

```powershell
$env:PYTHONPATH='agent/src'
python -m donghaeng_agent.welfare_erd --db agent/runtime/welfare_demo/welfare_erd_v2.sqlite --output agent/runtime/welfare_demo/erd_recommendations.json
python -m unittest discover -s agent/tests -p 'test_welfare_*standalone.py' -v
```

기존 파일을 덮어쓰지 않는다. DB 생성은 새 경로에만 가능하다.
가상 출처 demo-api와 가상 코드만 사용한다. ‘생활지원’, ‘일자리’ 명칭이 있어도 실제 공식
사업에 부여된 값이 아니다. 공식 카테고리 전체 목록을 확보했다는 의미도 아니다.

실행 결과: 총 5건 / 미분류 1건 / 검색 4건.
추천 후보(needs_confirmation) 2건, 기간 불일치 제외 1건, 원문·내용 보류 1건.
모든 결과에 is_synthetic=true, quality_status=hold, operational_alert_allowed=false.

## 다음 연결

실제 I5/V3 → 조합 규칙 → 공식 분류 또는 I5 검색 의도 → 새 DB 조회 → 조건 검토 → 추천 설명.
실제 지표와 이 신규 DB의 자동 연결은 미완료다. 기존 9태그 MappingRule도 새 계약으로 교체해야 한다.
I5 고저 기준·세부 조합 규칙은 아직 미확정이며 이 DB 변경으로 확정하지 않는다.
지역 실제 원인 조사(C 확장)는 계속 후속이다.
