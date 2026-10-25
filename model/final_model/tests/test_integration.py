import copy,json,math,os
from pathlib import Path
import pytest,jsonschema
from final_model.src.common import dump,sha,verify_bundle
from final_model.src.pipeline import build_behavior,historical_key,MONTHS,REGIONS,SEXES,AGES
from final_model.src.common import load_module
from final_model.src.query import query,survey_query

FINAL=Path(__file__).resolve().parents[1]
@pytest.fixture
def upstream():
    config=Path(os.environ.get("FINAL_MODEL_CONFIG",str(FINAL/"configs/local.json")))
    if not config.exists():config=FINAL/"configs/example.json"
    cfg=json.loads(config.read_text(encoding="utf-8-sig"))
    root=Path(cfg["index_root"])
    if not root.is_absolute():root=(FINAL.parent/root).resolve()
    if not (root/"v2/src/validate_baselines.py").is_file():
        pytest.skip("upstream isolation-index code not configured")
    base=load_module("test_baseline",root/"v2/src/validate_baselines.py")
    scorer=load_module("test_scorer",root/"v3/behavior/src/compare_step7_anomalies.py")
    robust=json.loads((root/"v3/behavior/configs/step7_3_experiment_v1.json").read_text(encoding="utf-8"))
    return base,scorer,robust

def panel():
    return {(m,r,s,a):{"skt":100.0,"card_a":100.0,"card_count":3100} for m in MONTHS for r in REGIONS for s in SEXES for a in AGES}

def calculate(p,upstream):
    return build_behavior(p,*upstream,{},{})

def test_constant_zero_mad_is_null(upstream):
    records=calculate(panel(),upstream)
    assert len(records)==100
    assert all(r["rule"]["candidate"] is False for r in records)
    assert all(r["robust"]["candidate"] is None for r in records)
    assert all(r["sources"]["skt"]["robust"]["status"]=="unavailable_zero_mad" for r in records)
    assert records[0]["rule"]["two_month_streak"] is None

def test_increase_cannot_be_decline(upstream):
    p=panel()
    for key in p:
        if key[0]=="202512":
            p[key]["skt"]=200
            p[key]["card_a"]=200
    records=calculate(p,upstream)
    december=[r for r in records if r["key"]["month"]=="202512"]
    assert all(r["rule"]["candidate"] is False for r in december)
    assert all(r["sources"]["skt"]["Q"]<0 for r in december)

def test_local_decline_and_Q_H_T(upstream):
    p=panel();p["202512","11680","MAN","20G"]["skt"]=20
    row=next(r for r in calculate(p,upstream) if r["key"]==dict(month="202512",region_code="11680",sex_code="MAN",age_code="20G"))
    assert row["rule"]["candidate"] is False
    value=row["sources"]["skt"]
    assert math.isclose(value["Q"],math.log(5))
    assert math.isclose(value["Q"],value["H"]+value["T"])
    assert row["quality"]["status"]=="hold" and row["operational_alert_allowed"] is False

@pytest.mark.parametrize("value",[None,0])
def test_missing_and_zero_not_normal(value,upstream):
    p=panel();p["202512","11680","MAN","20G"]["skt"]=value
    r=next(r for r in calculate(p,upstream) if r["key"]==dict(month="202512",region_code="11680",sex_code="MAN",age_code="20G"))
    assert r["rule"]["candidate"] is None
    assert r["sources"]["skt"]["Q"] is None
    assert r["sources"]["skt"]["activity_daily"]==value
    assert r["comparison_status"]=="unavailable"

def test_historical_no_cross_view_join():
    row=dict(dataset="data2",view="age",calendar_type="all")
    assert historical_key(row) is None
    row.update(view="age_sex",region="강남구",sex="남성",age="20",month="2025-10")
    assert historical_key(row)==("202510","11680","MAN","20G")
    row["dataset"]="data1"
    assert historical_key(row) is None

