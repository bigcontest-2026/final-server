"""Prepare public aggregate inputs before either model transport sees them.

This is deterministic minimization and pattern masking, not a classifier that
can establish whether arbitrary prose is public. Callers still supply only the
approved aggregate evidence. Unlike telemetry sanitization, limits reject the
whole input: evidence, numbers and identifiers are never silently truncated.
"""
from __future__ import annotations

import json
import math
import re

REDACTED = "[REDACTED]"
MAX_INPUT_BYTES = 180_000
MAX_DEPTH = 24
MAX_NODES = 20_000

_SENSITIVE_KEY = re.compile(
    r"(^|_)(authorization|api_key|apikey|access_token|refresh_token|id_token|"
    r"password|passwd|secret|credential|credentials|cookie|private_key|client_secret)(_|$)")
_PRIVATE_KEYS = {"personal_data", "private_data", "personal_raw_text", "private_raw_text",
                 "raw_personal_text", "personal_note", "private_note", "raw_user_input", "user_raw_text"}
_IDENTITY_KEYS = {"id", "evidence_id", "evidence_ids", "source_id", "record_id", "candidate_id", "call_id",
                  "case_id", "execution_id", "run_id", "version", "version_hash", "source_version", "model"}
_PATTERNS = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer " + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=:-]{6,}", re.I), REDACTED),
    (re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), REDACTED),
    (re.compile(r"(?<!\d)(?:\+82[- ]?|0)1[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"), REDACTED),
    (re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[- ]?[1-8]\d{6}(?!\d)"), REDACTED),
)


class ModelInputRejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _normalize_key(key):
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z])([A-Z])", r"\1_\2", key).lower()).strip("_")


def _mask(text):
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _identity_key(key):
    return key in _IDENTITY_KEYS or key.endswith(("_version", "_sha256", "_hash"))


def _reject_constant(_):
    raise ModelInputRejected("model_input_non_finite_number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelInputRejected("model_input_duplicate_json_key")
        result[key] = value
    return result


def _validate_shape(value, depth, budget):
    """Check even values that will be omitted; large private blobs are rejected."""
    budget[0] += 1
    if budget[0] > MAX_NODES:
        raise ModelInputRejected("model_input_too_many_nodes")
    if depth > MAX_DEPTH:
        raise ModelInputRejected("model_input_too_deep")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ModelInputRejected("model_input_non_finite_number")
        return
    if isinstance(value, str):
        try:
            encoded_size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ModelInputRejected("model_input_invalid_json") from exc
        if encoded_size > MAX_INPUT_BYTES:
            raise ModelInputRejected("model_input_too_large")
        return
    if isinstance(value, list):
        for item in value:
            _validate_shape(item, depth + 1, budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ModelInputRejected("model_input_non_string_key")
            _validate_shape(key, depth + 1, budget)
            _validate_shape(item, depth + 1, budget)
        return
    raise ModelInputRejected("model_input_unsupported_type")


def _serialized_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ModelInputRejected("model_input_invalid_json") from exc


def prepare_model_input(value):
    """Return an independent JSON-shaped value or raise ModelInputRejected.

    Credentials/private payload keys are omitted. Purpose inside a scope and all
    idempotency keys are omitted because they are not evidence. JSON encoded in
    message strings is parsed recursively, cleaned, and reserialized as a string.
    Evidence IDs/versions remain exact; sensitive content in one is rejected
    instead of silently changing the reference. Scalar numbers remain exact.
    """
    _validate_shape(value, 0, [0])
    if _serialized_size(value) > MAX_INPUT_BYTES:
        raise ModelInputRejected("model_input_too_large")
    budget = [0]

    def walk(item, depth=0, key="", in_scope=False):
        budget[0] += 1
        if budget[0] > MAX_NODES:
            raise ModelInputRejected("model_input_too_many_nodes")
        if depth > MAX_DEPTH:
            raise ModelInputRejected("model_input_too_deep")
        if item is None or type(item) in (bool, int):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ModelInputRejected("model_input_non_finite_number")
            return item
        if isinstance(item, str):
            masked = _mask(item)
            if _identity_key(key):
                if masked != item:
                    raise ModelInputRejected("model_input_sensitive_identifier")
                return item
            stripped = item.strip()
            if stripped.startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(item, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                except json.JSONDecodeError:
                    return masked  # Ordinary quoted source prose is not necessarily JSON.
                except ModelInputRejected:
                    raise
                except RecursionError as exc:
                    raise ModelInputRejected("model_input_too_deep") from exc
                except ValueError as exc:
                    raise ModelInputRejected("model_input_invalid_json") from exc
                if isinstance(decoded, (dict, list, str)):
                    cleaned = walk(decoded, depth + 1, key=key, in_scope=in_scope)
                    return json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
            return masked
        if isinstance(item, list):
            return [walk(child, depth + 1, key=key, in_scope=in_scope) for child in item]
        if isinstance(item, dict):
            result = {}
            for name, child in item.items():
                normalized = _normalize_key(name)
                if (_SENSITIVE_KEY.search(normalized) or normalized in _PRIVATE_KEYS or normalized == "idempotency_key"
                        or normalized == "token" and not isinstance(child, (int, float))
                        or in_scope and normalized == "purpose"):
                    continue
                safe_name = _mask(name)
                if safe_name in result:
                    raise ModelInputRejected("model_input_key_collision")
                result[safe_name] = walk(child, depth + 1, key=normalized, in_scope=in_scope or normalized == "scope")
            return result
        raise ModelInputRejected("model_input_unsupported_type")

    result = walk(value)
    if _serialized_size(result) > MAX_INPUT_BYTES:
        raise ModelInputRejected("model_input_too_large")
    return result
