"""Native web-only transports for regional context research.

The established inference provider remains unchanged. This capability has its own
instructions, schema, event allowlist, and source verification boundary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

import httpx

from .analysis_research import RESEARCH_INSTRUCTIONS, RESEARCH_PROMPT_VERSION, ResearchDraft
from .provider import CodexSubscriptionProvider, ProviderUnavailable, _subscription_slot


def _json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non_finite")))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ProviderUnavailable("research_output_schema_error") from None


class ResearchEvents:
    """Only the transport, never generated prose, can record web execution."""
    def __init__(self):
        self.final = None
        self.completed = False
        self.usage = None
        self.web_ids = set()
        self.queries = []
        self.urls = []

    def web(self, item):
        identity = item.get("id")
        if not isinstance(identity, str) or not identity:
            raise ProviderUnavailable("research_invalid_web_event")
        if identity in self.web_ids:
            return
        self.web_ids.add(identity)
        if len(self.web_ids) > 12:
            raise ProviderUnavailable("research_web_call_limit")
        action = item.get("action") or {}
        if not isinstance(action, dict):
            raise ProviderUnavailable("research_invalid_web_event")
        queries = action.get("queries", [])
        if not isinstance(queries, list):
            raise ProviderUnavailable("research_invalid_web_event")
        for query in [item.get("query"), action.get("query"), *queries]:
            if query is not None:
                if not isinstance(query, str) or len(query) > 2000:
                    raise ProviderUnavailable("research_invalid_web_event")
                if query.strip():
                    self.queries.append(query)
        for source in action.get("sources", []) or []:
            if isinstance(source, dict) and isinstance(source.get("url"), str):
                self.urls.append(source["url"])
        if isinstance(action.get("url"), str):
            self.urls.append(action["url"])

    def accept(self, event):
        if not isinstance(event, dict):
            raise ProviderUnavailable("research_invalid_event_stream")
        kind = event.get("type")
        if kind not in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                        "item.started", "item.updated", "item.completed"}:
            raise ProviderUnavailable("research_invalid_event_stream")
        if kind in ("error", "turn.failed"):
            raise ProviderUnavailable("research_model_failed")
        if kind.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict):
                raise ProviderUnavailable("research_invalid_event_stream")
            item_type = item.get("type")
            if item_type == "error":
                notice = item.get("message", "")
                if isinstance(notice, str) and notice.startswith((
                    "Code Mode is unavailable because code-mode host is disabled.",
                    "Exceeded skills context budget. All skill descriptions were removed")):
                    return
                raise ProviderUnavailable("research_model_failed")
            if item_type not in ("reasoning", "agent_message", "web_search", "web_search_call"):
                raise ProviderUnavailable("research_forbidden_tool")
            if kind == "item.completed" and item_type in ("web_search", "web_search_call"):
                if item.get("status") not in (None, "completed"):
                    raise ProviderUnavailable("research_web_search_failed")
                self.web(item)
            if kind == "item.completed" and item_type == "agent_message":
                self.final = item.get("text")
        if kind == "turn.completed":
            self.completed = True
            usage = event.get("usage") or {}
            if not isinstance(usage, dict):
                raise ProviderUnavailable("research_invalid_usage")
            self.usage = {key: value for key, value in usage.items() if key in
                          ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_output_tokens")}
            if any(type(value) is not int or value < 0 for value in self.usage.values()):
                raise ProviderUnavailable("research_invalid_usage")

    def evidence(self):
        return {"search_count": len(self.web_ids), "search_queries": list(dict.fromkeys(self.queries)),
                "consulted_urls": list(dict.fromkeys(self.urls))}


class CodexResearchProvider(CodexSubscriptionProvider):
    def __init__(self, executable=None, timeout_seconds=None):
        super().__init__(executable, timeout_seconds or os.getenv("DONGHAENG_ANALYSIS_RESEARCH_TIMEOUT_SECONDS", "120"))

    def _argv(self, directory):
        return [arg.replace('web_search="disabled"', 'web_search="live"') for arg in super()._argv(directory)]

    def _run_research(self, argv, prompt):
        collector = ResearchEvents()
        with tempfile.TemporaryFile() as input_file:
            input_file.write(prompt.encode())
            input_file.seek(0)
            try:
                process = subprocess.Popen(argv, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=self._environment(), start_new_session=True)
            except OSError:
                raise ProviderUnavailable("research_process_unavailable") from None
            started, retained, pending = time.monotonic(), 0, b""
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                    while selector.get_map() or process.poll() is None:
                        if time.monotonic() - started > self.timeout_seconds:
                            raise ProviderUnavailable("research_timeout")
                        for key, _ in selector.select(timeout=0.1):
                            block = os.read(key.fileobj.fileno(), 65536)
                            if not block:
                                selector.unregister(key.fileobj)
                                continue
                            retained += len(block)
                            if retained > 2_000_000:
                                raise ProviderUnavailable("research_output_too_large")
                            if key.data == "stderr":
                                continue  # Diagnostics can contain private host/auth context.
                            pending += block
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                collector.accept(_json(line))
                    if pending.strip():
                        raise ProviderUnavailable("research_invalid_event_stream")
                    process.wait(timeout=5)
                    if process.returncode != 0 or not collector.completed or not isinstance(collector.final, str):
                        raise ProviderUnavailable("research_incomplete_turn")
                    return collector
            finally:
                self._terminate(process)
                process.stdout.close()
                process.stderr.close()

    def research(self, payload):
        readiness = self.readiness()
        if not readiness["ready"]:
            raise ProviderUnavailable(readiness["error_code"])
        started = time.monotonic()
        with _subscription_slot(), tempfile.TemporaryDirectory(prefix="hermes-research-") as directory_name:
            directory = Path(directory_name)
            (directory / "decision.schema.json").write_text(json.dumps(ResearchDraft.model_json_schema()), encoding="utf-8")
            (directory / "instructions.md").write_text(RESEARCH_INSTRUCTIONS, encoding="utf-8")
            result = self._run_research(self._argv(directory), json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return {"result": _json(result.final), "web_evidence": result.evidence(),
                "model": {"provider": "codex_subscription", "model": self.model,
                          "cli_version": readiness["cli_version"], "billing_mode": "subscription",
                          "prompt_version": RESEARCH_PROMPT_VERSION, "usage": result.usage,
                          "duration_ms": round((time.monotonic() - started) * 1000), "actual_model_call": True}}


class ResponsesResearchProvider:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("DONGHAENG_MODEL")

    def research(self, payload):
        if not self.api_key or not self.model:
            raise ProviderUnavailable("research_provider_not_configured")
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False) as client:
                response = client.post("https://api.openai.com/v1/responses", headers={"Authorization": "Bearer " + self.api_key},
                    json={"model": self.model, "instructions": RESEARCH_INSTRUCTIONS,
                          "input": json.dumps(payload, ensure_ascii=False, allow_nan=False), "store": False,
                          "max_output_tokens": 6000, "max_tool_calls": 12,
                          "tools": [{"type": "web_search", "filters": {"allowed_domains": ["go.kr", "kosis.kr"]}}],
                          "include": ["web_search_call.action.sources"],
                          "text": {"format": {"type": "json_schema", "name": "regional_context_research",
                                               "schema": ResearchDraft.model_json_schema(), "strict": True}}})
        except httpx.TimeoutException:
            raise ProviderUnavailable("research_timeout") from None
        except httpx.HTTPError:
            raise ProviderUnavailable("research_network_error") from None
        if response.status_code != 200:
            raise ProviderUnavailable("research_model_http_error")
        data = _json(response.content)
        if not isinstance(data, dict) or data.get("status") != "completed":
            raise ProviderUnavailable("research_incomplete_turn")
        collector, texts = ResearchEvents(), []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                raise ProviderUnavailable("research_invalid_event_stream")
            kind = item.get("type")
            if kind == "web_search_call":
                if item.get("status") != "completed":
                    raise ProviderUnavailable("research_web_search_failed")
                collector.web(item)
            elif kind == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        texts.append(content.get("text", ""))
                        for annotation in content.get("annotations", []):
                            if annotation.get("type") == "url_citation" and isinstance(annotation.get("url"), str):
                                collector.urls.append(annotation["url"])
                    elif content.get("type") == "refusal":
                        raise ProviderUnavailable("research_model_refused")
            elif kind != "reasoning":
                raise ProviderUnavailable("research_forbidden_tool")
        return {"result": _json("".join(texts)), "web_evidence": collector.evidence(),
                "model": {"provider": "openai_responses", "model": self.model, "billing_mode": "api",
                          "prompt_version": RESEARCH_PROMPT_VERSION, "response_id": data.get("id"),
                          "duration_ms": round((time.monotonic() - started) * 1000), "actual_model_call": True}}


def create_research_provider():
    name = os.getenv("DONGHAENG_PROVIDER", "codex_subscription")
    if name == "codex_subscription":
        return CodexResearchProvider()
    if name == "openai_responses":
        return ResponsesResearchProvider()
    raise ProviderUnavailable("research_provider_not_configured")
