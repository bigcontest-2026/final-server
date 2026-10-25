"""Build extensible I5 inputs from explicit, month-specific MOIS population rows.

No downloads or population imputation. The frozen survey means remain unchanged.
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import dump, sha
from .geography import region_registry
from .survey_area_index import GROUPS, DOMAINS, read_frozen, profiles, require, build

BASE_FIELDS = ["month", "region_code", "region", "dong_code", "dong", "population_total", "population_20plus"]
POP_FIELDS = ["pop_" + "_".join(group) for group in GROUPS]


def integer(value):
    require(isinstance(value,str) and value.isascii() and value.isdigit(), "invalid_population_integer")
    return int(value)


def read_population(path, expected_dongs=None):
    with Path(path).open(encoding="utf-8-sig",newline="") as stream:
        reader=csv.DictReader(stream)
        require(set(BASE_FIELDS+POP_FIELDS) <= set(reader.fieldnames or []), "population_columns_missing")
        rows=list(reader)
    require(bool(rows), "empty_population_input")
    keys=set();regions={};dong_names={}
    for row in rows:
        code,month,dong=row["region_code"],row["month"],row["dong_code"]
        require(month in {f"2025{m:02d}" for m in range(7,13)}, "unsupported_month")
        require(len(dong)==10 and dong.isascii() and dong.isdigit() and dong[:5]==code, "invalid_dong_code")
        require(bool(row["dong"].strip()), "empty_dong_name")
        require(code not in regions or regions[code]==row["region"], "region_code_name_mismatch")
        regions[code]=row["region"]
        # Names can change between months but cannot conflict within the same month.
        key=(month,dong)
        require(key not in keys, "duplicate_dong_month")
        keys.add(key);dong_names[key]=row["dong"]
        for field in ["population_total","population_20plus",*POP_FIELDS]:
            row[field]=integer(row[field])
        require(row["population_20plus"]==sum(row[f] for f in POP_FIELDS)>0, "population_denominator_mismatch")
        require(row["population_total"]>=row["population_20plus"], "invalid_population_total")
    region_registry({"regions":regions})
    scope="observed_input_only"
    if expected_dongs:
        with Path(expected_dongs).open(encoding="utf-8-sig",newline="") as stream:
            registry=list(csv.DictReader(stream))
        expected={(r["month"],r["dong_code"]) for r in registry}
        require(len(expected)==len(registry) and expected==keys, "expected_dong_coverage_mismatch")
        scope="matches_supplied_monthly_dong_registry"
    return rows,scope


def make_tables(population,national):
    def score(row):
        total=row["population_20plus"]
        out=dict(row)
        for key in GROUPS:out["share_"+"_".join(key)]=row["pop_"+"_".join(key)]/total
        out["i5_reference"]=sum(out["share_"+"_".join(k)]*national[k]["i5"] for k in GROUPS)
        for domain in DOMAINS:
            out[domain+"_reference"]=sum(out["share_"+"_".join(k)]*national[k]["domains"][domain] for k in GROUPS)
        return out
    dong=[score(row) for row in population]
    groups=defaultdict(list)
    for row in population:groups[row["month"],row["region_code"]].append(row)
    sgg=[]
    for (month,code),rows in sorted(groups.items()):
        row=dict(month=month,region_code=code,region=rows[0]["region"])
        for field in ["population_total","population_20plus",*POP_FIELDS]:row[field]=sum(r[field] for r in rows)
        sgg.append(score(row))
    detail=[dict(month=row["month"],dong_code=row["dong_code"],sex=sex,age_group=age,
                 population=row["pop_"+sex+"_"+age]) for row in population for sex,age in GROUPS]
    return {"region_month_summary.csv":sgg,"dong_month_reference.csv":dong,
            "dong_month_six_group_population.csv":detail}


def prepare(population, national_source, output_root, expected_dongs=None, *, publish_latest=True):
    tables,_,upstream=read_frozen(national_source)
    national=profiles(tables["national_six_group_profile.csv"])
    rows,scope=read_population(population,expected_dongs)
    generated=make_tables(rows,national)
    run_id=datetime.now(timezone.utc).strftime("population_inputs_%Y%m%d_%H%M%S_%f")
    folder=Path(output_root)/run_id;folder.mkdir(parents=True,exist_ok=False)
    for name,data in generated.items():
        with (folder/name).open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)
    # Preserve bytes after the canonical newline check in read_frozen.
    national_path=Path(national_source)/"national_six_group_profile.csv"
    (folder/national_path.name).write_bytes(national_path.read_bytes().replace(b"\r\n",b"\n"))
    generated[national_path.name]=tables[national_path.name]
    files={name:{"rows":len(data),"sha256":sha(folder/name)} for name,data in generated.items()}
    # CSV writer uses CRLF; the consuming frozen reader hashes canonical LF bytes.
    import hashlib
    for name in files:files[name]["sha256"]=hashlib.sha256((folder/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    dump(folder/"manifest.json",dict(index_version=upstream["index_version"],data_version=run_id,files=files,
        scope=scope,nationwide_coverage_claimed=False,source_population_sha256=sha(population),
        source_survey_manifest_sha256=sha(Path(national_source)/"manifest.json"),
        expected_dong_registry_sha256=sha(expected_dongs) if expected_dongs else None))
    return folder,build(folder,output_root,publish_latest=publish_latest)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--population",required=True)
    parser.add_argument("--national-source",required=True)
    parser.add_argument("--output-root",required=True)
    parser.add_argument("--expected-dongs")
    args=parser.parse_args()
    source,bundle=prepare(**vars(args))
    print(json.dumps({"prepared_source":str(source),"bundle":str(bundle)},ensure_ascii=False))


if __name__=="__main__":main()
