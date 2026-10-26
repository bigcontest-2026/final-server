"""Versioned, user-authored preventive responses linked to one frozen analysis.

This module never contacts a model, welfare service, or institution. It records
review drafts and produces an immutable report from explicitly selected versions.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from .welfare_support_mapping import CATEGORY_LABELS as SUPPORT_NEED_LABELS
from .analysis_reports import report_region_name
from .planning_details import normalize_planning
from .analysis_reports import present_analysis_report
from .report_document import build_response_document, render_document_markdown

SCHEMA_VERSION = "welfare-plan-2.0"
KIND_LABELS = {"existing_program": "기존 사업 활용", "program_improvement": "사업 보완", "new_program": "신규 예방사업"}
EDIT_FIELDS = ("kind", "title", "target", "rationale", "content", "checks", "selected", "feedback", "program_reference")
REVIEW_FIELDS = {"eligibility": "지원 대상·조건", "support_content": "지원 내용",
                 "delivery": "전달기관·이용 경로", "finance_and_cost": "재원·본인부담·이용 비용"}
STRUCTURED_FIELDS = ("policy_review", "support_needs", "planning_details")
REVIEW_STATUS = {"unconfirmed": "미확인", "confirmed": "사용자 확인 기록", "not_applicable": "해당 없음으로 기록"}
USER_REVIEW_NOTE = "확인 상태와 근거는 사용자가 직접 입력한 기록입니다. 시스템의 검증이나 개인 지원 자격 확정이 아닙니다."
LIMITATIONS = [
    "이 보고서는 담당자가 작성·선택한 대응안을 모은 검토안입니다. 기관의 사업 승인이나 집행 확정을 의미하지 않습니다.",
    "활동 변화와 가능한 원인은 검토 근거이며 개인의 고립 상태나 지원 자격을 확정하지 않습니다.",
    "연결한 사업은 사용자가 선택한 참고 자료입니다. 사업 원문·신청기간·적용 대상·실행 가능성을 별도로 확인해야 합니다.",
    "사업 효과·예산·참여 인원은 별도 확인이 필요하며 이 보고서가 검증하거나 산출한 값이 아닙니다.",
    USER_REVIEW_NOTE,
]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).isoformat()


class WorkspaceConflict(Exception):
    def __init__(self, code, version):
        self.code, self.version = code, version
        super().__init__(code)


class WorkspaceInputError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def review_entry(value=None, previous=None, *, support_need=False):
    if value is not None and not isinstance(value, dict):
        raise WorkspaceInputError("invalid_policy_review")
    merged = {"status": "unconfirmed", "text": "", "evidence": "", **(previous or {}), **(value or {})}
    allowed = {"unconfirmed", "confirmed"} if support_need else set(REVIEW_STATUS)
    if merged.get("status") not in allowed or any(not isinstance(merged.get(key), str) for key in ("text", "evidence")):
        raise WorkspaceInputError("invalid_policy_review")
    if merged["status"] == "confirmed" and (not merged["text"].strip() or not merged["evidence"].strip()):
        raise WorkspaceInputError("review_confirmation_evidence_required")
    return {key: merged[key] for key in ("status", "text", "evidence")} | {"origin": "user_entered"}


def structured_review(incoming, previous=None):
    """Omitted fields and categories preserve stored user records, including old clients."""
    previous = previous or {}
    old_review, supplied = previous.get("policy_review") or {}, incoming.get("policy_review") or {}
    if not isinstance(supplied, dict) or set(supplied) - set(REVIEW_FIELDS):
        raise WorkspaceInputError("invalid_policy_review")
    review = {key: review_entry(supplied.get(key), old_review.get(key)) for key in REVIEW_FIELDS}
    needs = {item["category_code"]: deepcopy(item) for item in previous.get("support_needs", [])}
    updates = incoming.get("support_needs") or []
    if not isinstance(updates, list) or any(not isinstance(item, dict) for item in updates):
        raise WorkspaceInputError("invalid_support_needs")
    categories = [item.get("category_code") for item in updates]
    if any(key not in SUPPORT_NEED_LABELS for key in categories) or len(categories) != len(set(categories)):
        raise WorkspaceInputError("invalid_support_needs")
    for item in updates:
        key = item["category_code"]
        needs[key] = {"category_code": key, **review_entry(item, needs.get(key), support_need=True)}
    return {"policy_review": review, "support_needs": [needs[key] for key in SUPPORT_NEED_LABELS if key in needs]}


def _md(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # Preserve authored newlines but neutralize source/model/author markup.
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def _region_name(analysis):
    return report_region_name(analysis)


def _scope_text(scope):
    sexes = {"MAN": "남성", "WMAN": "여성"}
    ages = {"20G": "20대", "30G": "30대", "40G": "40대", "50G": "50대", "60GU": "60세 이상"}
    month = scope.get("month", "")
    month = f"{month[:4]}년 {int(month[4:])}월" if len(month) == 6 and month.isdigit() else month
    return f"{month} / {sexes.get(scope.get('sex_code'), '전체 성별')} / {ages.get(scope.get('age_code'), '전체 연령')}"


def render_response_report(report):
    if report.get("document"):
        return render_document_markdown(report["document"])
    analysis = report["analysis_snapshot"]
    scope = analysis["scope"]
    lines = [f"# {_md(report['title'])}", "", "담당자 검토안", "",
             f"지역: {_md(_region_name(analysis))} ({_md(scope.get('dong_code') or scope['region_code'])})",
             f"관측 범위: {_md(_scope_text(scope))}", f"원본 현황 분석 보고서: {_md(report['analysis_report_id'])}",
             f"작성 시각: {_md(report['created_at'])}", "",
             "## 지역 현황", "", "저장된 현황 분석을 기준으로 선택한 대응안을 정리했습니다.", ""]
    if scope.get("dong_code"):
        lines.append(f"선택 행정동의 I5 · 활동 집계 지역: 상위 시군구({_md(scope['region_code'])})")
    for assessment in analysis.get("assessments", []):
        s = assessment["scope"]
        lines.append(f"- {_md(_scope_text(s))}: {_md(assessment['category_label'])}")
    investigation = analysis.get("context_investigation") or {}
    if investigation.get("summary"):
        lines.extend(["", "### 지역 맥락 조사 요약", "", _md(investigation["summary"])])
    else:
        lines.extend(["", "지역 맥락 조사는 아직 진행하지 않았습니다."])
    lifecycle = analysis.get("lifecycle_interpretation")
    if isinstance(lifecycle, dict):
        source = lifecycle.get("source") or {}
        pages = source.get("pages") or []
        pages = "·".join(str(page) for page in pages) if isinstance(pages, list) else str(pages)
        lines.extend(["", "### 원본 분석의 생애주기 해석 참고", "",
                      "지역의 생활 여건과 참여 방식을 살펴볼 질문입니다.",
                      f"문헌: {_md(source.get('title') or '출처 미확인')} / {_md(source.get('year') or '')}",
                      f"참고 본문: {_md(pages or '원본 분석 출처 확인')}쪽 / 해석 가이드: {_md(lifecycle.get('guide_version'))}"])
        for group in lifecycle.get("groups") or []:
            lines.append(f"- {_md(group.get('age_label') or '대상 연령 미확인')}: 살펴볼 질문")
            for question in group.get("questions") or []:
                lines.append(f"  - {_md(question)}")
    lines.extend(["", f"## 선택한 대응안 {len(report['selected_plans'])}건", ""])
    for index, plan in enumerate(report["selected_plans"], 1):
        lines.extend([f"### {index}. {_md(plan['title'])}", "",
            f"유형: {KIND_LABELS[plan['kind']]} / 선택 버전: {plan['version']}", "",
            "**대상**", _md(plan["target"]), "", "**검토 이유**", _md(plan["rationale"] or "추가 확인 필요"), "",
            "**주요 내용**", _md(plan["content"]), "", "**확인할 사항**", _md(plan["checks"] or "추가 확인 필요"), ""])
        structured = structured_review({}, plan)
        lines.extend(["**지원 필요 확인 기록**", "", "담당자가 입력한 확인 내용입니다."])
        if not structured["support_needs"]:
            lines.append("- 지원 필요 확인 기록이 아직 없습니다.")
        for need in structured["support_needs"]:
            lines.extend([f"- {SUPPORT_NEED_LABELS[need['category_code']]}: {REVIEW_STATUS[need['status']]}",
                          f"  내용: {_md(need['text'] or '미확인')}", f"  사용자 입력 근거: {_md(need['evidence'] or '미기재')}"])
        lines.extend(["", "**사업 적용 전 네 가지 검토**", ""])
        for key, label in REVIEW_FIELDS.items():
            item = structured["policy_review"][key]
            lines.extend([f"- {label}: {REVIEW_STATUS[item['status']]}", f"  내용: {_md(item['text'] or '미확인')}",
                          f"  사용자 입력 근거: {_md(item['evidence'] or '미기재')}"])
        lines.extend(["", "검토 틀 참고: 생애주기별 사회적 고립 및 외로움 실태와 정책 과제, 본문 39–40·52쪽. 이용 비용과 접근성은 별도 확인합니다.", ""])
        if plan.get("feedback"):
            lines.extend(["**반영할 검토 의견**", _md(plan["feedback"]), ""])
        reference = plan.get("program_reference")
        if reference:
            title = _md(reference["title"])
            source = f"[{title}](<{reference['url']}>)" if reference.get("url") else title
            lines.extend(["**사용자가 연결한 참고 사업**", source,
                f"발행기관: {_md(reference.get('publisher') or '미확인')} / 사업 버전: {_md(reference['program_version_id'])}",
                "사업의 세부 내용은 연결한 원문에서 살펴볼 수 있습니다.", ""])
    lines.extend(["## 출처와 원본 연결", "", f"현황 분석: {_md(report['analysis_report_id'])}",
                  f"관측 규칙: {_md(analysis.get('rule_version'))}"])
    for source in investigation.get("sources", []):
        # Analysis source URLs have already been verified by the research engine.
        title = _md(source.get("title") or source["source_id"])
        url = source.get("url", "")
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        safe = parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password and not re.search(r"[\s<>\\]", url)
        link = f"[{title}](<{url}>)" if safe else title
        lines.append(f"- {_md(source['source_id'])}: {link} / 자료 대상 기간: {_md(source.get('reference_period') or '미확인')}")
    for family, provenance in analysis.get("provenance", {}).items():
        lines.append(f"- {_md(family)}: {_md(provenance.get('run_id'))} / manifest SHA-256 {_md(provenance.get('manifest_sha256'))}")
    return "\n".join(lines) + "\n"


class ResponseWorkspaceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS response_workspaces (
                analysis_report_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS response_plan_revisions (
                analysis_report_id TEXT NOT NULL, plan_id TEXT NOT NULL, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (analysis_report_id, plan_id, version))""")
            db.execute("""CREATE TABLE IF NOT EXISTS response_reports (
                report_id TEXT PRIMARY KEY, analysis_report_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, data TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS response_reports_parent ON response_reports(analysis_report_id, created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS response_workspace_members (
                analysis_report_id TEXT PRIMARY KEY, workspace_analysis_report_id TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS response_workspace_family ON response_workspace_members(workspace_analysis_report_id)")

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

    @staticmethod
    def _summary(report):
        keys = ("report_id", "analysis_report_id", "workspace_analysis_report_id", "report_type", "title", "created_at", "workspace_version", "storage_status", "filename", "selected_plan_count", "scope", "region_name")
        return {key: report[key] for key in keys if key in report}

    def _link_family(self, db, analysis_report_id, workspace_analysis_report_id=None, family_report_ids=None):
        """Import legacy per-report drafts once; keep their original rows intact.

        The member rows are migration receipts, committed atomically with copied
        revisions and the common draft. Saved analysis/response reports are never
        rewritten. A merge advances beyond every source version, invalidating
        editors opened before two populated workspaces became one.
        """
        existing = db.execute("SELECT workspace_analysis_report_id FROM response_workspace_members WHERE analysis_report_id=?", (analysis_report_id,)).fetchone()
        root = workspace_analysis_report_id or (existing[0] if existing else analysis_report_id)
        members = list(dict.fromkeys([root, *(family_report_ids or []), analysis_report_id]))
        row = db.execute("SELECT data FROM response_workspaces WHERE analysis_report_id=?", (root,)).fetchone()
        workspace = json.loads(row["data"]) if row else {"analysis_report_id": root, "version": 0, "updated_at": None, "plans": [], "storage_status": "saved"}
        imported, max_version = False, workspace["version"]
        for member in members:
            linked = db.execute("SELECT workspace_analysis_report_id FROM response_workspace_members WHERE analysis_report_id=?", (member,)).fetchone()
            if linked and linked[0] == root:
                continue
            if linked and linked[0] != member:
                raise sqlite3.DatabaseError("response_workspace_family_mismatch")
            if member != root:
                old = db.execute("SELECT data FROM response_workspaces WHERE analysis_report_id=?", (member,)).fetchone()
                legacy = json.loads(old["data"]) if old else None
                if legacy and legacy["plans"]:
                    imported = True
                    max_version = max(max_version, legacy["version"])
                    occupied = {plan["plan_id"] for plan in workspace["plans"]}
                    for old_plan in legacy["plans"]:
                        plan = deepcopy(old_plan)
                        old_id = plan["plan_id"]
                        # Equal ids can still hold different authored versions;
                        # preserve each branch rather than selecting a winner.
                        if old_id in occupied:
                            plan["plan_id"] = "plan_" + uuid5(NAMESPACE_URL, encoded([root, member, old_id])).hex
                        occupied.add(plan["plan_id"])
                        workspace["plans"].append(plan)
                        revisions = db.execute("SELECT version,created_at,data FROM response_plan_revisions WHERE analysis_report_id=? AND plan_id=? ORDER BY version", (member, old_id)).fetchall()
                        for revision in revisions:
                            data = json.loads(revision["data"])
                            data["plan_id"] = plan["plan_id"]
                            db.execute("INSERT INTO response_plan_revisions VALUES(?,?,?,?,?)", (root, plan["plan_id"], revision["version"], revision["created_at"], encoded(data)))
            db.execute("""INSERT INTO response_workspace_members VALUES(?,?) ON CONFLICT(analysis_report_id)
                DO UPDATE SET workspace_analysis_report_id=excluded.workspace_analysis_report_id""", (member, root))
        if imported:
            workspace.update(analysis_report_id=root, version=max_version + 1, updated_at=now())
            workspace.pop("reports", None)
            db.execute("""INSERT INTO response_workspaces VALUES(?,?,?,?) ON CONFLICT(analysis_report_id)
                DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at,data=excluded.data""",
                (root, workspace["version"], workspace["updated_at"], encoded(workspace)))
        return root

    @staticmethod
    def _family_members(db, root):
        rows = db.execute("SELECT analysis_report_id FROM response_workspace_members WHERE workspace_analysis_report_id=?", (root,)).fetchall()
        return list(dict.fromkeys([root, *(row[0] for row in rows)]))

    def _read(self, db, analysis_report_id, root=None):
        root = root or analysis_report_id
        row = db.execute("SELECT data FROM response_workspaces WHERE analysis_report_id=?", (root,)).fetchone()
        workspace = json.loads(row["data"]) if row else {"version": 0, "updated_at": None, "plans": [], "storage_status": "saved"}
        workspace.update(analysis_report_id=analysis_report_id, workspace_analysis_report_id=root)
        members = self._family_members(db, root)
        placeholders = ",".join("?" for _ in members)
        reports = db.execute(f"SELECT data FROM response_reports WHERE analysis_report_id IN ({placeholders}) ORDER BY created_at DESC,report_id DESC", members).fetchall()
        workspace["reports"] = [self._summary(json.loads(item["data"])) for item in reports]
        for plan in workspace["plans"]:
            plan.update(structured_review({}, plan))
            rows = db.execute("SELECT data FROM response_plan_revisions WHERE analysis_report_id=? AND plan_id=? ORDER BY version DESC LIMIT 25", (root, plan["plan_id"])).fetchall()
            plan["history"] = [{key: data[key] for key in ("version", "title", "selected", "feedback", "updated_at", "changed_fields")}
                               for data in (json.loads(item["data"]) for item in rows)]
        return workspace

    def get(self, analysis_report_id, **family):
        with self.connection(write=True) as db:
            root = self._link_family(db, analysis_report_id, **family)
            return self._read(db, analysis_report_id, root)

    def save(self, analysis_report_id, expected_version, plans, **family):
        with self.connection(write=True) as db:
            root = self._link_family(db, analysis_report_id, **family)
            workspace = self._read(db, analysis_report_id, root)
            if workspace["version"] != expected_version:
                raise WorkspaceConflict("response_workspace_changed", workspace["version"])
            old_by_id = {plan["plan_id"]: plan for plan in workspace["plans"]}
            if len(plans) > max(20, len(old_by_id)):
                raise WorkspaceInputError("response_plan_limit")
            requested_ids = [plan["plan_id"] for plan in plans if plan.get("plan_id")]
            if len(requested_ids) != len(set(requested_ids)):
                raise WorkspaceInputError("duplicate_response_plan")
            if set(requested_ids) - set(old_by_id):
                raise WorkspaceInputError("response_plan_not_in_workspace")
            if set(old_by_id) - set(requested_ids):
                raise WorkspaceInputError("response_plan_omission")
            updated, changed, timestamp = [], False, now()
            for incoming in plans:
                plan = {key: deepcopy(incoming[key]) for key in EDIT_FIELDS}
                old = old_by_id.get(incoming.get("plan_id"))
                plan.update(structured_review(incoming, old))
                try:
                    plan["planning_details"] = normalize_planning(incoming.get("planning_details"), (old or {}).get("planning_details"))
                except ValueError:
                    raise WorkspaceInputError("invalid_planning_details") from None
                reference = plan.get("program_reference")
                if reference:
                    reference["origin"] = "user_selected_welfare_catalog"
                    reference["verification_status"] = "user_selected_unverified"
                fields = [key for key in (*EDIT_FIELDS, *STRUCTURED_FIELDS) if old is None or encoded(old.get(key)) != encoded(plan[key])]
                if not fields:
                    updated.append({key: deepcopy(value) for key, value in old.items() if key != "history"})
                    continue
                changed = True
                plan.update({"plan_id": old["plan_id"] if old else "plan_" + uuid4().hex,
                             "version": old["version"] + 1 if old else 1,
                             "created_at": old["created_at"] if old else timestamp,
                             "updated_at": timestamp, "changed_fields": fields})
                db.execute("INSERT INTO response_plan_revisions VALUES(?,?,?,?,?)", (root, plan["plan_id"], plan["version"], timestamp, encoded(plan)))
                updated.append(plan)
            if [plan["plan_id"] for plan in updated] != [plan["plan_id"] for plan in workspace["plans"]]:
                changed = True
            if changed:
                result = {"analysis_report_id": root, "version": workspace["version"] + 1,
                          "updated_at": timestamp, "plans": updated, "storage_status": "saved"}
                db.execute("""INSERT INTO response_workspaces VALUES(?,?,?,?) ON CONFLICT(analysis_report_id)
                    DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at,data=excluded.data""",
                    (root, result["version"], timestamp, encoded(result)))
            return self._read(db, analysis_report_id, root)

    def create_report(self, analysis, expected_workspace_version, selected_plan_versions, title=None, **family):
        analysis_report_id = analysis["report_id"]
        with self.connection(write=True) as db:
            root = self._link_family(db, analysis_report_id, **family)
            workspace = self._read(db, analysis_report_id, root)
            if workspace["version"] != expected_workspace_version:
                raise WorkspaceConflict("response_workspace_changed", workspace["version"])
            selected = [plan for plan in workspace["plans"] if plan["selected"]]
            requested = [(item["plan_id"], item["version"]) for item in selected_plan_versions]
            actual = [(plan["plan_id"], plan["version"]) for plan in selected]
            if not selected or not requested:
                raise WorkspaceInputError("response_selection_empty")
            if len(set(requested)) != len(requested) or set(requested) != set(actual):
                raise WorkspaceConflict("response_selection_changed", workspace["version"])
            if any(not plan["title"].strip() or not plan["target"].strip() or not plan["content"].strip() for plan in selected):
                raise WorkspaceInputError("response_plan_incomplete")
            selected = [{key: deepcopy(value) for key, value in plan.items() if key != "history"} for plan in selected]
            has_existing = any(plan['kind'] == 'existing_program' for plan in selected)
            title = title or f"{_region_name(analysis)} " + ("복지서비스 연계 및 대응계획(안)" if has_existing else "복지사업 보완·신규 추진계획(안)")
            fingerprint = hashlib.sha256(encoded({"schema_version": SCHEMA_VERSION, "analysis_report_id": analysis_report_id,
                "workspace_analysis_report_id": root, "plans": selected, "title": title}).encode()).hexdigest()
            row = db.execute("SELECT data FROM response_reports WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row:
                return {**json.loads(row["data"]), "reused": True}
            result = {"schema_version": SCHEMA_VERSION, "report_id": "response_" + uuid4().hex,
                      "report_type": "preventive_response", "analysis_report_id": analysis_report_id, "workspace_analysis_report_id": root,
                      "workspace_version": workspace["version"], "title": title, "created_at": now(),
                      "scope": deepcopy(analysis["scope"]), "region_name": _region_name(analysis),
                      "selected_plans": selected, "selected_plan_count": len(selected), "analysis_snapshot": deepcopy(analysis),
                      "storage_status": "saved", "generation_method": "selected_authored_versions",
                      "quality_status": analysis.get("quality_status", "hold"), "operational_alert_allowed": False,
                      "limitations": list(LIMITATIONS)}
            result["filename"] = f"HERMES_예방대응_{analysis['scope'].get('dong_code') or analysis['scope']['region_code']}_{analysis['scope']['month']}_{result['report_id']}.md"
            result["document"] = build_response_document(result)
            result["markdown"] = render_response_report(result)
            db.execute("INSERT INTO response_reports VALUES(?,?,?,?,?)", (result["report_id"], analysis_report_id, fingerprint, result["created_at"], encoded(result)))
            return {**result, "reused": False}

    def get_report(self, report_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM response_reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            return None
        report = json.loads(row["data"])
        if report.get("analysis_snapshot"):
            report["analysis_snapshot"] = present_analysis_report(report["analysis_snapshot"])
        return report

    def list_reports(self, *, analysis_report_id=None, scope_filters=None, limit=20, offset=0, **family):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise WorkspaceInputError("invalid_pagination")
        clauses, values = [], []
        for field, value in (scope_filters or {}).items():
            if field not in ("region_code", "dong_code", "month", "sex_code", "age_code"):
                raise WorkspaceInputError("unsupported_scope_filter")
            clauses.append(f"json_extract(data,'$.scope.{field}')=?")
            values.append(value)
        with self.connection(write=bool(analysis_report_id)) as db:
            if analysis_report_id:
                root = self._link_family(db, analysis_report_id, **family)
                members = self._family_members(db, root)
                placeholders = ",".join("?" for _ in members)
                clauses.append(f"analysis_report_id IN ({placeholders})")
                values.extend(members)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            values = tuple(values)
            total = db.execute(f"SELECT COUNT(*) FROM response_reports {where}", values).fetchone()[0]
            rows = db.execute(f"SELECT data FROM response_reports {where} ORDER BY created_at DESC,report_id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return {"reports": [self._summary(json.loads(row["data"])) for row in rows], "total_matched": total,
                "offset": offset, "next_offset": offset + limit if offset + limit < total else None}
