from __future__ import annotations

import io
import json
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/".project-to-act/docs/project-development/evidence/full-product-20261101"

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={"width":1440,"height":1100})
    pid=context.request.post("http://127.0.0.1:8774/api/projects",data={"name":"视觉报告论文","domain":"材料","style":"严谨"}).json()["project"]["id"]
    page=context.new_page()
    errors=[]
    page.on("pageerror",lambda e:errors.append(str(e)))
    page.goto("http://127.0.0.1:8774")
    page.wait_for_load_state("networkidle")
    page.locator('[data-tab="visual"]').click()
    stream=io.BytesIO();Image.new("RGB",(800,500),"blue").save(stream,"PNG")
    page.locator("#visualAssetFile").set_input_files({"name":"reference.png","mimeType":"image/png","buffer":stream.getvalue()})
    page.locator("#visualAssetTitle").fill("参考图页")
    page.locator("#importVisualAsset").click()
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("已导入")')
    page.locator("#refreshVisualReports").click()
    page.wait_for_function('document.querySelector("#evidenceAsset").options.length===2')
    page.locator("#runVisualEvidence").click()
    page.wait_for_function('document.querySelector("#visualEvidenceStatus").textContent.includes("报告已生成")')
    assert "未授权云端" in page.locator("#visualReportDetail").inner_text()
    assert "信息不足" in page.locator("#visualReportDetail").inner_text()
    original=page.locator(".visual-finding-list").inner_text()
    page.locator("#visualReviewer").fill("作者")
    page.locator("#visualReviewDecision").select_option("reject")
    page.locator("#visualReviewReason").fill("缺少对应实验数据，暂不认可科学含义")
    page.locator("#visualReviewRefs").fill("实验记录，第2页")
    page.locator("#saveVisualReview").click()
    page.wait_for_function('document.querySelector("#visualEvidenceStatus").textContent.includes("人工决定已追加")')
    assert "作者驳回" in page.locator(".visual-review").inner_text()
    assert original==page.locator(".visual-finding-list").inner_text()
    page.locator("#extractVisualStyle").click()
    page.wait_for_function('document.querySelector("#visualEvidenceStatus").textContent.includes("需要可定位")')
    assert not page.locator("#approveVisualColor").count()
    # Reference region extraction creates a proposal with a separate approval action.
    page.get_by_text("参考证据与定位（可选）",exact=True).click()
    page.locator("#evidenceSourceAsset").select_option(index=1)
    page.locator("#runVisualEvidence").click()
    page.wait_for_function('document.querySelector("#visualEvidenceStatus").textContent.includes("报告已生成")')
    page.locator("#extractVisualStyle").click()
    page.locator("#approveVisualColor").wait_for()
    assert "#0000ff" in page.locator("#visualStyleCandidate").inner_text()
    rules=context.request.get(f"http://127.0.0.1:8774/api/projects/{pid}/drawing/style-rules").json()["rules"]
    assert rules[-1]["status"]=="candidate"
    # A file change makes the report and reviews historical, with no automatic acceptance.
    stream=io.BytesIO();Image.new("RGB",(800,500),"green").save(stream,"PNG")
    page.locator("#visualReplacementFile").set_input_files({"name":"updated.png","mimeType":"image/png","buffer":stream.getvalue()})
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("新版本已保存")')
    page.locator("#refreshVisualReports").click()
    page.wait_for_function('document.querySelector("#visualReportDetail").textContent.includes("此报告和人工决定已过期")')
    assert page.locator("#saveVisualReview").is_disabled()
    page.locator(".visual-evidence-studio").screenshot(path=str(OUTPUT/"REQ-024-desktop.png"))
    page.set_viewport_size({"width":390,"height":844})
    assert page.locator(".visual-evidence-studio").bounding_box()["width"]>=300
    assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
    page.locator(".visual-evidence-studio").screenshot(path=str(OUTPUT/"REQ-024-mobile.png"))
    assert not errors,errors
    (OUTPUT/"REQ-024-BROWSER.json").write_text(json.dumps({"status":"passed","external_calls":0,"pageerrors":errors,"checks":["local-checks","missing-evidence-unknown","append-only-human-review","located-color-candidate-not-published","asset-change-stale","desktop","390px"]},ensure_ascii=False,indent=2),encoding="utf-8")
    context.close();browser.close()
