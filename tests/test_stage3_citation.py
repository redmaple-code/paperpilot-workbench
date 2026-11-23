import json
import uuid

import app
from paperpilot.services import citation_engineering, citation_semantic
from tests.test_req013_citation_support import _project, _linked_reference, _attach_full_text


def setup_review(client):
    pid=_project(client)
    ref=_linked_reference(client,pid)
    _attach_full_text(client,pid,ref,'Graphene stability improves under controlled conditions.')
    base=f'/api/projects/{pid}'
    text=r'Graphene stability improves under controlled conditions\cite{graphene2024}. Another finding\cite{graphene2024}.'
    assert client.put(base+'/editor/draft',json={'path':'main.tex','base_version':1,'base_content':'','content':text}).status_code==200
    assert client.put(base+f'/citation-review/source-profiles/{ref}',json={'expected_version':0,'parse_reliability':'verified','source_role':'primary'}).status_code==200
    opts={'request_id':uuid.uuid4().hex,'input_kind':'draft','target_paths':['main.tex'],'draft_source':'editor'}
    p=client.post(base+'/citation-review/preview',json=opts)
    assert p.status_code==200,p.text
    opts.update(preview_fingerprint=p.json()['preview_fingerprint'],consent_to_send=True)
    task=client.post(base+'/citation-review/tasks',json=opts)
    assert task.status_code==200,task.text
    return pid,ref,base,opts,task.json()


def model_response(action,prompt,*args,**kwargs):
    raw=prompt.split('审查输入 JSON：\n',1)[1].split('\n附加协议',1)[0]
    payload=json.loads(raw);findings=[]
    for c in payload['claims']:
        s=payload['sources'][0];e=s['evidence'][0]
        findings.append({'claim_id':c['claim_id'],'sources':[{'source_id':s['source_id'],'relation':'supports','role':'support','evidence_id':e['evidence_id'],'fragment_id':e['fragment_id'],'quoted_text':e['text'],'rationale':'synthetic anchored evidence'}],
            'condition_results':[{'index':i,'status':'supported','source_ids':[s['source_id']],'reason':'explicit test condition'} for i,_ in enumerate(c['conditions'])],
            'suggestion':{'text':c['text'],'reasons':['synthetic'],'meaning_change_text':'derived suggestion'},
            'language_review':{'auxiliary_translation':'辅助译释','concept_differences':[]}})
    return {'ok':True,'content':json.dumps({'findings':findings}),'usage':{'total_tokens':100,'completion_tokens':50}}


def command(opts,**extra):return {'request_id':uuid.uuid4().hex,'consent_to_send':True,'preview_fingerprint':opts['preview_fingerprint'],**extra}


def test_engineering_scan_versions_multi_file_and_uncovered():
    files=[{'path':'main.tex','version':3,'content':r'\input{parts/a}\bibliography{one,two}'},
        {'path':'parts/a.tex','version':4,'content':r'\cite{a,b,c}\customcite{d}'},
        {'path':'one.bib','version':2,'content':'@article{a,title={A}}'},
        {'path':'two.bib','version':1,'content':'@book{b,title={B}}'}]
    result=citation_engineering.scan(files,['main.tex'])
    assert result['missing']==['c'] and result['available']==['a','b']
    assert result['citation_locations'][0]['version']==4
    assert any('自定义' in x['reason'] for x in result['uncovered_syntax'])
    assert not result['coverage_complete']
    assert citation_engineering.visible('a%comment\n\\%text\n')=='a        \n\\%text\n'


def test_explicit_empty_source_scope_never_expands():
    report={'claims':[],'source_pool':[{'source_id':'s'}],'context':{'references':[{'cite_key':'hidden'}]}}
    assert citation_semantic.model_input(report,[])['sources']==[]
    assert citation_semantic.model_input(report,[])['context']['references']==[]


def test_draft_tasks_partial_cancel_resume_disposition_and_purge(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client)
    calls=[]
    def call(*args,**kwargs):calls.append(1);return model_response(*args,**kwargs)
    monkeypatch.setattr(app,'call_ai',call)
    url=base+'/citation-review/tasks/'+task['id']
    assert len(task['items'])==2 and task['input_kind']=='draft'
    first_command=command(opts)
    first=client.post(url+'/step',json=first_command)
    assert first.status_code==200,first.text
    assert first.json()['coverage']['checked']==1,first.text
    assert first.json()['items'][0]['report']['claims'][0]['condition_results'][0]['status']=='supported'
    # Same explicit step ID cannot silently dispatch the following claim.
    assert client.post(url+'/step',json=first_command).json()['coverage']['checked']==1 and len(calls)==1
    client.post(url+'/cancel',json=command(opts))
    assert client.post(url+'/step',json=command(opts)).json()['coverage']['checked']==1
    resumed=client.post(url+'/resume',json=command(opts))
    assert resumed.status_code==200,resumed.text
    done=client.post(url+'/step',json=command(opts)).json()
    assert done['status']=='completed' and len(calls)==2
    claim=done['items'][0]['claim_id']
    d=client.post(url+f'/claims/{claim}/disposition',json={'request_id':'retain','action':'retain','reason':'retain original'})
    assert d.status_code==200 and d.json()['evidence_status_unchanged']
    assert client.post(url+'/delete',json={'request_id':'delete','confirm':False}).status_code==422
    assert client.post(url+'/delete',json={'request_id':'delete','confirm':True}).json()['cleanup_state']=='purged'
    conn=app.db()
    assert not conn.execute('SELECT 1 FROM citation_review_items WHERE task_id=?',(task['id'],)).fetchone()
    assert conn.execute('SELECT snapshot_json FROM citation_review_tasks WHERE id=?',(task['id'],)).fetchone()[0]=='{}'
    conn.close()
    assert client.get(base).status_code==200


