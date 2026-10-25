"""Durable, immutable analysis reports, separate from investigation checkpoints."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .analysis_reports import report_region_name


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


class AnalysisReportStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS analysis_reports (
                report_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, data TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS analysis_report_research (
                parent_report_id TEXT NOT NULL, prompt_version TEXT NOT NULL,
                lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0, result_id TEXT,
                PRIMARY KEY (parent_report_id, prompt_version))""")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _save(self, db, report):
        identity = {key: report[key] for key in (
            "schema_version", "scope", "rule_version", "threshold_percent", "provenance")}
        guide = report.get("lifecycle_interpretation")
        if guide is not None:
            identity["lifecycle_guide"] = {key: guide[key] for key in ("guide_version", "guide_sha256")}
        if report.get("activity_availability") is not None:
            identity["activity_availability"] = report["activity_availability"]
        if report.get("report_type") == "context_cause_analysis":
            identity.update({key: report[key] for key in (
                "report_type", "parent_report_id", "context_investigation")})
        fingerprint = hashlib.sha256(encoded(identity).encode()).hexdigest()
        row = db.execute("SELECT data FROM analysis_reports WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row:
            return {**json.loads(row["data"]), "reused": True}
        result = {**report, "report_id": "analysis_" + uuid4().hex,
                  "created_at": datetime.now(timezone.utc).isoformat(), "storage_status": "saved"}
        result.pop("reused", None)
        db.execute("INSERT INTO analysis_reports VALUES(?,?,?,?)",
                   (result["report_id"], fingerprint, result["created_at"], encoded(result)))
        return {**result, "reused": False}

    def save(self, report):
        with self.connection(write=True) as db:
            return self._save(db, report)

    def claim_research(self, parent_report_id, prompt_version, *, lease_seconds=600):
        """Claim a bounded cross-process lease; finished work survives restarts."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM analysis_report_research WHERE parent_report_id=? AND prompt_version=?",
                             (parent_report_id, prompt_version)).fetchone()
            if row and row["result_id"]:
                saved = db.execute("SELECT data FROM analysis_reports WHERE report_id=?", (row["result_id"],)).fetchone()
                if saved is None:
                    raise sqlite3.DatabaseError("research_result_missing")
                return {"status": "completed", "report": {**json.loads(saved["data"]), "reused": True}}
            now = time.time()
            if row and row["lease_token"] and row["lease_until"] > now:
                return {"status": "running"}
            token = uuid4().hex
            db.execute("""INSERT INTO analysis_report_research VALUES(?,?,?,?,NULL)
                ON CONFLICT(parent_report_id,prompt_version) DO UPDATE
                SET lease_token=excluded.lease_token,lease_until=excluded.lease_until""",
                (parent_report_id, prompt_version, token, now + lease_seconds))
            return {"status": "claimed", "token": token}

    def finish_research(self, parent_report_id, prompt_version, token, report):
        with self.connection(write=True) as db:
            row = db.execute("SELECT lease_token FROM analysis_report_research WHERE parent_report_id=? AND prompt_version=?",
                             (parent_report_id, prompt_version)).fetchone()
            if not row or row["lease_token"] != token:
                raise sqlite3.DatabaseError("research_lease_lost")
            result = self._save(db, report)
            db.execute("""UPDATE analysis_report_research SET result_id=?,lease_token=NULL,lease_until=0
                WHERE parent_report_id=? AND prompt_version=? AND lease_token=?""",
                (result["report_id"], parent_report_id, prompt_version, token))
            return result

    def release_research(self, parent_report_id, prompt_version, token):
        with self.connection(write=True) as db:
            db.execute("""UPDATE analysis_report_research SET lease_token=NULL,lease_until=0
                WHERE parent_report_id=? AND prompt_version=? AND lease_token=?""",
                (parent_report_id, prompt_version, token))

    def get(self, report_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM analysis_reports WHERE report_id=?", (report_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def family(self, report_id):
        """Resolve stored cause-analysis ancestry without changing any snapshot.

        Both ancestors and descendants are read in one SQLite snapshot. A broken
        or cyclic ancestry fails closed instead of joining unrelated workspaces.
        """
        with self.connection() as db:
            db.execute("BEGIN")
            requested = db.execute("SELECT data FROM analysis_reports WHERE report_id=?", (report_id,)).fetchone()
            if requested is None:
                return None
            root = json.loads(requested["data"])
            visited = set()
            while root.get("report_type") == "context_cause_analysis":
                if root["report_id"] in visited:
                    raise sqlite3.DatabaseError("analysis_family_cycle")
                visited.add(root["report_id"])
                row = db.execute("SELECT data FROM analysis_reports WHERE report_id=?", (root.get("parent_report_id"),)).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("analysis_family_parent_missing")
                root = json.loads(row["data"])
            members, pending = [root["report_id"]], [root["report_id"]]
            while pending:
                ancestor = pending.pop()
                rows = db.execute("""SELECT report_id FROM analysis_reports
                    WHERE json_extract(data,'$.report_type')='context_cause_analysis'
                    AND json_extract(data,'$.parent_report_id')=? ORDER BY created_at,report_id""", (ancestor,)).fetchall()
                for row in rows:
                    if row["report_id"] in members:
                        raise sqlite3.DatabaseError("analysis_family_cycle")
                    members.append(row["report_id"])
                    pending.append(row["report_id"])
            return {"workspace_analysis_report_id": root["report_id"], "family_report_ids": members}

    def list(self, *, limit=20, offset=0, region_code=None, dong_code=None, month=None, sex_code=None, age_code=None):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid_pagination")
        filters = {"region_code": region_code, "dong_code": dong_code, "month": month, "sex_code": sex_code, "age_code": age_code}
        conditions, parameters = [], []
        for key, value in filters.items():
            if value is not None:
                conditions.append(f"json_extract(data,'$.scope.{key}')=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM analysis_reports" + where, parameters).fetchone()[0]
            rows = db.execute("SELECT data FROM analysis_reports" + where + " ORDER BY created_at DESC, report_id DESC LIMIT ? OFFSET ?",
                              (*parameters, limit, offset)).fetchall()
        records = []
        for row in rows:
            report = json.loads(row["data"])
            record = {key: report[key] for key in (
                "report_id", "created_at", "scope", "rule_version", "threshold_percent", "summary",
                "quality_status", "operational_alert_allowed", "storage_status")}
            record.update({key: report.get(key) for key in (
                "report_type", "parent_report_id", "context_investigation_status", "activity_availability", "report_status")})
            record["region_name"] = report_region_name(report)
            records.append(record)
        return {"reports": records, "total_matched": total, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None}
