from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from paperpilot.services.content_versions import version_map
from paperpilot.services.project_errors import ProjectServiceError


_CITE_RE = re.compile(r"\\cite\w*\{([^}]+)\}")
_SENTENCE_RE = re.compile(r"[^。！？.!?\n]+[。！？.!?]?")
_QUOTE_RE = re.compile(r"(?:[\"“”])([^\"“”]+)(?:[\"”])")
_CONDITION_RE = re.compile(r"(?:在|当|如果|若|因|由于|under|when|if)[^，。.!?;；]{1,100}", re.IGNORECASE)
_LATEX_STRUCTURE_COMMAND_RE = re.compile(
    r"\\(?:documentclass|usepackage|begin|end|section|subsection|subsubsection|chapter|label|bibliography|addbibresource|input|include|newcommand|renewcommand|title|author|date|maketitle|tableofcontents|centering|item)\*?(?:\[[^\]\n]*\])?(?:\{[^{}\n]*\})?",
    re.IGNORECASE,
)
_TOKEN_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "under", "when", "where", "which",
    "into", "than", "then", "their", "there", "have", "has", "are", "was", "were", "been", "being", "can", "may",
}


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", value or "")
        if token.lower() not in _TOKEN_STOPWORDS
    }


def _claim_text(sentence: str) -> str:
    """Remove citation commands before classifying or comparing a claim."""
    return re.sub(r"\\cite\w*\{[^}]+\}", "", sentence or "").strip()


def _claim_type(sentence: str) -> str:
    comparable = _claim_text(sentence)
    lowered = comparable.lower()
    # Citation keys frequently contain years (for example ``smith2024``); they
    # must not turn an otherwise descriptive claim into a quantitative one.
    if re.search(r"(?:\b\d+(?:\.\d+)?\s*%|\b(?:increased|decreased|improved|reduced|平均|比例|率|数量|达到|约)\D{0,20}\d+(?:\.\d+)?\b)", comparable, re.IGNORECASE):
        return "quantitative"
    if any(marker in lowered for marker in ("because", "therefore", "导致", "因此", "因而", "由于")):
        return "causal"
    if any(marker in lowered for marker in ("more", "less", "higher", "lower", "相比", "优于", "大于", "小于")):
        return "comparative"
    if any(marker in lowered for marker in ("defined as", "refers to", "定义为", "是指")):
        return "definition"
    return "descriptive"


def _direct_quote(sentence: str) -> dict[str, Any]:
    matches = [match.group(1).strip() for match in _QUOTE_RE.finditer(sentence) if match.group(1).strip()]
    return {"detected": bool(matches), "spans": matches, "match_status": "not_applicable" if not matches else "pending"}


def _conditions(sentence: str) -> list[str]:
    comparable = _claim_text(sentence)
    values: list[str] = []
    for match in _CONDITION_RE.finditer(comparable):
        value = " ".join(match.group(0).strip().split(" "))
        value = value.rstrip("，,；;。.!?！？")
        if value and value not in values:
            values.append(value)
    return values[:8]


def _normalized_phrase(value: str) -> str:
    return "".join(str(value or "").lower().split())


def _source_phrases(value: str) -> list[str]:
    return [phrase.strip() for phrase in re.split(r"[.!?。！？\n]", str(value or "")) if phrase.strip()][:3]


