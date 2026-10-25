"""Read-only acquisition of the observed official routes, with versioned storage.

Usage: python acquire_official_sources.py --destination /absolute/new/directory
The destination must be empty/new; existing evidence is never overwritten.
This uses public HTML/file routes. It does not invoke welfare detail APIs.
"""
import argparse
import csv
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCES = {
    "welfare_catalog": "https://www.data.go.kr/data/15083323/fileData.do",
    "seoul_youth_full": "https://youth.seoul.go.kr/youthConts.do?key=2310100062&sc_pbancSeCd=008&sc_bbsStngSn=2212200001&sc_bbsCtgrySn=2310200007&sc_qnaCtgryCd=&sc_faqCtgryCd=013",
    "seoul_youth_application": "https://youth.seoul.go.kr/isltYouthSprt/step0.do?key=2311050002",
    "seoul_isolated_household": "https://news.seoul.go.kr/welfare/archives/569275",
    "seoul_family_closed": "https://youth.seoul.go.kr/infoData/sprtInfo/view.do?key=2309130006&sprtInfoId=69709",
    "chuncheon_mhc": "https://www.chmhc.org/sub.php?ardtab=2&menukey=41",
    "chuncheon_city_mental_health": "https://www.chuncheon.go.kr/health/business/mental/mental-health/",
    "bigcontest_home": "https://www.bigcontest.or.kr/",
    "contest_notice_list": "https://www.bigcontest.or.kr/theme/basic/notice_2026.php?page=1",
    "seoul_copyright": "https://www.seoul.go.kr/helper/copyright.do",
}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--destination", type=Path, required=True)
    args = p.parse_args()
    dest = args.destination
    if dest.exists() and any(dest.iterdir()):
        p.error("Destination must be empty; existing captures are preserved")
    dest.mkdir(parents=True, exist_ok=True)
    attempts = []

    def fetch(name, url, suffix=".html", form=None):
        started = datetime.now(timezone.utc).isoformat()
        try:
            request = urllib.request.Request(url,
                data=urllib.parse.urlencode(form).encode() if form else None,
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=35) as response:
                body = response.read()
                meta = dict(url=url, final_url=response.url, retrieved_at=started,
                            http_status=response.status,
                            content_type=response.headers.get("Content-Type"),
                            content_disposition=response.headers.get("Content-Disposition"),
                            size=len(body), sha256=hashlib.sha256(body).hexdigest(),
                            transport="public_web_or_file", welfare_api_call=False)
            (dest / (name + suffix)).write_bytes(body)
            (dest / (name + ".meta.json")).write_text(json.dumps(meta, ensure_ascii=False, indent=2)+"\n")
            attempts.append(dict(name=name, **meta))
            return body
        except Exception as e:
            attempts.append(dict(name=name, url=url, retrieved_at=started,
                                 status="fetch_failed", error=str(e)))
            return None

    for name, url in SOURCES.items():
        fetch(name, url)

    # This follows the public download button. JSON-LD's contentUrl was stale
    # during initial acquisition and returned PNG, so never trust its type alone.
    catalog_html = dest / "welfare_catalog.html"
    if catalog_html.exists():
        html = catalog_html.read_text(errors="replace")
        match = re.search(r"id=\"publicDataDetailPk\"[^>]*value=\"([^\"]+)\"", html)
        if match:
            raw = fetch("catalog_download_button_response",
                        "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do", ".json",
                        dict(publicDataPk="15083323", publicDataDetailPk=match.group(1),
                             atchFileId="", fileDetailSn="1", publicDataTyCode="PR0051"))
            if raw:
                download = json.loads(raw)
                if download.get("status") is True:
                    query = urllib.parse.urlencode(dict(atchFileId=download["atchFileId"],
                                 fileDetailSn=download["fileDetailSn"], insertDataPrcus="N"))
                    body = fetch("welfare_catalog_20260819", "https://www.data.go.kr/cmm/cmm/fileDownload.do?"+query, ".csv")
                    if body:
                        try:
                            rows=list(csv.DictReader(body.decode("utf-8-sig").splitlines()))
                            valid = bool(rows) and "서비스아이디" in rows[0]
                            attempts[-1].update(content_verified="welfare_csv" if valid else "unexpected_fields", actual_rows=len(rows))
                        except (UnicodeDecodeError, csv.Error) as e:
                            attempts[-1].update(content_verified=False, validation_error=str(e))

    for idx in ["34", "33", "32"]:
        fetch("bigcontest_notice"+idx, "https://www.bigcontest.or.kr/theme/basic/section3_notice_2026.ajax.php", ".json", dict(idx=idx))
    (dest / "acquisition_attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(dict(destination=str(dest), attempts=len(attempts)), ensure_ascii=False))

if __name__ == "__main__":
    main()
