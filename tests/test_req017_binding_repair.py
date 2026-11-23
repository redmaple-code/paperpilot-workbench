"""REQ-017 legacy baseline binding repair and failure invariants."""
import json
import app


def setup_mismatch(client):
    old=client.post('/api/domains',json={'name':'旧归属'}).json()
    new=client.post('/api/domains',json={'name':'现归属'}).json()
    p=client.post('/api/projects',json={'name':'归属修复论文','domain_id':old['id'],'style':'保留风格'}).json()['project'];pid=p['id']
    rev=client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    result=client.put(f'/api/projects/{pid}/settings/term_policy',json={'value':'domain_preferred','expected_project_revision':rev,'request_id':'own-override'})
    assert result.status_code==200,result.text
    conn=app.db()
    with conn:conn.execute('UPDATE projects SET domain_id=? WHERE id=?',(new['id'],pid))
    conn.close();preview=client.get(f'/api/projects/{pid}/settings/sync-preview').json();repair=preview['binding_repair']
    params={'expected_project_revision':repair['project_revision'],'expected_baseline_version':repair['baseline_version'],'expected_project_domain_id':new['id'],'expected_baseline_domain_id':old['id'],'request_id':'repair-1'}
    return pid,old,new,preview,params


def test_repair_preserves_values_overrides_and_audits_with_idempotency(client):
    pid,old,new,preview,params=setup_mismatch(client);assert preview['reason']=='domain_binding_mismatch' and preview['binding_repair']['available']
    before=client.get(f'/api/projects/{pid}/settings').json();result=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params)
    assert result.status_code==200,result.text
    after=result.json();assert after['effective']==before['effective'] and after['overrides']==before['overrides']
    assert after['baseline']['domain_id']==new['id'] and after['previous_baseline']==before['baseline']
    assert after['sources']['style']['source']=='project_baseline' and after['sources']['format_policy']['source']=='unknown'
    assert after['sources']['term_policy']['source']=='project_override'
    assert client.get(f'/api/projects/{pid}').json()['project']['domain_id']==new['id']
    replay=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params);assert replay.status_code==200 and replay.json()['idempotent_replay']
    conflict=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params | {'expected_baseline_domain_id':'other'});assert conflict.status_code==409
    conn=app.db();event=conn.execute("SELECT result_json FROM project_setting_events WHERE project_id=? AND action='binding_repair'",(pid,)).fetchone();conn.close()
    assert json.loads(event['result_json'])['previous_baseline']==before['baseline']


def test_stale_and_read_only_repair_never_mutate(client):
    pid,old,new,preview,params=setup_mismatch(client);before=client.get(f'/api/projects/{pid}/settings').json()
    denied=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params | {'expected_project_revision':params['expected_project_revision']-1})
    assert denied.status_code==409 and denied.json()['detail']['code']=='binding_repair_stale'
    assert client.get(f'/api/projects/{pid}/settings').json()==before
    conn=app.db()
    with conn:conn.execute('UPDATE projects SET archived=1 WHERE id=?',(pid,))
    conn.close();denied=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params)
    assert denied.status_code==409 and denied.json()['detail']['code']=='project_read_only'
    assert client.get(f'/api/projects/{pid}/settings').json()==before


def test_failed_audit_event_rolls_back_binding_and_project_revision(client):
    pid,old,new,preview,params=setup_mismatch(client);before=client.get(f'/api/projects/{pid}/settings').json();project=client.get(f'/api/projects/{pid}').json()['project']
    conn=app.db()
    with conn:conn.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON project_setting_events WHEN NEW.action='binding_repair' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    conn.close()
    try:
        result=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params)
        assert result.status_code>=500
    except Exception as exc:
        assert 'injected failure' in str(exc)
    assert client.get(f'/api/projects/{pid}/settings').json()==before
    assert client.get(f'/api/projects/{pid}').json()['project']==project


def test_missing_current_domain_and_published_paper_block_repair(client):
    pid,old,new,preview,params=setup_mismatch(client);before=client.get(f'/api/projects/{pid}/settings').json()
    conn=app.db()
    with conn:conn.execute("UPDATE projects SET domain_id='lost-domain' WHERE id=?",(pid,))
    conn.close();preview=client.get(f'/api/projects/{pid}/settings/sync-preview').json();assert not preview['binding_repair']['available']
    denied=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params | {'expected_project_domain_id':'lost-domain'})
    assert denied.status_code==409 and denied.json()['detail']['code']=='binding_repair_domain_missing'
    assert client.get(f'/api/projects/{pid}/settings').json()==before
    conn=app.db()
    with conn:conn.execute("UPDATE projects SET domain_id=?,lifecycle_state='published' WHERE id=?",(new['id'],pid))
    conn.close();denied=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params)
    assert denied.status_code==409 and denied.json()['detail']['code']=='project_read_only'


def test_repair_to_unbound_keeps_values_and_leaves_sync_unavailable(client):
    pid,old,new,preview,params=setup_mismatch(client);before=client.get(f'/api/projects/{pid}/settings').json()
    conn=app.db()
    with conn:conn.execute("UPDATE projects SET domain_id='' WHERE id=?",(pid,))
    conn.close();preview=client.get(f'/api/projects/{pid}/settings/sync-preview').json();assert preview['binding_repair']['available']
    result=client.post(f'/api/projects/{pid}/settings/binding-repair',json=params | {'expected_project_domain_id':''});assert result.status_code==200,result.text
    assert result.json()['effective']==before['effective'] and result.json()['baseline']['domain_id']==''
    assert client.get(f'/api/projects/{pid}/settings/sync-preview').json()['reason']=='project_has_no_domain'
