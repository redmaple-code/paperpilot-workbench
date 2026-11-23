"""Missing scenario assertions identified by the stage-four AC review."""
import json
import sqlite3
import uuid

import app
from tests.test_req001 import create_project,enter_revision
from tests.test_req008_continuation import _batch_request
from tests.test_stage3_citation import setup_review,command,model_response


def file(client,pid):return next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path']=='main.tex')


def candidate(client,pid,monkeypatch,action='follow',text='Stage-four scoped candidate.'):
    if action=='follow':text='\\documentclass{article}\n\\begin{document}\n'+text+'\n\\end{document}\n'
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:{'ok':True,'content':text})
    current=file(client,pid);base=f'/api/projects/{pid}'
    preview=client.post(base+'/ai-context',json={'action':action,'target_kind':'file','target_path':'main.tex','prompt':'synthetic scope test','content':current['content'],'selection':current['content']})
    assert preview.status_code==200,preview.text
    response=client.post(base+'/ai-jobs',json={'request_id':uuid.uuid4().hex,'action':action,'prompt':'synthetic scope test','content':current['content'],'selection':current['content'],
        'scope':{'targets':[{'kind':'file','path':'main.tex','start':0,'end':len(current['content'])}]},'base_file_versions':{'main.tex':current['version']},'context_version':preview.json()['version']})
    assert response.status_code==202,response.text
    result=client.get(base+'/ai-jobs/'+response.json()['job']['id']).json()
    assert result['job']['status']=='succeeded',result
    return result['candidate']


def test_follow_accepts_only_current_preview_and_source_change_stales_other_candidate(client,monkeypatch):
    pid=create_project(client)['project']['id'];workflow=enter_revision(client,pid);before=file(client,pid)
    a=candidate(client,pid,monkeypatch)
    assert file(client,pid)==before
    accepted=client.post(f'/api/projects/{pid}/candidates/{a["id"]}/decision',json={'request_id':'follow-accept','decision':'accept','base_file_versions':a['base_file_versions'],'allow_warnings':True})
    assert accepted.status_code==200,accepted.text
    assert file(client,pid)['version']==before['version']+1
    assert client.get(f'/api/projects/{pid}/workflow').json()['version']==workflow['version']
    b=candidate(client,pid,monkeypatch,text='Must remain a candidate.')
    client.post(f'/api/projects/{pid}/terms',json={'term':'current-source','definition':'new material version'})
    current=file(client,pid)
    assert client.get(f'/api/projects/{pid}/ai-jobs/{b["job_id"]}').json()['candidate']['stale']
    assert client.post(f'/api/projects/{pid}/candidates/{b["id"]}/decision',json={'request_id':'follow-stale','decision':'accept','base_file_versions':b['base_file_versions'],'allow_warnings':True}).status_code==409
    assert file(client,pid)==current


def test_continuation_siblings_expire_and_undo_preserves_subsequent_human_edit(client,monkeypatch):
    pid=create_project(client)['project']['id'];enter_revision(client,pid)
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:{'ok':True,'content':'Scoped continuation.'})
    batch=_batch_request(client,pid,request_id='stage4-siblings',segment_count=2)
    base=f'/api/projects/{pid}';before=file(client,pid);outline=client.get(base+'/outline/structured').json()
    batch=client.get(base+'/continuations/'+batch['id']).json();first,second=batch['segments']
    r=client.post(base+f'/continuations/{batch["id"]}/segments/{first["id"]}/decision',json={'request_id':'accept-first','decision':'accept','base_file_versions':first['candidate']['base_file_versions'],'allow_warnings':True})
    assert r.status_code==200,r.text
    current=file(client,pid)
    assert current['version']==before['version']+1
    stale=client.get(base+'/continuations/'+batch['id']).json()
    assert next(x for x in stale['segments'] if x['id']==second['id'])['candidate']['stale']
    r=client.post(base+f'/continuations/{batch["id"]}/segments/{second["id"]}/decision',json={'request_id':'accept-second-stale','decision':'accept','base_file_versions':second['candidate']['base_file_versions'],'allow_warnings':True})
    assert r.status_code==409,r.text
    undo=client.post(base+'/versions/restore',json={'target_kind':'file','target_path':'main.tex','target_version':before['version'],'base_version':current['version'],'action':'undo'})
    assert undo.status_code==200,undo.text
    restored=file(client,pid);assert restored['content']==before['content']
    edit=client.put(base+'/files',json={'path':'main.tex','base_version':restored['version'],'content':restored['content']+'\nLater human edit.'})
    assert edit.status_code==200,edit.text
    guard=client.post(base+'/versions/restore',json={'target_kind':'file','target_path':'main.tex','target_version':before['version'],'base_version':current['version'],'action':'undo'})
    assert guard.status_code==409
    assert 'Later human edit.' in file(client,pid)['content']
    assert client.get(base+'/outline/structured').json()==outline


