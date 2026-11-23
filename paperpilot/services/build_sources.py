"""Read-only materialization of persisted drafts/candidates into a TeX snapshot."""
from __future__ import annotations

import hashlib

from paperpilot.services.content_versions import current_version
from paperpilot.services.project_errors import ProjectServiceError


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def materialize(conn, project_id, files, purpose, *, draft_source='all', candidate_id=None):
    by_path = {row['path']: dict(row) for row in files}
    if purpose in {'submitted', 'release'}:
        if candidate_id or draft_source != 'all':
            raise ProjectServiceError(422, 'build_source_mismatch', '候选只能用于候选预检；提交/发布构建只读取已保存正文。')
        return list(by_path.values()), {'kind': 'saved', 'entries': []}
    if purpose == 'candidate_preview':
        if draft_source != 'all':
            raise ProjectServiceError(422, 'build_source_mismatch', '候选预检只读取指定候选的文件补丁；请移除草稿来源选择，或单独创建草稿预览。')
        if not candidate_id:
            raise ProjectServiceError(422, 'build_candidate_required', '候选预检必须选择具体候选；请从候选卡片点击“编译预检”，不会改为编译当前正文。')
        from paperpilot.services.ai_jobs import get_candidate
        candidate = get_candidate(conn, project_id, candidate_id)
        if candidate['status'] != 'candidate_ready' or candidate['stale']:
            raise ProjectServiceError(409, 'build_candidate_stale', '此候选已处理、基线过期或来源失效；请查看原候选与当前正文，生成有效候选后再预检。已有快照与历史PDF保留。')
        entries = []
        for patch in candidate['patches']:
            if patch['kind'] != 'file':
                continue
            path, base = patch['path'], int(patch['base_version'])
            by_path[path] = {'path': path, 'content': patch['content'], 'version': base}
            entries.append({'kind': 'candidate', 'path': path, 'base_version': base, 'sha256': digest(patch['content'])})
        if not entries:
            raise ProjectServiceError(422, 'build_candidate_has_no_files', '此候选只有大纲等非文件目标，没有可编译的工程文件；请先生成文件候选，或预览已保存正文。')
        return sorted(by_path.values(), key=lambda row: row['path']), {'kind': 'candidate', 'candidate_id': candidate_id,
            'entries': sorted(entries, key=lambda row: row['path']), 'context_version': candidate['context_version']}
    if candidate_id:
        raise ProjectServiceError(422, 'build_source_mismatch', '指定候选需要选择候选预检用途；草稿预览不会自动混入候选。')
    if draft_source not in {'all', 'editor', 'revision'}:
        raise ProjectServiceError(422, 'invalid_draft_source', '请选择全部已保存草稿、编辑器草稿或人工修订草稿。')
    drafts = []
    if draft_source in {'all', 'editor'}:
        drafts.extend(dict(row) | {'kind': 'editor'} for row in conn.execute(
            'SELECT path,content,base_version FROM editor_local_drafts WHERE project_id=? ORDER BY path', (project_id,)))
    if draft_source in {'all', 'revision'}:
        drafts.extend(dict(row) | {'kind': 'revision'} for row in conn.execute(
            "SELECT d.target_path AS path,d.content,d.base_version,d.mode_id FROM revision_drafts d JOIN human_revision_modes m ON m.id=d.mode_id AND m.project_id=d.project_id WHERE d.project_id=? AND d.target_kind='file' AND m.status='active' ORDER BY d.target_path", (project_id,)))
    chosen, entries = {}, []
    for draft in drafts:
        path = draft['path']
        if path not in by_path or int(draft['base_version']) != current_version(conn, project_id, 'file', path):
            raise ProjectServiceError(409, 'build_draft_stale', f'“{path}”的{("编辑器" if draft["kind"] == "editor" else "人工修订")}草稿基线已过期或文件已删除。请比较并恢复/更新草稿，或明确选择另一草稿来源/当前提交版；未丢弃草稿，也未改用正文。')
        if path in chosen and chosen[path] != draft['content']:
            raise ProjectServiceError(409, 'build_draft_conflict', f'“{path}”的编辑器草稿与人工修订草稿内容不同。请在“草稿来源”明确选择一种，再重新预览；两个草稿和正文均保留。')
        chosen[path] = draft['content']
        entries.append({'kind': draft['kind'], 'path': path, 'base_version': int(draft['base_version']),
            'sha256': digest(draft['content']), **({'mode_id': draft['mode_id']} if 'mode_id' in draft else {})})
        by_path[path]['content'] = draft['content']
    return sorted(by_path.values(), key=lambda row: row['path']), {'kind': 'draft', 'draft_source': draft_source, 'entries': sorted(entries, key=lambda row: (row['path'], row['kind']))}
