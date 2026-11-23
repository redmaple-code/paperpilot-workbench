"""Stage boundaries, honest retrieval status, revision branches and application safety."""
import json
import uuid
import pytest
import app
from paperpilot.services import research_design, research_stage_policies, provider_runtime
from tests.test_req022_assets import project
from tests.test_req021_startup import read, review_and_confirm


def seed(client, pid, stage="STAGE-02", *, level="unknown"):
    view = read(client, pid)
    field = next(iter(research_stage_policies.policy(stage)["fields"]))
    candidate_id, card_id = uuid.uuid4().hex, uuid.uuid4().hex
    card = {"id": card_id, "stage_id": stage, "policy_version": research_stage_policies.POLICY_VERSION, "payload": {field: "Specific proposed draft"}, "limitations": "Not verified", "dedup_report": {"level": level}}
    conn = app.db()
    with conn:
        conn.execute("INSERT INTO research_design_candidates(id,project_id,stage_id,request_id,cards_json,base_workflow_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (candidate_id, pid, stage, candidate_id, json.dumps([card]), view["workflow_version"], "2026-11-08", "2026-11-08"))
    conn.close()
    return {"request_id": uuid.uuid4().hex, "candidate_id": candidate_id, "card_id": card_id, "field": field, "expected_workflow_version": view["workflow_version"]}


@pytest.mark.parametrize("stage", ["STAGE-01", "STAGE-02", "STAGE-03", "STAGE-04", "STAGE-05"])
def test_stage_preview_and_generated_schema(client, monkeypatch, stage):
    pid = project(client)
    def execute(*args, **kwargs):
        fields = research_stage_policies.policy(stage)["fields"]
        card = {"idea": "A falsifiable proposal", "payload": {next(iter(fields)): "Concrete draft"}, "title": "Stage proposal", "limitations": "Not verified"}
        return {"ok": True, "content": json.dumps({"cards": [card]})}
    monkeypatch.setattr(provider_runtime, "execute_call", execute)
    url = f"/api/projects/{pid}/workflow/research-design"
    preview = client.post(url+"/preview", json={"stage_id": stage})
    assert preview.status_code == 200, preview.text
    assert preview.json()["outbound_preview"]["阶段"] == stage
    response = client.post(url, json={"request_id": uuid.uuid4().hex, "stage_id": stage, "consent_to_send": True, "possible_cost_confirmed": True, "preview_fingerprint": preview.json()["preview_fingerprint"]})
    assert response.status_code == 200, response.text
    card = response.json()["cards"][0]
    assert card["stage_id"] == stage and card["dedup_status"] == "not_checked"
    assert set(card["target_fields"]) <= set(research_stage_policies.policy(stage)["fields"])


def test_later_stage_apply_is_draft_only_and_idempotent(client):
    pid = project(client)
    data = seed(client, pid, "STAGE-04")
    url = f"/api/projects/{pid}/workflow/research-design/apply"
    applied = client.post(url, json=data)
    assert applied.status_code == 200, applied.text
    view = read(client, pid)
    assert view["plan"]["current_index"] == 0 and not view["confirmed_context"]
    assert view["plan"]["stages"][3]["fields"]["terms"] == "Specific proposed draft"
    replay = client.post(url, json=data)
    assert replay.status_code == 200 and replay.json()["idempotent_replay"]
    assert read(client, pid)["workflow_version"] == view["workflow_version"]


def test_target_whitelist_cross_project_and_stale_candidate(client):
    pid, other = project(client), project(client, "Other")
    data = seed(client, pid)
    url = f"/api/projects/{pid}/workflow/research-design/apply"
    assert client.post(url, json={**data, "field": "contribution"}).status_code == 422
    assert client.post(f"/api/projects/{other}/workflow/research-design/apply", json=data).status_code == 404
    review_and_confirm(client, pid)
    assert client.post(url, json=data).status_code == 409


def test_confirmed_stage_generates_then_requires_explicit_revision(client):
    pid = project(client)
    confirmed = review_and_confirm(client, pid)
    assert client.post(f"/api/projects/{pid}/workflow/research-design/preview", json={"stage_id": "STAGE-01"}).status_code == 200
    data = seed(client, pid, "STAGE-01")
    url = f"/api/projects/{pid}/workflow/research-design/apply"
    assert client.post(url, json=data).status_code == 409
    result = client.post(url, json={**data, "acknowledge_revision": True})
    assert result.status_code == 200, result.text
    assert read(client, pid)["plan"]["stages"][0]["status"] == "draft"
    historical = client.get(f"/api/projects/{pid}/workflow/startup/history/{confirmed['workflow_version']}").json()
    assert historical["plan"]["stages"][0]["status"] == "confirmed"


def test_high_similarity_can_be_adopted_after_reason_recorded(client):
    pid = project(client)
    data = seed(client, pid, level="high")
    url = f"/api/projects/{pid}/workflow/research-design/apply"
    assert client.post(url, json=data).status_code == 422
    assert client.post(url, json={**data, "review_reason": "Different boundary conditions; sources inspected"}).status_code == 200


def test_stage_schema_rejects_foreign_fields_and_missing_limitations():
    with pytest.raises(ValueError):
        research_stage_policies.validate_cards({"cards": [{"payload": {"contribution": "wrong stage"}, "limitations": "unknown"}]}, "STAGE-02")
    with pytest.raises(ValueError):
        research_stage_policies.validate_cards({"cards": [{"payload": {"outline": "draft"}}]}, "STAGE-02")


@pytest.mark.parametrize("ok,level,expected",[(False,"unknown","keyword_only_unavailable"),(True,"unknown","keyword_only"),(True,"high","keyword_only")])
def test_dedup_unavailable_empty_and_high_are_distinct(client,monkeypatch,ok,level,expected):
    from paperpilot.services import agent_business_tools
    monkeypatch.setattr(agent_business_tools,"keyword_search",lambda query:{"ok":ok,"level":level,"sources":[]})
    pid=project(client);data=seed(client,pid)
    url=f"/api/projects/{pid}/workflow/research-design/{data['candidate_id']}/cards/{data['card_id']}"
    report=client.post(url+"/dedup",json={"query":"query","consent_to_search":True})
    assert report.status_code==200,report.text
    assert report.json()["level"]==level and report.json()["queried_at"]
    cards=client.get(f"/api/projects/{pid}/workflow/research-design").json()["candidates"][0]["cards"]
    assert cards[0]["dedup_status"]==expected


def test_rejection_persists_without_writing_stage(client):
    pid=project(client);data=seed(client,pid)
    before=read(client,pid)
    url=f"/api/projects/{pid}/workflow/research-design/{data['candidate_id']}/cards/{data['card_id']}/reject"
    assert client.post(url,json={}).json()["status"]=="rejected"
    assert read(client,pid)==before
    assert client.post(f"/api/projects/{pid}/workflow/research-design/apply",json=data).status_code==409


def test_updated_similarity_report_invalidates_existing_action_preview(client):
    from tests.test_agent_actions import preview, apply
    pid=project(client);data=seed(client,pid)
    action=client.post(f'/api/projects/{pid}/agent/actions',json={'request_id':'research-action','operation':'research','base_version':data['expected_workflow_version'],
        'payload':{k:data[k] for k in ['candidate_id','card_id','field']}}).json()
    snapshot=preview(client,pid,action)
    conn=app.db()
    with conn:
        row=conn.execute('SELECT cards_json FROM research_design_candidates WHERE id=?',(data['candidate_id'],)).fetchone()
        cards=json.loads(row[0]);cards[0]['dedup_report']={'level':'high','sources':[{'title':'Newly retrieved neighbour'}]}
        conn.execute('UPDATE research_design_candidates SET cards_json=? WHERE id=?',(json.dumps(cards),data['candidate_id']))
    conn.close()
    result=apply(client,pid,action,snapshot)
    assert result.status_code==409 and result.json()['detail']['code']=='preview_stale'
    assert not read(client,pid)['plan']['stages'][1]['fields']['outline']


def test_later_stage_preview_includes_unconfirmed_drafts_and_versioned_structure(client):
    pid = project(client)
    view = read(client, pid)
    response = client.post(f"/api/projects/{pid}/workflow/startup/commands", json={"request_id":uuid.uuid4().hex,"expected_workflow_version":view["workflow_version"],"command":"save","stage_id":"STAGE-01","fields":{"research_question":"UNCONFIRMED QUESTION"}})
    assert response.status_code == 200, response.text
    result = client.post(f"/api/projects/{pid}/workflow/research-design/preview",json={"stage_id":"STAGE-03"})
    assert result.status_code == 200, result.text
    outbound=result.json()["outbound_preview"]
    assert "UNCONFIRMED QUESTION" in json.dumps(outbound,ensure_ascii=False)
    assert outbound["已确认前缀"] == []
    structure=outbound["现有结构（不是已核验证据）"]
    assert {"outline_version","outline_nodes","logic_version","logic_nodes","logic_edges"} <= structure.keys()


def test_old_policy_candidate_stays_readable_but_cannot_be_applied(client):
    pid=project(client)
    data=seed(client,pid)
    conn=app.db()
    with conn:
        row=conn.execute('SELECT cards_json FROM research_design_candidates WHERE id=?',(data['candidate_id'],)).fetchone()
        cards=json.loads(row[0]);cards[0]['policy_version']=research_stage_policies.POLICY_VERSION-1
        conn.execute('UPDATE research_design_candidates SET cards_json=? WHERE id=?',(json.dumps(cards),data['candidate_id']))
    conn.close()
    before=read(client,pid)
    response=client.post(f'/api/projects/{pid}/workflow/research-design/apply',json=data)
    assert response.status_code==409 and response.json()['detail']['code']=='target_changed'
    assert read(client,pid)==before
    assert client.get(f'/api/projects/{pid}/workflow/research-design').json()['candidates'][0]['cards'][0]['id']==data['card_id']
