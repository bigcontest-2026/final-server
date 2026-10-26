"""Fixed 2025-12 SGG reference transform for the existing I5 score."""
import math

REFERENCE_VERSION = "i5_sgg_202512_equal_weight_v1"
REFERENCE_MEAN = 35.22968484607534
REFERENCE_STD = 0.8623201205529184
REFERENCE_SCOPE = "2025년 12월 시군구 기준 상대점수"
TEN_GROUP_REFERENCE_VERSION = "i5_10group_sgg_202512_equal_weight_v1"
TEN_GROUP_REFERENCE_MEAN = 35.24084915654042
TEN_GROUP_REFERENCE_STD = 0.8708522905523021


def calculate_i5_display_score(i5, *, reference_version=REFERENCE_VERSION):
    """Return additive display fields; never change or round the source score."""
    if reference_version == REFERENCE_VERSION:
        mean, std = REFERENCE_MEAN, REFERENCE_STD
    elif reference_version == TEN_GROUP_REFERENCE_VERSION:
        mean, std = TEN_GROUP_REFERENCE_MEAN, TEN_GROUP_REFERENCE_STD
    else:
        raise ValueError("unsupported_i5_display_reference")
    result = {
        "i5_z": None,
        "i5_display_score": None,
        "i5_display_clipped": None,
        "i5_display_reference_version": reference_version,
    }
    if i5 is None:
        return result
    if isinstance(i5, bool) or not isinstance(i5, (int, float)) or not math.isfinite(i5) or not 0 <= i5 <= 100:
        raise ValueError("I5 must be a finite number between 0 and 100")
    z = (i5 - mean) / std
    unbounded = 5.0 + z
    result.update(i5_z=z, i5_display_score=max(0.0, min(10.0, unbounded)),
                  i5_display_clipped=unbounded < 0.0 or unbounded > 10.0)
    return result


def project_i5_record(record):
    """Copy a raw reference record and add its matching fixed display projection."""
    ten_group = (record.get("schema_version") == "survey_area_isolation_index_10group_v1"
                 or record.get("source_contract") == "survey_mois_reference_10group_v1")
    version = TEN_GROUP_REFERENCE_VERSION if ten_group else REFERENCE_VERSION
    raw = record.get("isolation_index", record.get("i5_reference"))
    return {**record, **calculate_i5_display_score(raw, reference_version=version)}
