"""Browser exercise of the reference-to-domain glossary workflow."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 960})

        def api(path, body, method='POST'):
            response = context.request.fetch(args.base_url + path, method=method, data=body)
            assert response.ok, (path, response.status, response.text())
            return response.json()

        domain = api('/api/domains', {'name': '数值方法领域'})
        project = api('/api/projects', {'name': '参考文献术语检验'})['project']
        pid = project['id']
        ref = api('/api/references', {'title': 'Reconstruction study', 'abstract':
            'WENO-Z+ scheme is used for reconstruction. WENO-JS, WENO-Z and the ENO-M scheme are comparisons.'})
        api(f'/api/projects/{pid}/references/{ref["id"]}', {
            'request_id': 'browser-domain-claim', 'reference_version': 1, 'cite_key': 'reconstruction',
        })

        page = context.new_page()
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(f'{args.base_url}/terms?project={pid}&tab=domain-review')
        panel = page.locator('#termLibrary')
        dialog = page.locator('dialog.tl-dialog[open]')
        rows = panel.locator('.tl-list')
        details = panel.locator('.tl-detail')
        panel.get_by_role('button', name='纳入当前论文文献').click()
        dialog.get_by_role('button', name='确认纳入').click()
        expect(rows).to_contain_text('WENO-Z+')
        rows.locator('.tl-row').filter(has=page.get_by_text('WENO-Z+', exact=True)).click()
        expect(details).to_contain_text('WENO-Z+ scheme is used for reconstruction.')
        expect(details).to_contain_text('摘要')
        expect(panel.get_by_role('button', name='文献生成 4 项候选', exact=True)).to_be_visible()
        page.screenshot(path=output / 'domain-reference-review.png')
        details.get_by_role('button', name='接受并入库', exact=True).click()
        expect(panel.locator('.tl-status')).to_contain_text('已入库 1 项，领域术语库 v2。1 项待补定义。')
        expect(dialog).to_have_count(0)
        pending = panel.get_by_role('navigation', name='术语库视图').get_by_role('button', name='待复核', exact=True)
        expect(pending).to_have_attribute('aria-pressed', 'true')
        expect(rows).not_to_contain_text('WENO-Z+')
        expect(rows).to_contain_text('WENO-JS')
        panel.get_by_role('button', name='已发布', exact=True).click()
        expect(rows).to_contain_text('待补定义')
        expect(details).to_contain_text('已发布的文献依据')
        assert api(f'/api/projects/{pid}', None, 'GET')['project']['domain_id'] == ''
        terms = api(f'/api/domains/{domain["id"]}/terms', None, 'GET')
        assert terms[0]['definition'] == ''
        assert terms[0]['reference_sources'][0]['version'] == 1
        pending.click()
        panel.get_by_role('searchbox').fill('WENO')
        panel.get_by_role('button', name='一键入库（2 项）', exact=True).click()
        expect(panel.locator('.tl-status')).to_contain_text('已入库 2 项，领域术语库 v3。2 项待补定义。')
        expect(pending).to_have_attribute('aria-pressed', 'true')
        expect(panel.get_by_role('searchbox')).to_have_value('WENO')
        panel.get_by_role('searchbox').clear()
        expect(rows).to_contain_text('ENO-M')
        terms = api(f'/api/domains/{domain["id"]}/terms', None, 'GET')
        assert {term['term'] for term in terms} == {'WENO-Z+', 'WENO-JS', 'WENO-Z'}
        assert {term['version'] for term in terms} == {3}

        api(f'/api/references/{ref["id"]}', {'base_version': 1, 'changes': {
            'abstract': 'WENO-JS scheme was added as an independent comparator.',
        }}, 'PUT')
        page.goto(f'{args.base_url}/?project={pid}#chat')
        expect(page.locator('#workspaceCurrentPaper')).to_have_text(project['name'])
        page.locator('#openPaperTools').click()
        page.locator('#paperToolDirectory [data-tool-id="terms"]').get_by_role('button', name='打开').click()
        panel.get_by_role('button', name='领域术语库', exact=True).click()
        panel.get_by_role('button', name='文献生成 1 项候选', exact=True).click()
        expect(rows).to_contain_text('WENO-JS')
        expect(details).to_contain_text('WENO-JS scheme was added as an independent comparator.')
        expect(rows).not_to_contain_text('WENO-Z+')
        page.locator('#openPaperTools').click()
        page.locator('#paperToolDirectory [data-tool-id="references"]').get_by_role('button', name='打开').click()
        references = page.locator('#referenceLibraryCard')
        references.locator('..').locator(':scope > summary').click()
        references.get_by_role('button', name='查看领域术语候选').click()
        expect(rows).to_contain_text('WENO-JS')
        assert not errors, errors
        browser.close()

    print('PASS: live reference sources, original quotations, explicit release, latest library edition, both entry points.')


if __name__ == '__main__':
    main()
