from fastapi.testclient import TestClient
from test_response_api import application, base, save, generate, plan, editable
from test_analysis_api import report_settings


def test_omission_record_keeps_all_authored_fields(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)['report_id']
        entry = {'status': 'not_applicable', 'text': '현 단계 추가 예산 없음', 'evidence': '고유근거문서', 'owner': '고유확인역할', 'next_action': '고유후속조치'}
        workspace = save(client, parent, plans=[plan(planning_details={'budget': entry})])
        report = generate(client, parent, workspace).json()
        for value in entry.values():
            if value != 'not_applicable':
                assert value in report['markdown']


def test_planning_details_survive_old_client_and_report_snapshot(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)['report_id']
        entry = {'status':'unconfirmed','text':'기관 운영 가능 여부 확인','evidence':'','owner':'복지 담당 역할(안)','next_action':'기관 회신 후 실행 검토'}
        saved = save(client, parent, plans=[plan(planning_details={'execution':entry})])
        assert saved['plans'][0]['planning_details']['execution'] == entry
        old_request = [editable(saved['plans'][0])]
        old_request[0]['feedback'] = '보존 확인'
        revised = save(client, parent, saved['version'], old_request)
        assert revised['plans'][0]['planning_details']['execution'] == entry
        report = generate(client, parent, revised).json()
        assert report['document']['version'] == 'welfare-plan-2.0'
        assert report['document']['sections'][0]['id'] == 'summary'
        assert '기관 회신 후 실행 검토' in report['markdown']
        assert '고립 위험의 증가 여부를 판단할 수 없습니다' in report['markdown']


def test_planning_detail_confirmation_requires_evidence_and_na_reason(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)['report_id']
        for entry in ({'status':'confirmed','text':'확인','evidence':''}, {'status':'not_applicable','text':''}):
            response = client.put(f'/api/model/analysis-reports/{parent}/response-workspace', json={'expected_version':0,'plans':[plan(planning_details={'budget':entry})]})
            assert response.status_code == 422


def test_ai_planning_suggestions_are_unconfirmed_and_nested_claims_checked(report_settings, monkeypatch):
    import json
    from unittest.mock import Mock
    from donghaeng_agent import response_assistance
    from test_response_assistance_api import model_result
    from test_response_api import base, application
    def run_entry(entry, expected):
        def result(messages, **kwargs):
            response = model_result(messages, **kwargs)
            body = json.loads(response['text'])
            body['suggestion']['planning_details'] = {'budget':entry}
            response['text'] = json.dumps(body, ensure_ascii=False)
            return response
        monkeypatch.setattr(response_assistance, 'create_provider', lambda:Mock(decide=result))
        with TestClient(application(report_settings)) as client:
            parent = base(client)['report_id']
            response = client.post(f'/api/model/analysis-reports/{parent}/response-workspace/assist',json={'mode':'draft','kind':'new_program','instruction':'초안 제안'})
            assert response.status_code == expected, response.text
    run_entry({'status':'unconfirmed','text':'비용 산정 필요','owner':'담당 역할(안)','next_action':'단가 근거 확인'}, 200)
    run_entry({'status':'confirmed','text':'예산 확보','evidence':'기관 확인'}, 503)
    run_entry({'status':'unconfirmed','text':'지원금 100만원을 지급합니다.'}, 503)


def test_applied_modules_and_authored_details_are_preserved_on_type_change(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)['report_id']
        saved = save(client, parent, plans=[plan(kind='new_program', planning_details={'budget':{'status':'not_applicable','text':'추가 사업비 없는 기존 자원 검토'}})])
        item = editable(saved['plans'][0])
        item['kind'] = 'existing_program'
        changed = save(client, parent, saved['version'], [item])
        result = generate(client, parent, changed).json()
        assert result['document']['metadata'][0]['value'] == 'A'
        assert '추가 사업비 없는 기존 자원 검토' in result['markdown']
        unknown = client.put(f'/api/model/analysis-reports/{parent}/response-workspace',json={'expected_version':changed['version'],'plans':[dict(item, planning_details={'unknown':{'text':'값'}})]})
        assert unknown.status_code == 422


def test_confirmed_decision_and_a_extra_cost_are_in_canonical_document(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client)['report_id']
        saved = save(client, parent, plans=[plan(kind='existing_program', planning_details={
            'decisions': {'status':'confirmed','text':'기존 공간의 연계 검토를 선택함','evidence':'검토회의 기록 1쪽','owner':'담당 역할','next_action':'기관 회신 확인'},
            'budget': {'status':'unconfirmed','text':'추가 업무 비용 산정 필요'},
            'funding': {'status':'not_applicable','text':'현 단계는 재원 신청 대상 아님'},
        })])
        report = generate(client, parent, saved).json()
        document = report['document']
        assert '추가 업무 비용 산정 필요' in document['sections'][0]['paragraphs'][-2]
        decisions = next(section for section in document['sections'] if section['id'] == 'decisions')
        assert any('검토회의 기록 1쪽' in paragraph for paragraph in decisions['paragraphs'])
        plan_body = next(section for section in document['sections'] if section['id'] == 'plan-1')
        assert any('현 단계는 재원 신청 대상 아님' in paragraph for paragraph in plan_body['paragraphs'])
        assert not any('확인할 내용 미기재' in paragraph for paragraph in plan_body['paragraphs'])
        assert all(limit in report['markdown'] for limit in report['limitations'])


def test_scope_labels_and_aggregate_observations_identify_each_group(report_settings):
    with TestClient(application(report_settings)) as client:
        parent = base(client, sex_code=None, age_code=None)['report_id']
        report = generate(client, parent, save(client, parent)).json()
        analysis = next(section for section in report['document']['sections'] if section['id'] == 'analysis')
        text = '\n'.join(analysis['paragraphs'])
        assert '남성 · 20대' in text and '여성 · 60세 이상' in text
        assert 'MAN' not in text and 'WMAN' not in text and '20G' not in text
        assert '10개 집단' in report['document']['sections'][0]['paragraphs'][0]
