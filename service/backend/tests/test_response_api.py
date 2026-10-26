"""Selected, authored response versions survive refresh and remain immutable."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.analysis_api import analysis_router
from app.config import Settings
from app.response_api import response_router
from app.security import install_security
from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.response_workspace import ResponseWorkspaceStore
from test_analysis_api import report_settings, request


ANALYSIS = "/api/model/analysis-reports"
REPORTS = "/api/model/response-reports"
EDIT_FIELDS = ("plan_id", "kind", "title", "target", "rationale", "content", "checks", "selected", "feedback", "program_reference")


def application(settings):
    app = FastAPI()
    install_security(app, settings)
    app.include_router(analysis_router(settings))
    app.include_router(response_router(settings))
    return app


def plan(**changes):
    return {"plan_id": None, "kind": "new_program", "title": "지역 참여공간 이용 검토", "target": "지역 주민 중 추가 확인한 참여 희망자",
            "rationale": "관측 변화와 실제 수요의 관계는 추가 확인이 필요합니다.", "content": "기존 참여공간 이용 현황을 확인하고 접근 방법을 검토합니다.",
            "checks": "담당 부서와 실제 수요·공간 이용 가능 여부 확인", "selected": True, "feedback": "", "program_reference": None, **changes}


def editable(item):
    result = {key: deepcopy(item[key]) for key in EDIT_FIELDS}
    if result["program_reference"]:
        result["program_reference"] = {key: result["program_reference"].get(key) for key in ("program_version_id", "title", "url", "publisher")}
    return result


def base(client, **changes):
    result = client.post(ANALYSIS, json=request(**changes))
    assert result.status_code == 200, result.text
    return result.json()


def save(client, report_id, version=0, plans=None):
    result = client.put(f"{ANALYSIS}/{report_id}/response-workspace", json={"expected_version": version, "plans": plans if plans is not None else [plan()]})
    assert result.status_code == 200, result.text
    return result.json()


def generate(client, report_id, workspace, **changes):
    body = {"expected_workspace_version": workspace["version"], "selected_plan_versions": [
        {"plan_id": item["plan_id"], "version": item["version"]} for item in workspace["plans"] if item["selected"]], **changes}
    return client.post(f"{ANALYSIS}/{report_id}/response-reports", json=body)


def test_dong_response_report_keeps_selected_area_and_parent_activity_reference(report_settings):
    with TestClient(application(report_settings)) as client:
        original = base(client, dong_code="1168051000")
        workspace = save(client, original["report_id"])
        response = generate(client, original["report_id"], workspace)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["scope"]["dong_code"] == "1168051000"
        assert report["region_name"] == "강남구 신사동"
        assert report["title"].startswith("강남구 신사동")
        assert "1168051000" in report["filename"]
        assert "활동 집계 지역: 상위 시군구(11680)" in report["markdown"]
        for boilerplate in ("해석하지 않습니다", "확정 근거로 사용하지 않습니다", "## 검토 한계"):
            assert boilerplate not in report["markdown"]
        assert report["limitations"]
        assert all(item in report["markdown"] for item in report["limitations"])
        selected = client.get(REPORTS + "?region_code=11680&dong_code=1168051000").json()
        assert selected["total_matched"] == 1
        assert selected["reports"][0]["report_id"] == report["report_id"]
        assert client.get(REPORTS + "?dong_code=1168052000").json()["total_matched"] == 0
        assert client.get(REPORTS + "?region_code=51110&dong_code=1168051000").status_code == 422


def test_workspace_versions_and_report_persist_after_restart(report_settings):
    with TestClient(application(report_settings)) as client:
        analysis = base(client)
        parent = analysis["report_id"]
        empty = client.get(f"{ANALYSIS}/{parent}/response-workspace").json()
        assert empty == {"analysis_report_id": parent, "workspace_analysis_report_id": parent, "version": 0, "plans": [], "reports": [], "updated_at": None, "storage_status": "saved"}
        saved = save(client, parent, plans=[plan(), plan(kind="program_improvement", title="기존 사업 접근 방법 보완")])
        assert saved["version"] == 1
        assert len(saved["plans"]) == 2 and all(item["version"] == 1 for item in saved["plans"])
        first_id = saved["plans"][0]["plan_id"]
        changed = [editable(item) for item in saved["plans"]]
        changed[0].update(content="검토 의견을 반영해 기존 참여공간 운영시간을 확인합니다.", feedback="운영시간을 먼저 확인해 주세요.")
        revised = save(client, parent, 1, changed)
        assert revised["version"] == 2 and revised["plans"][0]["version"] == 2
        assert revised["plans"][1]["version"] == 1
        assert [item["version"] for item in revised["plans"][0]["history"]] == [2, 1]
        assert revised["plans"][0]["history"][0]["feedback"] == changed[0]["feedback"]
        same = save(client, parent, 2, [editable(item) for item in revised["plans"]])
        assert same["version"] == 2
        result = generate(client, parent, revised)
        assert result.status_code == 200, result.text
        report = result.json()
        assert report["report_type"] == "preventive_response"
        assert report["analysis_report_id"] == parent and report["selected_plan_count"] == 2
        assert report["analysis_snapshot"]["assessments"] == analysis["assessments"]
        assert report["generation_method"] == "selected_authored_versions"
        assert report["selected_plans"][0]["plan_id"] == first_id
        assert report["selected_plans"][0]["version"] == 2
        assert "운영시간을 먼저 확인" in report["markdown"]
        assert "담당자 검토안" in report["markdown"] and report["operational_alert_allowed"] is False
        repeated = generate(client, parent, revised).json()
        assert repeated["reused"] is True and repeated["report_id"] == report["report_id"]
    with TestClient(application(report_settings)) as client:
        workspace = client.get(f"{ANALYSIS}/{parent}/response-workspace").json()
        assert workspace["version"] == 2 and workspace["reports"][0]["report_id"] == report["report_id"]
        assert client.get(f"{REPORTS}/{report['report_id']}").json()["markdown"] == report["markdown"]
        assert client.get(ANALYSIS + "/" + parent).json()["assessments"] == analysis["assessments"]
    assert report_settings.investigation_db_path.with_name("response_workspaces.sqlite3").exists()


def test_old_report_is_immutable_after_later_edits(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        saved = save(client, parent)
        original = generate(client, parent, saved).json()
        changed = [editable(item) for item in saved["plans"]]
        changed[0]["content"] = "후속 의견을 반영한 다른 검토 내용"
        next_workspace = save(client, parent, 1, changed)
        latest = generate(client, parent, next_workspace).json()
        assert original["report_id"] != latest["report_id"]
        old = client.get(f"{REPORTS}/{original['report_id']}").json()
        assert old["markdown"] == original["markdown"]
        assert old["selected_plans"][0]["content"] != changed[0]["content"]


def test_stale_editor_and_stale_selection_conflict_without_losing_content(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        first = save(client, parent)
        changes = [editable(item) for item in first["plans"]]
        changes[0]["feedback"] = "먼저 저장한 의견"
        save(client, parent, 1, changes)
        failed = client.put(f"{ANALYSIS}/{parent}/response-workspace", json={"expected_version": 1, "plans": [editable(item) for item in first["plans"]]})
        assert failed.status_code == 409 and failed.json()["detail"]["current_version"] == 2
        assert generate(client, parent, first).status_code == 409
        assert client.get(f"{ANALYSIS}/{parent}/response-workspace").json()["plans"][0]["feedback"] == "먼저 저장한 의견"


def test_generation_requires_exact_selected_versions_and_never_includes_unselected(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        saved = save(client, parent, plans=[plan(), plan(title="선택하지 않은 대응안", selected=False)])
        report = generate(client, parent, saved).json()
        assert report["selected_plan_count"] == 1
        assert "선택하지 않은 대응안" not in report["markdown"]
        bad = [{"plan_id": item["plan_id"], "version": item["version"]} for item in saved["plans"]]
        assert generate(client, parent, saved, selected_plan_versions=bad).status_code == 409
        wrong = [{"plan_id": saved["plans"][0]["plan_id"], "version": 99}]
        assert generate(client, parent, saved, selected_plan_versions=wrong).status_code == 409
        assert generate(client, parent, saved, selected_plan_versions=wrong * 2).status_code == 409


def test_draft_can_be_incomplete_but_final_report_requires_target_and_content(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        saved = save(client, parent, plans=[plan(target="", content="")])
        result = generate(client, parent, saved)
        assert result.status_code == 422 and result.json()["detail"]["code"] == "response_plan_incomplete"
        changed = [editable(item) for item in saved["plans"]]
        changed[0]["selected"] = False
        deselected = save(client, parent, 1, changed)
        assert generate(client, parent, deselected).status_code == 422
        assert client.get(REPORTS).json()["total_matched"] == 0


def test_missing_parent_and_foreign_plan_cannot_create_workspace(report_settings):
    with TestClient(application(report_settings)) as client:
        assert client.get(f"{ANALYSIS}/missing/response-workspace").status_code == 404
        assert client.put(f"{ANALYSIS}/missing/response-workspace", json={"expected_version": 0, "plans": [plan()]}).status_code == 404
        one, two = base(client)["report_id"], base(client, age_code="40G")["report_id"]
        saved = save(client, one)
        foreign = client.put(f"{ANALYSIS}/{two}/response-workspace", json={"expected_version": 0, "plans": [editable(saved["plans"][0])]})
        assert foreign.status_code == 422 and foreign.json()["detail"]["code"] == "response_plan_not_in_workspace"


def test_snapshot_cannot_silently_drop_or_duplicate_saved_plans(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        saved = save(client, parent)
        url = f"{ANALYSIS}/{parent}/response-workspace"
        dropped = client.put(url, json={"expected_version": 1, "plans": []})
        assert dropped.status_code == 422 and dropped.json()["detail"]["code"] == "response_plan_omission"
        duplicate = client.put(url, json={"expected_version": 1, "plans": [editable(saved["plans"][0])] * 2})
        assert duplicate.status_code == 422 and duplicate.json()["detail"]["code"] == "duplicate_response_plan"
        assert client.get(url).json()["version"] == 1


def test_reference_is_explicitly_unverified_and_report_escapes_markup(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        ref = {"program_version_id": "published-program-1", "title": "참고사업 <script>bad()</script>", "url": "https://example.go.kr/notice", "publisher": "자료 제공기관"}
        saved = save(client, parent, plans=[plan(title="<img src=x onerror=bad()>", content="[가짜](//evil.example) <script>bad()</script>", feedback="<svg onload=bad()>", program_reference=ref)])
        source = saved["plans"][0]["program_reference"]
        assert source["verification_status"] == "user_selected_unverified"
        assert source["origin"] == "user_selected_welfare_catalog"
        report = generate(client, parent, saved).json()
        assert "<script>" not in report["markdown"] and "<svg onload=bad()>" not in report["markdown"]
        assert "[가짜](//evil.example)" not in report["markdown"]
        assert "https://example.go.kr/notice" in report["markdown"]
        assert "사용자가 연결한 참고 사업" in report["markdown"]


@pytest.mark.parametrize("url", ["javascript:bad()", "https://user:password@example.go.kr/a", "http://127.0.0.1/private", "https://localhost/private", "https://example.go.kr/a>bad", "https://example.go.kr/\nscript"])
def test_unsafe_reference_urls_are_rejected(report_settings, url):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        result = client.put(f"{ANALYSIS}/{parent}/response-workspace", json={"expected_version": 0, "plans": [plan(program_reference={"program_version_id": "one", "title": "사업", "url": url})]})
        assert result.status_code == 422


def test_report_list_scopes_and_pagination(report_settings):
    with TestClient(application(report_settings)) as client:
        one, two = base(client)["report_id"], base(client, age_code="40G")["report_id"]
        first = generate(client, one, save(client, one)).json()
        second = generate(client, two, save(client, two)).json()
        assert client.get(REPORTS).json()["total_matched"] == 2
        page = client.get(REPORTS + "?limit=1").json()
        assert page["next_offset"] == 1
        assert client.get(REPORTS + "?offset=1&limit=1").json()["next_offset"] is None
        by_age = client.get(REPORTS + "?region_code=11680&month=202512&sex_code=MAN&age_code=20G").json()
        assert by_age["total_matched"] == 1 and by_age["reports"][0]["report_id"] == first["report_id"]
        assert by_age["reports"][0]["scope"] == first["scope"] and by_age["reports"][0]["region_name"]
        assert client.get(REPORTS + "?analysis_report_id=" + two).json()["reports"][0]["report_id"] == second["report_id"]
        assert client.get(REPORTS + "?region_code=51110").json()["total_matched"] == 0
        assert client.get(REPORTS + "?analysis_report_id=missing").status_code == 404
        assert client.get(REPORTS + "/missing").status_code == 404


@pytest.mark.parametrize("query", ["region_code=999", "month=202513", "sex_code=ALL", "age_code=young", "limit=0", "offset=-1", "extra=true"])
def test_list_rejects_invalid_filters(report_settings, query):
    with TestClient(application(report_settings)) as client:
        assert client.get(REPORTS + "?" + query).status_code == 422


def test_unknown_client_fields_and_forged_versions_are_rejected(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        url = f"{ANALYSIS}/{parent}/response-workspace"
        for changes in ({"version": 99}, {"verified": True}, {"quality_status": "ok"}, {"plan_id": "other"}):
            result = client.put(url, json={"expected_version": 0, "plans": [plan(**changes)]})
            assert result.status_code == 422
        assert client.put(url, json={"expected_version": True, "plans": [plan()]}).status_code == 422
        assert client.put(url, json={"expected_version": 0, "plans": [plan(title=" ")]}).status_code == 422


def researched_child(store, original, summary="합성 시험"):
    child = deepcopy(original)
    child.update(report_type="context_cause_analysis", parent_report_id=original["report_id"], context_investigation={"status": "no_evidence", "summary": summary, "sources": []}, context_investigation_status="no_evidence")
    for key in ("report_id", "created_at", "storage_status", "reused"):
        child.pop(key, None)
    return store.save(child)


def test_researched_child_shares_workspace_but_remains_exact_analysis_snapshot(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    with TestClient(application(report_settings)) as client:
        original = base(client)
        saved = save(client, original["report_id"])
        researched = researched_child(store, original)
        shared = client.get(f"{ANALYSIS}/{researched['report_id']}/response-workspace").json()
        assert shared["analysis_report_id"] == researched["report_id"]
        assert shared["workspace_analysis_report_id"] == original["report_id"]
        assert shared["plans"] == saved["plans"] and shared["version"] == 1
        report = generate(client, researched["report_id"], saved).json()
        assert report["analysis_report_id"] == researched["report_id"]
        assert report["workspace_analysis_report_id"] == original["report_id"]
        assert report["analysis_snapshot"]["context_investigation_status"] == "no_evidence"
        assert client.get(f"{ANALYSIS}/{original['report_id']}/response-workspace").json()["reports"][0]["report_id"] == report["report_id"]
        assert client.get(REPORTS, params={"analysis_report_id": original["report_id"]}).json()["reports"][0]["report_id"] == report["report_id"]
        changed = [editable(item) for item in shared["plans"]]
        changed[0]["feedback"] = "조사 뒤 추가한 의견"
        revised = save(client, researched["report_id"], 1, changed)
        stale = client.put(f"{ANALYSIS}/{original['report_id']}/response-workspace", json={"expected_version": 1, "plans": [editable(item) for item in saved["plans"]]})
        assert stale.status_code == 409 and stale.json()["detail"]["current_version"] == 2
        assert generate(client, original["report_id"], saved).status_code == 409
        root_report = generate(client, original["report_id"], revised).json()
        assert root_report["analysis_snapshot"] == client.get(f"{ANALYSIS}/{original['report_id']}").json()
        assert "context_investigation_status" not in root_report["analysis_snapshot"] or root_report["analysis_snapshot"]["context_investigation_status"] != "no_evidence"
    with TestClient(application(report_settings)) as client:
        root = client.get(f"{ANALYSIS}/{original['report_id']}/response-workspace").json()
        child = client.get(f"{ANALYSIS}/{researched['report_id']}/response-workspace").json()
        assert root["plans"] == child["plans"] == revised["plans"]
        assert root["version"] == child["version"] == 2
        assert len(root["reports"]) == len(child["reports"]) == 2
        assert client.get(f"{REPORTS}/{report['report_id']}").json()["analysis_snapshot"] == report["analysis_snapshot"]


def test_family_migration_preserves_both_legacy_spaces_history_and_reports(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    legacy = ResponseWorkspaceStore(report_settings.investigation_db_path.with_name("response_workspaces.sqlite3"))
    with TestClient(application(report_settings)) as client:
        original = base(client)
        child = researched_child(store, original)
        # Seed through the old per-id storage contract, before family-aware access.
        root_saved = legacy.save(original["report_id"], 0, [plan(title="원본의 기존 대응안")])
        child_saved = legacy.save(child["report_id"], 0, [plan(title="조사의 기존 대응안")])
        child_plans = [editable(item) for item in child_saved["plans"]]
        child_plans[0]["feedback"] = "이전 조사 의견"
        child_saved = legacy.save(child["report_id"], 1, child_plans)
        root_report = legacy.create_report(original, root_saved["version"], [{"plan_id": root_saved["plans"][0]["plan_id"], "version": 1}])
        child_report = legacy.create_report(child, child_saved["version"], [{"plan_id": child_saved["plans"][0]["plan_id"], "version": 2}])
        merged = client.get(f"{ANALYSIS}/{original['report_id']}/response-workspace").json()
        assert {item["title"] for item in merged["plans"]} == {"원본의 기존 대응안", "조사의 기존 대응안"}
        assert merged["version"] > max(root_saved["version"], child_saved["version"])
        migrated_child = next(item for item in merged["plans"] if item["title"] == "조사의 기존 대응안")
        assert [item["version"] for item in migrated_child["history"]] == [2, 1]
        assert migrated_child["feedback"] == "이전 조사 의견"
        assert {item["report_id"] for item in merged["reports"]} == {root_report["report_id"], child_report["report_id"]}
        assert client.get(f"{REPORTS}/{root_report['report_id']}").json()["markdown"] == root_report["markdown"]
        assert client.get(f"{REPORTS}/{child_report['report_id']}").json()["analysis_snapshot"] == child_report["analysis_snapshot"]
        stale = client.put(f"{ANALYSIS}/{child['report_id']}/response-workspace", json={"expected_version": child_saved["version"], "plans": child_plans})
        assert stale.status_code == 409
        newest = save(client, child["report_id"], merged["version"], [editable(item) for item in merged["plans"]])
        assert newest["version"] == merged["version"]
    with TestClient(application(report_settings)) as client:
        for report_id in (original["report_id"], child["report_id"]):
            loaded = client.get(f"{ANALYSIS}/{report_id}/response-workspace").json()
            assert loaded["analysis_report_id"] == report_id
            assert loaded["workspace_analysis_report_id"] == original["report_id"]
            assert loaded["plans"] == merged["plans"] and loaded["version"] == merged["version"]
            listed = client.get(REPORTS, params={"analysis_report_id": report_id}).json()
            assert {item["report_id"] for item in listed["reports"]} == {root_report["report_id"], child_report["report_id"]}


def test_merged_legacy_plans_over_twenty_remain_editable_and_reportable(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    legacy = ResponseWorkspaceStore(report_settings.investigation_db_path.with_name("response_workspaces.sqlite3"))
    with TestClient(application(report_settings)) as client:
        original = base(client)
        child = researched_child(store, original)
        for source in (original, child):
            legacy.save(source["report_id"], 0, [plan(title=f"기존 검토안 {index}") for index in range(12)])
        merged = client.get(f"{ANALYSIS}/{child['report_id']}/response-workspace").json()
        assert len(merged["plans"]) == 24
        changed = [editable(item) for item in merged["plans"]]
        changed[-1]["feedback"] = "보존된 대응안의 추가 의견"
        revised = save(client, original["report_id"], merged["version"], changed)
        generated = generate(client, child["report_id"], revised)
        assert generated.status_code == 200, generated.text
        assert generated.json()["selected_plan_count"] == 24
        extra = client.put(f"{ANALYSIS}/{original['report_id']}/response-workspace", json={"expected_version": revised["version"], "plans": changed + [plan()]})
        assert extra.status_code == 422
        unrelated = base(client, age_code="40G")
        oversized = client.put(f"{ANALYSIS}/{unrelated['report_id']}/response-workspace", json={"expected_version": 0, "plans": [plan() for _ in range(21)]})
        assert oversized.status_code == 422


def test_colliding_legacy_plan_ids_preserve_each_authored_branch_and_history(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    legacy = ResponseWorkspaceStore(report_settings.investigation_db_path.with_name("response_workspaces.sqlite3"))
    with TestClient(application(report_settings)) as client:
        original = base(client)
        child = researched_child(store, original)
        root_saved = legacy.save(original["report_id"], 0, [plan(title="원본 분기")])
        child_saved = legacy.save(child["report_id"], 0, [plan(title="별도로 수정한 조사 분기")])
        root_id, child_id = root_saved["plans"][0]["plan_id"], child_saved["plans"][0]["plan_id"]
        # Historical imports may carry copied plan ids with independent contents.
        with legacy.connection(write=True) as db:
            for table in ("response_workspaces", "response_plan_revisions"):
                rows = db.execute(f"SELECT rowid,data FROM {table} WHERE analysis_report_id=?", (child["report_id"],)).fetchall()
                for row in rows:
                    db.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (row["data"].replace(child_id, root_id), row["rowid"]))
            db.execute("UPDATE response_plan_revisions SET plan_id=? WHERE analysis_report_id=?", (root_id, child["report_id"]))
        merged = client.get(f"{ANALYSIS}/{child['report_id']}/response-workspace").json()
        assert {item["title"] for item in merged["plans"]} == {"원본 분기", "별도로 수정한 조사 분기"}
        assert len({item["plan_id"] for item in merged["plans"]}) == 2
        assert all(len(item["history"]) == 1 and item["history"][0]["title"] == item["title"] for item in merged["plans"])
        with legacy.connection() as db:
            old = json.loads(db.execute("SELECT data FROM response_workspaces WHERE analysis_report_id=?", (child["report_id"],)).fetchone()[0])
            assert old["plans"][0]["plan_id"] == root_id and old["plans"][0]["title"] == "별도로 수정한 조사 분기"
        assert save(client, original["report_id"], merged["version"], [editable(item) for item in merged["plans"]])["version"] == merged["version"]


def test_legacy_child_report_reuse_does_not_lose_canonical_workspace_link(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    legacy = ResponseWorkspaceStore(report_settings.investigation_db_path.with_name("response_workspaces.sqlite3"))
    with TestClient(application(report_settings)) as client:
        original = base(client)
        child = researched_child(store, original)
        saved = legacy.save(child["report_id"], 0, [plan()])
        previous = legacy.create_report(child, saved["version"], [{"plan_id": saved["plans"][0]["plan_id"], "version": 1}])
        shared = client.get(f"{ANALYSIS}/{child['report_id']}/response-workspace").json()
        current = generate(client, child["report_id"], shared).json()
        assert current["workspace_analysis_report_id"] == original["report_id"]
        assert current["analysis_snapshot"] == client.get(f"{ANALYSIS}/{child['report_id']}").json()
        assert current["analysis_snapshot"]["context_investigation"] == previous["analysis_snapshot"]["context_investigation"]
        assert client.get(f"{REPORTS}/{previous['report_id']}").json()["workspace_analysis_report_id"] == child["report_id"]
        assert generate(client, child["report_id"], shared).json()["report_id"] == current["report_id"]


def test_new_siblings_and_nested_investigations_share_the_original_family(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    with TestClient(application(report_settings)) as client:
        original = base(client)
        saved = save(client, original["report_id"])
        child = researched_child(store, original, "첫 번째 조사")
        sibling = researched_child(store, original, "다른 비교 근거의 조사")
        nested = researched_child(store, child, "추가 조사")
        for source in (child, sibling, nested):
            loaded = client.get(f"{ANALYSIS}/{source['report_id']}/response-workspace").json()
            assert loaded["analysis_report_id"] == source["report_id"]
            assert loaded["workspace_analysis_report_id"] == original["report_id"]
            assert loaded["plans"] == saved["plans"] and loaded["version"] == 1
        revised = [editable(item) for item in saved["plans"]]
        revised[0]["feedback"] = "추가 조사에서 저장한 의견"
        save(client, nested["report_id"], 1, revised)
        for source in (original, child, sibling):
            loaded = client.get(f"{ANALYSIS}/{source['report_id']}/response-workspace").json()
            assert loaded["version"] == 2 and loaded["plans"][0]["feedback"] == "추가 조사에서 저장한 의견"


def test_response_storage_tenant_binding(report_settings):
    settings = replace(report_settings, access_mode="institution", auth_issuer="https://issuer.example", auth_audience="test", auth_tenant_id="tenant-a", auth_public_key_path=Path("unused-public-key.pem"))
    response_router(settings)
    with pytest.raises(ValueError, match="database_tenant_binding_mismatch"):
        response_router(replace(settings, auth_tenant_id="tenant-b"))
    with pytest.raises(ValueError, match="database_tenant_binding_mismatch"):
        response_router(replace(settings, access_mode="local_development"))


def test_review_defaults_do_not_confirm_needs_from_age_or_selected_program(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client, age_code="20G")["report_id"]
        saved = save(client, parent, plans=[plan(kind="existing_program", program_reference={
            "program_version_id": "youth-one", "title": "청년 사업", "publisher": "기관"})])
        stored = saved["plans"][0]
        assert set(stored["policy_review"]) == {"eligibility", "support_content", "delivery", "finance_and_cost"}
        assert all(item["status"] == "unconfirmed" and item["text"] == item["evidence"] == "" for item in stored["policy_review"].values())
        assert stored["support_needs"] == []
        report = generate(client, parent, saved).json()
        assert "지원 필요 확인 기록이 아직 없습니다" in report["markdown"]
        assert "담당자가 입력한 확인 내용" in report["markdown"]


def test_reviews_survive_reload_partial_and_legacy_updates_without_deleting_records(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        saved = save(client, parent, plans=[plan(policy_review={
            "delivery": {"status": "confirmed", "text": "전화로 신청 경로 확인", "evidence": "담당기관 안내문 2026-10-25"},
            "finance_and_cost": {"text": "본인부담 확인 필요"}}, support_needs=[
                {"category_code": "social_connection", "status": "confirmed", "text": "참여 희망 확인", "evidence": "사용자 직접 상담 기록"},
                {"category_code": "housing", "text": "주거 상담 필요 여부 질문"}])])
        stored = saved["plans"][0]
        assert stored["support_needs"][0]["origin"] == "user_entered"
        assert stored["policy_review"]["delivery"]["origin"] == "user_entered"
        legacy = editable(stored)
        legacy["feedback"] = "기존 화면의 수정"
        saved = save(client, parent, 1, [legacy])
        assert saved["plans"][0]["policy_review"] == stored["policy_review"]
        assert saved["plans"][0]["support_needs"] == stored["support_needs"]
        partial = editable(saved["plans"][0])
        partial.update(policy_review={"finance_and_cost": {"evidence": "요금표 추가 확인 예정"}},
                       support_needs=[{"category_code": "housing", "evidence": "다음 상담에서 확인"}])
        revised = save(client, parent, 2, [partial])
        assert revised["plans"][0]["policy_review"]["delivery"] == stored["policy_review"]["delivery"]
        cost = revised["plans"][0]["policy_review"]["finance_and_cost"]
        assert cost["text"] == "본인부담 확인 필요" and cost["status"] == "unconfirmed"
        assert len(revised["plans"][0]["support_needs"]) == 2
        no_entries = editable(revised["plans"][0])
        no_entries.update(policy_review={}, support_needs=[])
        unchanged = save(client, parent, 3, [no_entries])
        assert unchanged["version"] == 3
    with TestClient(application(report_settings)) as client:
        loaded = client.get(f"{ANALYSIS}/{parent}/response-workspace").json()
        assert loaded["plans"][0]["policy_review"] == revised["plans"][0]["policy_review"]
        assert loaded["plans"][0]["support_needs"] == revised["plans"][0]["support_needs"]


@pytest.mark.parametrize("entry", [
    {"policy_review": {"eligibility": {"status": "confirmed"}}},
    {"policy_review": {"support_content": {"status": "confirmed", "text": "내용", "evidence": " "}}},
    {"support_needs": [{"category_code": "employment", "status": "confirmed", "evidence": "근거"}]},
])
def test_confirmation_requires_user_text_and_evidence_and_does_not_save(report_settings, entry):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        url = f"{ANALYSIS}/{parent}/response-workspace"
        response = client.put(url, json={"expected_version": 0, "plans": [plan(**entry)]})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "review_confirmation_evidence_required"
        assert client.get(url).json()["version"] == 0


@pytest.mark.parametrize("entry", [
    {"policy_review": {"eligibility": {"status": "automatically_verified"}}},
    {"policy_review": {"eligibility": {"origin": "model_confirmed"}}},
    {"policy_review": {"budget_score": {"status": "confirmed"}}},
    {"support_needs": [{"category_code": "unknown"}]},
    {"support_needs": [{"category_code": "housing", "status": "not_applicable"}]},
    {"support_needs": [{"category_code": "housing"}, {"category_code": "housing"}]},
])
def test_review_rejects_unknown_fields_inferred_status_or_duplicate_needs(report_settings, entry):
    with TestClient(application(report_settings)) as client:
        parent = base(client)["report_id"]
        response = client.put(f"{ANALYSIS}/{parent}/response-workspace", json={"expected_version": 0, "plans": [plan(**entry)]})
        assert response.status_code == 422


def test_frozen_response_report_includes_user_reviews_and_original_lifecycle_reference(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    with TestClient(application(report_settings)) as client:
        analysis = deepcopy(base(client))
        analysis["lifecycle_interpretation"] = {"guide_version": "test-guide", "guide_sha256": "1" * 64, "support_need_status": "unconfirmed",
            "source": {"title": "문헌 <script>제목</script>", "year": 2025, "pages": [39, 40, 52]},
            "groups": [{"age_label": "20대", "questions": ["이용 비용을 직접 확인했나요? <img src=x>"]}]}
        for key in ("report_id", "created_at", "storage_status", "reused"):
            analysis.pop(key, None)
        parent = store.save(analysis)["report_id"]
        saved = save(client, parent, plans=[plan(policy_review={
            "finance_and_cost": {"status": "confirmed", "text": "<script>본인부담 확인</script>", "evidence": "[안내문](javascript:bad) 확인"},
            "delivery": {"status": "not_applicable", "text": "운영기관이 정해지지 않은 초안"}}, support_needs=[
                {"category_code": "social_connection", "status": "confirmed", "text": "참여 희망 확인", "evidence": "직접 입력한 현장 상담 근거"}])])
        response = generate(client, parent, saved)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["selected_plans"][0]["policy_review"] == saved["plans"][0]["policy_review"]
        assert report["selected_plans"][0]["support_needs"] == saved["plans"][0]["support_needs"]
        assert report["analysis_snapshot"]["lifecycle_interpretation"] == analysis["lifecycle_interpretation"]
        assert "이용 비용을 직접 확인했나요" in report["markdown"] and "39·40·52쪽" in report["markdown"]
        assert "사용자 확인 기록" in report["markdown"] and "해당 없음으로 기록" in report["markdown"]
        assert "<script>" not in report["markdown"] and "<img src=x>" not in report["markdown"]
        assert "[안내문](javascript:bad)" not in report["markdown"]
        changed = editable(saved["plans"][0])
        changed["policy_review"] = {"finance_and_cost": {"status": "unconfirmed", "text": "요금 변경 재확인"}}
        revised = save(client, parent, 1, [changed])
        assert revised["plans"][0]["version"] == 2
        assert client.get(f"{REPORTS}/{report['report_id']}").json()["markdown"] == report["markdown"]
