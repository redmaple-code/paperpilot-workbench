"""Historical restore is reviewed, versioned and atomic across all files."""
import json
import sqlite3

import pytest

import app
from tests.test_req001 import save_revision
from paperpilot.services import human_revision
from tests.test_req011_builds import _project
from tests.test_req011_sources_queue import fake_compiler, main_file, save_main, editor_draft


def setup_history(client, monkeypatch):
    pid = _project(client)
    observed = []
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler(observed))
    built = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'historical', 'purpose': 'submitted'})
    assert built.status_code == 200, built.text
    old = client.get(f'/api/projects/{pid}').json()['files']
    for file in old:
        response = client.put(f'/api/projects/{pid}/files', json={'path': file['path'], 'base_version': file['version'], 'content': file['content'] + '\n% newer body'})
        assert response.status_code == 200, response.text
    return pid, built.json()['build_job']['id'], old, observed


def preview(client, pid, job, paths=None):
    return client.post(f'/api/projects/{pid}/builds/{job}/restore-preview', json={'include_paths': paths or []})


def prepare(client, pid, job, paths=None, request='prepare'):
    view = preview(client, pid, job, paths)
    assert view.status_code == 200, view.text
    response = client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': request, 'include_paths': paths or [], 'preview_fingerprint': view.json()['preview_fingerprint']})
    assert response.status_code == 200, response.text
    return response.json()


def commit(client, pid, candidate, request='commit'):
    return client.post(f"/api/projects/{pid}/human-revision/build-restores/{candidate['restore_id']}/commit", json={'request_id': request, 'candidate_fingerprint': candidate['candidate_fingerprint']})


