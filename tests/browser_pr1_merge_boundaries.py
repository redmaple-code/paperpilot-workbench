"""Exercise integration boundaries with synthetic papers in owned fixture storage."""
import argparse
import json
import re
import time
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        seed = context.request.post(args.base_url + '/api/projects', data={'name': '边界测试初始论文'})
        assert seed.ok
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.base_url)
        page.locator('.ws-paper-switcher > summary').click()
        page.locator('#newProject').click()
        name = 'PR1 合并边界 ' + uuid.uuid4().hex[:6]
        page.locator('#newName').fill(name)
        page.locator('#createProject').click()
        expect(page.locator('#workspaceCurrentPaper')).to_have_text(name)
        expect(page).to_have_url(args.base_url + '/#planning/startup')
        expect(page.locator('#startupCurrentTitle')).to_contain_text('研究问题')
        pid = page.evaluate('paperpilot.current.project.id')
        base = args.base_url + '/api/projects/' + pid
        checks.append('前端新建论文直接进入第一阶段规划')

        # The dialogue-to-draft path is exercised by browser_scholar_workspace.py.
        initial = context.request.get(base + '/workflow/startup').json()
        response = context.request.post(base + '/workflow/startup/commands', data={
            'command': 'save', 'request_id': uuid.uuid4().hex, 'stage_id': 'STAGE-01',
            'expected_workflow_version': initial['workflow_version'],
            'fields': {field: '合成验证内容 ' + field for field in ['research_question', 'contribution', 'scope']}})
        assert response.ok, response.text()
        page.evaluate('paperpilot.refreshWorkflow()')
        page.locator('#reviewStartup').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('待审阅')
        expect(page.locator('#wfGuide [data-stage="STAGE-01"]')).to_contain_text('待审阅')
        page.locator('#confirmStartup').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('写作大纲')
        expect(page.locator('#wfProgress')).to_contain_text('规划确认 1/5')
        expect(page.locator('#wfGuide [data-stage="STAGE-01"]')).to_contain_text('已确认')
        checks.append('审阅和确认同步更新阶段表单、侧栏和顶部进度')
        page.screenshot(path=str(output / 'planning-confirmed.png'))

        page.evaluate('paperpilot.openWorkspace("outline/text")')
        expect(page.locator('#outlineText')).to_be_visible()
        for _ in range(2):
            page.locator('#outline [data-task="nodes"]').click()
            expect(page.locator('#outlineCanvas .x6-host')).to_be_visible()
            page.wait_for_function('window.__outlineGraph?.getGraphArea().width > 200')
            page.once('dialog', lambda dialog: dialog.accept('合并边界章节'))
            with page.expect_response(lambda response: response.url.endswith('/outline/nodes') and response.request.method == 'POST') as mutation:
                page.locator('#ocAdd').click()
            assert mutation.value.ok, mutation.value.text()
            expect(page.locator('#outlineCanvas')).to_contain_text('合并边界章节')
            page.locator('#outline [data-task="text"]').click()
            expect(page.locator('#outlineText')).to_be_visible()
        graph = context.request.get(base + '/outline/structured').json()
        assert len([node for node in graph['nodes'] if node['title'] == '合并边界章节']) == 2
        checks.append('文本和节点任务反复切换后画布有实际尺寸，新增节点持久化')

        page.evaluate('paperpilot.openWorkspace("write")')
        editor = page.locator('#editor')
        original = editor.input_value()
        first = original + '\n% automatic-save-boundary\n'
        editor.fill(first)
        expect(page.locator('#editorSaveStatus')).to_have_text('已自动保存', timeout=20000)
        saved = context.request.get(base).json()
        assert any(file['content'] == first for file in saved['files'])
        second = first + '% keyboard-save-boundary\n'
        editor.fill(second)
        editor.press('Control+s')
        expect(page.locator('#editorSaveStatus')).to_have_text('已自动保存', timeout=20000)
        saved = context.request.get(base).json()
        assert any(file['content'] == second for file in saved['files'])
        checks.append('未启动AI写作也能自由编辑，自动保存和Ctrl+S均写入真实版本')

        deadline = time.monotonic() + 180
        succeeded = None
        while time.monotonic() < deadline:
            jobs = context.request.get(base + '/builds?limit=30').json()
            succeeded = next((job for job in jobs if job['status'] == 'succeeded' and job.get('current')), None)
            if succeeded:
                break
            page.wait_for_timeout(900)
        assert succeeded, jobs
        expect(page.locator('#pdfFrame')).to_have_attribute('src', re.compile('pdf-reading.html'), timeout=30000)
        expect(page.frame_locator('#pdfFrame').locator('.pdfViewer .page').first).to_be_visible(timeout=30000)
        checks.append('编辑触发真实TeX构建并在写作页渲染实时PDF')
        page.screenshot(path=str(output / 'writing-live-pdf.png'))

        page.evaluate('paperpilot.openWorkspace("history/builds")')
        page.locator('.ws-history-build-options > summary').click()
        expect(page.locator('#compile')).to_be_visible()
        with page.expect_response(lambda response: response.url.endswith('/compile') and response.request.method == 'POST') as response:
            page.locator('#compile').click()
        assert response.value.ok, response.value.text()
        checks.append('构建历史中手动编译入口可达且调用真实API')
        assert not errors, errors
        result = {'status': 'passed', 'checks': checks, 'page_errors': errors, 'real_model_calls': False,
                  'real_tex': True, 'isolated_storage': True}
        (output / 'MERGE-BOUNDARY-RESULT.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
