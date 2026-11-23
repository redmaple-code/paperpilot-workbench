"""Verify both terminology entry points against the isolated PaperPilot server."""
import argparse
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 960})

        def api(path, data=None, method='POST'):
            result = context.request.fetch(args.base_url + path, method=method, data=data)
            assert result.ok, (path, result.status, result.text())
            return result.json()

        domain = api('/api/domains', {'name': '计算流体力学'})
        domain_path = f"/api/domains/{domain['id']}"
        api(domain_path + '/resources', {'resource_type': 'term', 'title': '有限体积法', 'content': '领域定义一。'})
        project = api('/api/projects', {'name': '术语回归样稿', 'domain_id': domain['id']})['project']
        pid = project['id']
        project_path = f'/api/projects/{pid}'
        other = api('/api/projects', {'name': '未绑定领域的测试论文'})['project']
        api(project_path + '/files', {
            'path': 'main.tex',
            'base_version': 1,
            'content': 'Adaptive Flux Filter (AFF) and Latent Flux Annex (LFA) were evaluated.',
        }, 'PUT')
        reference = api('/api/references', {
            'title': 'A Study of Adaptive Flux Filters',
            'abstract': 'Adaptive Flux Filter (AFF) is used to control flux errors.',
        })
        api(project_path + '/references/' + reference['id'],
            {'request_id': 'term-browser-link', 'reference_version': 1, 'cite_key': 'filter'})

        errors = []
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        direct_url = f'{args.base_url}/terms?project={pid}'
        page.goto(direct_url)
        library = page.locator('#termLibrary')
        scope = library.get_by_role('navigation', name='术语库作用域')
        tabs = library.get_by_role('navigation', name='术语库视图')
        rows = library.locator('.tl-list')
        detail = library.get_by_role('region', name='术语详情')
        dialogs = page.locator('dialog.tl-dialog[open]')

        def action(label, locator=library):
            locator.get_by_role('button', name=label, exact=True).click()

        expect(library).to_be_visible()
        expect(page.locator('#termsBackToPaper')).to_have_attribute('href', f'/?project={pid}')
        expect(scope.get_by_role('button', name='论文术语库')).to_have_attribute('aria-pressed', 'true')
        expect(rows).to_contain_text('LFA')
        expect(rows).to_contain_text('AFF')
        expect(rows.get_by_role('checkbox', name='批量选择 LFA')).to_have_count(0)
        expect(rows.get_by_role('checkbox', name='批量选择 AFF')).to_be_visible()
        action('可疑')
        expect(rows).to_contain_text('LFA')
        expect(rows).not_to_contain_text('AFF')
        expect(detail).to_contain_text('当前范围未找到近似表述')
        action('全部')
        rows.get_by_role('button', name='AFF', exact=False).click()
        expect(detail).to_contain_text('稿件用法')
        expect(detail).to_contain_text('参考文献表述')
        expect(detail).to_contain_text('摘要')
        expect(detail.locator('mark').first).to_be_visible()
        page.screenshot(path=output / 'paper-terms-desktop.png')
        action('查看对照范围')
        expect(dialogs).to_contain_text('A Study of Adaptive Flux Filters')
        dialogs.get_by_role('checkbox').uncheck()
        action('按所选文献重新对照', dialogs)
        expect(rows.get_by_role('checkbox', name='批量选择 AFF')).to_have_count(0)
        action('查看对照范围')
        dialogs.get_by_role('checkbox').check()
        action('按所选文献重新对照', dialogs)
        rows.get_by_role('checkbox', name='批量选择 AFF').check()
        action('收录所选 1 项')
        expect(library.locator('.tl-status')).to_have_text('已收录 1 项论文术语。')
        action('已收录', tabs)
        expect(rows).to_contain_text('AFF')
        assert [term['term'] for term in api(project_path, method='GET')['terms']] == ['AFF']

        action('提交领域复核', detail)
        action('确认提交', dialogs)
        expect(scope.get_by_role('button', name='领域术语库')).to_have_attribute('aria-pressed', 'true')
        expect(rows).to_contain_text('AFF')
        assert all(term['term'] != 'AFF' for term in api(domain_path + '/terms', method='GET'))
        action('复核并发布', detail)
        dialogs.get_by_label('定义', exact=True).fill('人工确认的共享定义。')
        action('确认发布', dialogs)
        expect(library.locator('.tl-status')).to_have_text('已人工确认并发布领域版本。')
        action('已发布', tabs)
        expect(rows).to_contain_text('AFF')
        rows.get_by_role('button', name='有限体积法', exact=False).click()
        expect(detail).to_contain_text('领域定义一。')
        page.screenshot(path=output / 'domain-terms-desktop.png')
        action('领域版本历史')
        dialogs.get_by_label('查看领域版本').select_option('1')
        action('查看版本', dialogs)
        expect(library).to_contain_text('正在查看历史版本 v1')
        expect(rows).not_to_contain_text('AFF')
        expect(detail).to_contain_text('领域定义一。')
        action('复制到当前论文', detail)
        action('确认复制', dialogs)
        expect(rows).to_contain_text('有限体积法')
        expect(detail).to_contain_text('从领域库 v1 复制')

        action('领域术语库', scope)
        action('领域版本历史')
        dialogs.get_by_label('查看领域版本').select_option('2')
        action('查看版本', dialogs)
        rows.get_by_role('button', name='有限体积法', exact=False).click()
        action('编辑领域术语', detail)
        dialogs.get_by_label('定义', exact=True).fill('领域定义二。')
        action('保存术语', dialogs)
        expect(library).to_contain_text('已发布版本 v3')
        action('论文术语库', scope)
        rows.get_by_role('button', name='有限体积法', exact=False).click()
        expect(detail).to_contain_text('领域定义一。')
        snapshot = next(term for term in api(project_path, method='GET')['terms'] if term['term'] == '有限体积法')
        assert snapshot['definition'] == '领域定义一。'
        assert snapshot['source_domain_version'] == 1

        page.set_viewport_size({'width': 390, 'height': 844})
        action('待复核', tabs)
        library.get_by_role('searchbox').fill('LFA')
        rows.get_by_role('button', name='LFA', exact=False).press('Enter')
        expect(detail.get_by_role('heading', name='LFA')).to_be_focused()
        expect(rows).to_be_hidden()
        page.screenshot(path=output / 'paper-terms-mobile.png')
        action('返回术语列表', detail)
        expect(library.get_by_role('searchbox')).to_have_value('LFA')
        expect(rows.get_by_role('button', name='LFA', exact=False)).to_be_focused()
        rows.get_by_role('button', name='LFA', exact=False).click()
        action('排除', detail)
        expect(library).to_contain_text('本浏览器已从待复核列表排除')
        action('撤销排除')
        expect(rows).to_contain_text('LFA')
        assert page.evaluate('document.documentElement.scrollWidth') <= 390

        page.set_viewport_size({'width': 1440, 'height': 960})
        page.locator('#workspaceCurrentPaper').click()
        page.locator(f'.project-item[data-id="{other["id"]}"]').click()
        expect(page).to_have_url(re.compile(r'/terms\?project=' + other['id']))
        expect(rows).to_contain_text('当前没有待复核术语')
        action('领域术语库', scope)
        expect(library).to_contain_text('绑定此领域后可复制术语')
        expect(detail.get_by_role('button', name='复制到当前论文')).to_have_count(0)
        page.reload()
        expect(page.locator('#workspaceCurrentPaper')).to_have_text(other['name'])
        expect(rows).not_to_contain_text('LFA')

        page.goto(direct_url + '&tab=adopted')
        saved = tabs.get_by_role('button', name='已收录', exact=True)
        expect(saved).to_have_attribute('aria-pressed', 'true')
        expect(saved.locator('.tl-tab-count')).to_have_text('2')
        expect(rows).to_contain_text('有限体积法')
        page.reload()
        expect(saved).to_have_attribute('aria-pressed', 'true')
        expect(rows).to_contain_text('有限体积法')

        page.goto(f'{args.base_url}/?project={pid}#chat')
        expect(page.locator('#workspaceCurrentPaper')).to_have_text('术语回归样稿')
        page.locator('#openPaperTools').click()
        directory = page.locator('#paperToolDirectory')
        directory.locator('[data-tool-id="terms"]').get_by_role('button', name='打开', exact=True).click()
        expect(library).to_be_visible()
        action('已收录', tabs)
        expect(rows).to_contain_text('有限体积法')
        action('领域术语库', scope)
        expect(library).to_contain_text('已发布版本 v3')
        page.screenshot(path=output / 'domain-terms-conversation.png')
        page.get_by_role('button', name='返回对话', exact=True).click()
        expect(page.locator('#agentChat')).to_be_visible()
        assert not errors, errors
        print('PASS: 独立页、文献证据与批量采纳、人工发布、领域历史与论文快照、窄屏键盘、跨论文隔离、对话工具目录。')
        browser.close()


if __name__ == '__main__':
    main()
