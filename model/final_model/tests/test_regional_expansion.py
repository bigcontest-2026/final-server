"""Geographic expansion boundaries and exact real-data regression remain separate."""
import copy
import csv
import json
from pathlib import Path

import pytest
import jsonschema

from final_model.src.geography import region_registry,region_aliases,resolve_region
from final_model.src.pipeline import build_behavior,MONTHS,SEXES,AGES,historical_key
from final_model.src.query import query
from final_model.src.population_inputs import read_population,make_tables,BASE_FIELDS,POP_FIELDS
from final_model.src.survey_area_index import build_records,profiles,query as survey_query
from final_model.tests.test_integration import upstream,real_bundle
from final_model.tests.test_survey_area_index import synthetic_inputs,synthetic_bundle


def test_national_behavior_retains_per_region_statistics(upstream):
    regions={"11110":"종로구","11680":"강남구","26110":"중구"}
    panel={(m,r,s,a):dict(skt=100.,card_a=100.,card_count=3100) for m in MONTHS for r in regions for s in SEXES for a in AGES}
    for position,(s,a) in enumerate((s,a) for s in SEXES for a in AGES):
        panel["202512","11110",s,a]["skt"]=30.+position
    provenance=dict(model_version="final_activity_evidence_v1",run_id="synthetic_test_only",config_sha256="0"*64,
        inputs={str(i):"0"*64 for i in range(2)},source_code_sha256={str(i):"0"*64 for i in range(7)},
        analysis_period=["202507","202512"],source_release_dates_verified=False)
    records=build_behavior(panel,*upstream,provenance,{},regions)
    assert len(records)==150
    for region in regions:
        independent=build_behavior({k:v for k,v in panel.items() if k[1]==region},*upstream,provenance,{},{region:regions[region]})
        assert independent==[r for r in records if r["key"]["region_code"]==region]
    schema=json.loads((Path(__file__).parents[1]/"schemas/behavior.schema.json").read_text(encoding="utf-8"))
    for row in records:jsonschema.validate(row,schema)


def test_region_aliases_do_not_guess_ambiguous_district_names():
    regions=region_registry({"regions":{"26110":"중구","27110":"중구"}})
    assert "중구" not in region_aliases(regions)
    with pytest.raises(ValueError,match="unmapped_region"):resolve_region("중구",regions)
    assert resolve_region("부산 중구",regions,{"부산 중구":"26110"})=="26110"
    assert historical_key(dict(dataset="data2",view="age_sex",calendar_type="all",month="2025-10",region="26110",sex="남성",age="20"),regions)==("202510","26110","MAN","20G")


def test_missing_region_is_not_zero_or_no_candidate(real_bundle):
    result=query(real_bundle,region_code="26110")
    assert result["records"]==[] and result["availability"]["status"]=="data_unavailable"
    assert result["operational_alert_allowed"] is False
    with pytest.raises(ValueError,match="invalid_region_code"):query(real_bundle,region_code="bad")
    available=query(real_bundle,region_code="11680",candidate_only=True)
    assert available["availability"]["status"]=="available"


def population_rows():
    original,national=synthetic_inputs()
    rows=[]
    for code,name in [("11110","종로구"),("26110","중구"),("27110","중구")]:
        row={k:original[k] for k in BASE_FIELDS+POP_FIELDS}
        row.update(region_code=code,region=name,dong_code=code+"51000",dong="테스트동")
        for k in ["population_total","population_20plus",*POP_FIELDS]:row[k]=int(row[k])
        rows.append(row)
    return rows,national


def test_population_expansion_recalculates_every_area_without_fixed_counts():
    rows,national=population_rows()
    tables=make_tables(rows,profiles(national));tables["national_six_group_profile.csv"]=national
    sgg,dong=build_records(tables,"synthetic_only")
    assert len(sgg)==len(dong)==3
    assert {r["region_code"] for r in sgg}=={"11110","26110","27110"}
    assert all(r["isolation_index"]==pytest.approx(43.33333333333333) for r in sgg+dong)
    tables["dong_month_six_group_population.csv"].append(tables["dong_month_six_group_population.csv"][0])
    with pytest.raises(ValueError,match="duplicate_dong_population_group"):build_records(tables,"synthetic_only")


def test_population_input_rejects_missing_geographies_and_duplicate_rows(tmp_path):
    rows,_=population_rows();path=tmp_path/"population.csv"
    def write(data):
        with path.open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=BASE_FIELDS+POP_FIELDS);writer.writeheader();writer.writerows(data)
    write(rows)
    assert len(read_population(path)[0])==3
    expected=tmp_path/"expected.csv";expected.write_text("month,dong_code\n202512,9999951000\n",encoding="utf-8")
    with pytest.raises(ValueError,match="expected_dong_coverage_mismatch"):read_population(path,expected)
    write(rows+[rows[0]])
    with pytest.raises(ValueError,match="duplicate_dong_month"):read_population(path)


def test_survey_query_missing_region_is_explicit(tmp_path):
    bundle=synthetic_bundle(tmp_path)
    result=survey_query(bundle,region_code="26110")
    assert result["availability"]["status"]=="data_unavailable" and result["records"]==[]
