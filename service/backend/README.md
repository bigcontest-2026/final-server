# 동행 관측소 HTTP API

기본 `APP_MODE=final`은 형제 `model/final_model`의 `query()`와 `survey_area_index.query()`를 호출합니다. 이전 `donghaeng-model` 설치나 조사 에이전트 실행 없이 조회할 수 있습니다.

이 폴더에서 `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements-dev.txt`로 설치한 뒤 `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`으로 실행합니다. I5 생성과 실측 번들 설정은 [Service README](../../README.md)를 따릅니다.

- `app/final_model.py`: 현재 모델 코드 로딩, 번들 탐색·무결성·스키마 검사
- `app/final_model_api.py`: 현재 모델용 읽기 전용 HTTP API
- `app/legacy_main.py`: 선택 설치하는 이전 조사·시연 모드

`pytest`는 이 폴더에서 실행합니다. 상세 계약과 검증 범위는 [현재 모델 연결 문서](../docs/FINAL_MODEL_INTEGRATION.md)를 참고하세요.