def persisted(client, pid):
    conn = app.db()
    try:
        return {table: [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE project_id=? ORDER BY rowid', (pid,))]
            for table in ['files', 'content_versions', 'editor_local_drafts', 'revision_drafts', 'build_restore_events', 'assist_action_events', 'build_jobs', 'build_snapshots', 'project_setting_baselines', 'project_setting_overrides', 'project_setting_events', 'project_build_profile_overrides', 'project_build_profile_events']}
    finally:
        conn.close()


def test_full_diff_is_read_only_then_bundle_commits_new_versions_and_rebuilds(client, monkeypatch):
    pid, job, old, observed = setup_history(client, monkeypatch)
    before = persisted(client, pid)
    view = preview(client, pid, job)
    assert view.status_code == 200, view.text
    assert view.json()['can_prepare'] and all(f['changed'] and '-% newer body' in f['diff'] for f in view.json()['files'])
    assert 'historical_build_profile' in view.json()['configuration']
    assert persisted(client, pid) == before, 'preview and cancel must not write'
    candidate = prepare(client, pid, job)
    assert client.get(f'/api/projects/{pid}').json()['files'] != old
    response = commit(client, pid, candidate)
    assert response.status_code == 200, response.text
    result = response.json()
    restored = client.get(f'/api/projects/{pid}').json()['files']
    assert {f['path']: f['content'] for f in restored} == {f['path']: f['content'] for f in old}
    assert all(f['version'] == next(o['version'] for o in old if o['path'] == f['path']) + 2 for f in restored)
    assert client.get(result['status_url']).json()['status'] == 'succeeded'
    assert len(observed) == 2
    assert client.get(f'/api/projects/{pid}/human-revision/drafts').json() == []
    replay = commit(client, pid, candidate)
    assert replay.status_code == 200 and replay.json()['idempotent_replay']
    assert replay.json()['build_job']['id'] == result['build_job']['id'] and len(observed) == 2
    assert commit(client, pid, candidate, 'another-commit').status_code == 409


@pytest.mark.parametrize('draft_kind', ['editor', 'revision'])
def test_existing_unsubmitted_draft_blocks_all_staging_without_overwrite(client, monkeypatch, draft_kind):
    pid, job, old, _ = setup_history(client, monkeypatch)
    file = main_file(client, pid)
    if draft_kind == 'editor':
        editor_draft(client, pid, file['content'] + '\nUNCOMMITTED EDITOR')
    else:
        assert save_revision(client,pid,{'target_kind': 'file', 'target_path': 'main.tex', 'base_version': file['version'], 'content': file['content'] + '\nUNCOMMITTED REVISION'}).status_code == 200
    before = persisted(client, pid)
    view = preview(client, pid, job).json()
    assert not view['can_prepare'] and next(f for f in view['files'] if f['path'] == 'main.tex')['conflict']
    denied = client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': 'conflict', 'preview_fingerprint': view['preview_fingerprint']})
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'build_restore_conflict'
    assert persisted(client, pid) == before
    candidate = prepare(client, pid, job, ['references.bib'])
    applied = commit(client, pid, candidate)
    assert applied.status_code == 200, applied.text
    assert main_file(client, pid)['content'] == file['content']
    if draft_kind == 'editor':
        assert client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()['content'].endswith('UNCOMMITTED EDITOR')
    else:
        assert client.get(f'/api/projects/{pid}/human-revision/drafts').json()[0]['content'].endswith('UNCOMMITTED REVISION')


def test_preview_and_prepared_candidate_both_reject_later_edits(client, monkeypatch):
    pid, job, _, _ = setup_history(client, monkeypatch)
    view = preview(client, pid, job).json()
    save_main(client, pid, main_file(client, pid)['content'] + '\n% raced edit')
    response = client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': 'stale', 'preview_fingerprint': view['preview_fingerprint']})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'build_restore_preview_stale'
    candidate = prepare(client, pid, job)
    file = main_file(client, pid)
    editor_draft(client, pid, file['content'] + '\nKEEP THIS INPUT')
    before = persisted(client, pid)
    response = commit(client, pid, candidate)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'build_restore_candidate_stale'
    assert persisted(client, pid) == before


@pytest.mark.parametrize('kind', ['editor', 'revision'])
def test_repeated_persistence_of_identical_draft_does_not_invalidate_review(client, monkeypatch, kind):
    pid, job, old, _ = setup_history(client, monkeypatch)
    content = next(f for f in old if f['path'] == 'main.tex')['content']
    file = main_file(client, pid)
    def persist():
        if kind == 'editor':
            editor_draft(client, pid, content)
        else:
            response = save_revision(client,pid,{'target_kind': 'file', 'target_path': 'main.tex', 'base_version': file['version'], 'content': content})
            assert response.status_code == 200, response.text
    persist()
    view = preview(client, pid, job).json()
    assert view['can_prepare']
    persist()  # Same bytes and baseline, different metadata timestamp.
    response = client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': 'same-draft', 'preview_fingerprint': view['preview_fingerprint']})
    assert response.status_code == 200, response.text
    candidate = response.json()
    persist()
    applied = commit(client, pid, candidate)
    assert applied.status_code == 200, applied.text
    assert main_file(client, pid)['content'] == content
    if kind == 'editor':
        kept = client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()
        assert kept['content'] == content and not kept['stale']
        assert kept['base_version'] == main_file(client, pid)['version']


def test_second_file_commit_failure_rolls_back_versions_body_drafts_and_jobs(client, monkeypatch):
    pid, job, _, _ = setup_history(client, monkeypatch)
    candidate = prepare(client, pid, job)
    before = persisted(client, pid)
    actual = human_revision.save_content
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(args[3])
        if len(calls) == 2:
            raise sqlite3.OperationalError('controlled second file disk failure')
        return actual(*args, **kwargs)
    monkeypatch.setattr(human_revision, 'save_content', fail_second)
    response = commit(client, pid, candidate)
    assert response.status_code == 503 and response.json()['detail']['code'] == 'build_restore_write_failed'
    assert len(calls) == 2 and persisted(client, pid) == before
    monkeypatch.setattr(human_revision, 'save_content', actual)
    assert commit(client, pid, candidate).status_code == 200


def test_second_staged_draft_failure_rolls_back_all_staging(client, monkeypatch):
    pid, job, _, _ = setup_history(client, monkeypatch)
    before = persisted(client, pid)
    actual = human_revision.save_draft
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError('controlled staging failure')
        return actual(*args, **kwargs)
    monkeypatch.setattr(human_revision, 'save_draft', fail_second)
    response = client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': 'failed-stage'})
    assert response.status_code == 503 and len(calls) == 2
    assert persisted(client, pid) == before


def test_full_rebuild_queue_causes_entire_commit_to_roll_back(client, monkeypatch):
    pid, job, _, _ = setup_history(client, monkeypatch)
    candidate = prepare(client, pid, job)
    monkeypatch.setenv('PWA_BUILD_QUEUE_LIMIT', '1')
    monkeypatch.setattr(app, 'execute_build_job', lambda *a, **k: None)
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'occupy', 'purpose': 'submitted'})
    assert response.status_code == 200
    before = persisted(client, pid)
    denied = commit(client, pid, candidate)
    assert denied.status_code == 429 and denied.json()['detail']['code'] == 'build_queue_full'
    assert persisted(client, pid) == before


