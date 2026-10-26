"""The launcher only terminates process groups it can still identify."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

SPEC = importlib.util.spec_from_file_location("hermes_dev", Path(__file__).resolve().parents[3] / "scripts/dev.py")
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


@pytest.fixture
def child():
    process = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_DFL); print('ready',flush=True); time.sleep(30)"],
        start_new_session=True, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready"
    yield process
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=3)
    process.stdout.close()


def test_stop_refuses_a_reused_or_unrecognized_pid(child):
    record = {"pid": child.pid, "identity": "a different process start time"}
    assert dev.stop_process(record) is False
    assert child.poll() is None


def test_stop_terminates_the_owned_process_group(child):
    record = {"pid": child.pid, "identity": dev.process_identity(child.pid)}
    assert record["identity"]
    result = dev.stop_process(record)
    assert result is True, {"record": record, "after": dev.process_identity(child.pid),
                            "poll": child.poll(), "group": subprocess.run(["ps", "-axo", "pid,pgid,stat,command"], capture_output=True, text=True).stdout[-500:]}
    child.wait(timeout=3)
    with pytest.raises(ProcessLookupError):
        os.killpg(child.pid, 0)


def test_stop_keeps_unowned_records_instead_of_losing_them(tmp_path, child):
    path = tmp_path / "local-services.json"
    path.write_text(json.dumps({"api": {"pid": child.pid, "identity": "wrong"}}))
    assert dev.stop_services(path) is False
    assert json.loads(path.read_text())["api"]["pid"] == child.pid
    assert child.poll() is None


def test_occupied_port_is_detected_without_stopping_its_owner():
    import socket
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert dev.port_open(listener.getsockname()[1]) is True


def test_identity_survives_interpreter_reexec():
    process = subprocess.Popen([sys.executable, "-c",
        "import os,time; time.sleep(0.5); os.execv('/bin/sleep',['sleep','30'])"], start_new_session=True)
    try:
        before = dev.process_identity(process.pid)
        time.sleep(0.7)
        assert dev.process_identity(process.pid) == before
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_stop_does_not_lose_a_process_that_ignores_sigterm(tmp_path):
    process = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        path = tmp_path / "local-services.json"
        path.write_text(json.dumps({"api": {"pid": process.pid, "identity": dev.process_identity(process.pid)}}))
        # Bounded graceful shutdown must preserve ownership if the process stays alive.
        assert dev.stop_services(path) is False
        assert path.exists() and process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=3)
        process.stdout.close()


@pytest.mark.parametrize("url", ["", "   "])
def test_preflight_missing_remote_never_initializes_demo(monkeypatch, url):
    import dotenv
    from types import SimpleNamespace
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("WELFARE_DATABASE_URL", url)
    monkeypatch.setitem(sys.modules, "run_api", SimpleNamespace(configure=lambda: pytest.fail("must not initialize demo")))
    assert dev.preflight()["welfare"] == {"ready": False, "code": "remote_welfare_not_configured"}


def test_start_with_failed_preflight_never_spawns_or_signals(monkeypatch):
    monkeypatch.setattr(dev, "preflight", lambda: {"model": {"ready": False}})
    monkeypatch.setattr(dev.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    monkeypatch.setattr(dev.os, "killpg", lambda *a, **k: pytest.fail("must not signal"))
    assert dev.start() == 1


def test_start_with_occupied_port_never_spawns_or_signals(monkeypatch):
    monkeypatch.setattr(dev, "preflight", lambda: {"model": {"ready": True}})
    monkeypatch.setattr(dev, "read_state", lambda: {})
    monkeypatch.setattr(dev, "port_open", lambda port: True)
    monkeypatch.setattr(dev.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    monkeypatch.setattr(dev.os, "killpg", lambda *a, **k: pytest.fail("must not signal"))
    assert dev.start() == 1


def test_status_is_not_ready_when_configured_services_are_stopped(monkeypatch):
    monkeypatch.setattr(dev, "preflight", lambda: {"model": {"ready": True}})
    monkeypatch.setattr(dev, "read_state", lambda: {})
    monkeypatch.setattr(dev, "get_json", lambda url: None)
    monkeypatch.setattr(dev, "port_open", lambda port: False)
    result = dev.status()
    assert result["ready"] is False
    assert result["checks"]["model"]["ready"] is True
