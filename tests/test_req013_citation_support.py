import json

import app
from tests.planning_helper import complete_planning


def _project(client):
    response = client.post("/api/projects", json={"name": "文献对照测试", "domain": "材料科学"})
    assert response.status_code == 200, response.text
    project_id = response.json()["project"]["id"]
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(f"/api/projects/{project_id}/workflow/commands", json={"command": "start", "request_id": f"start-{project_id}", "base_version": workflow["version"]})
    assert started.status_code == 200, started.text
    entered = client.post(f"/api/projects/{project_id}/human-revision", json={"workflow_version": started.json()["version"]})
    assert entered.status_code == 200, entered.text
    return project_id


def _linked_reference(client, project_id, *, abstract=""):
    reference = client.post(
        "/api/references",
        json={"title": "Graphene stability study", "authors": "Smith", "year": "2024", "abstract": abstract},
    )
    assert reference.status_code == 200, reference.text
    ref_id = reference.json()["id"]
    linked = client.post(
        f"/api/projects/{project_id}/references/{ref_id}",
        json={"request_id": f"link-{ref_id}", "reference_version": 1, "cite_key": "graphene2024"},
    )
    assert linked.status_code == 200, linked.text
    return ref_id


def _attach_full_text(client, project_id, ref_id, text):
    """Create an imported evidence row and refresh the project's citation snapshot."""
    import_id = f"import-{ref_id}"
    evidence_id = f"evidence-{ref_id}"
    stamp = app.now()
    conn = app.db()
    conn.execute(
        "INSERT INTO oa_imports(id,project_id,idempotency_key,doi,title,source_url,oa_url,access_status,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (import_id, project_id, f"idempotency-{ref_id}", "10.1000/graphene", "Graphene stability study", "https://example.org/graphene", "https://example.org/graphene.pdf", "open_access", "imported", stamp, stamp),
    )
    conn.execute(
        "INSERT INTO external_evidence(id,project_id,import_id,source_url,content,fragments_json,attachment_path,confirmed_at,source_version) VALUES(?,?,?,?,?,?,?,?,?)",
        (evidence_id, project_id, import_id, "https://example.org/graphene.pdf", text, json.dumps([{"location": "page:1", "text": text}], ensure_ascii=False), "", stamp, "parser-test"),
    )
    conn.commit()
    conn.close()
    updated = client.put(
        f"/api/references/{ref_id}",
        json={"base_version": 1, "changes": {"full_text_presence": "full_text_available", "attachment_status": "retained", "confidence": "normal"}},
    )
    assert updated.status_code == 200, updated.text
    attached = client.post(
        f"/api/projects/{project_id}/references/{ref_id}/attachments",
        json={"project_id": project_id, "external_evidence_id": evidence_id, "base_version": 2},
    )
    assert attached.status_code == 200, attached.text
    refreshed = client.post(
        f"/api/projects/{project_id}/references/{ref_id}/refresh",
        json={"request_id": f"refresh-{ref_id}", "reference_version": 3, "base_version": 1},
    )
    assert refreshed.status_code == 200, refreshed.text


def test_citation_check_reports_support_and_uncited_claims(client):
    project_id = _project(client)
    client.post(f"/api/projects/{project_id}/refs", json={"cite_key": "smith2024", "title": "Graphene stability study", "authors": "Smith", "year": "2024"})
    content = "Graphene stability improves under controlled conditions\\cite{smith2024}. A second claim has no source."
    result = client.post(f"/api/projects/{project_id}/citation-check", json={"content": content}).json()
    assert result["summary"]["claim_count"] >= 2
    cited = next(item for item in result["claims"] if "smith2024" in item["citation_keys"])
    assert cited["role"] == "support"
    assert cited["supports"][0]["confidence"] in {"low", "medium", "high"}
    assert any(item["status"] == "uncited" for item in result["claims"])


def test_compare_keeps_legacy_missing_citation_report_and_adds_semantic_review(client):
    project_id = _project(client)
    client.put(f"/api/projects/{project_id}/files", json={"path": "main.tex", "content": "A claim\\cite{missing-key}.", "base_version": 1})
    result = client.post(f"/api/projects/{project_id}/compare").json()
    assert "missing-key" in result["missing"]
    assert "semantic_review" in result and result["semantic_review"]["summary"]["missing_citation_count"] == 1


