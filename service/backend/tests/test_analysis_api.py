"""Report ① is based on verified aggregates and persisted without outside calls."""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.analysis_api import analysis_router
from app.config import Settings
from app.security import install_security
from donghaeng_agent.analysis_research import _public_payload
from test_final_model import behavior, survey_row, write_bundle


ROOT = Path(__file__).resolve().parents[3]
PATH = "/api/model/analysis-reports"


def application(settings):
    result = FastAPI()
    install_security(result, settings)
    result.include_router(analysis_router(settings))
    return result


@pytest.fixture
def report_settings(tmp_path):
    activity = write_bundle(tmp_path / "activity", {
        "behavior_evidence.jsonl": [behavior("202511"), behavior(), behavior(age="40G")],
        "historical_reference.jsonl": [],
    }, "activity")
    survey = write_bundle(tmp_path / "survey", {
        "sgg_isolation_index.jsonl": [survey_row()],
        "dong_isolation_index.jsonl": [survey_row("administrative_dong", "1168051000", "신사동")],
    }, "survey")
    return Settings(final_model_bundle=activity, survey_area_bundle=survey,
                    investigation_db_path=tmp_path / "runtime" / "investigations.sqlite3")


def request(**changes):
    return {"region_code": "11680", "month": "202512", "sex_code": "MAN", "age_code": "20G", **changes}


def test_report_persists_restarts_and_deduplicates(report_settings):
    with TestClient(application(report_settings)) as client:
        first = client.post(PATH, json=request())
        assert first.status_code == 200, first.text
        report = first.json()
        assert report["storage_status"] == "saved" and report["reused"] is False
        assert report["summary"]["priority_review"] == 1
        assert report["quality_status"] == "hold" and report["operational_alert_allowed"] is False
        assert report["context"]["i5"]["record"]["i5_reference"] == 35
        assert report["context"]["mois"]["status"] == "unavailable"
        assert "-20.00%" in report["markdown"]
        assert "## 분석 자료 상세" in report["markdown"]
        assert "외부 원인 조사는 수행하지 않았습니다" not in report["markdown"]
        assert "원자료의 품질 보류와 운영 경보 금지" not in report["markdown"]
        assert report["limitations"]  # Internal metadata survives the presentation change.
        repeated = client.post(PATH, json=request()).json()
        assert repeated["report_id"] == report["report_id"] and repeated["reused"] is True
        assert repeated["created_at"] == report["created_at"]
        assert client.get(PATH).json()["total_matched"] == 1
    with TestClient(application(report_settings)) as client:
        saved = client.get(PATH + "/" + report["report_id"]).json()
        assert saved["markdown"] == report["markdown"]
        assert saved["provenance"] == report["provenance"]
        assert client.get(PATH + "/missing").status_code == 404
    assert not report_settings.investigation_db_path.exists()
    assert report_settings.investigation_db_path.with_name("analysis_reports.sqlite3").exists()


def test_missing_rows_are_held_and_not_zero(report_settings):
    with TestClient(application(report_settings)) as client:
        report = client.post(PATH, json=request(region_code="51110", sex_code=None, age_code=None)).json()
        assert report["summary"]["total"] == report["summary"]["assessment_hold"] == 10
        assert report["context"]["i5"]["record"] is None
        for item in report["assessments"]:
            assert item["sources"]["skt"]["change_percent"] is None
            assert item["sources"]["card_a"]["threshold_met"] is None
        assert "자료 없음" in report["markdown"]


