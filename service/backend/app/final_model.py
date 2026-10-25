"""Read-only adapter to the sibling model's public query functions."""
import hashlib
import importlib
import importlib.util
import json
import logging
import sys
from functools import lru_cache
from pathlib import Path
from threading import RLock

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .config import Settings

ACTIVITY_CONTRACT = "final_activity_evidence_v1"
SURVEY_CONTRACT = "survey_area_isolation_index_v1"
PROVINCES = dict(zip(
    ("11", "26", "27", "28", "29", "30", "31", "36", "41", "43", "44", "46", "47", "48", "50", "51", "52"),
    ("서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시", "세종특별자치시",
     "경기도", "충청북도", "충청남도", "전라남도", "경상북도", "경상남도", "제주특별자치도", "강원특별자치도", "전북특별자치도"),
))
MONTHS = [f"2025{month:02d}" for month in range(7, 13)]
logger = logging.getLogger(__name__)
MODULE_LOCK = RLock()


class ModelUnavailable(Exception):
    def __init__(self, family: str, code: str):
        self.family, self.code = family, code
        super().__init__(code)


@lru_cache(maxsize=8)
def model_modules(root: Path):
    """Load a checkout without installing its training dependencies or changing sys.path."""
    package = root.resolve() / "final_model"
    name = "_service_final_model_" + hashlib.sha256(str(package).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        query = importlib.import_module(name + ".src.query")
        survey = importlib.import_module(name + ".src.survey_area_index")
        common = importlib.import_module(name + ".src.common")
        validators = {
            key: Draft202012Validator(json.loads((package / "schemas" / filename).read_text(encoding="utf-8-sig")))
            for key, filename in {"behavior": "behavior.schema.json", "historical": "historical.schema.json",
                                  "survey": "survey_area_index.schema.json"}.items()
        }
        return query, survey, common, validators
    except Exception:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)
        raise


