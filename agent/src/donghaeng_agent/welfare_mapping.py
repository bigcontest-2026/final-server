"""Deterministic research mapping and welfare filtering; no inferred I5 cutoffs.

Conditions are tri-state predicates prepared by a versioned upstream evaluator.
Missing predicates remain unknown. A database adapter supplies program records;
this module never infers personal eligibility from area observations.
"""
from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping, Sequence

TAGS = frozenset({
    "social_connection", "isolation_outreach", "mental_health", "employment",
    "income_support", "housing", "health_care", "daily_living", "family_support",
})


@dataclass(frozen=True)
class MappingRule:
    rule_id: str
    version: str
    status: str
    signal_conditions: tuple[str, ...]
    i5_conditions: tuple[str, ...]
    tags: tuple[str, ...]
    reason: str

    def __post_init__(self):
        if not self.rule_id or not self.version or not self.reason:
            raise ValueError("rule identity, version and reason required")
        if self.status not in {"draft", "approved", "retired"}:
            raise ValueError("invalid rule status")
        if not self.signal_conditions or not self.i5_conditions:
            raise ValueError("both activity and I5 conditions required")
        if not self.tags or not set(self.tags) <= TAGS:
            raise ValueError("invalid welfare tags")


def select_fields(rules: Sequence[MappingRule], predicates: Mapping[str, bool | None],
                  evidence: Mapping[str, list[str]], *, research_mode: bool = True):
    if any(v is not None and type(v) is not bool for v in predicates.values()):
        raise ValueError("predicates must be true, false or null")
    matches, pending, tags = [], [], set()
    identities = [(r.rule_id, r.version) for r in rules]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate rule version")
    for rule in rules:
        if rule.status == "retired" or rule.status == "draft" and not research_mode:
            continue
        keys = rule.signal_conditions + rule.i5_conditions
        if any(predicates.get(k) is False for k in keys):
            continue
        missing = [k for k in keys if predicates.get(k) is None or not evidence.get(k)]
        if missing:
            pending.append({"rule_id": rule.rule_id, "version": rule.version,
                            "missing_conditions_or_evidence": missing})
            continue
        refs = sorted({ref for k in keys for ref in evidence[k]})
        matches.append({"rule_id": rule.rule_id, "version": rule.version,
                        "status": rule.status, "reason": rule.reason,
                        "welfare_fields": list(rule.tags), "evidence_ids": refs})
        tags.update(rule.tags)
    return {"welfare_fields": sorted(tags), "matched_rules": matches,
            "pending_rules": pending, "status": "selected" if tags else "held",
            "quality_status": "hold", "operational_alert_allowed": False}


def review_program(program: dict, mapping: dict, *, region_codes: set[str],
                   age_range: tuple[int, int], support_as_of: str):
    """Use verified structured content assessment, never title keyword guesses.

    content_matches[tag]: {status: match|mismatch|unknown, reason, evidence_ids}.
    The adapter must verify source bytes/quotes before setting source_verified.
    None geography/age/window means unknown, not unrestricted eligibility.
    """
    date.fromisoformat(support_as_of)
    if age_range[0] > age_range[1]:
        raise ValueError("invalid age range")
    selected = set(mapping["welfare_fields"]) & set(program.get("program_tags", []))
    result = {"program_id": program["program_id"],
              "is_synthetic": program.get("is_synthetic", False),
              "verification_scope": program.get("verification_scope", "adapter_supplied"),
              "program_version_id": program["program_version_id"],
              "title": program["title"], "conditions": [], "recommendation_reasons": [],
              "unconfirmed_conditions": [], "exclusion_reasons": []}
    if not selected:
        return {**result, "status": "excluded", "exclusion_reasons": ["tag_mismatch"]}
    if program.get("source_verified") is not True:
        return {**result, "status": "source_hold"}
    content = program.get("content_matches", {})
    matched = [tag for tag in sorted(selected)
               if content.get(tag, {}).get("status") == "match"
               and content[tag].get("evidence_ids") and content[tag].get("reason")]
    if not matched:
        mismatch = all(content.get(tag, {}).get("status") == "mismatch"
                       and content[tag].get("evidence_ids") for tag in selected)
        return {**result, "status": "excluded" if mismatch else "source_hold",
                "exclusion_reasons": ["content_mismatch"] if mismatch else [],
                "unconfirmed_conditions": [] if mismatch else ["support_content"]}

    def check(name, status):
        result["conditions"].append({"name": name, "status": status})
        if status == "mismatch":
            result["exclusion_reasons"].append(name)
        elif status == "unknown":
            result["unconfirmed_conditions"].append(name)

    regions = program.get("region_codes")
    check("region", "match" if regions is not None and ("*" in regions or region_codes.intersection(regions))
          else "unknown" if regions is None else "mismatch")
    ages = program.get("age_range")
    if ages is not None and (len(ages) != 2 or ages[0] > ages[1]):
        raise ValueError("invalid program age range")
    check("age", "unknown" if ages is None else
          "mismatch" if age_range[1] < ages[0] or age_range[0] > ages[1] else
          "match" if ages[0] <= age_range[0] and age_range[1] <= ages[1] else "unknown")
    start, end = program.get("start_date"), program.get("end_date")
    for value in (start, end):
        if value is not None:
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError("invalid application window")
    check("period", "mismatch" if program.get("application_status") == "closed"
          or start and support_as_of < start or end and support_as_of > end
          else "match" if start and end and program.get("application_status") == "open" else "unknown")
    # Personal circumstances cannot be established by this aggregate pipeline.
    for name in program.get("personal_conditions", []):
        check(name, "unknown")
    check("individual_eligibility_and_current_acceptance", "unknown")
    for tag in matched:
        result["recommendation_reasons"].append({"tag": tag,
            "content_reason": content[tag]["reason"], "content_evidence_ids": content[tag]["evidence_ids"],
            "mapping_rules": [r for r in mapping["matched_rules"] if tag in r["welfare_fields"]]})
    result["status"] = "excluded" if result["exclusion_reasons"] else "needs_confirmation"
    return result


def recommend(mapping: dict, search_welfare: Callable[..., dict], **scope):
    """Adapter owns pagination, corpus version, verified records and coverage."""
    if not mapping["welfare_fields"]:
        return {"status": "mapping_held", "programs": [], "coverage": None}
    response = search_welfare(welfare_fields=mapping["welfare_fields"], match_mode="or")
    if response["status"] != "ok":
        return {"status": "search_failed", "programs": [], "coverage": response.get("coverage")}
    programs = [review_program(p, mapping, **scope) for p in response["programs"]]
    return {"status": "completed", "programs": programs, "coverage": response["coverage"],
            "is_synthetic": response.get("is_synthetic", False),
            "corpus_version": response["corpus_version"],
            "quality_status": "hold", "operational_alert_allowed": False}
