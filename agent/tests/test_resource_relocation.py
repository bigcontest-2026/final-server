"""Verify source hashes and path boundaries after relocating the agent repository."""
import json
from pathlib import Path
import shutil

import pytest

from donghaeng_agent.tools import InvestigationTools

AGENT_ROOT = Path(__file__).resolve().parents[1]


def relocated_tools(tmp_path):
    root = tmp_path / "relocated-agent"
    shutil.copytree(AGENT_ROOT / "resources/support", root / "resources/support")
    captures = AGENT_ROOT / "evidence/support"
    if captures.exists():
        shutil.copytree(captures, root / "evidence/support")
    return InvestigationTools(tmp_path / "unused-snapshot.json", root / "resources/support/manifest.json")


def test_relocated_official_catalog_hash_and_search(tmp_path):
    tools = relocated_tools(tmp_path)
    result = tools.catalog_search()
    assert result["status"] == "ok"
    assert result["actual_rows"] == 461
    assert result["detail_verified"] is False
    assert result["eligibility_status"] == "unknown"


def test_relocated_captures_keep_hash_verification(tmp_path):
    tools = relocated_tools(tmp_path)
    sources = tools.manifest()["sources"]
    if not all(tools.source_path(s["capture_path"]).is_file() for s in sources):
        pytest.skip("full captures are local and excluded from Git")
    assert all(tools.details(s["id"])["capture_verified"] for s in sources)
    source = sources[0]
    path = tools.source_path(source["capture_path"])
    path.write_bytes(path.read_bytes() + b"tampered")
    assert tools.details(source["id"])["error"]["code"] == "capture_hash_mismatch"


def test_paths_cannot_escape_configured_capture_roots(tmp_path):
    tools = relocated_tools(tmp_path)
    outside = tmp_path / "unrelated.txt"
    outside.write_text("unrelated evidence", encoding="utf-8")
    with pytest.raises(ValueError):
        tools.source_path(str(outside))
    with pytest.raises(ValueError):
        tools.source_path("../unrelated.txt")


def test_symlink_cannot_escape_configured_capture_roots(tmp_path):
    tools = relocated_tools(tmp_path)
    outside = tmp_path / "unrelated.txt"
    outside.write_text("unrelated evidence", encoding="utf-8")
    link = tools.support_path.parent / "escaping-link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if getattr(exc,"winerror",None)==1314:
            pytest.skip("Windows symlink privilege unavailable; path traversal tested separately")
        raise
    with pytest.raises(ValueError):
        tools.source_path(str(link))


def test_explicit_capture_root_is_independent_of_module_location(tmp_path):
    root = tmp_path / "resources"
    root.mkdir()
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"sources": []}), encoding="utf-8")
    capture_root = tmp_path / "private-captures"
    capture_root.mkdir()
    capture = capture_root / "official.txt"
    capture.write_text("official evidence", encoding="utf-8")
    tools = InvestigationTools(tmp_path / "unused.json", manifest,
                               resource_root=tmp_path, capture_root=capture_root)
    assert tools.source_path("private-captures/official.txt") == capture.resolve()
