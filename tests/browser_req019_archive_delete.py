"""Archived history is read-only, and explicit deletion preserves handoff."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:33486'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 pid=next(x['id'] for x in ok(req.get(url+'/api/projects')) if x['name']=='快照论文甲')
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.locator(f'.project-item[data-id="{pid}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="knowledge"]').click();page.locator('#academicQuery').fill('graphene');page.locator('#academicSearch').click();page.get_by_text('原始检索候选',exact=False).first.wait_for()
 histories=ok(req.get(url+f'/api/projects/{pid}/searches'))['searches'];sid=histories[0]['id'];snap=ok(req.get(url+f'/api/projects/{pid}/search/{sid}'));cid=snap['candidates'][0]['id'];handoff=ok(req.post(url+f'/api/projects/{pid}/search/handoff',data=json.dumps({'candidate_id':cid}),headers={'Content-Type':'application/json'}));draft=handoff['reference_draft'];trace=draft['source_trace']
 page.locator('#archiveProject').click();page.get_by_role('button',name='恢复论文').wait_for();assert page.locator('#academicSearch').is_disabled();delete=page.locator(f'[data-delete-history="{sid}"]');delete.wait_for();assert delete.is_disabled();page.locator(f'[data-history="{sid}"]').click();page.get_by_text('只能查看检索历史',exact=False).first.wait_for();assert page.locator(f'[data-save-candidate="{cid}"]').is_disabled()
 page.locator('#archiveProject').click();page.get_by_role('button',name='归档论文').wait_for();page.wait_for_function('()=>!document.querySelector("#academicSearch").disabled');delete=page.locator(f'[data-delete-history="{sid}"]');delete.wait_for();page.once('dialog',lambda dialog:dialog.accept());delete.click();page.get_by_text('检索记录已删除',exact=False).wait_for();assert req.get(url+f'/api/projects/{pid}/search/{sid}').status==404;assert ok(req.get(url+'/api/references/drafts/'+draft['id']))['source_trace']==trace;assert not errors,errors
 files=['app.py','static/app.js','static/index.html','tests/test_req019_archive_delete.py','tests/browser_req019_archive_delete.py'];report={'passed':True,'isolated_database':True,'fixture_provider':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['archive disables writes and permits history viewing','restore enables writes','explicit deletion removes search','handoff provenance retained','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-019-ARCHIVE-DELETE-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