def test_citation_candidate_and_editor_draft_are_independent_explicit_inputs(client,monkeypatch):
    pid=create_project(client)['project']['id'];enter_revision(client,pid);base=f'/api/projects/{pid}'
    a=candidate(client,pid,monkeypatch,action='edit',text='Candidate-only assertion.')
    original=file(client,pid)
    assert client.put(base+'/editor/draft',json={'path':'main.tex','base_version':original['version'],'base_content':original['content'],'content':'Draft-only assertion.'}).status_code==200
    draft=client.post(base+'/citation-review/preview',json={'request_id':'draft-input','input_kind':'draft','draft_source':'editor','target_paths':['main.tex']})
    assert draft.status_code==200,draft.text
    assert 'Draft-only assertion.' in json.dumps(draft.json()['outbound_items'])
    explicit=client.post(base+'/citation-review/preview',json={'request_id':'candidate-input','input_kind':'candidate','candidate_id':a['id'],'target_paths':['main.tex']})
    assert explicit.status_code==200,explicit.text
    assert 'Candidate-only assertion.' in json.dumps(explicit.json()['outbound_items']) and 'Draft-only assertion.' not in json.dumps(explicit.json()['outbound_items'])
    assert explicit.json()['engineering']['citation_locations']==[]
    assert any(x['input_kind']=='candidate' for x in explicit.json()['engineering']['input_files'])
    assert file(client,pid)==original
    missing=client.post(base+'/citation-review/preview',json={'request_id':'missing','input_kind':'candidate','target_paths':['main.tex']})
    assert missing.status_code==422


def test_source_roles_visual_limits_self_copy_and_study_relations_are_explicit(client):
    pid,ref,base,opts,task=setup_review(client)
    client.post(base+'/refs',json={'cite_key':'related-copy','title':'Same research, explicit relation'})
    sources=client.get(base+'/citation-review/source-profiles').json()['sources'];other=next(s for s in sources if s['cite_key']=='related-copy')
    for sid,version in [(ref,1),(other['source_id'],0)]:
        r=client.put(base+'/citation-review/source-profiles/'+sid,json={'expected_version':version,'parse_reliability':'verified','source_role':'primary','study_group':'explicit-study-1','evidence_kind':'figure','derived_from_current':sid==ref})
        assert r.status_code==200,r.text
    from paperpilot.services import citation_review
    conn=app.db();snapshot=citation_review.snapshot(conn,pid,{'input_kind':'draft','target_paths':['main.tex'],'draft_source':'editor'})
    report=snapshot['reports'][0];source=next(s for s in report['source_pool'] if s['source_id']==ref)
    assert not source['evidence'] and source['evidence_limits']
    assert report['study_relations']['known_group_count']==1
    assert set(report['study_relations']['known_groups']['explicit-study-1'])=={ref,other['source_id']}
    assert report['policy']['no_author_identity_exclusion']
    assert all(s['support_status']!='supported' for c in report['claims'] for s in c['supports'])
    conn.close()


def test_original_result_type_correction_is_recorded_and_requires_new_identity(client):
    pid,ref,base,opts,task=setup_review(client);claim=task['items'][0]['claim_id'];url=base+'/citation-review/tasks/'+task['id']
    r=client.post(url+f'/claims/{claim}/disposition',json={'request_id':'correct-original','action':'correct_type','claim_type':'original_result','reason':'Synthetic author result, interpretation reviewed separately'})
    assert r.status_code==200 and r.json()['new_preview_required'] and r.json()['evidence_status_unchanged']
    preview=client.post(base+'/citation-review/preview',json={**opts,'claim_types':{claim:'original_result'}}).json()
    assert preview['preview_fingerprint']!=opts['preview_fingerprint']
    new=client.post(base+'/citation-review/tasks',json={**opts,'request_id':'original-result-review','claim_types':{claim:'original_result'},'preview_fingerprint':preview['preview_fingerprint']}).json()
    corrected=next(c for i in new['items'] for c in i['report']['claims'] if c['id']==claim)
    assert corrected['claim_type']=='original_result' and not corrected['support_required'] and corrected['interpretation_requires_separate_review']


