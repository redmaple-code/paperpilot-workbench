"""All authoring consumers expose the preflight boundary in the real browser."""
from pathlib import Path
import json
import os
from datetime import datetime,timezone
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101'
URL=os.environ.get('PAPERPILOT_TEST_URL','http://127.0.0.1:8779')

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1100});req=context.request
    pid=req.post(URL+'/api/projects',data={'name':'全写作入口预检验证'}).json()['project']['id']
    node=req.post(URL+f'/api/projects/{pid}/outline/nodes',data={'command':'create','request_id':'browser-node','base_version':0,'node':{'id':'browser-node','title':'研究背景'}})
    assert node.ok,node.text()
    workflow=req.get(URL+f'/api/projects/{pid}/workflow').json()
    started=req.post(URL+f'/api/projects/{pid}/workflow/commands',data={'command':'start','base_version':workflow['version'],'request_id':'browser-start'}).json()
    assert req.post(URL+f'/api/projects/{pid}/human-revision',data={'workflow_version':started['version']}).ok
    original=req.get(URL+f'/api/projects/{pid}').json()['files']
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('window.paperpilot.isRevisionActive?.()')
    def select_heading():page.locator('#editor').evaluate('(el)=>{const start=el.value.indexOf("Introduction");el.focus();el.setSelectionRange(start,start+12);}')
    select_heading();page.locator('#workflowAssistAction').select_option('rewrite')
    page.locator('#workflowAssistPrompt').fill('只修改所选标题')
    page.locator('#workflowAssistGenerate').click()
    try:page.locator('#workflowPreflightControls .primary').wait_for(timeout=8000)
    except Exception as exc:raise AssertionError({'errors':errors,'announcement':page.locator('#workflowAnnouncement').inner_text(),'context':page.locator('#workflowContextStatus').inner_text()}) from exc
    assert req.get(URL+f'/api/projects/{pid}/ai-jobs').json()==[]
    assert '接收方' in page.locator('#workflowContextStatus').inner_text()
    page.locator('#workflowPreflightControls .primary').click()
    page.wait_for_function('document.querySelector("#workflowCandidate").textContent.includes("AI candidate text")')
    assert len(req.get(URL+f'/api/projects/{pid}/ai-jobs').json())==1
    page.locator('#continuationGoal').fill('补充研究背景');page.locator('#continuationSegments').fill('2')
    page.locator('#continuationStart').click()
    assert '请选择大纲节点' in page.locator('#workflowAnnouncement').inner_text()
    assert not req.get(URL+f'/api/projects/{pid}/continuations').json()
    page.locator('#continuationOutlineNode').select_option('browser-node')
    select_heading();page.locator('#continuationStart').click()
    page.wait_for_function('document.querySelector("#workflowPreflightControls .primary")?.textContent.includes("2 个续写")')
    assert not req.get(URL+f'/api/projects/{pid}/continuations').json()
    page.locator('#workflowPreflightControls .primary').click()
    page.wait_for_function('document.querySelector("#continuationList").textContent.includes("补充研究背景")')
    batches=req.get(URL+f'/api/projects/{pid}/continuations').json()
    assert len(batches)==1 and batches[0]['segment_count']==2 and batches[0]['outline_node_id']=='browser-node' and batches[0]['outline_version']==1
    page.locator('#modificationInstruction').fill('压缩所选标题');select_heading();page.locator('#createModification').click()
    try:page.wait_for_function('document.querySelector("#workflowPreflightControls .primary")?.textContent.includes("指定修改")',timeout=8000)
    except Exception as exc:raise AssertionError({'errors':errors,'modification':page.locator('#modificationStatus').inner_text(),'announcement':page.locator('#workflowAnnouncement').inner_text()}) from exc
    assert not req.get(URL+f'/api/projects/{pid}/modifications').json()
    page.locator('#workflowPreflightControls .primary').click()
    page.wait_for_function('document.querySelector("#modificationStatus").textContent.includes("请求已提交")')
    assert len(req.get(URL+f'/api/projects/{pid}/modifications').json())==1
    page.get_by_role('tab',name='知识库',exact=True).click()
    page.locator('#logicSuggest').click()
    page.locator('#logicAIPreflight').wait_for(state='visible')
    assert '接收方' in page.locator('#logicAIRecipient').inner_text()
    page.locator('#logicAIConsent').check();page.locator('#logicAIConfirm').click()
    page.wait_for_function('document.querySelector("#logicGraphStatus").textContent.includes("收到 0 项建议")')
    assert req.get(URL+f'/api/projects/{pid}').json()['files']==original
    audit=req.get(URL+f'/api/projects/{pid}/ai-audit').json()
    assert len(audit)==5 and all(r['status']=='succeeded' for r in audit),audit
    assert not errors,errors
    report={'requirement':'REQ-014 v16','time':datetime.now(timezone.utc).isoformat(),'passed':True,'model_calls':'explicit UI-server test double, not cloud evidence',
        'checks':['single candidate preflight and recipient','batch count and preflight','modification preflight','logic source/recipient preview and explicit consent','five common audit records','manuscript unchanged','no browser error']}
    (OUT/'REQ-014-AUTHORING-NODE-BINDING-20261102.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'checks':len(report['checks'])},ensure_ascii=False))
    browser.close()
