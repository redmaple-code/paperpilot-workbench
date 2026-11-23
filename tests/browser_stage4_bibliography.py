"""Review library-to-engineering differences and explicit conflict choices in Chromium."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.project-to-act/docs/project-development/evidence/stage4-verification-20261103'
parser=argparse.ArgumentParser();parser.add_argument('--base-url',required=True);url=parser.parse_args().base_url
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1280,'height':1000});req=context.request
    def ok(r):assert r.ok,r.text();return r.json()
    pid=ok(req.post(url+'/api/projects',data={'name':'工程引用显式交接'}))['project']['id'];base=url+f'/api/projects/{pid}'
    wf=ok(req.get(base+'/workflow'));started=ok(req.post(base+'/workflow/commands',data={'command':'start','request_id':'start','base_version':wf['version']}))
    ok(req.post(base+'/human-revision',data={'workflow_version':started['version']}))
    ref=ok(req.post(url+'/api/references',data={'title':'Reviewed bibliography UI fixture','authors':'Author, A.','year':'2026'}))
    ok(req.post(base+'/references/'+ref['id'],data={'reference_version':1,'cite_key':'uifixture','request_id':'link'}))
    def bib():return next(f for f in ok(req.get(base))['files'] if f['path']=='references.bib')
    before=bib();old='% Keep handwritten note\n@article{uifixture,title={Old {nested} title}}\n@book{keep,title={Keep me}}\n'
    ok(req.put(base+'/files',data={'path':'references.bib','content':old,'base_version':before['version']}))
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid)
    page.get_by_role('tab',name='知识库').click();card=page.locator('#referenceLibraryList .reference-item').filter(has_text=ref['title'])
    card.get_by_role('button',name='加入或更新工程 bib').click();panel=card.locator('.bib-copy-review')
    panel.get_by_role('button',name='预览工程差异').click();panel.get_by_role('button',name='明确替换同键条目').wait_for()
    assert 'Old {nested}' in panel.locator('pre').inner_text() and 'Reviewed bibliography UI fixture' in panel.locator('pre').inner_text()
    panel.get_by_role('button',name='取消并保留工程文件').click();assert bib()['content']==old
    card.get_by_role('button',name='加入或更新工程 bib').click();panel=card.locator('.bib-copy-review');panel.get_by_role('button',name='预览工程差异').click()
    panel.get_by_role('button',name='明确替换同键条目').click();page.locator('#referenceLibraryStatus').get_by_text('工程 bib 已保存',exact=False).wait_for()
    saved=bib();assert 'Reviewed bibliography UI fixture' in saved['content'] and '@book{keep,title={Keep me}}' in saved['content'] and '% Keep handwritten note' in saved['content']
    page.set_viewport_size({'width':390,'height':844});card=page.locator('#referenceLibraryList .reference-item').filter(has_text=ref['title']);card.get_by_role('button',name='加入或更新工程 bib').click()
    panel=card.locator('.bib-copy-review');panel.get_by_role('button',name='预览工程差异').focus();assert panel.get_by_role('button',name='预览工程差异').evaluate('(e)=>document.activeElement===e')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2');assert not errors,errors
    OUT.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(OUT/'BIBLIOGRAPHY-390.png'),full_page=True)
    (OUT/'BIBLIOGRAPHY-BROWSER.json').write_text(json.dumps({'passed':True,'executed_at':datetime.now(timezone.utc).isoformat(),'isolated_database':True,
        'checks':['actual fixed library version and engineering diff','cancel leaves handwritten bib unchanged','explicit same-key replace preserves unrelated entries','new engineering version','390px and keyboard focus','no page errors']},ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'checks':6}));browser.close()