def test_integrity_fail_closed(tmp_path):
    (tmp_path/"data.json").write_text("{}",encoding="utf-8")
    dump(tmp_path/"manifest.json",dict(status="completed",is_synthetic=False,run_id="test",outputs={"data.json":sha(tmp_path/"data.json")}))
    verify_bundle(tmp_path)
    (tmp_path/"data.json").write_text("changed",encoding="utf-8")
    with pytest.raises(ValueError,match="hash_mismatch"):verify_bundle(tmp_path)

def test_reject_failed_or_synthetic(tmp_path):
    for status,synthetic in [("failed",False),("completed",True)]:
        dump(tmp_path/"manifest.json",dict(status=status,is_synthetic=synthetic))
        with pytest.raises(ValueError):verify_bundle(tmp_path)

@pytest.fixture
def real_bundle():
    explicit=os.environ.get("FINAL_MODEL_BUNDLE")
    if explicit:return Path(explicit)
    path=FINAL/"outputs/latest.json"
    if not path.exists(): pytest.skip("actual data bundle not yet generated")
    return FINAL/"outputs"/json.loads(path.read_text(encoding="utf-8"))["run_id"]

def test_actual_query_and_schema(real_bundle):
    result=query(real_bundle,region_code="11680",month="202510",sex_code="MAN",age_code="20G")
    assert result["total_matched"]==1 and result["records"][0]["historical_reference"] is not None
    assert result["operational_alert_allowed"] is False
    record=result["records"][0]["behavior"]
    schema=json.loads((FINAL/"schemas/behavior.schema.json").read_text(encoding="utf-8-sig"))
    jsonschema.validate(record,schema)
    invalid=copy.deepcopy(record);invalid["operational_alert_allowed"]=True
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,schema)
    invalid=copy.deepcopy(record);invalid["spatial_context"]["status"]="comparable"
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,schema)

def test_unavailable_history_not_replicated(real_bundle):
    result=query(real_bundle,region_code="11680",month="202510",sex_code="MAN",age_code="40G")
    assert result["records"][0]["historical_reference"] is None
    result=query(real_bundle,month="202508")
    assert result["total_matched"]==20 and all(r["historical_reference"] is None for r in result["records"])

def test_separate_survey_contract(real_bundle):
    rows=survey_query(real_bundle)
    assert len(rows)==6
    with pytest.raises(ValueError):survey_query(real_bundle,table="../behavior")
    with pytest.raises(ValueError):query(real_bundle,region_code="1168010100")
    with pytest.raises(ValueError):query(real_bundle,month="202513")
    with pytest.raises(ValueError):query(real_bundle,limit=101)


def test_future_month_cannot_change_october(upstream):
    p=panel()
    before=[r for r in calculate(p,upstream) if r["key"]["month"]=="202510"]
    for key in p:
        if key[0]=="202512":p[key]["skt"]=12345;p[key]["card_a"]=1
    after=[r for r in calculate(p,upstream) if r["key"]["month"]=="202510"]
    assert before==after

def test_all_actual_historical_records_schema(real_bundle):
    from final_model.src.common import read_jsonl
    schema=json.loads((FINAL/"schemas/historical.schema.json").read_text(encoding="utf-8-sig"))
    validator=jsonschema.Draft202012Validator(schema)
    rows=read_jsonl(real_bundle/"historical_reference.jsonl")
    assert len(rows)==880
    for row in rows:validator.validate(row)
    bad=copy.deepcopy(rows[0]);bad["future_values_used"]=True
    with pytest.raises(jsonschema.ValidationError):validator.validate(bad)


def test_external_scope_not_fabricated(real_bundle):
    from final_model.src.query import external_query
    result=external_query(real_bundle,"c1",district_code="11680",quarter=20254)
    assert result["total_matched"]>0 and result["main_model_join"] is False
    assert external_query(real_bundle,"c1",district_code="51110")["total_matched"]==0
    with pytest.raises(ValueError):external_query(real_bundle,"m3")
    with pytest.raises(ValueError):external_query(real_bundle,"c1",month="202510")
