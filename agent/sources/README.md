# 공식 지원 취득·권리 기록

이 폴더의 기존 파일은 2026-10-05 실행 당시의 기록과 스크립트를 바이트 그대로 보존했다. 파일의 예전 `service/shared/support`, `service/backend/evidence/support`, `output/...` 경로는 역사적 위치이며 현재 실행 경로가 아니다. `PRESERVATION.json`에 원본과 같은 SHA-256을 기록했다. 전체 웹 원문이 있는 `official_raw/`는 복사하지 않았다.

- `acquisition_ledger.json`: 실제 취득 시각·경로·응답·실패 기록.
- `rights_manifest.json`: 자료별 사용·공개 범위 확인 기록.
- `support_source_verification.json`, `welfare_catalog_audit.json`: 당시 원문·문구·CSV 검사.
- `B4_공식지원_취득검증.md`, `공식요건_모델링_대응표.md`: 취득 범위와 미확인 사항.
- `acquire_official_sources.py`: 새 빈 디렉터리에 공식 공개 페이지·CSV를 다시 취득하는 독립 도구. 상세 API·로그인 접수·기관 연락은 수행하지 않는다.
- `build_support_manifest.py`: 기존 구조와 당시 확인한 조건을 담은 역사적 빌더. 현재 패키지의 실행 명령으로 사용하지 않는다. 새 원문에서 과거 조건을 그대로 재사용하면 안 된다.

현재 자료 경로와 재취득·검토 절차는 [지원 자료 운영 안내](../docs/SUPPORT_RESOURCES.md)를 따른다. 보존한 권리 기록은 현재 재취득본의 권리를 새로 확인했다는 뜻이 아니다.
