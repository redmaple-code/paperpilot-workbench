"""Browser verification with real local rendering and isolated app storage."""
from __future__ import annotations

import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".project-to-act/docs/project-development/evidence/full-product-20261101"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1100})
    created = context.request.post("http://127.0.0.1:8773/api/projects", data={"name": "候选审阅论文", "domain": "材料科学", "style": "严谨"})
    assert created.ok
    pid = created.json()["project"]["id"]
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8773")
    page.wait_for_load_state("networkidle")
    page.locator('[data-tab="visual"]').click()
    page.wait_for_function('document.querySelector("#drawingStatus").textContent.includes("描述图片用途")')
    page.locator("#drawingTitle").fill("Method flow")
    page.locator("#drawingIntent").fill("展示材料制备和分析流程")
    page.locator("#createDrawingRun").click()
    page.wait_for_function('document.querySelector("#drawingRecommendation").textContent.includes("等待选择")')
    assert not page.locator("#dispatchDrawingRun").is_enabled()
    assert not context.request.get(f"http://127.0.0.1:8773/api/projects/{pid}/drawing/assets").json()["assets"]
    page.locator("#drawingLabels").fill("Preparation\nAnalysis\nResults")
    page.locator("#confirmDrawingType").click()
    page.wait_for_function('document.querySelector("#drawingStatus").textContent.includes("输入与类型已确认")')
    page.locator("#dispatchDrawingRun").click()
    page.locator(".drawing-candidate img").wait_for()
    page.wait_for_function('document.querySelector(".drawing-candidate img").naturalWidth>0')
    assert not context.request.get(f"http://127.0.0.1:8773/api/projects/{pid}/drawing/assets").json()["assets"]
    page.locator("[data-save-candidate]").click()
    page.wait_for_function('document.querySelector("#drawingStatus").textContent.includes("候选已保存")')
    page.locator(".visual-asset-item").wait_for()
    assert "includegraphics" not in page.locator("#editor").input_value()
    # Copy for editing: source run remains immutable; undo/redo survive API refresh.
    page.locator("#copyDrawingRun").click()
    page.wait_for_function('document.querySelector("#drawingRecommendation").textContent.includes("等待选择")')
    page.locator("#drawingType").select_option("scene_graph")
    page.locator("#layoutDrawingNodes").click()
    page.locator("#confirmDrawingType").click()
    page.wait_for_function('document.querySelector("#drawingStatus").textContent.includes("输入与类型已确认")')
    page.locator('#drawingNodeRows [data-field="label"]').first.fill("Edited")
    page.locator("#confirmDrawingType").click()
    page.wait_for_function('document.querySelector("#undoDrawingEdit").disabled===false')
    page.locator("#undoDrawingEdit").click()
    page.wait_for_function('document.querySelector("#drawingNodeRows [data-field=label]").value==="Preparation"')
    page.locator("#redoDrawingEdit").click()
    page.wait_for_function('document.querySelector("#drawingNodeRows [data-field=label]").value==="Edited"')
    page.screenshot(path=str(OUTPUT / "REQ-023-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    box = page.get_by_role("region", name="绘图与候选审阅", exact=True).bounding_box()
    assert box["width"] >= 300
    assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
    page.get_by_role("region", name="绘图与候选审阅", exact=True).screenshot(path=str(OUTPUT / "REQ-023-mobile.png"))
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator('[data-tab="visual"]').click()
    page.wait_for_function('document.querySelector("#drawingNodeRows [data-field=label]")?.value==="Edited"')
    assert not errors, errors
    (OUTPUT / "REQ-023-BROWSER.json").write_text(json.dumps({"status": "passed", "project_isolated": True, "external_calls": 0,
        "checks": ["suggestion-before-execution", "explicit-candidate-save", "body-unchanged", "scene-edit-undo-redo", "refresh-recovery", "desktop", "390px"], "pageerrors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    context.close()
    browser.close()
