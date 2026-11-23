"""Conversation-led product journey. Model is an explicit fixture, services are real."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='http://127.0.0.1:8894');parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[];layouts=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1512,'height':1050})
        def api(path,body=None):
            response=context.request.fetch(args.base_url+path,method='GET' if body is None else 'POST',data=body)
            assert response.ok,(path,response.status,response.text()[:400]);return response.json()
        pid=api('/api/projects',{'name':'对话式论文工作空间 · 隔离测试','domain':'研究方法'})['project']['id']
        other=api('/api/projects',{'name':'另一篇独立论文','domain':'方法验证'})['project']['id']
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.base_url+'/#planning/startup');page.wait_for_load_state('networkidle')
        page.evaluate('(pid)=>window.paperpilot.loadProject(pid)',pid)
        expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
        expect(page.locator('#conversationWelcome')).to_be_visible()
        expect(page.locator('#paperArtifact')).not_to_be_visible()
        expect(page.locator('#wfGuide')).not_to_be_visible();expect(page.locator('#wfProgress')).not_to_be_visible()
        bounds=page.locator('#workspaceAssistant').bounding_box();composer=page.locator('#agentInput').bounding_box()
        assert bounds['width']>1100 and composer['width']>720,(bounds,composer)
        assert page.url.endswith('#chat')
        page.screenshot(path=str(output/'01-conversation-home.png'))
        checks.append('默认首屏为完整宽幅对话，阶段导航与进度条收起')
        page.locator('#openPaperTools').click();page.locator('[data-tool-id="manuscript"]').get_by_role('button',name='打开',exact=True).click()
        expect(page.locator('#paperArtifact')).to_be_visible();expect(page.locator('#editor')).to_be_visible()
        assert page.locator('#agentInput').bounding_box()['width']>300
        original=page.locator('#editor').input_value();marker='\n% conversation-first-save-check\n'
        page.locator('#editor').fill(original+marker)
        expect(page.locator('#editorSaveStatus')).to_contain_text('已自动保存',timeout=20000)
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#paperArtifact')).to_be_visible(timeout=15000)
        expect(page.locator('#editor')).to_have_value(original+marker)
        page.locator('#closePaperArtifact').click();expect(page.locator('#paperArtifact')).not_to_be_visible()
        expect(page.locator('#agentInput')).to_be_focused()
        checks.append('稿件按需并排打开，真实自动保存与刷新恢复，关闭回到完整对话')
        current_url=page.url
        page.locator('#conversationModel').click();expect(page.locator('#agentModelDialog')).to_be_visible()
        assert page.url==current_url
        page.get_by_role('button',name='返回对话',exact=True).click()
        expect(page.locator('#agentInput')).to_be_visible()
        checks.append('模型配置在当前页完成，保留对话与输入位置')
        def send(text):
            expect(page.locator('#agentSend')).to_be_enabled(timeout=20000)
            page.locator('#agentInput').fill(text);page.locator('#agentSend').click()
            page.wait_for_function("!document.querySelector('#agentSend').disabled || !document.querySelector('#agentStop').hidden")
            if page.locator('#agentInput').input_value()==text and page.locator('#agentSettings').is_visible():
                expect(page.locator('#agentConsent')).to_be_enabled(timeout=15000)
                page.locator('#agentConsent').check();page.get_by_role('button',name='确认并发送',exact=True).click()
        send('请帮我建立这个领域的知识地图')
        expect(page.locator('#agentThread .assistant').last).to_contain_text('待验证候选',timeout=60000)
        expect(page.locator('#conversationOutputs [data-output="research"]')).to_be_visible()
        page.locator('[data-output="research"]').click()
        expect(page.locator('#researchGraphStatus')).to_contain_text('3 个知识节点',timeout=15000)
        expect(page.locator('#agentInput')).to_be_visible()
        page.screenshot(path=str(output/'02-dialogue-and-research.png'))
        page.locator('#closePaperArtifact').click()
        checks.append('论文总对话直接使用真实学徒工具，生成研究图并按需打开')
        expect(page.locator('#agentStop')).not_to_be_visible(timeout=60000)
        for index in range(5):
            send('请把刚才讨论的内容形成方案，供我审阅确认')
            card=page.locator('.planning-conversation-card').last
            expect(card).to_be_visible(timeout=60000)
            expect(page.locator('#agentStop')).not_to_be_visible(timeout=60000)
            expect(page.locator('#agentSend')).to_be_enabled(timeout=20000)
            card.get_by_role('button',name='审阅方案',exact=True).click()
            expect(card.get_by_role('button',name='确认这份方案',exact=True)).to_be_enabled()
            if index==0:page.screenshot(path=str(output/'03-plan-review.png'))
            card.get_by_role('button',name='确认这份方案',exact=True).click()
            expect(card).to_contain_text('已确认的论文方案',timeout=15000)
            expect(card.get_by_role('button',name='确认这份方案',exact=True)).to_have_count(0)
            plan=api(f'/api/projects/{pid}/workflow/startup')['plan']
            assert plan['stages'][index]['status']=='confirmed',plan
            expect(page.locator('#paperArtifact')).not_to_be_visible()
        assert plan['status']=='complete'
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('.planning-completed').last).to_contain_text('已确认的论文方案',timeout=15000)
        page.screenshot(path=str(output/'03-inline-plan-acceptance.png'))
        checks.append('五份方案全部在对话内审阅确认，无需打开规划页面')
        expect(page.locator('#agentSend')).to_be_enabled(timeout=20000)
        page.evaluate("paperpilot.openModuleAgent('outline')")
        expect(page.locator('#agentActiveTitle')).to_have_text('章节大纲')
        page.locator('#agentInput').fill('专题对话中尚未发送的结构想法')
        page.locator('#agentRoutes [data-module="paper"]').click()
        expect(page.locator('#agentThread')).to_contain_text('已确认的论文方案')
        page.evaluate("paperpilot.openModuleAgent('outline')")
        expect(page.locator('#agentInput')).to_have_value('专题对话中尚未发送的结构想法')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#agentInput')).to_have_value('专题对话中尚未发送的结构想法',timeout=15000)
        checks.append('专题对话按需使用，消息与未发送草稿独立恢复')
        page.evaluate('(pid)=>window.paperpilot.loadProject(pid)',other)
        expect(page.locator('#agentInput')).to_have_value('')
        page.evaluate('(pid)=>window.paperpilot.loadProject(pid)',pid)
        expect(page.locator('#agentInput')).to_have_value('专题对话中尚未发送的结构想法')
        checks.append('论文切换保留独立对话和草稿，候选跨论文隔离')
        for width in [1512,1280,1024,768,390,320]:
            page.set_viewport_size({'width':width,'height':1050 if width>=768 else 844})
            expect(page.locator('#agentInput')).to_be_visible()
            assert page.locator('#agentInput').bounding_box()['width']>250
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
            page.screenshot(path=str(output/f'chat-{width}.png'))
            page.locator('#openPaperTools').click();page.locator('[data-tool-id="outline"]').get_by_role('button',name='打开',exact=True).click()
            expect(page.locator('#paperArtifact')).to_be_visible()
            assert not page.locator('#paperArtifact').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),width
            if width<=1100:
                expect(page.locator('#paperArtifact')).to_have_css('position','absolute')
            page.locator('#closePaperArtifact').click()
            layouts.append({'width':width,'chat':True,'artifact':True})
        checks.append('六个断点下对话与成果面板均可操作，输入宽度和横向溢出检查通过')
        page.set_viewport_size({'width':390,'height':844})
        page.locator('.ws-mobile-toggle').click();expect(page.locator('.cf-conversations')).to_be_visible()
        page.locator('#agentRoutes [data-module="paper"]').click()
        expect(page.locator('.sidebar')).not_to_be_visible()
        checks.append('手机侧栏切换对话后回到主画布')
        assert not errors,errors
        (output/'BROWSER-RESULT.json').write_text(json.dumps({'status':'passed','checks':checks,'layouts':layouts,'page_errors':errors,'model':'explicit fixture','real_agent':True,'real_scholar_cli':True,'real_workflow':True,'real_cloud_model':False},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    print(json.dumps({'passed':len(checks),'layouts':len(layouts)*2,'page_errors':errors},ensure_ascii=False))


if __name__=='__main__':main()
