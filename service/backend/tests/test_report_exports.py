"""Exports preserve saved content and expose real document formats."""
from io import BytesIO
from zipfile import ZipFile, ZIP_STORED
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.report_export_api import report_export_router
from app.security import install_security
from donghaeng_agent.document_exports import export_report, report_document
from test_analysis_api import report_settings
from test_response_api import application as response_application, base, save, generate, plan


def sample():
    return {"report_id": "response_sample", "title": "춘천시 복지 대응계획(안)", "markdown": "# 저장된 원문\n\n## 요약\n지원 필요 미확인. 0원으로 바꾸지 않습니다.\n",
            "document": {"version": "welfare-plan-2.0", "title": "춘천시 복지 대응계획(안)", "subtitle": "담당자 검토안",
                         "metadata": [{"label": "기준", "value": "2025년 10월"}],
                         "sections": [{"id": "summary", "title": "핵심 요약", "paragraphs": ["활동 감소는 관측 결과이며 고립 위험 증가 여부는 미확인입니다.", "가나다라마바사" * 400, "https://example.com/" + "long-url-" * 80]}]}}


def test_markdown_is_exact_saved_snapshot_and_legacy_is_not_rewritten():
    report = sample()
    data, mime = export_report(report, "md")
    assert data.decode("utf-8") == report["markdown"]
    assert mime.startswith("text/markdown")
    legacy = {"title": "과거 저장본", "markdown": "# 과거 저장본\n\n## 결과\n미확인\n"}
    doc = report_document(legacy)
    assert doc["sections"][0]["paragraphs"] == ["미확인"]
    assert "고립" not in str(doc)


def test_pdf_embeds_font_and_unicode_and_has_multiple_pages():
    data, mime = export_report(sample(), "pdf")
    assert mime == "application/pdf" and data.startswith(b"%PDF-")
    assert b"/FontFile2" in data and b"/ToUnicode" in data
    assert data.count(b"/Type /Page ") >= 2
    assert data.endswith(b"%%EOF\n")


def test_hwpx_is_editable_xml_package_with_resolved_references():
    data, mime = export_report(sample(), "hwpx")
    assert mime == "application/hwp+zip"
    with ZipFile(BytesIO(data)) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.infolist()[0].compress_type == ZIP_STORED
        assert archive.read("mimetype") == b"application/hwp+zip"
        names = set(archive.namelist())
        for name in names:
            if name.endswith((".xml", ".hpf", ".rdf")):
                root = ET.fromstring(archive.read(name))
                for item in root.iter():
                    if "href" in item.attrib:
                        assert item.attrib["href"] in names
        section = ET.fromstring(archive.read("Contents/section0.xml"))
        text = "".join(section.itertext())
        assert sample()["document"]["title"] in text
        for line in sample()["document"]["sections"][0]["paragraphs"]:
            assert line in text
        assert "고립 위험 증가 여부는 미확인" in text
        assert not any(n.startswith("Scripts/") for n in names)


def test_hwpx_default_styles_and_section_references_are_resolved():
    data, _ = export_report(sample(), "hwpx")
    ns = {"hh": "http://www.hancom.co.kr/hwpml/2011/head", "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph"}
    with ZipFile(BytesIO(data)) as archive:
        header = ET.fromstring(archive.read("Contents/header.xml"))
        section = ET.fromstring(archive.read("Contents/section0.xml"))
        ids = lambda path: {x.attrib["id"] for x in header.findall(path, ns)}
        chars = ids(".//hh:charPr")
        paras = ids(".//hh:paraPr")
        styles = ids(".//hh:style")
        borders = ids(".//hh:borderFill")
        numberings = ids(".//hh:numbering")
        for item in section.iter():
            for key, allowed in [("charPrIDRef", chars), ("paraPrIDRef", paras), ("styleIDRef", styles),
                                 ("borderFillIDRef", borders), ("outlineShapeIDRef", numberings)]:
                if key in item.attrib:
                    assert item.attrib[key] in allowed, (key, item.attrib[key])
        props = section.find(".//hp:secPr", ns)
        assert props.find("hp:footNotePr", ns) is not None
        assert props.find("hp:endNotePr", ns) is not None
        assert len(props.findall("hp:pageBorderFill", ns)) == 3
        page = props.find("hp:pagePr", ns)
        assert page.attrib["width"] == "59528" and page.attrib["height"] == "84186"
        assert header.find("hh:trackchageConfig", ns) is not None


def test_pdf_does_not_silently_render_unsupported_emoji_as_missing_glyph():
    report = sample()
    report["document"]["sections"][0]["paragraphs"].append("사용자 입력 특수문자 \U0001f600")
    with pytest.raises(ValueError, match="pdf_unsupported_glyph"):
        export_report(report, "pdf")
    assert export_report(report, "hwpx")[0].startswith(b"PK")


def test_export_endpoints_validate_format_access_and_saved_report(report_settings):
    app = response_application(report_settings)
    app.include_router(report_export_router(report_settings))
    with TestClient(app) as client:
        analysis = base(client)
        workspace = save(client, analysis["report_id"])
        response = generate(client, analysis["report_id"], workspace).json()
        for kind, report in [("analysis", analysis), ("response", response)]:
            url = f"/api/model/{kind}-reports/{report['report_id']}/export"
            for extension in ("md", "pdf", "hwpx"):
                result = client.get(url, params={"format": extension})
                assert result.status_code == 200, result.text[:200]
                assert "filename*=UTF-8''" in result.headers["content-disposition"]
                assert result.headers["cache-control"] == "private, no-store"
                if extension == "md":
                    assert result.text == report["markdown"]
            assert client.get(url, params={"format": "exe"}).status_code == 422
            assert client.get(url + "?format=pdf&format=hwpx").status_code == 422
            assert client.get(url + "?format=pdf&unexpected=yes").status_code == 422
            assert client.get(url, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert client.get('/api/model/response-reports/missing/export?format=pdf').status_code == 404


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="unsupported_export_format"):
        export_report(sample(), "hwp")


def test_legacy_authored_literal_markers_survive_plain_conversion():
    document = report_document({'title': '저장본', 'markdown': '# 저장본\n\n## 내용\n담당자 입력: \\*\\*별표\\*\\* \\`코드\\`\n'})
    assert '**별표** `코드`' in document['sections'][0]['paragraphs'][0]


def test_pdf_unsupported_character_has_actionable_error_other_formats_work(report_settings):
    app = response_application(report_settings)
    app.include_router(report_export_router(report_settings))
    with TestClient(app) as client:
        analysis = base(client)
        workspace = save(client, analysis["report_id"], plans=[plan(title="담당자 입력 \U0001f600")])
        report = generate(client, analysis["report_id"], workspace).json()
        url = f"/api/model/response-reports/{report['report_id']}/export"
        pdf = client.get(url + "?format=pdf")
        assert pdf.status_code == 422
        assert pdf.json()["detail"]["code"] == "pdf_unsupported_glyph"
        assert "HWPX" in pdf.json()["detail"]["message"]
        for extension in ("md", "hwpx"):
            assert client.get(url, params={"format": extension}).status_code == 200
