"""Pinned local Phoenix launcher: loopback HTTP only, no gRPC listener.

Phoenix 20.23.0 binds gRPC to [::] independently of PHOENIX_HOST. The small
process-local adapter below disables that unused listener without editing the
installed dependency. It is deliberately version-gated and is not a production
deployment entrypoint.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import os
from pathlib import Path
import sys

PINNED_PHOENIX_VERSION = "20.23.0"
AGENT_ROOT = Path(__file__).resolve().parents[1]


def configure_local_environment(working_dir: Path, port: int) -> dict[str, str]:
    if not 1024 <= port <= 65535:
        raise ValueError("phoenix_local_port_must_be_unprivileged")
    working_dir = working_dir.resolve()
    working_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This launcher owns its server environment. Do not inherit collectors,
    # cloud provider keys, assistant/MCP settings or telemetry exporters.
    for key in list(os.environ):
        if key.startswith(("PHOENIX_", "OTEL_", "OPENAI_", "ANTHROPIC_", "ARIZE_")):
            os.environ.pop(key, None)
    config = {
        "PHOENIX_HOST": "127.0.0.1", "PHOENIX_PORT": str(port),
        "PHOENIX_WORKING_DIR": str(working_dir),
        "PHOENIX_SQL_DATABASE_URL": "sqlite:///" + str(working_dir / "phoenix.sqlite"),
        "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false", "PHOENIX_TELEMETRY_ENABLED": "false",
        "PHOENIX_DISABLE_AGENT_ASSISTANT": "true", "PHOENIX_ENABLE_MCP_SERVER": "false",
        "PHOENIX_AGENTS_DISABLE_WEB_ACCESS": "true", "PHOENIX_AGENTS_DISABLE_BASH": "true",
        "PHOENIX_AGENTS_DISABLE_GITHUB": "true", "PHOENIX_ALLOWED_SANDBOX_PROVIDERS": "NONE",
        "PHOENIX_ENABLE_PROMETHEUS": "false", "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS": "30",
    }
    os.environ.update(config)
    return config


def disable_unused_grpc(app_module):
    original = app_module.GrpcServer
    signature = inspect.signature(original.__init__)
    if "disabled" not in signature.parameters:
        raise RuntimeError("phoenix_grpc_disable_contract_changed")

    class HttpOnlyGrpcServer(original):
        def __init__(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs)
            bound.arguments["disabled"] = True
            super().__init__(*bound.args[1:], **bound.kwargs)

    app_module.GrpcServer = HttpOnlyGrpcServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=6006)
    parser.add_argument("--working-dir", type=Path, default=AGENT_ROOT / "runtime/phoenix")
    args = parser.parse_args()
    if importlib.metadata.version("arize-phoenix") != PINNED_PHOENIX_VERSION:
        raise SystemExit("Unsupported Phoenix version: review the HTTP-only adapter before upgrading.")
    configure_local_environment(args.working_dir, args.port)
    import phoenix.server.app as app_module
    disable_unused_grpc(app_module)
    from phoenix.server.main import main as phoenix_main
    sys.argv = ["phoenix", "serve", "--host", "127.0.0.1", "--port", str(args.port), "--no-internet"]
    phoenix_main()


if __name__ == "__main__":
    main()
