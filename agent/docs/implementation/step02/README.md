# Step 02 — 교체 가능한 임시 복지 DB

2026-10-25: SQLite 임시 DB와 검색 어댑터 구현. 실제 I5/V3 어댑터 및 최종 공식 복지 DB 연결은 아직 미완료다.

## 구조

- `corpus`: 코퍼스 버전·합성 여부.
- `programs`: 사업 ID·버전 및 내용/지역/연령/기간/개인 조건/출처를 JSON으로 저장.
- `program_tags`: 사업별 다중 태그와 검색 인덱스.
- `welfare_db.WelfareDB.search_welfare`: OR 검색, 중복 제거, 미분류 수와 coverage 반환.

이 구조는 임시 저장용이다. 최종 DB에서는 검색 어댑터만 같은 응답 계약으로 교체한다. 개인 자격 확인·공식 자료 최신성 확인을 구현한 것으로 취급하지 않는다.

## 합성 데이터

9개 분야 기본 레코드와 반례를 포함한 가상 사업 16건이다. 지역·연령·기간 불일치, 내용 불일치, 검증 미완료, 취업+관계 복수 태그, 미분류 사업을 포함한다. 실제 사업명·공식 URL은 생성하지 않았다.

합성 원문의 `source_verified=true`는 필터 분기 시험용 값이며 공식 출처 검증을 뜻하지 않는다. `allow_synthetic=True`를 명시해야 조회할 수 있고, 최종 추천에도 `is_synthetic=true`, `verification_scope=synthetic_fixture_only`를 전달한다. 9개 분야의 데이터를 담은 것과 9개 분야 매핑 규칙을 확정한 것은 다르다.

## 실행

프로젝트 루트에서:

```powershell
$env:PYTHONPATH='agent/src'
python -m donghaeng_agent.welfare_db --db agent/runtime/welfare_demo/welfare.sqlite --output agent/runtime/welfare_demo/recommendations.json
python -m unittest discover -s agent/tests -p 'test_welfare_*standalone.py' -v
```

기존 DB는 덮어쓰지 않는다. CLI는 기존 DB가 있으면 조회만 한다. 다른 초기 데이터를 만들려면 새 DB 경로를 지정한다. DB와 실행 결과는 Git 제외 runtime에 저장한다.

## 실제 검증 결과

- 합성 DB 생성 및 규칙→SQLite 검색→사업 필터→JSON 추천 실행 완료.
- 관계·참여 태그 검색 7건: 추가 확인 후보 2건, 명백한 불일치 제외 4건, 출처 검증 보류 1건.
- 전체 코퍼스 16건, 미분류 1건을 coverage에 표시.
- 기존 코어 포함 11개 테스트 통과. 최초 샌드박스 실행은 Windows 임시 폴더 권한으로 DB 시험이 실패했고, 권한 승인을 통한 재실행에서 모두 통과했다.

## 다음 단계

실제 활동신호/I5 조건 어댑터, graph 연결, 서비스 API·UI 연결은 별도다. 미정 I5 임계값을 시연 입력으로 확정하지 않는다. 공식 DB 전환 시 내용 적합성 근거·원문 검증·사업 조건을 실제 수집 데이터로 공급해야 한다.
