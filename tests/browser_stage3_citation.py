import argparse
import json
from pathlib import Path
import uuid
from playwright.sync_api import sync_playwright,expect


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True);checks=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1440,'height':1000});req=context.request
        def api(path,body=None,method='POST'):
            r=req.fetch(args.base_url+path,method=method,data=body);assert r.ok,(r.status,r.text());return r.json()
        paper=api('/api/projects',{'name':'文献任务浏览器测试-'+uuid.uuid4().hex[:6],'domain':'证据审查'});pid=paper['project']['id'];base=f'/api/projects/{pid}'
        api(base+'/refs',{'cite_key':'browser2026','title':'浏览器隔离元数据来源'})
        workflow=api(base+'/workflow',method='GET');started=api(base+'/workflow/commands',{'command':'start','request_id':uuid.uuid4().hex,'base_version':workflow['version']})
        api(base+'/human-revision',{'workflow_version':started['version']})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(args.base_url);page.wait_for_load_state('networkidle')
        text=r'Browser draft claim under controlled conditions\cite{browser2026}. Another claim\cite{browser2026}.'
        page.locator('#editor').fill(text)
        page.locator('[data-tab="knowledge"]').click()
        page.locator('#previewCitationTask').click()
        expect(page.locator('#citationTaskPreflight')).to_contain_text('2条论断')
        expect(page.locator('#citationTaskPreflight')).to_contain_text('Browser draft claim')
        page.locator('#citationTaskConsent').check();page.locator('#startCitationTask').click()
        expect(page.locator('#citationTaskResult')).to_contain_text('已检查2/2',timeout=30000)
        expect(page.locator('#citationTaskResult')).to_contain_text('metadata_only')
        saved=api(base,method='GET');assert next(f for f in saved['files'] if f['path']=='main.tex')['content']!=text
        checks.append('默认当前草稿快照、逐项持久完成，不提交正文或伪称元数据支持')
        items=page.locator('#citationTaskItems article');items.first.locator('[data-citation-reason]').fill('仅保留作者表达，证据仍需补充')
        items.first.locator('[data-citation-save]').click()
        expect(page.locator('#citationTaskStatus')).to_contain_text('不会自动判定证据通过')
        checks.append('逐论断人工处置与证据结论分离')
        page.locator('#citationTaskWorkbench').screenshot(path=str(output/'citation-task-desktop.png'))
        page.locator('#deleteCitationTask').click();page.locator('#citationDeleteConsent').check();page.locator('#commitCitationDelete').click()
        expect(page.locator('#citationTaskResult')).to_contain_text('已永久清理')
        checks.append('报告预览后永久清理，保留最小墓碑')
        page.set_viewport_size({'width':390,'height':844});page.locator('#refreshCitationTasks').focus();page.keyboard.press('Enter')
        page.locator('#citationTaskWorkbench').screenshot(path=str(output/'citation-task-mobile.png'))
        checks.append('窄屏与键盘刷新任务')
        assert not errors,errors
        browser.close()
    result={'checks':checks,'passed':len(checks),'page_errors':errors,'mode':'isolated_browser_mock_provider','product_acceptance':False}
    (output/'CITATION-BROWSER.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
