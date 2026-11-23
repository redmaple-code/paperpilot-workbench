"""Recovery listing retains legacy and per-page outline drafts without cross-project leaks."""

def test_outline_draft_recovery_listing_scopes_paginates_and_keeps_legacy(client):
 a=client.post('/api/projects',json={'name':'草稿甲'}).json()['project']['id'];b=client.post('/api/projects',json={'name':'草稿乙'}).json()['project']['id']
 def put(pid,key,content):
  response=client.put(f'/api/projects/{pid}/outline/drafts/{key}',json={'draft_key':key,'base_version':1,'content':content});assert response.status_code==200
 put(a,'editor',{'text':'旧草稿'});put(a,'tab:page-a:editor',{'text':'甲窗口'});put(a,'tab:page-b:editor',{'text':'乙窗口'});put(b,'tab:page-c:editor',{'text':'另一论文'})
 put(a,'node:n1',{'node_id':'n1','node':{'title':'旧节点'}});put(a,'tab:page-a:node-a',{'node_id':'n1','node':{'title':'新节点'}});put(a,'tab:page-b:node-b',{'node_id':'n2','node':{'title':'其他节点'}})
 first=client.get(f'/api/projects/{a}/outline/draft-recovery',params={'kind':'editor','limit':2}).json();assert len(first['drafts'])==2 and first['has_more']
 second=client.get(f'/api/projects/{a}/outline/draft-recovery',params={'kind':'editor','limit':2,'offset':2}).json();assert not second['has_more']
 combined=first['drafts']+second['drafts'];assert {x['preview'] for x in combined}=={'旧草稿','甲窗口','乙窗口'}
 nodes=client.get(f'/api/projects/{a}/outline/draft-recovery',params={'kind':'node','node_id':'n1'}).json()['drafts'];assert {x['preview'] for x in nodes}=={'旧节点','新节点'}
 assert client.get(f'/api/projects/{a}/outline/draft-recovery',params={'kind':'node'}).status_code==422
 assert client.get(f'/api/projects/{b}/outline/drafts/editor').status_code==404

def test_conditional_delete_rejects_stale_draft_and_keeps_legacy_api(client):
 pid=client.post('/api/projects',json={'name':'并发删除'}).json()['project']['id']
 def put(key,text):
  response=client.put(f'/api/projects/{pid}/outline/drafts/{key}',json={'draft_key':key,'base_version':1,'content':{'text':text}})
  assert response.status_code==200
  return response.json()
 first=put('editor','初稿');put('other','其他草稿');latest=put('editor','新内容')
 assert first['updated_at']!=latest['updated_at']
 stale=client.delete(f'/api/projects/{pid}/outline/drafts/editor',params={'if_updated_at':first['updated_at']})
 assert stale.status_code==409 and stale.json()['detail']['code']=='outline_draft_changed'
 assert client.get(f'/api/projects/{pid}/outline/drafts/editor').json()['content']['text']=='新内容'
 assert client.delete(f'/api/projects/{pid}/outline/drafts/editor',params={'if_updated_at':latest['updated_at']}).json()['deleted']
 assert client.get(f'/api/projects/{pid}/outline/drafts/other').status_code==200
 assert client.delete(f'/api/projects/{pid}/outline/drafts/other').json()['deleted']
