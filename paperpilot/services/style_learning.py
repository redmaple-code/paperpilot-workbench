"""Opt-in, versioned local style learning. It never owns manuscript writes."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import statistics
import uuid

from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.style_profile import StyleProfile

SCOPES = {"accepted_edit", "rejected_edit", "example", "feedback"}


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat()


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def initialize_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS style_learning_settings (
      project_id TEXT PRIMARY KEY REFERENCES projects(id), version INTEGER NOT NULL,
      consent_id TEXT, state TEXT NOT NULL, active_profile_id TEXT, last_error_code TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS style_learning_consents (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), version INTEGER NOT NULL,
      scopes_json TEXT NOT NULL, retention_days INTEGER NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL, revoked_at TEXT, UNIQUE(project_id,version)
    );
    CREATE TABLE IF NOT EXISTS style_learning_signals (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), consent_id TEXT NOT NULL,
      kind TEXT NOT NULL, source_version TEXT NOT NULL, before_text TEXT NOT NULL, content TEXT NOT NULL,
      retention_until TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(project_id,consent_id,kind,source_version)
    );
    CREATE TABLE IF NOT EXISTS style_learning_runs (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, consent_id TEXT NOT NULL, input_hash TEXT NOT NULL,
      signal_ids_json TEXT NOT NULL, base_profile_id TEXT, base_families_json TEXT NOT NULL,
      status TEXT NOT NULL, diagnostics_json TEXT NOT NULL DEFAULT '[]', error_code TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, claim_token TEXT NOT NULL DEFAULT '', UNIQUE(project_id,input_hash)
    );
    CREATE TABLE IF NOT EXISTS style_learning_rules (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, consent_id TEXT NOT NULL, run_id TEXT,
      family TEXT NOT NULL, version INTEGER NOT NULL, instruction TEXT NOT NULL,
      source_ids_json TEXT NOT NULL, confidence REAL NOT NULL, method_json TEXT NOT NULL,
      base_rule_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT,
      UNIQUE(project_id,family,version)
    );
    CREATE TABLE IF NOT EXISTS style_learning_profiles (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, consent_id TEXT NOT NULL,
      version INTEGER NOT NULL, parent_id TEXT, rule_ids_json TEXT NOT NULL,
      reason TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(project_id,version)
    );
    CREATE TABLE IF NOT EXISTS style_learning_events (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, event TEXT NOT NULL,
      details_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS style_learning_operations (
      project_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
      result_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(project_id,request_id)
    );
    CREATE INDEX IF NOT EXISTS idx_style_signal_project ON style_learning_signals(project_id,retention_until);
    CREATE INDEX IF NOT EXISTS idx_style_run_queue ON style_learning_runs(status,created_at);
    """)
    if "claim_token" not in {r["name"] for r in conn.execute("PRAGMA table_info(style_learning_runs)")}:
        conn.execute("ALTER TABLE style_learning_runs ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")


