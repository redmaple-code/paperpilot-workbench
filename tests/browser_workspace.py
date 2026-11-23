"""Workspace regression against an isolated real application; never point at user storage.

Creates named synthetic papers. No model generation or paid provider test is performed.
"""
import argparse
import json
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    checks = []; errors = []; bad_responses = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1000})
        def api(path, body=None, method='POST'):
            r = context.request.fetch(args.base_url+path, method=method, data=body)
            assert r.ok, (path, r.status, r.text()[:500])
            return r.json()
        paper = api('/api/projects', {'name':'前端回归 · 可编辑合成论文 '+uuid.uuid4().hex[:5], 'domain':'工作台验证'})
        pid = paper['project']['id']; base = '/api/projects/'+pid
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('response', lambda r: bad_responses.append({'url':r.url.split('/api/')[-1], 'status':r.status}) if r.status>=400 and '/api/' in r.url else None)
        page.goto(args.base_url); page.wait_for_load_state('networkidle')
        page.locator('.ws-paper-switcher > summary').click()
        page.locator(f'.project-item[data-id="{pid}"]').click()
        expect(page.locator('#projectName')).to_have_value(paper['project']['name'])
        expect(page.locator(f'.project-item[data-id="{pid}"]')).to_have_attribute('role', 'button')
        expect(page.locator(f'.project-item[data-id="{pid}"]')).to_have_attribute('tabindex', '0')
        page.wait_for_timeout(800)
        assert page.locator('#projectName').evaluate('(e)=>getComputedStyle(e).fontSize') == '20px'
        assert page.locator('#compile').evaluate('(e)=>getComputedStyle(e).backgroundColor') == 'rgb(0, 0, 0)'
        expect(page.locator('.ws-assist-toggle')).to_have_attribute('aria-expanded', 'true')
        page.evaluate('document.fonts.ready')
        assert page.evaluate('document.fonts.check(\'14px "Open Sans"\')')
        checks.append('Codex 黑色主动作、本地字体、20px论文标题与桌面辅助展开状态一致')
        page.locator('.ws-build-options > summary').click()
        page.locator('#autoPreviewBuild').uncheck()
        page.locator('.ws-build-options > summary').click()
        assert page.locator('#editor').bounding_box()['y'] < 350
        checks.append('桌面首屏直接编辑；无大面板阻挡')
        expect(page.locator('.ws-chapters button')).to_have_count(5)
        page.locator('.ws-chapters button').first.click()
        expect(page.locator('#outlineText')).to_be_focused()
        assert page.locator('#outlineText').evaluate('(e)=>getComputedStyle(e).backgroundColor')=='rgb(255, 255, 255)'
        checks.append('原编号大纲五章进入侧栏，点击定位；大纲普通/焦点表面统一白色')
        for tab in ['outline','knowledge','visual','preview','write']:
            page.locator(f'.tabs [data-tab="{tab}"]').click()
            expect(page.locator('.workspace')).to_have_attribute('data-view',tab)
            expect(page.locator('#'+tab)).to_be_visible()
        checks.append('五个原有主视图保留真实事件和内容')
        for view in ['planning','history','settings']:
            page.locator(f'.tabs [data-view="{view}"]').click()
            expect(page.locator(f'[data-workspace-view="{view}"]')).to_be_visible()
        checks.append('启动、设置和历史独立可达')
        page.locator('[data-tab="knowledge"]').click()
        nav = page.locator('#knowledge > .ws-subnav')
        for label,selector in [('发现论文','#academicQuery'),('参考文献','#knowledge-task-1 .ws-resource-create > summary'),('语料库','#knowledge-task-2 .ws-resource-create > summary'),('术语库','#termLibrary'),('论证图','#logicGraphCard'),('领域与复用','#sharedResourceWorkbench')]:
            nav.get_by_role('button',name=label,exact=True).click()
            expect(page.locator(selector)).to_be_visible()
        checks.append('知识库六类任务均可访问，保留领域共享与复制')
        nav.get_by_role('button',name='术语库',exact=True).click()
        page.locator('#termLibrary').get_by_role('button',name='添加术语',exact=True).click()
        editor = page.get_by_role('dialog', name='添加术语')
        editor.get_by_label('术语',exact=True).fill('工作台术语')
        editor.get_by_label('定义',exact=True).fill('合成测试定义')
        editor.get_by_role('button',name='保存术语').click()
        expect(page.locator('#termLibrary .tl-list')).to_contain_text('工作台术语')
        page.locator('#termLibrary').get_by_role('button',name='批量沉淀与分类').click()
        expect(page.locator('#termReviewChoices')).to_contain_text('工作台术语')
        page.get_by_role('dialog',name='批量沉淀与分类').get_by_role('button',name='关闭',exact=True).click()
        checks.append('新增术语同步进入原列表和人工复核工作台')
        nav.get_by_role('button',name='语料库',exact=True).click()
        page.locator('#knowledge-task-2 .ws-resource-create > summary').click()
        page.locator('#corpusTitle').fill('工作台语料')
        page.locator('#corpusContent').fill('这是隔离测试的研究语料。')
        page.locator('#addCorpus').click()
        expect(page.locator('#corpusList')).to_contain_text('工作台语料')
        expect(page.locator('#corpusLineageChoice')).to_contain_text('工作台语料')
        checks.append('新增语料自动进入来源与校正版选择器')
        page.locator('[data-view="planning"]').click()
        page.locator('#workspace-planning [data-task="progress"]').click()
        page.get_by_role('button',name='进入人工修订阶段',exact=True).click()
        page.locator('[data-tab="write"]').click()
        page.get_by_role('button',name='进入人工修订模式',exact=True).click()
        expect(page.locator('#workflowModeState')).to_contain_text('已进入')
        page.locator('.ws-file-actions > summary').click()
        page.locator('#newFilePath').fill('sections/workspace.tex')
        page.locator('#addFile').click()
        expect(page.locator('#fileSelect')).to_have_value('sections/workspace.tex')
        page.locator('.ws-file-actions').evaluate('(e)=>e.open=false')
        page.locator('#editor').fill('% synthetic workspace file\nA saved paragraph.\n')
        with page.expect_response(lambda r:'/human-revision/checkpoint' in r.url) as checkpoint:
            page.locator('#saveFile').click()
        assert checkpoint.value.ok, checkpoint.value.text()
        expect(page.locator('#fileSelect')).to_have_value('sections/workspace.tex')
        expect(page.locator('#editor')).to_have_value('% synthetic workspace file\nA saved paragraph.\n')
        saved = api(base,method='GET')
        assert next(f for f in saved['files'] if f['path']=='sections/workspace.tex')['content'].startswith('% synthetic')
        checks.append('附属文件保存检查点后保留当前文件与正文')
        page.locator('[data-view="settings"]').click()
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(500)
        for value in ['numeric','author-year']:
            page.locator('#setting-citation_style').fill(value)
            with page.expect_response(lambda r: r.url.endswith(base+'/settings/citation_style') and r.request.method=='PUT') as setting_response:
                page.locator('[data-setting-save="citation_style"]').click()
            assert setting_response.value.ok, setting_response.value.text()
            page.wait_for_load_state('networkidle')
            page.wait_for_timeout(500)
            expect(page.locator('#setting-citation_style')).to_have_value(value)
            assert api(base+'/settings',method='GET')['effective']['citation_style']==value
        assert api(base+'/settings',method='GET')['effective']['citation_style']=='author-year'
        checks.append('添加资料后连续保存配置，无陈旧论文版本409')
        page.evaluate('paperpilot.openProviderSettings()')
        expect(page.locator('#providerName')).to_be_visible()
        checks.append('旧模型配置入口能打开独立模型设置页')
        page.locator('[data-tab="write"]').click()
        page.locator('#closeWorkspaceAssistant').click()
        expect(page.locator('#workspaceAssistant')).not_to_be_visible()
        page.locator('.ws-assist-toggle').click()
        expect(page.locator('#workspaceAssistant')).to_be_visible()
        page.get_by_role('button',name='指定修改',exact=True).click()
        expect(page.locator('#modificationInstruction')).to_be_visible()
        page.get_by_role('button',name='恢复',exact=True).click()
        expect(page.locator('#compareEditorDraft')).to_be_visible()
        checks.append('辅助区可收放；延迟注入的修改、恢复控件仍可用')
        page.locator('[data-tab="preview"]').click()
        expect(page.locator('.ws-pdf-empty')).to_be_visible()
        page.locator('.ws-pdf-empty button').click()
        expect(page.locator('.build-history')).to_be_visible()
        checks.append('无PDF时有可操作空态并通往构建历史')
        page.locator('[data-view="planning"]').click()
        page.locator('#workspace-planning [data-task="progress"]').click()
        with page.expect_response(lambda r:'/compile' in r.url and r.request.method=='POST') as response_info:
            page.locator('#runReleaseCheck').click()
        payload = response_info.value.request.post_data_json
        assert payload['purpose']=='submitted'
        submitted=response_info.value.json();jobid=submitted['build_job']['id']
        expect(page.locator('#runReleaseCheck')).to_be_enabled(timeout=180000)
        job=api(base+'/builds/'+jobid,method='GET')
        assert job['status']=='succeeded', (job['status'],job.get('diagnostics'))
        expect(page.locator('#lifecycleMessage')).to_contain_text('提交版编译通过')
        assert api(base+'/lifecycle',method='GET')['release_check']['current']
        checks.append('专用发布检查显式submitted，真实TeX异步轮询到成功并核对当前资格')
        page.locator('[data-tab="preview"]').click()
        expect(page.locator('#pdfFrame')).to_be_visible()
        expect(page.locator('#write')).not_to_be_visible()
        assert 'pdf-reading.html' in page.locator('#pdfFrame').get_attribute('src')
        pdfpage=page.frame_locator('#pdfFrame').locator('.pdfViewer .page').first
        expect(pdfpage).to_be_visible(timeout=30000)
        assert pdfpage.bounding_box()['width']>300
        page.screenshot(path=str(out/'pdf.png'),full_page=True)
        checks.append('成功构建进入固定版本PDF阅读器，纸页真实渲染且缩放有效')
        published = next((x for x in api('/api/projects',method='GET') if x['lifecycle_state']=='published'),None)
        if published:
            page.locator('.ws-paper-switcher > summary').click()
            page.locator(f'.project-item[data-id="{published["id"]}"]').click()
            page.wait_for_function('(id)=>paperpilot.data?.project.id===id',arg=published['id'])
            expect(page.locator('#workspaceAccessState')).to_contain_text('只读')
            page.locator('[data-tab="write"]').click()
            assert page.locator('#editor').evaluate('(e)=>e.readOnly')
            expect(page.locator('#saveFile')).to_be_disabled()
            checks.append('既有发布论文编辑器只读、保存禁用，保留阅读')
        page.locator('.ws-paper-switcher > summary').click()
        page.locator(f'.project-item[data-id="{pid}"]').click()
        page.wait_for_function('(id)=>paperpilot.data?.project.id===id',arg=pid)
        expect(page.locator('#projectName')).to_have_value(paper['project']['name'])
        page.locator('[data-tab="write"]').click()
        page.get_by_role('button',name='AI 辅助',exact=True).click()
        for width,height,name in [(1440,1000,'desktop'),(1024,900,'tablet'),(768,900,'small-tablet'),(390,844,'mobile')]:
            page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(200)
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            assert page.locator('.side-note').evaluate('(e)=>e.getBoundingClientRect().top>=document.querySelector("#projects").getBoundingClientRect().bottom')
            expect(page.locator('.ws-assist-toggle')).to_have_attribute('aria-expanded', str(width > 1190).lower())
            for view in ['write','outline','knowledge','visual','planning','history','settings','preview']:
                page.evaluate('(name)=>paperpilot.openWorkspace(name)',view)
                assert not page.locator('.ws-center').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'), (width,view)
            page.evaluate('paperpilot.openWorkspace("write")')
            page.screenshot(path=str(out/f'{name}.png'),full_page=True)
        checks.append('1440/1024/768/390下八个视图无横向溢出，辅助按钮展开状态随断点同步')
        page.locator('.ws-mobile-toggle').click()
        expect(page.locator('#workspaceSidebar')).to_be_visible()
        page.locator('[data-tab="knowledge"]').click()
        expect(page.locator('#workspaceSidebar')).not_to_be_visible()
        expect(page.locator('#knowledge')).to_be_visible()
        page.locator('.ws-mobile-toggle').click();page.locator('[data-tab="write"]').click()
        page.locator('.ws-assist-toggle').click()
        expect(page.locator('#workspaceAssistant')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#workspaceAssistant')).not_to_be_visible()
        expect(page.locator('.ws-assist-toggle')).to_be_focused()
        checks.append('窄屏论文库、写作辅助抽屉可操作；Escape收起并返回焦点')
        ids=page.evaluate('Array.from(document.querySelectorAll("[id]")).map(e=>e.id)')
        assert len(ids)==len(set(ids)), 'duplicate DOM IDs'
        assert not errors,errors
        assert not bad_responses,bad_responses
        checks.append('无重复DOM ID、未捕获脚本错误或失败API请求')
        result={'status':'passed','checks':checks,'page_errors':errors,'failed_api':bad_responses,'project_id':pid,'real_application':True,'mocked_functions':[],'real_model_calls':False}
        (out/'BROWSER-RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False))
        browser.close()


if __name__=='__main__':
    main()
