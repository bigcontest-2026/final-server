"""신한 제공 카드 집계자료의 일별 기준선/월별 변화 검토. 원본 파일은 수정하지 않는다."""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import math
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd

VERSION = "1.1.0"
EXPERIMENT_ID = "C-3"
COMMON = ["TA_YMD", "MCT_SGG_CD", "MCT_RY_CD", "SEX_CCD", "AGE_CCD", "TS_AT", "USE_CNT"]
EXTRA = {"data1": "TIME_GB", "data2": "CLN_SGG_CD"}
VIEW_COLUMNS = {
    "age": [], "age_sex": ["sex"], "age_time": ["time_band"],
    "age_industry": ["industry"], "age_residence": ["residence_sido"],
}
GROUP_COLUMNS = ["group_id", "view", "region", "age", "sex", "time_band", "industry", "residence_sido"]
REGIONS = {
    "서울 강남구": "강남구", "서울특별시 강남구": "강남구", "강남구": "강남구",
    "강원 춘천시": "춘천시", "강원특별자치도 춘천시": "춘천시", "춘천시": "춘천시",
}
LIMITATIONS = [
    "merchant_district_not_customer_residential_district",
    "aggregate_transactions_not_unique_customers_or_individual_isolation",
    "six_month_window_no_yearly_seasonality_or_yoy_validation",
    "data1_and_data2_overlap_do_not_sum_or_count_as_independent_evidence",
    "views_and_calendar_subsets_overlap_do_not_sum",
    "missing_cells_not_imputed_zero_disclosure_rule_unverified",
    "thresholds_exploratory_no_ground_truth_accuracy",
    "current_snapshot_release_date_backtest_not_verified",
    "nominal_amount_not_inflation_adjusted",
    "no_causal_explanation_or_prediction_performed",
]