def begin(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def settings(conn, project_id):
    row = conn.execute("SELECT * FROM style_learning_settings WHERE project_id=?", (project_id,)).fetchone()
    return dict(row) if row else {"project_id": project_id, "version": 0, "consent_id": None, "state": "disabled", "active_profile_id": None, "last_error_code": None}


def consent(conn, project_id, *, collecting=False):
    setting = settings(conn, project_id)
    if setting["state"] in {"disabled", "paused"} or not setting["consent_id"]:
        return None
    row = conn.execute("SELECT * FROM style_learning_consents WHERE id=? AND project_id=? AND status='active'", (setting["consent_id"], project_id)).fetchone()
    return dict(row) if row else None


def event(conn, project_id, name, details=None):
    conn.execute("INSERT INTO style_learning_events VALUES(?,?,?,?,?)", (uuid.uuid4().hex, project_id, name, compact(details or {}), stamp()))


def version_guard(conn, project_id, expected):
    setting = settings(conn, project_id)
    if setting["version"] != expected:
        raise ProjectServiceError(409, "learning_version_conflict", "学习设置或档案已变化，请刷新并核对后重试；当前输入保留。")
    return setting


def operation(conn, project_id, request_id, action, payload):
    fp = digest({"action": action, **payload})
    old = conn.execute("SELECT * FROM style_learning_operations WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if old:
        if old["fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "请求编号已用于不同学习操作，请刷新后重试。")
        return fp, json.loads(old["result_json"]) | {"idempotent_replay": True}
    return fp, None


def receipt(conn, project_id, request_id, fp, result):
    # Receipts contain identifiers only; erasing learning data cannot leak it via cached responses.
    conn.execute("INSERT INTO style_learning_operations VALUES(?,?,?,?,?)", (project_id, request_id, fp, compact(result), stamp()))
    return result


def touch(conn, project_id, *, state=None, profile_id=...):
    conn.execute("UPDATE style_learning_settings SET version=version+1,updated_at=? WHERE project_id=?", (stamp(), project_id))
    if state is not None:
        conn.execute("UPDATE style_learning_settings SET state=?,last_error_code=NULL WHERE project_id=?", (state, project_id))
    if profile_id is not ...:
        conn.execute("UPDATE style_learning_settings SET active_profile_id=? WHERE project_id=?", (profile_id, project_id))
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (stamp(), project_id))


def valid_signals(conn, project_id, consent_id=None):
    active = consent(conn, project_id)
    if not active or (consent_id and active["id"] != consent_id):
        return []
    scopes = set(json.loads(active["scopes_json"]))
    return [dict(r) for r in conn.execute("SELECT * FROM style_learning_signals WHERE project_id=? AND consent_id=? AND retention_until>? ORDER BY created_at,id", (project_id, active["id"], stamp())) if r["kind"] in scopes]


def profile_row(conn, project_id, profile_id):
    row = conn.execute("SELECT * FROM style_learning_profiles WHERE id=? AND project_id=?", (profile_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "profile_missing", "当前论文中没有此经验档案版本。")
    return dict(row)


def rule_row(conn, project_id, rule_id):
    row = conn.execute("SELECT * FROM style_learning_rules WHERE id=? AND project_id=?", (rule_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "rule_missing", "当前论文中没有此规则，或学习来源已被删除。")
    return dict(row)


def rule_valid(conn, project_id, rule, signals=None):
    active = consent(conn, project_id)
    ids = set(json.loads(rule["source_ids_json"]))
    valid = {s["id"] for s in (signals if signals is not None else valid_signals(conn, project_id))}
    return bool(active and active["id"] == rule["consent_id"] and ids and ids <= valid)


def current_rules(conn, project_id, setting=None):
    setting = setting or settings(conn, project_id)
    if not setting["active_profile_id"] or not consent(conn, project_id):
        return []
    profile = profile_row(conn, project_id, setting["active_profile_id"])
    signals = valid_signals(conn, project_id)
    result = []
    for rid in json.loads(profile["rule_ids_json"]):
        row = conn.execute("SELECT * FROM style_learning_rules WHERE id=? AND project_id=? AND status='approved'", (rid, project_id)).fetchone()
        if row and rule_valid(conn, project_id, row, signals):
            result.append(dict(row))
    return result


def effective_profile(conn, project_id):
    setting = settings(conn, project_id)
    if setting["state"] in {"disabled", "paused"}:
        return StyleProfile(project_id=project_id, status=setting["state"])
    rules = current_rules(conn, project_id, setting)
    if not rules:
        return StyleProfile(project_id=project_id, status="collecting" if consent(conn, project_id) else "disabled")
    profile = profile_row(conn, project_id, setting["active_profile_id"])
    signals = {s["id"]: s for s in valid_signals(conn, project_id)}
    ids = sorted({sid for r in rules for sid in json.loads(r["source_ids_json"])})
    return StyleProfile(project_id=project_id, version=str(profile["version"]), instructions=tuple(r["instruction"] for r in rules),
        source_versions=tuple(sid+":"+signals[sid]["source_version"] for sid in ids), status="active",
        profile_id=profile["id"], consent_id=profile["consent_id"], rule_ids=tuple(r["id"] for r in rules), signal_ids=tuple(ids))


def new_profile(conn, project_id, rule_ids, reason):
    setting = settings(conn, project_id)
    ordinal = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM style_learning_profiles WHERE project_id=?", (project_id,)).fetchone()[0]
    pid = uuid.uuid4().hex
    conn.execute("INSERT INTO style_learning_profiles VALUES(?,?,?,?,?,?,?,?)", (pid, project_id, setting["consent_id"], ordinal, setting["active_profile_id"], compact(list(dict.fromkeys(rule_ids))), reason, stamp()))
    touch(conn, project_id, state="active" if rule_ids else "collecting", profile_id=pid)
    return pid


def invalidate_contexts(conn, project_id):
    from paperpilot.services.context import revoke_style_contexts
    return revoke_style_contexts(conn, project_id)


def opt_in(conn, project_id, payload):
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], "consent", payload)
    if prior:
        return prior
    setting = version_guard(conn, project_id, payload["expected_version"])
    if not payload["authorized"]:
        raise ProjectServiceError(422, "consent_required", "请明确授权本论文的学习范围与保留期限；未采集任何样本。")
    scopes = sorted(set(payload["scopes"]))
    if not scopes or set(scopes)-SCOPES or not 1 <= payload["retention_days"] <= 365:
        raise ProjectServiceError(422, "invalid_consent", "请选择学习范围和1～365天的保留期限。")
    if setting["consent_id"]:
        conn.execute("UPDATE style_learning_consents SET status='revoked',revoked_at=? WHERE id=?", (stamp(), setting["consent_id"]))
        invalidate_contexts(conn, project_id)
    cid = uuid.uuid4().hex
    ordinal = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM style_learning_consents WHERE project_id=?", (project_id,)).fetchone()[0]
    conn.execute("INSERT INTO style_learning_consents VALUES(?,?,?,?,?,?,?,NULL)", (cid, project_id, ordinal, compact(scopes), payload["retention_days"], "active", stamp()))
    conn.execute("INSERT INTO style_learning_settings(project_id,version,consent_id,state,active_profile_id,created_at,updated_at) VALUES(?,0,?,'collecting',NULL,?,?) ON CONFLICT(project_id) DO UPDATE SET consent_id=excluded.consent_id,active_profile_id=NULL,state='collecting',last_error_code=NULL", (project_id, cid, stamp(), stamp()))
    conn.execute("UPDATE style_learning_runs SET status='stale',updated_at=? WHERE project_id=? AND status IN ('queued','running')", (stamp(), project_id))
    touch(conn, project_id)
    event(conn, project_id, "consent_granted", {"consent_id": cid, "scopes": scopes, "retention_days": payload["retention_days"]})
    return receipt(conn, project_id, payload["request_id"], fp, {"consent_id": cid, "version": settings(conn, project_id)["version"]})


def enqueue(conn, project_id):
    active = consent(conn, project_id)
    if not active:
        raise ProjectServiceError(409, "consent_required", "请先启用或恢复本论文文风学习。")
    signals = valid_signals(conn, project_id)[-50:]
    if not signals:
        raise ProjectServiceError(409, "signals_required", "没有有效学习样本；请提交修改或添加示例/反馈后再蒸馏。")
    setting = settings(conn, project_id)
    families = {r["family"]: r["id"] for r in current_rules(conn, project_id, setting)}
    fp = digest({"consent": active["id"], "sources": [s["id"] for s in signals], "profile": setting["active_profile_id"], "distiller": "local-style-v1"})
    old = conn.execute("SELECT * FROM style_learning_runs WHERE project_id=? AND input_hash=?", (project_id, fp)).fetchone()
    if old:
        return old["id"]
    rid = uuid.uuid4().hex
    conn.execute("UPDATE style_learning_runs SET status='stale',updated_at=? WHERE project_id=? AND status='queued'", (stamp(), project_id))
    conn.execute("INSERT INTO style_learning_runs(id,project_id,consent_id,input_hash,signal_ids_json,base_profile_id,base_families_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)", (rid, project_id, active["id"], fp, compact([s["id"] for s in signals]), setting["active_profile_id"], compact(families), stamp(), stamp()))
    event(conn, project_id, "distillation_queued", {"run_id": rid, "signal_count": len(signals)})
    return rid


def add_signal(conn, project_id, kind, source_version, content, before=""):
    active = consent(conn, project_id)
    if not active or kind not in json.loads(active["scopes_json"]):
        raise ProjectServiceError(409, "consent_required", "当前授权不包含此类学习样本；请先调整学习范围。")
    from paperpilot.services.context import redact_sensitive_text
    content, _ = redact_sensitive_text(content[:16000])
    before, _ = redact_sensitive_text(before[:16000])
    source_version, _ = redact_sensitive_text(source_version)
    old = conn.execute("SELECT * FROM style_learning_signals WHERE project_id=? AND consent_id=? AND kind=? AND source_version=?", (project_id, active["id"], kind, source_version)).fetchone()
    if old:
        if old["content"] != content or old["before_text"] != before:
            raise ProjectServiceError(409, "source_version_reused", "此来源版本已有不同样本，请使用新的来源版本说明。")
        return old["id"]
    if not content.strip():
        raise ProjectServiceError(422, "empty_signal", "学习样本不能为空。")
    sid = uuid.uuid4().hex
    until = (now()+timedelta(days=active["retention_days"])).isoformat()
    conn.execute("INSERT INTO style_learning_signals VALUES(?,?,?,?,?,?,?,?,?)", (sid, project_id, active["id"], kind, source_version, before, content, until, stamp()))
    event(conn, project_id, "signal_collected", {"signal_id": sid, "kind": kind})
    enqueue(conn, project_id)
    return sid


def collect_explicit(conn, project_id, payload):
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], "signal", payload)
    if prior:
        if not conn.execute("SELECT 1 FROM style_learning_signals WHERE id=? AND project_id=?", (prior["signal_id"], project_id)).fetchone():
            raise ProjectServiceError(410, "signal_removed", "此请求的学习样本已过期或删除，不会重新采集。请明确添加新样本。")
        return prior
    version_guard(conn, project_id, payload["expected_version"])
    if payload["kind"] not in {"example", "feedback"}:
        raise ProjectServiceError(422, "invalid_signal", "手工添加请选择示例或明确反馈；实际采纳/拒绝由提交事件记录。")
    sid = add_signal(conn, project_id, payload["kind"], payload["source_version"], payload["content"])
    return receipt(conn, project_id, payload["request_id"], fp, {"signal_id": sid})


