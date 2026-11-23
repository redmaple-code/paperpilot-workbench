from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    errors: list[str] = []
    bad_responses: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("response", lambda response: bad_responses.append(f"{response.status} {response.url}") if response.status >= 400 else None)
        page.goto(args.base_url, wait_until="networkidle")
        page.wait_for_timeout(500)
        if page.locator("#newDialog").count() == 0:
            print(json.dumps({
                "url": page.url, "title": page.title(), "body": page.locator("body").inner_text(), "errors": errors,
                "bad_responses": bad_responses, "html": page.content(),
            }, ensure_ascii=False))
        elif not page.locator("#newDialog").evaluate("element => element.open"):
            print(json.dumps({"body": page.locator("body").inner_text(), "errors": errors, "bad_responses": bad_responses}, ensure_ascii=False))
        if not page.locator("#newDialog").evaluate("element => element.open"):
            page.locator("#newProject").click()
        page.locator("#newDialog[open]").wait_for()
        page.locator("#newName").fill("Workflow browser smoke")
        page.locator("#newDomain").fill("材料科学")
        page.get_by_role("button", name="创建").click()
        page.wait_for_function("document.querySelector('#workflowStatus')?.textContent.includes('待开始')", timeout=10000)

        page.get_by_role("button", name="进入人工修订阶段").focus()
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#workflowStatus')?.textContent.includes('人工修订')", timeout=10000)
        page.get_by_role("button", name="进入人工修订模式").focus()
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#workflowModeState')?.textContent.includes('已进入')", timeout=10000)
        assert page.locator("#workflowAssistControls").is_visible()
        assert page.locator("#workflowAssistAction option").count() == 7

        page.locator("#editor").fill("\\documentclass{article}\n\\begin{document}\nA saved draft about graphene stability.\\section{Introduction}\\end{document}")
        page.locator("#editor").evaluate("""element => {
          const start = element.value.indexOf('Introduction');
          element.setSelectionRange(start, start + 'Introduction'.length);
        }""")
        page.wait_for_function("document.querySelector('#workflowModeState')?.textContent.includes('1 项可恢复草稿')", timeout=10000)
        page.locator("#saveFile").click()
        page.wait_for_function("document.querySelector('#workflowAnnouncement')?.textContent.includes('已保存检查点 v2')", timeout=10000)
        page.locator("#fileSelect").select_option("references.bib")
        page.locator("#editor").fill("@misc{switchdraft, title={Switch recovery smoke}}")
        page.locator("#fileSelect").select_option("main.tex")
        page.wait_for_function("document.querySelector('#workflowModeState')?.textContent.includes('1 项可恢复草稿')", timeout=10000)
        project_id = page.evaluate("window.paperpilot.current.project.id")
        page.wait_for_function("""async projectId => {
          const response = await fetch(`/api/projects/${projectId}/human-revision/drafts`);
          const drafts = await response.json();
          return drafts.some(draft => draft.target_path === 'references.bib' && draft.content.includes('switchdraft'));
        }""", arg=project_id, timeout=10000)
        page.locator("#editor").evaluate("""element => {
          const start = element.value.indexOf('Introduction');
          element.setSelectionRange(start, start + 'Introduction'.length);
        }""")

        page.locator("#workflowAssistAction").select_option("rewrite")
        page.locator("#workflowAssistPrompt").fill("Rewrite the section heading")
        page.locator("#workflowAssistGenerate").click()
        page.locator("#workflowContext[open]").wait_for(timeout=15000)
        page.locator("#workflowPreflightControls button").get_by_text("使用此上下文生成候选").wait_for(timeout=15000)
        assert "大纲（完整）" in page.locator("#workflowPreview").inner_text()
        assert "费用未知" in page.locator("#workflowContextStatus").inner_text()
        page.locator("#workflowPreflightControls button").get_by_text("使用此上下文生成候选").click()
        page.get_by_role("button", name="审阅并应用全部").wait_for(timeout=10000)
        page.get_by_text("AI candidate text").wait_for(timeout=10000)

        project_root = Path(__file__).resolve().parents[1]
        shot_dir = project_root / ".project-to-act" / "docs" / "project-development" / "packages" / "REQ-001" / "v24" / "browser-evidence"
        shot_dir.mkdir(parents=True, exist_ok=True)
        desktop = shot_dir / "desktop.png"
        mobile = shot_dir / "mobile.png"
        page.screenshot(path=str(desktop), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        mobile_width = page.evaluate("document.documentElement.scrollWidth")
        viewport_width = page.evaluate("window.innerWidth")
        page.screenshot(path=str(mobile), full_page=True)
        if mobile_width > viewport_width + 2:
            diagnostics = page.evaluate("""() => ({
              root: {innerWidth, client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth, body: document.body.scrollWidth},
              elements: [...document.querySelectorAll('body *')].map(el => {
                const rect = el.getBoundingClientRect();
                return {tag: el.tagName, id: el.id, className: String(el.className || ''), right: Math.round(rect.right), width: Math.round(rect.width), client: el.clientWidth, scroll: el.scrollWidth, min: getComputedStyle(el).minWidth, overflow: getComputedStyle(el).overflowX};
              }).filter(item => item.right > innerWidth + 2 || item.scroll > item.client + 20).sort((a, b) => b.scroll - a.scroll).slice(0, 24)
            })""")
            raise AssertionError(f"mobile horizontal overflow: {mobile_width}px > {viewport_width}px; diagnostics={diagnostics}")
        page.get_by_role("button", name="审阅并应用全部").click()
        page.wait_for_function("document.querySelector('#editor').value.includes('AI candidate text') && !document.querySelector('#editor').value.includes('Introduction')", timeout=10000)
        if errors:
            raise AssertionError("browser errors: " + " | ".join(errors))
        print(json.dumps({
            "result": "passed",
            "desktop_screenshot": str(desktop),
            "mobile_screenshot": str(mobile),
            "mobile_width": mobile_width,
            "viewport_width": viewport_width,
            "browser_errors": errors,
        }, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
