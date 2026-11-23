"""Exercise the standalone outline and the conversation tool that opens it."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def run(base_url, output):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-proxy-server"])
        page = browser.new_page(viewport={"width": 1560, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        created = page.request.post(f"{base_url}/api/projects", data={"name": "独立大纲浏览器验证"})
        assert created.ok, created.text()
        project = created.json()["project"]["id"]
        api = f"{base_url}/api/projects/{project}/outline"
        url = f"{base_url}/outline?project={project}"

        page.goto(url + "#outline/formal")
        expect(page.locator("#ocEmpty")).to_have_text("还没有章节节点")
        expect(page.locator("#workspaceAssistant")).to_be_hidden()
        assert page.locator("#paperArtifact").count() == 0
        page.get_by_role("button", name="论文 Agent", exact=True).click()
        expect(page.locator("#workspaceAssistant")).to_be_visible()
        page.locator("#closeWorkspaceAssistant").click()
        expect(page.locator("#workspaceAssistant")).to_be_hidden()

        page.locator('#outline [data-task="text"]').click()
        page.locator("#outlineMigrationPreview").click()
        expect(page.locator("#outlineMigrationResult .outline-tree-node")).to_have_count(5)
        page.locator("#outlineMigrationCommit").click()
        expect(page.locator("#outlineStructuredStatus")).to_have_text("结构化大纲已确认")
        page.locator('#outline [data-task="formal"]').click()
        directory = page.locator("#outlineTree .outline-node-select")
        expect(directory).to_have_count(5)
        expect(page.locator("#ocZoomReset")).to_have_text("100%")
        parent = directory.first.get_attribute("data-node-id")
        page.locator("#ocAdd").click()
        expect(page.locator("#outlineNodeTitle")).to_be_focused()
        page.locator("#outlineNodeTitle").fill("章节画布同步验证")
        page.locator("#outlineNodeParent").select_option(parent)
        page.locator("#outlineNodeCreate").click()
        expect(page.locator("#ocBoard")).to_contain_text("章节画布同步验证")
        child = directory.filter(has_text="章节画布同步验证")
        child.focus()
        child.press("Enter")
        expect(child).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#outlineDetailTitle")).to_have_value("章节画布同步验证")
        version = page.request.get(f"{api}/structured").json()["version"]
        assert page.evaluate("window.__outlineGraph.getEdges().length") == 1
        page.locator("#ocAuto").click()
        assert page.request.get(f"{api}/structured").json()["version"] == version

        child_id = child.get_attribute("data-node-id")
        child_cell = page.locator(f'#ocBoard .x6-node[data-cell-id="{child_id}"]')
        rect = child_cell.locator("rect").first.bounding_box()
        page.mouse.move(rect["x"] + 80, rect["y"] + 34)
        page.mouse.down()
        page.mouse.move(rect["x"] + 115, rect["y"] + 53, steps=10)
        page.mouse.up()
        expect(page.locator("#ocStatus")).to_have_text("位置已保存")
        moved = page.request.get(f"{api}/structured").json()
        assert moved["version"] == version + 1
        assert next(node for node in moved["nodes"] if node["id"] == child_id)["canvas_x"] > 300

        new_parent = directory.filter(has_text="Related Work").get_attribute("data-node-id")
        source_port = page.locator(f'#ocBoard .x6-node[data-cell-id="{new_parent}"] circle[port-group="out"]')
        source = source_port.bounding_box()
        target = child_cell.locator('circle[port-group="in"]').bounding_box()
        page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
        page.mouse.down()
        page.mouse.move(target["x"] + target["width"] / 2, target["y"] + target["height"] / 2, steps=14)
        page.mouse.up()
        expect(page.locator("#ocStatus")).to_have_text("已设置父节点")
        linked = page.request.get(f"{api}/structured").json()
        assert linked["version"] == version + 2
        assert next(node for node in linked["nodes"] if node["id"] == child_id)["parent_id"] == new_parent

        page.reload()
        expect(directory).to_have_count(6)
        expect(page.locator("#ocZoomReset")).to_have_text("100%")
        page.screenshot(path=output / "desktop-outline.png")

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("window.__outlineGraph.getGraphArea().width === document.querySelector('#ocBoard').clientWidth")
        assert page.locator("#ocBoard").bounding_box()["width"] <= 390
        assert page.evaluate("document.querySelector('#workspaceMain').scrollWidth <= document.querySelector('#workspaceMain').clientWidth")

        page.locator('#outline [data-task="text"]').click()
        page.locator("#outlineText").fill("立即离开前的大纲草稿")
        page.locator("#outlineBackToPaper").click()
        page.wait_for_url(f"**/?project={project}#chat")
        drafts = page.request.get(f"{api}/draft-recovery?kind=editor").json()["drafts"]
        assert any(draft["preview"] == "立即离开前的大纲草稿" for draft in drafts)
        page.locator("#openPaperTools").click()
        page.locator("#paperToolSearch").fill("章节大纲")
        page.locator('[data-tool-id="outline"]').get_by_role("button", name="打开", exact=True).click()
        page.wait_for_url(f"**/outline?project={project}#outline/nodes")
        expect(page.get_by_role("button", name="从创意生成大纲")).to_be_visible()
        page.locator('#outline [data-task="formal"]').click()
        expect(directory).to_have_count(6)
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    run(options.base_url, options.output)
    print("Standalone outline and conversation tool entry passed.")
