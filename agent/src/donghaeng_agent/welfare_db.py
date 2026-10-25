"""Replaceable SQLite prototype adapter. Synthetic data require explicit opt-in."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .welfare_mapping import TAGS


class WelfareDB:
    def __init__(self, path, *, allow_synthetic=False):
        self.path = Path(path)
        self.allow_synthetic = allow_synthetic

    def create(self, programs, *, corpus_version, is_synthetic):
        # Exclusive creation prevents accidental replacement of a real corpus.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE corpus(version TEXT NOT NULL, is_synthetic INTEGER NOT NULL);
                CREATE TABLE programs(program_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE program_tags(program_id TEXT NOT NULL REFERENCES programs(program_id),
                    tag TEXT NOT NULL, PRIMARY KEY(program_id, tag));
                CREATE INDEX tags_lookup ON program_tags(tag);
            """)
            db.execute("INSERT INTO corpus VALUES (?,?)", (corpus_version, int(is_synthetic)))
            for p in programs:
                if not set(p.get("program_tags", [])) <= TAGS:
                    raise ValueError("invalid tag")
                if not p.get("program_version_id"):
                    raise ValueError("missing program version")
                db.execute("INSERT INTO programs VALUES (?,?)", (p["program_id"], json.dumps(p, ensure_ascii=False)))
                db.executemany("INSERT INTO program_tags VALUES (?,?)",
                               [(p["program_id"], tag) for tag in sorted(set(p.get("program_tags", [])))])

    def search_welfare(self, *, welfare_fields, match_mode="or"):
        if match_mode != "or" or not set(welfare_fields) <= TAGS:
            raise ValueError("unsupported query")
        # mode=ro: a typo cannot create an empty replacement database.
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            version, synthetic = db.execute("SELECT version,is_synthetic FROM corpus").fetchone()
            if synthetic and not self.allow_synthetic:
                raise ValueError("synthetic corpus requires explicit research opt-in")
            count = db.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
            unclassified = db.execute("SELECT COUNT(*) FROM programs p WHERE NOT EXISTS "
                                     "(SELECT 1 FROM program_tags t WHERE t.program_id=p.program_id)").fetchone()[0]
            tags = sorted(set(welfare_fields))
            rows = db.execute("SELECT p.payload FROM programs p WHERE EXISTS "
                              "(SELECT 1 FROM program_tags t WHERE t.program_id=p.program_id AND t.tag IN ("
                              + ",".join("?" for _ in tags) + ")) ORDER BY p.program_id", tags).fetchall() if tags else []
        programs = [json.loads(row[0]) for row in rows]
        for p in programs:
            p["is_synthetic"] = bool(synthetic)
            p["verification_scope"] = "synthetic_fixture_only" if synthetic else "adapter_supplied"
        return {"status": "ok", "programs": programs, "corpus_version": version,
                "is_synthetic": bool(synthetic), "coverage": {"total_programs": count,
                "unclassified": unclassified, "matched": len(programs), "complete": True,
                "source_kind": "synthetic_fixture" if synthetic else "imported_corpus"}}


def demo_programs():
    """Fictional records, not fetched official programs or validated mappings."""
    result = []
    for tag in sorted(TAGS):
        result.append({"program_id": "demo-" + tag, "program_version_id": "demo-v1",
            "title": "[가상] " + tag, "support_content": "합성 시험용 지원내용: " + tag,
            "source_category": "synthetic", "source_url": None, "retrieved_at": None,
            "program_tags": [tag], "source_verified": True,
            "content_matches": {tag: {"status": "match", "reason": "가상 지원내용 일치 시험",
                                      "evidence_ids": ["fixture:" + tag]}},
            "region_codes": ["11680"], "age_range": [19, 39],
            "start_date": "2026-01-01", "end_date": "2026-12-31",
            "application_status": "open", "personal_conditions": ["개인별 자격"]})
    base = next(p for p in result if p["program_tags"] == ["social_connection"])
    for suffix, patch in [
        ("wrong-region", {"region_codes": ["51110"]}),
        ("expired", {"end_date": "2026-09-30"}),
        ("wrong-age", {"age_range": [60, 99]}),
        ("content-mismatch", {"content_matches": {"social_connection": {
            "status": "mismatch", "reason": "태그는 있으나 내용 불일치 시험", "evidence_ids": ["fixture:mismatch"]}}}),
        ("unverified", {"source_verified": False}),
        ("employment-social", {"program_tags": ["employment", "social_connection"],
                                "personal_conditions": ["구직 의사", "미취업 여부"]}),
        ("unclassified", {"program_tags": [], "content_matches": {}}),
    ]:
        result.append({**base, **patch, "program_id": "demo-" + suffix, "title": "[가상] " + suffix})
    return result


def main():
    import argparse
    from .welfare_mapping import MappingRule, select_fields, recommend
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    store = WelfareDB(args.db, allow_synthetic=True)
    if not store.path.exists():
        store.create(demo_programs(), corpus_version="synthetic-welfare-v1", is_synthetic=True)
    rule = MappingRule("DEMO-ONLY", "v1", "draft", ("demo_activity",), ("demo_i5",),
                       ("social_connection",), "합성 조건으로 DB 추천 흐름 검증; 실제 I5 임계값 아님")
    mapping = select_fields([rule], {"demo_activity": True, "demo_i5": True},
                            {"demo_activity": ["fixture:activity"], "demo_i5": ["fixture:i5"]})
    result = recommend(mapping, store.search_welfare, region_codes={"11680"},
                       age_range=(20, 29), support_as_of="2026-10-25")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"mapping": mapping, "result": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"database": str(store.path), "output": str(output),
                      "is_synthetic": result["is_synthetic"], "coverage": result["coverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
