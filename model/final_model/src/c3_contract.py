"""C3 linkage uses the same month, region, sex and adult age as V3."""
from datetime import date


def age_code(age):
    if age == "60+":
        return "60GU"
    if age in ("20", "30", "40", "50"):
        return age + "G"
    raise ValueError("unsupported_c3_age")


def expected_link_keys(config, regions):
    start = date.fromisoformat(config["evaluation_start"])
    end = date.fromisoformat(config["evaluation_end"])
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    ages = [age_code(age) for age in config["age_groups"]]
    return {(month, region, sex, age) for month in months for region in regions
            for sex in ("MAN", "WMAN") for age in ages}
