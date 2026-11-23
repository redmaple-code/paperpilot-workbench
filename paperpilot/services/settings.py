from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


SETTING_KEYS = ("domain", "style", "citation_style", "term_policy", "format_policy")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _default_settings(project: sqlite3.Row | dict[str, Any], domain: sqlite3.Row | dict[str, Any] | None = None) -> dict[str, Any]:
    if domain is not None:
        source_name = str(domain.get("name", "") if isinstance(domain, dict) else domain["name"])
    else:
        source_name = str(project.get("domain", "") if isinstance(project, dict) else project["domain"])
    def value(key: str) -> Any:
        if isinstance(project, dict):
            return project.get(key, "")
        return project[key] if key in project.keys() else ""
    return {
        "domain": source_name,
        "style": str(value("style") or ""),
        "citation_style": str(value("citation_style") or ""),
        "term_policy": "project_isolated",
        "format_policy": "warn_only",
    }


def _domain_settings(conn: sqlite3.Connection, domain_id: str, project: sqlite3.Row | dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    domain = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    if domain is None:
        raise ProjectServiceError(404, "domain_missing", "领域不存在。")
    row = conn.execute("SELECT version,settings_json FROM domain_setting_versions WHERE domain_id=? ORDER BY version DESC LIMIT 1", (domain_id,)).fetchone()
    if row is None:
        settings = _default_settings(project or domain, domain)
        stamp = _now()
        conn.execute("INSERT INTO domain_setting_versions(domain_id,version,settings_json,status,created_at) VALUES(?,?,?,?,?)", (domain_id, int(domain["version"] or 1), _json(settings), "active", stamp))
        return int(domain["version"] or 1), settings
    return int(row["version"]), json.loads(row["settings_json"] or "{}")


def ensure_domain_settings(conn: sqlite3.Connection, domain_id: str) -> dict[str, Any]:
    version, values = _domain_settings(conn, domain_id)
    return {"domain_id": domain_id, "version": version, "settings": values}


def update_domain_settings(conn: sqlite3.Connection, domain_id: str, values: dict[str, Any], expected_version: int, request_id: str) -> dict[str, Any]:
    domain = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    if domain is None:
        raise ProjectServiceError(404, "domain_missing", "领域不存在。")
    _version, current = _domain_settings(conn, domain_id)
    if int(domain["version"]) != int(expected_version):
        raise ProjectServiceError(409, "domain_version_conflict", "领域默认已变化，请刷新后重试。")
    invalid = sorted(set(values) - set(SETTING_KEYS))
    if invalid:
        raise ProjectServiceError(422, "invalid_setting_key", f"不支持的领域配置项：{', '.join(invalid)}")
    merged = dict(current)
    merged.update(values)
    next_version = int(domain["version"]) + 1
    stamp = _now()
    conn.execute("UPDATE domains SET version=?,updated_at=? WHERE id=?", (next_version, stamp, domain_id))
    conn.execute("INSERT INTO domain_setting_versions(domain_id,version,settings_json,status,created_at) VALUES(?,?,?,?,?)", (domain_id, next_version, _json(merged), "active", stamp))
    return {"domain_id": domain_id, "version": next_version, "settings": merged, "request_id": request_id, "propagation": "existing_projects_unchanged_until_explicit_sync"}


def ensure_project_baseline(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "项目不存在。")
    existing = conn.execute("SELECT * FROM project_setting_baselines WHERE project_id=?", (project_id,)).fetchone()
    if existing:
        return _baseline_payload(existing)
    domain_id = str(project["domain_id"] or "")
    if domain_id:
        domain_version, settings = _domain_settings(conn, domain_id, project)
        # Project creation may include an explicit initial style/citation
        # choice.  Preserve it in the immutable paper baseline so later
        # domain changes remain opt-in and reviewable.
        if project["style"]:
            settings["style"] = str(project["style"])
        if project["citation_style"]:
            settings["citation_style"] = str(project["citation_style"])
    else:
        domain_version, settings = 0, _default_settings(project)
    key_sources = {key: {"source": "domain_baseline" if domain_id else "project_baseline",
                         "domain_version": domain_version if domain_id else None} for key in SETTING_KEYS}
    for key in ("style", "citation_style"):
        if project[key]:
            key_sources[key] = {"source": "project_baseline", "domain_version": None}
    stamp = _now()
    conn.execute(
        "INSERT INTO project_setting_baselines(project_id,domain_id,domain_version,version,settings_json,sources_json,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, domain_id, domain_version, 1, _json(settings), _json(key_sources), stamp),
    )
    return {"project_id": project_id, "domain_id": domain_id, "domain_version": domain_version, "version": 1, "settings": settings, "key_sources": key_sources, "updated_at": stamp}


def _baseline_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["settings"] = json.loads(item.pop("settings_json", "{}") or "{}")
    item["key_sources"] = json.loads(item.pop("sources_json", "{}") or "{}")
    return item


def _overrides(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    return {
        row["setting_key"]: {"value": json.loads(row["value_json"]), "version": int(row["version"]), "updated_at": row["updated_at"]}
        for row in conn.execute("SELECT * FROM project_setting_overrides WHERE project_id=? ORDER BY setting_key", (project_id,))
    }


def effective(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    baseline = ensure_project_baseline(conn, project_id)
    overrides = _overrides(conn, project_id)
    values = dict(baseline["settings"])
    sources: dict[str, dict[str, Any]] = {}
    for key in SETTING_KEYS:
        if key in overrides:
            values[key] = overrides[key]["value"]
            sources[key] = {"source": "project_override", "version": overrides[key]["version"]}
        else:
            values[key] = values.get(key, "")
            key_source = baseline["key_sources"].get(key)
            if not isinstance(key_source, dict) or key_source.get("source") not in {"domain_baseline", "project_baseline"}:
                key_source = {"source": "unknown", "domain_version": None}
            sources[key] = {**key_source, "version": baseline["version"]}
    return {"project_id": project_id, "effective": values, "sources": sources, "baseline": baseline, "overrides": overrides}


def read(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    return effective(conn, project_id)


def _check_key(key: str) -> None:
    if key not in SETTING_KEYS:
        raise ProjectServiceError(422, "invalid_setting_key", f"不支持的配置项：{key}")


def update_override(conn: sqlite3.Connection, project_id: str, key: str, value: Any, expected_project_revision: int, request_id: str) -> dict[str, Any]:
    _check_key(key)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "项目不存在。")
    replay = conn.execute("SELECT result_json FROM project_setting_events WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if replay:
        return json.loads(replay["result_json"]) | {"idempotent_replay": True}
    if int(project["project_revision"]) != int(expected_project_revision):
        raise ProjectServiceError(409, "project_revision_conflict", "项目配置已变化，请刷新后重试。")
    current = conn.execute("SELECT version FROM project_setting_overrides WHERE project_id=? AND setting_key=?", (project_id, key)).fetchone()
    version = int(current["version"] if current else 0) + 1
    stamp = _now()
    conn.execute(
        "INSERT INTO project_setting_overrides(project_id,setting_key,value_json,version,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,setting_key) DO UPDATE SET value_json=excluded.value_json,version=excluded.version,updated_at=excluded.updated_at",
        (project_id, key, _json(value), version, stamp),
    )
    if key in {"domain", "style", "citation_style"}:
        conn.execute(f"UPDATE projects SET {key}=?,updated_at=?,project_revision=project_revision+1 WHERE id=?", (str(value), stamp, project_id))
    else:
        conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    result = read(conn, project_id) | {"changed_key": key, "request_id": request_id}
    conn.execute("INSERT INTO project_setting_events(id,project_id,request_id,action,base_version,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, request_id, "override", expected_project_revision, _json(result), stamp))
    return result


def restore_override(conn: sqlite3.Connection, project_id: str, key: str, expected_project_revision: int, request_id: str) -> dict[str, Any]:
    _check_key(key)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "项目不存在。")
    replay = conn.execute("SELECT result_json FROM project_setting_events WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if replay:
        return json.loads(replay["result_json"]) | {"idempotent_replay": True}
    if int(project["project_revision"]) != int(expected_project_revision):
        raise ProjectServiceError(409, "project_revision_conflict", "项目配置已变化，请刷新后重试。")
    conn.execute("DELETE FROM project_setting_overrides WHERE project_id=? AND setting_key=?", (project_id, key))
    stamp = _now()
    if key in {"domain", "style", "citation_style"}:
        baseline = ensure_project_baseline(conn, project_id)
        conn.execute(f"UPDATE projects SET {key}=?,updated_at=?,project_revision=project_revision+1 WHERE id=?", (str(baseline["settings"].get(key, "")), stamp, project_id))
    else:
        conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    result = read(conn, project_id) | {"changed_key": key, "request_id": request_id, "restored": True}
    conn.execute("INSERT INTO project_setting_events(id,project_id,request_id,action,base_version,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, request_id, "restore", expected_project_revision, _json(result), stamp))
    return result


def sync_preview(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "项目不存在。")
    baseline = ensure_project_baseline(conn, project_id)
    if str(project["domain_id"] or "") != str(baseline["domain_id"] or ""):
        current_id = str(project["domain_id"] or "")
        old_id = str(baseline["domain_id"] or "")
        current_domain = conn.execute("SELECT id,name FROM domains WHERE id=?", (current_id,)).fetchone() if current_id else None
        old_domain = conn.execute("SELECT id,name FROM domains WHERE id=?", (old_id,)).fetchone() if old_id else None
        return {"project_id": project_id, "available": False, "reason": "domain_binding_mismatch", "diff": [], "baseline": baseline,
                "binding_repair": {"project_revision": int(project["project_revision"]), "baseline_version": int(baseline["version"]),
                    "project_domain_id": current_id, "project_domain_name": current_domain["name"] if current_domain else ("领域已不存在" if current_id else "未绑定领域"),
                    "baseline_domain_id": old_id, "baseline_domain_name": old_domain["name"] if old_domain else ("领域已不存在" if old_id else "未绑定领域"),
                    "effective_values": effective(conn, project_id)["effective"],
                    "available": (not current_id or bool(current_domain)) and not bool(project["archived"]) and project["lifecycle_state"] != "published"}}
    if not baseline["domain_id"]:
        return {"project_id": project_id, "available": False, "reason": "project_has_no_domain", "diff": [], "baseline": baseline}
    latest_version, latest = _domain_settings(conn, baseline["domain_id"], project)
    diff = [{"key": key, "old": baseline["settings"].get(key), "new": latest.get(key)} for key in SETTING_KEYS if baseline["settings"].get(key) != latest.get(key)]
    return {"project_id": project_id, "available": bool(diff), "domain_id": baseline["domain_id"], "from_version": baseline["domain_version"], "to_version": latest_version, "diff": diff, "baseline": baseline}


def repair_baseline_binding(conn: sqlite3.Connection, project_id: str, expected_project_revision: int, expected_baseline_version: int,
                            expected_project_domain_id: str, expected_baseline_domain_id: str, request_id: str) -> dict[str, Any]:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "论文不存在。")
    if project["archived"] or project["lifecycle_state"] == "published":
        raise ProjectServiceError(409, "project_read_only", "论文已归档或发布；请先解除只读，再处理配置归属。")
    params = {"expected_project_revision": expected_project_revision, "expected_baseline_version": expected_baseline_version,
              "expected_project_domain_id": expected_project_domain_id, "expected_baseline_domain_id": expected_baseline_domain_id}
    event = conn.execute("SELECT action,result_json FROM project_setting_events WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if event:
        prior = json.loads(event["result_json"])
        if event["action"] != "binding_repair" or prior.get("request_params") != params:
            raise ProjectServiceError(409, "setting_request_conflict", "此请求编号已用于其他配置操作，请重新检查。")
        return prior | {"idempotent_replay": True}
    baseline = ensure_project_baseline(conn, project_id)
    current_id = str(project["domain_id"] or "")
    old_id = str(baseline["domain_id"] or "")
    if (int(project["project_revision"]) != expected_project_revision or int(baseline["version"]) != expected_baseline_version
        or current_id != expected_project_domain_id or old_id != expected_baseline_domain_id):
        raise ProjectServiceError(409, "binding_repair_stale", "论文或配置归属已变化，请重新检查；当前配置未改变。")
    if current_id == old_id:
        raise ProjectServiceError(409, "binding_repair_not_needed", "论文和配置基线归属已一致；请刷新。")
    if current_id and not conn.execute("SELECT 1 FROM domains WHERE id=?", (current_id,)).fetchone():
        raise ProjectServiceError(409, "binding_repair_domain_missing", "论文当前领域已不存在；请先核对领域数据，未修改配置。")
    sources = dict(baseline["key_sources"])
    for key, source in list(sources.items()):
        if not isinstance(source, dict) or source.get("source") != "project_baseline":
            sources.pop(key, None)
    stamp = _now()
    conn.execute("UPDATE project_setting_baselines SET domain_id=?,domain_version=0,version=version+1,sources_json=?,updated_at=? WHERE project_id=?",
                 (current_id, _json(sources), stamp, project_id))
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (stamp, project_id))
    result = effective(conn, project_id) | {"repaired": True, "request_id": request_id, "request_params": params,
                                         "previous_baseline": baseline, "idempotent_replay": False}
    conn.execute("INSERT INTO project_setting_events(id,project_id,request_id,action,base_version,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                 (uuid.uuid4().hex, project_id, request_id, "binding_repair", expected_baseline_version, _json(result), stamp))
    return result


def sync_commit(conn: sqlite3.Connection, project_id: str, expected_baseline_version: int, selected_keys: list[str], request_id: str, expected_domain_version: int) -> dict[str, Any]:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "项目不存在。")
    for key in selected_keys:
        _check_key(key)
    if len(set(selected_keys)) != len(selected_keys):
        raise ProjectServiceError(422, "duplicate_setting_key", "配置项重复，请重新查看差异。")
    replay = conn.execute("SELECT action,base_version,result_json FROM project_setting_events WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if replay:
        prior = json.loads(replay["result_json"])
        if (replay["action"] != "sync" or int(replay["base_version"]) != expected_baseline_version
            or sorted(prior.get("selected_keys", [])) != sorted(selected_keys)
            or prior.get("expected_domain_version") != expected_domain_version):
            raise ProjectServiceError(409, "setting_request_conflict", "此请求编号已用于其他配置操作，请重新查看差异。")
        return prior | {"idempotent_replay": True}
    preview = sync_preview(conn, project_id)
    baseline = preview["baseline"]
    if int(baseline["version"]) != int(expected_baseline_version):
        raise ProjectServiceError(409, "baseline_version_conflict", "论文基线已变化，请重新查看差异。")
    if preview.get("reason") == "domain_binding_mismatch":
        raise ProjectServiceError(409, "domain_binding_mismatch", "论文领域与配置基线归属不一致；请先核对旧数据，未同步或修改任何配置。")
    if not preview.get("available"):
        return effective(conn, project_id) | {"synced": False, "reason": preview.get("reason", "no_changes")}
    latest_version, latest = _domain_settings(conn, baseline["domain_id"], project)
    if latest_version != expected_domain_version:
        raise ProjectServiceError(409, "domain_version_conflict", "领域默认在预览后发生变化；请重新查看差异，论文配置未改变。")
    if not selected_keys:
        return effective(conn, project_id) | {"synced": False, "reason": "no_selection"}
    settings = dict(baseline["settings"])
    sources = dict(baseline["key_sources"])
    for key in selected_keys:
        settings[key] = latest.get(key, settings.get(key, ""))
        sources[key] = {"source": "domain_baseline", "domain_version": latest_version}
    new_version = int(baseline["version"]) + 1
    stamp = _now()
    conn.execute("UPDATE project_setting_baselines SET domain_version=?,version=?,settings_json=?,sources_json=?,updated_at=? WHERE project_id=?", (latest_version, new_version, _json(settings), _json(sources), stamp, project_id))
    result = effective(conn, project_id) | {"synced": True, "selected_keys": selected_keys, "request_id": request_id, "expected_domain_version": expected_domain_version}
    conn.execute("INSERT INTO project_setting_events(id,project_id,request_id,action,base_version,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, request_id, "sync", expected_baseline_version, _json(result), stamp))
    return result

