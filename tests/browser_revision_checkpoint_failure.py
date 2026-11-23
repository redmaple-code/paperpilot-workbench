import json
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28494'
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);page=browser.new_page();page.goto(URL);page.locator('#newDialog[open]').wait_for();page.locator('#newName').fill('revision failure test');page.get_by_role('button',name='创建',exact=True).click();page.wait_for_function("document.querySelector('#workflowStatus')?.textContent.includes('待开始')")
 page.get_by_role('button',name='进入人工修订阶段').click();page.wait_for_function("document.querySelector('#workflowStatus')?.textContent.includes('人工修订')");page.get_by_role('button',name='进入人工修订模式').click();page.wait_for_function("document.querySelector('#workflowModeState')?.textContent.includes('已进入')")
 calls=[]
 def route_handler(route):
  if route.request.method=='POST': route.fulfill(status=503,content_type='application/json',body='{"detail":"保存失败测试"}')
  else: route.continue_()
 page.route('**/human-revision/drafts',route_handler)
 page.on('request',lambda req: calls.append(req.url) if req.url.endswith('/human-revision/checkpoint') else None)
 page.locator('#editor').fill('请保留失败后的输入');page.locator('#saveFile').click();page.wait_for_function("document.querySelector('#workflowAnnouncement').textContent.includes('保存失败测试')")
 assert page.locator('#editor').input_value()=='请保留失败后的输入';assert not calls;assert not page.locator('#editor').is_disabled()
 page.unroute_all();page.locator('#saveFile').click();page.wait_for_function("document.querySelector('#workflowAnnouncement').textContent.includes('已保存检查点')");assert page.locator('#editor').input_value()=='请保留失败后的输入'
 def race(route):
  data=json.loads(route.request.post_data);draft={k:data[k] for k in ('target_kind','target_path','base_version')};draft['content']='另一页面的新草稿'
  existing=page.request.get(route.request.url.replace('/checkpoint','/drafts')).json();draft['expected_draft_updated_at']=next(x['updated_at'] for x in existing if x['target_kind']==draft['target_kind'] and x['target_path']==draft['target_path'])
  result=page.request.post(route.request.url.replace('/checkpoint','/drafts'),data=draft);assert result.ok,result.text();route.continue_()
 page.route('**/human-revision/checkpoint',race);page.locator('#editor').fill('本页面的输入');page.locator('#saveFile').click();page.wait_for_function("document.querySelector('#workflowAnnouncement').textContent.includes('草稿已被其他页面更新')");assert page.locator('#editor').input_value()=='本页面的输入';assert not page.locator('#editor').is_disabled()
 browser.close();print('browser failure preservation, retry and stale checkpoint passed')