def capture_edit(conn, project_id, path, previous, content, version, source):
    active = consent(conn, project_id)
    if not active or "accepted_edit" not in json.loads(active["scopes_json"]) or not path.lower().endswith((".tex", ".md")) or previous is None:
        return
    if source not in {"candidate_apply", "human_checkpoint", "manual_edit", "editor_save"}:
        return
    # Linear comparison bounds the work even for large, repetitive manuscripts.
    start = 0
    while start < min(len(previous),len(content)) and previous[start] == content[start]:
        start += 1
    end_before, end_after = len(previous), len(content)
    while end_before > start and end_after > start and previous[end_before-1] == content[end_after-1]:
        end_before -= 1
        end_after -= 1
    before = previous[max(0,start-80):min(len(previous),end_before+80)][:4000]
    after = content[max(0,start-80):min(len(content),end_after+80)][:4000]
    if after.strip():
        add_signal(conn, project_id, "accepted_edit", f"{path}@{version}:{source}", after, before)


def capture_rejection(conn, project_id, candidate_id):
    active = consent(conn, project_id)
    if not active or "rejected_edit" not in json.loads(active["scopes_json"]) or not candidate_id:
        return
    candidate = conn.execute("SELECT * FROM draft_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
    if candidate:
        patches = json.loads(candidate["content_json"] or "[]")
        content = "\n".join(str(p.get("content", ""))[:1000] for p in patches)[:4000]
        if content.strip():
            add_signal(conn, project_id, "rejected_edit", "candidate:"+candidate_id, content)


def best_effort_capture(conn, project_id, callback, *args):
    """A learning failure cannot abort an already authorized manuscript commit."""
    conn.execute("SAVEPOINT style_learning_capture")
    try:
        callback(conn, project_id, *args)
        conn.execute("RELEASE SAVEPOINT style_learning_capture")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT style_learning_capture")
        conn.execute("RELEASE SAVEPOINT style_learning_capture")
        try:
            conn.execute("UPDATE style_learning_settings SET state='failed',last_error_code='signal_capture_failed',updated_at=? WHERE project_id=? AND state NOT IN ('disabled','paused')", (stamp(), project_id))
        except sqlite3.Error:
            pass


def sentence_lengths(text):
    text = re.sub(r"\\(?:cite\w*|ref|label)\{[^}]*\}", "", text)
    text = re.sub(r"\\[A-Za-z]+(?:\[[^\]]*\])?", "", text)
    return [len(re.findall(r"[\u3400-\u9fff]|[A-Za-z]+(?:'[A-Za-z]+)?", piece)) for piece in re.findall(r"[^。！？.!?]+[。！？.!?]+", text) if len(re.findall(r"[\u3400-\u9fff]|[A-Za-z]+", piece)) >= 3]


def distill_local(signals):
    """Measured candidates, not a claim to infer intent or scientific correctness."""
    proposals, diagnostics = [], []
    preferred = [s for s in signals if s["kind"] in {"accepted_edit", "example"} and sentence_lengths(s["content"])]
    lengths = [n for s in preferred for n in sentence_lengths(s["content"])]
    if len(lengths) >= 3:
        target = round(statistics.median(lengths))
        sources = [s["id"] for s in preferred]
        proposals.append({"family": "sentence_length", "instruction": f"参考近期已采纳的表达或写作示例，句长优先接近约{target}字/词；中文按字、英文按词统计。以清楚表达为先，不机械凑长度。",
            "sources": sources, "confidence": min(0.85,0.40+0.06*len(sources)),
            "method": {"method": "local_sentence_statistics_v1", "sample_count": len(sources), "sentence_count": len(lengths), "median_units": target, "limitation": "统计候选，置信度未校准；不判断事实、质量或作者意图。"}})
    else:
        diagnostics.append({"code": "insufficient_style_evidence", "message": "有效修改/示例中的完整句子不足3句；请继续积累或添加明确反馈，不会伪造偏好。"})
    paragraphs = [(s['id'], len(re.findall(r"[\u3400-\u9fff]|[A-Za-z]+", paragraph)))
        for s in preferred for paragraph in re.split(r"\n\s*\n",s['content']) if len(sentence_lengths(paragraph)) >= 2]
    if len(paragraphs) >= 3:
        target = round(statistics.median(size for _,size in paragraphs))
        proposals.append({'family':'paragraph_length','instruction':f'段落可参考近期示例约{target}字/词的长度组织，每段围绕一个中心；论证完整优先，不机械截断。',
            'sources':list(dict.fromkeys(sid for sid,_ in paragraphs)), 'confidence':min(0.8,0.4+0.03*len(paragraphs)),
            'method':{'method':'local_paragraph_statistics_v1','paragraph_count':len(paragraphs),'median_units':target,'limitation':'仅统计含至少两句的段落，至少三段才提出候选；不判断学术论证质量。'}})
    markers = ("however", "therefore", "moreover", "此外", "因此", "然而", "同时")
    counts = Counter({m: sum(len(re.findall(re.escape(m), s["content"], re.I)) for s in preferred) for m in markers})
    observed = [m for m,n in counts.most_common(3) if n >= 2]
    if observed:
        proposals.append({"family": "transitions", "instruction": "衔接方式可参考近期样本中反复出现的表达："+"、".join(observed)+"。只在逻辑需要时使用，避免机械添加。", "sources": [s["id"] for s in preferred], "confidence": min(0.8,0.45+0.03*sum(counts[m] for m in observed)), "method": {"method": "local_transition_frequency_v1", "counts": {m:counts[m] for m in observed}, "limitation": "频率不是因果偏好，需作者核验。"}})
    feedback = [s for s in signals if s["kind"] == "feedback"]
    for s in feedback[-10:]:
        proposals.append({"family": "feedback:"+s["id"], "instruction": s["content"][:2000], "sources": [s["id"]], "confidence": 0.8,
            "method": {"method": "explicit_author_feedback_v1", "limitation": "作者明确反馈的候选指示，仍需批准；不覆盖显式配置或当前任务。"}})
    rejected = sum(s["kind"] == "rejected_edit" for s in signals)
    if rejected:
        diagnostics.append({"code": "rejection_reason_unknown", "message": f"有{rejected}个拒绝结果但没有可判定的风格理由；仅保留观察，不从拒绝本身推断偏好。"})
    return proposals, diagnostics


def process_run(database, project_id, run_id):
    conn = database()
    claim_token=uuid.uuid4().hex
    try:
        with conn:
            begin(conn)
            run = conn.execute("SELECT * FROM style_learning_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if not run or run["status"] != "queued":
                return
            active = consent(conn, project_id)
            setting = settings(conn, project_id)
            ids = set(json.loads(run["signal_ids_json"]))
            signals = [s for s in valid_signals(conn, project_id, run["consent_id"]) if s["id"] in ids]
            if not active or len(signals) != len(ids) or setting["active_profile_id"] != run["base_profile_id"]:
                conn.execute("UPDATE style_learning_runs SET status='stale',error_code='stale_input',updated_at=? WHERE id=?", (stamp(),run_id))
                return
            conn.execute("UPDATE style_learning_runs SET status='running',claim_token=?,updated_at=? WHERE id=? AND status='queued'", (claim_token,stamp(),run_id))
            conn.execute("UPDATE style_learning_settings SET state='distilling',updated_at=? WHERE project_id=?", (stamp(),project_id))
        proposals, diagnostics = distill_local(signals)
        with conn:
            begin(conn)
            setting = settings(conn, project_id)
            current_ids = {s["id"] for s in valid_signals(conn, project_id, run["consent_id"])[-50:]}
            live = conn.execute("SELECT status,claim_token FROM style_learning_runs WHERE id=?", (run_id,)).fetchone()
            if not live or live["status"] != "running" or live["claim_token"]!=claim_token:
                return
            if ids != current_ids or setting["active_profile_id"] != run["base_profile_id"]:
                conn.execute("UPDATE style_learning_runs SET status='stale',error_code='stale_input',updated_at=? WHERE id=?", (stamp(),run_id))
                if setting["state"] == "distilling":
                    conn.execute("UPDATE style_learning_settings SET state=?,updated_at=? WHERE project_id=?", ("active" if current_rules(conn,project_id) else "collecting",stamp(),project_id))
                return
            conn.execute("UPDATE style_learning_rules SET status='stale' WHERE project_id=? AND status='candidate' AND run_id IS NOT NULL", (project_id,))
            base_families = json.loads(run["base_families_json"])
            for p in proposals:
                ordinal = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM style_learning_rules WHERE project_id=? AND family=?", (project_id,p["family"])).fetchone()[0]
                conn.execute("INSERT INTO style_learning_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)", (uuid.uuid4().hex,project_id,run["consent_id"],run_id,p["family"],ordinal,p["instruction"],compact(p["sources"]),p["confidence"],compact(p["method"]),base_families.get(p["family"]),"candidate",stamp()))
            conn.execute("UPDATE style_learning_runs SET status='completed',diagnostics_json=?,error_code=NULL,updated_at=? WHERE id=?", (compact(diagnostics),stamp(),run_id))
            conn.execute("UPDATE style_learning_settings SET state=?,last_error_code=NULL,updated_at=? WHERE project_id=?", ("active" if current_rules(conn,project_id) else "collecting",stamp(),project_id))
            event(conn,project_id,"distillation_completed",{"run_id":run_id,"candidate_count":len(proposals)})
    except Exception as exc:
        conn.rollback()
        with conn:
            failed=conn.execute("UPDATE style_learning_runs SET status='failed',error_code='local_distillation_failed',updated_at=? WHERE id=? AND claim_token=? AND status='running'", (stamp(),run_id,claim_token))
            if failed.rowcount:
                conn.execute("UPDATE style_learning_settings SET state='failed',last_error_code='local_distillation_failed',updated_at=? WHERE project_id=? AND state NOT IN ('disabled','paused')", (stamp(),project_id))
                event(conn,project_id,"distillation_failed",{"run_id":run_id,"code":"local_distillation_failed"})
    finally:
        conn.close()


def request_distillation(conn, project_id, payload):
    begin(conn)
    fp, prior = operation(conn,project_id,payload["request_id"],"distill",payload)
    if prior:
        return prior
    version_guard(conn,project_id,payload["expected_version"])
    rid = enqueue(conn,project_id)
    run = conn.execute("SELECT status FROM style_learning_runs WHERE id=?",(rid,)).fetchone()
    if run["status"] in {"failed","stale"}:
        conn.execute("UPDATE style_learning_runs SET status='queued',error_code=NULL,updated_at=? WHERE id=?",(stamp(),rid))
    return receipt(conn,project_id,payload["request_id"],fp,{"run_id":rid})


def approve(conn, project_id, payload):
    begin(conn)
    fp, prior = operation(conn,project_id,payload["request_id"],"approve",payload)
    if prior:
        return prior
    setting = version_guard(conn,project_id,payload["expected_version"])
    if not consent(conn,project_id):
        raise ProjectServiceError(409,"consent_required","请先启用或恢复当前论文学习，再批准经验。")
    current = {r["family"]:r["id"] for r in current_rules(conn,project_id,setting)}
    selected = [rule_row(conn,project_id,rid) for rid in dict.fromkeys(payload["rule_ids"])]
    if len({r["family"] for r in selected}) != len(selected):
        raise ProjectServiceError(422,"conflicting_rules","同一类经验请只选择一个版本。")
    for rule in selected:
        if rule["status"] != "candidate" or not rule_valid(conn,project_id,rule):
            raise ProjectServiceError(409,"source_unavailable","候选或来源已过期、撤权或更新；请使用当前样本重新蒸馏。")
        if current.get(rule["family"]) != rule["base_rule_id"]:
            raise ProjectServiceError(409,"stale_rule","同类有效规则已变化；请刷新并基于当前版本重新编辑或蒸馏。")
        current[rule["family"]] = rule["id"]
        conn.execute("UPDATE style_learning_rules SET status='approved',approved_at=? WHERE id=?",(stamp(),rule["id"]))
    pid = new_profile(conn,project_id,list(current.values()),"author_approval")
    invalidate_contexts(conn,project_id)
    event(conn,project_id,"rules_approved",{"profile_id":pid,"rule_ids":[r["id"] for r in selected]})
    return receipt(conn,project_id,payload["request_id"],fp,{"profile_id":pid,"version":settings(conn,project_id)["version"]})


def edit_rule(conn,project_id,rule_id,payload):
    begin(conn)
    fp, prior = operation(conn,project_id,payload["request_id"],"edit",{"rule_id":rule_id,**payload})
    if prior:
        return prior
    version_guard(conn,project_id,payload["expected_version"])
    rule = rule_row(conn,project_id,rule_id)
    if not rule_valid(conn,project_id,rule):
        raise ProjectServiceError(409,"source_unavailable","规则来源已不可用，请添加当前有效示例或反馈后重新蒸馏。")
    from paperpilot.services.context import redact_sensitive_text
    instruction,_ = redact_sensitive_text(payload["instruction"].strip())
    if not instruction:
        raise ProjectServiceError(422,"empty_rule","经验规则不能为空。")
    ordinal = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM style_learning_rules WHERE project_id=? AND family=?",(project_id,rule["family"])).fetchone()[0]
    base = next((r["id"] for r in current_rules(conn,project_id) if r["family"]==rule["family"]),None)
    rid = uuid.uuid4().hex
    conn.execute("INSERT INTO style_learning_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",(rid,project_id,rule["consent_id"],None,rule["family"],ordinal,instruction,rule["source_ids_json"],rule["confidence"],compact({"method":"explicit_author_edit","limitation":"编辑后仍需明确批准。"}),base,"candidate",stamp()))
    event(conn,project_id,"rule_edited_as_candidate",{"rule_id":rid,"parent_id":rule_id})
    return receipt(conn,project_id,payload["request_id"],fp,{"rule_id":rid})


def govern(conn,project_id,action,payload):
    begin(conn)
    fp, prior = operation(conn,project_id,payload["request_id"],action,payload)
    if prior:
        return prior
    setting = version_guard(conn,project_id,payload["expected_version"])
    if setting["version"]==0:
        raise ProjectServiceError(409,"consent_required","当前尚未启用学习，请先明确授权。")
    if action=="pause":
        if not consent(conn,project_id):
            raise ProjectServiceError(409,"consent_required","当前未启用学习，无需暂停；停用后需重新明确授权。")
        touch(conn,project_id,state="paused")
        invalidate_contexts(conn,project_id)
    elif action=="resume":
        row = conn.execute("SELECT status FROM style_learning_consents WHERE id=?",(setting["consent_id"],)).fetchone()
        if not row or row["status"]!="active" or setting["state"] not in {"paused","failed"}:
            raise ProjectServiceError(409,"consent_required","授权已撤回或当前无需恢复；请先重新授权。")
        touch(conn,project_id,state="collecting")
        if current_rules(conn,project_id):conn.execute("UPDATE style_learning_settings SET state='active' WHERE project_id=?",(project_id,))
    elif action=="disable":
        if setting["consent_id"]:conn.execute("UPDATE style_learning_consents SET status='revoked',revoked_at=? WHERE id=?",(stamp(),setting["consent_id"]))
        touch(conn,project_id,state="disabled",profile_id=None)
        invalidate_contexts(conn,project_id)
    elif action=="rollback":
        target = profile_row(conn,project_id,payload["target_profile_id"])
        active = consent(conn,project_id)
        rules = [rule_row(conn,project_id,rid) for rid in json.loads(target["rule_ids_json"])]
        if not active or active["id"]!=target["consent_id"] or any(r["status"]!="approved" or not rule_valid(conn,project_id,r) for r in rules):
            raise ProjectServiceError(409,"source_unavailable","历史档案的来源已过期、删除或撤权，不能通过回滚重新启用。")
        new_profile(conn,project_id,[r["id"] for r in rules],"author_rollback")
        invalidate_contexts(conn,project_id)
    elif action.startswith("remove:"):
        if not consent(conn,project_id):
            raise ProjectServiceError(409,"consent_required","请先明确启用或恢复学习，再调整当前档案。")
        rid=action.split(":",1)[1];rule_row(conn,project_id,rid)
        new_profile(conn,project_id,[r["id"] for r in current_rules(conn,project_id) if r["id"]!=rid],"author_rule_removal")
        invalidate_contexts(conn,project_id)
    else:
        raise ProjectServiceError(422,"invalid_action","不支持的经验治理操作。")
    if action in {"pause","disable"}:
        conn.execute("UPDATE style_learning_runs SET status='stale',updated_at=? WHERE project_id=? AND status IN ('queued','running')",(stamp(),project_id))
    event(conn,project_id,action.split(":")[0],{"version":settings(conn,project_id)["version"]})
    return receipt(conn,project_id,payload["request_id"],fp,{"version":settings(conn,project_id)["version"]})


def erase_signals(conn,project_id,signal_ids):
    ids = set(signal_ids)
    if not ids:
        return {"signals":0,"rules":0,"contexts":0}
    rules = [r["id"] for r in conn.execute("SELECT id,source_ids_json FROM style_learning_rules WHERE project_id=?",(project_id,)) if ids & set(json.loads(r["source_ids_json"]))]
    for rid in rules:
        conn.execute("DELETE FROM style_learning_rules WHERE id=? AND project_id=?",(rid,project_id))
    for sid in ids:
        conn.execute("DELETE FROM style_learning_signals WHERE id=? AND project_id=?",(sid,project_id))
    conn.execute("UPDATE style_learning_runs SET status='stale',error_code='source_removed',updated_at=? WHERE project_id=? AND status IN ('queued','running')",(stamp(),project_id))
    contexts = invalidate_contexts(conn,project_id)
    setting = settings(conn,project_id)
    if setting["state"] not in {"disabled","paused"} and consent(conn,project_id):
        new_profile(conn,project_id,[r["id"] for r in current_rules(conn,project_id)],"source_removed")
    else:
        touch(conn,project_id)
    return {"signals":len(ids),"rules":len(rules),"contexts":contexts}


def delete_learning(conn,project_id,payload):
    begin(conn)
    fp, prior = operation(conn,project_id,payload["request_id"],"delete",payload)
    if prior:
        return prior
    setting = version_guard(conn,project_id,payload["expected_version"])
    if payload["confirmation"]!="删除学习数据":
        raise ProjectServiceError(422,"delete_confirmation_required","请确认删除学习副本和规则；原正文与原始资料不会自动删除。")
    sid = payload.get("signal_id")
    if sid:
        row=conn.execute("SELECT id FROM style_learning_signals WHERE id=? AND project_id=?",(sid,project_id)).fetchone()
        if not row:
            raise ProjectServiceError(404,"signal_missing","当前论文中没有此样本，或已被清理。")
        counts=erase_signals(conn,project_id,[sid])
    else:
        counts={"signals":conn.execute("SELECT COUNT(*) FROM style_learning_signals WHERE project_id=?",(project_id,)).fetchone()[0],"rules":conn.execute("SELECT COUNT(*) FROM style_learning_rules WHERE project_id=?",(project_id,)).fetchone()[0],"contexts":invalidate_contexts(conn,project_id)}
        for table in ("style_learning_signals","style_learning_rules","style_learning_profiles","style_learning_runs","style_learning_consents","style_learning_events"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?",(project_id,))
        if setting["version"]:
            conn.execute("UPDATE style_learning_settings SET consent_id=NULL WHERE project_id=?",(project_id,))
            touch(conn,project_id,state="disabled",profile_id=None)
    event(conn,project_id,"learning_deleted",counts)
    return receipt(conn,project_id,payload["request_id"],fp,{"deleted":True,"counts":counts,"version":settings(conn,project_id)["version"]})


def purge_expired(conn,project_id):
    expired=[r["id"] for r in conn.execute("SELECT id FROM style_learning_signals WHERE project_id=? AND retention_until<=?",(project_id,stamp()))]
    if not expired:return {"signals":0,"rules":0,"contexts":0}
    begin(conn)
    counts=erase_signals(conn,project_id,expired)
    event(conn,project_id,"expired_sources_purged",counts)
    return counts


def public_rule(conn,project_id,row):
    item=dict(row)
    item["source_signal_ids"]=json.loads(item.pop("source_ids_json"))
    item["method"]=json.loads(item.pop("method_json"))
    item["source_valid"]=rule_valid(conn,project_id,row)
    retained={r['id'] for r in conn.execute("SELECT id FROM style_learning_signals WHERE project_id=? AND retention_until>?",(project_id,stamp()))}
    if not set(item['source_signal_ids'])<=retained:
        item['instruction']=''
        item['method']={"limitation":"来源已过期或删除，规则文本不可用。"}
    if not item["source_valid"]:
        item["effective_status"]="source_unavailable"
    else:
        item["effective_status"]=item["status"]
    return item


def read(conn,project_id):
    setting=settings(conn,project_id)
    active=consent(conn,project_id)
    all_consent=conn.execute("SELECT * FROM style_learning_consents WHERE id=? AND project_id=?",(setting["consent_id"],project_id)).fetchone()
    profile=effective_profile(conn,project_id)
    signals=[dict(r) for r in conn.execute("SELECT id,kind,source_version,content,before_text,retention_until,created_at,consent_id FROM style_learning_signals WHERE project_id=? ORDER BY created_at DESC LIMIT 100",(project_id,))]
    for signal in signals:
        signal["eligible"]=bool(active and signal["consent_id"]==active["id"] and signal["retention_until"]>stamp() and signal["kind"] in json.loads(active["scopes_json"]))
        if signal["retention_until"]<=stamp():signal.update(content='',before_text='',source_version='',expired=True)
    rules=[public_rule(conn,project_id,r) for r in conn.execute("SELECT * FROM style_learning_rules WHERE project_id=? ORDER BY created_at DESC,id LIMIT 100",(project_id,))]
    profiles=[]
    valid_rule_ids={r["id"] for r in conn.execute("SELECT * FROM style_learning_rules WHERE project_id=? AND status='approved'",(project_id,)) if rule_valid(conn,project_id,r)}
    for row in conn.execute("SELECT * FROM style_learning_profiles WHERE project_id=? ORDER BY version DESC LIMIT 30",(project_id,)):
        item=dict(row);item["rule_ids"]=json.loads(item.pop("rule_ids_json"));item["source_valid"]=bool(active and item["consent_id"]==active["id"] and set(item["rule_ids"])<=valid_rule_ids);profiles.append(item)
    runs=[]
    for row in conn.execute("SELECT id,status,error_code,diagnostics_json,created_at,updated_at FROM style_learning_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 20",(project_id,)):
        item=dict(row);item["diagnostics"]=json.loads(item.pop("diagnostics_json"));runs.append(item)
    visible_state=setting["state"]
    if visible_state=="active" and profile.status!="active":visible_state="collecting"
    instructions={"disabled":"默认关闭。选择学习范围和期限并明确授权后开始。","collecting":"继续提交修改或添加示例/反馈；候选需要作者批准才会用于写作。","distilling":"正在本地蒸馏候选，未自动批准或改变正文。","active":"当前有效批准经验可用于后续AI候选；可在上下文预览核对来源。","paused":"采集和经验使用已暂停；恢复后只使用仍有效的授权与来源。","failed":"学习处理失败，正文保存不受影响；恢复后重新蒸馏或继续添加当前样本。"}
    return {"project_id":project_id,"version":setting["version"],"state":visible_state,"consent_id":setting["consent_id"],"opt_in":bool(active),"scopes":json.loads(all_consent["scopes_json"]) if all_consent else [],"retention_days":all_consent["retention_days"] if all_consent else 30,
        "active_profile_id":setting["active_profile_id"],"profile_version":profile.version or None,"effective_instructions":list(profile.instructions),"effective_rule_ids":list(profile.rule_ids),"effective_source_versions":list(profile.source_versions),"profile_applies":profile.status=="active","signals":signals,"rules":rules,"profiles":profiles,"runs":runs,"last_error_code":setting["last_error_code"],"next_action":instructions.get(visible_state,instructions['failed']),"learning_backend_available":True,"external_calls":0,
        "privacy_boundary":"删除学习副本、派生规则及历史经验上下文；原正文/原始资料与已有候选文本不自动删除，已外发请求无法本地召回。"}


def maintenance_once(database):
    conn=database()
    try:
        with conn:
            begin(conn)
            projects=[r["project_id"] for r in conn.execute("SELECT project_id FROM style_learning_settings")]
            for pid in projects:purge_expired(conn,pid)
            recover_runs(conn)
        pending=conn.execute("SELECT id,project_id FROM style_learning_runs WHERE status='queued' ORDER BY created_at DESC LIMIT 4").fetchall()
    finally:
        conn.close()
    for row in pending:process_run(database,row["project_id"],row["id"])


def recover_runs(conn):
    cutoff=(now()-timedelta(seconds=60)).isoformat()
    conn.execute("UPDATE style_learning_runs SET status='queued',claim_token='',error_code='local_worker_restarted',updated_at=? WHERE status='running' AND updated_at<?",(stamp(),cutoff))


def maintenance_loop(database,stop_event):
    while not stop_event.is_set():
        try:
            maintenance_once(database)
        except Exception:
            # A transient store failure must not terminate the paper service.
            pass
        stop_event.wait(1.0)
