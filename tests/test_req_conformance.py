from __future__ import annotations

import json
from pathlib import Path

import pytest

import app
from paperpilot.services import requirement_conformance as conformance


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload) -> None:
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def make_project(root: Path, *, with_tests: bool = True) -> Path:
    requirements = [
        {
            "id": "REQ-900",
            "title": "示例需求",
            "version": 1,
            "status": "confirmed",
            "readiness": "ready_for_acceptance",
            "priority": "must",
            "module_id": "MOD-DEMO",
            "human_verification": "verified_for_current_round",
            "confirmation_lock": {"status": "locked"},
            "ai_confirmation": {"status": "complete"},
            "code_mapping": [
                {"module": "MOD-DEMO", "path": "app.py; static/demo.js", "entry": "POST /api/demo; /api/demo/state", "tests": "tests/test_demo.py"}
            ],
            "acceptance_scenarios": [
                {"id": "AC-900-01", "given": "条件一", "when": "动作一", "then": "结果一", "verification": "自动测试"},
                {"id": "AC-900-02", "given": "条件二", "when": "动作二", "then": "结果二", "verification": "自动测试"},
            ],
        },
        {
            "id": "REQ-901",
            "title": "被替代需求",
            "version": 1,
            "status": "superseded",
            "readiness": "ready_for_design",
            "priority": "should",
            "human_verification": "unverified",
            "code_mapping": [],
            "acceptance_scenarios": [{"id": "AC-901-01", "given": "g", "when": "w", "then": "t", "verification": ""}],
        },
    ]
    evidence = [
        {"id": "EVD-900", "kind": "test", "related_ids": ["REQ-900"], "status": "passed", "method": "pytest", "result": "1 passed", "location": "tests/test_demo.py"},
        {"id": "EVD-901", "kind": "test", "related_ids": ["REQ-901"], "status": "failed", "method": "pytest", "result": "1 failed", "location": "tests/test_demo.py"},
    ]
    _write_json(
        root / conformance.LEDGER_RELATIVE,
        {"schema_version": 2, "requirements": requirements, "evidence": evidence, "work_packages": []},
    )
    package = {
        "requirement_id": "REQ-900",
        "requirement_version": 1,
        "traceability": [
            {
                "ac_id": "AC-900-01",
                "neighboring_regression_nodes": ["tests/test_demo.py::test_demo_flow"],
                "browser_entry_points": ["tests/test_demo.py"],
                "execution_command": "python -m pytest tests/test_demo.py -q",
            },
            {
                "ac_id": "AC-900-02",
                "neighboring_regression_nodes": ["tests/test_demo.py::test_missing_node"],
                "browser_entry_points": [],
                "execution_command": "python -m pytest tests/test_demo.py -q",
            },
        ],
    }
    _write_json(root / conformance.PACKAGE_ROOT_RELATIVE / "REQ-900" / "v1" / "ENGINEERING-PACKAGE.json", package)
    _write(
        root / "app.py",
        '@app.get("/api/health")\ndef health():\n    return {}\n\n@app.post("/api/demo")\ndef demo():\n    return {}\n',
    )
    _write(root / "static" / "demo.js", "// demo\n")
    if with_tests:
        _write(root / "tests" / "test_demo.py", "def test_demo_flow():\n    assert True\n")
    else:
        (root / "tests").mkdir(parents=True, exist_ok=True)
    return root


def test_scan_reports_missing_node_and_missing_route(tmp_path):
    root = make_project(tmp_path)
    matrix = conformance.scan(root)
    assert (root / conformance.OUTPUT_RELATIVE / "matrix.json").is_file()
    assert (root / conformance.OUTPUT_RELATIVE / "REPORT.md").is_file()
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-900")
    codes = {check["code"] for check in req["checks"]}
    assert "REQ_ROUTE_MISSING" in codes  # /api/demo/state 未声明
    ac2 = next(item for item in req["acceptance_criteria"] if item["ac_id"] == "AC-900-02")
    assert ac2["auto_verdict"] == "unsupported"
    assert {check["code"] for check in ac2["checks"]} == {"AC_TEST_NODE_MISSING"}


