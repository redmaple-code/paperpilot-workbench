from tests.planning_helper import complete_planning
def _domain_and_project(client):
    domain = client.post("/api/domains", json={"name": "生物信息学"}).json()
    project = client.post("/api/projects", json={"name": "继承测试", "domain_id": domain["id"], "domain": "生物信息学", "style": "默认风格"}).json()
    return domain["id"], project["project"]["id"]


def test_domain_defaults_require_explicit_project_sync_and_project_override(client):
    domain_id, project_id = _domain_and_project(client)
    initial = client.get(f"/api/projects/{project_id}/settings").json()
    assert initial["baseline"]["domain_id"] == domain_id
    changed = client.put(
        f"/api/domains/{domain_id}/settings",
        json={"expected_version": 1, "request_id": "domain-settings-1", "settings": {"style": "领域新风格"}},
    )
    assert changed.status_code == 200, changed.text
    preview = client.get(f"/api/projects/{project_id}/settings/sync-preview").json()
    assert preview["available"] and any(item["key"] == "style" for item in preview["diff"])
    unchanged = client.get(f"/api/projects/{project_id}/settings").json()
    assert unchanged["effective"]["style"] == "默认风格"

    synced = client.post(
        f"/api/projects/{project_id}/settings/sync",
        json={"expected_baseline_version": initial["baseline"]["version"], "expected_domain_version": preview["to_version"], "selected_keys": ["style"], "request_id": "sync-1"},
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["effective"]["style"] == "领域新风格"

    project = client.get(f"/api/projects/{project_id}").json()["project"]
    override = client.put(
        f"/api/projects/{project_id}/settings/style",
        json={"value": "论文专用风格", "expected_project_revision": project["project_revision"], "request_id": "override-1"},
    )
    assert override.status_code == 200, override.text
    assert override.json()["sources"]["style"]["source"] == "project_override"
    restored = client.post(
        f"/api/projects/{project_id}/settings/style/restore",
        json={"expected_project_revision": override.json()["sources"]["style"].get("version", project["project_revision"] + 1), "request_id": "restore-1"},
    )
    # The endpoint uses project_revision for optimistic locking; retry with the
    # returned project revision when the per-key override version differs.
    if restored.status_code == 409:
        current = client.get(f"/api/projects/{project_id}").json()["project"]["project_revision"]
        restored = client.post(f"/api/projects/{project_id}/settings/style/restore", json={"expected_project_revision": current, "request_id": "restore-1b"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["effective"]["style"] == "领域新风格"


def test_format_check_uses_effective_settings_and_is_non_blocking(client):
    response = client.post("/api/projects", json={"name": "格式检查", "domain": "材料科学"})
    assert response.status_code == 200, response.text
    project_id = response.json()["project"]["id"]
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "start", "request_id": "format-start", "base_version": workflow["version"]},
    )
    assert started.status_code == 200, started.text
    entered = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": started.json()["version"]},
    )
    assert entered.status_code == 200, entered.text
    main = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    source = "\\documentclass{article}\n\\begin{document}\n\\cite bad\n"
    saved = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": source, "base_version": main["version"]},
    )
    assert saved.status_code == 200, saved.text
    checked = client.post(
        f"/api/projects/{project_id}/format-check",
        json={"target_path": "main.tex", "request_id": "format-run-1"},
    )
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["non_blocking"] is True
    assert body["summary"]["blocking_count"] == 0
    assert {item["code"] for item in body["warnings"]} >= {"missing_document_end", "malformed_citation_command"}
    assert all(item["path"] == "main.tex" and item["line"] >= 1 for item in body["warnings"])
    unsaved = client.post(
        f"/api/projects/{project_id}/format-check",
        json={"target_path": "main.tex", "content": source + "unsaved"},
    )
    assert unsaved.status_code == 409
    assert unsaved.json()["detail"]["code"] == "unsaved_draft_not_checkable"

    unrelated = client.post(
        f"/api/projects/{project_id}/corpus",
        json={"title": "无关语料", "content": "This change does not affect format checks."},
    )
    assert unrelated.status_code == 200, unrelated.text
    listed = client.get(f"/api/projects/{project_id}/format-check/runs/{body['run_id']}")
    assert listed.status_code == 200
    assert listed.json()["stale"] is False

    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    changed = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": source.replace("\\cite bad", "\\cite{fixed}"), "base_version": current["version"]},
    )
    assert changed.status_code == 200, changed.text
    stale = client.get(f"/api/projects/{project_id}/format-check/runs/{body['run_id']}")
    assert stale.status_code == 200 and stale.json()["stale"] is True
    assert "files" in stale.json()["stale_reason"]

    all_files = client.post(f"/api/projects/{project_id}/format-check", json={})
    assert all_files.status_code == 200, all_files.text
    assert all_files.json()["checked_files"]



