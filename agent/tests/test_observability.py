import json

import pytest

from donghaeng_agent import observability as obs


@pytest.fixture(autouse=True)
def isolated_tracer():
    obs.shutdown_observability()
    yield
    obs.shutdown_observability()


@pytest.fixture
def exported():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    assert obs.init_observability(obs.TraceConfig(enabled=True), exporter=exporter)["enabled"]
    return exporter


def test_disabled_tracing_never_changes_business_errors():
    assert obs.init_observability()["status"] == "disabled"
    with obs.trace_execution("case", "execution") as trace:
        assert trace.trace_id is None
        with obs.trace_step("tool", "TOOL", input={"public": True}) as span:
            span.set_output({"result": 1})
    with pytest.raises(RuntimeError, match="business failure"):
        with obs.trace_step("step"):
            raise RuntimeError("business failure")


def test_nested_spans_link_execution_and_preserve_unknown_subscription_cost(exported):
    with obs.trace_execution("case_1", "execution_1") as execution:
        with obs.trace_step("model.decide", "LLM", input={"evidence_ids": ["ev_public"]}) as model:
            model.set_attributes({"provider": "codex_cli", "auth_mode": "chatgpt_subscription",
                "billing_mode": "subscription", "model": "requested-model", "cost_usd": 999,
                "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": None}})
            model.set_output({"text": "공개 자료를 확인했습니다."})
            model_id = model.span_id
        root_id = execution.span_id
        trace_id = execution.trace_id
    assert obs.flush_observability()
    spans = exported.get_finished_spans()
    model, root = spans
    assert format(model.context.span_id, "016x") == model_id
    assert format(root.context.span_id, "016x") == root_id
    assert model.parent.span_id == root.context.span_id
    assert format(model.context.trace_id, "032x") == trace_id
    assert root.attributes["session.id"] == "case_1"
    metadata = json.loads(model.attributes["metadata"])
    assert metadata["cost_usd"] is None
    assert metadata["token_usage_status"] == "reported"
    assert metadata["usage"]["cached_input_tokens"] is None
    assert model.attributes["llm.token_count.prompt"] == 100
    assert "llm.model_name" not in model.attributes


def test_credentials_personal_contacts_and_reasoning_are_redacted_before_export(exported):
    with obs.trace_step("tool", input={"apiKey": "sensitive-value", "authorization": "Bearer hidden",
        "token": "session-secret", "contact": "person@example.com 010-1234-5678",
        "api_response": "sk-proj-secretlongvalue", "stderr": "private diagnostics",
        "nested": [{"type": "reasoning", "text": "private reasoning"}]}) as span:
        span.set_output({"ok": True, "refresh_token": "refresh-secret"})
    obs.flush_observability()
    attrs = json.dumps(dict(exported.get_finished_spans()[0].attributes))
    for secret in ("sensitive-value", "session-secret", "person@example.com", "010-1234-5678",
                   "sk-proj-secretlongvalue", "private diagnostics", "private reasoning", "refresh-secret"):
        assert secret not in attrs
    assert "REDACTED" in attrs


def test_errors_never_export_raw_message_or_stack(exported):
    with pytest.raises(ValueError):
        with obs.trace_step("model.call"):
            raise ValueError("raw account and credential diagnostic")
    obs.flush_observability()
    span = exported.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == "execution_failed"
    assert not span.events
    assert "raw account" not in str(span.attributes)


def test_failed_call_reports_unknown_usage_not_zero(exported):
    with obs.trace_step("model.call", "LLM", metadata={"provider": "codex_cli", "billing_mode": "subscription"}) as span:
        span.set_error("model_failed")
    obs.flush_observability()
    attributes = exported.get_finished_spans()[0].attributes
    metadata = json.loads(attributes["metadata"])
    assert metadata["usage"] is None
    assert metadata["token_usage_status"] == "not_reported"
    assert not any(k.startswith("llm.token_count") for k in attributes)


