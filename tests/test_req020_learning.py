"""REQ-020 v4: actual consumers, privacy, races and transaction boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import uuid
import pytest
import app
from paperpilot.services import style_learning as learning
from paperpilot.services.content_versions import save_content
from paperpilot.services.context import style_context_current
from test_req001 import enter_revision, make_candidate


def setup(client):
    result = client.post('/api/projects', json={'name':'学习验证','style':'严谨'}).json()
    return result['project']['id']


def view(client, pid):
    response = client.get(f'/api/projects/{pid}/style-learning')
    assert response.status_code == 200, response.text
    return response.json()


def command(client, pid, action, **extra):
    return client.post(f'/api/projects/{pid}/style-learning/{action}', json={
        'request_id':uuid.uuid4().hex, 'expected_version':view(client,pid)['version'], **extra})


def opt(client,pid,scopes=None):
    response = command(client,pid,'consent',authorized=True,scopes=scopes or ['example','feedback','accepted_edit','rejected_edit'],retention_days=30)
    assert response.status_code==200,response.text


def signal(client,pid,kind='feedback',content='请保持句子简洁，避免夸大结论。',source=None):
    response=command(client,pid,'signals',kind=kind,content=content,source_version=source or uuid.uuid4().hex)
    assert response.status_code==200,response.text
    return response.json()['signal_id']


def activate(client,pid):
    opt(client,pid)
    sid=signal(client,pid)
    assert command(client,pid,'distill').status_code==200
    candidate=next(r for r in view(client,pid)['rules'] if r['status']=='candidate')
    assert command(client,pid,'approve',rule_ids=[candidate['id']]).status_code==200
    return sid,candidate


def test_default_off_real_saves_scope_cas_and_project_isolation(client):
    pid=setup(client);enter_revision(client,pid)
    original=client.get(f'/api/projects/{pid}').json()['files'][0]
    saved=client.put(f'/api/projects/{pid}/files',json={'path':original['path'],'content':original['content']+'\nNew sentence.','base_version':original['version']})
    assert saved.status_code==200
    assert view(client,pid)['signals']==[]
    opt(client,pid,['feedback'])
    assert command(client,pid,'signals',kind='example',content='No scope.',source_version='v1').status_code==409
    response=client.post(f'/api/projects/{pid}/style-learning/pause',json={'expected_version':0,'request_id':'stale'})
    assert response.status_code==409
    other=setup(client)
    sid=signal(client,pid)
    assert not view(client,other)['signals']
    assert command(client,other,'delete',confirmation='删除学习数据',signal_id=sid).status_code==404
    assert view(client,pid)['state']=='collecting'


def test_explicit_idempotency_auto_queue_and_no_unapproved_injection(client):
    pid=setup(client);opt(client,pid)
    payload={'request_id':'once','expected_version':1,'kind':'example','source_version':'author-v1',
        'content':'We test a method. We compare the results. We report the limitations.'}
    path=f'/api/projects/{pid}/style-learning/signals'
    first=client.post(path,json=payload)
    assert first.status_code==200,first.text
    assert client.post(path,json=payload).json()['idempotent_replay']
    assert client.post(path,json=payload|{'content':'different'}).status_code==409
    assert client.post(path,json=payload|{'request_id':'different','content':'different'}).status_code==409
    learning.maintenance_once(app.db)
    state=view(client,pid)
    assert state['runs'][0]['status']=='completed'
    assert len(state['rules'])==1
    assert state['rules'][0]['method']['sentence_count']==3
    assert not state['profile_applies']
    assert '授权文风经验' not in client.get(f'/api/projects/{pid}/ai-context').json()['preview']
    assert command(client,pid,'approve',rule_ids=[state['rules'][0]['id']]).status_code==200
    preview=client.get(f'/api/projects/{pid}/ai-context').json()
    assert '授权文风经验' in preview['preview']
    source=next(s for s in preview['sources'] if s['kind']=='style_profile')
    assert source['signal_ids']==[first.json()['signal_id']]
    assert source['block_length']>0 and len(source['block_sha256'])==64


def test_real_accepted_edit_minimal_fragment_and_rejection_does_not_infer(client,monkeypatch):
    pid=setup(client);enter_revision(client,pid);opt(client,pid)
    file=client.get(f'/api/projects/{pid}').json()['files'][0]
    text=file['content']+'\nWe evaluate this method. We compare baseline results. We state limitations.'
    response=client.put(f'/api/projects/{pid}/files',json={'path':file['path'],'content':text,'base_version':file['version']})
    assert response.status_code==200,response.text
    sample=view(client,pid)['signals'][0]
    assert sample['kind']=='accepted_edit' and '@2:manual_edit' in sample['source_version']
    assert len(sample['content'])<len(text)
    job,_=make_candidate(client,monkeypatch,pid,request_id='reject-learning',content='Candidate rejected for unknown reason.')
    candidate=job['candidate']
    response=client.post(f"/api/projects/{pid}/candidates/{candidate['id']}/decision",json={'request_id':'reject','decision':'reject'})
    assert response.status_code==200,response.text
    assert any(s['kind']=='rejected_edit' for s in view(client,pid)['signals'])
    rejected=[{'kind':'rejected_edit','content':'Use very long sentences.','id':'rejected'}]
    rules,diagnostics=learning.distill_local(rejected)
    assert rules==[] and any(d['code']=='rejection_reason_unknown' for d in diagnostics)


def test_edit_approval_remove_rollback_pause_resume_disable(client):
    pid=setup(client);sid,rule=activate(client,pid)
    initial=view(client,pid)['active_profile_id']
    response=command(client,pid,f"rules/{rule['id']}/edit",instruction='独有修改规则。')
    assert response.status_code==200,response.text
    new_id=response.json()['rule_id']
    assert '独有修改规则。' not in view(client,pid)['effective_instructions']
    assert command(client,pid,'approve',rule_ids=[new_id]).status_code==200
    assert view(client,pid)['effective_rule_ids']==[new_id]
    assert command(client,pid,'rollback',target_profile_id=initial).status_code==200
    assert view(client,pid)['effective_rule_ids']==[rule['id']]
    assert command(client,pid,'pause').status_code==200
    assert not view(client,pid)['profile_applies']
    assert command(client,pid,f"rules/{rule['id']}/remove").status_code==409
    assert view(client,pid)['state']=='paused'
    assert command(client,pid,'signals',kind='feedback',content='blocked',source_version='blocked').status_code==409
    assert command(client,pid,'resume').status_code==200
    assert view(client,pid)['profile_applies']
    assert command(client,pid,f"rules/{rule['id']}/remove").status_code==200
    assert not view(client,pid)['profile_applies']
    assert command(client,pid,'rollback',target_profile_id=initial).status_code==200
    assert command(client,pid,'disable').status_code==200
    assert command(client,pid,'resume').status_code==409
    opt(client,pid)
    assert command(client,pid,'rollback',target_profile_id=initial).status_code==409


def test_delete_redacts_historical_context_stales_candidate_and_blocks_retry(client,monkeypatch):
    pid=setup(client);enter_revision(client,pid)
    sid,rule=activate(client,pid)
    manuscript=client.get(f'/api/projects/{pid}').json()['files']
    job,_=make_candidate(client,monkeypatch,pid,request_id='learned-context')
    candidate=job['candidate'];cid=candidate['id'];jid=job['job']['id']
    conn=app.db();snapshot=conn.execute('SELECT * FROM context_snapshots WHERE id=?',(candidate['context_snapshot_id'],)).fetchone();conn.close()
    assert rule['instruction'] in snapshot['context_text']
    assert command(client,pid,'delete',confirmation='删除学习数据',signal_id=sid).status_code==200
    conn=app.db()
    redacted=conn.execute('SELECT * FROM context_snapshots WHERE id=?',(snapshot['id'],)).fetchone()
    assert rule['instruction'] not in redacted['context_text']
    assert 'style_profile_redacted' in redacted['source_manifest_json']
    assert not conn.execute('SELECT 1 FROM style_learning_rules WHERE id=?',(rule['id'],)).fetchone()
    with conn:conn.execute("UPDATE ai_jobs SET status='failed' WHERE id=?",(jid,))
    conn.close()
    retry=client.post(f'/api/projects/{pid}/ai-jobs/{jid}/retry',json={'request_id':'retry-redacted'})
    assert retry.status_code==409,retry.text
    assert client.get(f'/api/projects/{pid}/candidates/{cid}').json()['stale']
    assert client.get(f'/api/projects/{pid}').json()['files']==manuscript
    assert command(client,pid,'delete',confirmation='删除学习数据').status_code==200
    state=view(client,pid)
    assert state['state']=='disabled' and not state['signals'] and not state['rules']
    conn=app.db();receipts=' '.join(r[0] for r in conn.execute('SELECT result_json FROM style_learning_operations'));conn.close()
    assert rule['instruction'] not in receipts


def test_ttl_read_guard_and_real_cleanup(client,monkeypatch):
    pid=setup(client);enter_revision(client,pid);sid,rule=activate(client,pid)
    job,_=make_candidate(client,monkeypatch,pid,request_id='ttl-context')
    profile=view(client,pid)['active_profile_id']
    conn=app.db()
    with conn:conn.execute("UPDATE style_learning_signals SET retention_until='2000-01-01' WHERE id=?",(sid,))
    snapshot=conn.execute('SELECT * FROM context_snapshots WHERE id=?',(job['candidate']['context_snapshot_id'],)).fetchone()
    assert not style_context_current(conn,pid,json.loads(snapshot['source_manifest_json']))
    conn.close()
    state=view(client,pid)
    assert not state['profile_applies'] and state['signals'][0]['content']==''
    assert state['rules'][0]['instruction']==''
    assert command(client,pid,'rollback',target_profile_id=profile).status_code==409
    learning.maintenance_once(app.db)
    assert view(client,pid)['signals']==[]
    conn=app.db();assert rule['instruction'] not in conn.execute('SELECT context_text FROM context_snapshots WHERE id=?',(snapshot['id'],)).fetchone()[0];conn.close()


def test_capture_store_failure_keeps_manuscript_transaction(client,monkeypatch):
    pid=setup(client);opt(client,pid)
    def fail(*args):
        args[0].execute("INSERT INTO style_learning_events VALUES('partial',?,'partial','{}','now')",(pid,))
        raise sqlite3.OperationalError('injected failure')
    monkeypatch.setattr(learning,'capture_edit',fail)
    conn=app.db()
    with conn:save_content(conn,pid,'file','main.tex','Saved despite learning failure.',source='manual_edit')
    assert conn.execute("SELECT content FROM files WHERE project_id=? AND path='main.tex'",(pid,)).fetchone()[0]=='Saved despite learning failure.'
    assert not conn.execute("SELECT 1 FROM style_learning_events WHERE id='partial'").fetchone()
    conn.close()
    assert view(client,pid)['state']=='failed'


def test_worker_claim_late_pause_failure_and_lease_recovery(client,monkeypatch):
    pid=setup(client);sid,rule=activate(client,pid)
    signal(client,pid,content='请在段首先明确本段目的。')
    state=view(client,pid);rid=state['runs'][0]['id']
    original=learning.distill_local
    calls=[]
    def pause_during_compute(signals):
        calls.append(1)
        assert command(client,pid,'pause').status_code==200
        return original(signals)
    monkeypatch.setattr(learning,'distill_local',pause_during_compute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:learning.process_run(app.db,pid,rid),range(2)))
    assert len(calls)==1
    assert view(client,pid)['state']=='paused'
    assert all(r['status']!='candidate' for r in view(client,pid)['rules'])
    assert command(client,pid,'resume').status_code==200
    def fail(signals):raise RuntimeError('not exposed')
    monkeypatch.setattr(learning,'distill_local',fail)
    assert command(client,pid,'distill').status_code==200
    state=view(client,pid)
    assert state['state']=='failed' and state['profile_applies']
    conn=app.db()
    with conn:
        conn.execute("UPDATE style_learning_runs SET status='running',claim_token='old',updated_at='2000-01-01' WHERE id=?",(rid,))
        learning.recover_runs(conn)
    row=conn.execute('SELECT status,claim_token FROM style_learning_runs WHERE id=?',(rid,)).fetchone()
    assert tuple(row)==('queued','')
    conn.close()
    monkeypatch.setattr(learning,'distill_local',original)
    learning.process_run(app.db,pid,rid)
    assert view(client,pid)['runs'][0]['status']=='completed'


def test_expired_signal_replay_and_source_reuse_cannot_resurrect(client):
    pid=setup(client);opt(client,pid)
    payload={'request_id':'sample-once','expected_version':1,'kind':'feedback','source_version':'v1','content':'唯一学习文本。'}
    path=f'/api/projects/{pid}/style-learning/signals'
    sid=client.post(path,json=payload).json()['signal_id']
    assert command(client,pid,'delete',confirmation='删除学习数据',signal_id=sid).status_code==200
    assert client.post(path,json=payload).status_code==410
    assert not view(client,pid)['signals']


def test_expiry_checked_before_actual_model_send(client,monkeypatch):
    pid=setup(client);enter_revision(client,pid);sid,rule=activate(client,pid)
    real_worker=app.process_ai_job
    monkeypatch.setattr(app,'process_ai_job',lambda *args:None)
    queued,_=make_candidate(client,monkeypatch,pid,request_id='not-yet-sent')
    assert queued['candidate'] is None
    conn=app.db()
    with conn:conn.execute("UPDATE style_learning_signals SET retention_until='2000-01-01' WHERE id=?",(sid,))
    conn.close()
    calls=[]
    monkeypatch.setattr(app,'call_ai',lambda *args,**kwargs:calls.append(1))
    real_worker(pid,queued['job']['id'])
    assert not calls
    job=client.get(f"/api/projects/{pid}/ai-jobs/{queued['job']['id']}").json()
    assert job['job']['error_code']=='style_profile_revoked'


def test_complete_sentence_and_paragraph_statistics_and_secret_redaction(client):
    assert len(learning.sentence_lengths('One full sentence. Another full sentence. This is unfinished'))==2
    content='First complete sentence. Second complete sentence.\n\nThird complete sentence. Fourth complete sentence.\n\nFifth complete sentence. Sixth complete sentence.'
    rules,_=learning.distill_local([{'id':'sample','kind':'example','content':content}])
    paragraph=next(r for r in rules if r['family']=='paragraph_length')
    assert paragraph['method']['paragraph_count']==3
    pid=setup(client);opt(client,pid)
    signal(client,pid,content='Prefer concise sentences. api_key=super-private-value',source='author-v1')
    assert command(client,pid,'distill').status_code==200
    serialized=json.dumps(view(client,pid),ensure_ascii=False)
    assert 'super-private-value' not in serialized and '[REDACTED]' in serialized


def test_existing_learning_schema_migrates_claim_and_does_not_rescan(client):
    pid=setup(client)
    conn=app.db()
    with conn:
        conn.execute('DROP TABLE style_learning_runs')
        conn.execute("CREATE TABLE style_learning_runs(id TEXT PRIMARY KEY,project_id TEXT,consent_id TEXT,input_hash TEXT,signal_ids_json TEXT,base_profile_id TEXT,base_families_json TEXT,status TEXT,diagnostics_json TEXT,error_code TEXT,created_at TEXT,updated_at TEXT)")
        learning.initialize_schema(conn)
    assert 'claim_token' in {r['name'] for r in conn.execute('PRAGMA table_info(style_learning_runs)')}
    assert conn.execute('SELECT COUNT(*) FROM style_learning_signals').fetchone()[0]==0
    conn.close()
    assert view(client,pid)['version']==0