def test_partial_sync_tracks_each_source_and_preserves_override(client):
    domain_id, project_id = _domain_and_project(client)
    before = client.get(f'/api/projects/{project_id}/settings').json()
    assert before['sources']['style']['source'] == 'project_baseline'
    assert before['sources']['term_policy']['source'] == 'domain_baseline'
    r = client.put(f'/api/domains/{domain_id}/settings', json={'expected_version':1,'request_id':'d-1','settings':{'style':'新风格','term_policy':'domain_preferred'}})
    assert r.status_code == 200
    preview = client.get(f'/api/projects/{project_id}/settings/sync-preview').json()
    payload={'expected_baseline_version':before['baseline']['version'],'expected_domain_version':preview['to_version'],'selected_keys':['term_policy'],'request_id':'partial-1'}
    synced=client.post(f'/api/projects/{project_id}/settings/sync',json=payload)
    assert synced.status_code == 200, synced.text
    view=synced.json();assert view['effective']['style']=='默认风格' and view['sources']['style']['source']=='project_baseline'
    assert view['effective']['term_policy']=='domain_preferred' and view['sources']['term_policy']['domain_version']==2
    assert view['sources']['format_policy']['domain_version']==1
    assert client.post(f'/api/projects/{project_id}/settings/sync',json=payload).json()['idempotent_replay'] is True
    assert client.post(f'/api/projects/{project_id}/settings/sync',json=payload | {'selected_keys':['style']}).status_code==409
    revision=client.get(f'/api/projects/{project_id}').json()['project']['project_revision']
    override=client.put(f'/api/projects/{project_id}/settings/term_policy',json={'value':'project_isolated','expected_project_revision':revision,'request_id':'partial-override'}).json()
    assert override['sources']['term_policy']['source']=='project_override'
    revision=client.get(f'/api/projects/{project_id}').json()['project']['project_revision']
    restored=client.post(f'/api/projects/{project_id}/settings/term_policy/restore',json={'expected_project_revision':revision,'request_id':'partial-restore'}).json()
    assert restored['sources']['term_policy']['domain_version']==2 and restored['effective']['term_policy']=='domain_preferred'


def test_preview_domain_version_change_rejected_and_old_provenance_unknown(client):
    import app
    domain_id, project_id = _domain_and_project(client)
    conn=app.db()
    with conn:conn.execute("UPDATE project_setting_baselines SET sources_json='{}' WHERE project_id=?",(project_id,))
    conn.close()
    prior=client.get(f'/api/projects/{project_id}/settings').json()
    assert all(v['source']=='unknown' for v in prior['sources'].values())
    assert client.put(f'/api/domains/{domain_id}/settings',json={'expected_version':1,'request_id':'version-2','settings':{'style':'版本2'}}).status_code==200
    preview=client.get(f'/api/projects/{project_id}/settings/sync-preview').json()
    assert client.put(f'/api/domains/{domain_id}/settings',json={'expected_version':2,'request_id':'version-3','settings':{'style':'版本3'}}).status_code==200
    payload={'expected_baseline_version':preview['baseline']['version'],'expected_domain_version':preview['to_version'],'selected_keys':['style'],'request_id':'stale-domain'}
    denied=client.post(f'/api/projects/{project_id}/settings/sync',json=payload)
    assert denied.status_code==409 and denied.json()['detail']['code']=='domain_version_conflict'
    assert client.get(f'/api/projects/{project_id}/settings').json()==prior
    fresh=client.get(f'/api/projects/{project_id}/settings/sync-preview').json()
    result=client.post(f'/api/projects/{project_id}/settings/sync',json=payload | {'expected_domain_version':fresh['to_version'],'request_id':'fresh-domain'}).json()
    assert result['sources']['style']['domain_version']==3 and result['sources']['format_policy']['source']=='unknown'


def test_mismatched_legacy_binding_blocks_cross_domain_sync_without_changes(client):
    import app
    domain_id, project_id = _domain_and_project(client)
    other=client.post('/api/domains',json={'name':'其他领域'}).json()
    conn=app.db()
    with conn:conn.execute('UPDATE projects SET domain_id=? WHERE id=?',(other['id'],project_id))
    conn.close()
    before=client.get(f'/api/projects/{project_id}/settings').json()
    preview=client.get(f'/api/projects/{project_id}/settings/sync-preview').json()
    assert not preview['available'] and preview['reason']=='domain_binding_mismatch'
    denied=client.post(f'/api/projects/{project_id}/settings/sync',json={'expected_baseline_version':before['baseline']['version'],'expected_domain_version':1,'selected_keys':['style'],'request_id':'mismatch'})
    assert denied.status_code==409 and denied.json()['detail']['code']=='domain_binding_mismatch'
    assert client.get(f'/api/projects/{project_id}/settings').json()==before


def test_old_schema_migration_keeps_values_and_reports_unknown_source(client):
    import app
    from paperpilot.core.database import initialize_schema
    _, project_id = _domain_and_project(client)
    before=client.get(f'/api/projects/{project_id}/settings').json()
    conn=app.db()
    with conn:conn.execute('ALTER TABLE project_setting_baselines DROP COLUMN sources_json')
    initialize_schema(conn);initialize_schema(conn);conn.close()
    after=client.get(f'/api/projects/{project_id}/settings').json()
    assert after['effective']==before['effective'] and after['baseline']['version']==before['baseline']['version']
    assert all(row['source']=='unknown' for row in after['sources'].values())
