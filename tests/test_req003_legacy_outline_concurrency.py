"""REQ-003 legacy outline versioning and conflict protection."""
from concurrent.futures import ThreadPoolExecutor
import threading
import app


def legacy_project(name='旧论文',content='旧大纲'):
 pid=__import__('uuid').uuid4().hex[:12];stamp=app.now();conn=app.db()
 with conn:
  conn.execute('INSERT INTO projects(id,name,domain,style,citation_style,created_at,updated_at,model,ai_base_url) VALUES(?,?,?,?,?,?,?,?,?)',(pid,name,'','','',stamp,stamp,'',''))
  if content is not None:conn.execute('INSERT INTO outline(project_id,content) VALUES(?,?)',(pid,content))
 conn.close();return pid


def test_legacy_first_save_preserves_old_text_and_rejects_stale_or_missing_version(client):
 pid=legacy_project();before=client.get(f'/api/projects/{pid}').json();assert not before['workflow_enabled'] and before['outline_version']==0 and before['outline']=='旧大纲'
 missing=client.put(f'/api/projects/{pid}/outline',json={'content':'缺少版本'});assert missing.status_code==422 and missing.json()['detail']['code']=='base_version_required'
 conn=app.db();assert conn.execute("SELECT count(*) FROM content_versions WHERE project_id=?",(pid,)).fetchone()[0]==0;conn.close()
 saved=client.put(f'/api/projects/{pid}/outline',json={'content':'新大纲','base_version':0});assert saved.status_code==200 and saved.json()['version']==2
 stale=client.put(f'/api/projects/{pid}/outline',json={'content':'覆盖旧稿','base_version':0});assert stale.status_code==409 and stale.json()['detail']['code']=='content_conflict'
 current=client.get(f'/api/projects/{pid}').json();assert current['outline']=='新大纲' and current['outline_version']==2
 conn=app.db();rows=conn.execute("SELECT version,source,content FROM content_versions WHERE project_id=? AND target_kind='outline' ORDER BY version",(pid,)).fetchall();conn.close();assert [(r['version'],r['source'],r['content']) for r in rows]==[(1,'legacy_outline_baseline','旧大纲'),(2,'legacy_outline_edit','新大纲')]


def test_legacy_concurrent_first_save_has_one_winner(client):
 pid=legacy_project();barrier=threading.Barrier(2)
 def write(value):
  barrier.wait();return client.put(f'/api/projects/{pid}/outline',json={'content':value,'base_version':0})
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,['修改甲','修改乙']))
 assert sorted(r.status_code for r in results)==[200,409]
 current=client.get(f'/api/projects/{pid}').json();assert current['outline'] in {'修改甲','修改乙'} and current['outline_version']==2
 conn=app.db();assert conn.execute("SELECT count(*) FROM content_versions WHERE project_id=? AND target_kind='outline'",(pid,)).fetchone()[0]==2;conn.close()


def test_legacy_first_save_failure_rolls_back_baseline_and_content(client):
 pid=legacy_project();conn=app.db()
 with conn:conn.execute("CREATE TRIGGER legacy_outline_failure BEFORE INSERT ON content_versions WHEN NEW.source='legacy_outline_edit' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
 conn.close();result=client.put(f'/api/projects/{pid}/outline',json={'content':'新稿','base_version':0});assert result.status_code==503
 current=client.get(f'/api/projects/{pid}').json();assert current['outline']=='旧大纲' and current['outline_version']==0
 conn=app.db();assert conn.execute('SELECT count(*) FROM content_versions WHERE project_id=?',(pid,)).fetchone()[0]==0
 with conn:conn.execute('DROP TRIGGER legacy_outline_failure')
 conn.close();assert client.put(f'/api/projects/{pid}/outline',json={'content':'新稿','base_version':0}).json()['version']==2


def test_legacy_missing_outline_and_unchanged_baseline_are_safe(client):
 pid=legacy_project(content=None);saved=client.put(f'/api/projects/{pid}/outline',json={'content':'','base_version':0});assert saved.status_code==200 and saved.json()['version']==1 and saved.json()['unchanged']
 assert client.get(f'/api/projects/{pid}').json()['outline']==''
 assert client.put(f'/api/projects/{pid}/outline',json={'content':'下一版','base_version':1}).json()['version']==2


def test_legacy_published_outline_is_read_only_without_migrating(client):
 pid=legacy_project();conn=app.db()
 with conn:conn.execute("UPDATE projects SET lifecycle_state='published' WHERE id=?",(pid,))
 conn.close();response=client.put(f'/api/projects/{pid}/outline',json={'content':'不可写','base_version':0});assert response.status_code==409 and response.json()['detail']['code']=='project_published_read_only'
 conn=app.db();assert conn.execute('SELECT content FROM outline WHERE project_id=?',(pid,)).fetchone()[0]=='旧大纲';assert conn.execute('SELECT count(*) FROM content_versions WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()