def test_pre_stage3_database_copy_upgrade_preserves_user_content_and_seeds_history_once(client,monkeypatch,tmp_path):
    pid=create_project(client)['project']['id'];base=f'/api/projects/{pid}'
    domain=client.post('/api/domains',json={'name':'Old database domain'}).json()
    resource=client.post(f'/api/domains/{domain["id"]}/resources',json={'resource_type':'corpus','title':'Existing shared content','content':'Must survive migration.'}).json()
    content=file(client,pid)['content'];conn=app.db();old=tmp_path/'old-copy.db';target=sqlite3.connect(old);conn.backup(target);target.close();conn.close()
    conn=sqlite3.connect(old)
    for name in ['citation_review_events','citation_review_items','citation_review_tasks','citation_source_profiles','corpus_curation_migrations','corpus_source_links','domain_resource_versions','continuation_budget_attempts']:
        conn.execute('DROP TABLE '+name)
    for table,column in [('terminology_reviews','input_fingerprint'),('outline_candidates','origin_json'),('continuation_segments','goal'),('continuation_segments','outline_node_id'),('continuation_batches','input_fingerprint'),('continuation_batches','subgoals_json')]:
        conn.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    conn.commit();conn.close();monkeypatch.setattr(app,'DB_PATH',old)
    app.init_db();app.init_db()
    assert file(client,pid)['content']==content
    versions=client.get(f'/api/domains/{domain["id"]}/resources/{resource["id"]}/versions')
    assert versions.status_code==200 and len(versions.json())==1
    assert versions.json()[0]['content']=='Must survive migration.'
    conn=app.db();assert not conn.execute('PRAGMA foreign_key_check').fetchall();conn.close()


def test_outline_update_move_delete_and_history_preserve_old_tree(client):
    pid=create_project(client)['project']['id'];base=f'/api/projects/{pid}/outline';version=0
    def change(command,**extra):
        nonlocal version
        response=client.post(base+'/nodes',json={'request_id':uuid.uuid4().hex,'command':command,'base_version':version,**extra})
        assert response.status_code==200,response.text
        version=response.json()['version'];return response.json()
    change('create',node={'id':'root-a','title':'A'})
    change('create',node={'id':'root-b','title':'B'})
    change('create',node={'id':'child','title':'Child','parent_id':'root-a'})
    changed=change('update',node_id='child',node={'title':'Edited child'})
    assert next(n for n in changed['nodes'] if n['id']=='child')['title']=='Edited child'
    moved=change('move',node_id='child',parent_id='root-b',position=0)
    assert next(n for n in moved['nodes'] if n['id']=='child')['parent_id']=='root-b'
    assert next(n for n in client.get(base+'/structured?version=3').json()['nodes'] if n['id']=='child')['parent_id']=='root-a'
    deleted=change('delete',node_id='child')
    assert 'child' not in {n['id'] for n in deleted['nodes']}
    assert 'child' in {n['id'] for n in client.get(base+'/structured?version=5').json()['nodes']}


