"""Native structure migration through real browser controls and isolated synthetic data."""
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
    url = args.base_url.rstrip('/')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {'mode': 'real_browser_synthetic_data', 'checks': [], 'errors': []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1600, 'height': 1100})
        request = context.request

        def api(path, body=None, method='POST'):
            response = request.fetch(url + path, method=method, data=body)
            assert response.ok, response.text()
            return response.json()

        pid = api('/api/projects', {'name': '原生结构迁移验证 ' + uuid.uuid4().hex[:5]})['project']['id']
        root = f'/api/projects/{pid}/structure-workspaces'
        current = api(root, {'request_id': 'native-source', 'entry_mode': 'paper_import',
                            'content': '研究问题是解释稳定性。\n\n拟比较两类方法。\n\n实验尚未开展，结果待验证。'})
        workspace = current['workspace']
        segments = workspace['source']['segments']
        nodes = []
        for index, title in enumerate(['研究问题', '方法计划', '验证边界']):
            segment = segments[index]
            nodes.append({'id': f'node-{index}', 'label': title, 'kind': 'claim', 'summary': title + '的原始说明',
                          'content': '', 'confidence': None, 'status': 'confirmed', 'provenance': 'user_edited',
                          'source_refs': [{'source_id': workspace['source']['id'], 'locator': segment['id'],
                                           'excerpt': segment['text'], 'granularity': 'paragraph', 'overview': segment['text']}]})
        workspace['graph'] = {'nodes': nodes, 'edges': [
            {'id': 'edge-1', 'from': 'node-0', 'to': 'node-1', 'relation': '拟通过方法回答', 'confidence': .4},
            {'id': 'edge-2', 'from': 'node-1', 'to': 'node-2', 'relation': '需验证适用范围', 'confidence': None}], 'groups': []}
        workspace['layout'].update(grouping_mode='manual', module_layout_version=1)
        workspace['paper_outline'] = [
            {'section_id': 'introduction', 'parent_id': None, 'position': 0, 'title': '引言', 'purpose': '交代研究问题', 'node_ids': ['node-0']},
            {'section_id': 'method', 'parent_id': None, 'position': 1, 'title': '方法与验证', 'purpose': '组织验证方案', 'node_ids': ['node-1', 'node-2']}]
        workspace['pending_suggestions'] = [{'id': 'suggestion-1', 'target_kind': 'node', 'target_id': 'node-0',
            'status': 'pending', 'suggested_action': 'update', 'proposed_fields': {'summary': '拟通过重复采样核验稳定性'}}]
        path = root + '/' + workspace['id']
        api(path, {'request_id': 'native-structure', 'expected_workspace_revision': current['version'],
                   'source_hash': workspace['source']['content_hash'], 'commands': [{'op': 'replace_graph', 'workspace': workspace}]}, 'PUT')
        api(path + '/layout', {'request_id': 'native-layout', 'expected_layout_revision': 0,
            'positions': {'node-0': {'x': 60, 'y': 70}, 'node-1': {'x': 400, 'y': 70}, 'node-2': {'x': 400, 'y': 250}}}, 'PUT')

        page = context.new_page()
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        page.goto(f'{url}/outline?project={pid}#outline/nodes')
        expect(page.locator('.structure-native')).to_be_visible()
        assert page.locator('#outline iframe').count() == 0
        assert page.locator('#outlineCanvas').count() == 0
        report['checks'].append('normal outline entry mounts one native component without editor iframe or X6 shell')
        close_assistant = page.locator('#closeWorkspaceAssistant')
        if close_assistant.is_visible():
            close_assistant.click()

        expect(page.locator('.structure-chapter-spine .structure-chapter-card')).to_have_count(2)
        page.locator('[data-map-section="method"]').click()
        expect(page.locator('.structure-branch-nodes [data-map-node]')).to_have_count(2)
        page.get_by_role('button', name='方法计划', exact=True).click()
        expect(page.get_by_label('节点或关系名称', exact=True)).to_have_value('方法计划')
        page.get_by_role('button', name='章节图谱', exact=True).click()
        page.get_by_role('button', name='‹ 章节总览', exact=True).click()
        page.locator('[data-map-section="introduction"]').click()
        page.get_by_role('button', name='完整图谱', exact=True).click()
        expect(page.locator('.structure-node')).to_have_count(3)
        expect(page.locator('[data-edge].selected')).to_have_count(0)
        page.locator('[data-edge="edge-1"]').press('Enter')
        expect(page.locator('[data-edge].selected')).to_have_count(1)
        expect(page.locator('.structure-edge-description')).to_contain_text('拟通过方法回答')
        page.locator('[data-section="introduction"]').click()
        expect(page.locator('[data-edge].selected')).to_have_count(0)
        report['checks'].append('the chapter overview drills into bound nodes and returns to the editable graph')

        page.get_by_label('章节写作目标', exact=True).fill('原生界面修改的章节目标')
        page.locator('[data-node="node-0"]').click()
        page.get_by_label('节点或关系名称', exact=True).fill('作者修改的研究问题')
        page.locator('[data-structure-action="save"]').click()
        expect(page.locator('.structure-status')).to_contain_text('已保存')
        saved = api(path, method='GET')
        assert saved['workspace']['paper_outline'][0]['purpose'] == '原生界面修改的章节目标'
        assert saved['workspace']['graph']['nodes'][0]['label'] == '作者修改的研究问题'
        report['checks'].append('chapter and node edits share the same save and persist both changes')

        page.get_by_role('button', name='来源依据', exact=True).click()
        page.locator('.structure-source-fragment').first.click()
        expect(page.locator('[data-node="node-0"]')).to_have_class('structure-node selected')
        report['checks'].append('source selection highlights its linked native graph node')

        page.get_by_role('button', name='内容与目标', exact=True).click()
        expect(page.locator('.structure-suggestion')).to_have_count(1)
        page.locator('.structure-suggestion').get_by_role('button', name='接受', exact=True).click()
        page.locator('[data-structure-action="save"]').click()
        expect(page.locator('.structure-status')).to_contain_text('已保存')
        saved = api(path, method='GET')
        assert saved['workspace']['pending_suggestions'][0]['status'] == 'accepted'
        report['checks'].append('explicit suggestion acceptance persists through the unified editor')

        page.locator('[data-section="introduction"]').click()
        page.get_by_label('章节写作目标', exact=True).fill('刷新恢复的未保存章节目标')
        page.reload()
        page.wait_for_load_state('networkidle')
        expect(page.get_by_label('章节写作目标', exact=True)).to_have_value('刷新恢复的未保存章节目标')
        page.locator('[data-structure-action="save"]').click()
        expect(page.locator('.structure-status')).to_contain_text('已保存')
        page.get_by_role('button', name='完整图谱', exact=True).click()
        report['checks'].append('one local recovery journal restores unsaved chapter edits after reload')

        before = api(path, method='GET')
        node = page.locator('[data-node="node-0"]')
        node.scroll_into_view_if_needed()
        box = node.bounding_box()
        page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.mouse.down()
        page.mouse.move(box['x'] + box['width'] / 2 + 42, box['y'] + box['height'] / 2 + 28, steps=8)
        page.mouse.up()
        page.locator('[data-structure-action="save"]').click()
        expect(page.locator('.structure-status')).to_contain_text('已保存')
        after = api(path, method='GET')
        assert after['version'] == before['version']
        assert after['host']['layout_revision'] > before['host']['layout_revision']
        assert after['workspace']['layout']['positions']['node-0'] != before['workspace']['layout']['positions']['node-0']
        report['checks'].append('real pointer drag changes layout without changing semantic version')

        page.once('dialog', lambda dialog: dialog.accept('作者新增的待验证论点'))
        page.get_by_role('button', name='添加论点', exact=True).click()
        expect(page.locator('.structure-node')).to_have_count(4)
        new_node = page.get_by_role('button', name='节点：作者新增的待验证论点', exact=True)
        new_id = new_node.get_attribute('data-node')
        page.locator('[data-editor-action="zoom-out"]').click()
        page.locator('[data-editor-action="fit"]').click()
        page.locator('[data-output="node-2"]').click()
        page.once('dialog', lambda dialog: dialog.accept('需要补充验证'))
        page.locator(f'[data-input="{new_id}"]').click()
        expect(page.locator('[data-edge]')).to_have_count(3)
        page.get_by_role('button', name='撤销结构编辑', exact=True).click()
        expect(page.locator('[data-edge]')).to_have_count(2)
        page.get_by_role('button', name='重做结构编辑', exact=True).click()
        expect(page.locator('[data-edge]')).to_have_count(3)
        page.locator('[data-structure-action="save"]').click()
        expect(page.locator('.structure-status')).to_contain_text('已保存')
        new_node.focus()
        new_node.press('Enter')
        expect(page.get_by_label('节点或关系名称', exact=True)).to_have_value('作者新增的待验证论点')
        report['checks'].append('native add-node, port connection, undo, redo and keyboard selection work together')

        remote = api(path, method='GET')
        remote['workspace']['paper_outline'][0]['purpose'] = '其他窗口的章节目标'
        api(path, {'request_id': 'native-remote-edit', 'expected_workspace_revision': remote['version'],
            'source_hash': remote['workspace']['source']['content_hash'],
            'commands': [{'op': 'replace_graph', 'workspace': remote['workspace']}]}, 'PUT')
        page.locator('[data-section="introduction"]').click()
        page.get_by_label('章节写作目标', exact=True).fill('作者选择保留的章节目标')
        page.locator('[data-structure-action="save"]').click()
        expect(page.locator('.structure-status')).to_contain_text('比较版本')
        page.locator('[data-structure-action="resolve"]').click()
        page.get_by_role('button', name='保存合并结果', exact=True).click()
        expect(page.locator('.structure-status')).to_contain_text('已保存')
        assert api(path, method='GET')['workspace']['paper_outline'][0]['purpose'] == '作者选择保留的章节目标'
        report['checks'].append('native conflict dialog explicitly resolves concurrent chapter edits')

        page.locator('[data-structure-action="proposal"]').click()
        expect(page.locator('.structure-apply-dialog[open]')).to_be_visible()
        page.once('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='应用所选章节与目标', exact=True).click()
        expect(page.locator('.structure-apply-dialog[open]')).to_have_count(0)
        outline = api(f'/api/projects/{pid}/outline/structured', method='GET')
        assert any(item.get('purpose') == '作者选择保留的章节目标' for item in outline['nodes'])
        report['checks'].append('reviewed native outline is adopted through the existing project action service')

        for width, height in [(768, 1024), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            expect(page.locator('.structure-native')).to_be_visible()
            assert page.locator('.structure-native').evaluate('(el)=>el.scrollWidth <= el.clientWidth + 1')
            page.screenshot(path=str(output / f'native-{width}.png'), full_page=True)
            report['checks'].append(f'native component renders without horizontal overflow at {width}px')
        page.reload()
        expect(page.locator('.structure-native')).to_be_visible()
        page.get_by_role('button', name='完整图谱', exact=True).click()
        zoom = page.locator('.structure-zoom')
        expect(zoom).not_to_have_text('100%')
        initial_zoom = zoom.inner_text()
        page.goto(f'{url}/outline?project={pid}#outline/formal')
        page.reload()
        expect(page.locator('.structure-chapter-card').first).to_be_attached()
        expect(page.locator('.structure-native')).to_be_hidden()
        page.locator('#outline [data-task="nodes"]').click()
        page.get_by_role('button', name='完整图谱', exact=True).click()
        expect(zoom).to_have_text(initial_zoom)
        report['checks'].append('opening the native tab after the formal chapter tab fits the visible canvas')
        page.set_viewport_size({'width': 1600, 'height': 1100})
        page.screenshot(path=str(output / 'native-editor.png'), full_page=True)
        assert not report['errors'], report['errors']
        report.update(passed=True, project_id=pid, workspace_id=workspace['id'])
        browser.close()

    (output / 'BROWSER-NATIVE.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'checks': len(report['checks'])}))


if __name__ == '__main__':
    main()
