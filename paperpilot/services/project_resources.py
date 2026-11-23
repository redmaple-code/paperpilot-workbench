"""Project resource creation shared by manual endpoints and accepted AI proposals."""
import uuid
from datetime import datetime, timezone
from paperpilot.services import corpus_lifecycle


def touch(conn, project_id):
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), project_id))


def add_term(conn, project_id, payload):
    item_id = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO terms(id,project_id,term,definition,aliases,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?,?)",
                 (item_id, project_id, payload["term"], payload.get("definition", ""), payload.get("aliases", ""), "", ""))
    touch(conn, project_id)
    return {"id": item_id, **payload, "version": 1}


def add_corpus(conn, project_id, payload):
    item_id = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO corpus(id,project_id,title,content,source,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?,?)",
                 (item_id, project_id, payload["title"], payload.get("content", ""), payload.get("source", ""), "", ""))
    version = corpus_lifecycle.ensure_source_version(conn, item_id, payload.get("content", ""), payload.get("source", ""), "manual")
    corpus_lifecycle.rebuild_index(conn, project_id, item_id, version["version"])
    touch(conn, project_id)
    return {"id": item_id, **payload, "source_version": version, "version": version["version"]}
