from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:43791")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    errors: list[str] = []
    dialogs: list[str] = []
    screenshot_root = Path(__file__).resolve().parents[1] / ".project-to-act" / "docs" / "project-development" / "evidence" / "REQ-004-006-v2"
    screenshot_root.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.set_default_timeout(8000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda request: errors.append(f"request failed: {request.method} {request.url} {request.failure}"))
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        created = []
        for name in ("UI Project A", "UI Project B"):
            response = page.request.post(f"{base_url}/api/projects", data={"name": name, "domain": "材料科学"})
            assert response.ok, response.text()
            created.append(response.json()["project"]["id"])
        page.goto(f"{base_url}/", wait_until="networkidle")
        page.locator(f"#projects .project-item[data-id='{created[0]}']").click()
        page.wait_for_function("document.querySelector('#projectName')?.value === 'UI Project A'")
        page.get_by_role("tab", name="知识库").click()
        page.wait_for_timeout(500)
        page.wait_for_function("document.querySelector('#logicGraphStatus')?.textContent.includes('图版本')")

        page.locator("#logicNodeKind").select_option("claim")
        page.locator("#logicNodeLabel").fill("关键论点")
        page.locator("#logicSaveNode").click()
        page.wait_for_timeout(500)
        page.wait_for_function("document.querySelector('#logicGraphStatus')?.textContent.includes('v1')")
        assert "缺口" in page.locator("#logicGraphIssues").inner_text()

        page.locator("#logicNodeKind").select_option("evidence")
        page.locator("#logicNodeLabel").fill("实验来源")
        page.locator("#logicSaveNode").click()
        page.wait_for_function("document.querySelector('#logicGraphStatus')?.textContent.includes('2 个节点')")
        page.wait_for_function("document.querySelector('#logicEdgeFrom').options.length >= 2")
        evidence_id = page.locator("#logicEdgeFrom option").nth(1).get_attribute("value")
        claim_id = page.locator("#logicEdgeTo option").nth(0).get_attribute("value")
        page.locator("#logicEdgeFrom").select_option(value=evidence_id)
        page.locator("#logicEdgeTo").select_option(value=claim_id)
        page.locator("#logicEdgeType").select_option("supports")
        page.locator("#logicEdgeForm button").click()
        page.wait_for_function("document.querySelector('#logicGraphStatus')?.textContent.includes('1 条关系')")
        graph_after_edge = page.request.get(f"{base_url}/api/projects/{created[0]}/logic/graph").json()
        assert graph_after_edge["edges"][0]["edge_type"] == "supports"
        assert graph_after_edge["gaps"] == []
        page.locator("#logicListView").click()
        assert page.locator("#logicListView").get_attribute("aria-pressed") == "true"
        page.locator("#logicGraphView").click()

        current_graph = page.request.get(f"{base_url}/api/projects/{created[0]}/logic/graph").json()
        server_node_id = f"server-concurrent-{uuid.uuid4().hex}"
        concurrent_request_id = f"browser-concurrent-save-{uuid.uuid4().hex}"
        concurrent_nodes = [*current_graph["nodes"], {"id": server_node_id, "kind": "claim", "label": "并发保存"}]
        concurrent = page.request.put(
            f"{base_url}/api/projects/{created[0]}/logic/graph",
            data={"request_id": concurrent_request_id, "base_version": current_graph["version"], "nodes": concurrent_nodes, "edges": current_graph["edges"]},
        )
        assert concurrent.ok, concurrent.text()
        page.locator("#logicNodeLabel").fill("本地冲突草稿")
        page.locator("#logicSaveNode").click()
        page.wait_for_function(
            "document.querySelector('#logicGraphStatus')?.textContent.includes('逻辑图版本已变化')",
            timeout=10000,
        )
        assert "逻辑图版本已变化" in page.locator("#logicGraphStatus").inner_text()
        assert page.locator("#logicNodeLabel").input_value() == "本地冲突草稿"
        assert page.evaluate("document.activeElement.id") == "logicGraphStatus"
        page.evaluate("window.paperpilot.reload()")
        page.wait_for_function("document.querySelector('#logicGraphStatus')?.textContent.includes('3 个节点')")

        page.locator("#logicSuggestionPrompt").fill("检查论点与证据是否匹配")
        page.locator("#logicSuggest").click()
        page.get_by_text("待审建议", exact=False).wait_for()
        page.locator("#logicSuggestionReview").get_by_role("button", name="拒绝建议").click()
        page.wait_for_function("document.querySelector('#logicGraphStatus')?.textContent.includes('建议已拒绝')")

        evidence_card = page.locator("#logicGraphNodes .logic-node").filter(has_text="实验来源")
        evidence_card.get_by_role("button", name="删除").click()
        page.wait_for_function("document.querySelectorAll('#logicGraphNodes .logic-node').length === 2")
        assert any("删除“实验来源”" in message for message in dialogs), dialogs
        page.locator("#logicUndo").click()
        page.wait_for_function("document.querySelectorAll('#logicGraphNodes .logic-node').length === 3")

        reference_title = f"Browser-Test Reference {uuid.uuid4().hex[:8]}"
        reference_doi = f"10.1000/browser-test-{uuid.uuid4().hex}"
        page.locator(".reference-library-card details summary").click()
        page.locator("#libraryReferenceTitle").fill(reference_title)
        page.locator("#libraryReferenceAuthors").fill("A. Author")
        page.locator("#libraryReferenceYear").fill("2025")
        page.locator("#libraryReferenceDoi").fill(reference_doi)
        page.locator("#referenceCreateForm button[type=submit]").click()
        page.get_by_text(reference_title, exact=True).wait_for()
        assert "原文对照不可用" in page.locator("#referenceLibraryList").inner_text()
        page.locator("#libraryReferenceTitle").fill(reference_title)
        page.locator("#libraryReferenceAuthors").fill("A. Author")
        page.locator("#libraryReferenceYear").fill("2025")
        page.locator("#libraryReferenceDoi").fill(reference_doi)
        page.locator("#libraryReferenceNotes").fill("保留合并笔记")
        page.locator("#referenceCreateForm button[type=submit]").click()
        page.locator("#referenceLibraryStatus button").first.wait_for()
        page.locator("#referenceLibraryStatus button").first.click()
        page.get_by_text("已合并记录；目标笔记和历史已保留。", exact=True).wait_for()
        assert page.locator(".reference-item").filter(has_text=reference_title).count() == 1

        page.locator(f"#projects .project-item[data-id='{created[1]}']").focus()
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#projectName')?.value === 'UI Project B'")
        page.locator(f"#projects .project-item[data-id='{created[1]}']").click()
        page.wait_for_function("document.querySelector('#projectName')?.value === 'UI Project B'")
        page.locator("#projectName").fill("Unsaved title")
        page.locator(f"#projects .project-item[data-id='{created[0]}']").click()
        page.locator("#projectSwitchDialog").wait_for(state="visible")
        page.get_by_role("button", name="放弃本页更改并切换").click()
        page.wait_for_function("document.querySelector('#projectName')?.value === 'UI Project A'")

        page.locator("#projectSearchScope").select_option("current")
        page.locator("#projectSearch").fill("UI Project B")
        page.wait_for_timeout(250)
        assert "没有匹配的论文" in page.locator("#projects").inner_text()
        page.locator("#projectSearchScope").select_option("global")
        page.locator(f"#projects .project-item[data-id='{created[1]}']").wait_for()
        page.locator("#projectSearch").fill("")
        page.locator("#projectSearchScope").select_option("current")
        page.wait_for_timeout(250)

        page.locator("#workflowControls").get_by_role("button", name="进入人工修订阶段").click()
        page.locator("#workflowModeControls").get_by_role("button", name="进入人工修订模式").wait_for()
        page.locator("#workflowModeControls").get_by_role("button", name="进入人工修订模式").click()
        page.wait_for_function("!document.querySelector('#workflowAssistControls').hidden")
        page.locator("#workflowAssistPrompt").fill("检查本次修改所需上下文")
        page.locator("#workflowAssistGenerate").click()
        page.wait_for_function("document.querySelector('#workflowContext').open")
        assert "模型" in page.locator("#workflowContextStatus").inner_text()
        assert "来源版本" in page.locator("#workflowSources").inner_text()

        desktop = screenshot_root / "desktop.png"
        mobile = screenshot_root / "mobile.png"
        page.screenshot(path=str(desktop), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(150)
        assert page.locator("#logicListView").get_attribute("aria-pressed") == "true"
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not overflow, "mobile viewport has horizontal overflow"
        page.screenshot(path=str(mobile), full_page=True)
        assert not errors, f"browser page errors: {errors}"
        (screenshot_root / "result.json").write_text(
            json.dumps({
                "result": "passed", "desktop_screenshot": str(desktop),
                "mobile_screenshot": str(mobile), "mobile_width": page.evaluate("window.innerWidth"),
                "browser_errors": errors, "dialogs": dialogs,
            }, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()


if __name__ == "__main__":
    main()