def test_lexical_overlap_never_claims_semantic_support_without_authorization(client):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id)
    _attach_full_text(client, project_id, ref_id, "Graphene stability improves by 20% under controlled conditions.")
    result = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"content": r"Graphene stability improves by 20% under controlled conditions\cite{graphene2024}."},
    ).json()
    claim = result["claims"][0]
    support = claim["supports"][0]
    assert claim["claim_type"] == "quantitative"
    assert "under controlled conditions" in claim["conditions"]
    assert support["support_status"] == "not_verifiable"
    assert support["confidence"] == "low"
    assert support["evidence_text"]
    assert support["source_identity"]
    assert support["evidence"][0]["evidence_text_sha256"]
    assert result["semantic_review"]["status"] == "consent_required"
    assert result["summary"]["support_status_counts"]["not_verifiable"] == 1


def test_claim_extraction_ignores_latex_structure_commands(client):
    project_id = _project(client)
    result = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"content": r"\documentclass{article} \usepackage[utf8]{inputenc} \begin{document} Graphene stability improves under controlled conditions. \section{Introduction} \end{document}"},
    )
    assert result.status_code == 200, result.text
    claims = result.json()["claims"]
    assert [item["text"] for item in claims] == ["Graphene stability improves under controlled conditions."]


def test_direct_quote_and_expression_mismatch_are_explicit(client):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id)
    _attach_full_text(client, project_id, ref_id, "The study states: Graphene stability improves under controlled conditions.")
    matched = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"content": r'The study states "Graphene stability improves under controlled conditions"\cite{graphene2024}.'},
    ).json()["claims"][0]["supports"][0]
    assert matched["direct_quote_match_status"] == "matched"
    assert matched["support_status"] == "not_verifiable"
    mismatched = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"content": r'The study states "Graphene stability decreases under controlled conditions"\cite{graphene2024}.'},
    ).json()["claims"][0]["supports"][0]
    assert mismatched["direct_quote_match_status"] == "mismatch"
    assert mismatched["support_status"] == "quote_mismatch"


def test_metadata_only_and_full_text_overlap_do_not_replace_semantic_adjudication(client):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id, abstract="Graphene stability improves under controlled conditions.")
    metadata = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"content": r"Graphene stability improves under controlled conditions\cite{graphene2024}."},
    ).json()["claims"][0]["supports"][0]
    assert metadata["support_status"] == "metadata_only"
    assert metadata["confidence"] != "high"

    _attach_full_text(client, project_id, ref_id, "Graphene stability.")
    partial = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"content": r"Graphene stability improves under controlled conditions\cite{graphene2024}."},
    ).json()["claims"][0]["supports"][0]
    assert partial["support_status"] == "not_verifiable"
    assert partial["confidence"] == "low"

    # A second reference demonstrates that an available full text can still
    # fail to support the claim; availability alone must not imply support.
    # The helper uses a fixed cite key, so use a separate project for the
    # negative case to preserve the link uniqueness contract.
    negative_project = _project(client)
    other_id = _linked_reference(client, negative_project)
    _attach_full_text(client, negative_project, other_id, "Catalyst morphology changes under vacuum.")
    unsupported = client.post(
        f"/api/projects/{negative_project}/citation-check",
        json={"content": r"Graphene stability improves under controlled conditions\cite{graphene2024}."},
    ).json()["claims"][0]["supports"][0]
    assert unsupported["support_status"] == "not_verifiable"


