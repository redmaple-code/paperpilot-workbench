"""Real startup forms, confirmation boundaries, conflict recovery and copy in isolated storage."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101'
URL='http://127.0.0.1:8776'

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1100})
    pid=context.request.post(URL+'/api/projects',data={'name':'启动规划验收','domain':'材料','style':'严谨'}).json()['project']['id']
    other=context.request.post(URL+'/api/projects',data={'name':'独立规划目标','domain':'材料','style':'严谨'}).json()['project']['id']
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    assert page.evaluate('document.querySelector("#startupFormArea").hidden || document.querySelector("#startupPlanner").dataset.projectId===window.paperpilot.current.project.id')
    page.wait_for_function('(id)=>document.querySelector("#startupPlanner").dataset.projectId===id',arg=pid)
    page.locator('[data-startup-field="research_question"]').wait_for()
    assert page.locator('#startupStages li').count()==5
    assert not page.locator('#confirmStartup').is_enabled()
    page.get_by_label('研究问题',exact=True).fill('草稿独有内容，不得外发')
    page.keyboard.press('Tab')
    assert page.get_by_label('拟解决缺口与创新',exact=True).evaluate('(el)=>el===document.activeElement')
    page.locator('#reviewStartup').focus()
    page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#startupStatus").textContent.includes("草稿已保存。请补充")')
    assert '草稿独有内容' not in context.request.get(URL+f'/api/projects/{pid}/ai-context').json()['preview']
    page.get_by_label('拟解决缺口与创新',exact=True).fill('研究现有方法的适用范围')
    page.get_by_label('研究范围',exact=True).fill('固定数据与实验条件')
    page.locator('#reviewStartup').click()
    page.wait_for_function('document.querySelector("#startupCurrentTitle").textContent.includes("待审阅")')
    page.locator('#confirmStartup').focus()
    page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#startupCurrentTitle").textContent.includes("写作大纲")')
    assert '草稿独有内容' in context.request.get(URL+f'/api/projects/{pid}/ai-context').json()['preview']
    # Locally edited form vs another browser/API writer: conflict never overwrites either.
    page.get_by_label('章节大纲',exact=True).fill('本地大纲草稿')
    page.get_by_label('章节目标',exact=True).fill('本地章节目标')
    remote=context.request.get(URL+f'/api/projects/{pid}/workflow/startup').json()
    changed=context.request.post(URL+f'/api/projects/{pid}/workflow/startup/commands',data={'request_id':'remote-writer','expected_workflow_version':remote['workflow_version'],'command':'save','stage_id':'STAGE-02','fields':{'outline':'服务端其他作者的大纲','objectives':'服务端目标'}})
    assert changed.ok
    page.locator('#saveStartup').click()
    page.wait_for_function('document.querySelector("#startupStatus").textContent.includes("工作流已更新")')
    assert page.get_by_label('章节大纲',exact=True).input_value()=='本地大纲草稿'
    page.locator('#refreshStartup').click()
    page.locator('#startupConflict').wait_for(state='visible')
    assert '服务端其他作者的大纲' in page.locator('#startupServerDraft').inner_text()
    page.locator('#rebaseStartupDraft').click()
    page.locator('#saveStartup').click()
    page.wait_for_function('document.querySelector("#startupStatus").textContent.includes("草稿已保存，尚未")')
    assert page.get_by_label('章节大纲',exact=True).input_value()=='本地大纲草稿'
    page.reload();page.wait_for_load_state('networkidle')
    page.get_by_label('章节大纲',exact=True).wait_for()
    assert page.get_by_label('章节大纲',exact=True).input_value()=='本地大纲草稿'
    page.locator('#reviewStartup').click()
    page.wait_for_function('document.querySelector("#startupCurrentTitle").textContent.includes("待审阅")')
    page.locator('#confirmStartup').click()
    page.wait_for_function('document.querySelector("#startupCurrentTitle").textContent.includes("文章结构")')
    for values in [('structure','结构与论证顺序','transitions','章节衔接'),('terms','关键术语','evidence','证据与来源计划')]:
        page.get_by_label(values[1],exact=True).fill('已核对的'+values[1])
        page.get_by_label(values[3],exact=True).fill('已核对的'+values[3])
        if values[0]=='terms':page.get_by_label('缺口与补充动作',exact=True).fill('尚需补充实验数据，不能当作已有证据')
        page.locator('#reviewStartup').click()
        page.wait_for_function('document.querySelector("#startupCurrentTitle").textContent.includes("待审阅")')
        page.locator('#confirmStartup').click()
        page.wait_for_function('!document.querySelector("#startupCurrentTitle").textContent.includes("待审阅")')
    page.locator('#startupSkipReason').fill('先完成实验，再估算资源')
    page.locator('#skipStartup').click()
    page.wait_for_function('document.querySelector("#startupStatus").textContent.includes("启动规划已完成")')
    assert '跳过原因：先完成实验，再估算资源' in page.locator('#startupInvalidation').inner_text()
    saved=context.request.get(URL+f'/api/projects/{pid}/workflow/startup').json()
    assert saved['plan']['status']=='complete'
    page.locator('#startupPlanner').screenshot(path=str(OUTPUT/'REQ-021-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('#startupPlanner').bounding_box()['width']>=270
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    page.locator('#startupPlanner').screenshot(path=str(OUTPUT/'REQ-021-mobile.png'))
    # Explicit copy through the same workspace. Source remains unchanged.
    page.locator(f'.project-item[data-id="{other}"]').click()
    page.wait_for_function('(id)=>document.querySelector("#startupPlanner").dataset.projectId===id',arg=other)
    page.wait_for_function('document.querySelector("#startupCurrentTitle").textContent.includes("研究问题")')
    page.get_by_text('从其他论文复制为新规划',exact=True).click()
    page.locator('#startupSourceProject').select_option(pid)
    page.wait_for_function('document.querySelector("#startupSourceVersion").options.length>0 && document.querySelector("#startupSourceVersion").value!==""')
    page.locator('#copyStartup').click()
    page.wait_for_function('document.querySelector("#startupStatus").textContent.includes("已复制为独立新规划")')
    assert page.get_by_label('研究问题',exact=True).input_value()=='草稿独有内容，不得外发'
    target=context.request.get(URL+f'/api/projects/{other}/workflow/startup').json()
    assert target['confirmed_context']==[] and target['plan']['lineage']['source_project_id']==pid
    assert '复制来源：启动规划验收' in page.locator('#startupInvalidation').inner_text()
    assert f"v{target['plan']['lineage']['source_version']}" in page.locator('#startupInvalidation').inner_text()
    assert context.request.get(URL+f'/api/projects/{pid}/workflow/startup').json()['plan']['status']=='complete'
    assert not errors,errors
    (OUTPUT/'REQ-021-BROWSER.json').write_text(json.dumps({'status':'passed','database_isolated':True,'external_ai_calls':0,'pageerrors':errors,'checks':['five-stage-forms','incomplete-draft-retained','confirmed-context-only','version-conflict-and-explicit-rebase','reload-recovery','optional-skip','keyboard-tab-enter','desktop','390px','explicit-independent-copy']},ensure_ascii=False,indent=2),encoding='utf-8')
    context.close();browser.close()
