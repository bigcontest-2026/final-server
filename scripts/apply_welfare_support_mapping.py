"""Audit or atomically install evidence-backed research service mappings.

The normal application connection remains read-only. This explicit maintenance
command alone opens a write transaction when --apply is supplied. Existing
programs, category tags, publications and evidence are never modified.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent/src'))


DDL = '''
CREATE TABLE IF NOT EXISTS support_mapping_rule_sets (
    rule_version text PRIMARY KEY,
    definition jsonb NOT NULL,
    definition_sha256 text NOT NULL CHECK (length(definition_sha256)=64),
    review_status text NOT NULL CHECK (review_status='research_rule'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS support_mapping_runs (
    run_id uuid PRIMARY KEY,
    rule_version text NOT NULL REFERENCES support_mapping_rule_sets(rule_version),
    publication_ids uuid[] NOT NULL,
    corpus_sha256 text NOT NULL CHECK (length(corpus_sha256)=64),
    total_programs integer NOT NULL CHECK (total_programs>=0),
    mapped_programs integer NOT NULL CHECK (mapped_programs>=0 AND mapped_programs<=total_programs),
    action_count integer NOT NULL CHECK (action_count>=0),
    status text NOT NULL CHECK (status='completed'),
    completed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE support_mapping_runs
    DROP CONSTRAINT IF EXISTS support_mapping_runs_rule_version_corpus_sha256_key;
CREATE UNIQUE INDEX IF NOT EXISTS support_runs_snapshot
    ON support_mapping_runs(rule_version,corpus_sha256,publication_ids);
CREATE TABLE IF NOT EXISTS program_support_actions (
    support_evidence_id uuid PRIMARY KEY,
    program_version_id uuid NOT NULL REFERENCES program_versions(program_version_id),
    category_code text NOT NULL REFERENCES categories(category_code),
    service_action text NOT NULL,
    rule_version text NOT NULL REFERENCES support_mapping_rule_sets(rule_version),
    rule_id text NOT NULL,
    source_field text NOT NULL CHECK (source_field IN ('support_content','title')),
    source_quote text NOT NULL CHECK (length(source_quote)>0),
    quote_start integer NOT NULL CHECK (quote_start>=0),
    quote_end integer NOT NULL CHECK (quote_end>quote_start),
    source_text_sha256 text NOT NULL CHECK (length(source_text_sha256)=64),
    extraction_reason text NOT NULL,
    review_status text NOT NULL CHECK (review_status IN ('rule_extracted','reviewed','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(program_version_id,rule_version,category_code,service_action)
);
CREATE INDEX IF NOT EXISTS support_actions_lookup
    ON program_support_actions(rule_version,category_code,service_action,program_version_id);
CREATE INDEX IF NOT EXISTS support_runs_publications
    ON support_mapping_runs USING gin(publication_ids);
REVOKE ALL ON support_mapping_rule_sets,support_mapping_runs,program_support_actions FROM PUBLIC;
'''

CORPUS_SQL = '''SELECT DISTINCT v.program_version_id,v.program_id,v.title,v.support_content,
    v.target_text,v.selection_text,v.content_hash,p.source_id
    FROM active_publications a
    JOIN publications pub ON pub.publication_id=a.publication_id AND pub.source_id=a.source_id
    JOIN publication_members m ON m.publication_id=pub.publication_id
    JOIN program_versions v ON v.program_version_id=m.program_version_id
    JOIN programs p ON p.program_id=v.program_id AND p.source_id=pub.source_id
    JOIN sources s ON s.source_id=p.source_id
    WHERE pub.publication_status='published' AND s.is_active
    ORDER BY v.program_version_id'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def prepare(rows, publications, definition, rule_version, extractor):
    """Reject any extraction that cannot be traced to exact supplied text."""
    corpus_hash = digest(canonical([
        (str(row['program_version_id']), row['content_hash'],
         digest(row['support_content'] or ''), digest(row['title'])) for row in rows]))
    run_id = str(uuid5(NAMESPACE_URL, canonical([rule_version, publications, corpus_hash])))
    actions, seen, mapped, counts = [], set(), set(), Counter()
    for row in rows:
        program = dict(row)
        program['program_version_id'] = str(program['program_version_id'])
        for action in extractor(program):
            field, start, end = action['source_field'], action['start'], action['end']
            if field not in ('support_content', 'title') or type(start) is not int or type(end) is not int:
                raise ValueError('invalid_extraction_locator')
            source = program[field] or ''
            if not 0 <= start < end <= len(source) or source[start:end] != action['source_quote']:
                raise ValueError('extraction_quote_mismatch')
            if action.get('rule_version') != rule_version or action.get('review_status') != 'rule_extracted':
                raise ValueError('invalid_extraction_version_or_status')
            key = (program['program_version_id'], rule_version, action['category_code'], action['service_action'])
            if key in seen:
                raise ValueError('duplicate_service_action')
            seen.add(key)
            evidence_id = str(uuid5(NAMESPACE_URL, canonical(key)))
            actions.append((evidence_id, *key[:1], action['category_code'], action['service_action'],
                rule_version, action['rule_id'], field, action['source_quote'], start, end,
                digest(source), action['reason'], 'rule_extracted'))
            mapped.add(program['program_version_id'])
            counts[action['category_code']] += 1
    return {'run_id': run_id, 'rule_version': rule_version, 'definition': definition,
        'definition_sha256': digest(canonical(definition)), 'publication_ids': publications,
        'corpus_sha256': corpus_hash, 'total_programs': len(rows), 'mapped_programs': len(mapped),
        'unmapped_programs': len(rows)-len(mapped), 'action_count': len(actions),
        'categories': dict(sorted(counts.items())), 'actions': actions}


@contextmanager
def maintenance_connection(apply):
    from donghaeng_agent.welfare_connection import _connection_parameters, configured_schema
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    with psycopg.connect(**_connection_parameters(), row_factory=dict_row,
                         prepare_threshold=None) as db:
        db.read_only = not apply
        db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with db.transaction():
            db.execute(sql.SQL("SET LOCAL statement_timeout='90s'; SET LOCAL lock_timeout='10s'; "
                'SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(configured_schema())))
            yield db


def apply_plan(db, plan):
    from psycopg.types.json import Jsonb
    # Serialize maintenance writers. This never changes source-table contents.
    db.execute("SELECT pg_advisory_xact_lock(1919245168, 1937076304)")
    db.execute(DDL)
    stored = db.execute('SELECT definition_sha256 FROM support_mapping_rule_sets WHERE rule_version=%s',
                        (plan['rule_version'],)).fetchone()
    if stored and stored['definition_sha256'] != plan['definition_sha256']:
        raise ValueError('rule_version_definition_changed')
    db.execute('''INSERT INTO support_mapping_rule_sets
        (rule_version,definition,definition_sha256,review_status) VALUES (%s,%s,%s,'research_rule')
        ON CONFLICT(rule_version) DO NOTHING''',
        (plan['rule_version'], Jsonb(plan['definition']), plan['definition_sha256']))
    existing = db.execute('SELECT * FROM support_mapping_runs WHERE run_id=%s::uuid',
                          (plan['run_id'],)).fetchone()
    if existing:
        if any(existing[key] != plan[key] for key in ('total_programs','mapped_programs','action_count','corpus_sha256')):
            raise ValueError('existing_run_coverage_mismatch')
    # A versioned extraction must not silently replace previously reviewed rows.
    old_rows = db.execute('''SELECT support_evidence_id,program_version_id,category_code,service_action,
        rule_version,rule_id,source_field,source_quote,quote_start,quote_end,source_text_sha256,
        extraction_reason,review_status FROM program_support_actions WHERE rule_version=%s''',
        (plan['rule_version'],)).fetchall()
    old = {str(row['support_evidence_id']): row for row in old_rows}
    columns = ('support_evidence_id','program_version_id','category_code','service_action','rule_version',
               'rule_id','source_field','source_quote','quote_start','quote_end','source_text_sha256',
               'extraction_reason','review_status')
    for values in plan['actions']:
        previous = old.get(values[0])
        if previous and any(str(previous[name]) != str(value)
                            for name,value in zip(columns[:-1], values[:-1])):
            raise ValueError('existing_action_changed_requires_new_rule_version')
    if existing:
        if any(values[0] not in old for values in plan['actions']):
            raise ValueError('completed_mapping_run_missing_actions')
        return 'already_applied'
    if plan['actions']:
        with db.cursor() as cursor:
            cursor.executemany('''INSERT INTO program_support_actions
                (support_evidence_id,program_version_id,category_code,service_action,rule_version,rule_id,
                 source_field,source_quote,quote_start,quote_end,source_text_sha256,extraction_reason,review_status)
                VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(program_version_id,rule_version,category_code,service_action) DO NOTHING''', plan['actions'])
    db.execute('''INSERT INTO support_mapping_runs
        (run_id,rule_version,publication_ids,corpus_sha256,total_programs,mapped_programs,action_count,status)
        VALUES (%s::uuid,%s,%s::uuid[],%s,%s,%s,%s,'completed')''',
        (plan['run_id'],plan['rule_version'],plan['publication_ids'],plan['corpus_sha256'],
         plan['total_programs'],plan['mapped_programs'],plan['action_count']))
    return 'applied'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Atomically create additive tables and backfill')
    parser.add_argument('--output', type=Path, help='Write the non-secret summary after success')
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT/'service/backend/.env', override=False, interpolate=False)
    from donghaeng_agent.welfare_connection import WelfareDatabaseError
    from donghaeng_agent.welfare_support_mapping import RULE_VERSION, extract_program_services
    from donghaeng_agent.welfare_postgres import support_rule_definition
    import psycopg
    try:
        with maintenance_connection(args.apply) as db:
            publications = [str(row['publication_id']) for row in db.execute('''SELECT a.publication_id
                FROM active_publications a JOIN publications p ON p.publication_id=a.publication_id
                AND p.source_id=a.source_id JOIN sources s ON s.source_id=a.source_id
                WHERE p.publication_status='published' AND s.is_active ORDER BY a.publication_id''').fetchall()]
            if not publications:
                raise ValueError('no_active_publications')
            plan = prepare(db.execute(CORPUS_SQL).fetchall(), publications,
                           support_rule_definition(), RULE_VERSION, extract_program_services)
            state = apply_plan(db, plan) if args.apply else 'dry_run'
        summary = {key:value for key,value in plan.items() if key not in ('actions','definition')}
        summary.update(status=state, checked_at=datetime.now(timezone.utc).isoformat(),
            manual_review_performed=False, operational_alert_allowed=False)
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return 0
    except WelfareDatabaseError as exc:
        print(json.dumps({'status':'failed','code':exc.code,'message':str(exc)},ensure_ascii=False))
    except psycopg.Error as exc:
        print(json.dumps({'status':'failed','code':'database_error','sqlstate':exc.sqlstate},ensure_ascii=False))
    except ValueError as exc:
        print(json.dumps({'status':'failed','code':str(exc)},ensure_ascii=False))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
