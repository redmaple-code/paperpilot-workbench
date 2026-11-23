import hashlib
import json
from pathlib import Path

import pytest

import app
from paperpilot.services import product_form as forms
from paperpilot.services import requirement_conformance as conformance


REQ = "REQ-001"
FORM_ID = "PF-REQ-001"
AC_IDS = ["AC-001-01", "AC-001-02"]


def _valid_form():
    return {
        "form_id": FORM_ID,
        "title": "Work area",
        "version": 3,
        "status": "draft",
        "requirement_ids": [REQ],
        "confirmation": {"kind": "none", "source_refs": [], "confirmed_at": "", "note": ""},
        "journeys": [{"id": "J-001-01", "actor": "Author", "trigger": "Needs work", "steps": ["Open", "Edit", "Review"], "success": "Result appears", "screens": ["S-001-01"]}],
        "screens": [{"id": "S-001-01", "name": "Work area", "location": "Workspace > Editor", "states": ["empty", "ready", "error"], "elements": ["Editor", "Status"], "actions": ["Edit", "Save"]}],
        "seams": [],
        "usage": ["Open workspace", "Edit content", "Review saved result"],
        "non_goals": ["No unrelated capabilities"],
        "acceptance_mapping": [{"journey_id": "J-001-01", "ac_ids": AC_IDS}],
        "source_refs": [],
        "ledger_version": 3,
    }


def _write_fixture(root: Path, source=None):
    (root / forms.FORMS_RELATIVE / FORM_ID).mkdir(parents=True)
    (root / forms.CHAIN_RELATIVE).mkdir(parents=True)
    requirement = {"id": REQ, "version": 3, "status": "active", "readiness": "draft", "title": "Requirement", "acceptance_scenarios": [{"id": item} for item in AC_IDS], "code_mapping": []}
    ledger = {"requirements": [requirement], "sources": [source] if source else [], "evidence": []}
    (root / conformance.LEDGER_RELATIVE).parent.mkdir(parents=True, exist_ok=True)
    (root / conformance.LEDGER_RELATIVE).write_text(json.dumps(ledger), encoding="utf-8")
    form = _valid_form()
    directory = root / forms.FORMS_RELATIVE / FORM_ID
    (directory / forms.JSON_NAME).write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / forms.MD_NAME).write_text(forms.render_form_markdown(form).replace("- TODO 说明失败时用户看到什么、原状态是否保留、如何重试。", "- 保留原状态并允许重试。"), encoding="utf-8")
    return form, directory


def _digest(directory):
    return forms._form_digest(directory)


def _source(digest, title="用户明确确认 PF-REQ-001 v3", kind="user_confirmation"):
    return {"id": "SRC-900", "kind": kind, "title": title, "locator": "test conversation message",
            "product_form_approval": {"decision": "approved", "actor": "user", "form_id": FORM_ID,
                                      "version": 3, "content_sha256": digest}}


def test_confirm_requires_explicit_sha_and_bound_source(tmp_path):
    form, directory = _write_fixture(tmp_path)
    digest = _digest(directory)
    ledger_path = tmp_path / conformance.LEDGER_RELATIVE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["sources"] = [_source(digest)]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    confirmed = forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)

    assert confirmed["status"] == "confirmed"
    assert (directory / forms.MD_NAME).read_text(encoding="utf-8").endswith("\n")
    receipt = json.loads((directory / forms.RECEIPT_NAME).read_text(encoding="utf-8"))
    assert receipt["content_sha256"] == digest
    assert receipt["version"] == form["version"]
    assert forms.validate_form(tmp_path, {**confirmed, "_md_text": (directory / forms.MD_NAME).read_text(encoding="utf-8"), "_md_exists": True}, {"SRC-900"}) == []