def test_late_cancel_does_not_publish_or_dispatch_next(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client)
    url=base+'/citation-review/tasks/'+task['id']
    def late(*args,**kwargs):
        assert client.post(url+'/cancel',json=command(opts)).status_code==200
        return model_response(*args,**kwargs)
    monkeypatch.setattr(app,'call_ai',late)
    result=client.post(url+'/step',json=command(opts)).json()
    assert result['status']=='cancelled' and result['coverage']['checked']==0
    assert result['items'][0]['error_code']=='late_result_discarded'
    assert client.post(url+'/resume',json=command(opts)).status_code==409
    assert client.post(url+'/resume',json=command(opts,duplicate_cost_confirmed=True)).status_code==200


def test_profile_change_invalidates_and_revocation_scrubs_derivatives(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client)
    monkeypatch.setattr(app,'call_ai',model_response)
    url=base+'/citation-review/tasks/'+task['id']
    done=client.post(url+'/step',json=command(opts)).json()
    assert done['coverage']['checked']==1
    conn=app.db()
    conn.execute("UPDATE oa_imports SET status='revoked' WHERE project_id=?",(pid,));conn.commit();conn.close()
    report=client.get(url).json()
    assert report['stale'] and report['cleanup_state']=='permission_filtered'
    claim=report['items'][0]['report']['claims'][0]
    assert claim['semantic_review']=={} and claim['language_review']=={}
    conn=app.db();stored=conn.execute('SELECT result_json FROM citation_review_items WHERE task_id=? AND sequence=1',(task['id'],)).fetchone()[0];conn.close()
    assert 'derived suggestion' not in stored and '辅助译释' not in stored
    assert client.post(url+'/step',json=command(opts)).status_code==409


def test_same_identity_reuses_completed_items_but_draft_change_invalidates(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client)
    monkeypatch.setattr(app,'call_ai',model_response)
    url=base+'/citation-review/tasks/'+task['id']
    client.post(url+'/step',json=command(opts))
    client.post(url+'/step',json=command(opts))
    repeated=client.post(base+'/citation-review/tasks',json={**opts,'request_id':uuid.uuid4().hex}).json()
    assert repeated['status']=='completed' and repeated['coverage']['checked']==2
    assert all(i['attempt']==0 for i in repeated['items'])
    assert client.put(base+'/editor/draft',json={'path':'main.tex','base_version':1,'base_content':'','content':'Different draft.'}).status_code==200
    assert client.get(url).json()['stale']
    assert client.post(url+f'/claims/{task["items"][0]["claim_id"]}/disposition',json={'request_id':'stale','action':'retain','reason':'old input'}).status_code==409


def test_unreliable_evidence_and_omitted_conditions_never_pass_whole_claim(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client)
    assert client.put(base+f'/citation-review/source-profiles/{ref}',json={'expected_version':1,'parse_reliability':'unreliable','source_role':'secondary'}).status_code==200
    p=client.post(base+'/citation-review/preview',json={**opts,'request_id':uuid.uuid4().hex}).json()
    opts.update(request_id=uuid.uuid4().hex,preview_fingerprint=p['preview_fingerprint'])
    new=client.post(base+'/citation-review/tasks',json=opts).json()
    monkeypatch.setattr(app,'call_ai',model_response)
    checked=client.post(base+'/citation-review/tasks/'+new['id']+'/step',json=command(opts)).json()
    claim=checked['items'][0]['report']['claims'][0]
    assert claim['supports'][0]['support_status']!='supported'
    assert claim['condition_results'][0]['status']=='unknown'


def test_deleted_inflight_task_never_resurrects(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client);url=base+'/citation-review/tasks/'+task['id']
    def late(*args,**kwargs):
        assert client.post(url+'/delete',json={'request_id':'delete-inflight','confirm':True}).status_code==200
        return model_response(*args,**kwargs)
    monkeypatch.setattr(app,'call_ai',late)
    result=client.post(url+'/step',json=command(opts))
    assert result.status_code==200 and result.json()['status']=='deleted' and not result.json()['items']


def test_explicit_no_sources_finishes_as_not_verifiable(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client)
    opts.update(request_id=uuid.uuid4().hex,source_ids=[])
    p=client.post(base+'/citation-review/preview',json=opts).json();opts['preview_fingerprint']=p['preview_fingerprint']
    new=client.post(base+'/citation-review/tasks',json=opts).json()
    def no_sources(action,prompt,*args,**kwargs):
        payload=json.loads(prompt.split('审查输入 JSON：\n',1)[1].split('\n附加协议',1)[0])
        assert not payload['sources'] and not payload['context']['references']
        return {'ok':True,'content':json.dumps({'findings':[{'claim_id':c['claim_id'],'sources':[]} for c in payload['claims']]})}
    monkeypatch.setattr(app,'call_ai',no_sources)
    result=client.post(base+'/citation-review/tasks/'+new['id']+'/step',json=command(opts)).json()
    assert result['coverage']['checked']==1
    assert result['items'][0]['report']['claims'][0]['supports'][0]['support_status']!='supported'
