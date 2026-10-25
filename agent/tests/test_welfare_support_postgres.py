"""Service search must retain provenance, pagination and installation boundaries."""
from contextlib import nullcontext
from datetime import date
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from donghaeng_agent import welfare_postgres as module
from donghaeng_agent.welfare_support_mapping import RULE_VERSION, RULE_DEFINITION, extract_program_services
from test_welfare_postgres import program_row, publication_rows


def test_mapping_requires_tables_and_completed_matching_rule():
    db=Mock()
    db.execute.return_value.fetchone.return_value={'rules':False,'runs':False,'actions':False}
    assert module.PostgresWelfareDB._support_mapping_status(db,['publication'])['mapping_status']=='unconfigured'
    assert db.execute.call_count==1
    db.reset_mock()
    db.execute.return_value.fetchone.side_effect=[{'rules':True,'runs':True,'actions':True},None]
    assert module.PostgresWelfareDB._support_mapping_status(db,['publication'])['mapping_status']=='unconfigured'
    query,args=db.execute.call_args.args
    assert "s.definition_sha256=%s" in query and "r.status='completed'" in query
    assert args[0]==RULE_VERSION and len(args[1])==64 and args[2]==['publication']


def test_service_search_binds_priority_and_counts_all_pages(monkeypatch):
    db=Mock()
    row=program_row()
    db.execute.return_value.fetchone.side_effect=[{'matched':203},{'mapped':4000}]
    db.execute.return_value.fetchall.return_value=[row]
    monkeypatch.setattr(module,'postgres_connection',lambda:nullcontext(db))
    monkeypatch.setattr(module.PostgresWelfareDB,'_publications',lambda self,db:module._serializable(publication_rows()))
    monkeypatch.setattr(module.PostgresWelfareDB,'_support_mapping_status',staticmethod(lambda db,ids:{
        'mapping_status':'configured_research','rule_version':RULE_VERSION,'mapping_reason':'연구용'}))
    monkeypatch.setattr(module.PostgresWelfareDB,'_stats',staticmethod(lambda db,base:{'total':16244,'unclassified':3937}))
    monkeypatch.setattr(module.PostgresWelfareDB,'_children',staticmethod(lambda db,ids:({}, {}, {}, {}, {})))
    link={'category_code':'social_connection','service_action':'교류 프로그램 지원',
          'evidence_id':'support:example','rule_version':RULE_VERSION,'review_status':'rule_extracted'}
    monkeypatch.setattr(module.PostgresWelfareDB,'_support_links',staticmethod(lambda db,ids,version:{str(row['program_version_id']):[link]}))
    result=module.PostgresWelfareDB().search_support(category_ids=['social_connection','income_support'],
        as_of=date(2026,10,25),offset=100,limit=100)
    assert result['next_offset']==200 and result['coverage']['matched']==203
    assert result['coverage']['support_unmapped']==12244 and result['coverage']['complete'] is False
    assert result['programs'][0]['support_links']==[link]
    assert result['programs'][0]['search_reasons'][0]['evidence_id']=='support:example'
    query,args=db.execute.call_args_list[1].args
    assert 'ORDER BY support_category_rank,support_region_rank' in query
    assert args[2]==['social_connection','income_support']
    assert 'social_connection' not in query
    assert args[-2:]==[100,100]


def test_backfill_is_deterministic_and_rejects_fabricated_quotes():
    path=Path(__file__).resolve().parents[2]/'scripts/apply_welfare_support_mapping.py'
    spec=importlib.util.spec_from_file_location('support_backfill',path)
    backfill=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)
    row={'program_version_id':'00000000-0000-4000-8000-000000000001',
         'title':'돌봄사업','support_content':'식사 지원 서비스를 제공합니다.', 'content_hash':'version-hash'}
    first=backfill.prepare([row],['publication'],RULE_DEFINITION,RULE_VERSION,extract_program_services)
    again=backfill.prepare([row],['publication'],RULE_DEFINITION,RULE_VERSION,extract_program_services)
    assert first==again and first['mapped_programs']==1
    republished=backfill.prepare([row],['new-publication'],RULE_DEFINITION,RULE_VERSION,extract_program_services)
    assert republished['corpus_sha256']==first['corpus_sha256']
    assert republished['run_id']!=first['run_id']
    assert republished['actions']==first['actions']
    def fabricated(program):
        action=extract_program_services(program)[0]
        return [{**action,'source_quote':'원문에 없는 내용'}]
    with pytest.raises(ValueError,match='extraction_quote_mismatch'):
        backfill.prepare([row],['publication'],RULE_DEFINITION,RULE_VERSION,fabricated)
