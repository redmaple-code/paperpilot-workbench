"""Read-only historical differences and guarded handoff to human revision."""
from __future__ import annotations

import difflib
import hashlib
import json
import uuid

from paperpilot.services import settings, visual_assets
from paperpilot.services.content_versions import current_version
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.workflow import current_snapshot


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def writable_mode(conn, project_id):
    project = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, 'project_missing', '论文不存在。')
    if project['archived']:
        raise ProjectServiceError(409, 'project_archived', '论文已归档；请先恢复论文，再准备历史恢复。不会隐式解除归档。')
    if project['lifecycle_state'] == 'published':
        raise ProjectServiceError(409, 'project_published_read_only', '论文已发布；请明确撤回发布后再恢复源码，发布历史保持不变。')
    workflow = current_snapshot(conn, project_id)
    # Stage is not a restriction for restoring history.
    from paperpilot.services.human_revision import active_mode
    mode = active_mode(conn, project_id)
    return dict(project), workflow, mode


def state_token(conn, project_id):
    project, workflow, mode = writable_mode(conn, project_id)
    from paperpilot.services.build_snapshots import build_profile
    return digest({'project_revision': project['project_revision'], 'workflow_version': workflow['version'], 'mode': mode,
        'files': [dict(r) for r in conn.execute('SELECT path,content FROM files WHERE project_id=? ORDER BY path', (project_id,))],
        'versions': [dict(r) for r in conn.execute('SELECT target_path,MAX(version) AS version FROM content_versions WHERE project_id=? AND target_kind=\'file\' GROUP BY target_path ORDER BY target_path', (project_id,))],
        'editor_drafts': [dict(r) for r in conn.execute('SELECT path,base_version,base_content,content FROM editor_local_drafts WHERE project_id=? ORDER BY path', (project_id,))],
        'revision_drafts': [dict(r) for r in conn.execute('SELECT target_kind,target_path,content,base_version,mode_id FROM revision_drafts WHERE project_id=? ORDER BY target_kind,target_path', (project_id,))],
        'settings': settings.read(conn, project_id), 'project_domain_id': str(project['domain_id'] or ''), 'build_profile': build_profile(conn,project_id)})


def historical_snapshot(conn, project_id, job_id):
    from paperpilot.services import build_snapshots
    job = conn.execute('SELECT * FROM build_jobs WHERE id=? AND project_id=?', (job_id, project_id)).fetchone()
    if job is None:
        raise ProjectServiceError(404, 'build_job_missing', '此论文内没有该构建任务。')
    if job['status'] != 'succeeded':
        raise ProjectServiceError(409, 'build_restore_unavailable', '只有成功构建的固定源码可以准备历史恢复；请先查看任务诊断。')
    return build_snapshots.get(conn, project_id, job['snapshot_id'])


def resource_checks(conn, project_id, snapshot, storage_root):
    result = []
    for asset in snapshot['manifest'].get('assets', []):
        item = {**asset, 'available': False}
        try:
            if storage_root is None:
                raise ProjectServiceError(503, 'asset_storage_unavailable', '受管资源存储不可用。')
            visual_assets.checked_file(conn, storage_root, project_id, asset['version_id'])
            item['available'] = True
        except (OSError, ProjectServiceError) as exc:
            item['reason'] = exc.message if isinstance(exc, ProjectServiceError) else '历史图像文件缺失或不可读，请从原资源恢复后再重试。'
        result.append(item)
    return result