def test_dong_report_preserves_its_i5_and_keeps_activity_at_parent_sgg(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = client.post(PATH, json=request()).json()
        response = client.post(PATH, json=request(dong_code="1168051000"))
        assert response.status_code == 200, response.text
        dong = response.json()
        assert dong["report_id"] != parent["report_id"]
        assert dong["scope"]["dong_code"] == "1168051000"
        assert dong["scope"]["region_code"] == "11680"
        assert dong["region_name"] == "강남구 신사동"
        assert dong["context"]["i5"]["record"]["area_code"] == "1168051000"
        assert dong["context"]["i5"]["record"]["spatial_unit"] == "administrative_dong"
        assert dong["assessments"] == parent["assessments"]
        assert dong["context"]["activity_scope"] == {"spatial_unit": "sgg", "region_code": "11680",
            "region_name": "강남구", "role": "parent_sgg_reference", "is_dong_observation": False}
        assert "강남구 신사동 (1168051000)" in dong["markdown"]
        assert "## 상위 시군구 활동 변화 참고" in dong["markdown"]
        assert "활동 집계 지역: 강남구 (11680)" in dong["markdown"]
        repeated = client.post(PATH, json=request(dong_code="1168051000")).json()
        assert repeated["report_id"] == dong["report_id"] and repeated["reused"] is True
        exact = client.get(PATH + "?region_code=11680&dong_code=1168051000").json()
        assert exact["total_matched"] == 1
        assert exact["reports"][0]["region_name"] == "강남구 신사동"
        assert client.get(PATH + "?dong_code=1168052000").json()["total_matched"] == 0
    with TestClient(application(report_settings)) as client:
        saved = client.get(PATH + "/" + dong["report_id"]).json()
        assert saved["scope"]["dong_code"] == "1168051000"
        assert saved["markdown"] == dong["markdown"]


@pytest.mark.parametrize("dong_code", ["11680", 1168051000, "5111051000", "1168099999"])
def test_dong_report_rejects_invalid_unknown_or_wrong_parent_scope(report_settings, dong_code):
    with TestClient(application(report_settings)) as client:
        assert client.post(PATH, json=request(dong_code=dong_code)).status_code == 422
        assert client.get(PATH).json()["total_matched"] == 0


def test_dong_report_list_rejects_parent_mismatch(report_settings):
    with TestClient(application(report_settings)) as client:
        assert client.get(PATH + "?region_code=11680&dong_code=5111051000").status_code == 422


def test_dong_without_parent_activity_saves_real_i5_partial_report_without_assessments(report_settings, monkeypatch):
    import app.analysis_api as api
    def unexpected_research(*args, **kwargs):
        raise AssertionError("A report without activity must not call research")
    monkeypatch.setattr(api, "research_analysis_report", unexpected_research)
    dong = {**survey_row("administrative_dong", "1111051500", "청운효자동"),
            "region_code": "11110", "region_name": "종로구", "i5_reference": 37.2, "isolation_index": 37.2}
    parent = {**survey_row("sgg", "11110", "종로구"), "region_code": "11110", "region_name": "종로구"}
    write_bundle(report_settings.survey_area_bundle, {"sgg_isolation_index.jsonl": [parent],
        "dong_isolation_index.jsonl": [dong]}, "survey")
    with TestClient(application(report_settings)) as client:
        response = client.post(PATH, json=request(region_code="11110", dong_code="1111051500", sex_code=None, age_code=None))
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["storage_status"] == "saved" and report["report_status"] == "partial"
        assert report["region_name"] == "종로구 청운효자동"
        assert report["context"]["i5"]["record"]["i5_reference"] == 37.2
        assert report["context"]["i5"]["record"]["area_code"] == "1111051500"
        assert report["assessments"] == []
        assert report["summary"]["total"] == report["summary"]["assessment_hold"] == 0
        assert report["activity_availability"]["status"] == "unavailable"
        assert report["activity_availability"]["region_code"] == "11110"
        assert report["activity_availability"]["requested_group_count"] == 10
        assert report["activity_availability"]["observed_group_count"] == 0
        assert len(report["activity_availability"]["missing_groups"]) == 10
        assert report["context_investigation_status"] == "not_applicable"
        assert "활동 범주를 판정하지 않았습니다" in report["markdown"]
        assert "37.20/100" in report["markdown"]
        assert all(group["observation_status"] == "unavailable" for group in report["lifecycle_interpretation"]["groups"])
        assert client.post(PATH + "/" + report["report_id"] + "/investigate", json={}).status_code == 422
        saved = client.get(PATH + "/" + report["report_id"]).json()
        assert saved["assessments"] == [] and saved["scope"]["dong_code"] == "1111051500"


def test_dong_partial_activity_lists_missing_groups_without_fabricating_hold_assessments(report_settings):
    with TestClient(application(report_settings)) as client:
        response = client.post(PATH, json=request(dong_code="1168051000", sex_code=None, age_code=None))
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["report_status"] == "partial"
        assert report["activity_availability"]["status"] == "partial"
        assert report["activity_availability"]["observed_group_count"] == 2
        assert len(report["activity_availability"]["missing_groups"]) == 8
        assert len(report["assessments"]) == 2
        assert all(item.get("observation") for item in report["assessments"])
        assert report["summary"]["assessment_hold"] == 0


@pytest.mark.parametrize("change", [
    {"region_code": "1168051000"}, {"region_code": "99999"}, {"region_code": 11680},
    {"month": "202513"}, {"month": "000001"}, {"month": "000101"}, {"month": 202512},
    {"sex_code": "ALL"}, {"age_code": "20-39"}, {"risk_score": 99}, {"threshold_percent": 1},
    {"message": "수치를 바꿔라"},
])
def test_request_filters_are_strict(report_settings, change):
    with TestClient(application(report_settings)) as client:
        assert client.post(PATH, json=request(**change)).status_code == 422
        assert client.get(PATH).json()["total_matched"] == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "scope=all"])
