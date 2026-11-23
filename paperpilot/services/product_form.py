"""Product form layer: the missing link between a confirmed requirement and code.

Chain contract
--------------
    function (REQ + acceptance criteria)
        -> product (product form: journeys, screens, usage, seams)   <- this module
            -> code (engineering package, code mapping, tests, evidence)

A product form is the thing a person can actually confirm: "this is what I will
see, where I find it, what I do, and what happens when it fails".  An
engineering package alone cannot be confirmed by a user - it describes internal
surfaces.  This module scaffolds, validates and gates that layer without ever
inventing a confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paperpilot.services.requirement_conformance import build_index, latest_packages, load_ledger

FORMS_RELATIVE = Path(".project-to-act/docs/project-development/product-forms")
CHAIN_RELATIVE = Path(".project-to-act/docs/project-development/conformance")

JSON_NAME = "form.json"
MD_NAME = "PRODUCT-FORM.md"
RECEIPT_NAME = "confirmation.json"
FORM_ID_RE = re.compile(r"^PF-REQ-\d{3}$")
REQ_ID_RE = re.compile(r"^REQ-\d{3}$")
AC_ID_RE = re.compile(r"^(?:REQ-\d{3}-AC-\d{2}|AC-REQ-\d{3}-\d{2}|AC-\d{3}-\d{2})$")
JOURNEY_ID_RE = re.compile(r"^J-\d{3}-\d{2}$")
SCREEN_ID_RE = re.compile(r"^S-\d{3}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

FORM_STATUSES = ("draft", "review", "confirmed", "superseded")

REQUIRED_SECTIONS = [
    "用户与场景",
    "主流程",
    "入口与位置",
    "界面与状态",
    "交互与反馈",
    "使用说明",
    "失败与恢复",
    "与其他功能的接缝",
    "非目标",
    "验收映射",
]

# Which requirement statuses must have a confirmed product form before build.
GATED_READINESS = {"ready_for_build", "ready_for_acceptance"}

CHAIN_STAGES = ["function", "product", "product_confirmed", "engineering_package", "package_bound", "code", "tests", "product_acceptance"]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# discovery


def discover_forms(root: Path) -> list[dict[str, Any]]:
    base = root / FORMS_RELATIVE
    forms: list[dict[str, Any]] = []
    if not base.is_dir():
        return forms
    for directory in sorted(base.iterdir()):
        if not directory.is_dir() or directory.name.startswith("_"):
            continue
        json_path = directory / JSON_NAME
        md_path = directory / MD_NAME
        if not json_path.is_file():
            continue
        try:
            body = _read_json(json_path)
            if not isinstance(body, dict):
                body = {"form_id": directory.name, "invalid_json": "JSON 根节点必须是对象"}
        except (OSError, json.JSONDecodeError) as exc:
            body = {"form_id": directory.name, "invalid_json": str(exc)}
        body["_dir"] = directory.name
        body.setdefault("form_id", directory.name)
        body["_json_path"] = json_path.relative_to(root).as_posix()
        body["_md_path"] = md_path.relative_to(root).as_posix()
        body["_md_exists"] = md_path.is_file()
        body["_md_text"] = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
        forms.append(body)
    return forms


def forms_by_requirement(root: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for form in discover_forms(root):
        req_ids = form.get("requirement_ids")
        if not isinstance(req_ids, list):
            req_ids = []
        if not req_ids and FORM_ID_RE.fullmatch(str(form.get("_dir") or "")):
            req_ids = [form["_dir"].removeprefix("PF-")]
        for req_id in req_ids:
            result.setdefault(str(req_id), []).append(form)
    return result


# --------------------------------------------------------------------------
# validation


def _section_bodies(markdown: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current is not None:
                bodies[current] = "\n".join(buffer).strip()
            current = match.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        bodies[current] = "\n".join(buffer).strip()
    return bodies


_TODO_RE = re.compile(r"(?:^|[\s：:（(\[【])(?:TODO|TBD|待补充|待填写|待改写|待确认)(?=$|[\s：:，,。；;）)\]】])", re.IGNORECASE)


def _contains_todo(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_todo(key) or _contains_todo(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_todo(item) for item in value)
    return isinstance(value, str) and bool(_TODO_RE.search(value))


def _is_placeholder(text: str) -> bool:
    stripped = text.strip()
    return not stripped or stripped in {"-", "—"} or _contains_todo(stripped)


def _form_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    json_path = directory / JSON_NAME
    md_path = directory / MD_NAME
    if not json_path.is_file() or not md_path.is_file():
        raise FileNotFoundError(f"表单必须同时包含 {JSON_NAME} 与 {MD_NAME}")
    payload = _read_json(json_path)
    if not isinstance(payload, dict):
        raise ValueError("表单 JSON 根节点必须是对象")
    # Workflow metadata changes during confirmation; the user-authored form content does not.
    content = {key: value for key, value in payload.items() if key not in {"status", "confirmation"}}
    digest.update(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(bytes([0]))
    digest.update(md_path.read_bytes())
    return digest.hexdigest()


def _valid_form_identity(form: dict[str, Any]) -> bool:
    return bool(
        FORM_ID_RE.fullmatch(str(form.get("form_id") or ""))
        and isinstance(form.get("version"), int)
        and not isinstance(form.get("version"), bool)
        and form["version"] >= 1
        and isinstance(form.get("requirement_ids"), list)
        and bool(form["requirement_ids"])
        and all(REQ_ID_RE.fullmatch(str(item)) for item in form["requirement_ids"])
    )


def _source_affirms_form(source: dict[str, Any], form: dict[str, Any], digest: str) -> bool:
    approval = source.get("product_form_approval")
    return bool(
        source.get("kind") in {"user_message", "user_confirmation"}
        and isinstance(source.get("locator"), str) and source["locator"].strip()
        and isinstance(approval, dict)
        and approval.get("decision") == "approved"
        and approval.get("actor") == "user"
        and approval.get("form_id") == form.get("form_id")
        and approval.get("version") == form.get("version")
        and approval.get("content_sha256") == digest
    )


def validate_form(root: Path, form: dict[str, Any], sources: set[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def issue(code: str, message: str, severity: str = "error") -> None:
        issues.append({"code": code, "message": message, "severity": severity})

    if form.get("invalid_json"):
        issue("FORM_BAD_JSON", f"{form['_json_path']} 不是合法 JSON：{form['invalid_json']}")
        return issues

    list_fields = ("requirement_ids", "journeys", "screens", "usage", "acceptance_mapping")
    for field in list_fields:
        value = form.get(field)
        if value is not None and not isinstance(value, list):
            issue("FORM_BAD_STRUCTURE", f"{field} 必须是数组")
            return issues
    for field in ("journeys", "screens", "acceptance_mapping"):
        if any(not isinstance(item, dict) for item in form.get(field) or []):
            issue("FORM_BAD_STRUCTURE", f"{field} 中的每项必须是对象")
            return issues
    for field in ("steps", "screens"):
        if any(not isinstance(item.get(field), list) for item in form.get("journeys") or [] if item.get(field) is not None):
            issue("FORM_BAD_STRUCTURE", f"journeys 中的 {field} 必须是数组")
            return issues
    for field in ("states", "elements", "actions"):
        if any(not isinstance(item.get(field), list) for item in form.get("screens") or [] if item.get(field) is not None):
            issue("FORM_BAD_STRUCTURE", f"screens 中的 {field} 必须是数组")
            return issues
    if any(not isinstance(item.get("ac_ids"), list) for item in form.get("acceptance_mapping") or [] if item.get("ac_ids") is not None):
        issue("FORM_BAD_STRUCTURE", "验收映射的 ac_ids 必须是数组")
        return issues
    if not isinstance(form.get("confirmation") or {}, dict):
        issue("FORM_BAD_STRUCTURE", "confirmation 必须是对象")
        return issues

    if form.get("status") not in FORM_STATUSES:
        issue("FORM_BAD_STATUS", f"status 必须是 {FORM_STATUSES} 之一，当前 {form.get('status')!r}")
    if not form.get("requirement_ids"):
        issue("FORM_NO_REQUIREMENT", "未声明覆盖的需求（requirement_ids）")
    elif any(not REQ_ID_RE.fullmatch(str(item)) for item in form.get("requirement_ids") or []):
        issue("FORM_BAD_REQUIREMENT_ID", "requirement_ids 含无效需求 ID")
    if not isinstance(form.get("version"), int) or isinstance(form.get("version"), bool) or form.get("version", 0) < 1:
        issue("FORM_BAD_VERSION", "version 必须是 >=1 的整数")
    if not FORM_ID_RE.fullmatch(str(form.get("form_id") or "")):
        issue("FORM_BAD_ID", "form_id 必须符合 PF-REQ-NNN")
    if _contains_todo(form):
        issue("FORM_TODO_JSON", "JSON 中仍有 TODO/TBD/待补充等占位内容")

    journeys = form.get("journeys") or []
    journey_ids = {str(item.get("id") or "") for item in journeys if isinstance(item, dict)}
    if not journeys:
        issue("FORM_NO_JOURNEY", "缺少用户任务（journeys）：没有任务就没有产品形态")
    for journey in journeys:
        jid = journey.get("id") or "<no-id>"
        if not JOURNEY_ID_RE.fullmatch(str(journey.get("id") or "")):
            issue("FORM_JOURNEY_NO_ID", f"{jid} 的 id 必须符合 J-NNN-NN")
        if len(journey.get("steps") or []) < 3:
            issue("FORM_JOURNEY_SHORT", f"{jid} 的步骤少于 3 步，无法描述完整任务")
        if _is_placeholder(str(journey.get("success") or "")):
            issue("FORM_JOURNEY_NO_SUCCESS", f"{jid} 缺少成功判据（用户怎么知道做完了）")
        if not journey.get("actor"):
            issue("FORM_JOURNEY_NO_ACTOR", f"{jid} 缺少执行者")

    screens = form.get("screens") or []
    screen_ids = {str(item.get("id") or "") for item in screens if isinstance(item, dict)}
    if not screens:
        issue("FORM_NO_SCREEN", "缺少界面/位置（screens）：用户在哪里看到它")
    for screen in screens:
        sid = screen.get("id") or "<no-id>"
        if not SCREEN_ID_RE.fullmatch(str(screen.get("id") or "")):
            issue("FORM_SCREEN_NO_ID", f"{sid} 的 id 必须符合 S-NNN-NN")
        if not screen.get("location"):
            issue("FORM_SCREEN_NO_LOCATION", f"{sid} 未说明在界面中的位置")
        if not screen.get("states"):
            issue("FORM_SCREEN_NO_STATES", f"{sid} 未列出状态（空/加载/错误/完成）", "warn")
        if not screen.get("actions"):
            issue("FORM_SCREEN_NO_ACTIONS", f"{sid} 未列出用户动作", "warn")

    usage = form.get("usage") or []
    if len(usage) < 3:
        issue("FORM_USAGE_SHORT", "使用说明少于 3 步；用户需要可照做的步骤")

    mapping = form.get("acceptance_mapping") or []
    if not mapping:
        issue("FORM_NO_AC_MAPPING", "缺少验收映射：无法证明产品形态覆盖了需求验收条件")
    mapped_acs: set[str] = set()
    for entry in mapping:
        if entry.get("journey_id") not in journey_ids:
            issue("FORM_MAPPING_NO_JOURNEY", f"验收映射的 journey_id 无效：{entry.get('journey_id')!r}")
        ac_ids = entry.get("ac_ids") or []
        if not isinstance(ac_ids, list):
            issue("FORM_BAD_STRUCTURE", "验收映射的 ac_ids 必须是数组")
            continue
        if not ac_ids:
            issue("FORM_MAPPING_NO_AC", "验收映射缺少 ac_ids")
        for ac_id in ac_ids:
            if not AC_ID_RE.fullmatch(str(ac_id)):
                issue("FORM_MAPPING_BAD_AC", f"验收映射含无效 AC ID：{ac_id!r}")
            if str(ac_id) in mapped_acs:
                issue("FORM_MAPPING_DUPLICATE", f"验收映射重复覆盖 {ac_id}")
            mapped_acs.add(str(ac_id))
    expected_acs: set[str] = set()
    ledger = load_ledger(root)
    requirements = {str(item.get("id")): item for item in ledger.get("requirements", [])}
    for req_id in form.get("requirement_ids") or []:
        requirement = requirements.get(str(req_id))
        if requirement:
            expected_acs.update(_ac_ids(requirement))
    if expected_acs and mapped_acs != expected_acs:
        missing = sorted(expected_acs - mapped_acs)
        extra = sorted(mapped_acs - expected_acs)
        issue("FORM_MAPPING_INCOMPLETE", f"AC 映射必须完整且精确；缺少 {missing}，多出 {extra}")

    confirmation = form.get("confirmation") or {}
    kind = confirmation.get("kind")
    refs = confirmation.get("source_refs") or []
    if form.get("status") == "confirmed":
        if kind != "user":
            issue("FORM_CONFIRMED_NOT_USER", "状态为 confirmed，但确认来源不是用户")
        receipt_path = root / FORMS_RELATIVE / str(form.get("form_id") or "") / RECEIPT_NAME
        try:
            receipt = _read_json(receipt_path) if receipt_path.is_file() else {}
            digest = _form_digest(receipt_path.parent)
        except (OSError, ValueError, TypeError):
            receipt, digest = {}, ""
        if not isinstance(receipt, dict):
            receipt = {}
        if not refs or not receipt or receipt.get("source_refs") != refs:
            issue("FORM_CONFIRMED_NO_SOURCE", "缺少独立确认回执或其来源列表不匹配")
        if receipt.get("form_id") != form.get("form_id") or receipt.get("version") != form.get("version"):
            issue("FORM_CONFIRMED_BAD_BINDING", "确认回执未绑定当前表单 ID/版本")
        if not digest or receipt.get("content_sha256") != digest:
            issue("FORM_CONFIRMED_STALE", "表单内容已在确认后变化，必须重新取得明确确认")
        if _is_placeholder(str(confirmation.get("confirmed_at") or "")) or receipt.get("confirmed_at") != confirmation.get("confirmed_at"):
            issue("FORM_CONFIRMED_NO_DATE", "确认回执缺少匹配的确认时间")
        source_records = {str(item.get("id")): item for item in load_ledger(root).get("sources", [])}
        for ref in refs:
            source = source_records.get(str(ref))
            if not source or not _source_affirms_form(source, form, receipt.get("content_sha256", "")):
                issue("FORM_CONFIRMED_BAD_SOURCE", f"{ref} 未明确记录对当前表单版本和摘要的肯定用户确认")

    bodies = _section_bodies(form.get("_md_text") or "")
    if not form.get("_md_exists"):
        issue("FORM_NO_DOC", f"缺少 {MD_NAME}：结构化 JSON 不能替代人可读的产品说明")
    else:
        if _contains_todo(form.get("_md_text") or ""):
            issue("FORM_TODO_MARKDOWN", "Markdown 中仍有 TODO/TBD/待补充等占位内容")
        for section in REQUIRED_SECTIONS:
            body = bodies.get(section)
            if body is None:
                issue("FORM_SECTION_MISSING", f"缺少章节「{section}」")
            elif _is_placeholder(body) and section != "与其他功能的接缝":
                issue("FORM_SECTION_EMPTY", f"章节「{section}」没有内容")

    return issues


# --------------------------------------------------------------------------
# scaffold


def _ac_ids(requirement: dict[str, Any]) -> list[str]:
    scenarios = requirement.get("acceptance_scenarios") or []
    if scenarios:
        return [str(item.get("id")) for item in scenarios]
    criteria = requirement.get("acceptance_criteria") or []
    return [f"{requirement.get('id')}-AC-{n:02d}" for n in range(1, len(criteria) + 1)]


def _journey_from_requirement(requirement: dict[str, Any]) -> dict[str, Any]:
    req_id = requirement.get("id", "REQ-UNKNOWN")
    criteria = requirement.get("acceptance_criteria") or []
    steps = ["TODO 明确从现有工作区哪个入口进入。"]
    for index, text in enumerate(criteria[:4], start=1):
        steps.append(f"TODO 将验收条件 {index} 转为可观察的用户动作：{text}")

    steps.append("TODO 明确看到的结果、失败反馈及恢复动作。")
    return {
        "id": f"J-{req_id.split('-')[-1]}-01",
        "actor": "论文作者",
        "trigger": str(requirement.get("trigger") or "作者需要完成该功能时"),
        "steps": steps,
        "success": "TODO 描述用户能观察到的完成判据；对应需求：" + str(requirement.get("outcome") or requirement.get("title") or ""),
        "screens": [],
    }


def scaffold(root: Path, req_id: str, force: bool = False) -> dict[str, Any]:
    if force:
        raise ValueError("不支持强制覆盖；请手动维护现有表单并递增版本")
    ledger = load_ledger(root)
    requirement = next((item for item in ledger.get("requirements", []) if item.get("id") == req_id), None)
    if requirement is None:
        raise KeyError(f"账本中没有 {req_id}")
    directory = root / FORMS_RELATIVE / f"PF-{req_id}"
    if directory.exists():
        raise FileExistsError(f"{directory.relative_to(root).as_posix()} 已存在；为保护人工编辑内容，不允许覆盖")

    journey = _journey_from_requirement(requirement)
    ac_ids = _ac_ids(requirement)
    related = [str(item) for item in requirement.get("related_requirement_ids") or []]
    body = {
        "form_id": f"PF-{req_id}",
        "title": f"{requirement.get('title')} 产品形态",
        "version": int(requirement.get("version") or 1),
        "status": "draft",
        "requirement_ids": [req_id],
        "confirmation": {"kind": "none", "source_refs": [], "confirmed_at": "", "note": ""},
        "journeys": [journey],
        "screens": [
            {
                "id": f"S-{req_id.split('-')[-1]}-01",
                "name": requirement.get("module_title") or requirement.get("title"),
                "location": "TODO 标出当前导航和工作区中的真实入口。",
                "states": ["TODO 逐一描述空、加载、错误、完成的可见表现"],
                "elements": ["TODO 列出用户可见的关键元素"],
                "actions": ["TODO 列出用户能执行的动作及反馈"],
            }
        ],
        "seams": [{"with": other, "contract": "TODO 明确交接数据、责任边界及失败反馈。"} for other in related],
        "usage": [
            "1. TODO 从哪个入口进入。",
            "2. TODO 按什么顺序操作。",
            "3. TODO 如何确认成功或失败并恢复。",
        ],
        "non_goals": ["TODO 明确本形态不包含的能力。"],
        "acceptance_mapping": [{"journey_id": journey["id"], "ac_ids": ac_ids}],
        "source_refs": list(requirement.get("source_refs") or []),
        "ledger_version": requirement.get("version"),
    }
    _write_json(directory / JSON_NAME, body)
    _write_text(directory / MD_NAME, render_form_markdown(body))
    return body


def render_form_markdown(form: dict[str, Any]) -> str:
    req = ", ".join(form.get("requirement_ids") or [])
    lines = [
        f"# {form.get('title', '产品形态')}",
        "",
        f"> 形态 ID：`{form.get('form_id')}`　｜　覆盖需求：{req}　｜　版本：v{form.get('version')}　｜　状态：**{form.get('status')}**",
        ">",
        "> 本文件是「功能 → 产品 → 代码」中间层的确认对象。用户确认的是这一层，不是工程包。",
        "",
    ]
    actor = "、".join(sorted({str(j.get("actor")) for j in form.get("journeys") or [] if j.get("actor")})) or "TODO"
    lines += ["## 用户与场景", "", f"- 执行者：{actor}", ""]
    for journey in form.get("journeys") or []:
        lines.append(f"- {journey.get('id')}：{journey.get('trigger')}　成功判据：{journey.get('success')}")
    lines += ["", "## 主流程", ""]
    for journey in form.get("journeys") or []:
        lines.append(f"### {journey.get('id')}")
        lines.append("")
        for index, step in enumerate(journey.get("steps") or [], start=1):
            lines.append(f"{index}. {step}")
        lines.append("")
    lines += ["## 入口与位置", ""]
    for screen in form.get("screens") or []:
        lines.append(f"- {screen.get('id')} {screen.get('name')}：{screen.get('location')}")
    lines += ["", "## 界面与状态", ""]
    for screen in form.get("screens") or []:
        lines.append(f"- {screen.get('id')} 状态：{'、'.join(screen.get('states') or [])}")
        for element in screen.get("elements") or []:
            lines.append(f"  - 元素：{element}")
    lines += ["", "## 交互与反馈", ""]
    for screen in form.get("screens") or []:
        for action in screen.get("actions") or []:
            lines.append(f"- {screen.get('id')}：{action}")
    lines += ["", "## 使用说明", ""]
    for step in form.get("usage") or []:
        lines.append(f"- {step}")
    lines += ["", "## 失败与恢复", ""]
    lines += ["- 失败时保留已提交前的状态，展示可理解的错误原因，并提供重试入口。", ""]
    lines += ["## 与其他功能的接缝", ""]
    for seam in form.get("seams") or []:
        lines.append(f"- 与 {seam.get('with')}：{seam.get('contract')}")
    lines += ["", "## 非目标", ""]
    for item in form.get("non_goals") or []:
        lines.append(f"- {item}")
    lines += ["", "## 验收映射", ""]
    lines.append("| 用户任务 | 覆盖的验收条件 |")
    lines.append("|---|---|")
    for entry in form.get("acceptance_mapping") or []:
        lines.append(f"| {entry.get('journey_id')} | {', '.join(entry.get('ac_ids') or [])} |")
    lines += ["", "## 确认记录", ""]
    confirmation = form.get("confirmation") or {}
    lines.append(f"- 确认方式：{confirmation.get('kind') or 'none'}")
    lines.append(f"- 确认来源：{', '.join(confirmation.get('source_refs') or []) or '—'}")
    lines.append(f"- 确认时间：{confirmation.get('confirmed_at') or '—'}")
    if confirmation.get("note"):
        lines.append(f"- 备注：{confirmation['note']}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# confirmation


def confirm(root: Path, form_id: str, source_refs: list[str], note: str = "", expected_sha256: str = "") -> dict[str, Any]:
    if not FORM_ID_RE.fullmatch(form_id):
        raise ValueError("form_id 必须符合 PF-REQ-NNN")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("必须显式提供当前表单的 64 位小写 SHA-256")
    ledger = load_ledger(root)
    source_records = {str(item.get("id")): item for item in ledger.get("sources", [])}
    missing = [ref for ref in source_refs if ref not in source_records]
    if missing:
        raise ValueError(f"确认来源不在账本 sources 中：{', '.join(missing)}")
    if not source_refs or len(set(source_refs)) != len(source_refs):
        raise ValueError("必须提供不重复的账本 SRC 来源")

    directory = root / FORMS_RELATIVE / form_id
    json_path = directory / JSON_NAME
    if not json_path.is_file():
        raise FileNotFoundError(f"未找到 {json_path}")
    form = _read_json(json_path)
    if not isinstance(form, dict):
        raise ValueError("表单 JSON 根节点必须是对象")
    if form.get("status") == "confirmed":
        raise ValueError("已确认表单不可重复确认或覆盖回执")
    if (directory / RECEIPT_NAME).exists():
        raise ValueError("已有确认回执，不可覆盖；请先核对表单与回执状态")
    if not _valid_form_identity(form) or form.get("form_id") != form_id:
        raise ValueError("表单 ID、需求 ID 或版本无效")
    digest = _form_digest(directory)
    if digest != expected_sha256:
        raise ValueError("表单摘要与用户提交的 SHA-256 不匹配；确认已拒绝")
    issues = validate_form(root, {**form, "_md_text": (directory / MD_NAME).read_text(encoding="utf-8") if (directory / MD_NAME).is_file() else "", "_md_exists": (directory / MD_NAME).is_file(), "_json_path": json_path.relative_to(root).as_posix()}, set(source_records))
    blocking = [item for item in issues if item["severity"] == "error"]
    if blocking:
        raise ValueError("产品形态仍有未完成项，不能确认：" + "；".join(item["message"] for item in blocking[:6]))

    for ref in source_refs:
        if not _source_affirms_form(source_records[ref], form, digest):
            raise ValueError(f"{ref} 未明确记录对 {form_id} v{form['version']}（SHA-256 {digest}）的肯定用户确认")
    confirmed_at = _now()
    form["status"] = "confirmed"
    form["confirmation"] = {
        "kind": "user",
        "source_refs": list(source_refs),
        "confirmed_at": confirmed_at,
        "note": note,
        "content_sha256": digest,
    }
    _write_json(directory / RECEIPT_NAME, {
        "form_id": form_id,
        "version": form["version"],
        "content_sha256": digest,
        "source_refs": list(source_refs),
        "confirmed_at": confirmed_at,
    })
    _write_json(json_path, form)
    return form


def bind_package(root: Path, requirement_id: str, requirement_version: int, package: dict[str, Any]) -> dict[str, Any]:
    """Bind a new package to the exact, currently confirmed product form."""
    if not isinstance(requirement_version, int) or isinstance(requirement_version, bool) or requirement_version < 1:
        raise ValueError("requirement_version 必须是正整数")
    ledger = load_ledger(root)
    requirement = next((item for item in ledger.get("requirements", []) if item.get("id") == requirement_id), None)
    if not requirement or requirement.get("version") != requirement_version:
        raise ValueError("需求不存在或版本已过期")
    candidates = [item for item in forms_by_requirement(root).get(requirement_id, []) if item.get("status") == "confirmed"]
    sources = {str(item.get("id")) for item in ledger.get("sources", [])}
    valid = [item for item in candidates if _valid_form_identity(item) and item.get("version") == requirement_version
             and not any(issue["severity"] == "error" for issue in validate_form(root, item, sources))]
    if len(valid) != 1:
        raise ValueError("没有唯一且有效的已确认产品形态，或确认版本与需求不一致")
    form = valid[0]
    directory = root / FORMS_RELATIVE / str(form["form_id"])
    digest = _form_digest(directory)
    receipt_path = directory / RECEIPT_NAME
    receipt = _read_json(receipt_path) if receipt_path.is_file() else {}
    if receipt.get("form_id") != form["form_id"] or receipt.get("version") != form["version"] or receipt.get("content_sha256") != digest:
        raise ValueError("产品形态确认已失效或表单内容已变化")
    package["product_form_ref"] = {"form_id": form["form_id"], "version": form["version"], "content_sha256": digest}
    package["requirement_version"] = requirement_version
    return package


# --------------------------------------------------------------------------
# chain


def _first(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return items[0] if items else None


def build_chain(root: Path) -> dict[str, Any]:
    ledger = load_ledger(root)
    packages = latest_packages(root)
    index = build_index(root)
    forms_map = forms_by_requirement(root)
    sources = {str(item.get("id")) for item in ledger.get("sources", [])}
    evidence = ledger.get("evidence", [])

    requirements: list[dict[str, Any]] = []
    for requirement in ledger.get("requirements", []):
        req_id = requirement.get("id", "")
        if requirement.get("status") == "superseded":
            continue
        forms = forms_map.get(req_id, [])
        confirmed = [form for form in forms if form.get("status") == "confirmed" and not any(
            item["severity"] == "error" for item in validate_form(root, form, sources)
        )]
        form = next(iter(confirmed), None) or _first(forms)
        package = packages.get(req_id)
        ac_ids = _ac_ids(requirement)

        mapped_acs: set[str] = set()
        if form and isinstance(form.get("acceptance_mapping"), list):
            for entry in form["acceptance_mapping"]:
                if isinstance(entry, dict) and isinstance(entry.get("ac_ids"), list):
                    mapped_acs.update(str(item) for item in entry["ac_ids"])

        package_ref = (package or {}).get("product_form_ref") or {}
        form_digest = ""
        if form:
            try:
                form_digest = _form_digest(root / FORMS_RELATIVE / str(form.get("form_id") or ""))
            except (OSError, ValueError, TypeError):
                pass
        package_form_ok = bool(
            form and form in confirmed and isinstance(package_ref, dict)
            and package_ref.get("form_id") == form.get("form_id")
            and package_ref.get("version") == form.get("version")
            and package_ref.get("content_sha256") == form_digest
            and (package or {}).get("requirement_version") == requirement.get("version")
        )

        req_evidence = [item for item in evidence if req_id in (item.get("related_ids") or [])]
        receipt_path = root / FORMS_RELATIVE / str((form or {}).get("form_id") or "") / RECEIPT_NAME
        try:
            receipt = _read_json(receipt_path) if receipt_path.is_file() else {}
        except (OSError, ValueError, TypeError):
            receipt = {}
        if not isinstance(receipt, dict):
            receipt = {}
        product_acceptance = bool(confirmed and package_form_ok) and any(
            item.get("product_acceptance") is True and item.get("status") == "passed"
            and item.get("product_form_ref") == {
                "form_id": (form or {}).get("form_id"),
                "version": (form or {}).get("version"),
                "content_sha256": receipt.get("content_sha256"),
            }
            for item in req_evidence
        )
        passed_evidence = [item for item in req_evidence if item.get("status") == "passed"]

        code_paths: list[str] = []
        for mapping in requirement.get("code_mapping") or []:
            for part in re.split(r"[;,]", str(mapping.get("path") or "")):
                part = part.strip()
                if part and " " not in part and re.search(r"\.[A-Za-z0-9]{1,6}$", part):
                    code_paths.append(part)
        code_files_exist = bool(code_paths) and all(index.has_path(path) for path in code_paths)

        test_files_exist = bool(requirement.get("code_mapping")) and all(
            not mapping.get("tests") or all(index.has_path(path.strip()) for path in str(mapping.get("tests") or "").split(";") if path.strip())
            for mapping in requirement.get("code_mapping") or []
        )

        stages = {
            "function": bool(ac_ids),
            "product": bool(forms),
            "product_confirmed": bool(confirmed),
            "engineering_package": bool(package),
            "package_bound": package_form_ok,
            "code": code_files_exist,
            "tests": test_files_exist,
            "product_acceptance": product_acceptance,
        }
        missing = [stage for stage in CHAIN_STAGES if not stages[stage]]
        requirements.append(
            {
                "requirement_id": req_id,
                "title": requirement.get("title", ""),
                "readiness": requirement.get("readiness", ""),
                "version": requirement.get("version"),
                "form_id": (form or {}).get("form_id") or "",
                "form_status": (form or {}).get("status") or "none",
                "form_version": (form or {}).get("version"),
                "form_issues": validate_form(root, form, sources) if form else [],
                "code_files_exist": code_files_exist,
                "test_files_exist": test_files_exist,
                "product_form_sha256": form_digest,
                "package_present": bool(package),
                "package_form_ref": package_ref,
                "ac_count": len(ac_ids),
                "ac_mapped": len([ac for ac in ac_ids if ac in mapped_acs]),
                "unmapped_acs": [ac for ac in ac_ids if ac not in mapped_acs],
                "evidence_passed": len(passed_evidence),
                "product_acceptance": product_acceptance,
                "stages": stages,
                "missing_stages": missing,
                "gate_blocked": requirement.get("readiness") in GATED_READINESS and not (confirmed and package_form_ok),
                "chain_complete": not missing,
            }
        )

    summary = _chain_summary(requirements)
    return {
        "schema": "product-chain/1",
        "generated_at": _now(),
        "summary": summary,
        "requirements": requirements,
    }


def _chain_summary(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    stage_counts = {stage: sum(1 for item in requirements if item["stages"][stage]) for stage in CHAIN_STAGES}
    return {
        "requirements": len(requirements),
        "chain_complete": sum(1 for item in requirements if item["chain_complete"]),
        "gate_blocked": sum(1 for item in requirements if item["gate_blocked"]),
        "forms": {
            "none": sum(1 for item in requirements if item["form_status"] == "none"),
            "draft": sum(1 for item in requirements if item["form_status"] == "draft"),
            "review": sum(1 for item in requirements if item["form_status"] == "review"),
            "confirmed": sum(1 for item in requirements if item["form_status"] == "confirmed"),
        },
        "stages": stage_counts,
        "package_bound": sum(1 for item in requirements if item["package_form_ref"]),
        "product_accepted": sum(1 for item in requirements if item["product_acceptance"]),
    }


def render_chain_report(chain: dict[str, Any]) -> str:
    summary = chain["summary"]
    lines = ["# 需求固化链路报告", ""]
    lines.append(f"> 生成时间：{chain['generated_at']}")
    lines.append(">")
    lines.append("> 链路：功能（REQ/AC）→ 产品（形态确认）→ 代码（工程包/映射/测试）→ 产品验收。任一环缺失即不算固化。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- 活动需求 {summary['requirements']} 条，链路完整 {summary['chain_complete']} 条")
    lines.append(f"- 产品形态：未建 {summary['forms']['none']}、草稿 {summary['forms']['draft']}、待评审 {summary['forms']['review']}、已确认 {summary['forms']['confirmed']}")
    lines.append(f"- 工程包已绑定形态版本：{summary['package_bound']} 条")
    lines.append(f"- 当前门禁条件缺口：{summary['gate_blocked']} 条（诊断值；不回退历史就绪状态）")
    lines.append(f"- 有产品验收证据：{summary['product_accepted']} 条")
    lines.append("")
    lines.append("## 各环节覆盖")
    lines.append("")
    lines.append("| 环节 | 覆盖 |")
    lines.append("|---|---|")
    stage_labels = {
        "function": "需求与验收条件",
        "product": "产品形态（存在）",
        "product_confirmed": "产品形态（已确认）",
        "engineering_package": "工程包",
        "package_bound": "工程包绑定形态版本",
        "code": "代码映射文件存在",
        "tests": "测试入口存在",
        "product_acceptance": "产品验收证据",
    }
    for stage in CHAIN_STAGES:
        lines.append(f"| {stage_labels[stage]} | {summary['stages'][stage]}/{summary['requirements']} |")
    lines.append("")
    lines.append("## 逐需求链路")
    lines.append("")
    lines.append("| 需求 | 就绪度 | 产品形态 | 工程包 | 形态绑定 | AC 映射 | 缺失环节 | 新操作门禁条件 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for item in chain["requirements"]:
        lines.append(
            "| {req} {title} | {readiness} | {form} | {pkg} | {bound} | {mapped}/{total} | {missing} | {gate} |".format(
                req=item["requirement_id"],
                title=item["title"],
                readiness=item["readiness"],
                form=item["form_status"],
                pkg="有" if item["package_present"] else "无",
                bound="是" if item["stages"]["package_bound"] else "否",
                mapped=item["ac_mapped"],
                total=item["ac_count"],
                missing="、".join(stage_labels[stage] for stage in item["missing_stages"]) or "—",
                gate="缺确认/绑定" if item["gate_blocked"] else "满足",
            )
        )
    lines.append("")
    lines.append("## 需要先补产品形态的需求")
    lines.append("")
    pending = [item for item in chain["requirements"] if item["form_status"] in ("none", "draft", "review")]
    if not pending:
        lines.append("- 无")
    for item in pending:
        lines.append(f"### {item['requirement_id']} {item['title']}")
        lines.append("")
        lines.append(f"- 当前形态：{item['form_status']}　｜　就绪度：{item['readiness']}")
        if item["form_issues"]:
            for issue in item["form_issues"][:8]:
                lines.append(f"- [{issue['severity']}] {issue['message']}")
        else:
            lines.append("- 尚未创建形态包；先执行 scaffold，再按模板填写用户任务、入口、状态、使用说明与接缝。")
        lines.append("")
    return "\n".join(lines)


def write_chain(root: Path, chain: dict[str, Any] | None = None) -> dict[str, Any]:
    chain = chain or build_chain(root)
    out = root / CHAIN_RELATIVE
    _write_json(out / "chain.json", chain)
    _write_text(out / "CHAIN-REPORT.md", render_chain_report(chain))
    return chain
