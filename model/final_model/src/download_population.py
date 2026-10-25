"""Download the official MOIS nationwide age/sex CSVs for the fixed I5 period."""
import argparse
import json
from pathlib import Path
from datetime import datetime,timezone
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from .common import sha,dump

PAGE="https://jumin.mois.go.kr/ageStatMonth.do"
ENDPOINT="https://jumin.mois.go.kr/downloadCsvAge.do?searchYearMonth=month&xlsStats=3"


def download(output,months=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    records=[]
    for month in months or [f"2025{m:02d}" for m in range(7,13)]:
        params=dict(sltOrgType="1",sltOrgLvl1="A",sltOrgLvl2="",gender="gender",sum="sum",sltUndefType="",
            searchYearStart=month[:4],searchMonthStart=month[4:],searchYearEnd=month[:4],searchMonthEnd=month[4:],
            sltOrderType="1",sltOrderValue="ASC",sltArgTypes="10",sltArgTypeA="0",sltArgTypeB="100",category="month")
        path=output/f"mois_age_sex_{month}.csv"
        if not path.exists():
            request=Request(ENDPOINT,data=urlencode(params).encode(),headers={"Referer":PAGE,"User-Agent":"Mozilla/5.0","Content-Type":"application/x-www-form-urlencoded"})
            with urlopen(request,timeout=60) as response:
                content=response.read();headers=dict(response.headers)
            if b'<html' in content[:1024].lower() or len(content)<10000:
                raise ValueError(f"MOIS_download_not_population_csv:{month}")
            path.write_bytes(content)
            dump(path.with_suffix('.response.json'),dict(headers=headers,parameters=params,source_url=ENDPOINT,
                retrieved_at=datetime.now(timezone.utc).isoformat()))
        records.append(dict(month=month,file=path.name,sha256=sha(path),bytes=path.stat().st_size,parameters=params))
        print(json.dumps(records[-1]),flush=True)
    dump(output/"download_manifest.json",dict(source_page=PAGE,endpoint=ENDPOINT,files=records))


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--months',nargs='+')
    download(**vars(parser.parse_args()))
