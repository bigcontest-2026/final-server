"""Local durable worker with renewable, fenced leases and separate checkpoints.

SQLite supports multiple workers on one host. Production multi-host deployment
requires the planned PostgreSQL queue/checkpoint migration, not a shared DB file.
"""
import sqlite3
import threading
import time
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import build_investigation_graph
from .observability import trace_execution
from .provider import ProviderUnavailable
from .repository import RepositoryError


class FencedSqliteSaver(SqliteSaver):
    """Serialize ownership validation with checkpoint writes across both DBs."""

    def __init__(self, connection, repository):
        super().__init__(connection)
        self.repository = repository

    def put(self, config, checkpoint, metadata, new_versions):
        with self.repository.checkpoint_guard():
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        with self.repository.checkpoint_guard():
            return super().put_writes(config, writes, task_id, task_path)


class InvestigationWorker:
    def __init__(self, repository, tools, checkpoint_path, *, lease_seconds=120, heartbeat_seconds=None, execution_timeout_seconds=480, welfare_request=None):
        self.repository, self.tools = repository, tools
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = lease_seconds
        if execution_timeout_seconds <= 0:
            raise ValueError("execution_timeout_seconds must be positive")
        self.execution_timeout_seconds = execution_timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds if heartbeat_seconds is not None else lease_seconds / 3
        if not 0 < self.heartbeat_seconds < lease_seconds:
            raise ValueError("heartbeat_seconds must be between zero and lease_seconds")
        self.worker_id = "worker_" + uuid.uuid4().hex
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False, timeout=15)
        self.checkpoint_connection.execute("PRAGMA journal_mode=WAL")
        self.checkpoint_connection.execute("PRAGMA busy_timeout=15000")
        self.saver = FencedSqliteSaver(self.checkpoint_connection, repository)
        self.graph = build_investigation_graph(repository, tools, self.saver, welfare_request=welfare_request)
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error_code = None
        self._drain_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closed = False

    def start(self):
        with self._lifecycle_lock:
            if self._closed or self.stop_event.is_set():
                raise RuntimeError("A closed worker cannot restart; create a new worker")
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name="investigation-local-worker", daemon=True)
            self.thread.start()

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    if not self.drain_once():
                        self.stop_event.wait(0.1)
                except (sqlite3.Error, RepositoryError) as exc:
                    # A transient local storage error must not silently kill polling.
                    # Raw exception text may contain data and is deliberately omitted.
                    self.last_error_code = getattr(exc, "code", "worker_storage_unavailable")
                    self.stop_event.wait(1)
        finally:
            self._close_connection_if_idle()

    def _heartbeat(self, job, finished, lost):
        while not finished.wait(self.heartbeat_seconds):
            try:
                renewed = self.repository.heartbeat(job, self.lease_seconds)
            except (sqlite3.Error, RepositoryError):
                # Fail closed if ownership cannot be confirmed. The durable job can
                # be reclaimed after expiry; its current attempt cannot commit.
                renewed = False
            if not renewed:
                lost.set()
                return

    def drain_once(self):
        if self.stop_event.is_set() or not self._drain_lock.acquire(blocking=False):
            return False
        try:
            job = self.repository.claim(self.lease_seconds, self.worker_id)
            if not job:
                return False
            return self._execute(job)
        finally:
            self._drain_lock.release()
            if self.stop_event.is_set():
                self._close_connection_if_idle()

    def _execute(self, job):
        heartbeat_finished, lease_lost = threading.Event(), threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(job, heartbeat_finished, lease_lost),
                                     name="investigation-lease-heartbeat", daemon=True)
        heartbeat.start()
        started = time.monotonic()
        trace = None
        try:
            execution = self.repository.execution(job["execution_id"])
            with self.repository.job_scope(job, self.stop_event, lease_lost, deadline=started + self.execution_timeout_seconds), trace_execution(
                execution["case_id"], execution["execution_id"],
                metadata={"job_action": job["action"], "job_attempt": job["attempts"]},
            ) as trace:
                config = {"configurable": {"thread_id": execution["graph_thread_id"]}, "recursion_limit": 30}
                snapshot = self.graph.get_state(config)
                pending_interrupt = bool(snapshot.tasks and any(t.interrupts for t in snapshot.tasks))
                if job["action"] == "resume" and pending_interrupt:
                    payload = Command(resume=job["payload"])
                elif snapshot.values:
                    # Resume committed progress, including an already consumed
                    # review command. Sending it again can replay a later interrupt.
                    payload = None
                elif job["action"] == "resume":
                    raise RepositoryError("review_checkpoint_missing", "검토를 재개할 체크포인트가 없습니다.")
                else:
                    payload = {"case_id": execution["case_id"], "execution_id": execution["execution_id"]}
                if job["action"] != "resume" and pending_interrupt:
                    result = {"__interrupt__": True}
                else:
                    result = self.graph.invoke(payload, config)
                self.repository.check_active_job()
                status = "interrupted" if result.get("__interrupt__") else "succeeded"
                metrics = self._metrics(job, started)
                metrics.update(trace_id=trace.trace_id, span_id=trace.span_id)
                if self.repository.finish_job(job, status, metrics=metrics):
                    trace.set_output({"status": status})
                else:
                    self.last_error_code = "job_lease_lost"
                    trace.set_error("job_lease_lost")
        except Exception as exc:
            code = exc.code if isinstance(exc, (ProviderUnavailable, RepositoryError)) else "investigation_execution_failed"
            self.last_error_code = code
            if code == "worker_shutdown" or self.stop_event.is_set():
                self.repository.release_job(job)
            elif code != "job_lease_lost" and not lease_lost.is_set():
                # No automatic retry loop for auth/quota/format errors. An explicit
                # retry keeps the same durable checkpoint and idempotency contract.
                metrics = self._metrics(job, started)
                if trace is not None:
                    metrics.update(trace_id=trace.trace_id, span_id=trace.span_id)
                self.repository.finish_job(job, "failed", {
                    "code": code, "exception_type": type(exc).__name__,
                    "retryable": code in ("model_timeout", "model_http_429", "model_network_error", "model_process_failed"),
                }, metrics)
        finally:
            heartbeat_finished.set()
            heartbeat.join(timeout=min(self.heartbeat_seconds + 1, 16))
        return True

    def _metrics(self, job, started):
        return {"duration_ms": (time.monotonic() - started) * 1000,
                "checkpoint": "langgraph_sqlite", "job_action": job["action"],
                "job_attempt": job["attempts"],
                **self.repository.execution_metrics(job["execution_id"])}

    def _close_connection_if_idle(self):
        if not self._drain_lock.acquire(blocking=False):
            return False
        try:
            with self._lifecycle_lock:
                if not self._closed:
                    self.checkpoint_connection.close()
                    self._closed = True
            return True
        finally:
            self._drain_lock.release()

    def close(self, timeout=25):
        """Request cooperative cancellation; return whether all work has stopped.

        An uninterruptible tool may outlive the timeout. Keep its checkpoint handle
        and heartbeat alive until it exits instead of closing resources underneath
        it or handing its still-running operation to another worker prematurely.
        """
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=timeout)
        if self.thread and self.thread.is_alive():
            return False
        return self._close_connection_if_idle()
