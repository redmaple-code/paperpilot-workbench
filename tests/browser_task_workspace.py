"""Exercise task routing and the image-to-manuscript flow in isolated storage."""
import argparse
import io
import json
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from reportlab.pdfgen.canvas import Canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    failed = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})

        def api(path, data=None, method='GET'):
            response = context.request.fetch(args.base_url + path, method=method, data=data)
            assert response.ok, (path, response.status, response.text()[:500])
            return response.json()

        paper = api('/api/projects', {'name': '任务改版隔离论文', 'domain': '界面验证'}, 'POST')
        pid = paper['project']['id']
        base = '/api/projects/' + pid
        api('/api/projects', {'name': '用于切换的另一篇论文', 'domain': '界面验证'}, 'POST')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: failed.append([response.url, response.status])
                if response.status >= 400 and '/api/' in response.url else None)
        page.goto(args.base_url + '/#visual/library')
        page.wait_for_load_state('networkidle')
        expect(page.locator('.ws-paper-switcher')).not_to_have_attribute('open', '')
        page.locator('.ws-paper-switcher > summary').click()
        page.locator(f'.project-item[data-id="{pid}"]').click()
        expect(page.locator('#workspaceCurrentPaper')).to_have_text('任务改版隔离论文')
        expect(page.locator('.ws-paper-switcher')).not_to_have_attribute('open', '')
        checks.append('论文切换器收合，选择论文后显示正确上下文')
        expect(page.locator('#visualImportForm')).not_to_be_visible()
        expect(page.locator('#drawingIntent')).not_to_be_visible()
        expect(page.locator('#runVisualEvidence')).not_to_be_visible()
        expect(page.locator('#compile')).not_to_be_visible()
        expect(page.locator('#drawImage')).to_have_count(0)
        checks.append('图像库首屏按任务组织，绘图、检查与旧入口分离')
        page.screenshot(path=str(output / 'image-library-empty.png'))
        page.locator('#visual > .ws-subnav [data-task="review"]').click()
        expect(page.locator('#runVisualEvidence')).to_be_disabled()
        page.locator('#refreshVisualReports').click()
        expect(page.locator('#visualEvidenceStatus')).to_contain_text('已刷新')
        expect(page.locator('#runVisualEvidence')).to_be_disabled()
        page.locator('#visual > .ws-subnav [data-task="library"]').click()

        page.locator('#openVisualImport').click()
        expect(page.locator('#importVisualAsset')).to_be_disabled()
        expect(page.locator('#visualPdfPage')).not_to_be_visible()
        stream = io.BytesIO()
        Image.new('RGB', (640, 360), '#527a86').save(stream, format='PNG')
        page.locator('#visualAssetFile').set_input_files({
            'name': 'experiment.png', 'mimeType': 'image/png', 'buffer': stream.getvalue()
        })
        page.locator('#visualAssetTitle').fill('实验流程图')
        page.locator('#importVisualAsset').click()
        expect(page.locator('#visualMainPreview')).to_be_visible()
        expect(page.locator('#visualAssetStatus')).to_contain_text('已导入')
        expect(page.locator('#visualImportForm')).not_to_be_visible()
        pdf = io.BytesIO()
        canvas = Canvas(pdf)
        canvas.drawString(40, 100, 'First page')
        canvas.showPage()
        canvas.drawString(40, 100, 'Second page')
        canvas.save()
        page.locator('#visualReplacementFile').set_input_files({
            'name': 'replacement.pdf', 'mimeType': 'application/pdf', 'buffer': pdf.getvalue()
        })
        expect(page.locator('#visualReplacementPage')).to_be_visible()
        page.locator('#visualReplacementPage').fill('2')
        page.locator('#confirmVisualReplacement').click()
        expect(page.locator('#visualAssetStatus')).to_contain_text('新版本已保存')
        assets = api(base + '/drawing/assets')['assets']
        assert assets[0]['current_version']['page_no'] == 2
        assert len(assets[0]['versions']) == 2
        version = assets[0]['current_version_id']
        checks.append('按需导入PNG，替换多页PDF时选择页码并保留旧版本')

        page.locator('#reviewVisualAsset').click()
        expect(page).to_have_url(args.base_url + '/#visual/review')
        expect(page.locator('#evidenceAsset')).to_have_value(version)
        page.locator('#runVisualEvidence').click()
        expect(page.locator('#visualReportDetail')).to_contain_text('报告 v', timeout=20000)
        checks.append('从图像详情进入所选版本的真实本地检查')
        page.screenshot(path=str(output / 'image-review.png'))
        page.locator('#visual > .ws-subnav [data-task="library"]').click()
        page.locator('#insertVisualAsset').click()
        expect(page.locator('.workspace')).to_have_attribute('data-view', 'write')
        assert '\\includegraphics' in page.locator('#editor').input_value()
        expect(page.locator('#editor')).to_be_focused()
        checks.append('插入所选图像版本后返回正文并显示真实引用')

        page.locator('[data-tab="visual"]').click()
        page.locator('#visual > .ws-subnav [data-task="library"]').click()
        page.locator('#openVisualDrawing').click()
        page.locator('#drawingTitle').fill('方法流程')
        page.locator('#drawingIntent').fill('展示准备、测量和分析三个步骤')
        page.locator('#createDrawingRun').click()
        expect(page.locator('#drawingRunDetails')).to_be_visible()
        page.locator('#drawingLabels').fill('Preparation\nMeasurement\nAnalysis')
        page.locator('#confirmDrawingType').click()
        expect(page.locator('#dispatchDrawingRun')).to_be_enabled()
        page.locator('#dispatchDrawingRun').click()
        expect(page.locator('[data-save-candidate]')).to_be_enabled(timeout=30000)
        page.locator('[data-save-candidate]').click()
        expect(page).to_have_url(args.base_url + '/#visual/library', timeout=20000)
        expect(page.locator('.visual-detail-head h3')).to_have_text('方法流程')
        assert len(api(base + '/drawing/assets')['assets']) == 2
        checks.append('本地绘图候选保存后进入图像库并选中新资产')
        page.screenshot(path=str(output / 'image-library.png'))

        page.locator('[data-tab="knowledge"]').click()
        page.locator('#knowledge [data-task="terms"]').click()
        expect(page.locator('#term')).not_to_be_visible()
        page.locator('#termLibrary').get_by_role('button',name='添加术语',exact=True).click()
        editor = page.get_by_role('dialog', name='添加术语')
        editor.get_by_label('术语',exact=True).fill('结构化工作台')
        editor.get_by_label('定义',exact=True).fill('将资料录入、浏览与高级复核按任务分离。')
        editor.get_by_role('button',name='保存术语').click()
        expect(page.locator('#termLibrary .tl-list')).to_contain_text('结构化工作台')
        page.screenshot(path=str(output / 'terminology.png'))
        page.reload()
        expect(page.locator('#termLibrary')).to_be_visible()
        expect(editor).not_to_be_visible()
        checks.append('资料按需录入，重载保留术语子任务且收合录入表单')

        page.locator('[data-view="settings"]').click()
        page.locator('#workspace-settings [data-task="images"]').click()
        expect(page.locator('#drawingProviderUrl')).to_be_visible()
        expect(page.locator('#providerUrl')).not_to_be_visible()
        page.locator('#workspace-settings [data-task="paper"]').click()
        page.locator('.ws-paper-management').locator('..').locator('summary').click()
        expect(page.locator('#archiveProject')).to_be_visible()
        checks.append('图像提供方归入设置，论文归档归入论文管理')
        page.locator('#workspace-settings [data-task="models"]').click()
        page.locator('#providerSettings > .ws-subnav [data-task="connection"]').click()
        page.locator('#providerName').fill('未提交的连接配置')
        page.locator('#providerSettings > .ws-subnav [data-task="capacity"]').click()
        expect(page.locator('#providerInputLimit')).to_be_visible()
        expect(page.locator('#providerName')).not_to_be_visible()
        page.locator('#providerSettings > .ws-subnav [data-task="test"]').click()
        expect(page.locator('#providerTestConsent')).to_be_visible()
        page.locator('#providerSettings > .ws-subnav [data-task="binding"]').click()
        expect(page.locator('#providerBind')).to_be_visible()
        page.locator('#providerSettings > .ws-subnav [data-task="connection"]').click()
        expect(page.locator('#providerName')).to_have_value('未提交的连接配置')
        checks.append('模型连接、能力、测试和绑定分区切换保留未提交输入')
        page.screenshot(path=str(output / 'model-settings.png'))
        page.locator('[data-view="planning"]').click()
        page.locator('#workspace-planning [data-task="progress"]').click()
        expect(page.locator('#projectLifecycleCard')).to_be_visible()
        expect(page.locator('#startupPlanner')).not_to_be_visible()
        page.locator('[data-tab="outline"]').click()
        page.locator('#outline [data-task="nodes"]').click()
        expect(page.locator('#outlineCanvas .x6-host')).to_be_visible()
        expect(page.locator('#outlineText')).not_to_be_visible()
        page.locator('[data-view="history"]').click()
        page.locator('#workspace-history [data-task="builds"]').click()
        expect(page.locator('.build-history')).to_be_visible()
        expect(page.locator('#workflowJobHistory')).not_to_be_visible()
        checks.append('大纲、规划与历史均按真实任务拆分，其他任务退出可见布局')

        layout_count = 0
        routes = ['write', 'outline/text', 'outline/nodes', 'outline/suggestions',
                  'knowledge/discover', 'knowledge/references', 'knowledge/corpus', 'knowledge/terms',
                  'knowledge/graph', 'knowledge/shared', 'visual/library', 'visual/drawing', 'visual/review',
                  'planning/startup', 'planning/progress', 'planning/resources',
                  'settings/paper', 'settings/models', 'settings/images', 'settings/learning',
                  'history/content', 'history/revisions', 'history/ai', 'history/builds', 'preview']
        for width, height in [(1440, 1000), (1024, 900), (768, 900), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            for route in routes:
                page.goto(args.base_url + '/#' + route)
                expect(page.locator('.workspace')).to_have_attribute('data-view', route.split('/')[0])
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), (width, route)
                assert not page.locator('.ws-center').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'), (width, route)
                layout_count += 1
            page.goto(args.base_url + '/#visual/library')
            page.screenshot(path=str(output / f'layout-{width}.png'))
        assert not errors, errors
        assert not failed, failed
        checks.append('25个任务在四宽度下无横向溢出、失败API或脚本错误')
        result = {'status': 'passed', 'checks': checks, 'layout_checks': layout_count,
                  'page_errors': errors, 'failed_api': failed, 'real_model_calls': False}
        (output / 'TASK-BROWSER-RESULT.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
