"""SQLite implementation of the Notion ERD's core, not a Supabase deployment.

Official category identifiers are source-scoped. I5 links are independent.
Legacy internal nine-tag records are deliberately not auto-converted.
"""
import json
import sqlite3
from datetime import date
from contextlib import closing
from pathlib import Path

DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE sources(source_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE programs(program_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources,
 source_program_id TEXT NOT NULL, UNIQUE(source_id,source_program_id));
CREATE TABLE program_versions(version_id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES programs,
 title TEXT NOT NULL, support_content TEXT NOT NULL, source_verified INTEGER NOT NULL,
 content_review TEXT NOT NULL CHECK(content_review IN ('match','mismatch','unknown')),
 start_date TEXT, end_date TEXT, application_status TEXT NOT NULL, source_url TEXT);
CREATE TABLE source_categories(category_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources,
 source_code TEXT NOT NULL, label TEXT NOT NULL, UNIQUE(source_id,source_code));
CREATE TABLE program_source_categories(version_id TEXT REFERENCES program_versions,
 category_id TEXT REFERENCES source_categories, PRIMARY KEY(version_id,category_id));
CREATE TABLE category_relations(from_id TEXT REFERENCES source_categories, to_id TEXT REFERENCES source_categories,
 relation TEXT CHECK(relation IN ('equivalent','related')), review_status TEXT NOT NULL,
 PRIMARY KEY(from_id,to_id));
CREATE TABLE i5_domains(code TEXT PRIMARY KEY CHECK(code IN ('E','P','O','C','U')));
CREATE TABLE program_i5_domains(version_id TEXT REFERENCES program_versions, domain TEXT REFERENCES i5_domains,
 reason TEXT NOT NULL, review_status TEXT NOT NULL, rule_version TEXT NOT NULL,
 PRIMARY KEY(version_id,domain));
CREATE TABLE administrative_regions(region_id TEXT PRIMARY KEY, code_system TEXT NOT NULL,
 code TEXT NOT NULL, name TEXT NOT NULL, parent_id TEXT REFERENCES administrative_regions,
 UNIQUE(code_system,code));
CREATE TABLE program_regions(version_id TEXT REFERENCES program_versions,
 scope_type TEXT NOT NULL CHECK(scope_type IN ('nationwide','specific','unknown','conflicting')),
 region_id TEXT REFERENCES administrative_regions,
 CHECK((scope_type='specific' AND region_id IS NOT NULL) OR (scope_type!='specific' AND region_id IS NULL)));
CREATE TABLE eligibility_rules(version_id TEXT REFERENCES program_versions, kind TEXT NOT NULL,
 value_json TEXT NOT NULL, evidence_id TEXT NOT NULL);
CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, version_id TEXT REFERENCES program_versions,
 quote TEXT NOT NULL, source_url TEXT, verification_scope TEXT NOT NULL);
