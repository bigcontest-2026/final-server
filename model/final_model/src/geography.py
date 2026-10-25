"""Explicit geographic scope; a valid code never implies observed data coverage."""
import re

LEGACY_REGIONS = {"11680": "강남구", "51110": "춘천시"}
PROVINCES = {"11", "26", "27", "28", "29", "30", "31", "36", "41", "43", "44", "46", "47", "48", "50", "51", "52"}


def validate_region_code(code):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{5}", code) or code[:2] not in PROVINCES:
        raise ValueError("invalid_region_code")
    return code


def region_registry(config=None):
    regions = dict((config or {}).get("regions", LEGACY_REGIONS))
    if not regions:
        raise ValueError("empty_region_registry")
    for code, name in regions.items():
        validate_region_code(code)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invalid_region_name")
    return regions


def region_aliases(regions, explicit=None):
    aliases = {code: code for code in regions}
    counts = {name: list(regions.values()).count(name) for name in regions.values()}
    aliases.update({name: code for code, name in regions.items() if counts[name] == 1})
    defaults = {"서울 강남구": "11680", "서울특별시 강남구": "11680",
                "강원 춘천시": "51110", "강원특별자치도 춘천시": "51110"}
    aliases.update({name: code for name, code in defaults.items() if code in regions})
    for name, code in (explicit or {}).items():
        if code not in regions or (name in aliases and aliases[name] != code):
            raise ValueError("invalid_or_ambiguous_region_alias:" + name)
        aliases[name] = code
    return aliases


def resolve_region(value, regions, aliases=None):
    try:
        return region_aliases(regions, aliases)[value]
    except KeyError:
        raise ValueError("unmapped_region:" + str(value)) from None


def availability(rows, region_code=None, month=None):
    """Describe the exact query slice, preserving zero candidates versus no observations."""
    sources = sorted({name for row in rows for name,value in row.get("sources",{}).items()
                      if value.get("activity_daily") is not None})
    has_behavior = any("sources" in row for row in rows)
    present = bool(sources) if has_behavior else bool(rows)
    return {"status": "available" if present else "data_unavailable",
            "reason": None if present else "no_matching_observations_in_bundle",
            "available_sources":sources,
            "region_code": region_code, "month": month,
            "coverage_verified": False, "operational_alert_allowed": False}