def test_semantic_preview_requires_explicit_consent_and_exact_evidence_anchor(client, monkeypatch):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id)
    _attach_full_text(client, project_id, ref_id, "Graphene stability improves under controlled conditions.")
    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    source_text = r"Graphene stability improves under controlled conditions\cite{graphene2024}."
    saved = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": source_text, "base_version": current["version"]},
    )
    assert saved.status_code == 200, saved.text
    conn = app.db()
    conn.execute("UPDATE projects SET ai_base_url=?,model=? WHERE id=?", ("http://127.0.0.1:8000/v1", "local-semantic-test", project_id))
    conn.commit()
    conn.close()

    called = []
    def fake_ai(*args, **kwargs):
        called.append((args, kwargs))
        prompt = args[1]
        preview_data = json.loads(prompt.split("审查输入 JSON：\n", 1)[1])
        claim = preview_data["claims"][0]
        source = next(item for item in preview_data["sources"] if item["cite_key"] == "graphene2024")
        fragment = source["evidence"][0]
        answer = {
            "findings": [{
                "claim_id": claim["claim_id"],
                "sources": [{
                    "source_id": source["source_id"], "relation": "supports", "role": "support",
                    "evidence_id": fragment["evidence_id"], "fragment_id": fragment["fragment_id"],
                    "quoted_text": "Graphene stability improves under controlled conditions.",
                    "rationale": "论断与来源在研究对象、方向和条件上相符。",
                    "condition_assessment": "条件一致。",
                }],
                "expression_examples": [{
                    "source_id": source["source_id"], "evidence_id": fragment["evidence_id"],
                    "fragment_id": fragment["fragment_id"],
                    "text": "Graphene stability improves under controlled conditions",
                    "rationale": "来源中的表达范例。",
                }],
                "suggestion": {
                    "text": source_text,
                    "reasons": ["保留原意和条件。"],
                    "meaning_change_text": "",
                },
            }]
        }
        return {"ok": True, "content": json.dumps(answer), "model": "local-semantic-test", "usage": {"completion_tokens": 50, "total_tokens": 300}}

    monkeypatch.setattr(app, "call_ai", fake_ai)
    preview = client.post(
        f"/api/projects/{project_id}/citation-check/preview",
        json={"request_id": "semantic-consent-1", "target_path": "main.tex"},
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["model"]["ready"] is True
    assert preview_body["outbound_preview"]["claims"][0]["text"] == source_text
    assert called == []

    local = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"request_id": "semantic-local-only", "target_path": "main.tex"},
    )
    assert local.status_code == 200, local.text
    assert local.json()["semantic_review"]["status"] == "consent_required"
    assert called == []

    stale = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={
            "request_id": "semantic-consent-1", "target_path": "main.tex", "consent_to_send": True,
            "preview_fingerprint": "stale-preview",
        },
    )
    assert stale.status_code == 409
    assert called == []

    completed = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={
            "request_id": preview_body["request_id"], "target_path": "main.tex",
            "source_ids": preview_body["source_scope"], "consent_to_send": True,
            "preview_fingerprint": preview_body["preview_fingerprint"],
        },
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["semantic_review"]["status"] == "completed"
    support = body["claims"][0]["supports"][0]
    assert support["support_status"] == "supported"
    assert support["confidence"] == "medium"
    assert body["claims"][0]["semantic_review"]["suggestion"]["text"] == source_text
    assert body["policy"]["automatic_writes"] is False
    handoff = client.post(
        f"/api/projects/{project_id}/citation-check/runs/{body['run_id']}/claims/{body['claims'][0]['id']}/handoff",
        json={"request_id": "semantic-handoff-1", "base_version": saved.json()["version"], "confirm_handoff": True},
    )
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["automatic_write"] is False
    assert handoff.json()["status"] == "draft_saved_for_review"
    unchanged = client.get(f"/api/projects/{project_id}").json()
    assert next(item for item in unchanged["files"] if item["path"] == "main.tex")["content"] == source_text
    draft = client.get(f"/api/projects/{project_id}/human-revision/drafts").json()
    assert draft and draft[0]["content"] == source_text
    replay_handoff = client.post(
        f"/api/projects/{project_id}/citation-check/runs/{body['run_id']}/claims/{body['claims'][0]['id']}/handoff",
        json={"request_id": "semantic-handoff-1", "base_version": saved.json()["version"], "confirm_handoff": True},
    )
    assert replay_handoff.status_code == 200 and replay_handoff.json()["idempotent_replay"] is True
    replay = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={
            "request_id": preview_body["request_id"], "target_path": "main.tex",
            "source_ids": preview_body["source_scope"], "consent_to_send": True,
            "preview_fingerprint": preview_body["preview_fingerprint"],
        },
    )
    assert replay.status_code == 200 and replay.json()["run_id"] == body["run_id"]
    assert len(called) == 1


def test_semantic_result_rejects_unlisted_or_unanchored_evidence(client, monkeypatch):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id)
    _attach_full_text(client, project_id, ref_id, "Graphene stability improves under controlled conditions.")
    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    text = r"Graphene stability improves under controlled conditions\cite{graphene2024}."
    client.put(f"/api/projects/{project_id}/files", json={"path": "main.tex", "content": text, "base_version": current["version"]})
    conn = app.db()
    conn.execute("UPDATE projects SET ai_base_url=?,model=? WHERE id=?", ("http://127.0.0.1:8000/v1", "local-semantic-test", project_id))
    conn.commit()
    conn.close()
    monkeypatch.setattr(app, "call_ai", lambda *_args, **_kwargs: {
        "ok": True,
        "content": json.dumps({"findings": [{"claim_id": "claim-1", "sources": [{
            "source_id": "invented-source", "relation": "supports", "role": "support",
            "evidence_id": "invented-evidence", "fragment_id": "invented-fragment",
            "quoted_text": "invented", "rationale": "fabricated",
        }]}]}),
        "usage": {},
    })
    preview = client.post(f"/api/projects/{project_id}/citation-check/preview", json={"request_id": "semantic-anchor", "target_path": "main.tex"}).json()
    result = client.post(f"/api/projects/{project_id}/citation-check", json={
        "request_id": preview["request_id"], "target_path": "main.tex", "source_ids": preview["source_scope"],
        "consent_to_send": True, "preview_fingerprint": preview["preview_fingerprint"],
    })
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["semantic_review"]["rejected_unanchored_findings"] == 1
    assert body["claims"][0]["supports"][0]["support_status"] == "not_verifiable"


