"""Build factual support cards from locally acquired official captures.

Run from project root after acquisition. Full HTML/text evidence is local only;
the manifest exposes facts, source URLs and minimal excerpts, not full originals.
"""
import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RAW = Path(__file__).resolve().parent / "official_raw"
DEST = ROOT / "service/shared/support"
DEST.mkdir(parents=True, exist_ok=True)
EVIDENCE = ROOT / "service/backend/evidence/support"
(EVIDENCE / "raw").mkdir(parents=True, exist_ok=True)
(EVIDENCE / "text").mkdir(exist_ok=True)

def condition(cid, label, kind, operator, value, quote):
    return dict(id=cid, label=label, kind=kind, operator=operator,
                value=value, source_quote=quote)

def card(cid, name, title, regions, amin, amax, summary, conditions,
         period=None, institution=None, contact=None, unknowns=None,
         limitations=None, rights=None):
    meta = json.loads((RAW / (name + ".meta.json")).read_text())
    origin = RAW / (name + ".html")
    sha = hashlib.sha256(origin.read_bytes()).hexdigest()
    assert sha == meta["sha256"]
    shutil.copyfile(origin, EVIDENCE / "raw" / (cid + ".html"))
    # Extracted source text stays local alongside the unchanged captured bytes.
    shutil.copyfile(RAW / (name + ".txt"), EVIDENCE / "text" / (cid + ".txt"))
    return dict(id=cid, title=title, institution=institution,
                url=meta["url"], retrieved_at=meta["retrieved_at"],
                published_at=None, source_kind="official_capture",
                capture_path="backend/evidence/support/raw/" + cid + ".html", sha256=sha,
                content_text_path="backend/evidence/support/text/" + cid + ".txt",
                content_text_sha256=hashlib.sha256((RAW / (name + ".txt")).read_bytes()).hexdigest(),
                conditions=conditions, regions=regions, age_min=amin, age_max=amax,
                period=period or dict(start=None, end=None, status="unknown"),
                contact=contact, summary=summary, rights_note=rights,
                unknown_fields=unknowns or [], limitations=limitations or [],
                external_model_payload="facts_and_short_excerpt_only",
                full_capture_publication="pending_rights_review")

seoul_rights = ("서울시 공개 페이지의 개별 공공누리 표시를 확인하지 못함. "
                "저작권 정책 https://www.seoul.go.kr/helper/copyright.do 에서는 "
                "공공누리 미부착 자료 이용의 사전 협의를 안내한다. "
                "원문 전체는 로컬 검증용이며 공개 패키지·외부 모델에는 사실 조건, URL, 짧은 인용만 전달. "
                "원문 재배포/학습 권리 확정 아님.")