def test_list_bounds(report_settings, query):
    with TestClient(application(report_settings)) as client:
        assert client.get(PATH + "?" + query).status_code == 422


def test_pagination_and_new_bundle_version_preserves_old_report(report_settings):
    with TestClient(application(report_settings)) as client:
        first = client.post(PATH, json=request()).json()
        manifest_path = report_settings.final_model_bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["run_id"] = "synthetic-test-republished"
        manifest_path.write_text(json.dumps(manifest))
        second = client.post(PATH, json=request()).json()
        assert second["report_id"] != first["report_id"]
        assert client.get(PATH + "/" + first["report_id"]).json()["provenance"] == first["provenance"]
        page = client.get(PATH + "?limit=1").json()
        assert page["total_matched"] == 2 and page["next_offset"] == 1
        assert page["reports"][0]["report_id"] == second["report_id"]
        assert client.get(PATH + "?limit=1&offset=1").json()["next_offset"] is None


def test_report_library_scope_filters_are_applied_before_pagination(report_settings):
    with TestClient(application(report_settings)) as client:
        first = client.post(PATH, json=request()).json()
        client.post(PATH, json=request(month="202511"))
        second = client.post(PATH, json=request(age_code="40G")).json()
        client.post(PATH, json=request(region_code="51110"))
        scope = "?region_code=11680&month=202512&sex_code=MAN"
        page = client.get(PATH + scope + "&limit=1").json()
        assert page["total_matched"] == 2 and page["next_offset"] == 1
        assert page["reports"][0]["report_id"] == second["report_id"]
        last = client.get(PATH + scope + "&limit=1&offset=1").json()
        assert last["reports"][0]["report_id"] == first["report_id"]
        assert last["next_offset"] is None
        exact = client.get(PATH + scope + "&age_code=20G").json()
        assert exact["total_matched"] == 1 and exact["reports"][0]["report_id"] == first["report_id"]
        assert client.get(PATH + "?region_code=11680&month=202512&sex_code=WMAN").json()["total_matched"] == 0


@pytest.mark.parametrize("query", ["region_code=99999", "region_code=1168051000", "month=202513", "month=000101", "sex_code=ALL", "age_code=70G"])
def test_report_library_rejects_invalid_scope_filters(report_settings, query):
    with TestClient(application(report_settings)) as client:
        assert client.get(PATH + "?" + query).status_code == 422


@pytest.mark.parametrize("family", ["activity", "survey"])
def test_corrupt_sources_reject_even_when_report_already_saved(report_settings, family):
    with TestClient(application(report_settings)) as client:
        first = client.post(PATH, json=request()).json()
        root = report_settings.final_model_bundle if family == "activity" else report_settings.survey_area_bundle
        source = root / ("behavior_evidence.jsonl" if family == "activity" else "sgg_isolation_index.jsonl")
        source.write_text(source.read_text() + "\n")
        result = client.post(PATH, json=request())
        assert result.status_code == 503 and result.json()["detail"]["code"] == "bundle_invalid"
        assert str(root) not in result.text
        assert client.get(PATH).json()["total_matched"] == 1
        assert client.get(PATH + "/" + first["report_id"]).json()["report_id"] == first["report_id"]


