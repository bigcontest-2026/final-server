"""Explicit, opt-in Phoenix tracing with redaction before any exporter sees data.

Business state never depends on Phoenix. No instrumentor is installed globally:
Codex CLI and application tool calls are traced at their explicit call boundaries.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
import math
import os
import re
import threading
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"
MAX_TEXT_LENGTH = 4000
MAX_ITEMS = 40
MAX_DEPTH = 6
_SENSITIVE_KEYS = re.compile(
    r"(^|[_.-])(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|password|passwd|secret|credential|cookie|auth[_-]?json|"
    r"reasoning|analysis|chain[_-]?of[_-]?thought|stderr|stdout|raw[_-]?diagnostics)([_.-]|$)",
    re.IGNORECASE,
)
_PATTERNS = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer " + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), REDACTED),
    (re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), REDACTED),
    (re.compile(r"(?<!\d)(?:\+82[- ]?|0)1[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"), REDACTED),
    (re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)"), REDACTED),
)
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,159}$")
_lock = threading.RLock()
_provider: Any = None
_tracer: Any = None
_state: dict[str, Any] = {"enabled": False, "status": "disabled", "exported_spans": 0,
                          "failed_spans": 0, "started_spans": 0, "finished_spans": 0,
                          "last_error_code": None}


def sanitize(value: Any, *, max_depth: int = MAX_DEPTH, _depth: int = 0, _budget: list[int] | None = None) -> Any:
    """Bound and redact structured values; never serialize arbitrary object reprs.

    This is defense in depth, not an authorization/PII classifier. Callers must
    submit public evidence summaries, not private records or raw CLI events.
    """
    if _budget is None:
        if not isinstance(max_depth, int) or not 1 <= max_depth <= 12:
            raise ValueError("sanitization_depth_must_be_1_to_12")
        _budget = [512]
    _budget[0] -= 1
    if _budget[0] < 0:
        return "[ITEM_BUDGET]"
    if _depth > max_depth:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        for pattern, replacement in _PATTERNS:
            value = pattern.sub(replacement, value)
        return value[:MAX_TEXT_LENGTH] + ("[TRUNCATED]" if len(value) > MAX_TEXT_LENGTH else "")
    if isinstance(value, Mapping):
        if value.get("type") in ("reasoning", "analysis", "command_execution"):
            return REDACTED
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                result["_truncated"] = True
                break
            key = str(key)[:160] if isinstance(key, (str, int)) else "unsupported_key"
            normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", key)
            safe_key = sanitize(key, max_depth=max_depth, _depth=_depth + 1, _budget=_budget)
            sensitive = _SENSITIVE_KEYS.search(normalized) or re.search(r"(^|[_.-])token$", normalized, re.I)
            if normalized == "reasoning_output_tokens" and (item is None or
                    isinstance(item, int) and not isinstance(item, bool) and item >= 0):
                sensitive = False  # Numeric usage is not internal reasoning text.
            result[safe_key] = REDACTED if sensitive else sanitize(item, max_depth=max_depth, _depth=_depth + 1, _budget=_budget)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item, max_depth=max_depth, _depth=_depth + 1, _budget=_budget) for item in value[:MAX_ITEMS]]
    return "[UNSUPPORTED_TYPE]"


def _json(value: Any) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def local_endpoint(endpoint: str) -> str:
    """Reject non-loopback exporters, credentials, query strings and redirects."""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("phoenix_endpoint_must_be_local_http")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        loopback, port = False, None
    if not loopback or port is None or parsed.path.rstrip("/") not in ("", "/v1/traces"):
        raise ValueError("phoenix_endpoint_must_be_local_http")
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1/traces", "", ""))


@dataclass(frozen=True)
class TraceConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:6006/v1/traces"
    project_name: str = "hermes-development"
    max_queue_size: int = 512
    max_export_batch_size: int = 64
    schedule_delay_millis: int = 1000
    export_timeout_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "TraceConfig":
        return cls(
            enabled=os.getenv("DONGHAENG_PHOENIX_ENABLED", "false").lower() in ("1", "true", "yes"),
            endpoint=os.getenv("DONGHAENG_PHOENIX_ENDPOINT", cls.endpoint),
            project_name=os.getenv("DONGHAENG_PHOENIX_PROJECT", cls.project_name),
        )


def init_observability(config: TraceConfig | None = None, *, exporter: Any = None) -> dict[str, Any]:
    """Start a private tracer provider. `exporter` supports isolated integration tests.

    Repeated initialization is idempotent. Invalid settings/missing optional
    dependencies disable tracing and expose an error code, never break the app.
    """
    global _provider, _tracer
    config = config or TraceConfig.from_env()
    with _lock:
        if _provider is not None:
            return observability_status()
        if not config.enabled:
            _state.update(enabled=False, status="disabled", last_error_code=None)
            return observability_status()
        try:
            endpoint = local_endpoint(config.endpoint)
            if not _IDENTIFIER.fullmatch(config.project_name):
                raise ValueError("invalid_phoenix_project_name")
            if not 1 <= config.max_export_batch_size <= config.max_queue_size <= 8192:
                raise ValueError("invalid_phoenix_buffer_limits")
            if not 100 <= config.schedule_delay_millis <= 10000 or not 0.1 <= config.export_timeout_seconds <= 5:
                raise ValueError("invalid_phoenix_export_timeout")
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
            if exporter is None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                import requests
                # A local collector must not redirect traces to an external host.
                class LocalSession(requests.Session):
                    def __init__(self):
                        super().__init__()
                        self.trust_env = False

                    def request(self, method, url, **kwargs):
                        local_endpoint(url)
                        kwargs["allow_redirects"] = False
                        # OTel merges header environment variables even when a
                        # caller supplies headers={}. Keep transport headers
                        # only so unrelated collector credentials never leak.
                        kwargs["headers"] = {k: v for k, v in (kwargs.get("headers") or {}).items()
                                             if k.lower() in {"content-type", "content-encoding", "user-agent"}}
                        kwargs["cert"] = None
                        return super().request(method, url, **kwargs)
                exporter = OTLPSpanExporter(endpoint=endpoint, timeout=config.export_timeout_seconds,
                                            session=LocalSession())

            class CountingExporter(SpanExporter):
                def export(self, spans):
                    try:
                        result = exporter.export(spans)
                    except Exception:
                        result = SpanExportResult.FAILURE
                    with _lock:
                        field = "exported_spans" if result == SpanExportResult.SUCCESS else "failed_spans"
                        _state[field] += len(spans)
                        _state["last_error_code"] = None if result == SpanExportResult.SUCCESS else "phoenix_export_failed"
                    return result

                def shutdown(self):
                    try:
                        exporter.shutdown()
                    except Exception:
                        pass

            _provider = TracerProvider(resource=Resource({
                "service.name": "donghaeng-agent", "service.version": "0.1.0",
                "openinference.project.name": config.project_name,
            }), shutdown_on_exit=False)
            _provider.add_span_processor(BatchSpanProcessor(
                CountingExporter(), max_queue_size=config.max_queue_size,
                max_export_batch_size=config.max_export_batch_size,
                schedule_delay_millis=config.schedule_delay_millis,
                export_timeout_millis=int(config.export_timeout_seconds * 1000)))
            _tracer = _provider.get_tracer("donghaeng_agent.explicit", "1.0")
            _state.update(enabled=True, status="configured", last_error_code=None,
                          project_name=config.project_name, endpoint=endpoint)
        except Exception as exc:
            _provider, _tracer = None, None
            _state.update(enabled=False, status="unavailable", last_error_code=(
                "phoenix_dependencies_missing" if isinstance(exc, ImportError) else
                "phoenix_configuration_invalid" if isinstance(exc, ValueError) else "phoenix_initialization_failed"))
        return observability_status()


def observability_status() -> dict[str, Any]:
    with _lock:
        return {**_state, "queued_or_dropped_spans": max(0,
            _state["finished_spans"] - _state["exported_spans"] - _state["failed_spans"])}


def flush_observability(timeout_millis: int = 3000) -> bool:
    provider = _provider
    if provider is None:
        return True
    try:
        return bool(provider.force_flush(timeout_millis=timeout_millis))
    except Exception:
        return False


def shutdown_observability() -> None:
    global _provider, _tracer
    # Do not hold the lock while a batch processor waits for its exporter.
    with _lock:
        provider, _provider, _tracer = _provider, None, None
        _state.update(enabled=False, status="shutdown")
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:
            pass


class TraceSpan:
    def __init__(self, span: Any = None):
        self._span = span
        self._metadata: dict[str, Any] = {}

    @property
    def trace_id(self) -> str | None:
        context = self._span.get_span_context() if self._span else None
        return format(context.trace_id, "032x") if context and context.is_valid else None

    @property
    def span_id(self) -> str | None:
        context = self._span.get_span_context() if self._span else None
        return format(context.span_id, "016x") if context and context.is_valid else None

    def set_output(self, output: Any) -> None:
        if self._span:
            self._span.set_attribute("output.value", _json(output))
            self._span.set_attribute("output.mime_type", "application/json")

    def set_error(self, error: Any) -> None:
        """Record only an allow-shaped error code; never raw exceptions/stacks."""
        if not self._span:
            return
        candidate = error if isinstance(error, str) else getattr(error, "code", None)
        code = candidate if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}", candidate) else "execution_failed"
        from opentelemetry.trace import Status, StatusCode
        self._span.set_attribute("error.code", code)
        self._span.set_status(Status(StatusCode.ERROR, code))

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        if not self._span:
            return
        safe = sanitize(attributes)
        self._metadata.update(safe)
        if "provider" in self._metadata:
            usage = self._metadata.get("usage")
            self._metadata["token_usage_status"] = "reported" if isinstance(usage, dict) and any(
                isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)
                for k in ("input_tokens", "output_tokens", "total_tokens")) else "not_reported"
            if not isinstance(usage, dict):
                self._metadata["usage"] = None
        # OTel doesn't support null attributes. JSON metadata preserves unknown
        # token counts and cost_usd=null rather than incorrectly writing zero.
        if self._metadata.get("billing_mode") == "subscription":
            self._metadata["cost_usd"] = None
            self._metadata["cost_scope"] = "subscription_not_api_billing"
        self._span.set_attribute("metadata", _json(self._metadata))
        for key in ("provider", "auth_mode", "billing_mode", "execution_id", "case_id", "strategy",
                    "model_call_succeeded", "execution_origin", "prompt_version", "schema_version"):
            value = self._metadata.get(key)
            if isinstance(value, (str, bool, int, float)):
                self._span.set_attribute("hermes." + key, value)
        tokens = self._metadata.get("usage")
        if isinstance(tokens, dict):
            for source, target in (("input_tokens", "prompt"), ("output_tokens", "completion"),
                                   ("total_tokens", "total")):
                value = tokens.get(source)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self._span.set_attribute("llm.token_count." + target, value)
            cached = tokens.get("cached_input_tokens")
            if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
                self._span.set_attribute("llm.token_count.prompt_details.cache_read", cached)
        # Suppress model-name-based API cost inference for subscription calls.
        # The actual/requested model remains available in sanitized metadata.
        if self._metadata.get("billing_mode") != "subscription" and self._metadata.get("model"):
            self._span.set_attribute("llm.model_name", str(self._metadata["model"]))


@contextmanager
def trace_step(name: str, kind: str = "CHAIN", input: Any = None,
               metadata: Mapping[str, Any] | None = None) -> Iterator[TraceSpan]:
    tracer = _tracer
    if tracer is None:
        yield TraceSpan()
        return
    safe_name = name if isinstance(name, str) and _IDENTIFIER.fullmatch(name) else "agent.step"
    safe_kind = kind if kind in {"CHAIN", "LLM", "TOOL", "RETRIEVER", "AGENT", "EVALUATOR"} else "CHAIN"
    # record_exception=False prevents the SDK adding raw messages/stack traces.
    with tracer.start_as_current_span(safe_name, attributes={"openinference.span.kind": safe_kind},
                                      record_exception=False, set_status_on_exception=False) as raw:
        span = TraceSpan(raw)
        with _lock:
            _state["started_spans"] += 1
        if input is not None:
            raw.set_attribute("input.value", _json(input))
            raw.set_attribute("input.mime_type", "application/json")
        span.set_attributes(metadata or {})
        try:
            yield span
        except BaseException as exc:
            from langgraph.errors import GraphInterrupt
            if isinstance(exc, GraphInterrupt):
                # A durable human-review pause is expected graph control flow.
                span.set_attributes({"control_flow": "graph_interrupt"})
                span.set_output({"status": "interrupted"})
            else:
                span.set_error(exc)
            raise
        finally:
            with _lock:
                _state["finished_spans"] += 1


@contextmanager
def trace_execution(case_id: str, execution_id: str,
                    metadata: Mapping[str, Any] | None = None) -> Iterator[TraceSpan]:
    with trace_step("investigation.execute", "AGENT", metadata={
        **(metadata or {}), "case_id": case_id, "execution_id": execution_id}) as span:
        if span._span:
            span._span.set_attribute("session.id", sanitize(case_id))
        yield span
