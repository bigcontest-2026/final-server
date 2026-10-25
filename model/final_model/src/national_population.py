"""Validate official nationwide MOIS totals and build actual I5 reference artifacts."""
import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from .common import dump,sha,read_jsonl,write_jsonl
from .geography import PROVINCES
from .population_inputs import BASE_FIELDS,POP_FIELDS,prepare
from .survey_area_index import GROUPS,profiles,read_frozen,make_record,require


def count(value):
    require(re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)",value) is not None,"invalid_official_integer")
    return int(value.replace(",",""))


def read_month(path,month):
    with Path(path).open(encoding="cp949",newline="") as stream:
        reader=csv.DictReader(stream);header=reader.fieldnames
        require(len(header)==40 and len(set(header))==40 and header[0]=="행정구역","invalid_official_header")
        prefix=f"{month[:4]}년{month[4:]}월_"
        require(all(name.startswith(prefix) for name in header[1:]),"official_month_mismatch")
        source={}
        for row in reader:
            require(None not in row and all(value is not None for value in row.values()),"official_row_width")
            match=re.fullmatch(r"(.+?)\s*\(([0-9]{10})\)",row[header[0]])
            require(match is not None,"invalid_official_area")
            name,code=match.group(1).strip(),match.group(2)
            require(code not in source,"duplicate_official_code")
            values=[count(row[key]) for key in header[1:]]
            # Each sex block: total, age-range total, eleven 10-year age bands.
            for offset in (0,13,26):
                require(values[offset]==values[offset+1]==sum(values[offset+2:offset+13]),"official_age_sum_mismatch")
            require(all(values[i]==values[i+13]+values[i+26] for i in range(13)),"official_sex_sum_mismatch")
            source[code]=dict(name=name,values=values)
    provinces={code[:2] for code in source if code[2:]=="00000000"}
    require(provinces==PROVINCES,"official_province_coverage_mismatch")
    leaves={code:row for code,row in source.items() if code[5:]!="00000"}
    district={code[:5]:row for code,row in source.items() if code[5:]=="00000" and code[2:]!="00000000"}
    require(set(code[:5] for code in leaves)<=set(district),"missing_official_district")
    checks=[]
    for province in sorted(provinces):
        children=[row for code,row in leaves.items() if code[:2]==province]
        require(all(sum(row["values"][i] for row in children)==source[province+"00000000"]["values"][i] for i in range(39)),"province_leaf_sum_mismatch")
    for code,row in district.items():
        children=[v for k,v in leaves.items() if k[:5]==code]
        if not children and any(row["values"]):
            require(code.endswith("0"),"unknown_district_hierarchy")
            children=[v for k,v in leaves.items() if k[:4]==code[:4]]
        require(all(sum(child["values"][i] for child in children)==row["values"][i] for i in range(39)),"district_leaf_sum_mismatch:"+code)
        checks.append(code)

    def population(code,row,dong=False):
        value=row["values"];region=code[:5]
        region_label=district[region]["name"].split()
        result=dict(month=month,region_code=region,region=" ".join(region_label[1:]) if len(region_label)>1 else region_label[0],
                    population_total=value[0],population_20plus=sum(value[4:13]))
        if dong:
            result.update(dong_code=code,dong=row["name"].split()[-1])
        for sex,offset in (("남",13),("여",26)):
            result[f"pop_{sex}_20-39"]=value[offset+4]+value[offset+5]
            result[f"pop_{sex}_40-59"]=value[offset+6]+value[offset+7]
            result[f"pop_{sex}_60+"]=sum(value[offset+8:offset+13])
        require(result["population_20plus"]==sum(result[field] for field in POP_FIELDS),"six_group_sum_mismatch")
        return result

    population_rows=[];registry=[];unscored=[]
    for code,row in leaves.items():
        item=population(code,row,True)
        location=dict(month=month,dong_code=code,region_code=code[:5],dong=item["dong"],region=item["region"],
                      province_code=code[:2],province_name=source[code[:2]+"00000000"]["name"],full_name=row["name"])
        registry.append(location)
        if item["population_20plus"]==0:
            unscored.append(dict(**location,reason="zero_registered_adult_population",population_total=item["population_total"]))
        else:population_rows.append(item)
    aggregates=[population(code+"00000",row) for code,row in district.items() if sum(row["values"][4:13])>0]
    report=dict(month=month,source_rows=len(source),published_dong_rows=len(leaves),scored_dong_rows=len(population_rows),
        unscored_zero_adult_rows=len(unscored),official_district_rows=len(district),leaf_region_codes=len({c[:5] for c in leaves}),
        provinces_checked=len(provinces),district_totals_checked=len(checks),all_39_population_fields_reconciled=True,
        zero_population_district_codes=[code for code,row in district.items() if row["values"][0]==0],
        national_population=sum(source[p+"00000000"]["values"][0] for p in provinces),sha256=sha(path))
    return population_rows,aggregates,registry,unscored,report


