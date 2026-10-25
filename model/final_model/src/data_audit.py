"""Inspect real geographic scope and resident-population column availability."""
import argparse
import csv
import io
import json
from collections import Counter,defaultdict
from pathlib import Path
from zipfile import ZipFile

from .common import dump,sha


def audit(data_root):
    root=Path(data_root);result={"data_root":str(root.resolve()),"files":[],"card":[]}
    for path in sorted(root.iterdir()):
        if not path.is_file():continue
        result["files"].append(dict(name=path.name,bytes=path.stat().st_size,sha256=sha(path)))
        if path.suffix.lower()==".zip":
            with ZipFile(path) as archive:
                for name in archive.namelist():
                    if not name.lower().endswith(".txt"):continue
                    with archive.open(name) as binary:
                        header=binary.readline().decode("cp949",errors="replace")
                    if "MCT_SGG_CD" not in header:continue
                    counts=Counter();months=set()
                    with archive.open(name) as binary,io.TextIOWrapper(binary,encoding="cp949",newline="") as stream:
                        for row in csv.DictReader(stream,delimiter="\t"):
                            counts[row["MCT_SGG_CD"]]+=1;months.add(row["TA_YMD"][:6])
                    result["card"].append(dict(archive=path.name,member=name,region_rows=dict(counts),months=sorted(months)))
    merge=root/"sk_merge_csv_with_hangjangdong.csv"
    counts=Counter();dongs=defaultdict(set);names={};columns=[]
    with merge.open(encoding="utf-8-sig",newline="") as stream:
        reader=csv.DictReader(stream);columns=reader.fieldnames
        for row in reader:
            key=(row["STD_YM"],row["sgg_code"])
            counts[key]+=1;dongs[key].add(row["administrative_dong_code"])
            names[row["sgg_code"]]=row["sgg_name"]
    result["skt_merge"]=dict(columns=columns,regions=names,region_months=[dict(month=m,region_code=r,
        rows=count,observed_dongs=len(dongs[m,r])) for (m,r),count in sorted(counts.items())],
        resident_population_columns_found=[name for name in columns if name.startswith("pop_") or "주민등록" in name or "population_20plus" in name],
        geographic_completeness_verified=False,
        warning="FLOW_POP_CNT columns are SKT flow, not MOIS registered-resident population")
    for path in root.glob("*.json"):
        data=json.loads(path.read_text(encoding="utf-8-sig"))
        if "DATA" in data and data["DATA"] and "signgu_cd" in data["DATA"][0]:
            result["c1"]=dict(file=path.name,regions=sorted({r["signgu_cd"] for r in data["DATA"]}),
                quarters=sorted({r["stdr_yyqu_cd"] for r in data["DATA"]}))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",required=True);parser.add_argument("--output",required=True)
    args=parser.parse_args();result=audit(args.data_root);dump(args.output,result)
    print(json.dumps({"report":args.output,"skt_regions":len(result["skt_merge"]["regions"]),
                      "resident_population_columns":result["skt_merge"]["resident_population_columns_found"]}))


if __name__=="__main__":main()
