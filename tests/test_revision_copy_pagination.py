import app
from tests.test_req001 import create_project

def test_revision_copy_pages_are_scoped_stable_and_do_not_return_full_content(client):
 a=create_project(client)['project']['id'];b=create_project(client)['project']['id'];conn=app.db()
 with conn:
  for i in range(35):
   conn.execute('INSERT INTO revision_draft_copies(project_id,copy_id,target_kind,target_path,content,base_version,updated_at) VALUES(?,?,?,?,?,?,?)',(a,f'copy-{i:02d}','file','main.tex',f'内容{i:02d}'+('x'*200),1,'2026-01-01T00:00:00+00:00'))
  conn.execute('INSERT INTO revision_draft_copies(project_id,copy_id,target_kind,target_path,content,base_version,updated_at) VALUES(?,?,?,?,?,?,?)',(b,'other','file','main.tex','另一个论文',1,'2026-01-01T00:00:00+00:00'))
 conn.close();url=f'/api/projects/{a}/human-revision/draft-copies-page';first=client.get(url,params={'limit':20}).json();assert len(first['copies'])==20 and first['next_cursor'];assert all('content' not in x and len(x['preview'])==100 for x in first['copies']);cursor=first['next_cursor'];second=client.get(url,params={'limit':20,'before_updated_at':cursor['updated_at'],'before_copy_id':cursor['copy_id']}).json();assert len(second['copies'])==15 and second['next_cursor'] is None
 ids=[x['copy_id'] for x in first['copies']+second['copies']];assert len(set(ids))==35 and 'other' not in ids
 assert client.get(f'/api/projects/{a}/human-revision/draft-copies/copy-00').json()['content'].startswith('内容00')
 assert client.get(f'/api/projects/{b}/human-revision/draft-copies/copy-00').status_code==404
 assert client.get(url,params={'before_copy_id':'copy-01'}).status_code==422
 assert client.get(url,params={'limit':101}).status_code==422