sources = [
    card("seoul-youth-isolation", "seoul_youth_full", "서울 고립·은둔청년 지원", ["서울"], 19, 39,
         "서울 거주 청년 및 가족을 위한 심리지원, 관계 회복, 생활습관, 진로 프로그램. 개인 상태와 기간 확인을 거쳐 기관이 판단한다.", [
         condition("seoul-residence", "서울 거주 여부", "residence", "equals", "서울", "서울 거주"),
         condition("age", "일반 연령 범위", "age", "between", [19, 39], "만19~39세"),
         condition("birth-date", "기준 출생일 범위 확인", "other", "requires_evidence", ["1986-01-01", "2007-12-31"], "1986년 1월 1일 ~ 2007년 12월 31일"),
         condition("veteran-exception", "의무복무 제대군인 최대 3년 연장과 복무기간별 공지 확인", "other", "requires_evidence", {"maximum_extension_years": 3}, "최대 3년 연장"),
         condition("individual-state", "고립 기준 또는 은둔 기준의 개인 상태 확인", "other", "requires_evidence", {"isolation": "정서적 또는 물리적 고립 중 해당 기준", "seclusion": "집 중심 생활과 경제활동·구직·학업 조건"}, ""),
         condition("duration", "해당 고립 또는 은둔 상태가 최소 6개월 지속했는지 확인", "other", "requires_evidence", {"minimum_months": 6}, "최소 6개월 이상"),
         condition("application-current", "로그인 이후 실제 접수 가능 여부 확인", "application_status", "requires_evidence", None, "")],
         institution="서울특별시 / 서울청년기지개센터", contact="02-6953-2520",
         unknowns=["개인 정확 출생일", "거주 여부", "고립·은둔 개인 기준", "지속기간", "제대군인 예외", "현재 접수 수용 여부", "소득·가구 조건 유무", "원문 최초 게시일"],
         limitations=["지역 집계 근거만으로 개인 자격을 판정할 수 없음", "나이 문구와 출생일 범위를 함께 보존했으며 조회 시점의 만 나이로 임의 재해석하지 않음", "신청 화면은 로그인 필요로 상세 접수 상태 미검증", "기존 단축 URL은 이동 화면을 반환했으며 공식 메뉴의 전체 파라미터 경로에서 상세 취득 성공"], rights=seoul_rights),
    card("seoul-isolated-household", "seoul_isolated_household", "서울 고립은둔가구 지원 및 서울연결처방", ["서울"], None, None,
         "서울의 고립가구를 생활권 복지관과 공공 창구로 연결하고, 상담을 거쳐 서비스 및 커뮤니티를 연계한다. 사업별 개별 모집 시기는 다르다.", [
         condition("seoul-residence", "서울 시민·거주지 확인", "residence", "equals", "서울", "서울 시민"),
         condition("individual-state", "외로움·고립·은둔 상태와 욕구를 기관 상담으로 확인", "other", "requires_evidence", None, "외로움·고립·은둔"),
         condition("program-period", "제공단체별 모집·운영 시기 확인", "application_period", "requires_evidence", None, "모집시기, 운영시기 상이"),
         condition("capacity", "개별 기관·프로그램 접수 및 수용 여부 확인", "application_status", "requires_evidence", None, "")],
         institution="서울특별시 고독대응과 / 서울시복지재단 고립예방센터", contact="거주지 구청·동주민센터·종합사회복지관, 120(+5), 02-2133-7180 / 02-6353-0333",
         unknowns=["개인 상태", "개별 기관·프로그램 모집기간", "수용 가능 인원", "연령 제한 유무", "소득·가구 조건 유무", "원문 최초 게시일"],
         limitations=["수정일 2026-09-28, 2026년 25개 자치구 복지관 69개소 안내", "기관 수는 실제 참여 가능 인원이나 연결 실적이 아님", "일반 고립가구 지원과 청년 지원을 구분"],
         rights="공공누리 제4유형 표시 확인: 출처표시·상업적 이용금지·변경금지. 원문 캡처는 변경 없이 로컬 증빙 보존, 공개 패키지는 사실 조건·링크·해시·짧은 인용으로 제한. 전체 재배포/변경/외부 모델 학습은 별도 권리 검토 필요."),
    card("chuncheon-mental-health", "chuncheon_mhc", "춘천시정신건강복지센터 정신건강상담", ["춘천"], None, None,
         "정신건강의 어려움으로 도움을 희망하는 춘천시민 또는 진단을 받은 당사자·가족의 무료 상담과 지역자원 안내. 고립 전용 프로그램이나 고립 진단은 아니다.", [
         condition("chuncheon-residence", "춘천 시민 여부 확인", "residence", "equals", "춘천", "춘천시민 누구나"),
         condition("need", "정신건강 어려움 등 도움 요청 또는 진단·가족 상태 확인", "other", "requires_evidence", None, ""),
         condition("appointment", "전화·내소·홈페이지로 상담 접수 및 실제 예약 확인", "application_status", "requires_evidence", None, "상담 접수")],
         institution="춘천시정신건강복지센터", contact="정신건강상담 033-242-7574, jungsinbo@chmhc.org; 월~금 09:00~18:00(점심12:00~13:00)",
         unknowns=["개인 거주·상담 욕구", "실제 예약 수용", "연령 제한 유무", "소득·가구 조건 유무", "게시·수정일"],
         limitations=["센터 운영기관의 공개 상세 원문", "우울·정신건강 어려움을 고립 라벨로 대체하지 않음", "상담 무료는 확인됐으나 치료비지원은 별도 자격·센터 문의 사항"],
         rights="기관 누리집 All Rights Reserved 표시, 재배포 라이선스 미확인. 원문 전체는 로컬 검증용, 외부에는 공개 사실 조건·URL·짧은 인용만. 원문 전체 공개·학습 권리 확정 아님."),
    card("seoul-family-training-ended", "seoul_family_closed", "2026 서울 고립은둔청년 지킴이 양성교육(공지 접수 종료)", ["서울"], None, None,
         "고립·은둔 생활에 어려움을 겪는 서울 거주 19~39세 자녀의 부모·가족 교육. 게시된 전 과정 모집기간이 조회 시점에 지나 후보를 제외한다.", [
         condition("child-residence", "자녀의 서울 거주 확인", "other", "requires_evidence", "서울", "서울에 거주하는"),
         condition("child-age", "자녀 연령 및 고립·은둔 생활 확인", "other", "requires_evidence", [19, 39], "19~39세 자녀"),
         condition("family-role", "참여자가 부모 또는 가족인지 확인", "household", "requires_evidence", "부모 또는 가족", "부모님 혹은 가족"),
         condition("period-ended", "게시된 마지막 심화 모집도 종료", "application_period", "date_range", ["2026-05-11", "2026-09-25"], "9월 25일")],
         period=dict(start="2026-05-11", end="2026-09-25", status="closed"),
         institution="서울특별시 미래청년기획관", contact="운영사무국 031-246-2362",
         unknowns=["후속 모집·추가 접수 여부", "자녀 정확 연령·거주·상태", "부모·가족 관계"],
         limitations=["상단 신청기간은 5월11일~6월3일이나 본문에 이후 모집이 추가됨: 모두 보존", "본문 일부 날짜와 요일 표기가 달라 날짜 원문은 유지하고 기관 확인 필요로 표시", "운영기간과 모집기간을 구분: 이후 강의가 진행돼도 신규 접수 가능으로 판단하지 않음"], rights=seoul_rights),
]

