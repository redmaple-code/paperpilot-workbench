"""Real UI integration against tests/ui_server.py's isolated synthetic workspace."""
import argparse
import json
from pathlib import Path
import uuid

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    checks=[]; errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000})
        req=context.request
        def api(path, body=None, method='POST'):
            response=req.fetch(args.base_url+path, method=method, data=body)
            assert response.ok, (path,response.status,response.text())
            return response.json()
        domain=api('/api/domains',{'name':'浏览器资源领域-'+uuid.uuid4().hex[:8]})
        paper=api('/api/projects',{'name':'第三阶段浏览器论文','domain_id':domain['id']})
        pid=paper['project']['id']; base=f'/api/projects/{pid}'
        api(f"/api/domains/{domain['id']}/resources",{'resource_type':'term','title':'资源术语','content':'领域原定义'})
        value=api(base+'/terms',{'term':'资源术语','definition':'论文独立定义'})
        api(base+'/outline/nodes',{'request_id':uuid.uuid4().hex,'command':'create','base_version':0,'node':{'id':'intro','title':'原始引言'}})
        corpus=api(base+'/corpus',{'title':'浏览器语料','content':'v1原文'})
        cbase=base+f"/corpus/{corpus['id']}"
        api(cbase+'/curations',{'base_source_version':1,'content':'C1人工校正','notes':'保留原绑定'})
        refresh=api(cbase+'/refresh',{'request_id':uuid.uuid4().hex,'base_source_version':1,'content':'v2新原文','permission_state':'authorized'})
        api(cbase+f"/refresh/{refresh['id']}/decision",{'request_id':uuid.uuid4().hex,'decision':'accept'})
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.base_url + f'/terms?project={pid}')
        page.locator('#termLibrary').get_by_role('button',name='已收录',exact=True).click()
        page.locator('#termLibrary').get_by_role('button',name='批量沉淀与分类').click()
        page.locator('#refreshTermReview').click()
        expect(page.locator(f'[data-classify-term="{value["id"]}"]')).to_be_visible()
        page.locator('#previewTermPromotion').click()
        page.locator(f'[data-promotion-choice="{value["id"]}"]').check()
        page.locator(f'[data-promotion-resolution="{value["id"]}"]').select_option('use_paper')
        page.locator('#commitTermPromotion').click()
        expect(page.locator('#termReviewWorkbenchStatus')).to_contain_text('已确认沉淀')
        assert api(f"/api/domains/{domain['id']}/terms?version=1",method='GET')[0]['definition']=='领域原定义'
        checks.append('领域冲突逐项确认，旧版本保留')
        page.locator(f'[data-classify-term="{value["id"]}"]').click()
        page.locator('#consentTermClassification').check();page.locator('#sendTermClassification').click()
        expect(page.locator('#termManualInbox')).to_contain_text('低置信度建议')
        assert api(f"/api/domains/{domain['id']}/terms",method='GET')[0]['definition']=='论文独立定义'
        page.locator('#termManualInbox [data-review-definition]').fill('人工复核的新定义')
        page.locator('#termManualInbox [data-review-replace]').check()
        page.locator('#termManualInbox [data-review-accept]').click()
        expect(page.locator('#termReviewWorkbenchStatus')).to_contain_text('已人工确认')
        checks.append('低置信度进入收件箱，编辑后才发布')
        page.get_by_role('dialog',name='批量沉淀与分类').get_by_role('button',name='关闭',exact=True).click()
        page.goto(args.base_url + f'/?project={pid}#chat/knowledge/corpus')
        page.locator('#corpusLineageWorkbench').locator('..').locator(':scope > summary').click()
        page.locator('#refreshCorpusLineage').click();page.locator('#corpusLineageChoice').select_option(corpus['id'])
        detail=page.locator('#corpusCurationVersions details').first;detail.locator('summary').click()
        detail.locator('[data-migration-content]').fill('C2迁移后人工校正')
        detail.locator('[data-preview-migration]').click()
        page.get_by_role('button',name='只采纳这项迁移',exact=True).click()
        expect(page.locator('#corpusMigrationCandidates')).to_contain_text('accepted')
        versions=api(cbase+'/lineage',method='GET')['curation_versions']
        assert [(v['source_version'],v['content']) for v in versions]==[(2,'C2迁移后人工校正'),(1,'C1人工校正')]
        checks.append('校正版逐项迁移，C1仍绑定v1')
        page.screenshot(path=str(out/'resources-desktop.png'),full_page=True)
        page.goto(args.base_url + f'/outline?project={pid}#outline/suggestions')
        page.locator('#structuredOutlineInstruction').fill('改进引言，再建议一个方法章节')
        page.locator('#previewStructuredOutline').click();page.locator('#consentStructuredOutline').check()
        page.locator('#generateStructuredOutline').click()
        expect(page.locator('[data-node-operation]')).to_have_count(2)
        page.locator('[data-node-operation="1"]').uncheck()
        page.get_by_role('button',name='仅采纳所选节点修改',exact=True).click()
        expect(page.locator('#structuredSuggestionWorkbenchStatus')).to_contain_text('只将选中的节点')
        nodes=api(base+'/outline/structured',method='GET')['nodes']
        assert len(nodes)==1 and nodes[0]['title']=='人工选择后的引言'
        checks.append('AI结构化节点diff，只应用所选项')
        workflow=api(base+'/workflow',method='GET')
        started=api(base+'/workflow/commands',{'command':'start','request_id':uuid.uuid4().hex,'base_version':workflow['version']})
        api(base+'/human-revision',{'workflow_version':started['version']})
        paper=api(base,method='GET');file=next(f for f in paper['files'] if f['path']=='main.tex')
        api(base+'/editor/draft',{'path':'main.tex','base_version':file['version'],'base_content':file['content'],'content':'浏览器恢复副本'},method='PUT')
        page.goto(args.base_url + f'/?project={pid}#chat/write')
        page.locator('#compareEditorDraft').click()
        expect(page.locator('#editorDraftComparison')).to_contain_text('服务器当前')
        page.locator('#draftCopyTarget').fill('browser-recovered.tex');page.locator('#saveComparedDraftCopy').click()
        expect(page.locator('#editorRecoveryWorkbenchStatus')).to_contain_text('已另存到')
        files={f['path']:f['content'] for f in api(base,method='GET')['files']}
        assert files['main.tex']==file['content'] and files['browser-recovered.tex']=='浏览器恢复副本'
        checks.append('三方草稿比较与另存不覆盖原文')
        page.set_viewport_size({'width':390,'height':844})
        page.goto(args.base_url + f'/terms?project={pid}')
        page.locator('#termLibrary').get_by_role('button',name='已收录',exact=True).click()
        page.locator('#termLibrary').get_by_role('button',name='批量沉淀与分类').click()
        page.locator('#refreshTermReview').focus();page.keyboard.press('Enter')
        expect(page.locator('#termReviewChoices')).to_contain_text('资源术语')
        page.screenshot(path=str(out/'resources-mobile.png'),full_page=True)
        checks.append('窄屏与键盘刷新路径可用')
        assert not errors, errors
        browser.close()
    result={'checks':checks,'passed':len(checks),'page_errors':errors,'mode':'isolated_browser_mock_provider','product_acceptance':False}
    (out/'RESOURCE-BROWSER.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