CREATE TABLE publications(publication_id TEXT PRIMARY KEY, is_synthetic INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','published')));
CREATE TABLE publication_members(publication_id TEXT REFERENCES publications,
 version_id TEXT REFERENCES program_versions, PRIMARY KEY(publication_id,version_id));
CREATE INDEX category_lookup ON program_source_categories(category_id,version_id);
CREATE INDEX domain_lookup ON program_i5_domains(domain,version_id);
"""


class ERDWelfareDB:
    def __init__(self, path, publication_id, *, allow_synthetic=False):
        self.path = Path(path)
        self.publication_id = publication_id
        self.allow_synthetic = allow_synthetic

    def search(self, *, category_ids=(), i5_domains=(), keyword=None):
        if not set(i5_domains) <= set("EPOCU"):
            raise ValueError("invalid I5 domain")
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            publication = db.execute("SELECT * FROM publications WHERE publication_id=?", (self.publication_id,)).fetchone()
            if publication is None or publication["status"] != "published":
                raise ValueError("publication unavailable")
            synthetic = bool(publication["is_synthetic"])
            if synthetic and not self.allow_synthetic:
                raise ValueError("synthetic corpus requires opt-in")
            for cid in category_ids:
                if not db.execute("SELECT 1 FROM source_categories WHERE category_id=?", (cid,)).fetchone():
                    raise ValueError("unknown source category")
            rows = db.execute("SELECT v.* FROM program_versions v JOIN publication_members m "
                              "ON v.version_id=m.version_id WHERE m.publication_id=? ORDER BY v.version_id",
                              (self.publication_id,)).fetchall()
            results, unclassified = [], 0
            for row in rows:
                vid = row["version_id"]
                categories = [dict(r) for r in db.execute("SELECT c.* FROM source_categories c JOIN "
                    "program_source_categories p ON c.category_id=p.category_id WHERE p.version_id=?", (vid,))]
                links = [dict(r) for r in db.execute("SELECT * FROM program_i5_domains WHERE version_id=?", (vid,))]
                unclassified += not bool(categories)
                actual = {c["category_id"] for c in categories}
                reasons = []
                for cid in category_ids:
                    if cid in actual:
                        reasons.append({"type": "exact_category", "category_id": cid, "rank": 1})
                    for rel in db.execute("SELECT * FROM category_relations WHERE from_id=? AND review_status='approved'", (cid,)):
                        if rel["to_id"] in actual:
                            reasons.append({"type": rel["relation"], "category_id": rel["to_id"], "rank": 2})
                if keyword and keyword.casefold() in (row["title"] + " " + row["support_content"]).casefold():
                    reasons.append({"type": "content_keyword", "keyword": keyword, "rank": 3})
                for link in links:
                    if link["domain"] in i5_domains and (link["review_status"] == "approved" or synthetic and link["review_status"] == "draft"):
                        reasons.append({"type": "i5_link", "domain": link["domain"],
                                        "reason": link["reason"], "rule_version": link["rule_version"],
                                        "review_status": link["review_status"], "rank": 3})
                if not reasons:
                    continue
                results.append({**dict(row), "source_categories": categories, "i5_links": links,
                    "search_reasons": reasons,
                    "regions": [dict(r) for r in db.execute("SELECT * FROM program_regions WHERE version_id=?", (vid,))],
                    "conditions": [{**dict(r), "value": json.loads(r["value_json"])} for r in db.execute(
                        "SELECT * FROM eligibility_rules WHERE version_id=?", (vid,))],
                    "evidence": [dict(r) for r in db.execute("SELECT * FROM evidence WHERE version_id=?", (vid,))],
                    "is_synthetic": synthetic})
        results.sort(key=lambda p: (min(r["rank"] for r in p["search_reasons"]), p["version_id"]))
        return {"status": "ok", "publication_id": self.publication_id, "is_synthetic": synthetic,
                "programs": results, "coverage": {"total": len(rows), "matched": len(results), "unclassified": unclassified}}


def review_candidates(search_result, *, region_ids, age_range, as_of):
    """Only verified caller-supplied region IDs; never infer hierarchy by prefix."""
    day = date.fromisoformat(as_of)
    output = []
    for p in search_result['programs']:
        excluded, unknown = [], ['개인별 최종 자격 및 현재 접수 여부']
        scopes = p['regions']
        if not scopes or any(r['scope_type'] in ('unknown','conflicting') for r in scopes):
            unknown.append('적용 지역')
        elif not any(r['scope_type']=='nationwide' or r['region_id'] in region_ids for r in scopes):
            excluded.append('적용 지역 불일치')
        for rule in p['conditions']:
            value=rule['value']
            if rule['kind']=='age' and value is not None:
                if age_range[1] < value[0] or age_range[0] > value[1]:
                    excluded.append('연령 불일치')
                elif not (value[0] <= age_range[0] <= age_range[1] <= value[1]):
                    unknown.append('연령 구간 일부만 해당')
            elif value is None:
                unknown.append(rule['kind'])
        if p['application_status']=='closed':
            excluded.append('접수 종료')
        for field, comparison in [('start_date',lambda x: day<x),('end_date',lambda x: day>x)]:
            if p[field] is None:
                unknown.append(field)
            elif comparison(date.fromisoformat(p[field])):
                excluded.append('사업 기간 불일치')
        if p['content_review']=='mismatch':
            excluded.append('지원내용 불일치')
        held = not p['source_verified'] or p['content_review']!='match' or not p['evidence']
        if held:
            unknown.append('원문 및 지원내용 확인')
        output.append({**p, 'status':'excluded' if excluded else 'source_hold' if held else 'needs_confirmation',
            'recommendation_reasons':p['search_reasons'], 'unconfirmed_conditions':unknown,
            'exclusion_reasons':excluded})
    return {**search_result,'programs':output,'quality_status':'hold','operational_alert_allowed':False}


def create_demo(path):
    """New file only. Labels/codes are fixtures, not an official category catalog."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(DDL)
        db.execute("INSERT INTO sources VALUES ('demo-api','가상 API; 공식 수집 아님')")
        db.executemany("INSERT INTO i5_domains VALUES (?)", [(x,) for x in "EPOCU"])
        db.execute("INSERT INTO administrative_regions VALUES ('demo-gangnam','KR_SGG','11680','강남구',NULL)")
        for code, label in [('life','생활지원'),('job','일자리'),('participation','참여 지원(가상)')]:
            db.execute("INSERT INTO source_categories VALUES (?,?,?,?)", ('demo:'+code,'demo-api',code,label))
        db.execute("INSERT INTO category_relations VALUES ('demo:life','demo:participation','related','approved')")
        db.execute("INSERT INTO publications VALUES ('erd-demo-v2',1,'published')")
        for key, categories, domains, scope, region, ages, end, verified, content in [
            ('social',['life'],['P','C','U'],'specific','demo-gangnam',[19,39],'2026-12-31',1,'집단 활동과 안부 확인'),
            ('work',['job','participation'],['P','E'],'nationwide',None,[19,39],'2026-12-31',1,'집단 일경험과 동료 교류'),
            ('expired',['life'],['U'],'nationwide',None,[19,39],'2026-09-30',1,'생활 지원'),
            ('unknown',['life'],['U'],'unknown',None,None,None,0,'상세 미확인'),
            ('unclassified',[],[],'unknown',None,None,None,0,'미분류 자료'),
        ]:
            vid=key+':v1'
            db.execute("INSERT INTO programs VALUES (?,?,?)", (key,'demo-api',key))
            db.execute("INSERT INTO program_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (vid,key,'[가상] '+key,content,verified,'match' if verified else 'unknown','2026-01-01',end,'open',None))
            db.executemany("INSERT INTO program_source_categories VALUES (?,?)", [(vid,'demo:'+c) for c in categories])
            db.executemany("INSERT INTO program_i5_domains VALUES (?,?,?,?,?)",
                           [(vid,d,'가상 사업 내용 기반 연결 시험','draft','demo-link-v1') for d in domains])
            db.execute("INSERT INTO program_regions VALUES (?,?,?)", (vid,scope,region))
            db.execute("INSERT INTO evidence VALUES (?,?,?,?,?)", ('fixture:'+key,vid,content,None,'synthetic_fixture_only'))
            for kind, value in [('age',ages),('sex',None),('personal_eligibility',None)]:
                db.execute("INSERT INTO eligibility_rules VALUES (?,?,?,?)", (vid,kind,json.dumps(value),'fixture:'+key))
            db.execute("INSERT INTO publication_members VALUES (?,?)", ('erd-demo-v2',vid))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("foreign key violation")


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--output', required=True)
    args=parser.parse_args()
    if not Path(args.db).exists():
        create_demo(args.db)
    result=ERDWelfareDB(args.db,'erd-demo-v2',allow_synthetic=True).search(category_ids=['demo:life'],i5_domains=['P','U'])
    result=review_candidates(result,region_ids={'demo-gangnam'},age_range=(20,29),as_of='2026-10-25')
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result['coverage']))