def json_clean(value):
    if isinstance(value, dict): return {str(k): json_clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [json_clean(v) for v in value]
    if isinstance(value, np.generic): return json_clean(value.item())
    if isinstance(value, (pd.Timestamp,)): return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and not math.isfinite(value): return None
    if value is pd.NA or value is pd.NaT: return None
    return value


def write_json(path, value):
    Path(path).write_text(json.dumps(json_clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""): h.update(b)
    return h.hexdigest()


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_config(c):
    template = json.loads((Path(__file__).parent / "config_c3.json").read_text(encoding="utf-8"))
    if set(c) != set(template): raise ValueError("설정 키가 기본 config_c3.json과 다릅니다.")
    dates = [pd.Timestamp(c[k]) for k in ["reference_start", "reference_end", "evaluation_start", "evaluation_end"]]
    if not dates[0] < dates[1] < dates[2] <= dates[3]:
        raise ValueError("기준 시작 < 기준 끝 < 평가 시작 <= 평가 끝이어야 합니다.")
    if any(d.hour or d.minute or d.second or d.tzinfo is not None for d in dates):
        raise ValueError("날짜는 시간대 없는 YYYY-MM-DD로 입력하세요.")
    if dates[2].day != 1 or dates[3] != dates[3] + pd.offsets.MonthEnd(0):
        raise ValueError("월별 집계를 위해 평가 기간은 월 첫날~월 마지막날로 지정하세요.")
    ages = c["age_groups"]
    if not isinstance(ages, list) or len(set(ages)) != len(ages) or any(a not in [str(x) for x in range(10, 100, 10)] + ["60+"] for a in ages):
        raise ValueError("age_groups는 [\"20\", \"30\"] 형태이며 빈 목록이면 전 연령입니다.")
    if "60+" in ages and set(ages) & {"60", "70", "80", "90"}:
        raise ValueError("60+ and its constituent age bands cannot be selected together")
    allowed = {"data1": {"age", "age_sex", "age_time", "age_industry"}, "data2": {"age", "age_sex", "age_residence", "age_industry"}}
    if set(c["views"]) != set(allowed): raise ValueError("views에는 data1, data2가 필요합니다.")
    for ds, vs in c["views"].items():
        if not isinstance(vs, list) or "age" not in vs or len(vs) != len(set(vs)) or not set(vs) <= allowed[ds]:
            raise ValueError(f"{ds}: 지원하지 않는 view입니다. age는 필수입니다.")
    for k in ["min_reference_days_per_weekday", "min_monthly_comparable_days"]:
        if type(c[k]) is not int or c[k] < 2: raise ValueError(f"{k}는 2 이상의 정수여야 합니다.")
    for k in ["min_reference_coverage", "min_monthly_coverage", "cell_count_warning_ratio"]:
        if type(c[k]) not in (int, float) or not 0 < c[k] <= 1: raise ValueError(f"{k}는 0 초과 1 이하여야 합니다.")
    for k in ["decline_pct", "robust_z_threshold", "small_relative_mad"]:
        if type(c[k]) not in (int, float) or not math.isfinite(c[k]) or c[k] <= 0: raise ValueError(f"{k}는 양의 유한수여야 합니다.")
    if c["decline_pct"] > 100 or c["small_relative_mad"] > 1: raise ValueError("감소율 또는 MAD 설정 범위를 확인하세요.")
    drops = c["sensitivity_drop_pct"]
    if not isinstance(drops, list) or not drops or any(type(t) not in (float, int) or not 0 < t <= 100 for t in drops):
        raise ValueError("민감도 감소율은 0 초과 100 이하 목록이어야 합니다.")
    m = c["isolation_forest"]
    if set(m) != set(template["isolation_forest"]): raise ValueError("isolation_forest 설정 키를 확인하세요.")
    if type(m["enabled"]) is not bool or not 0 < m["contamination"] <= .5: raise ValueError("IF enabled/contamination 설정 오류")
    if any(type(m[k]) is not int for k in ["n_estimators", "min_training_rows", "random_state"]): raise ValueError("IF 정수 설정 오류")
    if not 10 <= m["n_estimators"] <= 1000 or m["min_training_rows"] < 20: raise ValueError("IF 표본/트리 수 설정 오류")
    return c


def load_calendar(folder, config):
    folder = Path(folder)
    meta = json.loads((folder / "calendar_metadata.json").read_text(encoding="utf-8"))
    if config["reference_start"] < meta["coverage_start"] or config["evaluation_end"] > meta["coverage_end"]:
        raise ValueError("설정 기간이 공휴일 달력의 검증 범위 밖입니다. calendar 파일과 metadata를 먼저 갱신하세요.")
    df = pd.read_csv(folder / "calendar_2025_H2.csv", dtype=str, keep_default_na=False)
    if set(df.columns) != {"date", "name"} or df.date.duplicated().any() or df.name.eq("").any():
        raise ValueError("공휴일 달력 형식/중복을 확인하세요.")
    parsed = pd.to_datetime(df.date, format="%Y-%m-%d", errors="raise")
    if not parsed.between(meta["coverage_start"], meta["coverage_end"]).all(): raise ValueError("공휴일이 달력 범위를 벗어났습니다.")
    return dict(zip(df.date, df.name)), meta


@dataclass(frozen=True)
class InputSource:
    path: Path
    member: str | None
    dataset: str
    encoding: str
    sep: str

    @contextmanager
    def open(self):
        if self.member is None:
            with self.path.open("rb") as f: yield f
        else:
            with ZipFile(self.path) as z:
                with z.open(self.member) as f: yield f

    @property
    def label(self): return self.path.name + ("::" + self.member if self.member else "")


def _identify(path, member=None):
    probe = InputSource(Path(path), member, "", "", "")
    with probe.open() as f: sample = f.read(8192)
    text = None
    for encoding in ["utf-8-sig", "cp949", "euc-kr"]:
        try:
            text = codecs.getincrementaldecoder(encoding)().decode(sample, final=False)
            break
        except UnicodeDecodeError: continue
    if not text: return None
    first = text.splitlines()[0] if text.splitlines() else ""
    sep = "\t" if "\t" in first else ","
    header = [x.strip().lstrip("\ufeff").upper() for x in next(csv.reader([first], delimiter=sep))]
    if not set(COMMON) <= set(header): return None
    if len(header) != len(set(header)): raise ValueError(f"중복 열 이름: {probe.label}")
    matches = [ds for ds, extra in EXTRA.items() if set(COMMON + [extra]) == set(header)]
    if len(matches) != 1: raise ValueError(f"원본 8개 열과 다른 입력입니다: {probe.label}. 머지 파일 대신 원본을 지정하세요.")
    return InputSource(Path(path), member, matches[0], encoding, sep)


def discover_sources(input_path, dataset="auto"):
    p = Path(input_path).expanduser().resolve()
    if not p.exists(): raise FileNotFoundError(f"입력 경로가 없습니다: {p}. --input으로 원본 ZIP/TXT/폴더를 지정하세요.")
    paths = sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in {".zip", ".txt", ".csv", ".tsv"}) if p.is_dir() else [p]
    found = []
    for path in paths:
        if path.suffix.lower() == ".zip":
            with ZipFile(path) as z:
                members = sorted(n for n in z.namelist() if Path(n).suffix.lower() in {".txt", ".csv", ".tsv"} and not n.startswith("__MACOSX/"))
            for member in members:
                s = _identify(path, member)
                if s: found.append(s)
        else:
            s = _identify(path)
            if s: found.append(s)
    if dataset in EXTRA: found = [s for s in found if s.dataset == dataset]
    if not found: raise ValueError("신한 원본 8개 열이 있는 TXT/CSV를 찾지 못했습니다. data/에 원본을 넣거나 --input 경로를 확인하세요.")
    if dataset == "both" and {s.dataset for s in found} != set(EXTRA): raise ValueError("--dataset both는 데이터1·2가 모두 필요합니다. 하나만 실행하려면 --dataset data1 또는 data2를 지정하세요.")
    return found


def group_identity(view, region, age, **extras):
    value = dict(view=view, region=region, age=age, sex="ALL", time_band="ALL", industry="ALL", residence_sido="ALL")
    value.update(extras)
    value["group_id"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    return value


def normalize_chunk(df, dataset):
    df = df.copy()
    df.columns = [str(x).strip().lstrip("\ufeff").upper() for x in df.columns]
    for c in df: df[c] = df[c].str.strip()
    keys = [c for c in COMMON + [EXTRA[dataset]] if c not in ["TS_AT", "USE_CNT"]]
    for c in keys:
        if c == "CLN_SGG_CD":
            df[c] = df[c].replace("", "정보없음")
        elif df[c].eq("").any(): raise ValueError(f"{c}에 빈 값이 있습니다. 0으로 채우지 않고 중단합니다.")
    if not df.TA_YMD.str.fullmatch(r"\d{8}").all(): raise ValueError("TA_YMD는 YYYYMMDD여야 합니다.")
    df["date"] = pd.to_datetime(df.TA_YMD, format="%Y%m%d", errors="raise")
    df["region"] = df.MCT_SGG_CD.map(REGIONS)
    if df.region.isna().any(): raise ValueError(f"지원하지 않는 가맹점 지역: {sorted(df.loc[df.region.isna(), 'MCT_SGG_CD'].unique())[:5]}")
    df["age"] = df.AGE_CCD.str.replace(r"\s+", "", regex=True).str.replace("대", "", regex=False)
    corporate = df.SEX_CCD.eq("법인") | df.age.eq("법인")
    if (df.SEX_CCD.eq("법인") != df.age.eq("법인")).any(): raise ValueError("성별/연령 법인 표기가 서로 다릅니다.")
    if not df.loc[~corporate, "age"].isin([str(a) for a in range(10, 100, 10)]).all(): raise ValueError("연령 범주를 확인하세요. 원본의 '20 대' 등만 지원합니다.")
    if not df.loc[~corporate, "SEX_CCD"].isin(["남성", "여성"]).all(): raise ValueError("알 수 없는 성별 범주입니다.")
    if dataset == "data2" and corporate.any(): raise ValueError("법인 제외 자료인 데이터2에 법인 행이 있습니다. 입력을 확인하세요.")
    for col, out in [("TS_AT", "amount"), ("USE_CNT", "count")]:
        if not df[col].str.fullmatch(r"\d+").all(): raise ValueError(f"{col}에 음수/결측/숫자가 아닌 값이 있습니다. 임의 변환하지 않습니다.")
        df[out] = pd.to_numeric(df[col], errors="raise")
        if (df[out] > 2**53 - 1).any(): raise ValueError(f"{col} 값이 정확한 수치 계산 범위를 넘습니다.")
    df["sex"] = df.SEX_CCD
    df["industry"] = df.MCT_RY_CD
    if dataset == "data1":
        if not df.TIME_GB.isin(["00_05", "06_11", "12_17", "18_23"]).all(): raise ValueError("TIME_GB의 6시간 범주를 확인하세요.")
        df["time_band"] = df.TIME_GB
    else: df["residence_sido"] = df.CLN_SGG_CD
    return df, corporate


def aggregate_sources(sources, dataset, config, log=print, chunksize=100000):
    """청크별 집계. 실제 키를 정수 코드로 보존해 청크/파일 간 중복도 정확히 검사."""
    sources = [s for s in sources if s.dataset == dataset]
    aggregates = {v: [] for v in config["views"][dataset]}
    key_columns = ["date", "region", "industry", "sex", "age", "time_band" if dataset == "data1" else "residence_sido"]
    dictionaries = {k: {} for k in key_columns}
    key_arrays, input_info, seen_sha = [], [], set()
    stats = Counter()
    raw_categories = {k: set() for k in ["region", "age", "sex", "industry"]}
    date_counts, monthly_rows = Counter(), Counter()
    min_values = {"amount": None, "count": None}
    for source in sources:
        log(f"[{dataset}] 읽는 중: {source.label}")
        h = hashlib.sha256()
        with source.open() as f:
            for b in iter(lambda: f.read(1024 * 1024), b""): h.update(b)
        digest = h.hexdigest()
        if digest in seen_sha: raise ValueError("같은 원본이 중복 선택됐습니다. ZIP과 압축을 푼 TXT 중 하나만 넣으세요.")
        seen_sha.add(digest)
        source_rows = 0
        with source.open() as f:
            for raw in pd.read_csv(f, sep=source.sep, encoding=source.encoding, dtype=str, keep_default_na=False, chunksize=chunksize):
                df, corporate = normalize_chunk(raw, dataset)
                source_rows += len(df)
                stats["input_rows"] += len(df)
                stats["corporate_rows_excluded"] += int(corporate.sum())
                stats["count_equals_5_rows"] += int(df["count"].eq(5).sum())
                for k in min_values:
                    m = int(df[k].min())
                    min_values[k] = m if min_values[k] is None else min(min_values[k], m)
                for k in raw_categories: raw_categories[k].update(df[k].unique())
                date_counts.update(df.date.dt.strftime("%Y-%m-%d").value_counts().to_dict())
                monthly_rows.update(df.date.dt.strftime("%Y-%m").value_counts().to_dict())
                coded = []
                for k in key_columns:
                    vals = df[k].dt.strftime("%Y%m%d") if k == "date" else df[k]
                    dic = dictionaries[k]
                    for x in vals.unique():
                        if x not in dic: dic[x] = len(dic)
                    coded.append(vals.map(dic).to_numpy(dtype=np.int32))
                key_arrays.append(np.column_stack(coded))
                in_period = df.date.between(config["reference_start"], config["evaluation_end"])
                stats["outside_analysis_period_rows_ignored"] += int((~in_period).sum())
                personal = df[~corporate & in_period].copy()
                # Raw duplicate keys were encoded above BEFORE this many-to-one mapping.
                # Every view sums daily counts/amounts before computing weekday medians.
                if "60+" in config["age_groups"]:
                    personal["age"] = personal["age"].replace({a: "60+" for a in ("60", "70", "80", "90")})
                if config["age_groups"]: personal = personal[personal.age.isin(config["age_groups"])]
                stats["selected_rows"] += len(personal)
                for view in aggregates:
                    cols = ["date", "region", "age"] + VIEW_COLUMNS[view]
                    g = personal.groupby(cols, observed=True, dropna=False).agg(amount=("amount", "sum"), count=("count", "sum"), reported_cells=("count", "size")).reset_index()
                    aggregates[view].append(g)
        input_info.append({"filename": source.path.name, "zip_member": source.member, "sha256": digest, "encoding": source.encoding, "delimiter": source.sep, "rows": source_rows})
    if not key_arrays: raise ValueError(f"{dataset}: 빈 입력입니다.")
    log(f"[{dataset}] 원본 키 중복 검사")
    keys = np.concatenate(key_arrays)
    _, frequencies = np.unique(keys, axis=0, return_counts=True)
    duplicate_rows = int((frequencies - 1).sum())
    if duplicate_rows: raise ValueError(f"{dataset}: 같은 원본 집계 키가 {duplicate_rows:,}행 중복됩니다. 합산하지 않고 중단합니다. 입력 파티션 중복을 확인하세요.")
    del keys, key_arrays
    daily_parts = []
    for view, parts in aggregates.items():
        cols = ["date", "region", "age"] + VIEW_COLUMNS[view]
        g = pd.concat(parts, ignore_index=True).groupby(cols, as_index=False, observed=True).agg(amount=("amount", "sum"), count=("count", "sum"), reported_cells=("reported_cells", "sum"))
        if ((g.amount > 2**53 - 1) | (g["count"] > 2**53 - 1)).any(): raise ValueError("집계값이 정확한 수치 계산 범위를 넘었습니다.")
        dims = g[[c for c in cols if c != "date"]].drop_duplicates().copy()
        identities = [group_identity(view, r["region"], r["age"], **{k: r[k] for k in VIEW_COLUMNS[view]}) for r in dims.to_dict("records")]
        if identities:
            ids = pd.DataFrame(identities)
            g = g.merge(ids, on=["region", "age"] + VIEW_COLUMNS[view], validate="many_to_one")
            daily_parts.append(g)
    if not daily_parts: raise ValueError(f"{dataset}: 지정 기간/연령에 분석할 행이 없습니다.")
    daily = pd.concat(daily_parts, ignore_index=True).sort_values(["group_id", "date"]).reset_index(drop=True)
    fingerprint = hashlib.sha256(json.dumps(sorted(seen_sha)).encode()).hexdigest()
    profile = {"dataset": dataset, "source_fingerprint": fingerprint, "files": input_info, **dict(stats), "duplicate_key_rows": 0,
               "minimum_raw_values": min_values, "categories": {k: sorted(v) for k, v in raw_categories.items()},
               "rows_per_month": dict(sorted(monthly_rows.items())), "source_date_count": len(date_counts),
               "date_min": min(date_counts), "date_max": max(date_counts), "selected_age_groups": sorted(daily.age.unique()),
               "age_aggregation": {"60+": ["60", "70", "80", "90"]} if "60+" in config["age_groups"] else {},
               "age_aggregation_stage": "daily_raw_sum_before_weekday_baseline",
               "disclosure_rule_verified": False, "count_floor_note": "최소 건수·5건 행 비율은 관측된 특성입니다. 비공개/보정 규칙은 확인되지 않았으며, 누락 셀을 0으로 바꾸지 않습니다.",
               "daily_aggregate_rows": len(daily), "group_count": int(daily.group_id.nunique())}
    return daily, profile


def calendar_frame(config, holidays):
    d = pd.DataFrame({"date": pd.date_range(config["reference_start"], config["evaluation_end"])})
    d["weekday"] = d.date.dt.weekday
    d["holiday_name"] = d.date.dt.strftime("%Y-%m-%d").map(holidays).fillna("")
    d["day_type"] = np.where(d.holiday_name.ne(""), "holiday", np.where(d.weekday.ge(5), "weekend", "weekday"))
    d["phase"] = np.where(d.date.le(pd.Timestamp(config["reference_end"])), "reference", np.where(d.date.ge(pd.Timestamp(config["evaluation_start"])), "evaluation", "gap"))
    return d


def make_daily_results(daily, config, holidays):
    cal = calendar_frame(config, holidays)
    cal["join"] = 1
    groups = daily[GROUP_COLUMNS].drop_duplicates().copy()
    first = daily.groupby("group_id").date.min()
    groups["first_observed_date"] = groups.group_id.map(first)
    groups["join"] = 1
    grid = groups.merge(cal, on="join").drop(columns="join")
    grid = grid.merge(daily[["group_id", "date", "amount", "count", "reported_cells"]], on=["group_id", "date"], how="left", validate="one_to_one")
    ref = grid[grid.phase.eq("reference") & grid.day_type.ne("holiday")]
    refs = []
    for (gid, dow), part in ref.groupby(["group_id", "weekday"], sort=True):
        valid = part.dropna(subset=["amount", "count"])
        n, expected = len(valid), len(part)
        sufficient = n >= config["min_reference_days_per_weekday"] and n / expected >= config["min_reference_coverage"]
        item = {"group_id": gid, "weekday": int(dow), "reference_n": n, "reference_expected_days": expected,
                "reference_coverage": n / expected, "reference_usable": sufficient,
                "reported_cells_median": float(valid.reported_cells.median()) if n else None,
                "reference_dates": valid.date.dt.strftime("%Y-%m-%d").tolist()}
        for metric in ["amount", "count"]:
            vals = valid[metric].to_numpy(dtype=float)
            med = float(np.median(vals)) if sufficient else None
            mad = float(np.median(np.abs(vals - med))) if sufficient else None
            item[f"{metric}_median"] = med
            item[f"{metric}_mad"] = mad
            item[f"{metric}_reference_values"] = vals.tolist()
        refs.append(item)
    ref_table = pd.DataFrame(refs)
    fields = [k for k in ref_table.columns if k not in ["reference_dates", "amount_reference_values", "count_reference_values"]]
    result = grid.merge(ref_table[fields], on=["group_id", "weekday"], how="left", validate="many_to_one")
    nonholiday = result.day_type.ne("holiday")
    for metric in ["amount", "count"]:
        med, mad = result[f"{metric}_median"], result[f"{metric}_mad"]
        usable = nonholiday & result[metric].notna() & med.gt(0) & result.reference_usable.fillna(False)
        result[f"{metric}_change_pct"] = (100 * (result[metric] / med - 1)).where(usable)
        result[f"{metric}_robust_z"] = ((result[metric] - med) / (1.4826 * mad)).where(usable & mad.gt(0))
        result[f"{metric}_log_ratio"] = (np.log1p(result[metric]) - np.log1p(med)).where(usable)
        result[f"{metric}_small_mad"] = (mad / med < config["small_relative_mad"]).where(usable)
        rates = result[f"{metric}_change_pct"]
        result[f"{metric}_decline_flag"] = rates.le(-config["decline_pct"] + 1e-10).astype("boolean").where(rates.notna())
        z = result[f"{metric}_robust_z"]
        result[f"{metric}_robust_flag"] = (z.lt(-config["robust_z_threshold"]) & rates.le(-config["decline_pct"] + 1e-10)).astype("boolean").where(z.notna() & rates.notna())
    result["review_flag"] = result.amount_decline_flag | result.count_decline_flag
    result["robust_support_flag"] = result.amount_robust_flag | result.count_robust_flag
    result["reported_cells_ratio"] = result.reported_cells / result.reported_cells_median.replace(0, np.nan)
    result["cell_coverage_warning"] = result.reported_cells_ratio.lt(config["cell_count_warning_ratio"]) & result.reported_cells_ratio.notna()
    result["withheld_reason"] = np.select(
        [result.day_type.eq("holiday"), result.amount.isna() | result["count"].isna(), ~result.reference_usable.fillna(False), result.amount_median.le(0) | result.count_median.le(0)],
        ["holiday_context_only", "no_reported_rows_missing_not_zero", "insufficient_reference_days_or_coverage", "nonpositive_reference"], default="")
    return result.sort_values(["group_id", "date"]).reset_index(drop=True), ref_table


def fit_isolation_forest(result, config):
    result = result.copy()
    result["if_score"] = np.nan
    result["if_flag"] = pd.Series(pd.NA, index=result.index, dtype="boolean")
    result["if_model_id"] = ""
    metadata = []
    options = config["isolation_forest"]
    features = ["amount_log_ratio", "count_log_ratio"]
    if not options["enabled"]: return result, [{"status": "disabled"}]
    from sklearn.ensemble import IsolationForest
    for view, part in result.groupby("view", sort=True):
        valid = part[features].notna().all(axis=1)
        train = part[valid & part.phase.eq("reference")]
        target = part[valid & part.phase.eq("evaluation")]
        meta = {"model_id": f"if_{view}", "view": view, "features": features, "training_rows": len(train), "evaluation_rows": len(target),
                "normalization": "log1p(observed)-log1p(reference_same_weekday_median)",
                "reference_start": config["reference_start"], "reference_end": config["reference_end"],
                "threshold_calibration": "training_in_sample_contamination_exploratory", "probability": False, "options": options}
        if len(train) < options["min_training_rows"]:
            metadata.append({**meta, "status": "insufficient_training_rows"})
            continue
        if train[features].drop_duplicates().shape[0] < 2:
            metadata.append({**meta, "status": "constant_training_features"})
            continue
        model = IsolationForest(n_estimators=options["n_estimators"], contamination=options["contamination"], random_state=options["random_state"], n_jobs=1)
        model.fit(train[features].to_numpy(dtype=float))
        if len(target):
            scores = -model.score_samples(target[features].to_numpy(dtype=float))
            result.loc[target.index, "if_score"] = scores
            result.loc[target.index, "if_flag"] = pd.array(scores > -model.offset_, dtype="boolean")
            result.loc[target.index, "if_model_id"] = meta["model_id"]
        metadata.append({**meta, "status": "fitted", "score_threshold": float(-model.offset_), "fit_max_date": train.date.max().strftime("%Y-%m-%d")})
    return result, metadata


def tri_any(values):
    if any(v is True for v in values): return True
    if all(v is False for v in values): return False
    return None


def monthly_summaries(results, config):
    """같은 날짜의 관측 합계와 요일 기준값 합계를 비교. 휴일/결측을 0으로 만들지 않음."""
    descriptive = []
    for (gid, month), p in results.assign(month=results.date.dt.strftime("%Y-%m")).groupby(["group_id", "month"], sort=True):
        if pd.Timestamp(month) + pd.offsets.MonthEnd(0) < p.first_observed_date.iloc[0]: continue
        meta = {k: p.iloc[0][k] for k in GROUP_COLUMNS}
        row = {**meta, "month": month, "observed_days": int(p.amount.notna().sum()), "calendar_days": len(p), "reported_cells": int(p.reported_cells.sum()), "amount_reported_sum": p.amount.sum(min_count=1), "count_reported_sum": p["count"].sum(min_count=1)}
        descriptive.append(json_clean(row))
    target = results[results.phase.eq("evaluation")].assign(month=lambda x: x.date.dt.strftime("%Y-%m"))
    monthly, sensitivity = [], []
    for (gid, month), part in target.groupby(["group_id", "month"], sort=True):
        if pd.Timestamp(month) + pd.offsets.MonthEnd(0) < part.first_observed_date.iloc[0]: continue
        meta = {k: part.iloc[0][k] for k in GROUP_COLUMNS}
        for category in ["all", "weekday", "weekend", "holiday"]:
            p = part if category == "all" else part[part.day_type.eq(category)]
            if p.empty: continue
            normal_days = p[p.day_type.ne("holiday")]
            base = {**meta, "month": month, "calendar_type": category, "calendar_days": len(p),
                    "observed_days": int(p.amount.notna().sum()), "eligible_nonholiday_days": len(normal_days),
                    "holiday_days": int(p.day_type.eq("holiday").sum()), "cell_coverage_warning_days": int(p.cell_coverage_warning.sum()),
                    "if_evaluated_days": int(p.if_flag.notna().sum()), "if_anomaly_days": int(p.if_flag.eq(True).sum()),
                    "if_decline_supported_days": int((p.if_flag.eq(True) & p.review_flag.eq(True)).sum()),
                    "robust_supported_days": int(p.robust_support_flag.eq(True).sum()),
                    "daily_decline_days": int(p.review_flag.eq(True).sum()), "daily_evaluated_days": int(p.review_flag.notna().sum()),
                    "withheld_day_reasons": dict(Counter(x for x in p.withheld_reason if x))}
            flags = []
            for metric in ["amount", "count"]:
                valid = p[f"{metric}_change_pct"].notna()
                n = int(valid.sum())
                coverage = n / len(normal_days) if len(normal_days) else None
                reasons = []
                if category == "holiday": reasons.append("holiday_context_only")
                if not len(normal_days): reasons.append("no_eligible_nonholiday_days")
                if n < config["min_monthly_comparable_days"]: reasons.append("too_few_comparable_days")
                if coverage is None or coverage < config["min_monthly_coverage"]: reasons.append("insufficient_monthly_coverage")
                observed = float(p.loc[valid, metric].sum()) if n else None
                expected = float(p.loc[valid, f"{metric}_median"].sum()) if n else None
                change = 100 * (observed / expected - 1) if expected and expected > 0 else None
                if change is None: reasons.append("no_positive_comparable_baseline")
                flag = bool(change <= -config["decline_pct"] + 1e-10) if not reasons else None
                flags.append(flag)
                base.update({f"{metric}_reported_sum": p[metric].sum(min_count=1), f"{metric}_comparable_sum": observed,
                             f"{metric}_baseline_sum": expected, f"{metric}_change_pct": change, f"{metric}_comparable_days": n,
                             f"{metric}_coverage": coverage, f"{metric}_decline_flag": flag, f"{metric}_withheld_reasons": reasons})
                for drop in config["sensitivity_drop_pct"]:
                    sensitivity.append({"group_id": gid, "month": month, "calendar_type": category, "metric": metric, "decline_threshold_pct": drop,
                                        "review_flag": bool(change <= -drop + 1e-10) if not reasons else None})
            base["review_flag"] = tri_any(flags)
            base["signal_type"] = "amount_and_count_decline" if flags == [True, True] else "amount_decline" if flags[0] is True else "count_decline" if flags[1] is True else "withheld" if None in flags else "no_decline_flag"
            base["quality_review_required"] = base["cell_coverage_warning_days"] > 0 or None in flags
            monthly.append(json_clean(base))
    return descriptive, monthly, sensitivity


def make_agent_records(monthly, dataset, profile, config, is_demo, run_id):
    records = []
    fingerprint = config_hash(config)
    for row in monthly:
        r = {k: row[k] for k in GROUP_COLUMNS + ["month", "calendar_type", "review_flag", "signal_type", "quality_review_required"]}
        r.update({"schema_version": "c3.agent.v1", "experiment_id": EXPERIMENT_ID, "code_version": VERSION, "run_id": run_id,
                  "record_id": f"C3:{VERSION}:{dataset}:{row['group_id']}:{row['month']}:{row['calendar_type']}",
                  "dataset": dataset, "is_demo": is_demo, "evidence_role": "provided_card_behavior_context", "geography_basis": "merchant_location",
                  "geography_granularity": "district", "residence_granularity": "source_province_category" if dataset == "data2" else None,
                  "period_start": row["month"] + "-01", "period_end": (pd.Timestamp(row["month"]) + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d"),
                  "time_granularity": "month", "baseline_method": "fixed_reference_same_weekday_daily_median",
                  "reference_start": config["reference_start"], "reference_end": config["reference_end"], "threshold_pct": config["decline_pct"],
                  "future_values_used": False, "prediction_performed": False, "source_release_dates_verified": False,
                  "source_fingerprint": profile["source_fingerprint"], "config_sha256": fingerprint, "limitations": LIMITATIONS})
        for metric, unit in [("amount", "source_amount_unit"), ("count", "transactions")]:
            r[metric] = {k.removeprefix(metric + "_"): v for k, v in row.items() if k.startswith(metric + "_")}
            r[metric]["unit"] = unit
        r["daily_support"] = {k: row[k] for k in ["calendar_days", "observed_days", "eligible_nonholiday_days", "holiday_days", "daily_evaluated_days", "daily_decline_days", "robust_supported_days", "if_evaluated_days", "if_anomaly_days", "if_decline_supported_days", "cell_coverage_warning_days", "withheld_day_reasons"]}
        r["evidence_files"] = {"monthly": f"{dataset}/monthly_results.csv", "daily": f"{dataset}/daily_results.csv", "reference": f"{dataset}/reference_points.jsonl", "if_models": f"{dataset}/model_metadata.json"}
        records.append(json_clean(r))
    return records