@pytest.mark.parametrize('guard', ['archived', 'published', 'stage'])
def test_preview_stage_and_commit_recheck_permissions_without_transition(client, monkeypatch, guard):
    pid, job, _, _ = setup_history(client, monkeypatch)
    candidate = prepare(client, pid, job)
    conn = app.db()
    with conn:
        if guard == 'stage':
            conn.execute("UPDATE workflow_snapshots SET current_stage='completed' WHERE project_id=?", (pid,))
        elif guard == 'archived':
            conn.execute('UPDATE projects SET archived=1 WHERE id=?', (pid,))
        else:
            conn.execute("UPDATE projects SET lifecycle_state='published' WHERE id=?", (pid,))
    conn.close()
    before = persisted(client, pid)
    if guard == 'stage':
        # DEC-078 / PR1-04: the position is independent of history access.
        assert preview(client, pid, job).status_code == 200
        restored = commit(client, pid, candidate)
        assert restored.status_code == 200, restored.text
        assert client.get(f'/api/projects/{pid}/workflow').json()['current_stage'] == 'completed'
        return
    assert preview(client, pid, job).status_code == 409
    assert client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': 'new-prepare'}).status_code == 409
    assert commit(client, pid, candidate).status_code == 409
    assert persisted(client, pid) == before


def test_restore_request_scope_integrity_and_cross_paper_boundaries(client, monkeypatch):
    pid, job, _, _ = setup_history(client, monkeypatch)
    assert preview(client, pid, job, ['not-in-history.tex']).status_code == 422
    assert preview(client, pid, job, ['main.tex', 'main.tex']).status_code == 422
    candidate = prepare(client, pid, job, ['main.tex'])
    changed = client.post(f'/api/projects/{pid}/builds/{job}/restore', json={'request_id': 'prepare', 'include_paths': ['references.bib']})
    assert changed.status_code == 409
    other = _project(client)
    assert preview(client, other, job).status_code == 404
    assert commit(client, other, candidate).status_code == 404
    bad = {**candidate, 'candidate_fingerprint': '0'*64}
    assert commit(client, pid, bad).status_code == 409


def test_deleted_file_can_be_recreated_as_new_version_without_deleting_current_only_files(client, monkeypatch):
    from paperpilot.services.content_versions import delete_content, save_content
    pid, job, old, _ = setup_history(client, monkeypatch)
    conn = app.db()
    with conn:
        delete_content(conn, pid, 'references.bib', 2)
        save_content(conn, pid, 'file', 'keep.tex', 'CURRENT ONLY', expected_version=0)
    conn.close()
    view = preview(client, pid, job).json()
    assert view['current_only_paths'] == ['keep.tex']
    assert not next(f for f in view['files'] if f['path'] == 'references.bib')['current_exists']
    response = commit(client, pid, prepare(client, pid, job))
    assert response.status_code == 200, response.text
    files = {f['path']: f for f in client.get(f'/api/projects/{pid}').json()['files']}
    assert files['references.bib']['version'] == 4 and files['keep.tex']['content'] == 'CURRENT ONLY'


