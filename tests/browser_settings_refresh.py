"""Keep unsaved settings and selection through a delayed background refresh."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        response = context.request.post(args.base_url + '/api/projects', data={
            'name': '配置刷新隔离测试', 'domain': '界面验证'
        })
        assert response.ok
        project_id = response.json()['project']['id']
        settings_url = args.base_url + f'/api/projects/{project_id}/settings'
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.base_url)
        page.wait_for_load_state('networkidle')
        page.locator('.ws-paper-switcher > summary').click()
        page.locator(f'.project-item[data-id="{project_id}"]').click()
        page.locator('[data-view="settings"]').click()
        field = page.locator('#setting-citation_style')
        expect(field).to_be_visible()
        page.wait_for_load_state('networkidle')

        pending = []

        def hold_settings(route):
            pending.append((route, route.fetch()))

        page.route(settings_url, hold_settings)
        page.evaluate("document.dispatchEvent(new CustomEvent('paperpilot:project-changed'))")
        for _ in range(50):
            if pending:
                break
            page.wait_for_timeout(100)
        assert pending, 'background settings refresh did not start'
        field.fill('unsaved local citation style')
        field.evaluate('(input)=>input.setSelectionRange(2, 8)')
        for route, response in pending:
            route.fulfill(response=response)
        page.unroute(settings_url, hold_settings)
        page.wait_for_load_state('networkidle')
        expect(field).to_have_value('unsaved local citation style')
        expect(field).to_be_focused()
        assert field.evaluate('(input)=>[input.selectionStart,input.selectionEnd]') == [2, 8]

        with page.expect_response(lambda result: result.url.endswith('/settings/citation_style') and result.request.method == 'PUT') as saved:
            page.locator('[data-setting-save="citation_style"]').click()
        assert saved.value.ok, saved.value.text()
        page.wait_for_load_state('networkidle')
        view = context.request.get(settings_url).json()
        assert view['effective']['citation_style'] == 'unsaved local citation style'

        field.fill('another unsaved local value')
        with page.expect_response(lambda result: result.url.endswith('/settings/citation_style/restore')) as restored:
            page.locator('[data-setting-restore="citation_style"]').click()
        assert restored.value.ok, restored.value.text()
        page.wait_for_load_state('networkidle')
        effective = context.request.get(settings_url).json()['effective']['citation_style']
        expect(field).to_have_value(str(effective or ''))
        assert effective != 'another unsaved local value'
        assert not errors, errors
        result = {
            'status': 'passed',
            'checks': ['延迟刷新保留未保存输入、焦点与选区', '保存写入实际配置', '显式恢复基线覆盖当前字段草稿'],
            'page_errors': errors,
            'real_model_calls': False
        }
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        (output / 'SETTINGS-REFRESH-RESULT.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