def test_continuation_unknown_second_result_preserves_first_and_requires_explicit_retry(client,monkeypatch):
    pid=create_project(client)['project']['id'];enter_revision(client,pid);calls=[];base=f'/api/projects/{pid}';before=file(client,pid)
    def call(*args,**kwargs):
        calls.append(1)
        return {'ok':False,'error_code':'provider_result_unknown','error':'synthetic response lost'} if len(calls)==2 else {'ok':True,'content':'Completed segment.'}
    monkeypatch.setattr(app,'call_ai',call)
    batch=_batch_request(client,pid,request_id='stage4-partial',segment_count=2,hard_budget_tokens=50000)
    task=client.get(base+'/continuations/'+batch['id']).json()
    first=next(s for s in task['segments'] if s['candidate']);assert len(calls)==2
    assert task['status']=='paused' and file(client,pid)==before
    blocked=client.post(base+f'/continuations/{task["id"]}/resume',json={'request_id':'without-confirm'})
    assert blocked.status_code==409 and blocked.json()['detail']['code']=='duplicate_cost_confirmation_required'
    retry=client.post(base+f'/continuations/{task["id"]}/resume',json={'request_id':'explicit-confirm','duplicate_cost_confirmed':True})
    assert retry.status_code==200,retry.text
    after=client.get(base+'/continuations/'+task['id']).json()
    assert len(calls)==3 and len([s for s in after['segments'] if s['candidate']])==2
    assert next(s for s in after['segments'] if s['id']==first['id'])['candidate']['id']==first['candidate']['id']
    assert file(client,pid)==before


def test_concept_differences_only_use_anchored_sources_and_never_replace_source_or_body(client,monkeypatch):
    pid,ref,base,opts,task=setup_review(client);before=client.get(base).json()['files']
    def response(*args,**kwargs):
        result=model_response(*args,**kwargs);body=json.loads(result['content'])
        for finding in body['findings']:
            finding['language_review']={'auxiliary_translation':'合成辅助译释，不是来源原文', 'concept_differences':[
                {'source_id':ref,'paper_term':'stable','source_term':'stability','reason':'synthetic scope distinction'},
                {'source_id':'invented-source','paper_term':'false','source_term':'invented','reason':'must be dropped'}]}
        return {**result,'content':json.dumps(body)}
    monkeypatch.setattr(app,'call_ai',response)
    checked=client.post(base+'/citation-review/tasks/'+task['id']+'/step',json=command(opts))
    assert checked.status_code==200,checked.text
    claim=checked.json()['items'][0]['report']['claims'][0]
    language=claim['language_review']
    assert language['auxiliary_translation']=='合成辅助译释，不是来源原文'
    assert len(language['concept_differences'])==1 and language['concept_differences'][0]['source_id']==ref
    assert 'Graphene stability improves' in json.dumps(claim['supports'])
    assert client.get(base).json()['files']==before


def test_registered_pricing_tracks_currency_source_and_actual_input_output_usage(client,monkeypatch):
    import pytest
    from tests.provider_fixtures import seed_generation_fixture
    profile=next(p for p in client.get('/api/ai/profiles').json() if p['name']=='Isolated authoring fixture')
    model=profile['models'][0]
    rate={'input_per_million':2,'output_per_million':8,'currency':'CNY','source':'Synthetic published-rate fixture; not a provider quote'}
    limits={model:{'input_tokens':1000000,'output_tokens':100000,'source':'isolated consumer fixture','pricing':rate}}
    changed=client.put('/api/ai/profiles/'+profile['id'],json={'base_version':profile['current_version'],'changes':{'model_limits':limits}})
    assert changed.status_code==200,changed.text
    seed_generation_fixture(profile['id'],model)
    default=client.get('/api/ai/default').json()
    bound=client.put('/api/ai/default',json={'profile_id':profile['id'],'config_revision':changed.json()['current_version'],
        'model_id':model,'expected_version':default['version']})
    assert bound.status_code==200,bound.text
    pid=create_project(client)['project']['id'];enter_revision(client,pid)
    before=file(client,pid)
    monkeypatch.setattr(app,'call_ai',lambda *a,**kw:{'ok':True,'content':'Priced synthetic candidate.', 'usage':{'prompt_tokens':120,'completion_tokens':30,'total_tokens':150}})
    batch=_batch_request(client,pid,request_id='known-price',segment_count=1,hard_budget_tokens=50000,hard_budget_amount=100)
    saved=client.get(f'/api/projects/{pid}/continuations/'+batch['id']).json()
    budget=saved['budget']
    assert budget['currency']=='CNY' and budget['pricing_source']==rate['source']
    assert budget['pricing_verification']=='user_provided_unverified'
    assert budget['usage']['tokens']==150 and budget['usage']['unknown_attempts']==0
    assert budget['usage']['cost']==pytest.approx((120*2+30*8)/1000000)
    assert budget['usage']['attempts']==1 and saved['segments'][0]['candidate']
    assert file(client,pid)==before
