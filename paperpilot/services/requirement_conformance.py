"""Requirement conformance: compare confirmed requirements with the real product.

The ledger (`.project-to-act/docs/project-manager/state.json`) is the only
authority for requirement text, acceptance criteria and evidence references.
This module never rewrites it.  It produces a separate, regenerable matrix and
keeps human judgements in their own file so a rescan can never erase them.

Two different questions are answered and never mixed:

* automatic checks - does the repository still contain the artefacts the
  ledger claims (test nodes, browser entries, code paths, API routes, evidence
  records)?  A passing automatic check means "reviewable", not "correct".
* human judgements - does the delivered behaviour match the confirmed intent?
  Only an explicit `correct` / `wrong` judgement counts as conformance.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "requirement-conformance/1"

LEDGER_RELATIVE = Path(".project-to-act/docs/project-manager/state.json")
PACKAGE_ROOT_RELATIVE = Path(".project-to-act/docs/project-development/packages")
OUTPUT_RELATIVE = Path(".project-to-act/docs/project-development/conformance")

VERDICTS = ("correct", "wrong", "partial", "unverified")
VERDICT_LABELS = {
    "correct": "正确",
    "wrong": "错误",
    "partial": "部分符合",
    "unverified": "待核",
}
AUTO_LABELS = {
    "supported": "有可核对入口",
    "partial": "入口不完整",
    "unsupported": "入口缺失",
    "unverified": "无可核对入口",
}

ROOT_CAUSES = {
    "requirement_ambiguity": "需求表述模糊/不可观察",
    "confirmation_gap": "未经人工确认即实现",
    "design_drift": "工程设计偏离需求",
    "implementation_gap": "实现未按设计落地",
    "ui_entry_gap": "能力存在但缺少入口/形态不符",
    "integration_seam": "跨模块接缝断裂",
    "evidence_gap": "证据不足或不可复核",
    "scope_creep": "实现超出确认范围",
    "unknown": "未定位",
}

CHECK_LABELS = {
    "AC_NO_SCENARIO": "需求未登记该验收场景",
    "AC_NO_TRACEABILITY": "工程包缺少该 AC 的追溯条目",
    "AC_TEST_FILE_MISSING": "追溯引用的测试文件不存在",
    "AC_TEST_NODE_MISSING": "追溯引用的测试节点不存在",
    "AC_BROWSER_ENTRY_MISSING": "追溯引用的浏览器入口不存在",
    "AC_EXEC_FILE_MISSING": "执行命令引用的文件不存在",
    "REQ_EVIDENCE_MISSING": "需求没有任何证据记录",
    "REQ_EVIDENCE_UNVERIFIED": "证据全部处于 needs_review",
    "REQ_EVIDENCE_FAILED": "存在失败结论的证据",
    "REQ_CODE_PATH_MISSING": "代码映射声明的文件不存在",
    "REQ_ROUTE_MISSING": "代码映射声明的接口未在源码中找到",
    "REQ_ROUTE_METHOD_MISMATCH": "接口路径存在但 HTTP 方法不同",
    "REQ_UI_SURFACE_UNDECLARED": "未声明前端入口文件",
    "REQ_CLAIM_UNBACKED": "宣称可验收但缺少人工核验",
    "REQ_AC_COUNT_MISMATCH": "验收场景数与工程包追溯数不一致",
    "REQ_GLOBAL_AUDIT_FAILED": "全局真实体验审查未通过",
    "REQ_SUPERSEDED": "该需求已被替代，不参与当前对照",
}

_ROOT_CAUSE_CHECK_HINTS = {
    "REQ_GLOBAL_AUDIT_FAILED": "integration_seam",
    "REQ_ROUTE_MISSING": "implementation_gap",
    "REQ_ROUTE_METHOD_MISMATCH": "implementation_gap",
    "REQ_UI_SURFACE_UNDECLARED": "ui_entry_gap",
    "REQ_CLAIM_UNBACKED": "evidence_gap",
    "REQ_EVIDENCE_FAILED": "evidence_gap",
    "REQ_EVIDENCE_UNVERIFIED": "evidence_gap",
    "REQ_EVIDENCE_MISSING": "evidence_gap",
    "AC_TEST_NODE_MISSING": "evidence_gap",
    "AC_TEST_FILE_MISSING": "evidence_gap",
    "AC_EXEC_FILE_MISSING": "evidence_gap",
    "AC_EXEC_PATH_EXTERNAL": "evidence_gap",
    "AC_NO_TRACEABILITY": "design_drift",
}

_ERROR_CHECKS = {
    "AC_NO_TRACEABILITY",
    "AC_TEST_FILE_MISSING",
    "AC_TEST_NODE_MISSING",
    "AC_EXEC_FILE_MISSING",
    "REQ_EVIDENCE_MISSING",
    "REQ_CODE_PATH_MISSING",
    "REQ_ROUTE_MISSING",
    "REQ_EVIDENCE_FAILED",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_ledger(root: Path) -> dict[str, Any]:
    path = root / LEDGER_RELATIVE
    if not path.is_file():
        raise FileNotFoundError(f"未找到需求账本：{path}")
    return _read_json(path)


def latest_packages(root: Path) -> dict[str, dict[str, Any]]:
    """Return the newest engineering package body per requirement id."""
    result: dict[str, dict[str, Any]] = {}
    base = root / PACKAGE_ROOT_RELATIVE
    if not base.is_dir():
        return result
    for req_dir in sorted(base.iterdir()):
        if not req_dir.is_dir():
            continue
        versions: list[tuple[int, Path]] = []
        for version_dir in req_dir.iterdir():
            if version_dir.is_dir() and re.fullmatch(r"v\d+", version_dir.name):
                versions.append((int(version_dir.name[1:]), version_dir))
        for _, version_dir in sorted(versions, reverse=True):
            candidate = version_dir / "ENGINEERING-PACKAGE.json"
            if candidate.is_file():
                result[req_dir.name] = _read_json(candidate)
                break
    return result


# --------------------------------------------------------------------------
# repository index


@dataclass
class RepoIndex:
    root: Path
    test_nodes: dict[str, set[str]] = field(default_factory=dict)
    routes: set[str] = field(default_factory=set)
    route_files: dict[str, set[str]] = field(default_factory=dict)

    def has_node(self, reference: str) -> bool:
        path_part, _, node = reference.partition("::")
        if not path_part:
            return False
        if not (self.root / path_part).is_file():
            return False
        if not node:
            return True
        leaf = node.split("::")[-1]
        leaf = re.sub(r"\[.*\]$", "", leaf)  # pytest parametrisation suffix
        return leaf in self.test_nodes.get(path_part, set())

    def has_path(self, relative: str) -> bool:
        return (self.root / relative).is_file()

    def match_route(self, method: str, path: str) -> bool:
        wanted = f"{method.upper()} {_normalize_route(path)}"
        if wanted in self.routes:
            return True
        # Loose suffix match: a mapping may name only the tail of a nested route.
        tail = _normalize_route(path)
        if not tail.startswith("/"):
            return False
        return any(route.endswith(tail) and route.split(" ", 1)[0] == method.upper() for route in self.routes)

    def match_any_method(self, path: str) -> bool:
        tail = _normalize_route(path)
        if not tail.startswith("/"):
            return False
        return any(route.endswith(tail) for route in self.routes)


def _normalize_route(path: str) -> str:
    path = re.sub(r"\{[^}]*\}", "{}", path.strip())
    path = re.sub(r":\w+", "{}", path)
    return path.rstrip("/") or "/"


_DECORATOR_RE = re.compile(r"@(app|router)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_PREFIX_RE = re.compile(r"APIRouter\(\s*[^)]*prefix\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)


def build_index(root: Path) -> RepoIndex:
    index = RepoIndex(root=root)
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            index.test_nodes[path.relative_to(root).as_posix()] = set(_TEST_DEF_RE.findall(text))
    candidates: list[Path] = []
    app_file = root / "app.py"
    if app_file.is_file():
        candidates.append(app_file)
    api_dir = root / "paperpilot" / "api"
    if api_dir.is_dir():
        candidates.extend(sorted(api_dir.glob("*.py")))
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        prefix_match = _PREFIX_RE.search(text)
        prefix = prefix_match.group(1) if prefix_match else ""
        relative = path.relative_to(root).as_posix()
        for target, method, route in _DECORATOR_RE.findall(text):
            full = route if route.startswith(prefix) or not prefix else prefix.rstrip("/") + "/" + route.lstrip("/")
            if target == "app":
                full = route
            key = f"{method.upper()} {_normalize_route(full)}"
            index.routes.add(key)
            index.route_files.setdefault(key, set()).add(relative)
    return index


# --------------------------------------------------------------------------
# judgements


def judgements_path(root: Path) -> Path:
    return root / OUTPUT_RELATIVE / "judgements.json"


def load_judgements(root: Path) -> dict[str, Any]:
    path = judgements_path(root)
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "updated_at": "", "entries": {}}
    payload = _read_json(path)
    payload.setdefault("entries", {})
    return payload


def save_judgement(
    root: Path,
    target: str,
    verdict: str,
    cause: str = "unknown",
    expected: str = "",
    observed: str = "",
    note: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {VERDICTS} 之一")
    if cause not in ROOT_CAUSES:
        raise ValueError(f"cause 必须是 {list(ROOT_CAUSES)} 之一")
    payload = load_judgements(root)
    entries = payload["entries"]
    if verdict == "unverified" and not (observed or note or evidence):
        entries.pop(target, None)
    else:
        entries[target] = {
            "target": target,
            "verdict": verdict,
            "cause": cause,
            "expected": expected,
            "observed": observed,
            "note": note,
            "evidence": evidence,
            "updated_at": _now(),
        }
    payload["schema"] = SCHEMA_VERSION
    payload["updated_at"] = _now()
    _write_json(judgements_path(root), payload)
    return entries.get(target, {"target": target, "verdict": "unverified"})


# --------------------------------------------------------------------------
# checks


def _check(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {
        "code": code,
        "label": CHECK_LABELS.get(code, code),
        "severity": "error" if code in _ERROR_CHECKS else "warn",
        "message": message,
        "detail": detail,
    }


def _parse_execution_files(command: str) -> list[str]:
    found = re.findall(r"([A-Za-z0-9_./\\-]+\.py)", command or "")
    return [item.replace("\\", "/") for item in found]


def _parse_endpoints(code_mapping: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    endpoints: list[tuple[str, str]] = []
    for mapping in code_mapping or []:
        entry = str(mapping.get("entry") or "")
        method = ""
        for chunk in re.split(r"[;,]", entry):
            chunk = chunk.strip()
            if not chunk:
                continue
            match = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(.+)$", chunk, re.IGNORECASE)
            if match:
                method = match.group(1).upper()
                endpoints.append((method, match.group(2).strip()))
            elif method and chunk.startswith("/"):
                endpoints.append((method, chunk))
    return endpoints


def _evidence_for(ledger: dict[str, Any], req_id: str) -> list[dict[str, Any]]:
    return [item for item in ledger.get("evidence", []) if req_id in (item.get("related_ids") or [])]


CROSS_CUTTING_THRESHOLD = 8

PRODUCT_AUDIT_KINDS = {
    "real_user_journey_audit",
    "frontend_refinement_verification",
}


def is_cross_cutting(item: dict[str, Any]) -> bool:
    """Evidence attached to many requirements is a product-level audit, not REQ evidence."""
    return len(set(item.get("related_ids") or [])) >= CROSS_CUTTING_THRESHOLD


def _is_human_verified(requirement: dict[str, Any]) -> bool:
    human = str(requirement.get("human_verification") or "")
    if human.startswith("verified") or human == "pending":
        return human.startswith("verified")
    lock = (requirement.get("confirmation_lock") or {}).get("status")
    return lock == "locked"


def evaluate_ac(
    req_id: str,
    ac: dict[str, Any],
    traceability: dict[str, dict[str, Any]],
    index: RepoIndex,
) -> dict[str, Any]:
    ac_id = ac.get("id") or ""
    checks: list[dict[str, Any]] = []
    trace = traceability.get(ac_id)
    if not trace:
        checks.append(_check("AC_NO_TRACEABILITY", f"工程包追溯表中没有 {ac_id}"))
    else:
        for reference in trace.get("neighboring_regression_nodes") or []:
            if not index.has_node(reference):
                code = "AC_TEST_FILE_MISSING" if not (index.root / reference.partition("::")[0]).is_file() else "AC_TEST_NODE_MISSING"
                checks.append(_check(code, f"引用的测试节点不存在：{reference}"))
        for reference in trace.get("browser_entry_points") or []:
            if not index.has_path(reference):
                checks.append(_check("AC_BROWSER_ENTRY_MISSING", f"引用的浏览器入口不存在：{reference}"))
        for path in _parse_execution_files(str(trace.get("execution_command") or "")):
            if re.match(r"^[A-Za-z]:/", path) or path.startswith("/") or path.startswith("~/"):
                checks.append(_check("AC_EXEC_PATH_EXTERNAL", f"执行命令引用仓库外路径：{path}"))
            elif not index.has_path(path):
                code = "AC_EXEC_FILE_MISSING" if str(trace.get("required_evidence")) != "manual" else "AC_EXEC_PATH_EXTERNAL"
                checks.append(_check(code, f"执行命令引用的文件不存在：{path}"))
    if checks:
        has_error = any(item["severity"] == "error" for item in checks)
        auto = "unsupported" if has_error else "partial"
    else:
        auto = "supported"
    return {
        "ac_id": ac_id,
        "requirement_id": req_id,
        "given": ac.get("given", ""),
        "when": ac.get("when", ""),
        "then": ac.get("then", ""),
        "verification": ac.get("verification", ""),
        "auto_verdict": auto,
        "checks": checks,
    }


def evaluate_requirement(
    requirement: dict[str, Any],
    package: dict[str, Any] | None,
    ledger: dict[str, Any],
    index: RepoIndex,
) -> dict[str, Any]:
    req_id = requirement.get("id", "")
    checks: list[dict[str, Any]] = []
    traceability = {}
    if package:
        traceability = {item.get("ac_id"): item for item in package.get("traceability") or [] if item.get("ac_id")}

    scenarios = requirement.get("acceptance_scenarios") or []
    if not scenarios:
        text_criteria = requirement.get("acceptance_criteria") or []
        scenarios = [
            {"id": f"{req_id}-AC-{n:02d}", "then": text, "verification": ""}
            for n, text in enumerate(text_criteria, start=1)
        ]
        if text_criteria:
            checks.append(_check("AC_NO_SCENARIO", "验收条件只有文字，没有结构化场景记录"))
    if package and len(traceability) != len(scenarios):
        checks.append(
            _check(
                "REQ_AC_COUNT_MISMATCH",
                f"验收场景 {len(scenarios)} 条，工程包追溯 {len(traceability)} 条",
                sorted(set(traceability) - {item.get('id') for item in scenarios}),
            )
        )

    ac_records = [evaluate_ac(req_id, ac, traceability, index) for ac in scenarios]

    code_paths = []
    for mapping in requirement.get("code_mapping") or []:
        path = str(mapping.get("path") or "").strip()
        if not path:
            continue
        for part in re.split(r"[;,]", path):
            part = part.strip()
            if not part or " " in part:
                # Mapping rows occasionally hold prose ("REQ-018 ImportRequest interface").
                continue
            if not re.search(r"\.[A-Za-z0-9]{1,6}$", part):
                continue
            code_paths.append(part)
            if not index.has_path(part):
                checks.append(_check("REQ_CODE_PATH_MISSING", f"代码映射声明的文件不存在：{part}", path))
        endpoints = _parse_endpoints([mapping])
        missing = [f"{method} {route}" for method, route in endpoints if not index.match_route(method, route) and not index.match_any_method(route)]
        mismatch = [
            f"{method} {route}"
            for method, route in endpoints
            if not index.match_route(method, route) and index.match_any_method(route)
        ]
        if missing:
            checks.append(_check("REQ_ROUTE_MISSING", "代码映射声明的接口未找到", missing))
        if mismatch:
            checks.append(_check("REQ_ROUTE_METHOD_MISMATCH", "接口路径存在但方法或层级与映射不同", mismatch))
    if code_paths and not any(p.endswith((".js", ".html", ".css")) for p in code_paths):
        static = [p for p in requirement.get("implementation_evidence") or [] if str(p).endswith((".js", ".html"))]
        if not static:
            checks.append(_check("REQ_UI_SURFACE_UNDECLARED", "代码映射与实现证据都未声明前端入口文件"))

    evidence = _evidence_for(ledger, req_id)
    local_evidence = [item for item in evidence if not is_cross_cutting(item)]
    audit_evidence = [item for item in evidence if is_cross_cutting(item)]
    if not local_evidence:
        checks.append(_check("REQ_EVIDENCE_MISSING", "需求没有专属证据记录"))
    else:
        statuses = {str(item.get("status") or "") for item in local_evidence}
        if statuses == {"needs_review"}:
            checks.append(_check("REQ_EVIDENCE_UNVERIFIED", "全部专属证据仍是 needs_review"))
        if "failed" in statuses:
            checks.append(_check("REQ_EVIDENCE_FAILED", "存在 failed 的专属证据"))
    failed_audits = [item for item in audit_evidence if item.get("status") == "failed"]
    if failed_audits:
        ids = ", ".join(str(item.get("id")) for item in failed_audits)
        checks.append(
            _check(
                "REQ_GLOBAL_AUDIT_FAILED",
                f"跨需求真实体验审查未通过：{ids}",
                [item.get("artifact_ref") or item.get("location") for item in failed_audits],
            )
        )

    readiness = str(requirement.get("readiness") or "")
    if readiness == "ready_for_acceptance" and not _is_human_verified(requirement):
        checks.append(
            _check(
                "REQ_CLAIM_UNBACKED",
                f"就绪度为 {readiness}，但人工核验状态为 {requirement.get('human_verification') or '未记录'}",
            )
        )

    if requirement.get("status") == "superseded":
        checks.append(_check("REQ_SUPERSEDED", "该需求已被替代，不作为当前产品形态的对照对象"))

    errors = [item for item in checks if item["severity"] == "error"]
    warns = [item for item in checks if item["severity"] == "warn"]
    for ac in ac_records:
        errors.extend(item for item in ac["checks"] if item["severity"] == "error")
        warns.extend(item for item in ac["checks"] if item["severity"] == "warn")

    if requirement.get("status") == "superseded":
        auto_verdict = "unverified"
    elif errors:
        auto_verdict = "unsupported"
    elif warns:
        auto_verdict = "partial"
    elif ac_records:
        auto_verdict = "supported"
    else:
        auto_verdict = "unverified"

    return {
        "requirement_id": req_id,
        "title": requirement.get("title", ""),
        "version": requirement.get("version"),
        "status": requirement.get("status", ""),
        "readiness": readiness,
        "priority": requirement.get("priority", ""),
        "module_id": requirement.get("module_id", ""),
        "human_verification": requirement.get("human_verification") or "",
        "confirmation_lock": (requirement.get("confirmation_lock") or {}).get("status") or "",
        "ai_confirmation": (requirement.get("ai_confirmation") or {}).get("status") or "",
        "auto_verdict": auto_verdict,
        "checks": checks,
        "evidence_count": len(local_evidence),
        "evidence_passed": sum(1 for item in local_evidence if item.get("status") == "passed"),
        "audit_evidence_count": len(audit_evidence),
        "package_present": bool(package),
        "acceptance_criteria": ac_records,
    }


# --------------------------------------------------------------------------
# matrix


def _resolve_judgement(target: str, judgements: dict[str, Any]) -> dict[str, Any] | None:
    entries = judgements.get("entries") or {}
    return entries.get(target)


def build_matrix(root: Path) -> dict[str, Any]:
    ledger = load_ledger(root)
    packages = latest_packages(root)
    index = build_index(root)
    judgements = load_judgements(root)
    requirements = [
        evaluate_requirement(requirement, packages.get(requirement.get("id", "")), ledger, index)
        for requirement in ledger.get("requirements", [])
    ]
    for requirement in requirements:
        key_req = f"{requirement['requirement_id']}::*"
        req_judgement = _resolve_judgement(key_req, judgements)
        requirement["judgement"] = req_judgement
        requirement["verdict"] = (req_judgement or {}).get("verdict", "unverified")
        for ac in requirement["acceptance_criteria"]:
            target = f"{requirement['requirement_id']}::{ac['ac_id']}"
            judgement = _resolve_judgement(target, judgements)
            ac["judgement"] = judgement
            ac["verdict"] = (judgement or {}).get("verdict", "unverified")

    summary = _summarise(requirements)
    global_evidence = [
        {
            "id": item.get("id"),
            "kind": item.get("kind"),
            "status": item.get("status"),
            "scope": item.get("scope", ""),
            "artifact_ref": item.get("artifact_ref") or item.get("location", ""),
            "p1_findings": item.get("p1_findings"),
            "observed_findings": item.get("observed_findings"),
            "related_count": len(item.get("related_ids") or []),
        }
        for item in ledger.get("evidence", [])
        if is_cross_cutting(item)
        and (item.get("status") == "failed" or item.get("kind") in PRODUCT_AUDIT_KINDS)
    ]
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": _now(),
        "root": str(root),
        "summary": summary,
        "global_evidence": global_evidence,
        "requirements": requirements,
    }


def _summarise(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    ac_counts = {verdict: 0 for verdict in VERDICTS}
    req_counts = {verdict: 0 for verdict in VERDICTS}
    causes: dict[str, int] = {}
    auto_counts: dict[str, int] = {}
    check_counts: dict[str, int] = {}
    for requirement in requirements:
        req_counts[requirement["verdict"]] = req_counts.get(requirement["verdict"], 0) + 1
        auto_counts[requirement["auto_verdict"]] = auto_counts.get(requirement["auto_verdict"], 0) + 1
        for ac in requirement["acceptance_criteria"]:
            ac_counts[ac["verdict"]] = ac_counts.get(ac["verdict"], 0) + 1
        judgement = requirement.get("judgement")
        ac_judged = False
        for ac in requirement["acceptance_criteria"]:
            ac_judgement = ac.get("judgement")
            if ac_judgement and ac_judgement.get("target", "").endswith("::" + ac["ac_id"]):
                ac_judged = True
                if ac_judgement.get("verdict") in ("wrong", "partial"):
                    cause = ac_judgement.get("cause") or "unknown"
                    causes[cause] = causes.get(cause, 0) + 1
        if judgement and not ac_judged and judgement.get("verdict") in ("wrong", "partial"):
            cause = judgement.get("cause") or "unknown"
            causes[cause] = causes.get(cause, 0) + 1
        for check in requirement.get("checks") or []:
            check_counts[check["code"]] = check_counts.get(check["code"], 0) + 1
        for ac in requirement["acceptance_criteria"]:
            for check in ac.get("checks") or []:
                check_counts[check["code"]] = check_counts.get(check["code"], 0) + 1
    return {
        "requirements": len(requirements),
        "acceptance_criteria": sum(len(item["acceptance_criteria"]) for item in requirements),
        "requirement_verdicts": req_counts,
        "ac_verdicts": ac_counts,
        "auto_verdicts": auto_counts,
        "root_causes": causes,
        "checks": check_counts,
    }


# --------------------------------------------------------------------------
# report


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


def render_report(matrix: dict[str, Any]) -> str:
    summary = matrix["summary"]
    ac = summary["ac_verdicts"]
    req = summary["requirement_verdicts"]
    lines: list[str] = []
    lines.append("# 需求对照报告")
    lines.append("")
    lines.append(f"> 生成时间：{matrix['generated_at']}　｜　账本：`{LEDGER_RELATIVE.as_posix()}`")
    lines.append(">")
    lines.append("> 自动检查只回答「仓库里是否还有账本声称的测试/代码/接口入口」，不等于产品正确；`正确`/`错误` 只来自显式人工判定。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- 需求 {summary['requirements']} 条，验收条件 {summary['acceptance_criteria']} 条")
    lines.append(f"- 判定：正确 {ac.get('correct', 0)}　错误 {ac.get('wrong', 0)}　部分符合 {ac.get('partial', 0)}　待核 {ac.get('unverified', 0)}")
    lines.append(f"- 需求级：正确 {req.get('correct', 0)}　错误 {req.get('wrong', 0)}　部分符合 {req.get('partial', 0)}　待核 {req.get('unverified', 0)}")
    lines.append("")
    lines.append("## 根因分布（已判定为错误/部分符合的需求）")
    lines.append("")
    if summary["root_causes"]:
        for cause, count in sorted(summary["root_causes"].items(), key=lambda item: -item[1]):
            lines.append(f"- {ROOT_CAUSES.get(cause, cause)}（`{cause}`）：{count}")
    else:
        lines.append("- 尚无带根因的判定")
    lines.append("")

    global_evidence = matrix.get("global_evidence") or []
    if global_evidence:
        lines.append("## 全局证据（跨需求审查）")
        lines.append("")
        for item in global_evidence:
            detail = f"，P1 {item['p1_findings']} 项" if item.get("p1_findings") is not None else ""
            lines.append(
                f"- {item['id']}（{item['kind']}，{item['status']}，覆盖 {item['related_count']} 条需求{detail}）：{item['artifact_ref']}"
            )
        lines.append("")

    wrong: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]] = []
    for requirement in matrix["requirements"]:
        ac_judged = False
        for ac_item in requirement["acceptance_criteria"]:
            judgement = ac_item.get("judgement")
            if not judgement or not str(judgement.get("target", "")).endswith("::" + ac_item["ac_id"]):
                continue
            ac_judged = True
            if judgement.get("verdict") in ("wrong", "partial"):
                wrong.append((requirement, ac_item, judgement))
        req_judgement = requirement.get("judgement")
        if req_judgement and not ac_judged and req_judgement.get("verdict") in ("wrong", "partial"):
            wrong.append((requirement, None, req_judgement))

    lines.append("## 错误与偏差清单")
    lines.append("")
    if not wrong:
        lines.append("尚无人工判定为错误的需求；下方「自动检查发现」列出可先行核查的结构性问题。")
    for requirement, ac_item, judgement in wrong:
        scope = f" · {ac_item['ac_id']}" if ac_item else " · 整条需求"
        lines.append(
            f"### {requirement['requirement_id']} {requirement['title']}（{VERDICT_LABELS.get(judgement['verdict'])}{scope}）"
        )
        lines.append("")
        lines.append(f"- 根因：{ROOT_CAUSES.get(judgement.get('cause', 'unknown'))}")
        if ac_item and ac_item.get("then"):
            lines.append(f"- 需求期望：{ac_item['then']}")
        elif judgement.get("expected"):
            lines.append(f"- 需求期望：{judgement['expected']}")
        if judgement.get("observed"):
            lines.append(f"- 实际表现：{judgement['observed']}")
        if judgement.get("evidence"):
            lines.append(f"- 证据：{judgement['evidence']}")
        if judgement.get("note"):
            lines.append(f"- 备注：{judgement['note']}")
        lines.append("")

    correct = [r for r in matrix["requirements"] if r["verdict"] == "correct"]
    correct_acs = [
        (requirement, ac_item)
        for requirement in matrix["requirements"]
        for ac_item in requirement["acceptance_criteria"]
        if ac_item.get("judgement")
        and str(ac_item["judgement"].get("target", "")).endswith("::" + ac_item["ac_id"])
        and ac_item["judgement"].get("verdict") == "correct"
    ]
    lines.append("## 已确认正确的需求")
    lines.append("")
    if correct or correct_acs:
        for requirement in correct:
            judgement = requirement.get("judgement") or {}
            line = f"- {requirement['requirement_id']} {requirement['title']}（整条需求）"
            if judgement.get("note"):
                line += f"：{judgement['note']}"
            lines.append(line)
        for requirement, ac_item in correct_acs:
            judgement = ac_item.get("judgement") or {}
            line = f"- {requirement['requirement_id']} {requirement['title']} · {ac_item['ac_id']}"
            if judgement.get("note"):
                line += f"：{judgement['note']}"
            lines.append(line)
    else:
        lines.append("- 无（尚未录入人工判定）")
    lines.append("")

    lines.append("## 自动检查发现（结构性问题，按需求）")
    lines.append("")
    flagged = [r for r in matrix["requirements"] if r["checks"] or any(ac["checks"] for ac in r["acceptance_criteria"])]
    if not flagged:
        lines.append("- 未发现结构性缺口")
    for requirement in flagged:
        if requirement.get("status") == "superseded":
            continue
        lines.append(f"### {requirement['requirement_id']} {requirement['title']}")
        lines.append("")
        for check in requirement["checks"]:
            detail = f"（{', '.join(map(str, check['detail']))}）" if isinstance(check.get("detail"), list) else (f"（{check['detail']}）" if check.get("detail") else "")
            lines.append(f"- [{check['severity']}] {check['label']}：{check['message']}{detail}")
        ac_issues = [ac for ac in requirement["acceptance_criteria"] if ac["checks"]]
        for ac_item in ac_issues:
            codes = sorted({item["label"] for item in ac_item["checks"]})
            lines.append(f"- {ac_item['ac_id']}：{'；'.join(codes)}")
        lines.append("")

    lines.append("## 逐条对照明细")
    lines.append("")
    lines.append("| 需求 | AC | 期望结果（then） | 自动核对 | 判定 | 根因 |")
    lines.append("|---|---|---|---|---|---|")
    for requirement in matrix["requirements"]:
        for ac_item in requirement["acceptance_criteria"]:
            then = str(ac_item.get("then") or "").replace("|", "／")[:80]
            judgement = ac_item.get("judgement") or {}
            lines.append(
                "| {req} | {ac} | {then} | {auto} | {verdict} | {cause} |".format(
                    req=requirement["requirement_id"],
                    ac=ac_item["ac_id"],
                    then=then,
                    auto=AUTO_LABELS.get(ac_item["auto_verdict"], ac_item["auto_verdict"]),
                    verdict=VERDICT_LABELS.get(ac_item["verdict"], ac_item["verdict"]),
                    cause=ROOT_CAUSES.get(judgement.get("cause", ""), "—") if judgement else "—",
                )
            )
    lines.append("")
    lines.append("## 证据与确认状态（需求级）")
    lines.append("")
    lines.append("| 需求 | 版本 | 就绪度 | 人工核验 | 确认锁 | 证据(passed/总数) | 自动核对 |")
    lines.append("|---|---:|---|---|---|---|---|")
    for requirement in matrix["requirements"]:
        lines.append(
            f"| {requirement['requirement_id']} | {requirement['version']} | {requirement['readiness']} | "
            f"{requirement['human_verification'] or '—'} | {requirement['confirmation_lock'] or '—'} | "
            f"{requirement['evidence_passed']}/{requirement['evidence_count']} | {AUTO_LABELS.get(requirement['auto_verdict'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(root: Path, matrix: dict[str, Any] | None = None) -> dict[str, Any]:
    matrix = matrix or build_matrix(root)
    out_dir = root / OUTPUT_RELATIVE
    _write_json(out_dir / "matrix.json", matrix)
    (out_dir / "REPORT.md").write_text(render_report(matrix), encoding="utf-8")
    return matrix


def scan(root: Path) -> dict[str, Any]:
    return write_outputs(root)


def iter_ac(matrix: dict[str, Any]) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for requirement in matrix["requirements"]:
        for ac_item in requirement["acceptance_criteria"]:
            yield requirement, ac_item
