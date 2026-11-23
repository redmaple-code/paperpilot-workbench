"""Free editing removes the mode gate, not version/readonly/idempotency boundaries."""
import app
from tests.test_req022_assets import project


def test_edit_before_planning_is_versioned_and_idempotent(client):
    pid=project(client)
    before=client.get(f'/api/projects/{pid}').json()
    current=next(f for f in before['files'] if f['path']=='main.tex')
    workflow=client.get(f'/api/projects/{pid}/workflow').json()
    payload={'path':'main.tex','base_version':current['version'],'content':current['content']+'\n% free editing','request_id':'free-edit'}
    url=f'/api/projects/{pid}/editor/save'
    first=client.put(url,json=payload)
    assert first.status_code==200,first.text
    assert first.json()['version']==current['version']+1
    assert client.put(url,json=payload).json()['idempotent_replay']
    assert client.put(url,json={**payload,'request_id':'stale-write'}).status_code==409
    assert client.get(f'/api/projects/{pid}/workflow').json()['version']==workflow['version']
    conn=app.db();assert conn.execute('SELECT count(*) FROM human_revision_modes WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()


def test_archived_paper_cannot_save_editor_or_stage_draft(client):
    pid=project(client)
    assert client.put(f'/api/projects/{pid}/archive').status_code==200
    result=client.put(f'/api/projects/{pid}/editor/save',json={'path':'main.tex','base_version':1,'content':'blocked','request_id':'archived'})
    assert result.status_code==403,result.text
    startup=client.get(f'/api/projects/{pid}/workflow/startup').json()
    result=client.post(f'/api/projects/{pid}/workflow/startup/commands',json={'request_id':'archived-stage','expected_workflow_version':startup['workflow_version'],
                      'command':'save','stage_id':'STAGE-04','fields':{'terms':'blocked'}})
    assert result.status_code==403,result.text
    assert client.get(f'/api/projects/{pid}/workflow/startup').json()==startup
