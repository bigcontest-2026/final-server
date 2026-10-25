import pytest

from donghaeng_agent.mock_contracts import Dataset


@pytest.fixture
def payload():
    return {
        "schema_version": "1.0", "mode": "mock", "as_of": "2026-09-29T09:00:00+09:00",
        "disclaimer": "합성 데이터", "districts": [{
            "code": "demo-001", "name": "가상동", "district": "가상구", "risk_score": 55,
            "risk_level": "watch", "previous_score": 48, "quality": "valid", "population": 15000,
            "signals": [{"key": "mobility", "label": "이동", "value": 76, "unit": "지수", "change_pct": -12}],
            "trend": [{"period": "2026-04", "score": 40}], "summary": "시연용",
        }], "policies": [{
            "id": "demo-policy-1", "title": "[가상] 연결 프로그램", "districts": ["*"],
            "target": "지역 주민", "tags": ["mobility"], "description": "시연용",
            "start_date": "2026-01-01", "end_date": "2026-12-31", "source_url": None,
            "is_mock": True, "contact": "실제 접수처 없음",
        }],
    }


@pytest.fixture
def dataset(payload):
    return Dataset.model_validate(payload)