def build_national(raw_root,national_source,output_root):
    raw_root=Path(raw_root);output_root=Path(output_root)
    all_rows=[];aggregates=[];registry=[];unscored=[];reports=[]
    for month in [f"2025{m:02d}" for m in range(7,13)]:
        rows,sgg,locations,missing,report=read_month(raw_root/f"mois_age_sex_{month}.csv",month)
        all_rows.extend(rows);aggregates.extend(sgg);registry.extend(locations);unscored.extend(missing);reports.append(report)
    staging=output_root/"national_population_inputs";staging.mkdir(parents=True,exist_ok=True)
    path=staging/"population.csv"
    with path.open("w",encoding="utf-8",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=BASE_FIELDS+POP_FIELDS);writer.writeheader();writer.writerows(all_rows)
    expected=staging/"expected_dongs.csv"
    with expected.open("w",encoding="utf-8",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=["month","dong_code"]);writer.writeheader()
        writer.writerows({k:r[k] for k in ("month","dong_code")} for r in all_rows)
    prepared,bundle=prepare(path,national_source,output_root,expected,publish_latest=False)
    manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"));manifest["status"]="validating";dump(bundle/"manifest.json",manifest)
    tables,_,_=read_frozen(national_source);national=profiles(tables["national_six_group_profile.csv"])
    # Include official parent cities as well as 5-digit districts, without double-counting them in dong sums.
    region_names={row["region_code"]:row["region"] for row in aggregates}
    computed=[]
    for row in aggregates:
        weighted=dict(row)
        total=row["population_20plus"]
        for group in GROUPS:weighted["share_"+"_".join(group)]=row["pop_"+"_".join(group)]/total
        for field,score in [("i5_reference","i5")]:weighted[field]=sum(weighted["share_"+"_".join(k)]*national[k][score] for k in GROUPS)
        for domain in ("E","P","O","C","U"):weighted[domain+"_reference"]=sum(weighted["share_"+"_".join(k)]*national[k]["domains"][domain] for k in GROUPS)
        computed.append(make_record(weighted,"sgg",national,prepared.name,region_names))
    write_jsonl(bundle/"sgg_isolation_index.jsonl",sorted(computed,key=lambda r:(r["month"],r["region_code"])))
    dump(bundle/"area_registry.json",registry);dump(bundle/"unscored_areas.json",unscored)
    report=dict(status="passed",source_page="https://jumin.mois.go.kr/ageStatMonth.do",source_registration_type="all_registered_Korean_residents_excluding_foreigners",
        months=reports,nationwide_published_population_coverage_verified=True,
        official_zero_population_omissions_not_inferred=True,source_scope="all_published_MOIS_administrative_units",
        scored_dong_month_rows=len(all_rows),sgg_month_rows=len(computed),unscored_areas=unscored,
        parent_cities_and_districts_overlap_do_not_sum=True,local_survey_measured=False,operational_alert_allowed=False)
    dump(bundle/"national_validation_report.json",report)
    validation=json.loads((bundle/"validation_report.json").read_text(encoding="utf-8"))
    validation.update(sgg_month_rows=len(computed),sgg_count=len(region_names),nationwide_population_coverage_verified=True,
        unscored_zero_adult_rows=len(unscored),schema_records_validated=len(computed)+len(all_rows))
    dump(bundle/"validation_report.json",validation)
    manifest.update(status="completed",geographic_scope="nationwide",national_coverage=report,
        validation=validation,
        outputs={p.name:sha(p) for p in bundle.iterdir() if p.is_file() and p.name!="manifest.json"})
    dump(bundle/"manifest.json",manifest)
    # Publish only after the final files and output hashes exist.
    dump(output_root/"survey_area_index_latest.json",dict(run_id=manifest["run_id"]))
    print(json.dumps(dict(bundle=str(bundle),scored_dong_month_rows=len(all_rows),sgg_month_rows=len(computed),unscored=len(unscored))),flush=True)
    return bundle


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument('--raw-root',required=True);parser.add_argument('--national-source',required=True);parser.add_argument('--output-root',required=True)
    build_national(**vars(parser.parse_args()))