def preview(conn, project_id, job_id, include_paths=None, *, remove_paths=None, setting_keys=None, restore_build_environment=False, storage_root=None):
    project, workflow, mode = writable_mode(conn, project_id)
    snapshot = historical_snapshot(conn, project_id, job_id)
    known = {f['path'] for f in snapshot['files']}
    selected = list(include_paths or [])
    if len(selected) != len(set(selected)) or set(selected) - known:
        raise ProjectServiceError(422, 'restore_paths_invalid', '恢复范围含重复或历史快照中不存在的文件；请刷新差异后重新选择。')
    selected = set(selected or known)
    current_only = [dict(r) for r in conn.execute('SELECT path,content FROM files WHERE project_id=? ORDER BY path', (project_id,)) if r['path'] not in known]
    removals = list(remove_paths or [])
    if len(removals) != len(set(removals)) or set(removals) - {row['path'] for row in current_only}:
        raise ProjectServiceError(422, 'restore_removal_invalid', '移除范围必须是当前独有文件且不能重复；请刷新差异后重新选择。')
    removal_rows=[]
    for file in current_only:
        path=file['path']
        drafts=[]
        editor=conn.execute('SELECT content,base_content,base_version FROM editor_local_drafts WHERE project_id=? AND path=?',(project_id,path)).fetchone()
        if editor and (editor['content']!=file['content'] or editor['base_content']!=file['content'] or int(editor['base_version'])!=current_version(conn,project_id,'file',path)):
            drafts.append('editor')
        if conn.execute("SELECT 1 FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?",(project_id,path)).fetchone():
            drafts.append('revision')
        removal_rows.append({'path':path,'selected':path in removals,'base_version':current_version(conn,project_id,'file',path),
                             'current_sha256':digest(file['content']),'drafts':drafts,'conflict':bool(drafts),
                             'diff':'\n'.join(difflib.unified_diff(file['content'].splitlines(),[],fromfile=path+' 当前文件',tofile=path+' 将移除',lineterm=''))})
    rows = []
    for file in snapshot['files']:
        path, incoming = file['path'], file['content']
        current = conn.execute('SELECT content FROM files WHERE project_id=? AND path=?', (project_id, path)).fetchone()
        content = current['content'] if current else ''
        drafts = []
        for kind, query in [('editor', 'SELECT content,base_version FROM editor_local_drafts WHERE project_id=? AND path=?'),
                            ('revision', "SELECT content,base_version FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?")]:
            draft = conn.execute(query, (project_id, path)).fetchone()
            if draft:
                drafts.append({'kind': kind, 'sha256': digest(draft['content']), 'base_version': draft['base_version'],
                    'conflict': draft['content'] not in {content, incoming}})
        rows.append({'path': path, 'selected': path in selected, 'current_exists': current is not None,
            'base_version': current_version(conn, project_id, 'file', path), 'snapshot_version': file.get('version'),
            'current_sha256': digest(content) if current else None, 'incoming_sha256': digest(incoming),
            'changed': current is None or content != incoming, 'drafts': drafts,
            'conflict': any(d['conflict'] for d in drafts),
            'diff': '\n'.join(difflib.unified_diff(content.splitlines(), incoming.splitlines(), fromfile=path+' 当前正文', tofile=path+' 历史源码', lineterm=''))})
    if not rows:
        raise ProjectServiceError(422, 'restore_paths_empty', '历史快照没有可恢复文件。')
    assets = resource_checks(conn, project_id, snapshot, storage_root)
    try:
        current_assets = visual_assets.capture_manifest(conn, project_id, [dict(r) for r in conn.execute('SELECT path,content FROM files WHERE project_id=? ORDER BY path', (project_id,))])
    except ProjectServiceError as exc:
        current_assets = {'unavailable': True, 'reason': exc.message}
    effective = settings.read(conn, project_id).get('effective', {})
    selected_settings=list(setting_keys or [])
    if len(selected_settings)!=len(set(selected_settings)) or set(selected_settings)-set(settings.SETTING_KEYS):
        raise ProjectServiceError(422,'restore_settings_invalid','所选配置项不存在或重复；请刷新差异后重新选择。')
    historical=snapshot['manifest'].get('settings', {})
    stored_effective=snapshot['settings'].get('effective', {}) if isinstance(snapshot['settings'],dict) else {}
    if not isinstance(historical,dict): historical={}
    if not isinstance(stored_effective,dict): stored_effective={}
    config_rows=[]
    for key in settings.SETTING_KEYS:
        available=key in historical and key in stored_effective and historical[key]==stored_effective[key]
        reason='' if available else '历史快照未完整记录或配置副本不一致，不能恢复此项。'
        if available and key in {'domain','style','citation_style'} and not isinstance(historical[key],str):
            available=False;reason='历史配置值格式不受支持，不能自动恢复。'
        if available and key=='domain':
            historical_domain=snapshot['manifest'].get('project_domain_id')
            if historical_domain is None:
                available=False;reason='旧快照未记录领域归属身份，无法可靠恢复领域配置；保留当前归属，可取消此项后继续恢复其他内容。'
            elif historical_domain!=str(project['domain_id'] or ''):
                available=False;reason='历史领域归属与当前不同，本期不支持已有论文跨领域迁移；保留当前归属，可取消此项后继续恢复其他内容。'
            elif historical_domain and not conn.execute('SELECT 1 FROM domains WHERE id=?',(historical_domain,)).fetchone():
                available=False;reason='历史领域已不可用；保留当前归属，可取消此项后继续恢复其他内容。'
        config_rows.append({'key':key,'current':effective.get(key),'historical':historical.get(key),'selected':key in selected_settings,
                            'available':available,'reason':reason,'changed':key in historical and effective.get(key)!=historical[key]})
    from paperpilot.services.build_snapshots import build_profile
    current_profile=build_profile(conn,project_id)
    if restore_build_environment:
        from paperpilot.services import build_environment
        environment_review=build_environment.inspect_historical(snapshot['manifest'].get('build_profile'),current_profile)
    else:
        environment_review={'available':True,'message':'默认保留本论文当前编译环境；选中历史环境并更新差异后检查本地镜像。'}
    environment_review={'selected':bool(restore_build_environment),**environment_review}
    result = {'job_id': job_id, 'snapshot_id': snapshot['id'], 'project_revision': project['project_revision'],
        'workflow_version': workflow['version'], 'mode_id': mode['id'], 'files': rows, 'assets': assets, 'current_assets': current_assets,
        'current_only_paths': [r['path'] for r in current_only], 'current_only_files': removal_rows,
        'configuration': {'historical_settings': snapshot['manifest'].get('settings', {}), 'current_settings': effective,
            'choices':config_rows,
            'historical_build_profile': snapshot['manifest'].get('build_profile', {}), 'current_build_profile': current_profile,
            'environment_review':environment_review,
            'policy': '论文配置与编译环境默认保留当前值；历史编译环境需核验本地镜像，作为本论文配置写入，不安装或下载工具。领域归属不同或未记录时不能自动切换。'},
        'can_prepare': all(a['available'] for a in assets) and not any(r['selected'] and r['conflict'] for r in rows+removal_rows) and not any(row['selected'] and not row['available'] for row in config_rows) and environment_review['available'],
        'guidance': '请先查看完整差异。当前独有文件默认保留，可逐项选择移除；有未提交草稿的文件不能移除。术语、大纲和工作流不会回退。',
        'state_token': state_token(conn, project_id)}
    result['preview_fingerprint'] = digest(result)
    return result


