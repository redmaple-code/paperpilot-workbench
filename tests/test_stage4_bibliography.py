"""AC-011-16: explicit library-to-engineering review, conflict and version recovery."""
import json
import sqlite3
import uuid
import pytest
import app
from paperpilot.services import bibliography_import
from tests.test_req011_builds import _project


def setup(client):
    pid=_project(client);base=f'/api/projects/{pid}'
    reference=client.post('/api/references',json={'title':'Reviewed library title','authors':'Author, A.','year':'2026','doi':'10.1000/bib-fixture'}).json()
    r=client.post(base+'/references/'+reference['id'],json={'reference_version':1,'cite_key':'reviewed2026','request_id':'link'})
    assert r.status_code==200,r.text
    return pid,base,reference


def file(client,base):return next(f for f in client.get(base).json()['files'] if f['path']=='references.bib')


def preview(client,base,ref):
    r=client.post(base+'/bibliography/preview',json={'reference_id':ref['id']})
    assert r.status_code==200,r.text
    return r.json()


def payload(ref,p,**extra):return {'reference_id':ref['id'],'request_id':uuid.uuid4().hex,'preview_fingerprint':p['preview_fingerprint'],'confirm':True,**extra}


def test_preview_cancel_commit_and_library_update_keep_engineering_snapshot(client):
    pid,base,ref=setup(client);before=file(client,base);p=preview(client,base,ref)
    assert p['reference_version']==1 and '+@article{reviewed2026' in p['diff'] and not p['conflict']
    assert file(client,base)==before,'preview and closing it must not write'
    body=payload(ref,p);r=client.post(base+'/bibliography/commit',json=body)
    assert r.status_code==200,r.text
    after=file(client,base);assert after['version']==before['version']+1 and 'Reviewed library title' in after['content']
    assert client.post(base+'/bibliography/commit',json=body).json()['idempotent_replay']
    assert file(client,base)==after
    assert client.post(base+'/bibliography/commit',json={**body,'conflict_resolution':'replace'}).status_code==409
    assert client.put('/api/references/'+ref['id'],json={'base_version':1,'changes':{'title':'Later title'}}).status_code==200
    assert file(client,base)==after
    assert preview(client,base,ref)['reference_version']==1
    conn=app.db();events=conn.execute("SELECT scope_json FROM assist_action_events WHERE project_id=? AND action_type='bibliography_import'",(pid,)).fetchall();conn.close()
    assert len(events)==1 and json.loads(events[0]['scope_json'])['reference_version']==1


def test_same_key_conflict_only_explicit_replace_preserves_nested_and_handwritten_entries(client):
    pid,base,ref=setup(client);old=file(client,base)
    text='% handwritten note\n@book{other,title={Keep {deep} braces}}\n@article{reviewed2026,title={Old {nested} title}}\n% trailing note'
    assert client.put(base+'/files',json={'path':'references.bib','content':text,'base_version':old['version']}).status_code==200
    p=preview(client,base,ref);assert p['conflict']
    assert client.post(base+'/bibliography/commit',json=payload(ref,p)).status_code==409
    assert file(client,base)['content']==text
    r=client.post(base+'/bibliography/commit',json=payload(ref,p,conflict_resolution='replace'));assert r.status_code==200,r.text
    new=file(client,base)['content'];assert '@book{other,title={Keep {deep} braces}}' in new and '% trailing note' in new
    assert 'Old {nested}' not in new and new.count('@article{reviewed2026')==1


@pytest.mark.parametrize('change',['file','link','draft'])
def test_preview_rejects_later_file_link_or_draft_change_without_losing_work(client,change):
    pid,base,ref=setup(client);p=preview(client,base,ref);before=file(client,base)
    if change=='file':
        client.put(base+'/files',json={'path':'references.bib','content':before['content']+'% Later human edit','base_version':before['version']})
    elif change=='link':
        client.put('/api/references/'+ref['id'],json={'base_version':1,'changes':{'title':'New source version'}})
        assert client.post(base+'/references/'+ref['id']+'/refresh',json={'request_id':'refresh','reference_version':2,'base_version':1}).status_code==200
    else:
        assert client.put(base+'/editor/draft',json={'path':'references.bib','base_version':before['version'],'base_content':before['content'],'content':'Unsaved manual work'}).status_code==200
    expected=file(client,base)
    r=client.post(base+'/bibliography/commit',json=payload(ref,p));assert r.status_code==409,r.text
    assert file(client,base)==expected
    if change=='draft':assert client.get(base+'/editor/draft',params={'path':'references.bib'}).json()['content']=='Unsaved manual work'


def test_scope_stage_and_ambiguous_bib_are_rejected_before_write(client):
    pid,base,ref=setup(client);other=_project(client)
    assert client.post(f'/api/projects/{other}/bibliography/preview',json={'reference_id':ref['id']}).status_code==404
    f=file(client,base)
    client.put(base+'/files',json={'path':'references.bib','content':'@article{broken,title={Unclosed','base_version':f['version']})
    assert client.post(base+'/bibliography/preview',json={'reference_id':ref['id']}).json()['detail']['code']=='bib_parse_incomplete'
    conn=app.db();conn.execute('UPDATE projects SET archived=1 WHERE id=?',(pid,));conn.commit();conn.close()
    assert client.post(base+'/bibliography/preview',json={'reference_id':ref['id']}).status_code==409


def test_import_write_failure_rolls_back_file_version_and_audit_and_can_retry(client,monkeypatch):
    pid,base,ref=setup(client);p=preview(client,base,ref);before=file(client,base);body=payload(ref,p)
    actual=bibliography_import.human_revision.record_event
    monkeypatch.setattr(bibliography_import.human_revision,'record_event',lambda *a,**k:(_ for _ in ()).throw(sqlite3.OperationalError('injected event store failure')))
    assert client.post(base+'/bibliography/commit',json=body).status_code==503
    assert file(client,base)==before
    monkeypatch.setattr(bibliography_import.human_revision,'record_event',actual)
    assert client.post(base+'/bibliography/commit',json=body).status_code==200


@pytest.mark.parametrize('text',[
    '@article(key,title={A ) in {nested} title},year="2026")',
    '@article{key,title="A } literal in quoted text",year={2026}}',
    '% @article{ignored,title={Comment}}\n@article{key,title={An escaped \\} brace}}',
])
def test_entry_locator_preserves_balanced_braces_quotes_parentheses_and_comments(text):
    spans=bibliography_import.entry_spans(text)
    assert len(spans)==1 and spans[0][0]=='key'
    assert text[spans[0][1]:spans[0][2]].startswith('@article') and spans[0][2]==len(text)
