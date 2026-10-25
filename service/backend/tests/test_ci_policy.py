import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("backend_ci_policy", Path(__file__).resolve().parents[2] / "scripts/check_backend_ci.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def report(tmp_path, extra=None, missing=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for name in policy.REQUIRED_SUITES - {missing}:
        ET.SubElement(suite, "testcase", classname="tests." + name, name="test_required")
    if extra:
        case = ET.SubElement(suite, "testcase", classname="tests." + extra[0], name=extra[1])
        ET.SubElement(case, "skipped", message=extra[2])
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    return path


def test_only_explicit_private_capture_skips_are_allowed(tmp_path):
    path = report(tmp_path, ("test_investigations", "test_real_source", "official capture integration requires local agent/evidence/support resources"))
    assert policy.verify_report(path)["passed"]
    path = report(tmp_path, ("test_observability", "test_required_exporter", "could not import opentelemetry.sdk"))
    assert not policy.verify_report(path)["passed"]


def test_missing_agent_suite_is_not_a_passing_ci_result(tmp_path):
    summary = policy.verify_report(report(tmp_path, missing="test_provider"))
    assert not summary["passed"]
    assert "test_provider:not_collected" in summary["errors"]