def test_untracked_mois_never_enters_report(report_settings):
    path = report_settings.final_model_bundle / "survey_mois_reference" / "region_month_summary.csv"
    path.parent.mkdir()
    path.write_text("region_code,month,population_total\n11680,202512,1\n")
    with TestClient(application(report_settings)) as client:
        assert client.post(PATH, json=request()).status_code == 503
        assert client.get(PATH).json()["total_matched"] == 0


def test_bundle_change_between_reads_is_rejected(report_settings, monkeypatch):
    import app.analysis_api as api
    original = api._survey_input
    def changed(model, scope):
        result = original(model, scope)
        manifest_path = report_settings.final_model_bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["run_id"] = "changed-mid-read"
        manifest_path.write_text(json.dumps(manifest))
        return result
    monkeypatch.setattr(api, "_survey_input", changed)
    with TestClient(application(report_settings)) as client:
        response = client.post(PATH, json=request())
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "bundle_changed_during_query"
        assert client.get(PATH).json()["total_matched"] == 0


@pytest.mark.parametrize("field,value", [
    ("month", "2025-11"), ("region", "춘천시"), ("sex", "여성"), ("age", "30"),
    ("industry", "음식점"), ("time_band", "오전"), ("residence_sido", "서울"),
])
def test_exact_c3_id_must_also_match_scope(report_settings, field, value):
    source_root = ROOT / "bundles/activity"
    rows = [json.loads(line) for line in (source_root / "behavior_evidence.jsonl").read_text().splitlines()]
    row = next(row for row in rows if row["historical_reference_id"] and row["key"]["region_code"] == "11680")
    histories = [json.loads(line) for line in (source_root / "historical_reference.jsonl").read_text().splitlines()]
    reference = next(item for item in histories if item["record_id"] == row["historical_reference_id"])
    reference[field] = value
    write_bundle(report_settings.final_model_bundle, {"behavior_evidence.jsonl": [row],
                 "historical_reference.jsonl": [reference]}, "activity")
    with TestClient(application(report_settings)) as client:
        result = client.post(PATH, json=request())
        assert result.status_code == 503 and result.json()["detail"]["code"] == "bundle_invalid"
        assert client.get(PATH).json()["total_matched"] == 0


def test_tracked_but_invalid_mois_value_is_rejected(report_settings):
    root = report_settings.final_model_bundle
    relative = "survey_mois_reference/region_month_summary.csv"
    path = root / relative
    path.parent.mkdir()
    path.write_text("region_code,month,population_total,population_20plus,one_person_household_share\n11680,202512,100,80,NaN\n")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["outputs"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest))
    with TestClient(application(report_settings)) as client:
        assert client.post(PATH, json=request()).status_code == 503
        assert client.get(PATH).json()["total_matched"] == 0


