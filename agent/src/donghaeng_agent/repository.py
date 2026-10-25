"""SQLite business records and durable jobs; graph checkpoints live separately."""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .observability import trace_step


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class RepositoryError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
 request TEXT NOT NULL, status TEXT NOT NULL, measurement_status TEXT NOT NULL DEFAULT 'not_checked',
 support_status TEXT NOT NULL DEFAULT 'not_searched', current_brief_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS executions(execution_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases,
 strategy TEXT NOT NULL, status TEXT NOT NULL, graph_thread_id TEXT UNIQUE NOT NULL,
 versions TEXT NOT NULL, metrics TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution ON executions(case_id)
 WHERE status IN ('queued','running','interrupted');
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions,
 action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL REFERENCES cases,
 execution_id TEXT, type TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS tool_calls(call_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions,
 step_key TEXT NOT NULL, tool_name TEXT NOT NULL, request_hash TEXT NOT NULL, input TEXT NOT NULL, output TEXT NOT NULL,
 duration_ms REAL NOT NULL, at TEXT NOT NULL, UNIQUE(execution_id,step_key));
CREATE TABLE IF NOT EXISTS source_versions(source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 data TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY(source_id,content_hash));
CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases,
 execution_id TEXT NOT NULL REFERENCES executions, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS briefs(brief_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases,
 execution_id TEXT NOT NULL UNIQUE REFERENCES executions, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(review_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases,
 brief_id TEXT NOT NULL REFERENCES briefs, data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS followups(followup_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases,
 data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 response TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(scope,idempotency_key));
"""


class InvestigationRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # LangGraph propagates context into its task/checkpoint executor threads.
        self._job_context = ContextVar("investigation_job_" + uuid.uuid4().hex, default=None)
        with self.connection() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO schema_versions VALUES(1,?)", (now(),))
            db.commit()
            # Additive migration also supports databases created before leases had owners.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for column in ("lease_token", "lease_owner"):
                if column not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS claimable_jobs ON jobs(status,lease_until)")
            db.execute("INSERT OR IGNORE INTO schema_versions VALUES(2,?)", (now(),))

    @contextmanager
    def connection(self, *, write=False, check_job=True):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                context = self._job_context.get() if check_job and write else None
                if context:
                    self._assert_context(db, context)
                yield db
                if context:
                    self._assert_context(db, context)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def _assert_lease(self, db, job):
        row = db.execute("SELECT status,lease_until,lease_token,lease_owner,execution_id FROM jobs WHERE job_id=?",
                         (job["job_id"],)).fetchone()
        if (not row or not job.get("lease_token") or row["status"] != "running"
                or row["execution_id"] != job["execution_id"]
                or row["lease_token"] != job["lease_token"] or row["lease_owner"] != job.get("lease_owner")
                or row["lease_until"] is None or row["lease_until"] <= time.time()):
            raise RepositoryError("job_lease_lost", "작업 소유권이 만료되거나 다른 작업자로 변경되었습니다.")

    def _assert_context(self, db, context):
        job, cancelled, lease_lost, deadline = context
        if lease_lost is not None and lease_lost.is_set():
            raise RepositoryError("job_lease_lost", "작업 소유권을 확인할 수 없습니다.")
        if cancelled is not None and cancelled.is_set():
            raise RepositoryError("worker_shutdown", "작업자가 종료되어 체크포인트에서 재개해야 합니다.")
        if deadline is not None and time.monotonic() >= deadline:
            raise RepositoryError("agent_wall_time_exhausted", "작업 전체 실행 시간 제한을 초과했습니다.")
        self._assert_lease(db, job)

    @contextmanager
    def job_scope(self, job, cancelled=None, lease_lost=None, deadline=None):
        token = self._job_context.set((job, cancelled, lease_lost, deadline))
        try:
            self.check_active_job()
            yield
        finally:
            self._job_context.reset(token)

    def check_active_job(self):
        """Cooperative cancellation hook for model subprocess/HTTP polling loops."""
        context = self._job_context.get()
        if context:
            with self.connection() as db:
                self._assert_context(db, context)

    @contextmanager
    def checkpoint_guard(self):
        """Hold the ownership transaction while committing a separate checkpoint DB.

        This fences checkpoint writers as well as business writes. A claimant cannot
        take ownership between the token check and checkpoint commit. SQLite remains
        a same-host deployment; this is not a distributed PostgreSQL implementation.
        """
        with self.connection(write=True):
            yield

    def _case(self, db, case_id):
        row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise RepositoryError("case_not_found", "조사 사건이 없습니다.", 404)
        return row

    def _version(self, case, expected):
        if case["version"] != expected:
            raise RepositoryError("version_conflict", "다른 변경이 저장되었습니다. 최신 사건을 다시 확인하세요.")

    def _replay(self, db, scope, request):
        row = db.execute("SELECT * FROM commands WHERE scope=? AND idempotency_key=?", (scope, request["idempotency_key"])).fetchone()
        if row:
            if row["request_hash"] != digest(request):
                raise RepositoryError("idempotency_conflict", "같은 요청 키에 다른 입력이 사용되었습니다.")
            return json.loads(row["response"])

    def _command(self, db, scope, request, response):
        db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (scope, request["idempotency_key"], digest(request), encoded(response), now()))

    def replay_command(self, scope, request):
        with self.connection() as db:
            return self._replay(db, scope, request)

    def _event(self, db, case_id, execution_id, kind, payload, key):
        db.execute("INSERT OR IGNORE INTO events(case_id,execution_id,type,payload,at,event_key) VALUES(?,?,?,?,?,?)",
                   (case_id, execution_id, kind, encoded(payload), now(), key))

    def event(self, case_id, execution_id, kind, payload, key):
        with self.connection(write=True) as db:
            self._event(db, case_id, execution_id, kind, payload, key)

    def create(self, request):
        with self.connection(write=True) as db:
            replay = self._replay(db, "create", request)
            if replay:
                return replay
            case_id, at = "case_" + uuid.uuid4().hex, now()
            db.execute("INSERT INTO cases(case_id,version,request,status,created_at,updated_at) VALUES(?,1,?,'draft',?,?)", (case_id, encoded(request), at, at))
            self._event(db, case_id, None, "case_created", {"scope": request}, case_id + ":created")
            response = {"case_id": case_id, "case_version": 1, "status": "draft"}
            self._command(db, "create", request, response)
            return response

    def enqueue(self, case_id, request, versions):
        with self.connection(write=True) as db:
            replay = self._replay(db, case_id + ":run", request)
            if replay:
                return replay
            case = self._case(db, case_id)
            self._version(case, request["expected_case_version"])
            if db.execute("SELECT 1 FROM executions WHERE case_id=? AND status IN ('queued','running','interrupted')", (case_id,)).fetchone():
                raise RepositoryError("active_execution", "진행 중이거나 검토를 기다리는 실행이 있습니다.")
            execution_id, at = "exec_" + uuid.uuid4().hex, now()
            db.execute("INSERT INTO executions(execution_id,case_id,strategy,status,graph_thread_id,versions,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?,?)",
                       (execution_id, case_id, request["strategy"], execution_id, encoded(versions), at, at))
            db.execute("INSERT INTO jobs(job_id,execution_id,action,payload,status) VALUES(?,?,'run','{}','queued')", (uuid.uuid4().hex, execution_id))
            db.execute("UPDATE cases SET version=version+1,status='investigating',updated_at=? WHERE case_id=?", (at, case_id))
            self._event(db, case_id, execution_id, "execution_queued", {"strategy": request["strategy"], "versions": versions}, execution_id + ":queued")
            response = {"case_id": case_id, "case_version": case["version"] + 1, "execution_id": execution_id, "status": "queued"}
            self._command(db, case_id + ":run", request, response)
            return response

    def review(self, case_id, request):
        with self.connection(write=True) as db:
            replay = self._replay(db, case_id + ":review", request)
            if replay:
                return replay
            case = self._case(db, case_id)
            self._version(case, request["expected_case_version"])
            if case["support_status"] == "stale" and request["decision"] == "accept":
                raise RepositoryError("stale_support", "공식 지원 근거가 변경되거나 접근할 수 없습니다. 이전 브리프 수락 전에 재검토가 필요합니다.")
            brief = db.execute("SELECT * FROM briefs WHERE brief_id=? AND case_id=?", (request["brief_id"], case_id)).fetchone()
            if not brief or brief["version"] != request["brief_version"] or case["current_brief_id"] != brief["brief_id"]:
                raise RepositoryError("brief_version_conflict", "현재 조사 브리프 버전과 검토 요청이 다릅니다.")
            execution = db.execute("SELECT * FROM executions WHERE execution_id=?", (brief["execution_id"],)).fetchone()
            if execution["status"] != "interrupted":
                raise RepositoryError("not_awaiting_review", "검토 대기 실행이 아닙니다.")
            review_id = "review_" + uuid.uuid4().hex
            data = {**request, "review_id": review_id, "at": now(), "outcome_scope": "brief_review_only"}
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?)", (review_id, case_id, brief["brief_id"], encoded(data), data["at"]))
            db.execute("INSERT INTO jobs(job_id,execution_id,action,payload,status) VALUES(?,?,'resume',?,'queued')", (uuid.uuid4().hex, execution["execution_id"], encoded(data)))
            db.execute("UPDATE executions SET status='queued',updated_at=? WHERE execution_id=?", (now(), execution["execution_id"]))
            db.execute("UPDATE cases SET version=version+1,status='investigating',updated_at=? WHERE case_id=?", (now(), case_id))
            self._event(db, case_id, execution["execution_id"], "review_recorded", data, review_id)
            response = {"case_id": case_id, "case_version": case["version"] + 1, "review_id": review_id, "execution_id": execution["execution_id"], "status": "queued"}
            self._command(db, case_id + ":review", request, response)
            return response

    def claim(self, lease_seconds=120, owner=None):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.connection(write=True, check_job=False) as db:
            row = db.execute("""SELECT j.* FROM jobs j JOIN executions e ON e.execution_id=j.execution_id
                WHERE e.status IN ('queued','running') AND
                (j.status='queued' OR (j.status='running' AND (j.lease_until IS NULL OR j.lease_until<=?)))
                ORDER BY j.rowid LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            lease_token = uuid.uuid4().hex
            lease_owner = owner or uuid.uuid4().hex
            db.execute("UPDATE jobs SET status='running',lease_until=?,attempts=attempts+1,lease_token=?,lease_owner=? WHERE job_id=?",
                       (time.time() + lease_seconds, lease_token, lease_owner, row["job_id"]))
            db.execute("UPDATE executions SET status='running',updated_at=? WHERE execution_id=?", (now(), row["execution_id"]))
            claimed = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            return {**dict(claimed), "payload": json.loads(claimed["payload"])}

    def heartbeat(self, job, lease_seconds=120):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.connection(write=True, check_job=False) as db:
            try:
                self._assert_lease(db, job)
            except RepositoryError:
                return False
            db.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (time.time() + lease_seconds, job["job_id"]))
            return True

    def release_job(self, job, reason="worker_shutdown"):
        """Requeue after cooperative shutdown; never release another worker's lease."""
        with self.connection(write=True, check_job=False) as db:
            try:
                self._assert_lease(db, job)
            except RepositoryError:
                return False
            db.execute("UPDATE jobs SET status='queued',lease_until=NULL,lease_token=NULL,lease_owner=NULL WHERE job_id=?", (job["job_id"],))
            db.execute("UPDATE executions SET status='queued',updated_at=? WHERE execution_id=?", (now(), job["execution_id"]))
            case_id = db.execute("SELECT case_id FROM executions WHERE execution_id=?", (job["execution_id"],)).fetchone()[0]
            self._event(db, case_id, job["execution_id"], "execution_requeued", {"reason": reason, "attempt": job["attempts"]},
                        job["job_id"] + ":released:" + job["lease_token"])
            return True

    def retry(self, case_id, request):
        with self.connection(write=True) as db:
            replay = self._replay(db, case_id + ":retry", request)
            if replay:
                return replay
            case = self._case(db, case_id)
            self._version(case, request["expected_case_version"])
            row = db.execute("SELECT * FROM executions WHERE execution_id=? AND case_id=?", (request["execution_id"], case_id)).fetchone()
            if not row or row["status"] != "failed":
                raise RepositoryError("not_failed_execution", "실패한 실행만 같은 체크포인트로 재개할 수 있습니다.")
            if db.execute("SELECT 1 FROM executions WHERE case_id=? AND status IN ('queued','running','interrupted')", (case_id,)).fetchone():
                raise RepositoryError("active_execution", "이 사건에 진행 중인 실행이 있습니다.")
            error = json.loads(row["error"] or "{}")
            if error.get("code") in ("source_version_changed_before_execution", "tool_cache_conflict", "model_configuration_missing", "model_configuration_changed_before_execution"):
                raise RepositoryError("new_execution_required", "원자료 또는 모델 설정 계약이 달라져 새 실행이 필요합니다.")
            previous = db.execute("SELECT action,payload FROM jobs WHERE execution_id=? ORDER BY rowid DESC LIMIT 1", (row["execution_id"],)).fetchone()
            # A failed review-resume must replay that exact durable command if the
            # checkpoint never consumed it. Do not invent another human decision.
            action = "resume" if previous and previous["action"] == "resume" else "retry"
            payload = previous["payload"] if action == "resume" else "{}"
            db.execute("INSERT INTO jobs(job_id,execution_id,action,payload,status) VALUES(?,?,?,?, 'queued')", (uuid.uuid4().hex, row["execution_id"], action, payload))
            db.execute("UPDATE executions SET status='queued',updated_at=? WHERE execution_id=?", (now(), row["execution_id"]))
            db.execute("UPDATE cases SET version=version+1,status='investigating',updated_at=? WHERE case_id=?", (now(), case_id))
            response = {"case_id": case_id, "case_version": case["version"] + 1, "execution_id": row["execution_id"], "status": "queued"}
            self._event(db, case_id, row["execution_id"], "execution_retry_queued", {"previous_error": error}, row["execution_id"] + ":retry:" + request["idempotency_key"])
            self._command(db, case_id + ":retry", request, response)
            return response

    def execution_metrics(self, execution_id):
        with self.connection() as db:
            rows = db.execute("SELECT output,duration_ms FROM tool_calls WHERE execution_id=? ORDER BY rowid", (execution_id,)).fetchall()
        model = []
        token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        usage_reports = {key: 0 for key in token_usage}
        for row in rows:
            output = json.loads(row["output"])
            metrics = output.get("metrics") if isinstance(output, dict) else None
            if not isinstance(metrics, dict) or not (metrics.get("model_call_succeeded") or metrics.get("actual_api_success")):
                continue
            model.append(metrics)
            usage = metrics.get("usage") or {}
            if isinstance(usage, dict):
                for key in token_usage:
                    value = usage.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                        token_usage[key] += value
                        usage_reports[key] += 1
        if model:
            token_usage = {key: value if usage_reports[key] else None for key, value in token_usage.items()}
        billing_modes = sorted({m.get("billing_mode", "api_tokens" if m.get("actual_api_success") else "unknown") for m in model})
        subscription_only = bool(billing_modes) and all(mode in ("subscription", "chatgpt_subscription", "subscription_quota") for mode in billing_modes)
        return {"actual_model_calls": len(model), "model_metrics": model, "tool_calls": len(rows),
                "tool_duration_ms": sum(r["duration_ms"] for r in rows), "cost_usd": None,
                "token_usage": token_usage, "token_usage_complete": all(count == len(model) for count in usage_reports.values()),
                "billing_modes": billing_modes,
                "cost_status": "not_applicable; subscription_quota_not_api_dollars" if subscription_only else "not_measured; account_pricing_not_configured"}

    def execution(self, execution_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not row:
                raise RepositoryError("execution_not_found", "실행 기록이 없습니다.", 404)
            result = dict(row)
            for field in ("versions", "metrics", "error"):
                result[field] = json.loads(result[field]) if result[field] else None
            return result

    def finish_job(self, job, status, error=None, metrics=None):
        if status not in ("interrupted", "succeeded", "failed"):
            raise ValueError("Unsupported execution completion status")
        with self.connection(write=True, check_job=False) as db:
            try:
                self._assert_lease(db, job)
            except RepositoryError:
                return False
            execution = db.execute("SELECT * FROM executions WHERE execution_id=?", (job["execution_id"],)).fetchone()
            case_id = execution["case_id"]
            db.execute("UPDATE jobs SET status='done',lease_until=NULL,lease_token=NULL,lease_owner=NULL WHERE job_id=?", (job["job_id"],))
            db.execute("UPDATE executions SET status=?,error=?,metrics=?,updated_at=? WHERE execution_id=?", (status, encoded(error) if error else None, encoded(metrics or {}), now(), execution["execution_id"]))
            case_status = "awaiting_review" if status == "interrupted" else "held" if status == "failed" else None
            if case_status:
                db.execute("UPDATE cases SET version=version+1,status=?,updated_at=? WHERE case_id=?", (case_status, now(), case_id))
            self._event(db, case_id, execution["execution_id"], "execution_" + status, {"error": error, "metrics": metrics or {}}, job["job_id"] + ":finished")
            return True

    def put_brief(self, case_id, execution_id, brief):
        with self.connection(write=True) as db:
            existing = db.execute("SELECT data FROM briefs WHERE execution_id=?", (execution_id,)).fetchone()
            if existing:
                return json.loads(existing["data"])
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM briefs WHERE case_id=?", (case_id,)).fetchone()[0]
            brief = {**brief, "brief_id": "brief_" + execution_id[5:], "brief_version": version, "version": version, "created_at": now()}
            db.execute("INSERT INTO briefs VALUES(?,?,?,?,?)", (brief["brief_id"], case_id, execution_id, version, encoded(brief)))
            db.execute("UPDATE cases SET version=version+1,current_brief_id=?,measurement_status=?,support_status=?,updated_at=? WHERE case_id=?",
                       (brief["brief_id"], brief["measurement_status"], brief["support_status"], now(), case_id))
            self._event(db, case_id, execution_id, "brief_saved", {"brief_id": brief["brief_id"], "brief_version": version}, execution_id + ":brief")
            return brief

    def apply_review(self, case_id, execution_id, review):
        with self.connection(write=True) as db:
            key = execution_id + ":applied:" + review["review_id"]
            if not db.execute("SELECT 1 FROM events WHERE event_key=?", (key,)).fetchone():
                status = {"accept": "reviewed", "reject": "held", "request_info": "awaiting_info"}[review["decision"]]
                db.execute("UPDATE cases SET version=version+1,status=?,updated_at=? WHERE case_id=?", (status, now(), case_id))
                self._event(db, case_id, execution_id, "review_applied", {"decision": review["decision"], "outcome_scope": "brief_review_only"}, key)

    def tool_result(self, execution_id, step_key, tool_name, arguments, fn):
        with trace_step(tool_name, kind="CHAIN" if tool_name.startswith("model_") else "TOOL",
                        metadata={"execution_id": execution_id, "step_key": step_key, "cache_hit": False}) as trace:
            self.check_active_job()
            with self.connection() as db:
                row = db.execute("SELECT * FROM tool_calls WHERE execution_id=? AND step_key=?", (execution_id, step_key)).fetchone()
                if row:
                    if row["request_hash"] != digest(arguments) or row["tool_name"] != tool_name:
                        raise RepositoryError("tool_cache_conflict", "도구 단계의 입력이 이전 실행과 다릅니다.")
                    trace.set_attributes({"cache_hit": True})
                    return json.loads(row["output"])
            started = time.monotonic()
            result = fn()
            elapsed = (time.monotonic() - started) * 1000
            with self.connection(write=True) as db:
                # Recheck under the write transaction: a concurrent replay cannot return
                # a different value than the result actually committed for this step.
                existing = db.execute("SELECT * FROM tool_calls WHERE execution_id=? AND step_key=?", (execution_id, step_key)).fetchone()
                if existing:
                    if existing["request_hash"] != digest(arguments) or existing["tool_name"] != tool_name:
                        raise RepositoryError("tool_cache_conflict", "도구 단계의 입력이 이전 실행과 다릅니다.")
                    trace.set_attributes({"concurrent_cache_hit": True})
                    return json.loads(existing["output"])
                db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?)", ("tool_" + uuid.uuid4().hex, execution_id, step_key, tool_name, digest(arguments), encoded(arguments), encoded(result), elapsed, now()))
                return result

    def put_evidence(self, case_id, execution_id, evidence):
        with self.connection(write=True) as db:
            for item in evidence:
                db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)", (item["evidence_id"], case_id, execution_id, encoded(item)))
                db.execute("INSERT OR IGNORE INTO source_versions VALUES(?,?,?,?)", (item["source_id"], item["version_hash"], encoded(item), now()))

    def record_recheck(self, case_id, request, changes):
        with self.connection(write=True) as db:
            replay = self._replay(db, case_id + ":recheck", request)
            if replay:
                return replay
            case = self._case(db, case_id)
            self._version(case, request["expected_case_version"])
            key = "recheck_" + uuid.uuid4().hex
            # Even identical bytes create a new retrieval event; they are not new observations.
            changed = any(x["status"] in ("changed", "unavailable") for x in changes)
            measurement_changed = any(x["source_id"] == "published_research_snapshot" and x["status"] != "same" for x in changes)
            db.execute("UPDATE cases SET version=version+1,support_status=?,measurement_status=?,updated_at=? WHERE case_id=?", ("stale" if changed else case["support_status"], "stale" if measurement_changed else case["measurement_status"], now(), case_id))
            self._event(db, case_id, None, "sources_rechecked", {"changes": changes, "requires_new_run": changed, "scope": "local_capture_recheck; no live HTTP fetch"}, key)
            response = {"case_id": case_id, "case_version": case["version"] + 1, "changes": changes, "requires_new_run": changed, "recheck_id": key}
            self._command(db, case_id + ":recheck", request, response)
            return response

    def followup(self, case_id, request):
        with self.connection(write=True) as db:
            replay = self._replay(db, case_id + ":followup", request)
            if replay:
                return replay
            case = self._case(db, case_id)
            self._version(case, request["expected_case_version"])
            for item in request["evidence_ids"]:
                if not db.execute("SELECT 1 FROM evidence WHERE evidence_id=? AND case_id=?", (item, case_id)).fetchone():
                    raise RepositoryError("missing_followup_evidence", "사건에 저장된 후속 근거가 필요합니다.")
            if request["status"] in ("connection_confirmed", "participation_confirmed"):
                raise RepositoryError("field_evidence_required", "현재 저장된 자료는 지역 집계·지원 공고입니다. 실제 연결·참여 확인 자료를 취득하는 후속 계약이 필요합니다.")
            fid = "followup_" + uuid.uuid4().hex
            data = {**request, "followup_id": fid}
            db.execute("INSERT INTO followups VALUES(?,?,?,?)", (fid, case_id, encoded(data), now()))
            db.execute("UPDATE cases SET version=version+1,updated_at=? WHERE case_id=?", (now(), case_id))
            self._event(db, case_id, None, "followup_recorded", data, fid)
            response = {"case_id": case_id, "case_version": case["version"] + 1, "followup_id": fid}
            self._command(db, case_id + ":followup", request, response)
            return response

    def get(self, case_id):
        with self.connection() as db:
            case = dict(self._case(db, case_id))
            case["case_version"] = case.pop("version")
            case["request"] = json.loads(case["request"])
            for name, table in (("briefs", "briefs"), ("reviews", "reviews"), ("followups", "followups")):
                case[name] = [json.loads(x[0]) for x in db.execute(f"SELECT data FROM {table} WHERE case_id=? ORDER BY rowid", (case_id,))]
            # Ownership comes from constrained SQL columns, never from payload
            # fields or an inferred evidence-ID naming convention.
            case["evidence"] = [{**json.loads(row["data"]), "evidence_id": row["evidence_id"],
                "case_id": row["case_id"], "execution_id": row["execution_id"]}
                for row in db.execute("SELECT evidence_id,case_id,execution_id,data FROM evidence WHERE case_id=? ORDER BY rowid", (case_id,))]
            case["events"] = [{**dict(x), "payload": json.loads(x["payload"])} for x in db.execute("SELECT * FROM events WHERE case_id=? ORDER BY event_id", (case_id,))]
            ids = [x[0] for x in db.execute("SELECT execution_id FROM executions WHERE case_id=? ORDER BY rowid", (case_id,))]
        case["executions"] = [self.execution(x) for x in ids]
        for execution in case["executions"]:
            with self.connection() as db:
                execution["tool_calls"] = [{**dict(row), "input": json.loads(row["input"]), "output": json.loads(row["output"])} for row in db.execute("SELECT * FROM tool_calls WHERE execution_id=? ORDER BY rowid", (execution["execution_id"],))]
        case["schema_version"] = "investigation-1.0"
        return case

    def list(self):
        with self.connection() as db:
            ids = [x[0] for x in db.execute("SELECT case_id FROM cases ORDER BY updated_at DESC")]
        return [self.get(x) for x in ids]
