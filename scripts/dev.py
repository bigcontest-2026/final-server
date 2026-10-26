"""Manage this checkout's loopback API and sibling final-client, without a shell.

Usage: .venv/bin/python scripts/dev.py start|stop|status
Status checks credentials/readiness, never performs model inference.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime/local-services"
STATE = RUNTIME / "local-services.json"
FRONTEND = ROOT.parent / "final-client/frontend"


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return ""
    try:
        # macOS Python and /usr/bin/env can exec a new interpreter under the same PID.
        # Birth time and process group are stable across exec; command strings are not.
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid="],
                                text=True, capture_output=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def owned(record):
    return bool(record.get("identity")) and process_identity(record.get("pid")) == record["identity"]


def stop_process(record):
    pid = record.get("pid")
    if not owned(record):
        if process_identity(pid):
            return False
        # A dead group leader may have surviving children. Retain the record,
        # but do not signal a group whose original leader can no longer be verified.
        if type(pid) is int and pid > 1:
            try:
                os.killpg(pid, 0)
                return False
            except ProcessLookupError:
                pass
            except OSError:
                return False
        return True
    try:
        if os.getpgid(pid) != pid:
            return False
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            # Reap only our direct children; detached services may have another parent.
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False  # Keep ownership records for a surviving or unresponsive group.
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False


def read_state(path=STATE):
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid_process_state")
    return value


def stop_services(path=STATE):
    records = read_state(path)
    remaining = {name: record for name, record in records.items() if not stop_process(record)}
    if remaining:
        path.write_text(json.dumps(remaining, indent=2) + "\n")
        return False
    path.unlink(missing_ok=True)
    return True


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return json.load(response)
    except Exception:
        return None


def preflight():
    checks = {}
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / "service/backend/.env", override=False, interpolate=False)
        checks["settings"] = {"ready": os.getenv("APP_MODE", "final") == "final" and
            os.getenv("ACCESS_MODE", "local_development") == "local_development"}
        if not os.getenv("WELFARE_DATABASE_URL", "").strip():
            checks["welfare"] = {"ready": False, "code": "remote_welfare_not_configured"}
            return checks
        # With a remote URL configure() never creates or selects synthetic data.
        from run_api import configure
        configure()
        from app.config import Settings
        from app.final_model import FinalModel
        from donghaeng_agent.provider import create_provider
        from donghaeng_agent.welfare_postgres import PostgresWelfareDB
        import uvicorn  # noqa: F401
        settings = Settings.from_env()
        checks["model"] = {**create_provider().readiness(), "inference": "not_run_by_status"}
        status = FinalModel(settings).status()
        checks["bundles"] = {"ready": status["status"] == "ok", "sources": status["sources"]}
        mapping = PostgresWelfareDB().support_mapping_status()
        checks["welfare"] = {"ready": mapping.get("mapping_status") == "configured_research",
            "read_only": True, "mapping_status": mapping.get("mapping_status"),
            "rule_version": mapping.get("rule_version"), "total_programs": mapping.get("total_programs")}
        checks["storage"] = {"ready": os.access(settings.investigation_db_path.parent if
            settings.investigation_db_path.parent.exists() else settings.investigation_db_path.parent.parent, os.W_OK),
            "path": str(settings.investigation_db_path.parent)}
        checks["frontend"] = {"ready": (FRONTEND / "node_modules/.bin/vite").exists()}
    except Exception as exc:
        # Driver messages and arbitrary diagnostics may include secrets. Emit only safe codes.
        checks["dependency"] = {"ready": False, "code": getattr(exc, "code", "configuration_or_dependency_unavailable")}
    return checks


def status():
    checks = preflight()
    records = read_state()
    health = get_json("http://127.0.0.1:8011/api/health")
    services = {"api": {"listening": port_open(8011), "health_ok": bool(health and health.get("status") == "ok"),
                        "owned": owned(records.get("api", {}))},
                "client": {"listening": port_open(5173), "owned": owned(records.get("client", {}))}}
    return {"ready": bool(checks) and all(check.get("ready") is True for check in checks.values())
                     and services["api"]["health_ok"] and services["client"]["listening"],
            "checks": checks,
            "services": services,
            "urls": {"client": "http://127.0.0.1:5173", "api": "http://127.0.0.1:8011", "docs": "http://127.0.0.1:8011/docs"}}


def start():
    checks = preflight()
    if not checks or not all(item.get("ready") is True for item in checks.values()):
        print(json.dumps({"started": False, "checks": checks}, ensure_ascii=False, indent=2))
        return 1
    records = read_state()
    if records and all(owned(records.get(name, {})) for name in ("api", "client")):
        print(json.dumps({"started": True, "already_running": True}, ensure_ascii=False))
        return 0
    if port_open(8011) or port_open(5173):
        print(json.dumps({"started": False, "code": "port_in_use", "message": "5173/8011 포트의 기존 서버를 확인해 주세요. 다른 프로세스는 중지하지 않았습니다."}, ensure_ascii=False))
        return 1
    if records and not stop_services():
        print(json.dumps({"started": False, "code": "process_identity_mismatch"}))
        return 1
    RUNTIME.mkdir(parents=True, exist_ok=True)
    records = {}
    try:
        for name, cwd, command in [
            ("api", ROOT, [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/run_api.py")]),
            ("client", FRONTEND, [str(FRONTEND / "node_modules/.bin/vite"), "--host", "127.0.0.1", "--port", "5173", "--strictPort"]),
        ]:
            with (RUNTIME / f"{name}.log").open("ab") as log:
                process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            # Capture the process birth/group identity, which survives interpreter exec.
            for _ in range(20):
                identity = process_identity(process.pid)
                if identity:
                    break
                time.sleep(0.05)
            records[name] = {"pid": process.pid, "identity": identity}
            STATE.write_text(json.dumps(records, indent=2) + "\n")
            STATE.chmod(0o600)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            health = get_json("http://127.0.0.1:8011/api/health")
            if health and health.get("status") == "ok" and port_open(5173) and all(owned(r) for r in records.values()):
                print(json.dumps({"started": True, "client": "http://127.0.0.1:5173", "api": "http://127.0.0.1:8011", "logs": str(RUNTIME)}, ensure_ascii=False))
                return 0
            if not all(owned(r) for r in records.values()):
                break
            time.sleep(0.2)
    except Exception:
        pass
    stop_services()
    print(json.dumps({"started": False, "code": "startup_failed", "logs": str(RUNTIME)}))
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    if args.action == "start":
        return start()
    if args.action == "stop":
        stopped = stop_services()
        print(json.dumps({"stopped": stopped}))
        return 0 if stopped else 1
    result = status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