def test_semantic_result_is_discarded_if_source_permission_changes_during_call(client, monkeypatch):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id)
    _attach_full_text(client, project_id, ref_id, "Graphene stability improves under controlled conditions.")
    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    text = r"Graphene stability improves under controlled conditions\cite{graphene2024}."
    client.put(f"/api/projects/{project_id}/files", json={"path": "main.tex", "content": text, "base_version": current["version"]})
    conn = app.db()
    conn.execute("UPDATE projects SET ai_base_url=?,model=? WHERE id=?", ("http://127.0.0.1:8000/v1", "local-semantic-test", project_id))
    conn.commit()
    conn.close()
    def revoke_while_responding(*_args, **_kwargs):
        conn = app.db()
        conn.execute("UPDATE oa_imports SET status='cancelled' WHERE id=?", (f"import-{ref_id}",))
        conn.commit()
        conn.close()
        return {"ok": True, "content": json.dumps({"findings": []}), "usage": {}}
    monkeypatch.setattr(app, "call_ai", revoke_while_responding)
    preview = client.post(f"/api/projects/{project_id}/citation-check/preview", json={"request_id": "semantic-revoked-race", "target_path": "main.tex"}).json()
    result = client.post(f"/api/projects/{project_id}/citation-check", json={
        "request_id": preview["request_id"], "target_path": "main.tex", "source_ids": preview["source_scope"],
        "consent_to_send": True, "preview_fingerprint": preview["preview_fingerprint"],
    })
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "citation_result_stale_after_provider"


def test_citation_reports_are_persisted_version_bound_and_idempotent(client):
    project_id = _project(client)
    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    content = r"Graphene stability improves under controlled conditions\cite{missing-key}."
    saved = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": content, "base_version": current["version"]},
    )
    assert saved.status_code == 200, saved.text

    first = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"request_id": "citation-report-1", "target_path": "main.tex"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["run_id"] and body["source_version"] == saved.json()["version"]
    replay = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"request_id": "citation-report-1", "target_path": "main.tex"},
    )
    assert replay.status_code == 200 and replay.json()["run_id"] == body["run_id"]
    history = client.get(f"/api/projects/{project_id}/citation-check/runs")
    assert history.status_code == 200 and history.json()[0]["run_id"] == body["run_id"]

    changed = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": content + "\nAdditional saved claim.", "base_version": saved.json()["version"]},
    )
    assert changed.status_code == 200
    historical = client.get(f"/api/projects/{project_id}/citation-check/runs/{body['run_id']}")
    assert historical.status_code == 200
    assert historical.json()["stale"] is True
    conflict = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"request_id": "citation-report-1", "target_path": "main.tex"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "citation_check_idempotency_conflict"


def test_historical_citation_report_redacts_revoked_full_text_evidence(client):
    project_id = _project(client)
    ref_id = _linked_reference(client, project_id)
    _attach_full_text(client, project_id, ref_id, "Graphene stability improves under controlled conditions.")
    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    content = r"Graphene stability improves under controlled conditions\cite{graphene2024}."
    saved = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": content, "base_version": current["version"]},
    )
    assert saved.status_code == 200, saved.text
    report = client.post(
        f"/api/projects/{project_id}/citation-check",
        json={"request_id": "citation-revoke-report", "target_path": "main.tex"},
    )
    assert report.status_code == 200, report.text
    support = report.json()["claims"][0]["supports"][0]
    assert "Graphene stability improves" in support["evidence_text"]

    import_id = f"import-{ref_id}"
    cancelled = client.post(f"/api/projects/{project_id}/imports/{import_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    historical = client.get(
        f"/api/projects/{project_id}/citation-check/runs/{report.json()['run_id']}"
    )
    assert historical.status_code == 200
    revoked_support = historical.json()["claims"][0]["supports"][0]
    assert revoked_support["support_status"] == "metadata_only"
    assert revoked_support["evidence_text"] == ""
    assert revoked_support["expression_comparison"]["source_phrases"] == []

