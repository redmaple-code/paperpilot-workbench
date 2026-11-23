from concurrent.futures import ThreadPoolExecutor
import app
from tests.test_req003 import create_project

def test_node_retries_replay_exactly_and_conflicting_payloads_do_not_change_graph(client):
 pid=create_project(client);url=f'/api/projects/{pid}/outline/nodes';create={'command':'create','request_id':'node-create-idem','base_version':0,'node':{'id':'n','title':'原章节'}}
 first=client.post(url,json=create);assert first.status_code==200,first.text
 other=client.post(url,json={**create,'node':{'id':'different','title':'其他内容'}});assert other.status_code==409 and other.json()['detail']['code']=='outline_request_id_conflict'
 delete={'command':'delete','request_id':'node-delete-idem','base_version':1,'node_id':'n'};removed=client.post(url,json=delete);assert removed.status_code==200,removed.text
 assert client.post(url,json=delete).json()==removed.json()
 assert client.post(url,json=create).json()==first.json()
 latest=client.get(f'/api/projects/{pid}/outline/structured').json();assert latest['version']==2 and not latest['nodes']
 assert client.post(url,json={**delete,'command':'update','node':{'title':'不应生效'}}).status_code==409

def test_restore_replay_and_cross_operation_or_legacy_id_conflicts(client):
 pid=create_project(client);url=f'/api/projects/{pid}/outline/nodes';created=client.post(url,json={'command':'create','request_id':'node-one','base_version':0,'node':{'id':'n','title':'第一版'}}).json()
 updated=client.post(url,json={'command':'update','request_id':'node-two','base_version':1,'node_id':'n','node':{'title':'第二版'}});assert updated.status_code==200
 restore_url=f'/api/projects/{pid}/outline/versions/1/restore';body={'request_id':'restore-one','base_version':2};first=client.post(restore_url,json=body);assert first.status_code==200
 assert client.post(restore_url,json=body).json()==first.json()
 assert client.post(f'/api/projects/{pid}/outline/versions/2/restore',json=body).status_code==409
 assert client.post(url,json={'command':'create','request_id':'restore-one','base_version':3,'node':{'id':'x','title':'不能复用'}}).status_code==409
 assert client.post(f'/api/projects/{pid}/outline/candidates',json={'request_id':'node-one','base_version':3,'operations':[]}).status_code==409
 candidate=client.post(f'/api/projects/{pid}/outline/candidates',json={'request_id':'new-candidate','base_version':3,'operations':[]}).json()
 collision=client.post(f"/api/projects/{pid}/outline/candidates/{candidate['id']}/decision",json={'request_id':'node-one','decision':'reject','base_version':3})
 assert collision.status_code==409 and collision.json()['detail']['code']=='outline_request_id_conflict'
 assert client.post(f'/api/projects/{pid}/outline/migration/preview',json={'request_id':'node-one'}).status_code==409
 conn=app.db()
 with conn:conn.execute('DELETE FROM outline_command_requests WHERE project_id=? AND request_id=?',(pid,'node-two'))
 conn.close();response=client.post(url,json={'command':'update','request_id':'node-two','base_version':1,'node_id':'n','node':{'title':'第二版'}})
 assert response.status_code==409 and response.json()['detail']['code']=='outline_request_id_conflict'
 assert client.get(f'/api/projects/{pid}/outline/structured').json()['version']==3

def test_command_record_failure_rolls_back_and_retry_is_safe(client):
 pid=create_project(client);conn=app.db()
 with conn:conn.execute("CREATE TRIGGER fail_outline_request BEFORE INSERT ON outline_command_requests BEGIN SELECT RAISE(ABORT,'injected'); END")
 conn.close();body={'command':'create','request_id':'retry','base_version':0,'node':{'id':'n','title':'新节点'}}
 response=client.post(f'/api/projects/{pid}/outline/nodes',json=body);assert response.status_code==503
 assert client.get(f'/api/projects/{pid}/outline/structured').json()['version']==0
 conn=app.db()
 with conn:conn.execute('DROP TRIGGER fail_outline_request')
 conn.close();response=client.post(f'/api/projects/{pid}/outline/nodes',json=body);assert response.status_code==200 and response.json()['version']==1

def test_concurrent_different_payloads_with_same_request_have_one_winner(client):
 pid=create_project(client);url=f'/api/projects/{pid}/outline/nodes'
 def send(i):return client.post(url,json={'command':'create','request_id':'same','base_version':0,'node':{'id':f'n{i}','title':f'章节{i}'}})
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(send,[1,2]))
 assert sorted(r.status_code for r in results)==[200,409]
 graph=client.get(f'/api/projects/{pid}/outline/structured').json();assert graph['version']==1 and len(graph['nodes'])==1

def test_concurrent_identical_requests_replay_one_result(client):
 pid=create_project(client);url=f'/api/projects/{pid}/outline/nodes';body={'command':'create','request_id':'same-retry','base_version':0,'node':{'id':'n','title':'章节'}}
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:client.post(url,json=body),range(2)))
 assert [r.status_code for r in results]==[200,200] and results[0].json()==results[1].json()
 assert client.get(f'/api/projects/{pid}/outline/structured').json()['version']==1