def test_historical_asset_integrity_is_rechecked_at_preview_and_commit(client, monkeypatch):
    import base64
    import io
    from PIL import Image
    from paperpilot.services import visual_assets
    pid = _project(client)
    stream = io.BytesIO();Image.new('RGB', (40, 30), 'red').save(stream, 'PNG')
    imported = client.post(f'/api/projects/{pid}/drawing/assets/import', json={'filename': 'restore.png', 'request_id': 'asset', 'content_base64': base64.b64encode(stream.getvalue()).decode()}).json()
    asset, version = imported['asset'], imported['version']
    file = main_file(client, pid)
    inserted = client.post(f"/api/projects/{pid}/drawing/assets/{asset['id']}/insert-into-editor", json={'version_id': version['id'], 'request_id': 'insert', 'target_path': 'main.tex', 'base_file_version': file['version'], 'caption': 'restore figure'})
    assert inserted.status_code == 200, inserted.text
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler([]))
    built = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'asset-history', 'purpose': 'submitted'}).json()
    job = built['build_job']['id']
    candidate = prepare(client, pid, job)
    conn = app.db();path = visual_assets.checked_file(conn, app.PROJECTS_DIR, pid, version['id']);conn.close()
    original = path.read_bytes();path.write_bytes(b'corrupted historical image')
    view = preview(client, pid, job)
    assert view.status_code == 200 and not view.json()['can_prepare'] and not view.json()['assets'][0]['available']
    before = persisted(client, pid)
    denied = commit(client, pid, candidate)
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'build_restore_resources_missing'
    assert persisted(client, pid) == before
    path.write_bytes(original)
    assert commit(client, pid, candidate).status_code == 200


def test_current_only_file_can_be_reviewed_removed_and_recovered_as_version(client,monkeypatch):
    from paperpilot.services.content_versions import save_content, restore_content
    from paperpilot.services.editor import save_draft
    pid,job,_,_=setup_history(client,monkeypatch);conn=app.db()
    with conn:
        save_content(conn,pid,'file','extra.tex','CURRENT ONLY',expected_version=0)
        save_draft(conn,pid,'extra.tex',1,'CURRENT ONLY','CURRENT ONLY')
    conn.close();before=persisted(client,pid)
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'remove_paths':['extra.tex']}).json()
    assert view['can_prepare'] and view['current_only_files'][0]['selected'] and 'CURRENT ONLY' in view['current_only_files'][0]['diff']
    assert persisted(client,pid)==before
    response=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'remove-extra','remove_paths':['extra.tex'],'preview_fingerprint':view['preview_fingerprint']})
    assert response.status_code==200,response.text;candidate=response.json()
    assert candidate['removals'][0]['path']=='extra.tex'
    applied=commit(client,pid,candidate);assert applied.status_code==200,applied.text
    assert applied.json()['removed_versions']=={'extra.tex':2}
    assert not any(d['path']=='extra.tex' for d in persisted(client,pid)['editor_local_drafts'])
    assert 'extra.tex' not in {f['path'] for f in client.get(f'/api/projects/{pid}').json()['files']}
    conn=app.db()
    with conn: restored=restore_content(conn,pid,'file','extra.tex',1,2,'manual_restore')
    conn.close();assert restored['version']==3
    assert next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path']=='extra.tex')['content']=='CURRENT ONLY'


@pytest.mark.parametrize('draft_kind',['editor','revision'])
def test_current_only_file_with_draft_cannot_be_selected_for_removal(client,monkeypatch,draft_kind):
    from paperpilot.services.content_versions import save_content
    pid,job,_,_=setup_history(client,monkeypatch);conn=app.db()
    with conn:save_content(conn,pid,'file','extra.tex','CURRENT',expected_version=0)
    conn.close()
    if draft_kind=='editor':
        from paperpilot.services.editor import save_draft
        conn=app.db()
        with conn:save_draft(conn,pid,'extra.tex',1,'CURRENT','UNSAVED')
        conn.close()
    else:
        response=save_revision(client,pid,{'target_kind':'file','target_path':'extra.tex','base_version':1,'content':'UNSAVED'})
        assert response.status_code==200,response.text
    before=persisted(client,pid)
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'remove_paths':['extra.tex']}).json()
    assert not view['can_prepare'] and view['current_only_files'][0]['conflict']
    denied=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'cannot-remove','remove_paths':['extra.tex'],'preview_fingerprint':view['preview_fingerprint']})
    assert denied.status_code==409 and persisted(client,pid)==before


