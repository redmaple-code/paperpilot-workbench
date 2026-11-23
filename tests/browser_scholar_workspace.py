"""AI dialogue -> native research graph -> explicit planning review, in a real browser."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1512, 'height': 1100})
        response = context.request.post(args.base_url + '/api/projects', data={'name': '领域研究交互验证', 'domain': '研究方法'})
        pid = response.json()['project']['id']
        other = context.request.post(args.base_url + '/api/projects', data={'name': '另一篇独立论文'}).json()['project']['id']
        page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.base_url + '/#planning/startup')
        page.locator('.ws-paper-switcher > summary').click()
        page.locator(f'.project-item[data-id="{pid}"]').click()
        expect(page.locator('#researchWorkspace')).to_be_visible()
        expect(page.locator('#researchSkillStatus')).to_have_text('scholar-apprentice 已连接')
        expect(page.locator('#workspaceAssistant #agentChat')).to_be_visible()
        page.locator('#openResearchAgent').click()
        expect(page.locator('#startupFieldInputs')).not_to_be_visible()
        expect(page.locator('#researchGraphEmpty')).to_be_visible()
        expect(page.locator('#reviewStartup')).to_be_disabled()
        expect(page.locator('.research-heading h2')).to_be_visible()
        assert page.locator('.research-heading').evaluate('(element) => getComputedStyle(element).backgroundColor') == 'rgba(0, 0, 0, 0)'
        checks.append('第一阶段显示研究对话与图画布，手工研究字段退出默认界面')
        page.screenshot(path=str(output / 'research-empty.png'))

        page.locator('.research-starters button').first.click()
        expect(page.locator('#agentInput')).to_have_value('帮我建立这个领域的知识地图')
        page.locator('#agentSend').click()
        expect(page.locator('#agentPreview')).to_have_attribute('open', '')
        expect(page.locator('#agentRunStatus')).to_contain_text('请核对接收方')
        page.locator('#agentPreview').evaluate('(element)=>element.open=true')
        page.locator('#agentConsent').check()
        page.locator('#agentSend').click()
        expect(page.locator('#researchGraphStatus')).to_contain_text('3 个知识节点', timeout=60000)
        expect(page.locator('#agentRunStatus')).to_contain_text('本轮完成', timeout=60000)
        expect(page.locator('#researchGraphStatus')).to_contain_text('4 个路线节点')
        assert context.request.get(args.base_url + f'/api/projects/{pid}/agent/research').json()['revision'] == 1
        assert context.request.get(args.base_url + f'/api/projects/{other}/agent/research').json()['revision'] == 0
        checks.append('持久Agent经授权真实调用skill的知识图与路线接口，跨论文隔离')
        page.locator('#researchNodeList button').first.click()
        expect(page.locator('#researchNodeDetail')).to_contain_text('作者在当前会话提出的目标')
        page.locator('.ws-center').evaluate('(element) => element.scrollTo(0, 0)')
        page.screenshot(path=str(output / 'research-knowledge.png'))
        page.locator('[data-research-view="routes"]').click()
        expect(page.locator('#researchNodeList button')).to_have_count(4)
        page.locator('#researchNodeList button').filter(has_text='核查最近邻与反例').click()
        expect(page.locator('#researchNodeDetail')).to_contain_text('原始来源核对')
        page.locator('.ws-center').evaluate('(element) => element.scrollTo(0, 0)')
        checks.append('知识节点展示来源和条件，路线图展示目标到判别任务的实际结构')
        page.screenshot(path=str(output / 'research-routes.png'))

        before = context.request.get(args.base_url + f'/api/projects/{pid}/workflow/startup').json()
        assert not before['plan']['stages'][0]['fields']['research_question']
        page.locator('#researchAdoptDraft').click()
        page.get_by_role('button', name='写入规划草稿', exact=True).click()
        expect(page.locator('#researchSummaryFields')).to_contain_text('待检验的研究问题')
        page.locator('#reviewStartup').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('待审阅')
        page.locator('#confirmStartup').click()
        expect(page.locator('#startupCurrentTitle')).to_contain_text('写作大纲')
        expect(page.locator('#workspaceAssistant #agentChat')).to_be_visible()
        checks.append('AI方案经明确采纳、审阅和确认进入规划，各阶段保持全局Agent侧栏')

        page.locator('#wfGuide [data-stage="STAGE-01"]').click()
        expect(page.locator('#researchWorkspace')).to_be_visible()
        page.reload()
        expect(page.locator('#researchGraphStatus')).to_contain_text('3 个知识节点', timeout=15000)
        expect(page.locator('#agentThread')).to_contain_text('待验证候选')
        checks.append('刷新恢复原会话、研究图与路线')
        for width in [1512, 1024, 768, 390]:
            page.set_viewport_size({'width': width, 'height': 1100 if width > 390 else 844})
            page.wait_for_timeout(200)
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
            assert not page.locator('.ws-center').evaluate('(element) => element.scrollWidth > element.clientWidth + 1'), width
            page.screenshot(path=str(output / f'research-{width}.png'))
        checks.append('1512、1024、768、390四断点无横向溢出')

        page.set_viewport_size({'width': 1512, 'height': 1100})
        notice = page.locator('#workflowAnnouncement')
        page.evaluate("document.querySelector('#workflowAnnouncement').textContent='提示位置与淡出验证'")
        expect(notice).to_be_visible()
        box = notice.bounding_box()
        assert box['x'] > 750 and box['y'] > 900, box
        page.wait_for_timeout(3200)
        page.evaluate("document.querySelector('#workflowAnnouncement').textContent='新提示重新计时'")
        page.wait_for_timeout(2400)
        expect(notice).to_be_visible()
        expect(notice).not_to_be_visible(timeout=5000)
        checks.append('提示位于右下角，五秒后淡出，后到提示重新计时')
        assert not errors, errors
        result = {'status': 'passed', 'checks': checks, 'page_errors': errors, 'real_scholar_cli': True,
                  'real_agent_runtime': True, 'model': 'deterministic fixture', 'real_cloud_model': False}
        (output / 'BROWSER-RESULT.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(result, ensure_ascii=False)); browser.close()


if __name__ == '__main__':
    main()