def prepare(conn, project_id, job_id, request_id, include_paths=None, *, remove_paths=None, setting_keys=None, restore_build_environment=False, preview_fingerprint=None, storage_root=None):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    writable_mode(conn, project_id)
    params = sorted(include_paths or [])
    removal_params=sorted(remove_paths or [])
    setting_params=sorted(setting_keys or [])
    existing = conn.execute('SELECT * FROM build_restore_events WHERE project_id=? AND job_id=? AND request_id=?', (project_id, job_id, request_id)).fetchone()
    if existing:
        result = json.loads(existing['result_json'])
        if result.get('requested_paths', params) != params or result.get('requested_removals', []) != removal_params or result.get('requested_settings', []) != setting_params or result.get('requested_environment',False)!=bool(restore_build_environment) or result.get('preview_fingerprint') != preview_fingerprint:
            raise ProjectServiceError(409, 'build_restore_request_conflict', '此恢复请求编号已用于另一范围或差异；请查看原候选，重新预览后使用新编号。')
        return result | {'idempotent_replay': True}
    view = preview(conn, project_id, job_id, include_paths, remove_paths=remove_paths, setting_keys=setting_keys, restore_build_environment=restore_build_environment, storage_root=storage_root)
    if preview_fingerprint and view['preview_fingerprint'] != preview_fingerprint:
        raise ProjectServiceError(409, 'build_restore_preview_stale', '差异展示后正文、草稿、工作流或资源已变化；请重新查看差异，现稿未改动。')
    if not view['can_prepare']:
        raise ProjectServiceError(409, 'build_restore_conflict', '恢复有草稿冲突、资源缺失或所选配置不可恢复；请按差异页处理或取消对应选择后重新预览，所有现稿均保留。')
    snapshot = historical_snapshot(conn, project_id, job_id)
    contents = {f['path']: f['content'] for f in snapshot['files']}
    from paperpilot.services.human_revision import save_draft
    drafts = []
    for file in view['files']:
        if not file['selected']:
            continue
        save_draft(conn, project_id, 'file', file['path'], contents[file['path']], file['base_version'])
        drafts.append({'target_kind': 'file', 'target_path': file['path'], 'base_version': file['base_version'], 'content_sha256': digest(contents[file['path']]), 'content_length': len(contents[file['path']])})
    from paperpilot.services.build_snapshots import _now
    restore_id = uuid.uuid4().hex
    result = {'ok': True, 'restore_id': restore_id, 'job_id': job_id, 'snapshot_id': snapshot['id'], 'mode_id': view['mode_id'], 'drafts': drafts,
        'conflicts': [], 'status': 'restore_candidate', 'requested_paths': params, 'requested_removals':removal_params, 'requested_settings':setting_params, 'requested_environment':bool(restore_build_environment),
        'environment_to_restore':view['configuration']['environment_review'].get('profile') if restore_build_environment else None,
        'environment_notice':view['configuration']['environment_review'].get('message') if restore_build_environment else '',
        'historical_image_verified':view['configuration']['environment_review'].get('historical_image_verified') if restore_build_environment else None,
        'settings_to_restore':[{'key':row['key'],'value':row['historical']} for row in view['configuration']['choices'] if row['selected']],
        'removals':[{'path':row['path'],'base_version':row['base_version'],'current_sha256':row['current_sha256']} for row in view['current_only_files'] if row['selected']], 'preview_fingerprint': preview_fingerprint,
        'prepared_state_token': state_token(conn, project_id), 'configuration_policy': view['configuration']['policy']}
    result['candidate_fingerprint'] = digest(result)
    conn.execute('INSERT INTO build_restore_events(id,project_id,job_id,request_id,result_json,created_at) VALUES(?,?,?,?,?,?)',
        (restore_id, project_id, job_id, request_id, json.dumps(result, ensure_ascii=False), _now()))
    return result