def test_removal_failure_rolls_back_restored_files_and_job(client,monkeypatch):
    from paperpilot.services.content_versions import save_content
    pid,job,_,_=setup_history(client,monkeypatch);conn=app.db()
    with conn:save_content(conn,pid,'file','extra.tex','KEEP',expected_version=0)
    conn.close()
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'remove_paths':['extra.tex']}).json()
    candidate=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'remove-fail','remove_paths':['extra.tex'],'preview_fingerprint':view['preview_fingerprint']}).json();before=persisted(client,pid)
    monkeypatch.setattr(human_revision,'delete_content',lambda *args,**kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('injected deletion failure')))
    result=commit(client,pid,candidate)
    assert result.status_code==503 and persisted(client,pid)==before


def test_removal_selection_and_late_edit_are_revalidated(client,monkeypatch):
    from paperpilot.services.content_versions import save_content
    from paperpilot.services.editor import save_draft
    pid,job,_,_=setup_history(client,monkeypatch);conn=app.db()
    with conn:save_content(conn,pid,'file','extra.tex','KEEP',expected_version=0)
    conn.close()
    for paths in [['main.tex'],['missing.tex'],['extra.tex','extra.tex']]:
        assert client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'remove_paths':paths}).status_code==422
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'remove_paths':['extra.tex']}).json()
    candidate=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'remove-race','remove_paths':['extra.tex'],'preview_fingerprint':view['preview_fingerprint']}).json()
    conn=app.db()
    with conn:save_draft(conn,pid,'extra.tex',1,'KEEP','LATE UNSAVED EDIT')
    conn.close();before=persisted(client,pid)
    denied=commit(client,pid,candidate)
    assert denied.status_code==409 and denied.json()['detail']['code']=='build_restore_candidate_stale'
    assert persisted(client,pid)==before


def set_setting(client,pid,key,value,request):
    revision=client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    response=client.put(f'/api/projects/{pid}/settings/{key}',json={'value':value,'expected_project_revision':revision,'request_id':request})
    assert response.status_code==200,response.text
    return response.json()


def test_selected_historical_settings_apply_atomically_and_rebuild(client,monkeypatch):
    pid,job,_,_=setup_history(client,monkeypatch)
    historical=client.get(f'/api/projects/{pid}/settings').json()['effective']
    set_setting(client,pid,'style','当前风格','style-new')
    set_setting(client,pid,'citation_style','当前引文','citation-new')
    before=client.get(f'/api/projects/{pid}/settings').json()['effective']
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':['style']}).json()
    rows={item['key']:item for item in view['configuration']['choices']}
    assert view['can_prepare'] and rows['style']['selected'] and rows['style']['historical']==historical['style']
    assert not rows['citation_style']['selected'] and before['style']=='当前风格'
    response=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'settings-prepare','setting_keys':['style'],'preview_fingerprint':view['preview_fingerprint']})
    assert response.status_code==200,response.text;candidate=response.json()
    assert client.get(f'/api/projects/{pid}/settings').json()['effective']==before
    result=commit(client,pid,candidate);assert result.status_code==200,result.text
    assert result.json()['restored_settings']=={'style':historical['style']}
    now=client.get(f'/api/projects/{pid}/settings').json()['effective']
    assert now['style']==historical['style'] and now['citation_style']=='当前引文'
    build=client.get(result.json()['status_url']).json();snapshot=client.get(f"/api/projects/{pid}/build-snapshots/{build['snapshot_id']}").json()
    assert snapshot['manifest']['settings']['style']==historical['style'] and snapshot['manifest']['settings']['citation_style']=='当前引文'
    assert commit(client,pid,candidate).json()['idempotent_replay']


def test_settings_restore_failure_rolls_back_files_settings_and_job(client,monkeypatch):
    from paperpilot.services import settings
    pid,job,_,_=setup_history(client,monkeypatch)
    set_setting(client,pid,'style','新风格','new-style');set_setting(client,pid,'citation_style','新引文','new-citation')
    keys=['style','citation_style'];view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':keys}).json()
    candidate=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'settings-fail','setting_keys':keys,'preview_fingerprint':view['preview_fingerprint']}).json()
    before=persisted(client,pid);settings_before=client.get(f'/api/projects/{pid}/settings').json()
    actual=settings.update_override;calls=[]
    def fail_second(*args,**kwargs):
        calls.append(args[2])
        if len(calls)==2:raise sqlite3.OperationalError('injected settings failure')
        return actual(*args,**kwargs)
    monkeypatch.setattr(settings,'update_override',fail_second)
    denied=commit(client,pid,candidate)
    assert denied.status_code==503 and len(calls)==2
    assert persisted(client,pid)==before and client.get(f'/api/projects/{pid}/settings').json()==settings_before


