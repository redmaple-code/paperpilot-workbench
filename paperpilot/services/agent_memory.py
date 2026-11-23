"""Short-term (working) and long-term memory.

Long-term writes pass four gates before they are stored: new, sourced,
consequential, stable. No source means no memory — that is the whole point.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from paperpilot.services.agent_tools import keywords, overlap_ratio

KINDS = ("episodic", "semantic", "procedural", "preference")
WORKING_SCRATCHPAD_CAP = 40
EVIDENCE_KINDS = {"finding", "correction", "constraint", "question"}


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_schema(conn) -> None:
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS agent_memories(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL, content_json TEXT NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
        importance REAL NOT NULL DEFAULT 0.5, stability TEXT NOT NULL DEFAULT 'stable',
        status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT NOT NULL DEFAULT '',
        conflict_group TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, last_recalled_at TEXT NOT NULL DEFAULT ''
      );
      CREATE INDEX IF NOT EXISTS idx_agent_memories_recall
        ON agent_memories(project_id, kind, status, importance DESC);
      CREATE TABLE IF NOT EXISTS agent_memory_events(
        id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, project_id TEXT NOT NULL,
        action TEXT NOT NULL, gate_result_json TEXT NOT NULL DEFAULT '{}',
        actor TEXT NOT NULL DEFAULT 'agent', created_at TEXT NOT NULL
      );
    ''')


    columns={r['name'] for r in conn.execute('PRAGMA table_info(agent_memories)')}
    if 'approval_version' not in columns:
        conn.execute("ALTER TABLE agent_memories ADD COLUMN approval_version INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE agent_memories SET status='pending',stability='tentative' WHERE status='active'")


# ---------------- working memory ----------------

def new_working(goal: str, stage_id: str) -> dict:
    return {
        "goal": goal, "stage_id": stage_id,
        "plan": [], "scratchpad": [], "tool_cache": {},
        "open_questions": [], "evidence_ledger": [],
    }


def remember(working: dict, item: dict, seq: int) -> dict:
    """Store one working-memory note. Rejects notes without a source step."""
    if item.get("source_seq") is None:
        return {"ok": False, "error": "missing_source_seq"}
    kind = item.get("kind", "finding")
    if kind not in EVIDENCE_KINDS:
        return {"ok": False, "error": "invalid_kind"}
    note = {"kind": kind, "text": str(item.get("text", ""))[:500], "source_seq": int(item["source_seq"]),
            "importance": 0.6 if kind in {"correction", "constraint"} else 0.4}
    scratch = list(working.get("scratchpad") or [])
    if any(existing.get("text") == note["text"] for existing in scratch):
        return {"ok": True, "deduped": True}
    scratch.append(note)
    if len(scratch) > WORKING_SCRATCHPAD_CAP:
        scratch.sort(key=lambda entry: (entry.get("importance", 0), entry.get("source_seq", 0)))
        scratch = scratch[-WORKING_SCRATCHPAD_CAP:]
    working["scratchpad"] = scratch
    if kind == "question":
        questions = list(working.get("open_questions") or [])
        if note["text"] not in questions:
            questions.append(note["text"])
        working["open_questions"] = questions
    return {"ok": True, "stored": note}


def add_evidence(working: dict, items: list[dict]) -> None:
    ledger = {str(item.get("id")): item for item in (working.get("evidence_ledger") or [])}
    for item in items or []:
        marker = str(item.get("id") or item.get("doi") or item.get("title") or "")
        if not marker:
            continue
        ledger.setdefault(marker, {
            "id": marker, "title": item.get("title", ""), "year": item.get("year", ""),
            "doi": item.get("doi", ""), "source": item.get("source", ""),
        })
    working["evidence_ledger"] = list(ledger.values())


# ---------------- long-term memory ----------------

def _gate(conn, project_id: str, candidate: dict) -> dict:
    kind = candidate.get("kind")
    content = candidate.get("content") or {}
    text = json.dumps(content, ensure_ascii=False)

    # G2 first: without a source nothing is stored, regardless of quality.
    if not candidate.get("source_kind") or not candidate.get("source_id"):
        return {"pass": False, "gate": "G2", "reason": "无来源"}
    if not valid_source(conn,project_id,candidate):
        return {"pass": False, "gate": "G2", "reason": "来源不存在、未完成、不可访问或不属于当前论文"}

    rows = conn.execute(
        "SELECT id, content_json, importance FROM agent_memories "
        "WHERE project_id=? AND kind=? AND status IN ('active','pending')", (project_id, kind)).fetchall()
    best = 0.0
    conflict = ""
    for row in rows:
        ratio = overlap_ratio(text, row["content_json"] or "")
        best = max(best, ratio)
        if ratio >= 0.6 and row["content_json"] != text:
            conflict = row["id"]
    # G1: near-duplicate is dropped rather than stored twice.
    if best >= 0.85:
        return {"pass": False, "gate": "G1", "reason": "与既有记忆重复", "ratio": round(best, 3)}

    importance = float(candidate.get("importance", 0.5))
    # G3: only memory that can change a later judgement is worth keeping.
    if importance < 0.3:
        return {"pass": False, "gate": "G3", "reason": "影响不足", "importance": importance}

    # G4: uncertain statements are stored but marked tentative.
    stability = "tentative"
    return {"pass": True, "conflict_with": conflict, "importance": importance, "stability": stability}


def write(conn, project_id: str, candidate: dict, *, actor: str = "agent") -> dict:
    verdict = _gate(conn, project_id, candidate)
    if not verdict["pass"]:
        conn.execute(
            "INSERT INTO agent_memory_events(id,memory_id,project_id,action,gate_result_json,actor,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, "", project_id, "reject", json.dumps(verdict, ensure_ascii=False), actor, stamp()))
        return {"ok": False, "gate": verdict}

    memory_id = uuid.uuid4().hex
    now = stamp()
    group = ""
    if verdict.get("conflict_with"):
        group = f"conflict-{memory_id[:8]}"
        conn.execute("UPDATE agent_memories SET conflict_group=? WHERE id=? AND conflict_group=''",
                     (group, verdict["conflict_with"]))
    conn.execute(
        "INSERT INTO agent_memories(id,project_id,kind,title,content_json,source_kind,source_id,importance,"
        "stability,status,superseded_by,conflict_group,created_at,updated_at,last_recalled_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, project_id, candidate["kind"], str(candidate.get("title", ""))[:200],
         json.dumps(candidate.get("content") or {}, ensure_ascii=False),
         candidate["source_kind"], str(candidate["source_id"]), verdict["importance"],
         verdict["stability"], "pending", "", group, now, now, ""))
    conn.execute(
        "INSERT INTO agent_memory_events(id,memory_id,project_id,action,gate_result_json,actor,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, memory_id, project_id, "accept",
         json.dumps(verdict, ensure_ascii=False), actor, now))
    return {"ok": True, "id": memory_id, "gate": verdict}


def recall(conn, project_id: str, query: str, *, kinds: tuple = KINDS, limit: int = 8) -> dict:
    rows = conn.execute(
        "SELECT * FROM agent_memories WHERE project_id=? AND status='active' "
        "AND kind IN (%s) ORDER BY importance DESC, updated_at DESC LIMIT 200"
        % ",".join("?" * len(kinds)), (project_id, *kinds)).fetchall()
    if not rows:
        return {"items": [], "manifest": []}

    words = keywords(query)
    scored = []
    for row in rows:
        text = f"{row['title']} {row['content_json']}"
        if not valid_source(conn,project_id,dict(row)):
            continue
        relevance = len(words & keywords(text)) / max(1, len(words)) if words else 0.0
        importance = float(row["importance"] or 0)
        scored.append((relevance * 0.5 + importance * 0.5, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [row for _, row in scored[:limit]]
    # Keep a slice for never-recalled memories so recall cannot narrow over time.
    never = [row for _, row in scored[limit:] if not row["last_recalled_at"]][:max(1, limit // 5)]
    chosen.extend(never)

    items = [{
        "id": row["id"], "kind": row["kind"], "title": row["title"],
        "content": json.loads(row["content_json"] or "{}"),
        "source_kind": row["source_kind"], "source_id": row["source_id"],
        "conflict_group": row["conflict_group"], "stability": row["stability"],
    } for row in chosen]
    # Preview/recall is read-only. Usage belongs to the committed run manifest,
    # not an uncommitted write transaction that could break turn admission.
    return {"items": items, "manifest": [{"id": item["id"], "kind": item["kind"]} for item in items]}


def decide(conn, project_id: str, memory_id: str, action: str, *, actor: str = "user") -> dict:
    row = conn.execute("SELECT * FROM agent_memories WHERE id=? AND project_id=?",
                       (memory_id, project_id)).fetchone()
    if row is None:
        return {"ok": False, "error": "memory_not_found"}
    if action == 'approve':
        if row['status'] not in ('pending','active') or not valid_source(conn,project_id,dict(row)):
            return {'ok':False,'error':'memory_source_invalid'}
        conn.execute("UPDATE agent_memories SET status='active',approval_version=1,updated_at=? WHERE id=?",(stamp(),memory_id))
    elif action == "reject":
        conn.execute("UPDATE agent_memories SET status='rejected',updated_at=? WHERE id=?", (stamp(), memory_id))
    elif action == "delete":
        conn.execute("UPDATE agent_memories SET status='deleted',updated_at=? WHERE id=?", (stamp(), memory_id))
    elif action == "supersede":
        conn.execute("UPDATE agent_memories SET status='superseded',updated_at=? WHERE id=?", (stamp(), memory_id))
    else:
        return {"ok": False, "error": "invalid_action"}
    conn.execute(
        "INSERT INTO agent_memory_events(id,memory_id,project_id,action,gate_result_json,actor,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, memory_id, project_id, action, "{}", actor, stamp()))
    return {"ok": True}


def valid_source(conn,project_id,candidate):
    if candidate.get('source_kind')=='agent_run':
        return bool(conn.execute("SELECT 1 FROM agent_runs WHERE id=? AND project_id=? AND status='succeeded'",(candidate.get('source_id'),project_id)).fetchone())
    return False


def list_memories(conn, project_id: str, kind: str = "") -> list[dict]:
    if kind:
        rows = conn.execute("SELECT * FROM agent_memories WHERE project_id=? AND kind=? AND status!='deleted' "
                            "ORDER BY created_at DESC LIMIT 100", (project_id, kind)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agent_memories WHERE project_id=? AND status!='deleted' "
                            "ORDER BY created_at DESC LIMIT 100", (project_id,)).fetchall()
    return [{
        "id": row["id"], "kind": row["kind"], "title": row["title"],
        "content": json.loads(row["content_json"] or "{}"), "status": row["status"],
        "stability": row["stability"], "importance": row["importance"],
        "source_kind": row["source_kind"], "source_id": row["source_id"],
        "conflict_group": row["conflict_group"], "created_at": row["created_at"],
    } for row in rows]