class FinalModel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._survey_cache = None
        self._survey_lock = RLock()

    def bundle_path(self, family: str) -> Path:
        explicit = self.settings.final_model_bundle if family == "activity" else self.settings.survey_area_bundle
        if explicit is not None:
            return explicit.resolve()
        if family == "survey":
            published = self.settings.model_root / "data/i5/national_2025"
            if published.exists():
                return published.resolve()
        pointer = "latest.json" if family == "activity" else "survey_area_index_latest.json"
        for root in (self.settings.model_runtime_root, self.settings.model_root / "final_model/outputs"):
            path = root / pointer
            if not path.exists():
                continue
            run_id = json.loads(path.read_text(encoding="utf-8-sig"))["run_id"]
            if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
                raise ValueError("invalid_bundle_pointer")
            bundle = (root / run_id).resolve()
            if not bundle.is_relative_to(root.resolve()):
                raise ValueError("invalid_bundle_pointer")
            return bundle
        raise ModelUnavailable(family, "bundle_missing")

    def read(self, family: str, operation):
        try:
            root = self.bundle_path(family)
            if not (root / "manifest.json").is_file():
                raise ModelUnavailable(family, "bundle_missing")
            with MODULE_LOCK:
                query, survey, common, validators = model_modules(self.settings.model_root)
            manifest_hash = common.sha(root / "manifest.json")
            manifest = common.verify_bundle(root)
            required = ({"behavior_evidence.jsonl", "historical_reference.jsonl"} if family == "activity"
                        else {"sgg_isolation_index.jsonl", "dong_isolation_index.jsonl"})
            if not required.issubset(manifest["outputs"]):
                raise ValueError("untracked_required_output")
            if family == "survey" and manifest.get("contract_version") != SURVEY_CONTRACT:
                raise ValueError("unsupported_survey_contract")
            if family == "activity" and manifest.get("provenance", {}).get("model_version") != ACTIVITY_CONTRACT:
                raise ValueError("unsupported_activity_contract")
            result = operation(root, manifest, query, survey, common, validators)
            if common.sha(root / "manifest.json") != manifest_hash:
                raise ValueError("bundle_changed_during_query")
            return result
        except ModelUnavailable:
            raise
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, ImportError, AttributeError, ValidationError) as exc:
            # Never expose local paths or source data in HTTP error responses.
            logger.warning("%s model unavailable: %s", family, type(exc).__name__)
            raise ModelUnavailable(family, "bundle_invalid") from exc

    def behavior(self, **filters):
        from donghaeng_agent.signal_rules import (
            SIGNAL_RULE_VERSION, THRESHOLD_PERCENT, classify_activity, previous_month,
        )

        def read(root, manifest, query, survey, common, validators):
            result = query.query(root, **filters)
            rows = common.read_jsonl(root / "behavior_evidence.jsonl")
            histories = common.read_jsonl(root / "historical_reference.jsonl")
            ids = set()
            for row in histories:
                validators["historical"].validate(row)
                if row["record_id"] in ids:
                    raise ValueError("duplicate_historical_key")
                ids.add(row["record_id"])
            by_key = {}
            for row in rows:
                validators["behavior"].validate(row)
                key = tuple(row["key"][k] for k in ("region_code", "month", "sex_code", "age_code"))
                if key in by_key or row["historical_reference_id"] is not None and row["historical_reference_id"] not in ids:
                    raise ValueError("invalid_behavior_key_or_history")
                by_key[key] = row
            # Use the full verified bundle for continuity, even when this page
            # filters out the preceding month. The original model flags remain
            # untouched; candidate_only still refers to that original contract.
            for item in result["records"]:
                row = item["behavior"]
                key = row["key"]
                identity = tuple(key[name] for name in ("region_code", "month", "sex_code", "age_code"))
                if by_key.get(identity) != row:
                    raise ValueError("behavior_changed_during_query")
                prior = by_key.get((key["region_code"], previous_month(key["month"]), key["sex_code"], key["age_code"]))
                item["activity_assessment"] = classify_activity(row, previous_behavior=prior)
            result["classification_rule"] = {"rule_version": SIGNAL_RULE_VERSION, "threshold_percent": THRESHOLD_PERCENT}
            result["manifest_sha256"] = common.sha(root / "manifest.json")
            return result
        return self.read("activity", read)

    def survey_rows(self, root, manifest, common, validators):
        # Output bytes are hash-verified by read() on every request. Only the expensive
        # whole-bundle schema validation is reused for the exact same output hashes.
        signature = str(root), tuple(sorted(manifest["outputs"].items()))
        with self._survey_lock:
            if self._survey_cache and self._survey_cache[0] == signature:
                return self._survey_cache[1]
            keys, rows = set(), []
            for filename in ("sgg_isolation_index.jsonl", "dong_isolation_index.jsonl"):
                for row in common.read_jsonl(root / filename):
                    validators["survey"].validate(row)
                    key = row["spatial_unit"], row["area_code"], row["month"]
                    if key in keys or row["isolation_index"] != row["i5_reference"]:
                        raise ValueError("invalid_survey_key_or_score")
                    keys.add(key)
                    rows.append(row)
            self._survey_cache = signature, rows
            return rows

    def survey_area(self, province_code=None, **filters):
        def read(root, manifest, query, survey, common, validators):
            result = survey.query(root, **filters)
            rows = self.survey_rows(root, manifest, common, validators)
            if province_code is not None:
                # Filter the verified model rows before paging; a province may exceed 500 dongs.
                selected = [row for row in rows if row["spatial_unit"] == filters.get("level", "sgg")
                            and row["region_code"].startswith(province_code)
                            and all(value is None or row[key] == value for key, value in filters.items()
                                    if key in ("region_code", "month", "dong_code"))]
                offset, limit = filters.get("offset", 0), filters.get("limit", 100)
                result.update(records=selected[offset:offset + limit], total_matched=len(selected),
                              next_offset=offset + limit if offset + limit < len(selected) else None,
                              availability=survey.availability(selected, filters.get("region_code"), filters.get("month")))
            result["availability"]["province_code"] = province_code
            return {**result, "run_id": manifest["run_id"]}
        return self.read("survey", read)

    def regions(self, month=None):
        def read(root, manifest, query, survey, common, validators):
            rows = [row for row in self.survey_rows(root, manifest, common, validators)
                    if month is None or row["month"] == month]
            regions, dongs = {}, {}
            for row in rows:
                code = row["region_code"]
                if row["spatial_unit"] == "sgg":
                    regions[code] = {"code": code, "name": row["region_name"], "province_code": code[:2],
                                     "province_name": PROVINCES[code[:2]]}
                else:
                    dongs.setdefault(code, set()).add(row["area_code"])
            records = [{**region, "dong_count": len(dongs.get(code, set()))} for code, region in sorted(regions.items())]
            return {"run_id": manifest["run_id"], "regions": records,
                    "months": sorted({row["month"] for row in self.survey_rows(root, manifest, common, validators)}),
                    "geographic_scope": manifest.get("geographic_scope", "published_bundle_only")}
        return self.read("survey", read)

    def status(self):
        sources = {}
        for family, reader in (("activity", self.behavior), ("survey", self.survey_area)):
            try:
                result = reader(limit=1)
                sources[family] = {"ready": True, "code": "ready", "run_id": result["run_id"],
                                   "contract_version": result["contract_version"]}
            except ModelUnavailable as exc:
                sources[family] = {"ready": False, "code": exc.code}
        return {"mode": "final", "status": "ok" if all(s["ready"] for s in sources.values()) else "partial" if any(s["ready"] for s in sources.values()) else "unavailable",
                "sources": sources,
                "months": MONTHS, "sex_codes": ["MAN", "WMAN"], "age_codes": ["20G", "30G", "40G", "50G", "60GU"],
                "quality_status": "hold", "operational_alert_allowed": False}
