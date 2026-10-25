"""Synthetic privacy strings only; no real credential or personal record is read."""
import copy
import json

import pytest

from donghaeng_agent.model_input import (MAX_DEPTH, MAX_INPUT_BYTES, MAX_NODES,
    ModelInputRejected, prepare_model_input)


def test_canonical_json_content_is_minimized_without_changing_evidence():
    evidence = {"evidence_id": "ev_reference_123", "version_hash": "a" * 64, "model_version": "I5_2025_final_v1",
                "i5_reference": 33.33105401585454, "Q": -0.2345, "missing": None,
                "operational_alert_allowed": False, "quality_status": "hold",
                "source_quote": "공식 자료의 지원 대상과 접수 기간은 담당 창구에서 확인합니다.",
                "purpose": "공식 사업의 공개 목적"}
    source = [{"role": "user", "content": json.dumps({
        "scope": {"region_code": "2611051000", "purpose": "전송하지 않는 개인 자유문", "idempotency_key": "not-evidence"},
        "observations": [evidence], "authorization": "unstructured-test-secret"}, ensure_ascii=False)}]
    original = copy.deepcopy(source)
    cleaned = prepare_model_input(source)
    decoded = json.loads(cleaned[0]["content"])
    assert decoded["scope"] == {"region_code": "2611051000"}
    assert decoded["observations"] == [evidence]
    assert "authorization" not in decoded
    assert source == original


@pytest.mark.parametrize("secret", [
    "sk-proj-synthetic0123456789", "Bearer synthetic-authorization-token", "api_key=synthetic-secret-123",
    "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.signature",
    "example.person@example.invalid", "010-1234-5678", "+82 10 1234 5678", "900101-1234567", "9001011234567",
])
def test_sensitive_text_is_masked_in_source_quotes_without_removing_the_quote(secret):
    result = prepare_model_input({"source_quote": "공식 원문의 확인 문구 " + secret + " 끝"})
    assert "source_quote" in result
    assert secret not in result["source_quote"]
    assert "[REDACTED]" in result["source_quote"]
    assert result["source_quote"].startswith("공식 원문의 확인 문구 ")


def test_camelcase_credentials_and_private_payloads_are_omitted():
    result = prepare_model_input({"apiKey": "secret", "Authorization": "secret", "refreshToken": "secret",
        "nested": {"client_secret": "secret", "personal_raw_text": "unnecessary private prose"},
        "token_count": 120, "reasoning_output_tokens": 50, "token": 7})
    assert result == {"nested": {}, "token_count": 120, "reasoning_output_tokens": 50, "token": 7}


def test_nested_json_strings_cannot_bypass_structured_key_removal():
    encoded = json.dumps(json.dumps({"api_key": "synthetic-unprefixed-secret", "scope": {"purpose": "omit", "region_code": "11680"}}))
    result = prepare_model_input({"content": encoded})
    assert json.loads(json.loads(result["content"])) == {"scope": {"region_code": "11680"}}
    assert prepare_model_input(result) == result


def test_protected_identifier_is_rejected_instead_of_corrupting_reference():
    with pytest.raises(ModelInputRejected, match="model_input_sensitive_identifier"):
        prepare_model_input({"evidence_ids": ["person@example.invalid"]})


def test_original_numbers_versions_and_public_prose_are_preserved():
    source = {"value": 0, "negative": -50.125, "null": None, "date": "2025-12", "source_id": "published_research_snapshot",
              "record_id": "survey_i5:2025:administrative_dong:2611051000:202512",
              "source_quote": "[참고] 2025년 12월 인구 2,000명, I5 35.2점, 예산 1000000000000원",
              "schema_version": "survey_area_isolation_index_v1"}
    assert prepare_model_input(source) == source


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(ModelInputRejected, match="model_input_non_finite_number"):
        prepare_model_input({"value": value})


def test_size_is_rejected_even_when_oversized_private_field_would_be_removed():
    with pytest.raises(ModelInputRejected, match="model_input_too_large"):
        prepare_model_input({"scope": {"purpose": "x" * MAX_INPUT_BYTES}})


def test_depth_and_expanded_json_depth_are_rejected():
    value = "public"
    for _ in range(MAX_DEPTH + 1):
        value = [value]
    for input_value in (value, {"content": json.dumps(value)}):
        with pytest.raises(ModelInputRejected, match="model_input_too_deep"):
            prepare_model_input(input_value)


def test_node_limit_and_duplicate_embedded_keys_are_rejected():
    with pytest.raises(ModelInputRejected, match="model_input_too_many_nodes"):
        prepare_model_input([None] * MAX_NODES)
    with pytest.raises(ModelInputRejected, match="model_input_duplicate_json_key"):
        prepare_model_input({"content": '{"i5": 1, "i5": 2}'})


def test_unsupported_values_are_not_coerced_to_repr():
    with pytest.raises(ModelInputRejected, match="model_input_unsupported_type"):
        prepare_model_input({"value": object()})


@pytest.mark.parametrize("value", ["\ud800", '{"value":"\\ud800"}', '{"value":' + "9" * 5000 + '}'])
def test_invalid_unicode_and_oversized_embedded_integer_have_safe_error_codes(value):
    with pytest.raises(ModelInputRejected, match="model_input_invalid_json"):
        prepare_model_input({"content": value})