def test_api_confirmation_rejects_unapproved_source_without_receipt(tmp_path, client, monkeypatch):
    _, directory = _write_fixture(tmp_path, _source("0" * 64, kind="implementation"))
    monkeypatch.setattr(app, "ROOT", tmp_path)
    response = client.post("/api/conformance/forms/confirm", json={
        "form_id": FORM_ID, "source_refs": ["SRC-900"], "expected_sha256": _digest(directory),
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "form_not_ready"
    assert not (directory / forms.RECEIPT_NAME).exists()
    chain = client.get("/api/conformance/chain")
    assert chain.status_code == 200
    item = chain.json()["requirements"][0]
    assert item["form_status"] == "draft"
    assert not item["stages"]["product_confirmed"]


def test_confirm_rejects_missing_digest_arbitrary_source_and_stale_digest(tmp_path):
    _, directory = _write_fixture(tmp_path, _source("0" * 64, title="unrelated source", kind="implementation"))
    with pytest.raises(ValueError, match="SHA-256"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"])
    with pytest.raises(ValueError, match="摘要"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="未明确记录"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=_digest(directory))


def test_edited_markdown_invalidates_confirmed_digest(tmp_path):
    _, directory = _write_fixture(tmp_path)
    digest = _digest(directory)
    ledger_path = tmp_path / conformance.LEDGER_RELATIVE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["sources"] = [_source(digest)]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    confirmed = forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)
    with (directory / forms.MD_NAME).open("a", encoding="utf-8") as stream:
        stream.write("\nUser edit\n")
    issues = forms.validate_form(tmp_path, {**confirmed, "_md_text": (directory / forms.MD_NAME).read_text(encoding="utf-8"), "_md_exists": True}, {"SRC-900"})
    assert "FORM_CONFIRMED_STALE" in {item["code"] for item in issues}
    with pytest.raises(ValueError, match="不可重复"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)


def test_scaffold_never_overwrites_even_force(tmp_path):
    _write_fixture(tmp_path)
    target = tmp_path / forms.FORMS_RELATIVE / FORM_ID / forms.MD_NAME
    target.write_text("human edits", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持强制覆盖"):
        forms.scaffold(tmp_path, REQ, force=True)
    with pytest.raises(FileExistsError):
        forms.scaffold(tmp_path, REQ)
    assert target.read_text(encoding="utf-8") == "human edits"


@pytest.mark.parametrize("value", ["TODO", "prefix TODO suffix", "待确认", "word TBD here"])
def test_todo_detection_in_nested_values(value):
    assert forms._contains_todo({"items": [{"text": value}]})


def test_validation_requires_exact_ac_mapping_and_valid_ids(tmp_path):
    form, directory = _write_fixture(tmp_path)
    form["journeys"][0]["id"] = "journey"
    form["screens"][0]["id"] = "screen"
    form["acceptance_mapping"][0]["ac_ids"] = [AC_IDS[0], "bad-id"]
    (directory / forms.JSON_NAME).write_text(json.dumps(form), encoding="utf-8")
    issues = forms.validate_form(tmp_path, {**form, "_md_text": "", "_md_exists": False}, set())
    codes = {item["code"] for item in issues}
    assert {"FORM_JOURNEY_NO_ID", "FORM_SCREEN_NO_ID", "FORM_MAPPING_BAD_AC", "FORM_MAPPING_INCOMPLETE"} <= codes


def test_package_binding_requires_confirmed_exact_version_and_digest(tmp_path):
    _, directory = _write_fixture(tmp_path)
    digest = _digest(directory)
    ledger_path = tmp_path / conformance.LEDGER_RELATIVE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["sources"] = [_source(digest)]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)
    package = forms.bind_package(tmp_path, REQ, 3, {})
    assert package["product_form_ref"] == {"form_id": FORM_ID, "version": 3, "content_sha256": digest}
    with pytest.raises(ValueError, match="版本已过期"):
        forms.bind_package(tmp_path, REQ, 2, {})
    with (directory / forms.MD_NAME).open("a", encoding="utf-8") as stream:
        stream.write("edited")
    with pytest.raises(ValueError, match="没有唯一且有效|已失效"):
        forms.bind_package(tmp_path, REQ, 3, {})


@pytest.mark.parametrize("field,value", [("journeys", "bad"), ("screens", [None]),
                                         ("acceptance_mapping", ["bad"]), ("confirmation", "bad"),
                                         ("journeys", [{"id": "J-001-01", "steps": 12}]),
                                         ("screens", [{"id": "S-001-01", "states": "ready"}]),
                                         ("acceptance_mapping", [{"journey_id": "J-001-01", "ac_ids": 3}])])
def test_invalid_form_structure_is_reported_without_crash(tmp_path, field, value):
    form, directory = _write_fixture(tmp_path)
    form[field] = value
    (directory / forms.JSON_NAME).write_text(json.dumps(form), encoding="utf-8")
    issues = forms.validate_form(tmp_path, {**form, "_md_text": "", "_md_exists": False}, set())
    assert "FORM_BAD_STRUCTURE" in {item["code"] for item in issues}


@pytest.mark.parametrize("payload", [[], "invalid", {"requirement_ids": "REQ-001"},
                                      {"requirement_ids": [REQ], "acceptance_mapping": [None]},
                                      {"requirement_ids": [REQ], "acceptance_mapping": [{"ac_ids": 7}]}])
def test_chain_reports_malformed_form_without_crashing(tmp_path, payload):
    _, directory = _write_fixture(tmp_path)
    (directory / forms.JSON_NAME).write_text(json.dumps(payload), encoding="utf-8")
    chain = forms.build_chain(tmp_path)
    item = chain["requirements"][0]
    assert item["form_status"] == "none" or not item["stages"]["product_confirmed"]
    assert any(issue["severity"] == "error" for issue in item["form_issues"])


@pytest.mark.parametrize("payload", [[], "invalid", {"requirement_ids": "REQ-001"}])
def test_confirm_rejects_malformed_form_without_receipt(tmp_path, payload):
    _, directory = _write_fixture(tmp_path, _source("0" * 64))
    (directory / forms.JSON_NAME).write_text(json.dumps(payload), encoding="utf-8")
    digest = _digest(directory) if isinstance(payload, dict) else "0" * 64
    with pytest.raises(ValueError, match="根节点|需求 ID"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)
    assert not (directory / forms.RECEIPT_NAME).exists()


def test_confirm_does_not_overwrite_existing_receipt(tmp_path):
    _, directory = _write_fixture(tmp_path, _source("0" * 64))
    digest = _digest(directory)
    (directory / forms.RECEIPT_NAME).write_text("old receipt", encoding="utf-8")
    with pytest.raises(ValueError, match="已有确认回执"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)
    assert (directory / forms.RECEIPT_NAME).read_text(encoding="utf-8") == "old receipt"


def test_chain_reports_malformed_receipt_without_crashing(tmp_path):
    _, directory = _write_fixture(tmp_path)
    digest = _digest(directory)
    ledger_path = tmp_path / conformance.LEDGER_RELATIVE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["sources"] = [_source(digest)]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)
    (directory / forms.RECEIPT_NAME).write_text("invalid json", encoding="utf-8")
    item = forms.build_chain(tmp_path)["requirements"][0]
    assert not item["stages"]["product_confirmed"]
    assert not item["product_acceptance"]
    assert any(issue["severity"] == "error" for issue in item["form_issues"])


def test_duplicate_acceptance_mapping_blocks_confirmation(tmp_path):
    form, directory = _write_fixture(tmp_path)
    form["acceptance_mapping"].append({"journey_id": "J-001-01", "ac_ids": [AC_IDS[0]]})
    (directory / forms.JSON_NAME).write_text(json.dumps(form), encoding="utf-8")
    digest = _digest(directory)
    ledger_path = tmp_path / conformance.LEDGER_RELATIVE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["sources"] = [_source(digest)]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(ValueError, match="重复覆盖"):
        forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)


def test_invalid_confirmation_receipt_is_blocking_issue(tmp_path):
    _, directory = _write_fixture(tmp_path)
    digest = _digest(directory)
    ledger_path = tmp_path / conformance.LEDGER_RELATIVE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["sources"] = [_source(digest)]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    confirmed = forms.confirm(tmp_path, FORM_ID, ["SRC-900"], expected_sha256=digest)
    (directory / forms.RECEIPT_NAME).write_text("invalid json", encoding="utf-8")
    issues = forms.validate_form(tmp_path, {**confirmed, "_md_text": (directory / forms.MD_NAME).read_text(encoding="utf-8"), "_md_exists": True}, {"SRC-900"})
    assert "FORM_CONFIRMED_NO_SOURCE" in {item["code"] for item in issues}
