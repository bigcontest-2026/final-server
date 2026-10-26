"""Read-only exports of frozen analysis and response reports."""
import logging
import re
import sqlite3
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response

from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.document_exports import export_report, PdfUnsupportedGlyph
from donghaeng_agent.response_workspace import ResponseWorkspaceStore
from .security import _bind_database

logger = logging.getLogger(__name__)


def report_export_router(settings):
    router = APIRouter()
    analysis = AnalysisReportStore(settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    responses = ResponseWorkspaceStore(settings.investigation_db_path.with_name("response_workspaces.sqlite3"))
    with analysis.connection(write=True) as db:
        _bind_database(db, settings, ("analysis_reports", "analysis_report_research", "analysis_report_summaries"))
    with responses.connection(write=True) as db:
        _bind_database(db, settings, ("response_workspaces", "response_plan_revisions", "response_reports", "response_workspace_members"))

    def download(store, report_id, request, format):
        if set(request.query_params) - {"format"} or len(request.query_params.getlist("format")) > 1:
            raise HTTPException(422, detail={"code": "invalid_export_query", "message": "다운로드 형식을 하나만 선택해 주세요."})
        try:
            report = store.get_report(report_id) if isinstance(store, ResponseWorkspaceStore) else store.get(report_id)
        except (sqlite3.Error, OSError):
            raise HTTPException(503, detail={"code": "report_storage_unavailable", "message": "저장된 보고서를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."}) from None
        if report is None:
            raise HTTPException(404, detail={"code": "report_not_found", "message": "저장된 보고서를 찾을 수 없습니다."})
        try:
            data, mime = export_report(report, format)
        except PdfUnsupportedGlyph:
            raise HTTPException(422, detail={"code": "pdf_unsupported_glyph", "message": "PDF 글꼴이 지원하지 않는 문자가 있습니다. 한글(HWPX) 또는 Markdown으로 내려받거나 특수문자를 수정해 주세요."}) from None
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            logger.exception("Snapshot export failed for format %s", format)
            raise HTTPException(503, detail={"code": "report_export_unavailable", "message": "파일을 만들지 못했습니다. 잠시 후 다시 시도하거나 다른 형식으로 내려받아 주세요."}) from None
        title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", str(report.get("title") or "HERMES_보고서"))[:120].strip(" .") or "HERMES_보고서"
        filename = f"{title}.{format}"
        return Response(content=data, media_type=mime, headers={
            "Content-Disposition": f'attachment; filename="HERMES_report.{format}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })

    @router.get("/api/model/response-reports/{report_id}/export")
    def response_export(report_id: str, request: Request, format: Literal["md", "pdf", "hwpx"] = Query(default="pdf")):
        return download(responses, report_id, request, format)

    @router.get("/api/model/analysis-reports/{report_id}/export")
    def analysis_export(report_id: str, request: Request, format: Literal["md", "pdf", "hwpx"] = Query(default="pdf")):
        return download(analysis, report_id, request, format)

    return router