catalog_path = RAW / "welfare_catalog_20260819.csv"
rows = list(csv.DictReader(catalog_path.read_text(encoding="utf-8-sig").splitlines()))
catalog = [dict(service_id=r["서비스아이디"], title=r["서비스명"], url=r["서비스URL"],
                summary=r["서비스요약"], related_site=r["사이트"] or None,
                contact=r["대표문의"] or None, ministry=r["소관부처명"],
                department=r["소관조직명"], basis_year=r["기준연도"],
                modified_at=r["최종수정일"], detail_verified=False,
                source_kind="official_catalog", eligibility_status="unknown") for r in rows]
catalog_meta=json.loads((RAW / "welfare_catalog_20260819.meta.json").read_text())
city_capture=RAW / "chuncheon_city_mental_health.html"
if city_capture.exists():
    sources[2]["official_link_provenance"] = dict(
        url="https://www.chuncheon.go.kr/health/business/mental/mental-health/",
        capture_path="output/donghaeng_execution_20261005/sources/official_raw/chuncheon_city_mental_health.html",
        sha256=hashlib.sha256(city_capture.read_bytes()).hexdigest(),
        claim="춘천시 보건소 공식 페이지에 센터 홈페이지와 033-242-7574 연결 확인")
shutil.copyfile(catalog_path, DEST / "welfare_catalog_20260819.csv")
(DEST / "catalog.json").write_text(json.dumps(dict(schema_version="1.0", source_url="https://www.data.go.kr/data/15083323/fileData.do", retrieved_at=catalog_meta["retrieved_at"], sha256=catalog_meta["sha256"], actual_rows=len(rows), items=catalog), ensure_ascii=False, indent=2)+"\n")
manifest = dict(schema_version="1.0", retrieved_at=datetime.now(timezone(timedelta(hours=9))).isoformat(), sources=sources,
                catalog=dict(path="shared/support/catalog.json", raw_path="shared/support/welfare_catalog_20260819.csv", actual_rows=len(rows), sha256=catalog_meta["sha256"], eligibility_status="unknown", rights_note="공공데이터포털 무료·이용허락범위 제한없음. 목록 요약은 개인 자격 검증이 아님."),
                api_status=dict(central_detail="not_called_key_required", local_detail="not_called_key_required"),
                scope="Current official support facts checked against retrospective 2025 measurements; no personal eligibility determination.")
(DEST / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
(EVIDENCE / ".gitignore").write_text("# Full captures stay local pending source rights review.\nraw/\ntext/\n")
print(json.dumps(dict(sources=len(sources),catalog_rows=len(rows),manifest=str(DEST / "manifest.json")),ensure_ascii=False))
