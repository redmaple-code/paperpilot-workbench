import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PWA_AI_BASE_URL", "https://example.invalid")
os.environ.setdefault("PWA_AI_API_KEY", "test-key")
os.environ.setdefault("PWA_AI_MODEL", "deepseek-chat")

import app
from tests.planning_helper import complete_planning


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app, "PROJECTS_DIR", tmp_path / "projects")
    app.PROJECTS_DIR.mkdir()
    app.init_db()
    from tests.provider_fixtures import install_default_provider_fixture
    install_default_provider_fixture()
    return TestClient(app.app)


def create(client):
    r = client.post("/api/projects", json={"name": "测试论文", "domain": "材料科学", "style": "Nature"})
    assert r.status_code == 200
    pid = r.json()["project"]["id"]
    complete_planning(client, pid)
    workflow = client.get(f"/api/projects/{pid}/workflow").json()
    started = client.post(f"/api/projects/{pid}/workflow/commands", json={"command": "start", "request_id": f"start-{pid}", "base_version": workflow["version"]})
    assert started.status_code == 200
    entered = client.post(f"/api/projects/{pid}/human-revision", json={"workflow_version": started.json()["version"]})
    assert entered.status_code == 200
    return pid


def create_legacy_project():
    pid = "legacy-test-project"
    stamp = app.now()
    conn = app.db()
    conn.execute(
        "INSERT INTO projects(id,name,domain,style,citation_style,created_at,updated_at,model,ai_base_url) VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, "旧论文", "材料科学", "Nature", "", stamp, stamp, "", ""),
    )
    conn.commit()
    conn.close()
    return pid


def test_project_file_and_outline_flow(client):
    pid = create(client)
    p = client.get(f"/api/projects/{pid}").json()
    assert {x["path"] for x in p["files"]} == {"main.tex", "references.bib"}
    assert client.put(f"/api/projects/{pid}/files", json={"path":"main.tex","content":"\\documentclass{article}\n\\begin{document}Hi\\end{document}","base_version":1}).json()["ok"]
    assert client.put(f"/api/projects/{pid}/outline", json={"content":"1. Introduction","base_version":1}).json()["ok"]


def test_markdown_latex_roundtrip(client):
    latex = client.post("/api/convert", json={"direction":"markdown_to_latex","source":"# Title\n\n**bold**"}).json()["content"]
    assert "\\section{Title}" in latex and "\\textbf{bold}" in latex
    md = client.post("/api/convert", json={"direction":"latex_to_markdown","source":latex}).json()["content"]
    assert "# Title" in md and "**bold**" in md


def test_knowledge_and_citation_compare(client):
    pid = create(client)
    client.post(f"/api/projects/{pid}/terms", json={"term":"graphene","definition":"二维材料"})
    client.post(f"/api/projects/{pid}/corpus", json={"title":"实验记录","content":"稳定性测试语料"})
    client.post(f"/api/projects/{pid}/refs", json={"cite_key":"smith2024","title":"A study"})
    client.post(f"/api/projects/{pid}/refs", json={"cite_key":"section2025","title":"A section study"})
    client.put(f"/api/projects/{pid}/files", json={"path":"main.tex","content":"Text \\cite{smith2024,missing2020}.","base_version":1})
    client.put(f"/api/projects/{pid}/files", json={"path":"sections/results.tex","content":"Section evidence \\cite{section2025}.","base_version":0})
    r = client.post(f"/api/projects/{pid}/compare").json()
    assert "missing2020" in r["missing"] and "smith2024" in r["cited"] and "section2025" in r["cited"]
    assert client.get(f"/api/projects/{pid}").json()["corpus"][0]["title"] == "实验记录"


def test_compile_pdf(client):
    pid = create(client)
    client.put(f"/api/projects/{pid}/files", json={"path":"main.tex","content":"\\documentclass{article}\n\\begin{document}Hello\\end{document}","base_version":1})
    response = client.post(f"/api/projects/{pid}/compile")
    assert response.status_code == 200, response.text
    r = response.json()
    assert r["ok"] is True and r["build_job"]["id"]
    job = client.get(r["status_url"]).json()
    if job["status"] == "succeeded":
        assert client.get(f"/api/projects/{pid}/pdf").status_code == 200
    else:
        assert job["status"] == "failed"
        assert client.get(f"/api/projects/{pid}/pdf").status_code == 404


def test_ai_config_is_redacted(client):
    r = client.get("/api/config").json()
    assert r["configured"] is True and "api_key" not in r and "key" not in r


def test_project_settings_logic_and_svg(client):
    pid = create(client)
    updated = client.put(f"/api/projects/{pid}", json={"domain":"生物信息学","style":"严谨","model":"deepseek-v4-flash"}).json()
    assert updated["project"]["model"] == "deepseek-v4-flash"
    node = client.post(f"/api/projects/{pid}/logic", json={"kind":"claim","label":"方法提高稳定性","links":"evidence-1"}).json()
    assert node["label"] == "方法提高稳定性" and node["links"] == "evidence-1"
    image = client.post(f"/api/projects/{pid}/draw", json={"title":"流程图","labels":["问题","方法","结果"]}).json()
    assert image["ok"] and image["run"]["status"] == "awaiting_confirmation"
    assert image["body_written"] is False
    assert client.get(f"/api/projects/{pid}/drawing/assets").json()["assets"] == []


