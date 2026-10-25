"""Run required offline agent/service checks and reject unintended test skips."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parent
REQUIRED_SUITES = frozenset({"test_provider", "test_observability", "test_worker_reliability",
    "test_phoenix_feedback", "test_phoenix_readback", "test_investigation_evidence", "test_security", "test_national_i5",
    "test_model_input", "test_observability_lifecycle", "test_security_negative_review",
    "test_provider_adversarial", "test_phoenix_launcher", "test_semantic_review_annotation",
    "test_repository_evidence", "test_backup"})


def verify_report(path):
    cases = ET.parse(path).getroot().findall(".//testcase")
    seen, skipped, errors = set(), [], []
    for case in cases:
        suite = case.get("classname", "").rsplit(".", 1)[-1]
        name = case.get("name", "")
        seen.add(suite)
        skip = case.find("skipped")
        if case.find("failure") is not None or case.find("error") is not None:
            errors.append(f"{suite}::{name}:failed")
        if skip is not None:
            reason = (skip.get("message", "") + " " + (skip.text or "")).lower()
            allowed = (
                suite == "test_investigations" and "official capture integration requires local" in reason
                or suite == "test_resource_relocation" and name == "test_relocated_captures_keep_hash_verification" and "full captures are local" in reason
                or suite == "test_final_model" and name == "test_actual_frozen_I5_roundtrip" and "real frozen v3 export" in reason
                or suite == "ApplicationPeriodPostgresTests" and "set run_welfare_postgres_tests=1" in reason)
            if not allowed:
                errors.append(f"{suite}::{name}:unexpected_skip")
            skipped.append({"suite": suite, "name": name, "allowed_private_resource_skip": allowed})
    errors.extend(f"{suite}:not_collected" for suite in sorted(REQUIRED_SUITES - seen))
    if not cases:
        errors.append("no_tests_collected")
    return {"passed": not errors, "test_count": len(cases), "allowed_skips": skipped, "errors": errors,
            "external_model_calls_allowed": False,
            "execution_environment": "github_actions" if os.getenv("GITHUB_ACTIONS") else "local"}


def main():
    # Missing dependencies must fail instead of letting importorskip hide the agent.
    for module in ("donghaeng_agent", "donghaeng_model", "final_model", "opentelemetry.sdk", "pandas", "jwt"):
        importlib.import_module(module)
    destination = SERVICE / ".artifacts/backend-ci"
    destination.mkdir(parents=True, exist_ok=True)
    report = destination / "junit.xml"
    env = {**os.environ, "PYTHONPATH": str(SERVICE / "backend"), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "DONGHAENG_PROVIDER": "openai_responses", "OPENAI_API_KEY": "", "DONGHAENG_PHOENIX_ENABLED": "false",
           "ACCESS_MODE": "local_development"}
    result = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "agent/tests"),
                             str(SERVICE / "backend/tests"), "-q", f"--junitxml={report}"], cwd=ROOT, env=env)
    if not report.is_file():
        return result.returncode or 1
    summary = verify_report(report)
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return result.returncode or (0 if summary["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