def test_human_review_interrupt_is_control_flow_not_failure(exported):
    from langgraph.errors import GraphInterrupt
    with pytest.raises(GraphInterrupt):
        with obs.trace_step("graph.review"):
            raise GraphInterrupt(())
    obs.flush_observability()
    span = exported.get_finished_spans()[0]
    assert span.status.status_code.name == "UNSET"
    assert json.loads(span.attributes["metadata"])["control_flow"] == "graph_interrupt"
    assert "error.code" not in span.attributes
    assert not span.events


def test_reasoning_token_count_is_allowed_but_reasoning_text_is_not():
    assert obs.sanitize({"reasoning_output_tokens": 12})["reasoning_output_tokens"] == 12
    assert obs.sanitize({"reasoning_output_tokens": None})["reasoning_output_tokens"] is None
    assert obs.sanitize({"reasoning_output_tokens": "private content"})["reasoning_output_tokens"] == obs.REDACTED


@pytest.mark.parametrize("endpoint", ["https://host.example/v1/traces", "http://192.168.1.5:6006",
    "http://localhost:6006/?token=hidden", "http://user:password@127.0.0.1:6006",
    "http://127.0.0.1:6006/other"])
def test_remote_or_credential_endpoint_is_rejected(endpoint):
    with pytest.raises(ValueError, match="phoenix_endpoint_must_be_local_http"):
        obs.local_endpoint(endpoint)


def test_bad_configuration_is_reported_without_interrupting_business():
    result = obs.init_observability(obs.TraceConfig(enabled=True, endpoint="https://external.invalid"))
    assert result["status"] == "unavailable"
    assert result["last_error_code"] == "phoenix_configuration_invalid"
    with obs.trace_step("business") as span:
        assert span.span_id is None


def test_exporter_outage_is_counted_without_failing_business():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SpanExporter
    class FailedExporter(SpanExporter):
        def export(self, spans):
            raise RuntimeError("private network diagnostic")
    failed_before = obs.observability_status()["failed_spans"]
    obs.init_observability(obs.TraceConfig(enabled=True), exporter=FailedExporter())
    with obs.trace_step("business") as span:
        span.set_output({"business_completed": True})
    obs.flush_observability()
    status = obs.observability_status()
    assert status["failed_spans"] == failed_before + 1
    assert status["last_error_code"] == "phoenix_export_failed"


def test_large_and_unknown_values_are_bounded():
    class Secret:
        def __repr__(self):
            return "SECRET_REPR"
    safe = obs.sanitize({"value": "x" * 10000, "items": list(range(200)), "object": Secret()})
    assert len(safe["value"]) < 4100
    assert len(safe["items"]) == 40
    assert safe["object"] == "[UNSUPPORTED_TYPE]"


def test_local_transport_drops_inherited_credentials_and_disallows_redirects(monkeypatch):
    exporter_module = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    import requests
    captured = {}
    class Exporter:
        def __init__(self, **kwargs):
            captured["session"] = kwargs["session"]
        def shutdown(self):
            pass
    def request(self, method, url, **kwargs):
        captured["request"] = kwargs
        return object()
    monkeypatch.setattr(exporter_module, "OTLPSpanExporter", Exporter)
    monkeypatch.setattr(requests.Session, "request", request)
    assert obs.init_observability(obs.TraceConfig(enabled=True))["enabled"]
    session = captured["session"]
    assert session.trust_env is False
    session.request("POST", "http://127.0.0.1:6006/v1/traces",
                    headers={"Authorization": "Bearer unrelated", "custom-secret": "value", "Content-Type": "application/x-protobuf"},
                    allow_redirects=True, cert="private.pem")
    assert captured["request"]["headers"] == {"Content-Type": "application/x-protobuf"}
    assert captured["request"]["allow_redirects"] is False
    assert captured["request"]["cert"] is None
    with pytest.raises(ValueError):
        session.request("POST", "https://external.example/v1/traces")
