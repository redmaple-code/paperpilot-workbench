"""Search-card identity, explicit handoff, transient storage and safe legacy wiring."""
import uuid
import app
from paperpilot.services import academic_search, agent_tools
from tests.test_req022_assets import project
from tests.test_agent_actions import create, preview, apply


def provider(monkeypatch):
    class Provider:
        name = "fixture"
        def search(self, query, *_):
            return {"results": [{"id": "https://example.org/"+query, "title": query+" first", "doi": "10.1234/"+query},
                                {"id": "https://example.org/"+query+"-second", "title": query+" second"}], "next_cursor": None}
    monkeypatch.setattr(academic_search, "provider_from_env", lambda: Provider())


def test_two_searches_preserve_original_candidate_identity_until_explicit_handoff(client, monkeypatch):
    provider(monkeypatch)
    pid = project(client)
    url = f"/api/projects/{pid}/agent/business-search"
    first = client.post(url, json={"query": "alpha", "consent_to_search": True}).json()
    second = client.post(url, json={"query": "beta", "consent_to_search": True}).json()
    card = first["cards"][1]
    conn = app.db()
    assert conn.execute("SELECT count(*) FROM academic_candidates WHERE project_id=?", (pid,)).fetchone()[0] == 0
    conn.close()
    action = create(client, pid, "reference", card["payload"])
    snap = preview(client, pid, action)
    assert snap["diff"]["title"] == "alpha second"
    applied = apply(client, pid, action, snap)
    assert applied.status_code == 200, applied.text
    draft = applied.json()["result"]["reference_draft"]
    assert draft["metadata"]["title"] == "alpha second"
    assert draft["full_text_presence"] == "metadata_only"
    conn = app.db()
    rows = conn.execute("SELECT title FROM academic_candidates WHERE project_id=?", (pid,)).fetchall()
    assert [r[0] for r in rows] == ["alpha second"]
    conn.close()


def test_read_only_search_is_rejected_and_no_oa_is_not_imported(client, monkeypatch):
    provider(monkeypatch)
    pid = project(client)
    card = client.post(f"/api/projects/{pid}/agent/business-search", json={"query": "closed", "consent_to_search": True}).json()["cards"][0]
    conn = app.db();version = conn.execute("SELECT project_revision FROM projects WHERE id=?", (pid,)).fetchone()[0];conn.close()
    response = client.post(f"/api/projects/{pid}/agent/actions", json={"request_id":uuid.uuid4().hex,"operation":"oa","base_version":version,"payload":card["payload"]})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "oa_unavailable"
    assert client.put(f"/api/projects/{pid}/archive").status_code == 200
    assert client.post(f"/api/projects/{pid}/agent/business-search", json={"query":"blocked","consent_to_search":True}).status_code in {403,409}


def test_legacy_outline_tool_only_creates_proposal(client):
    pid = project(client)
    conn = app.db()
    from paperpilot.services import outline
    before = outline.read_outline(conn, pid)
    result = agent_tools.REGISTRY["edit_outline_nodes"].handler({"command":"create","base_version":before["version"],"node":{"title":"Proposed only"}}, {"conn":conn,"project_id":pid})
    assert result["ok"] and result["card"]["kind"] == "outline_proposal"
    assert outline.read_outline(conn,pid) == before
    conn.close()


def test_oa_application_only_creates_one_request_without_fetch_or_import(client, monkeypatch):
    class Provider:
        name="fixture"
        def search(self,*_):
            return {"results":[{"id":"https://example.org/open","title":"Open sample", "open_access":{"is_oa":True},
                                "best_oa_location":{"is_oa":True,"pdf_url":"https://example.org/open.pdf"}}]}
    monkeypatch.setattr(academic_search,"provider_from_env",lambda:Provider())
    monkeypatch.setattr(app,"_fetch_oa_import",lambda *_: (_ for _ in ()).throw(AssertionError("must not fetch in action transaction")))
    pid=project(client)
    card=client.post(f"/api/projects/{pid}/agent/business-search",json={"query":"open","consent_to_search":True}).json()["cards"][0]
    action=create(client,pid,"oa",card["payload"])
    snap=preview(client,pid,action)
    result=apply(client,pid,action,snap,"oa-once")
    assert result.status_code==200,result.text
    assert result.json()["result"]["status"]=="created"
    assert apply(client,pid,action,snap,"oa-once").json()["idempotent_replay"]
    conn=app.db()
    assert conn.execute("SELECT count(*) FROM oa_imports WHERE project_id=?",(pid,)).fetchone()[0]==1
    assert conn.execute("SELECT count(*) FROM corpus WHERE project_id=?",(pid,)).fetchone()[0]==0
    conn.close()
