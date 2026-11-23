"""Actual browser/application flow using ui_agent_server's explicitly simulated model."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='http://127.0.0.1:8877');parser.add_argument('--output',required=True);args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);checks=[];errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1440,'height':1000})
        def api(path,body=None,method='POST'):
            r=context.request.fetch(args.base_url+path,method=method,data=body)
            assert r.ok,(path,r.status,r.text()[:500])
            return r.json()
        a=api('/api/projects',{'name':'会话A · 合成论文','domain':'实验方法'})['project']['id']
        b=api('/api/projects',{'name':'会话B · 隔离论文','domain':'计算方法'})['project']['id']
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(args.base_url);page.wait_for_load_state('networkidle')
            # Inspect the actual rendered controls before acting.
            page.screenshot(path=str(out/'01-initial.png'),full_page=True)
            (out/'rendered-controls.json').write_text(json.dumps(page.locator('#agentChat button,#agentChat select').evaluate_all('(nodes)=>nodes.map(n=>({id:n.id,text:n.textContent}))'),ensure_ascii=False,indent=2),encoding='utf8')
            page.evaluate('(pid)=>{window.paperpilot.loadProject(pid)}',a)
            expect(page.locator('#projectName')).to_have_value('会话A · 合成论文')
            expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
            expect(page.locator('#agentPreviewBody')).to_contain_text('接收方',timeout=15000)
            cid=page.locator('#agentConversation').input_value()
            page.locator('#agentSettingsToggle').click()
            page.locator('#agentPreview').evaluate('(element)=>element.open=true')
            page.locator('#agentConsent').check()
            page.locator('#agentInput').fill('解释实验边界')
            page.locator('#agentSend').click()
            expect(page.locator('#agentThread .assistant').last).to_contain_text('第一轮的可见结论',timeout=15000)
            expect(page.locator('#agentSend')).to_be_enabled();checks.append('first_turn_and_send_reset')
            page.locator('#agentInput').fill('第二轮沿用刚才结论')
            page.locator('#agentSend').click()
            expect(page.locator('#agentThread .assistant').last).to_contain_text('第二轮沿用第一轮边界',timeout=15000)
            checks.append('second_turn_without_repeated_consent')
            def lose_response(route):
                route.fetch()  # server commits and dispatches, browser loses the response
                route.abort('failed')
            page.route('**/agent/runs',lose_response)
            expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
            page.locator('#agentInput').fill('发送结果丢失后的核对')
            page.locator('#agentSend').click()
            expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
            page.unroute('**/agent/runs',lose_response)
            page.reload();page.wait_for_load_state('networkidle')
            expect(page.locator('#agentInput')).to_have_value('',timeout=15000)
            expect(page.locator('#agentThread .user')).to_have_count(3,timeout=15000)
            checks.append('lost_send_response_recovers_without_resend')
            page.reload();page.wait_for_load_state('networkidle')
            expect(page.locator('#agentConversation')).to_have_value(cid,timeout=15000)
            expect(page.locator('#agentThread .assistant')).to_have_count(3,timeout=15000);checks.append('refresh_restores_history')
            for instruction,expected in [('起草第一稿','第一份草稿'),('起草第二稿','第二份草稿')]:
                expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
                page.locator('#agentInput').fill(instruction);page.locator('#agentSend').click()
                expect(page.locator('#agentThread .agent-draft-text').last).to_contain_text(expected,timeout=15000)
            original=page.locator('#editor').input_value()
            anchor=page.locator('#editor').evaluate('(n)=>n.selectionStart')
            page.locator('#agentThread .agent-card').filter(has=page.locator('pre',has_text='第一份草稿')).get_by_role('button',name='插入原位置').click()
            expect(page.locator('#editor')).to_have_value(original[:anchor]+'第一份草稿说明实验边界，强调材料约束和可验证性。'+original[anchor:],timeout=10000)
            checks.append('old_card_inserts_its_own_draft')
            inserted=page.locator('#editor').input_value()
            page.locator('#agentThread .agent-card').filter(has=page.locator('pre',has_text='第二份草稿')).get_by_role('button',name='替换原选区').click()
            expect(page.locator('.agent-compare-dialog')).to_be_visible()
            page.locator('.agent-compare-dialog').get_by_role('button',name='取消',exact=True).click()
            assert page.locator('#editor').input_value()==inserted;checks.append('changed_target_compares_without_overwrite')
            page.screenshot(path=str(out/'02-history-and-drafts.png'),full_page=True)
            expect(page.locator('#editorSaveStatus')).to_contain_text('已自动保存',timeout=15000)
            page.locator('#agentInput').fill('慢任务：解释研究限制')
            page.locator('#agentSend').click();expect(page.locator('#agentStop')).to_be_visible(timeout=10000)
            page.evaluate('(pid)=>{window.paperpilot.loadProject(pid)}',b)
            page.wait_for_timeout(400)
            if page.locator('#projectSwitchDialog').is_visible():
                options=page.locator('#projectSwitchActions button');(out/'switch-options.json').write_text(json.dumps(options.all_text_contents(),ensure_ascii=False),encoding='utf8')
                options.filter(has_text='放弃本页更改并切换').click()
            expect(page.locator('#projectName')).to_have_value('会话B · 隔离论文',timeout=15000)
            expect(page.locator('#agentSend')).to_be_enabled(timeout=15000)
            page.wait_for_timeout(2000)
            assert '第一轮的可见结论' not in page.locator('#agentThread').inner_text();checks.append('late_A_result_does_not_enter_B')
            page.locator('#agentSettingsToggle').click();page.locator('#agentPreview').evaluate('(element)=>element.open=true')
            page.locator('#agentConsent').check();page.locator('#agentInput').fill('慢任务：供取消验证')
            page.locator('#agentSend').click();expect(page.locator('#agentStop')).to_be_visible(timeout=10000);page.locator('#agentStop').click()
            expect(page.locator('#agentRunStatus')).to_contain_text('已停止',timeout=15000)
            expect(page.locator('#agentSend')).to_be_enabled();checks.append('cancel_terminal_controls_recover')
            page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
            expect(page.locator('#agentInput')).to_be_visible()
            page.screenshot(path=str(out/'03-mobile.png'),full_page=True)
            bounds=page.locator('#agentInput').bounding_box()
            assert bounds and bounds['width']>150, bounds
            checks.append('mobile_input_remains_usable')
            assert not errors,errors
        finally:
            page.screenshot(path=str(out/'last-state.png'),full_page=True)
            (out/'browser-results.json').write_text(json.dumps({'mode':'simulated_model_real_browser','checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
            browser.close()
    print(json.dumps({'checks':checks,'page_errors':errors},ensure_ascii=False))


if __name__=='__main__':main()