def test_old_unknown_domain_or_changed_domain_cannot_be_silently_restored(client,monkeypatch):
    from paperpilot.services import domain_workspace
    pid,job,_,_=setup_history(client,monkeypatch);conn=app.db()
    with conn:
        other=domain_workspace.create_domain(conn,'新的领域')
        rev=conn.execute('SELECT project_revision FROM projects WHERE id=?',(pid,)).fetchone()[0]
        domain_workspace.assign_project_domain(conn,pid,other['id'],rev)
    conn.close()
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':['domain']}).json()
    domain=next(item for item in view['configuration']['choices'] if item['key']=='domain')
    assert not view['can_prepare'] and not domain['available'] and '归属' in domain['reason']
    denied=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'domain-denied','setting_keys':['domain'],'preview_fingerprint':view['preview_fingerprint']})
    assert denied.status_code==409
    conn=app.db()
    with conn:
        row=conn.execute('SELECT s.id,s.manifest_json FROM build_snapshots s JOIN build_jobs b ON b.snapshot_id=s.id WHERE b.id=?',(job,)).fetchone()
        manifest=json.loads(row['manifest_json']);manifest.pop('project_domain_id');conn.execute('UPDATE build_snapshots SET manifest_json=? WHERE id=?',(json.dumps(manifest),row['id']))
    conn.close()
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':['domain']}).json()
    assert not view['can_prepare'] and '未记录领域归属身份' in next(i['reason'] for i in view['configuration']['choices'] if i['key']=='domain')


def test_settings_scope_and_changed_historical_copy_rejected(client,monkeypatch):
    pid,job,_,_=setup_history(client,monkeypatch)
    for keys in [['bad-key'],['style','style']]:
        assert client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':keys}).status_code==422
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':['style']}).json()
    candidate=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'settings-copy','setting_keys':['style'],'preview_fingerprint':view['preview_fingerprint']}).json()
    conn=app.db()
    with conn:
        row=conn.execute('SELECT s.id,s.settings_json FROM build_snapshots s JOIN build_jobs b ON b.snapshot_id=s.id WHERE b.id=?',(job,)).fetchone()
        contents=json.loads(row['settings_json']);contents['effective']['style']='损坏';conn.execute('UPDATE build_snapshots SET settings_json=? WHERE id=?',(json.dumps(contents),row['id']))
    conn.close();before=persisted(client,pid)
    result=commit(client,pid,candidate)
    assert result.status_code==409 and result.json()['detail']['code']=='build_restore_settings_unavailable' and persisted(client,pid)==before


def test_configuration_change_after_preview_or_prepare_rejects_stale_restore(client,monkeypatch):
    pid,job,_,_=setup_history(client,monkeypatch);keys=['style']
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':keys}).json()
    set_setting(client,pid,'citation_style','later','later-1')
    denied=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'stale-config','setting_keys':keys,'preview_fingerprint':view['preview_fingerprint']})
    assert denied.status_code==409 and denied.json()['detail']['code']=='build_restore_preview_stale'
    view=client.post(f'/api/projects/{pid}/builds/{job}/restore-preview',json={'setting_keys':keys}).json()
    candidate=client.post(f'/api/projects/{pid}/builds/{job}/restore',json={'request_id':'late-config','setting_keys':keys,'preview_fingerprint':view['preview_fingerprint']}).json()
    set_setting(client,pid,'format_policy','strict-review','later-2')
    before=persisted(client,pid);settings_before=client.get(f'/api/projects/{pid}/settings').json()
    denied=commit(client,pid,candidate)
    assert denied.status_code==409 and denied.json()['detail']['code']=='build_restore_candidate_stale'
    assert persisted(client,pid)==before and client.get(f'/api/projects/{pid}/settings').json()==settings_before
