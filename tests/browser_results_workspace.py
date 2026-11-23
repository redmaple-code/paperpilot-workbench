"""Browser check for importing, deriving, charting and reopening an exact result."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 980})
        api = context.request
        first = api.post(args.base_url + "/api/projects", data={"name": "结果联动验证", "domain": "数值试验"}).json()
        second = api.post(args.base_url + "/api/projects", data={"name": "隔离论文", "domain": "数值试验"}).json()
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.base_url + "/?view=module")
        page.wait_for_function("window.paperpilot?.data?.project")
        page.evaluate("(id) => paperpilot.loadProject(id)", first["project"]["id"])
        page.locator("#openPaperTools").click()
        page.locator('[data-tool-id="results"]').get_by_role("button", name="打开", exact=True).click()
        expect(page.locator("#resultEmpty")).to_be_visible()

        def upload(trigger, filename, content):
            page.locator(trigger).click()
            page.locator("#resultFile").set_input_files({"name": filename, "mimeType": "text/csv", "buffer": content})
            page.locator("#saveResultUpload").click()

        upload("#newResultDataset", "convergence.csv", b"x,loss,run\n0,2,a\n1,1,a\n1,1,a\n2,0.5,b\n")
        expect(page.locator("#resultStatus")).to_contain_text("v1")
        expect(page.locator("#resultTable tbody tr")).to_have_count(4)
        original_id = page.locator("#resultVersionSelect").input_value()

        page.locator("#resultProcessPanel summary").click()
        page.locator("#resultOperation").select_option("deduplicate")
        page.locator("#resultProcessNote").fill("去掉重复采样")
        page.get_by_role("button", name="处理并保存为新版本").click()
        expect(page.locator("#resultStatus")).to_contain_text("v2")
        expect(page.locator("#resultTable tbody tr")).to_have_count(3)
        processed_id = page.locator("#resultVersionSelect").input_value()

        upload("#appendResultVersion", "new.csv", b"x,loss,run\n0,8,a\n1,4,a\n")
        expect(page.locator("#resultStatus")).to_contain_text("v3")
        page.locator("#resultVersionSelect").select_option(processed_id)
        expect(page.locator("#resultTable tbody tr")).to_have_count(3)
        expect(page.locator("#resultProvenance")).to_contain_text("v1")
        page.locator("#resultChartPanel summary").click()
        page.locator("#resultChartTitle").fill("Loss by step")
        page.get_by_role("button", name="生成图表预览").click()
        expect(page.locator("#resultChartPreview")).to_be_visible()
        page.wait_for_function("document.querySelector('#resultChartImage').naturalWidth > 0")
        expect(page.locator("#resultChartCaption")).to_contain_text("v2")
        page.locator("#expandPaperArtifact").click()
        page.locator("#resultChartPreview").screenshot(path=str(args.output / "results-chart.png"))
        page.locator("#newResultDataset").scroll_into_view_if_needed()
        page.screenshot(path=str(args.output / "results-desktop.png"))
        page.locator("#saveResultChart").click()

        expect(page.locator("#openVisualResultSource")).to_be_visible()
        asset_preview = page.locator("#visualMainPreview").get_attribute("src")
        assert asset_preview and api.get(args.base_url + asset_preview).ok
        page.locator("#openVisualResultSource").click()
        expect(page.locator("#resultVersionSelect")).to_have_value(processed_id)
        expect(page.locator("#resultLinkedImages")).to_be_visible()
        page.locator("#resultVersionSelect").select_option(original_id)
        expect(page.locator("#resultTable tbody tr")).to_have_count(4)
        expect(page.locator("#resultLinkedImages")).to_be_hidden()
        page.locator("#resultVersionSelect").select_option(processed_id)
        page.get_by_role("button", name="Loss by step · 图像 v1").click()
        expect(page.locator("#visualMainPreview")).to_have_attribute("src", asset_preview)
        page.locator("#openVisualResultSource").click()
        expect(page.locator("#resultVersionSelect")).to_have_value(processed_id)

        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#resultTable")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page.locator("#resultsWorkspace").bounding_box()["width"] > 270
        page.locator("#newResultDataset").scroll_into_view_if_needed()
        page.screenshot(path=str(args.output / "results-mobile.png"))

        page.evaluate("(id) => paperpilot.loadProject(id)", second["project"]["id"])
        page.evaluate("paperpilot.openWorkspace('visual/results')")
        expect(page.locator("#resultEmpty")).to_be_visible()
        expect(page.locator("#resultTable td")).to_have_count(0)
        assert not errors, errors
        print("Desktop and 390px mobile result import, process, chart, historical links and project isolation passed.")
        browser.close()


if __name__ == "__main__":
    main()
