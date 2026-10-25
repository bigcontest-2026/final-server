from pathlib import Path

from app.config import Settings, DEFAULT_I5_BUNDLE


def test_i5_default_uses_published_model_data_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("I5_BUNDLE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    assert settings.i5_bundle_path == DEFAULT_I5_BUNDLE
    assert (settings.i5_bundle_path / "manifest.json").is_file()


def test_i5_explicit_override_is_kept_even_if_missing(tmp_path, monkeypatch):
    override = tmp_path / "relocated-i5"
    monkeypatch.setenv("I5_BUNDLE_PATH", str(override))
    assert Settings.from_env().i5_bundle_path == override


def test_agent_resource_overrides_are_independent_of_process_working_directory(tmp_path, monkeypatch):
    agent_root = tmp_path / "relocated-agent"
    monkeypatch.setenv("DONGHAENG_AGENT_ROOT", str(agent_root))
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    assert settings.agent_resource_root == agent_root
    assert settings.support_manifest_path == agent_root / "resources/support/manifest.json"
    assert settings.support_capture_root == agent_root / "evidence/support"
    assert settings.research_path.is_absolute()


def test_explicit_manifest_and_capture_overrides_are_kept(tmp_path, monkeypatch):
    manifest = tmp_path / "custom" / "manifest.json"
    captures = tmp_path / "separate-captures"
    monkeypatch.setenv("OFFICIAL_SUPPORT_MANIFEST", str(manifest))
    monkeypatch.setenv("OFFICIAL_SUPPORT_CAPTURE_ROOT", str(captures))
    settings = Settings.from_env()
    assert settings.support_manifest_path == manifest
    assert settings.support_capture_root == captures
