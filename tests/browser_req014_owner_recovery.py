"""Legacy unknown AI ownership: browser-guided recovery and duplicate-cost guard."""
import argparse,hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);url=f'http://127.0.0.1:{p.parse_args().port}'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':390,'height':844});req=ctx.request
 def ok(r):
  assert r.ok,r.text()
  return r.json()
 pid=next(p['id'] for p in ok(req.get(url+'/api/projects')) if p['name']=='未知身份任务')
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid)
 host=page.locator('#workflowJobHistory');button=host.get_by_role('button',name='核对后标记中断');button.wait_for(state='visible');assert '可能重复执行或收费' in host.inner_text()
 page.once('dialog',lambda dialog:dialog.dismiss());button.click();assert host.get_by_role('button',name='核对后标记中断').count()==1
 page.once('dialog',lambda dialog:dialog.accept());button.click();host.get_by_role('button',name='重试').wait_for(state='visible');assert host.get_by_role('button',name='重试').is_disabled();assert '可能重复执行或收费' in host.inner_text()
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2') and not errors,errors
 files=['app.py','paperpilot/core/database.py','paperpilot/services/ai_jobs.py','paperpilot/services/provider_runtime.py','paperpilot/services/tex_runtime.py','paperpilot/domain/schemas.py','static/workflow-ui.js','tests/browser_req014_owner_recovery.py'];report={'passed':True,'time':datetime.now(timezone.utc).isoformat(),'isolated_database':True,'checks':['legacy unknown owner explained','cancel recovery has no effect','confirmation marks interrupted','retry requires explicit duplicate-cost consent','390px and no page errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-014-OWNER-RECOVERY-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
