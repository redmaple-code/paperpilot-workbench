"""Same-page paper/subagent journeys with a fixture model and real runtime/skill."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8894')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1512,'height':1100})
        def api(path,body=None):
            response=context.request.fetch(args.base_url+path,method='GET' if body is None else 'POST',data=body)
            assert response.ok,(path,response.status,response.text()[:300])
            return response.json()
        pid=api('/api/projects',{'name':'全局 Agent · 隔离测试论文','domain':'合成研究方法','request_id':'sidebar-'+__import__('uuid').uuid4().hex})['project']['id']
        other=api('/api/projects',{'name':'另一篇隔离论文','domain':'合成材料','request_id':'sidebar-other-'+__import__('uuid').uuid4().hex})['project']['id']
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.base_url+'/#planning/startup')
        page.wait_for_load_state('networkidle')
        page.evaluate('(pid)=>window.paperpilot.loadProject(pid)',pid)
        expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
        expect(page.locator('#workspaceAssistant #agentChat')).to_be_visible()
        expect(page.locator('#agentActiveTitle')).to_have_text('论文总对话')
        expect(page.locator('#researchWorkspace #agentChat')).to_have_count(0)
        expect(page.locator('#startupPlanner #agentTrace,#startupPlanner #agentPreview')).to_have_count(0)
        expect(page.locator('#viewStartupHistory,#copyStartup')).to_have_count(0)
        page.screenshot(path=str(output/'global-agent-main.png'))
        checks.append('论文总对话位于全局侧栏，模块内无运行配置和旧规划入口')
        original_url=page.url
        root=page.locator('#agentConversation').input_value()
        page.locator('#agentInput').fill('总对话待发送草稿')
        page.locator('#openResearchAgent').click()
        expect(page.locator('#agentActiveTitle')).to_have_text('研究问题与创新')
        expect(page.locator('#agentSend')).to_be_enabled()
        child=page.locator('#agentConversation').input_value()
        assert child!=root and page.url==original_url
        page.locator('#agentInput').fill('研究子对话草稿')
        page.locator('[data-module="paper"]').click()
        expect(page.locator('#agentInput')).to_have_value('总对话待发送草稿')
        page.locator('#openResearchAgent').click()
        expect(page.locator('#agentInput')).to_have_value('研究子对话草稿')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#agentConversation')).to_have_value(child,timeout=15000)
        expect(page.locator('#agentInput')).to_have_value('研究子对话草稿')
        checks.append('父子对话在同页切换，草稿和选中身份可刷新恢复')
        page.locator('#agentSettingsToggle').click()
        page.locator('#agentConfigureModel').click()
        expect(page.locator('#agentModelDialog')).to_be_visible()
        assert page.url==original_url
        expect(page.locator('#agentModelDialog #providerSettings')).to_be_visible()
        page.get_by_role('button',name='返回对话',exact=True).click()
        expect(page.locator('#agentModelDialog')).not_to_be_visible()
        assert page.url==original_url
        expect(page.locator('#agentInput')).to_have_value('研究子对话草稿')
        checks.append('模型配置在页内弹窗完成，关闭后恢复同一对话')
        page.locator('#agentInput').fill('请梳理研究对象、竞争机理与判别方案，并写入研究图')
        page.locator('#agentSend').click()
        expect(page.locator('#agentSettings')).to_be_visible()
        expect(page.locator('#agentConsent')).to_be_enabled()
        page.locator('#agentConsent').check()
        page.locator('#agentSend').click()
        expect(page.locator('#agentThread .assistant').last).to_contain_text('待验证候选',timeout=60000)
        expect(page.locator('#agentSettings')).not_to_be_visible()
        expect(page.locator('#researchGraphStatus')).to_contain_text('3 个知识节点',timeout=15000)
        page.locator('#researchNodeList button').first.click()
        expect(page.locator('#researchNodeDetail')).to_be_visible()
        page.screenshot(path=str(output/'global-agent-research.png'))
        page.locator('[data-research-view="routes"]').click()
        expect(page.locator('#researchNodeList button')).to_have_count(4)
        checks.append('研究子 Agent 真实调用原生学徒，侧栏对话驱动模块知识图和路线图')
        page.locator('[data-module="paper"]').click()
        expect(page.locator('#agentThread .assistant')).to_have_count(0)
        page.locator('#agentInput').fill('汇总研究进度并安排下一步')
        preview=api(f'/api/projects/{pid}/agent/preview',{'conversation_id':root})
        assert any(c['id']==child and c['revision']==2 for c in preview['context_manifest']['related_conversations'])
        page.locator('#agentSend').click();expect(page.locator('#agentConsent')).to_be_enabled()
        page.locator('#agentConsent').check();page.locator('#agentSend').click()
        expect(page.locator('#agentThread .assistant').last).to_contain_text('隔离模型回复',timeout=15000)
        checks.append('总 Agent 获取模块进展摘要，父子消息独立保存')
        page.locator('#researchAdoptDraft').click()
        page.get_by_role('button',name='写入规划草稿',exact=True).click()
        expect(page.locator('#researchSummaryFields')).to_contain_text('待检验的研究问题')
        page.locator('#reviewStartup').click();page.locator('#confirmStartup').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('写作大纲')
        expect(page.locator('#agentActiveTitle')).to_have_text('论文总对话')
        page.locator('#openCurrentModuleAgent').click()
        expect(page.locator('#agentActiveTitle')).to_have_text('章节大纲')
        checks.append('方案可采纳审阅，下一模块有专属子对话')
        for view in ['write','knowledge','visual','outline','preview','planning/startup']:
            page.evaluate('(view)=>window.paperpilot.openWorkspace(view)',view)
            expect(page.locator('#workspaceAssistant #agentChat')).to_be_visible()
            expect(page.locator('#agentActiveTitle')).to_have_text('章节大纲')
        checks.append('切换工作区保留当前子对话，全局侧栏始终可用')
        page.locator('#agentInput').fill('大纲草稿只属于本论文')
        page.evaluate('(pid)=>window.paperpilot.loadProject(pid)',other)
        expect(page.locator('#agentActiveTitle')).to_have_text('论文总对话')
        expect(page.locator('#agentInput')).to_have_value('')
        page.evaluate('(pid)=>window.paperpilot.loadProject(pid)',pid)
        expect(page.locator('#agentActiveTitle')).to_have_text('章节大纲')
        expect(page.locator('#agentInput')).to_have_value('大纲草稿只属于本论文')
        checks.append('跨论文切换隔离会话、草稿与研究上下文')
        for width in [1512,1191,1024,768,390]:
            page.set_viewport_size({'width':width,'height':1100 if width>390 else 844})
            if width<1191:
                page.locator('.ws-assist-toggle').click()
            expect(page.locator('#agentInput')).to_be_visible()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'),width
            assert not page.locator('#workspaceAssistant').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),width
            page.screenshot(path=str(output/f'global-agent-{width}.png'))
            if width<1191:
                page.locator('#closeWorkspaceAssistant').click()
                expect(page.locator('#workspaceAssistant')).not_to_be_visible()
        checks.append('五个断点侧栏、抽屉与输入框可用且无横向溢出')
        assert not errors,errors
        (output/'BROWSER-RESULT.json').write_text(json.dumps({'status':'passed','checks':checks,'page_errors':errors,'real_agent_runtime':True,'real_scholar_cli':True,'real_cloud_model':False},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    print(json.dumps({'status':'passed','checks':len(checks)},ensure_ascii=False))


if __name__=='__main__':
    main()
