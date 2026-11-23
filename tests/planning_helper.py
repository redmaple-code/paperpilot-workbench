"""Shared test helper: planning is the only path into human_revision."""
from __future__ import annotations


def complete_planning(client, project_id: str) -> dict:
    """Confirm all five planning stages so the workflow reaches human_revision."""
    confirmed = None
    for _ in range(5):
        view = client.get(f"/api/projects/{project_id}/workflow/startup").json()
        definition = next(s for s in view["definitions"] if s["id"] == view["current_stage"])
        fields = {key: "test-" + key for key in definition["fields"]}
        reviewed = client.post(
            f"/api/projects/{project_id}/workflow/startup/commands",
            json={"command": "review", "request_id": f"plan-review-{view['workflow_version']}",
                  "expected_workflow_version": view["workflow_version"], "stage_id": view["current_stage"],
                  "fields": fields},
        )
        assert reviewed.status_code == 200, reviewed.text
        confirmed = client.post(
            f"/api/projects/{project_id}/workflow/startup/commands",
            json={"command": "confirm", "request_id": f"plan-confirm-{view['workflow_version']}",
                  "expected_workflow_version": reviewed.json()["workflow_version"], "stage_id": view["current_stage"]},
        )
        assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def workflow_ready_for_revision(client, project_id: str) -> dict:
    """Complete planning and return the resulting workflow projection."""
    complete_planning(client, project_id)
    return client.get(f"/api/projects/{project_id}/workflow").json()
