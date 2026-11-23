import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        desktop = browser.new_context(viewport={"width": 1440, "height": 900})
        paper = desktop.request.post(base + "/api/projects", data={"name": "创新点桌面验收"}).json()["project"]["id"]
        other = desktop.request.post(base + "/api/projects", data={"name": "创新点隔离验收"}).json()["project"]["id"]
        prepared = desktop.request.put(base + f"/api/projects/{other}/files", data={
            "path": "main.tex", "base_version": 1, "content": "\\section{Introduction}\n本文提出有原文定位的方案。"
        })
        assert prepared.ok, prepared.text()
        page = desktop.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + f"/innovations?project={paper}")
        page.get_by_role("button", name="新建草稿").click()
        page.get_by_label("创新主张").fill("一条待证据支撑的创新主张")
        page.get_by_role("button", name="添加支撑工作").click()
        page.get_by_label("需要做什么").fill("与一个既有方法做对照")
        page.get_by_role("button", name="保存草稿").click()
        page.get_by_role("button", name="确认此版本").click()
        expect(page.get_by_text("创新点已确认。")).to_be_visible()
        expect(page.get_by_text("待关联实际工作或产物")).to_be_visible()
        page.reload()
        expect(page.get_by_role("heading", name="主创新 · v1")).to_be_visible()
        page.get_by_role("link", name="返回论文").click()
        page.locator("#openPaperTools").click()
        tool = page.get_by_text("论文创新点", exact=True)
        expect(tool).to_be_visible()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.locator('[data-tool-id="innovations"]').get_by_role("button", name="打开").click()
        expect(page.get_by_role("button", name="一条待证据支撑的创新主张 · v1")).to_be_visible()
        page.get_by_label("当前论文").select_option(other)
        expect(page.get_by_text("这篇论文还没有创新点。")).to_be_visible()
        page.get_by_text("从现有材料提取").click()
        page.get_by_label("来源", exact=True).select_option(label="main.tex")
        page.get_by_role("button", name="查看原文候选").click()
        page.get_by_role("button", name="作为候选", exact=True).click()
        expect(page.get_by_label("创新主张")).to_have_value("本文提出有原文定位的方案。")
        page.get_by_role("button", name="确认此版本").click()
        expect(page.get_by_role("heading", name="主创新 · v1")).to_be_visible()
        mobile = browser.new_page(viewport={"width": 390, "height": 840})
        mobile.goto(base + f"/innovations?project={paper}")
        mobile.get_by_role("button", name="一条待证据支撑的创新主张 · v1").focus()
        mobile.keyboard.press("Enter")
        expect(mobile.get_by_role("heading", name="主创新 · v1")).to_be_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth === innerWidth")
        assert not errors, errors
        (output / "innovation-browser.json").write_text(json.dumps({
            "mode": "real_browser_synthetic_data",
            "desktop": ["draft", "confirmation_before_results", "reload", "conversation_tool", "paper_isolation", "manuscript_extraction"],
            "mobile": ["keyboard_open", "no_horizontal_overflow"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()


if __name__ == "__main__":
    main()