def _evidence_payload(evidence: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    texts: list[str] = []
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        raw_fragments = item.get("fragments") or []
        fragments: list[dict[str, Any]] = []
        for index, fragment in enumerate(raw_fragments):
            if not isinstance(fragment, dict):
                continue
            fragment_text = str(fragment.get("text", ""))
            if not fragment_text:
                continue
            location = str(fragment.get("location", ""))
            fragment_hash = hashlib.sha256(fragment_text.encode("utf-8")).hexdigest()
            fragment_id = hashlib.sha256(
                f"{item.get('evidence_id', '')}|{index}|{location}|{fragment_hash}".encode("utf-8")
            ).hexdigest()[:24]
            fragments.append({**fragment, "fragment_id": fragment_id, "text_sha256": fragment_hash})
        text = " ".join(str(fragment.get("text", "")) for fragment in fragments).strip()
        if text:
            texts.append(text)
        source_url = str(item.get("source_url") or "")
        source_version = str(item.get("source_version") or "")
        evidence_id = str(item.get("evidence_id") or "")
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        source_identity = hashlib.sha256(
            "|".join((evidence_id, source_url, source_version, text_hash)).encode("utf-8")
        ).hexdigest() if text or evidence_id or source_url else ""
        normalized.append({
            **item,
            "fragments": fragments,
            "evidence_text": text,
            "evidence_text_sha256": text_hash,
            "source_identity": source_identity,
        })
    return " ".join(texts), normalized


def _reference_rows(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT cite_key,title,authors,year,bibtex FROM refs WHERE project_id=?", (project_id,)):
        result[row["cite_key"]] = {"cite_key": row["cite_key"], "title": row["title"], "authors": row["authors"], "year": row["year"], "bibtex": row["bibtex"], "source_type": "project_bib"}
    for row in conn.execute("SELECT cite_key,reference_id,reference_version,citation_snapshot_json,link_status FROM project_reference_links WHERE project_id=? AND link_status IN ('linked','update_available')", (project_id,)):
        snapshot = __import__("json").loads(row["citation_snapshot_json"] or "{}")
        result[row["cite_key"]] = {"cite_key": row["cite_key"], "reference_id": row["reference_id"], "reference_version": row["reference_version"], "link_status": row["link_status"], **snapshot, "source_type": "reference_library"}
    return result


def _evidence_for_reference(conn: sqlite3.Connection, item: dict[str, Any]) -> list[dict[str, Any]]:
    reference_id = item.get("reference_id")
    if not reference_id:
        return []
    rows = conn.execute(
        "SELECT e.id,e.source_url,e.fragments_json,e.source_version FROM reference_attachments a JOIN external_evidence e ON e.id=a.external_evidence_id JOIN oa_imports i ON i.id=e.import_id WHERE a.reference_id=? AND a.reference_version=? AND i.status='imported'",
        (reference_id, int(item.get("reference_version") or 0)),
    ).fetchall()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        fragments = __import__("json").loads(row["fragments_json"] or "[]")
        evidence.append({"evidence_id": row["id"], "source_url": row["source_url"], "source_version": row["source_version"], "fragments": fragments})
    return evidence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_content(conn: sqlite3.Connection, project_id: str, target_path: str | None) -> str:
    if target_path:
        row = conn.execute(
            "SELECT content FROM files WHERE project_id=? AND path=?",
            (project_id, target_path),
        ).fetchone()
        if row is None:
            raise ProjectServiceError(404, "citation_target_missing", "引用审查目标文件不存在。")
        return str(row["content"])
    rows = conn.execute(
        "SELECT content FROM files WHERE project_id=? AND lower(path) LIKE '%.tex' ORDER BY path",
        (project_id,),
    ).fetchall()
    return "\n".join(str(row["content"]) for row in rows)


def _input_fingerprint(dependency_fingerprint: str, content: str) -> str:
    return hashlib.sha256(json.dumps({
        "dependencies": dependency_fingerprint,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }, sort_keys=True).encode("utf-8")).hexdigest()


def dependencies(conn: sqlite3.Connection, project_id: str, target_path: str | None) -> dict[str, Any]:
    versions = version_map(conn, project_id)
    files = [
        {
            "path": row["path"],
            "version": int(versions.get(row["path"], 0)),
            "sha256": hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest(),
        }
        for row in conn.execute("SELECT path,content FROM files WHERE project_id=? ORDER BY path", (project_id,))
        if target_path is None or row["path"] == target_path
    ]
    refs = _reference_rows(conn, project_id)
    reference_dependencies = []
    evidence_dependencies = []
    for key, ref in sorted(refs.items()):
        reference_dependencies.append({
            "cite_key": key,
            "reference_id": ref.get("reference_id", ""),
            "reference_version": ref.get("reference_version", 0),
            "title": ref.get("title", ""),
            "abstract_sha256": hashlib.sha256(str(ref.get("abstract", "")).encode("utf-8")).hexdigest(),
            "link_status": ref.get("link_status", "linked"),
        })
        for evidence in _evidence_for_reference(conn, ref):
            evidence_dependencies.append({
                "evidence_id": evidence.get("evidence_id"),
                "source_version": evidence.get("source_version"),
                "text_sha256": hashlib.sha256(
                    " ".join(
                        str(fragment.get("text", ""))
                        for fragment in evidence.get("fragments", [])
                        if isinstance(fragment, dict)
                    ).encode("utf-8")
                ).hexdigest(),
            })
    outline = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
    terms = [
        {"id": row["id"], "version": int(row["version"] or 0), "status": row["status"], "term": row["term"]}
        for row in conn.execute("SELECT id,version,status,term FROM terms WHERE project_id=? ORDER BY id", (project_id,))
    ]
    logic = [
        {
            "id": row["id"], "kind": row["kind"], "label": row["label"],
            "details_sha256": hashlib.sha256(str(row["details"]).encode("utf-8")).hexdigest(),
        }
        for row in conn.execute("SELECT id,kind,label,details FROM logic_nodes WHERE project_id=? ORDER BY id", (project_id,))
    ]
    payload = {
        "target_path": target_path or "",
        "files": files,
        "outline_sha256": hashlib.sha256(str(outline["content"] if outline else "").encode("utf-8")).hexdigest(),
        "outline_version": int(versions.get("__outline__", 0)),
        "terms": terms,
        "logic": logic,
        "references": reference_dependencies,
        "evidence": evidence_dependencies,
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def _authorized_evidence_ids(conn: sqlite3.Connection, project_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT e.id FROM external_evidence e "
        "JOIN oa_imports i ON i.id=e.import_id AND i.project_id=e.project_id "
        "JOIN reference_attachments a ON a.external_evidence_id=e.id AND a.project_id=e.project_id "
        "JOIN project_reference_links l ON l.reference_id=a.reference_id AND l.project_id=a.project_id "
        "AND l.reference_version=a.reference_version "
        "WHERE e.project_id=? AND i.status='imported' AND l.link_status IN ('linked','update_available')",
        (project_id,),
    ).fetchall()
    return {str(row["id"]) for row in rows}


def filter_current_permissions(
    conn: sqlite3.Connection,
    project_id: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Remove revoked full-text evidence from fresh and historical reports."""
    result = copy.deepcopy(report)
    authorized = _authorized_evidence_ids(conn, project_id)
    revoked_evidence: set[str] = set()
    revoked_sources: set[str] = set()
    for claim in result.get("claims", []):
        for support in claim.get("supports", []):
            original_evidence = support.get("evidence") or []
            visible_evidence = [
                item for item in original_evidence
                if not item.get("evidence_id") or item.get("evidence_id") in authorized
            ]
            if len(visible_evidence) == len(original_evidence):
                continue
            revoked_evidence.update(str(item.get("evidence_id")) for item in original_evidence if item.get("evidence_id") and item.get("evidence_id") not in authorized)
            support["evidence"] = visible_evidence
            if visible_evidence:
                support["evidence_text"] = " ".join(item.get("evidence_text", "") for item in visible_evidence).strip()
                support["source_identity"] = visible_evidence[0].get("source_identity", "")
                continue
            support["evidence_text"] = ""
            support.pop('semantic_finding', None)
            support['reasons'] = []
            support["source_identity"] = ""
            support["confidence"] = "low"
            support["support_status"] = "metadata_only"
            support["evidence_overlap_terms"] = 0
            support["evidence_overlap_ratio"] = 0.0
            support["evidence_matched_terms"] = []
            support["direct_quote_match_status"] = (
                "missing" if support.get("direct_quote_match_status") in {"matched", "mismatch"}
                else "not_applicable"
            )
            comparison = support.get("expression_comparison") or {}
            if comparison.get("mode") == "evidence_text_only":
                comparison["source_phrases"] = []
                comparison["mode"] = "permission_revoked"
            reasons = support.setdefault("reasons", [])
            if "全文证据授权已撤回，历史报告已隐藏对应片段" not in reasons:
                reasons.append("全文证据授权已撤回，历史报告已隐藏对应片段")
    for source in result.get("source_pool", []):
        evidence = source.get("evidence") or []
        visible = [item for item in evidence if not item.get("evidence_id") or item.get("evidence_id") in authorized]
        if len(visible) != len(evidence):
            revoked_sources.add(str(source.get("source_id", "")))
            revoked_evidence.update(str(item.get("evidence_id")) for item in evidence if item.get("evidence_id") and item.get("evidence_id") not in authorized)
            source["evidence"] = visible
            source["full_text_presence"] = "metadata_only"
            source["permission_filtered"] = True
            source["evidence_text"] = ""
    for claim in result.get("claims", []):
        review = claim.get("semantic_review") or {}
        for finding in review.get("sources", []):
            if finding.get("evidence_id") and finding["evidence_id"] in revoked_evidence:
                finding.update({
                    "relation": "insufficient_evidence", "role": "metadata_only",
                    "evidence_id": "", "fragment_id": "", "quoted_text": "", "location": "",
                    "rationale": "来源授权已撤回，历史报告隐藏全文派生判断。", "full_text": False,
                })
        review["expression_examples"] = [
            item for item in review.get("expression_examples", [])
            if item.get("evidence_id") not in revoked_evidence
        ]
        claim["evidence_candidates"] = [
            item for item in claim.get("evidence_candidates", [])
            if item.get("evidence_id") not in revoked_evidence
        ]
        suggestion = review.get("suggestion") or {}
        if set(suggestion.get("used_source_ids", [])) & revoked_sources:
            suggestion.update({"text": "", "meaning_change_text": "", "reasons": [], "status": "permission_revoked"})
        if review.get("sources") and not any(item.get("full_text") for item in review["sources"]):
            suggestion.update({"text": "", "meaning_change_text": "", "reasons": [], "status": "permission_revoked"})
        if revoked_evidence:
            claim['language_review'] = {}
            for condition in claim.get('condition_results', []):
                condition.update({'status':'unknown','reason':'来源授权已撤回，派生判断已隐藏'})
    return result


def run_report(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    request_id: str,
    content: str | None = None,
    target_path: str | None = None,
    semantic_response: str | dict[str, Any] | None = None,
    semantic_model: str = "",
    allowed_source_ids: list[str] | None = None,
    analysis_identity: dict[str, Any] | None = None,
    semantic_error: str = "",
    semantic_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = dependencies(conn, project_id, target_path)
    source_content = _source_content(conn, project_id, target_path) if content is None else content
    input_hash = _input_fingerprint(current["fingerprint"], source_content)
    identity = analysis_identity or {"mode": "local", "model": "", "rule_version": "citation-semantics-v2"}
    existing = conn.execute(
        "SELECT * FROM citation_check_runs WHERE project_id=? AND request_id=?",
        (project_id, request_id),
    ).fetchone()
    if existing:
        previous = json.loads(existing["result_json"] or "{}")
        if existing["source_fingerprint"] != input_hash or previous.get("analysis_identity", {}) != identity:
            raise ProjectServiceError(409, "citation_check_idempotency_conflict", "request_id 已用于不同的引用审查输入。")
        return citation_run_payload(conn, existing)

    report = analyze(conn, project_id, content=source_content, target_path=target_path)
    report["analysis_identity"] = identity
    if semantic_response is not None:
        from paperpilot.services.citation_semantic import apply_response
        try:
            report = apply_response(
                report, semantic_response, model=semantic_model,
                allowed_source_ids=allowed_source_ids,
            )
        except (ValueError, TypeError, KeyError):
            report["semantic_review"] = {
                "status": "provider_failed",
                "message": "语义模型返回格式无法验证；未采用其判断。",
                "requires_human_review": True,
                "automatic_writes": False,
            }
    else:
        report["semantic_review"] = {
            "status": "provider_failed" if semantic_error else "consent_required",
            "message": semantic_error or "未执行语义模型核验；词汇重叠仅作检索提示，不代表论断已获支持。",
            "requires_human_review": True,
            "automatic_writes": False,
        }
    if semantic_metadata:
        report.setdefault("semantic_review", {}).update(semantic_metadata)
    report["source"]["content_sha256"] = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
    versions = version_map(conn, project_id)
    source_version = int(versions.get(target_path, 0)) if target_path else 0
    run_id = uuid.uuid4().hex
    stamp = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO citation_check_runs(id,project_id,request_id,target_path,source_project_revision,source_version,source_fingerprint,status,result_json,created_at,updated_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, project_id, request_id, target_path or "",
            int(report["source"]["project_revision"]), source_version, input_hash,
            "completed", json.dumps(report, ensure_ascii=False), stamp.isoformat(),
            stamp.isoformat(), (stamp + timedelta(days=30)).isoformat(),
        ),
    )
    row = conn.execute("SELECT * FROM citation_check_runs WHERE id=?", (run_id,)).fetchone()
    return citation_run_payload(conn, row)


def citation_run_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    if expires <= datetime.now(timezone.utc):
        raise ProjectServiceError(410, "citation_check_expired", "该引用审查报告已过期。")
    current = dependencies(conn, row["project_id"], row["target_path"] or None)
    try:
        current_content = _source_content(conn, row["project_id"], row["target_path"] or None)
        current_fingerprint = _input_fingerprint(current["fingerprint"], current_content)
    except ProjectServiceError:
        current_fingerprint = ""
    report = filter_current_permissions(
        conn, row["project_id"], json.loads(row["result_json"] or "{}")
    )
    stale = current_fingerprint != row["source_fingerprint"]
    report.update({
        "run_id": row["id"],
        "request_id": row["request_id"],
        "status": "stale" if stale else row["status"],
        "stale": stale,
        "source_version": int(row["source_version"]),
        "dependency_fingerprint": row["source_fingerprint"],
        "expires_at": row["expires_at"],
    })
    from paperpilot.services import visual_evidence
    files = [dict(f) for f in conn.execute("SELECT path,content FROM files WHERE project_id=?", (row["project_id"],))]
    report["visual_reports"] = visual_evidence.projections_for_files(conn, row["project_id"], files)
    return report


def analyze(conn: sqlite3.Connection, project_id: str, content: str | None = None, target_path: str | None = None) -> dict[str, Any]:
    if content is None:
        if target_path:
            rows = conn.execute("SELECT path,content FROM files WHERE project_id=? AND path=?", (project_id, target_path)).fetchall()
        else:
            rows = conn.execute("SELECT path,content FROM files WHERE project_id=? AND lower(path) LIKE '%.tex' ORDER BY path", (project_id,)).fetchall()
        content = "\n".join(row["content"] for row in rows)
    refs = _reference_rows(conn, project_id)
    project = conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
    outline = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
    terms = [dict(row) for row in conn.execute("SELECT id,term,definition,aliases,version FROM terms WHERE project_id=? ORDER BY term", (project_id,))]
    logic = [dict(row) for row in conn.execute("SELECT id,label,details,links FROM logic_nodes WHERE project_id=? ORDER BY rowid", (project_id,))]
    claims: list[dict[str, Any]] = []
    source_content = content or ""
    scan_content = _LATEX_STRUCTURE_COMMAND_RE.sub(
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        source_content,
    )
    for index, match in enumerate(_SENTENCE_RE.finditer(scan_content), start=1):
        scan_span = match.group(0)
        leading = len(scan_span) - len(scan_span.lstrip())
        trailing = len(scan_span.rstrip())
        claim_start = match.start() + leading
        claim_end = match.start() + trailing
        sentence = " ".join(source_content[claim_start:claim_end].split())
        if len(sentence) < 12:
            continue
        claim_text = _claim_text(sentence)
        claim_tokens = _tokens(claim_text)
        claim_type = _claim_type(sentence)
        direct_quote = _direct_quote(sentence)
        conditions = _conditions(sentence)
        cited: list[str] = []
        for cite in _CITE_RE.finditer(sentence):
            cited.extend(key.strip() for key in cite.group(1).split(",") if key.strip())
        cited = list(dict.fromkeys(cited))
        supports: list[dict[str, Any]] = []
        for key in cited:
            ref = refs.get(key)
            if ref is None:
                supports.append({
                    "cite_key": key,
                    "status": "missing",
                    "support_status": "missing",
                    "confidence": "none",
                    "evidence": [],
                    "evidence_text": "",
                    "source_identity": "",
                    "evidence_overlap_ratio": 0.0,
                    "direct_quote_match_status": "not_applicable" if not direct_quote["detected"] else "missing",
                    "reasons": ["正文引用键未进入当前论文工程清单"],
                })
                continue
            source_text = " ".join(str(ref.get(field, "")) for field in ("title", "authors", "abstract", "bibtex"))
            source_tokens = _tokens(source_text)
            matched_terms = sorted(claim_tokens & source_tokens)
            overlap = len(matched_terms)
            lexical_similarity = round(overlap / max(1, len(claim_tokens)), 3)
            evidence_raw = _evidence_for_reference(conn, ref)
            evidence_text, evidence = _evidence_payload(evidence_raw)
            evidence_tokens = _tokens(evidence_text)
            evidence_matched_terms = sorted(claim_tokens & evidence_tokens)
            evidence_overlap_ratio = round(len(evidence_matched_terms) / max(1, len(claim_tokens)), 3)
            source_phrases = _source_phrases(evidence_text if evidence_text else str(ref.get("abstract", "")))
            quote_status = "not_applicable"
            support_status = "metadata_only" if not evidence else "not_verifiable"
            reasons: list[str] = []
            if not evidence:
                reasons.append("当前只有元数据/摘要，未找到可回放全文证据")
                if lexical_similarity >= 0.25:
                    reasons.append("元数据或摘要存在词汇重叠，仅作为检索提示，不能证明正文论断成立")
                elif not source_tokens:
                    reasons.append("参考文献缺少可比较的摘要或书目信息")
            else:
                reasons.append("存在可回放全文证据；需语义核验论断、适用条件与证据之间的关系")
                if direct_quote["detected"]:
                    normalized_evidence = _normalized_phrase(evidence_text)
                    quote_matches = [
                        quote for quote in direct_quote["spans"]
                        if _normalized_phrase(quote) and _normalized_phrase(quote) in normalized_evidence
                    ]
                    quote_status = "matched" if quote_matches else "mismatch"
                    if quote_status == "matched":
                        support_status = "not_verifiable"
                        reasons.append("直接引文可定位；引文与作者论断的支持关系仍需语义核验")
                    else:
                        support_status = "quote_mismatch"
                        reasons.append("直接引文未在全文证据中定位，需人工核验")
                else:
                    reasons.append("词汇重叠率不用于推断支持、部分支持或反证")
            confidence = "low" if evidence or source_tokens else "none"
            supports.append({
                "cite_key": key,
                # ``status`` is retained for callers that only need to know
                # whether the reference key exists.  ``support_status`` is the
                # semantic result used by REQ-013.
                "status": "available",
                "support_status": support_status,
                "source_type": ref.get("source_type"),
                "reference_version": ref.get("reference_version"),
                "evidence": evidence,
                "evidence_text": evidence_text,
                "source_identity": evidence[0].get("source_identity", "") if evidence else "",
                "confidence": confidence,
                "overlap_terms": overlap,
                "matched_terms": matched_terms,
                "lexical_similarity": lexical_similarity,
                "evidence_overlap_terms": len(evidence_matched_terms),
                "evidence_matched_terms": evidence_matched_terms,
                "evidence_overlap_ratio": evidence_overlap_ratio,
                "direct_quote_match_status": quote_status,
                "expression_comparison": {
                    "source_phrases": source_phrases,
                    "mode": "evidence_text_only" if evidence else "metadata_or_abstract_only",
                    "matched_terms": matched_terms,
                    "evidence_matched_terms": evidence_matched_terms,
                    "claim_type": claim_type,
                },
                "reasons": reasons,
            })
        role = "support" if cited else "expression_candidate"
        claims.append({
            "id": f"claim-{index}",
            "text": sentence,
            "char_start": claim_start,
            "char_end": claim_end,
            "citation_keys": cited,
            "claim_type": claim_type,
            "conditions": conditions,
            "direct_quote": direct_quote,
            "role": role,
            "supports": supports,
            "status": "needs_human_review" if cited else "uncited",
        })
    source_pool: list[dict[str, Any]] = []
    for key, ref in sorted(refs.items()):
        evidence_text, evidence = _evidence_payload(_evidence_for_reference(conn, ref))
        source_id = str(ref.get("reference_id") or f"cite:{key}:{int(ref.get('reference_version') or 0)}")
        source_pool.append({
            "source_id": source_id,
            "cite_key": key,
            "reference_id": ref.get("reference_id", ""),
            "reference_version": int(ref.get("reference_version") or 0),
            "title": ref.get("title", ""),
            "authors": ref.get("authors", ""),
            "year": ref.get("year", ""),
            "abstract": ref.get("abstract", ""),
            "full_text_presence": ref.get("full_text_presence", "metadata_only") if evidence else "metadata_only",
            "evidence": evidence,
            "evidence_text": evidence_text,
        })
    uncited = [item for item in claims if item["status"] == "uncited"]
    weak = [item for item in claims if any(s.get("confidence") in {"low", "none"} or s.get("support_status") in {"not_supported", "partial", "contradicted", "not_verifiable", "metadata_only", "quote_mismatch", "missing"} for s in item["supports"])]
    reference_context = [
        {
            "cite_key": item.get("cite_key"),
            "title": item.get("title", ""),
            "authors": item.get("authors", ""),
            "year": item.get("year", ""),
            "source_type": item.get("source_type"),
            "reference_version": item.get("reference_version"),
            "full_text_presence": item.get("full_text_presence", "metadata_only"),
            "confidence": item.get("confidence", "lowered"),
        }
        for item in refs.values()
    ]
    return {
        "project_id": project_id,
        "target_path": target_path or "__all_tex__",
        "source": {"kind": "saved_content", "project_revision": int(project["project_revision"] if project else 0)},
        "context": {"outline": outline["content"] if outline else "", "logic": logic, "terms": terms, "references": reference_context, "scope": "target_file" if target_path else "full_saved_project_context", "read_only": True},
        "claims": claims,
        "source_pool": source_pool,
        "summary": {
            "claim_count": len(claims),
            "uncited_count": len(uncited),
            "weak_support_count": len(weak),
            "missing_citation_count": sum(1 for item in claims for support in item["supports"] if support.get("status") == "missing"),
            "support_status_counts": {
                status: sum(1 for item in claims for support in item["supports"] if support.get("support_status") == status)
                for status in ("supported", "partial", "contradicted", "not_supported", "not_verifiable", "metadata_only", "missing", "quote_mismatch")
            },
        },
        "policy": {
            "automatic_writes": False,
            "requires_human_review": True,
            "full_text_preferred": True,
            "metadata_only_confidence": "lowered",
            "semantic_statuses": ["supported", "partial", "contradicted", "not_supported", "not_verifiable", "metadata_only", "missing", "quote_mismatch"],
        },
    }

