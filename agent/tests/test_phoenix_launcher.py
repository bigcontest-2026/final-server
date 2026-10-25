import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("serve_phoenix_local", Path(__file__).parents[1] / "evaluation/serve_phoenix_local.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_grpc_adapter_forces_disabled_for_keyword_and_positional_calls():
    class Original:
        def __init__(self, callback, port, disabled=False):
            self.disabled = disabled
            self.port = port
    module = SimpleNamespace(GrpcServer=Original)
    launcher.disable_unused_grpc(module)
    assert module.GrpcServer(None, port=4317, disabled=False).disabled is True
    assert module.GrpcServer(None, 4317, False).disabled is True


def test_grpc_contract_change_fails_closed():
    class Changed:
        def __init__(self, callback):
            pass
    with pytest.raises(RuntimeError, match="phoenix_grpc_disable_contract_changed"):
        launcher.disable_unused_grpc(SimpleNamespace(GrpcServer=Changed))


def test_local_environment_prevents_inherited_external_exports(tmp_path, monkeypatch):
    # Environment mutation is process-local and restored by the fixture.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("PHOENIX_HOST", "0.0.0.0")
    monkeypatch.setenv("PHOENIX_AGENTS_COLLECTOR_ENDPOINT", "https://external.example")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-be-inherited")
    config = launcher.configure_local_environment(tmp_path / "phoenix", 6006)
    assert config["PHOENIX_HOST"] == "127.0.0.1"
    assert config["PHOENIX_SQL_DATABASE_URL"].endswith("/phoenix/phoenix.sqlite")
    assert config["PHOENIX_ALLOW_EXTERNAL_RESOURCES"] == "false"
    assert config["PHOENIX_DISABLE_AGENT_ASSISTANT"] == "true"
    assert config["PHOENIX_ENABLE_MCP_SERVER"] == "false"
    assert config["PHOENIX_ALLOWED_SANDBOX_PROVIDERS"] == "NONE"
    assert "PHOENIX_AGENTS_COLLECTOR_ENDPOINT" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
