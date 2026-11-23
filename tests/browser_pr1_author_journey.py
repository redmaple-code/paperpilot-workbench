"""Real-browser checks against the isolated UI fixture server; no real-model claim."""
import argparse
import json
from pathlib import Path
import uuid
from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args();url=args.base_url.rstrip('/');out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1100})
        pid=context.request.post(url+'/api/projects',data={'name':'03–06 Browser '+uuid.uuid4().hex[:6],'domain':'软件验证','style':'严谨'}).json()['project']['id']
        other=context.request.post(url+'/api/projects',data={'name':'隔离目标 '+uuid.uuid4().hex[:6]}).json()['project']['id']
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(url);page.locator('.ws-paper-switcher > summary').click();page.locator(f'.project-item[data-id="{pid}"]').click()
        page.wait_for_function('(id)=>window.paperpilot.current?.project.id===id',arg=pid)
        page.wait_for_function('!!document.querySelector("#agentConversation")?.value')
        expect(page.locator('#workflowStatus')).not_to_have_text('正在读取工作流状态...')
        cid=page.locator('#agentConversation').input_value()
        base=url+f'/api/projects/{pid}/workflow/startup'
        initial=context.request.get(base).json()
        page.locator('#wfGuide [data-stage="STAGE-04"]').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('术语与证据')
        assert context.request.get(base).json()['workflow_version']==initial['workflow_version']
        page.locator('[data-startup-field="terms"]').fill('先定义术语，再整理来源')
        page.locator('#saveStartup').click()
        page.wait_for_function('!document.querySelector("#saveStartup").disabled')
        saved=context.request.get(base).json()
        assert saved['plan']['current_index']==0 and saved['confirmed_context']==[]
        assert saved['plan']['stages'][3]['fields']['terms']=='先定义术语，再整理来源'
        checks.append('future-stage-save-without-confirmation-position-change')

        page.locator('#wfGuide [data-stage="STAGE-02"]').click()
        page.locator('[data-startup-field="outline"]').fill('保留的未保存输入')
        page.locator('#wfGuide [data-stage="STAGE-04"]').click()
        page.locator('.stage-draft-dialog button[value="keep"]').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('术语与证据')
        page.locator('#wfGuide [data-stage="STAGE-02"]').click()
        expect(page.locator('[data-startup-field="outline"]')).to_have_value('保留的未保存输入')
        page.locator('#saveStartup').click();page.wait_for_function('!document.querySelector("#saveStartup").disabled')
        checks.append('unsaved-input-keep-and-restore')

        page.locator('#wfGuide [data-stage="STAGE-04"]').click()
        page.locator('#researchDesign > summary').click()
        page.locator('#rdPreview').click()
        expect(page.locator('#researchDesignPreview')).to_be_visible()
        expect(page.locator('#researchDesignOutbound')).to_contain_text('STAGE-04')
        page.locator('#rdConsent').check()
        page.locator('#rdGenerate').click()
        expect(page.locator('#researchDesignCards .rd-card')).to_have_count(1,timeout=30000)
        page.locator('#researchDesignCards').get_by_role('button',name='预览草稿应用').click()
        apply_button=page.locator('#researchDesignCards').get_by_role('button',name='明确应用')
        expect(apply_button).to_be_enabled()
        assert not context.request.get(base).json()['plan']['stages'][3]['fields']['terms'].startswith('界面测试草稿')
        apply_button.click()
        page.wait_for_function('document.querySelector("[data-startup-field=terms]").value.includes("界面测试草稿")')
        assert context.request.get(base).json()['confirmed_context']==[]
        checks.append('stage04-schema-preview-then-explicit-draft-apply')

        workflow=context.request.get(base).json();project=context.request.get(url+f'/api/projects/{pid}').json()['project']
        page.evaluate('document.querySelector("#editor").value="UNSAVED MANUSCRIPT";document.querySelector("#domain").value="UNSAVED SETTINGS"')
        action=context.request.post(url+f'/api/projects/{pid}/agent/actions',data={'request_id':uuid.uuid4().hex,'conversation_id':cid,'operation':'term','base_version':project['project_revision'],
            'payload':{'term':'界面术语','definition':'通过显式应用写入的合成定义'}}).json()
        page.wait_for_selector(f'[data-business-card="{action["id"]}"]',timeout=12000)
        # Desktop assistant is visible; actions remain bound to their original card.
        card=page.locator(f'[data-business-card="{action["id"]}"]')
        card.get_by_role('button',name='预览差异').click()
        expect(card.get_by_role('button',name='明确应用')).to_be_enabled()
        card.get_by_role('button',name='明确应用').click()
        expect(card.locator('.business-status')).to_have_text('已应用')
        expect(page.locator('#termList')).to_contain_text('界面术语')
        expect(page.locator('#editor')).to_have_value('UNSAVED MANUSCRIPT')
        expect(page.locator('#domain')).to_have_value('UNSAVED SETTINGS')
        checks.append('resource-refresh-preserves-unsaved-editor-and-settings')
        checks.append('business-card-explicit-apply-in-conversation')

        search=context.request.post(url+f'/api/projects/{pid}/agent/business-search',data={'query':'reproducible research','conversation_id':cid,'consent_to_search':True}).json()
        selected=search['cards'][1]
        result_card=page.locator(f'[data-business-card="{selected["id"]}"]')
        expect(result_card).to_be_visible(timeout=12000)
        assert result_card.get_by_role('button',name='预览全文导入').count()==0
        with page.expect_response(lambda response:response.url.endswith('/agent/actions') and response.request.method=='POST') as handoff:
            result_card.get_by_role('button',name='加入参考文献草稿').click()
        # Use the server's stable action ID, rather than a result ordinal.
        reference_action=handoff.value.json()
        reference=page.locator(f'[data-business-card="{reference_action["id"]}"]')
        reference.get_by_role('button',name='预览差异').click()
        expect(reference.locator('pre')).to_contain_text('合成检索结果二')
        reference.get_by_role('button',name='明确应用').click()
        expect(reference.locator('.business-status')).to_have_text('已应用')
        checks.append('conversation-search-selected-reference-handoff-no-oa')

        stage_button=page.locator('#wfGuide [data-stage="STAGE-05"]')
        stage_button.focus();stage_button.press('Enter')
        expect(page.locator('#startupCurrentTitle')).to_contain_text('工作量与资源')
        checks.append('stage-navigation-keyboard-enter')

        page.screenshot(path=str(out/'desktop.png'),full_page=True)
        for width in [768,390]:
            page.set_viewport_size({'width':width,'height':1000})
            page.wait_for_timeout(250)
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
            assert page.locator('#startupFieldInputs').is_visible()
            page.screenshot(path=str(out/f'width-{width}.png'),full_page=True)
            checks.append(f'responsive-{width}')
        page.set_viewport_size({'width':1440,'height':1100})
        page.once('dialog',lambda dialog:dialog.accept())
        page.locator('.ws-paper-switcher > summary').click();page.locator(f'.project-item[data-id="{other}"]').click()
        page.wait_for_timeout(300)
        if page.locator('.stage-draft-dialog[open]').count():
            page.locator('.stage-draft-dialog button[value="keep"]').click()
        if page.locator('dialog[open]').count():
            page.locator('dialog[open]').get_by_role('button',name='放弃本页更改并切换').click()
        page.wait_for_function('(id)=>window.paperpilot.current?.project.id===id',arg=other)
        assert not context.request.get(url+f'/api/projects/{other}/workflow/startup').json()['confirmed_context']
        assert context.request.post(url+f'/api/projects/{other}/agent/actions/{action["id"]}/preview',data={}).status==404
        checks.append('cross-project-action-rejected')
        browser.close()
    report={'status':'passed','checks':checks,'pageerrors':errors,'database_isolated':True,'model_mode':'deterministic_ui_fixture','external_model_calls':0,'product_acceptance':False}
    (out/'BROWSER-REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert not errors,errors
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