def test_missing_test_file_marks_ac_unsupported(tmp_path):
    root = make_project(tmp_path, with_tests=False)
    matrix = conformance.scan(root)
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-900")
    ac1 = next(item for item in req["acceptance_criteria"] if item["ac_id"] == "AC-900-01")
    assert ac1["auto_verdict"] == "unsupported"
    assert "AC_TEST_FILE_MISSING" in {check["code"] for check in ac1["checks"]}


def test_judgement_survives_rescan_and_reports_buckets(tmp_path):
    root = make_project(tmp_path)
    conformance.scan(root)
    conformance.save_judgement(
        root,
        target="REQ-900::AC-900-02",
        verdict="wrong",
        cause="implementation_gap",
        observed="点击后没有反应",
        note="复现于首次打开",
        evidence="tests/test_demo.py::test_missing_node",
    )
    matrix = conformance.scan(root)
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-900")
    ac2 = next(item for item in req["acceptance_criteria"] if item["ac_id"] == "AC-900-02")
    assert ac2["verdict"] == "wrong"
    assert matrix["summary"]["ac_verdicts"]["wrong"] == 1
    assert matrix["summary"]["root_causes"]["implementation_gap"] == 1
    report = conformance.render_report(matrix)
    assert "错误与偏差清单" in report and "点击后没有反应" in report
    assert "逐条对照明细" in report


def test_requirement_level_judgement_does_not_inflate_ac_counts(tmp_path):
    root = make_project(tmp_path)
    conformance.save_judgement(root, target="REQ-900::*", verdict="partial", cause="design_drift", note="整体形态偏离")
    matrix = conformance.build_matrix(root)
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-900")
    assert req["verdict"] == "partial"
    # A whole-requirement verdict describes the requirement, not each AC.
    assert all(ac["verdict"] == "unverified" for ac in req["acceptance_criteria"])
    assert matrix["summary"]["ac_verdicts"]["partial"] == 0


def test_superseded_requirement_is_not_scored_as_unsupported(tmp_path):
    root = make_project(tmp_path)
    matrix = conformance.scan(root)
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-901")
    assert req["auto_verdict"] == "unverified"
    assert any(check["code"] == "REQ_SUPERSEDED" for check in req["checks"])


def test_invalid_judgement_is_rejected(tmp_path):
    root = make_project(tmp_path)
    with pytest.raises(ValueError):
        conformance.save_judgement(root, target="REQ-900::AC-900-01", verdict="maybe")


def test_api_matrix_judgement_and_report(tmp_path, client, monkeypatch):
    root = make_project(tmp_path / "fixture-root")
    monkeypatch.setattr(app, "ROOT", root)

    response = client.get("/api/conformance")
    assert response.status_code == 200, response.text
    matrix = response.json()
    assert matrix["summary"]["requirements"] == 2

    catalog = client.get("/api/conformance/catalog").json()
    assert "wrong" in catalog["verdicts"]

    saved = client.post(
        "/api/conformance/judgements",
        json={
            "target": "REQ-900::AC-900-01",
            "verdict": "correct",
            "cause": "unknown",
            "observed": "行为符合",
            "note": "已核对",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["verdict"] == "correct"

    matrix = client.get("/api/conformance").json()
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-900")
    assert req["acceptance_criteria"][0]["verdict"] == "correct"

    report = client.get("/api/conformance/report")
    assert report.status_code == 200
    assert "需求对照报告" in report.text

    cleared = client.request("DELETE", "/api/conformance/judgements", params={"target": "REQ-900::AC-900-01"})
    assert cleared.status_code == 200
    matrix = client.get("/api/conformance").json()
    req = next(item for item in matrix["requirements"] if item["requirement_id"] == "REQ-900")
    assert req["acceptance_criteria"][0]["verdict"] == "unverified"

    bad = client.post("/api/conformance/judgements", json={"target": "REQ-900::AC-900-01", "verdict": "maybe"})
    assert bad.status_code == 400


def test_page_is_served(client):
    response = client.get("/conformance")
    assert response.status_code == 200
    assert "需求对照" in response.text
    assert '/static/product-shape-review.html' in response.text

    shape = client.get("/static/product-shape-review.html")
    assert shape.status_code == 200
    assert "讨论稿 / 未获用户确认" in shape.text
    assert 'data-view="history"' in shape.text
    assert "等待评审的产品取舍" in shape.text
