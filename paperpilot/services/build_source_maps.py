"""Source locations belong to a sealed build, never to mutable workspace paths."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import threading

from paperpilot.services.project_errors import ProjectServiceError

FORMAT = 'synctex-1'
SEAL_NAME = '.paperpilot-source-map.json'
_slots = threading.BoundedSemaphore(2)
_guard = threading.Lock()
_paper_locks = {}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def display_text(value):
    # HTML textareas normalize platform newlines; line positions are unchanged.
    return value.replace('\r\n', '\n').replace('\r', '\n')


def seal(run_dir, snapshot):
    """Record exact PDF/map/source identities after the compiler has stopped."""
    run_dir = Path(run_dir)
    metadata = {'format': FORMAT, 'snapshot_id': snapshot['id'], 'snapshot_identity': snapshot['manifest']['identity'], 'available': False}
    try:
        mapping, pdf = run_dir / 'main.synctex.gz', run_dir / 'main.pdf'
        if not mapping.is_file() or mapping.is_symlink():
            metadata['message'] = '此构建没有源码位置映射；可阅读PDF或查看固定源码，重新创建支持定位的构建后再试。'
        else:
            sources = {}
            for file in snapshot['files']:
                path = run_dir / file['path']
                if not path.resolve().is_relative_to(run_dir.resolve()) or path.is_symlink() or not path.is_file() or file_sha(path) != sha(file['content'].encode()):
                    raise ValueError('source changed during compilation')
                sources[file['path']] = sha(file['content'].encode())
            metadata.update(available=True, pdf_sha256=file_sha(pdf), mapping_sha256=file_sha(mapping), source_sha256=sources)
        target = run_dir / SEAL_NAME
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        temporary.replace(target)
    except (OSError, ValueError, KeyError):
        return {'available': False, 'message': '位置映射未能与实际源码、PDF一起核验保存；PDF仍可阅读，定位已暂停，请检查存储后重新构建。'}
    return metadata


def _job(conn, project_id, job_id):
    from paperpilot.services import build_snapshots
    if not conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
        raise ProjectServiceError(404, 'project_missing', '论文不存在。')
    row = conn.execute('SELECT * FROM build_jobs WHERE id=? AND project_id=?', (job_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, 'build_job_missing', '此论文内没有该构建。')
    return dict(row), build_snapshots.get(conn, project_id, row['snapshot_id'])


def _run_directory(storage_root, project_id, job_id):
    root = Path(storage_root).resolve()
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value) for value in (project_id, job_id)):
        raise ProjectServiceError(409, 'source_map_storage_invalid', '构建存储身份不可用，未读取其他目录。')
    project = root / project_id
    run = project / 'build-runs' / job_id
    if project.is_symlink() or run.is_symlink() or not run.resolve().is_relative_to(project.resolve()) or project.resolve().parent != root:
        raise ProjectServiceError(409, 'source_map_storage_invalid', '构建目录不安全，定位已暂停，未读取其他目录。')
    return run


def info(conn, storage_root, project_id, job_id):
    job, snapshot = _job(conn, project_id, job_id)
    result = {'available': False, 'format': FORMAT, 'message': '此构建没有可用位置映射；PDF和固定源码仍可查看。'}
    if job['status'] != 'succeeded':
        return {**result, 'message': '此任务尚无成功PDF；可以从诊断查看对应版本源码。'}
    try:
        run = _run_directory(storage_root, project_id, job_id)
        meta_path, mapping, pdf = run / SEAL_NAME, run / 'main.synctex.gz', run / 'main.pdf'
        if not meta_path.is_file():
            return result
        if any(p.is_symlink() for p in (meta_path, mapping, pdf)):
            raise ValueError('symlink')
        metadata = json.loads(meta_path.read_text(encoding='utf-8'))
        if not metadata.get('available'):
            return {**result, 'message': metadata.get('message') or result['message']}
        expected_sources = {f['path']: sha(f['content'].encode()) for f in snapshot['files']}
        if metadata.get('format') != FORMAT or metadata.get('snapshot_id') != snapshot['id'] or metadata.get('snapshot_identity') != snapshot['manifest']['identity'] or metadata.get('source_sha256') != expected_sources:
            raise ValueError('identity')
        if file_sha(pdf) != metadata.get('pdf_sha256') or metadata['pdf_sha256'] != job['artifact_sha256'] or file_sha(mapping) != metadata.get('mapping_sha256'):
            raise ValueError('artifact changed')
        for path, expected in expected_sources.items():
            source = run / path
            if not source.resolve().is_relative_to(run.resolve()) or source.is_symlink() or file_sha(source) != expected:
                raise ValueError('source changed')
        return {'available': True, 'format': FORMAT, 'message': '双击PDF定位源码，或从源码行定位PDF；历史与不匹配版本只打开对应快照。'}
    except (OSError, ValueError, KeyError, TypeError, ProjectServiceError):
        return {**result, 'message': 'PDF、位置映射或冻结源码缺失/不匹配，定位已暂停；可下载PDF或按原快照重建，不会跳到错误版本。'}


def reading(conn, storage_root, project_id, job_id):
    from paperpilot.services import build_snapshots
    job, snapshot = _job(conn, project_id, job_id)
    described = build_snapshots.get_job(conn, project_id, job_id)
    profile = snapshot['manifest'].get('build_profile', {})
    can_rebuild_map = job['status'] == 'succeeded' and profile.get('source_map_format') == FORMAT
    rebuild_reason = '' if can_rebuild_map else ('此任务未成功；请查看诊断并修复源码，再编译当前版本。' if job['status'] != 'succeeded' else '此历史快照的固定环境没有位置映射。可先查看固定源码，通过历史恢复草稿建立新版本，再用当前编译环境构建。')
    return {'job_id': job_id, 'snapshot_id': snapshot['id'], 'snapshot_revision': snapshot['project_revision'], 'purpose': snapshot['purpose'],
        'current': described['current'], 'pdf_url': f'/api/projects/{project_id}/builds/{job_id}/pdf', 'source_map': info(conn, storage_root, project_id, job_id),
        'can_rebuild_map': can_rebuild_map, 'rebuild_reason': rebuild_reason,
        'source_files': [{'path': f['path'], 'version': f['version'], 'sha256': sha(f['content'].encode()), 'line_count': display_text(f['content']).count('\n') + 1} for f in snapshot['files']]}


def source(conn, project_id, job_id, path, line=1):
    from paperpilot.services import build_snapshots
    from paperpilot.services.content_versions import current_version
    job, snapshot = _job(conn, project_id, job_id)
    file = next((f for f in snapshot['files'] if f['path'] == path), None)
    if file is None:
        raise ProjectServiceError(404, 'source_not_in_snapshot', '该文件不在此构建快照内，不会读取项目外或其他论文源码。')
    if not 1 <= line <= display_text(file['content']).count('\n') + 1:
        raise ProjectServiceError(422, 'source_line_invalid', '行号超出该版本源码范围，请重新选择。')
    current = conn.execute('SELECT content FROM files WHERE project_id=? AND path=?', (project_id, path)).fetchone()
    source_kind = snapshot['manifest'].get('source', {}).get('kind', 'saved')
    can_open_current = bool(current and source_kind != 'candidate' and (current['content'] == file['content'] or source_kind == 'draft') and current_version(conn, project_id, 'file', path) == file['version'] and build_snapshots.snapshot_current(conn, project_id, snapshot)['current'])
    return {'job_id': job_id, 'snapshot_id': snapshot['id'], 'path': path, 'line': line, 'version': file['version'], 'content': file['content'],
        'sha256': sha(file['content'].encode()), 'display_sha256': sha(display_text(file['content']).encode()), 'can_open_current': can_open_current,
        'message': '源码与此构建版本一致。' if can_open_current else '此位置属于历史/预览快照，仅查看该版本源码；不会跳转或覆盖当前编辑内容。'}


def _records(output):
    records, current = [], {}
    for raw in output.splitlines():
        key, separator, value = raw.strip().partition(':')
        if not separator or key not in {'Input', 'Line', 'Column', 'Page', 'x', 'y', 'h', 'v', 'W', 'H'}:
            continue
        if key in {'Input', 'Page'} and key in current:
            records.append(current); current = {}
        current[key] = value
    if current:
        records.append(current)
    return records


def _snapshot_path(value):
    value = value.replace('\\', '/')
    if value.startswith('/workspace/'):
        value = value[len('/workspace/'):]
    elif value.startswith('/'):
        return None
    parts = []
    for part in value.split('/'):
        if part in {'', '.'}:
            continue
        if part == '..':
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)


def query(conn, storage_root, project_id, job_id, *, direction, path=None, line=1, column=0, source_sha256=None, source_version=None, source_context='snapshot', page=1, x=0, y=0):
    status = info(conn, storage_root, project_id, job_id)
    if not status['available']:
        raise ProjectServiceError(409, 'source_map_unavailable', status['message'])
    job, snapshot = _job(conn, project_id, job_id)
    run = _run_directory(storage_root, project_id, job_id)
    if direction == 'forward':
        selected = source(conn, project_id, job_id, path, line)
        expected_hash = selected['display_sha256'] if source_context == 'editor' else selected['sha256']
        if source_sha256 != expected_hash:
            raise ProjectServiceError(409, 'source_map_source_mismatch', '当前输入与此PDF源码不一致，请先编译当前草稿，或在历史快照中选择对应行；未跳转到其他版本。')
        if source_version != selected['version'] or (source_context == 'editor' and not selected['can_open_current']):
            raise ProjectServiceError(409, 'source_map_version_mismatch', '此PDF对应其他版本或候选，请在只读快照中定位，或编译当前源码；不会把历史映射用于当前编辑器。')
        if any(c in path for c in ':\r\n'):
            raise ProjectServiceError(422, 'source_map_path_unsupported', '此文件名不支持位置查询，可查看固定源码。')
        arguments = ['view', '-i', f'{line}:{column}:/workspace/{path}', '-o', '/workspace/main.pdf']
    else:
        arguments = ['edit', '-o', f'{page}:{x}:{y}:/workspace/main.pdf']
    with _guard:
        lock = _paper_locks.setdefault(project_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ProjectServiceError(429, 'source_map_busy', '此论文正在定位，请稍后再试；PDF仍可阅读。')
    acquired = _slots.acquire(blocking=False)
    try:
        if not acquired:
            raise ProjectServiceError(429, 'source_map_busy', '本机定位任务已满，请稍后再试；PDF仍可阅读。')
        from paperpilot.adapters.latex import query_synctex
        response = query_synctex(run, Path(storage_root) / project_id, job_id, arguments, snapshot['manifest'].get('build_profile', {}))
        if not response['ok']:
            raise ProjectServiceError(409, response['code'], response['message'])
        # Recheck after the external reader; no result may outlive changed files.
        if not info(conn, storage_root, project_id, job_id)['available']:
            raise ProjectServiceError(409, 'source_map_changed', '查询期间PDF或位置映射已变化，未执行跳转；请刷新后重试。')
        for record in _records(response['output']):
            if direction == 'forward':
                try:
                    target_page = int(record['Page']); tx, ty = float(record['x']), float(record['y'])
                    if target_page < 1 or not all(math.isfinite(v) and 0 <= v <= 1_000_000 for v in (tx, ty)):
                        continue
                    return {'job_id': job_id, 'snapshot_id': snapshot['id'], 'page': target_page, 'x': tx, 'y': ty}
                except (ValueError, KeyError):
                    continue
            candidate_path = _snapshot_path(record.get('Input', ''))
            if candidate_path not in {f['path'] for f in snapshot['files']}:
                continue
            try:
                return source(conn, project_id, job_id, candidate_path, int(record['Line']))
            except (ValueError, KeyError, ProjectServiceError):
                continue
        raise ProjectServiceError(409, 'source_map_no_location', '该位置没有可用的论文源码映射（可能为空白、字体或宏包内容）。请换一处正文，或查看固定快照。')
    finally:
        if acquired:
            _slots.release()
        lock.release()


def source_diagnostics(snapshot, output):
    files = {f['path']: f for f in snapshot['files']}
    diagnostics = []
    seen = set()
    for raw in output.splitlines():
        match = re.match(r'^(?:/workspace/|\./)?([^:\r\n]+):(\d+):\s*(.+)$', raw.strip())
        if not match:
            continue
        path, line, message = match[1], int(match[2]), match[3]
        if path not in files or not 1 <= line <= display_text(files[path]['content']).count('\n') + 1 or (path, line) in seen:
            continue
        seen.add((path, line));diagnostics.append({'code': 'tex_source_error', 'path': path, 'line': line, 'message': message[:1000]})
        if len(diagnostics) == 20:
            break
    return diagnostics