def test_copy_resource_between_isolated_projects(client, monkeypatch):
    source = create(client)
    target = create(client)
    term = client.post(f"/api/projects/{source}/terms", json={"term":"Bayes","definition":"概率推断框架","aliases":"贝叶斯"}).json()
    draft_copy = client.post(f"/api/projects/{target}/copy-resource", json={
        "source_project_id": source,
        "resource_type": "term",
        "resource_id": term["id"],
    })
    assert draft_copy.status_code == 409
    project = client.get(f"/api/projects/{source}").json()["project"]
    submitted = client.post(f"/api/projects/{source}/lifecycle", json={
        "command": "submit_review", "request_id": "source-submit", "base_version": project["project_revision"],
    })
    assert submitted.status_code == 200, submitted.text
    def successful_compile(*_args, **_kwargs):
        directory = Path(_args[3])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'main.pdf').write_bytes(b'isolated release artifact fixture')
        return {'ok': True, 'log': '', 'pdf_url': '__PDF__'}
    monkeypatch.setattr(app, "compile_project_files", successful_compile)
    checked = client.post(f"/api/projects/{source}/compile", json={'purpose': 'submitted'})
    assert checked.status_code == 200
    assert client.get(f"/api/projects/{source}/lifecycle").json()["release_check"]["status"] == "passed"
    project = client.get(f"/api/projects/{source}").json()["project"]
    published = client.post(f"/api/projects/{source}/lifecycle", json={
        "command": "publish", "request_id": "source-publish", "base_version": project["project_revision"],
    })
    assert published.status_code == 200, published.text
    copied = client.post(f"/api/projects/{target}/copy-resource", json={
        "source_project_id": source,
        "resource_type": "term",
        "resource_id": term["id"],
    }).json()
    assert copied["ok"]
    assert copied["source"]["lifecycle_state"] == "published"
    target_data = client.get(f"/api/projects/{target}").json()
    copied_term = next(item for item in target_data["terms"] if item["term"] == "Bayes")
    assert copied_term["aliases"] == "贝叶斯" and copied_term["source_project_id"] == source and copied_term["source_resource_id"] == term["id"]
    assert copied_term["source_project_state"] == "published"
    assert copied_term["source_project_revision"] == copied["source"]["project_revision"]


def test_file_lifecycle_and_project_ai_override(client, monkeypatch):
    pid = create(client)
    updated = client.put(f"/api/projects/{pid}", json={"ai_base_url": "http://local-model:11434/v1", "model": "local-test"}).json()
    assert updated["project"]["ai_base_url"] == "http://local-model:11434/v1"
    assert client.put(f"/api/projects/{pid}/files", json={"path":"sections/methods.tex","content":"Methods","base_version":0}).json()["ok"]
    assert client.delete(f"/api/projects/{pid}/files/sections/methods.tex?base_version=1").json()["ok"]
    assert client.delete(f"/api/projects/{pid}/files/main.tex").status_code == 400

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"choices":[{"message":{"content":"可编辑伴写结果"}}]}'

        def json(self):
            return {"choices": [{"message": {"content": "可编辑伴写结果"}}]}

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        # The adapter sends UTF-8 bytes now (json= escapes non-ASCII and the
        # gateway rejects it), so the test decodes the body it actually sends.
        raw = kwargs.get("data")
        captured["body"] = json.loads(raw) if isinstance(raw, (bytes, bytearray)) else kwargs.get("json")
        return FakeResponse()

    monkeypatch.setattr(app.requests, "post", fake_post)
    assert client.post(f"/api/projects/{pid}/ai", json={"action":"companion", "content":"A sentence"}).status_code == 409
    legacy = create_legacy_project()
    client.put(f"/api/projects/{legacy}", json={"ai_base_url": "http://local-model:11434/v1", "model": "local-test"})
    assert client.post(f"/api/projects/{legacy}/ai", json={"action":"companion", "content":"A sentence"}).status_code==409
    assert not captured
    result=app.call_ai('companion','','A sentence','','local-test','http://127.0.0.1:11434/v1',context='领域：材料科学')
    assert result["ok"] and result["content"] == "可编辑伴写结果"
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert "领域：材料科学" in captured["body"]["messages"][1]["content"]
    for action in ("write", "continue", "edit", "outline"):
        assert client.post(f"/api/projects/{legacy}/ai", json={"action":action, "content":"A sentence"}).status_code==409
    monkeypatch.delenv("PWA_AI_API_KEY", raising=False)
    local_result = app.call_ai("write", "", "Local model test", "", "local-test", "http://127.0.0.1:11434/v1")
    assert local_result["ok"]


def test_project_search_metadata_and_archive(client):
    pid = create(client)
    assert client.put(f"/api/projects/{pid}", json={"domain":"天体物理","style":"严谨"}).json()["project"]["domain"] == "天体物理"
    archived = client.put(f"/api/projects/{pid}/archive").json()
    assert archived["ok"] and archived["archived"] is True
    restored = client.put(f"/api/projects/{pid}/archive").json()
    assert restored["archived"] is False