def test_real_bundle_all_100_observations_and_c3_context(tmp_path):
    settings = Settings(final_model_bundle=ROOT / "bundles/activity",
                        survey_area_bundle=ROOT / "model/data/i5/national_2025",
                        investigation_db_path=tmp_path / "investigations.sqlite3")
    manifest = settings.final_model_bundle / "manifest.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    counts, priorities = Counter(), []
    with TestClient(application(settings)) as client:
        for region in ("11680", "51110"):
            for month in ("202508", "202509", "202510", "202511", "202512"):
                result = client.post(PATH, json={"region_code": region, "month": month})
                assert result.status_code == 200, result.text
                report = result.json()
                counts.update(item["category"] for item in report["assessments"])
                priorities.extend(item for item in report["assessments"] if item["category"] == "priority_review")
                assert report["context"]["i5"]["status"] == report["context"]["mois"]["status"] == "available"
                assert "manifest SHA-256" in report["markdown"]
                assert report["context_investigation_status"] == "not_performed"
                guide = report["lifecycle_interpretation"]
                assert len(guide["groups"]) == 10
                assert guide["support_need_status"] == "unconfirmed"
                assert guide["source"]["regional_confirmation"] is False
                assert guide["changes_activity_classification"] is False
                payload = _public_payload(report)
                assert payload["observations"] == report["assessments"]
                assert payload["lifecycle_interpretation"] == guide
                assert all(group["observation_status"] == "available" for group in guide["groups"])
                if region == "51110" and month == "202510":
                    assert "-17.41%" in report["markdown"] and "-5.21%" in report["markdown"]
                    assert "+10.71%" in report["markdown"]
                    assert "1인 세대" in report["markdown"] and "전체 세대" in report["markdown"]
                    assert "기준기간 2025-07-01~2025-09-30" in report["markdown"]
                    assert "원본 10% 감소 기준" in report["markdown"] and "같은 요일" in report["markdown"]
                    assert "SKT_SEP_OCT_BOUNDARY_VERSION_CHANGED" in report["markdown"]
                    assert "2개월 연속 5% 동시 감소: 해당 없음; 상대적 이례성: 확인됨" in report["markdown"]
                    assert "통신 z 8.80, 카드 z 0.91, 모델 기준 3.50" in report["markdown"]
                    assert "통신 +21.72%, 카드 +16.09%" in report["markdown"]
                    item = next(item for item in report["assessments"] if item["scope"]["sex_code"] == "MAN" and item["scope"]["age_code"] == "20G")
                    reference = item["historical_reference"]
                    assert reference["independent_evidence"] is False
                    assert reference["record"]["record_id"].startswith("C3:")
                    assert reference["record"]["count"]["change_pct"] > 0
        assert client.get(PATH).json()["total_matched"] == 10
    assert counts == {"no_threshold_decline": 70, "single_source_decline": 19, "joint_decline": 10, "priority_review": 1}
    assert len(priorities) == 1
    assert priorities[0]["scope"] == {"region_code": "51110", "month": "202510", "sex_code": "MAN", "age_code": "20G"}
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before


def test_i5_display_in_report_ai_input_and_export(report_settings):
    from app.report_export_api import report_export_router
    app = application(report_settings)
    app.include_router(report_export_router(report_settings))
    with TestClient(app) as client:
        report = client.post(PATH, json=request()).json()
        i5 = report['context']['i5']['record']
        assert i5['i5_reference'] == 35
        assert i5['i5_display_score'] == pytest.approx(4.733643178906618)
        assert '4.73/10' in report['markdown']
        assert 'I5: 35.00/100' not in report['markdown']
        assert '0~100' in report['markdown']
        public = _public_payload(report)
        assert public['context']['i5']['record']['i5_display_score'] == i5['i5_display_score']
        exported = client.get(PATH + '/' + report['report_id'] + '/export?format=md')
        assert exported.status_code == 200
        assert '4.73/10' in exported.text


def test_old_report_display_is_projected_without_database_rewrite(report_settings):
    from donghaeng_agent.analysis_report_store import AnalysisReportStore, encoded
    from donghaeng_agent.analysis_reports import render_analysis_report
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name('analysis_reports.sqlite3'))
    with TestClient(application(report_settings)) as client:
        report = client.post(PATH, json=request()).json()
        raw = report['context']['i5']['record']
        for key in ('i5_z', 'i5_display_score', 'i5_display_clipped', 'i5_display_reference_version'):
            raw.pop(key, None)
        report['markdown'] = render_analysis_report(report)
        report['markdown'] = __import__('re').sub(r'^I5 표시점수: .*$', 'I5: 35.00/100 · 지역 전체 인구구성 기준', report['markdown'], flags=__import__('re').MULTILINE)
        original = encoded(report)
        with store.connection(write=True) as db:
            db.execute('UPDATE analysis_reports SET data=? WHERE report_id=?', (original, report['report_id']))
        loaded = client.get(PATH + '/' + report['report_id']).json()
        assert loaded['context']['i5']['record']['i5_display_score'] == pytest.approx(4.733643178906618)
        assert '4.73/10' in loaded['markdown']
        assert loaded['provenance'] == report['provenance']
        with store.connection() as db:
            assert db.execute('SELECT data FROM analysis_reports WHERE report_id=?', (report['report_id'],)).fetchone()[0] == original
