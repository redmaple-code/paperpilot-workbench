"""Non-secret TeX runtime leases outside the untrusted build mount.

Leases survive a killed worker. Recovery checks process birth identity and
container labels before stopping anything; it never claims cleanup on a
Docker communication failure.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

NAME = re.compile(r'^pwa-[a-zA-Z0-9_.-]{1,36}-[a-zA-Z0-9_.-]{1,20}-[0-9a-f]{8}$')


def birth_token(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            if exit_time.dwHighDateTime or exit_time.dwLowDateTime:
                return None
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # Field 22 is the process start time; comm can contain spaces/parentheses.
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def current_owner():
    pid = os.getpid()
    token = birth_token(pid)
    if token is None:
        raise OSError('Cannot identify compilation worker birth time')
    return {'owner_pid': pid, 'owner_token': token}


def owner_alive(record):
    pid = record.get('owner_pid')
    token = record.get('owner_token')
    return bool(pid and token and birth_token(pid) == str(token))


def owner_state(record):
    """Distinguish a dead owner from one whose identity cannot be inspected."""
    pid, token = record.get('owner_pid'), record.get('owner_token')
    if not pid or not token:
        return 'unknown'
    actual = birth_token(pid)
    if actual is not None:
        return 'alive' if actual == str(token) else 'dead'
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return 'dead' if ctypes.get_last_error() == 87 else 'unknown'
        try:
            code = wintypes.DWORD()
            if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
                return 'dead'
            return 'unknown'
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return 'unknown'
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OSError, ValueError):
        return 'unknown'


def root_hash(project_dir):
    return hashlib.sha256(os.path.normcase(str(Path(project_dir).resolve())).encode('utf-8')).hexdigest()


def labels(project_dir, job_id):
    return {'io.paperpilot.managed': 'tex', 'io.paperpilot.project': root_hash(project_dir), 'io.paperpilot.job': job_id}


def reserve(project_dir, job_id, container_name):
    if not NAME.fullmatch(container_name):
        raise ValueError('Invalid managed container identity')
    project = Path(project_dir).resolve()
    registry = project / '.tex-runtime'
    registry.mkdir(parents=True, exist_ok=True)
    if registry.resolve().parent != project:
        raise ValueError('Runtime registry cannot point outside the project')
    record = {'schema': 1, 'job_id': job_id, 'container_name': container_name,
        'labels': labels(project, job_id), **current_owner(), 'created_at': time.time()}
    path = registry / (container_name + '.json')
    temp = path.with_suffix('.tmp')
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    return path


def _run(docker, *args):
    return subprocess.run([docker, *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, check=False)


def cleanup(project_dir, path, *, docker='docker', force=False):
    """Return a diagnostic, retaining the lease unless absence is verified."""
    project = Path(project_dir).resolve()
    path = Path(path)
    try:
        if path.resolve().parent != project / '.tex-runtime' or not NAME.fullmatch(path.stem) or path.stat().st_size > 8192:
            return {'status': 'invalid_lease', 'cleanup_verified': False}
        record = json.loads(path.read_text(encoding='utf-8'))
        name = record['container_name']
        if name != path.stem or record['labels'] != labels(project, record['job_id']):
            return {'status': 'invalid_lease', 'cleanup_verified': False}
        if not force:
            state = owner_state(record)
            if state == 'alive':
                return {'status': 'active_owner', 'job_id': record['job_id'], 'cleanup_verified': False}
            if state != 'dead':
                return {'status': 'owner_unknown', 'job_id': record['job_id'], 'cleanup_verified': False}
        for _ in range(15):
            listed = _run(docker, 'ps', '-aq', '--filter', 'name=^/' + name + '$')
            if listed.returncode != 0:
                return {'status': 'cleanup_unavailable', 'job_id': record['job_id'], 'cleanup_verified': False}
            if not listed.stdout.strip():
                path.unlink(missing_ok=True)
                return {'status': 'cleaned', 'job_id': record['job_id'], 'cleanup_verified': True}
            inspected = _run(docker, 'inspect', '--format', '{{json .Config.Labels}}', name)
            if inspected.returncode == 0:
                actual = json.loads(inspected.stdout)
                if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in record['labels'].items()):
                    return {'status': 'container_identity_conflict', 'job_id': record['job_id'], 'cleanup_verified': False}
                _run(docker, 'rm', '-f', name)
            time.sleep(.1)
        return {'status': 'cleanup_pending', 'job_id': record['job_id'], 'cleanup_verified': False}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {'status': 'cleanup_unavailable', 'cleanup_verified': False}


def recover(project_dir, *, docker='docker', job_ids=None, force=False, build_only=False):
    registry = Path(project_dir).resolve() / '.tex-runtime'
    if not registry.is_dir() or registry.resolve().parent != Path(project_dir).resolve():
        return []
    result = []
    for path in sorted(registry.glob('pwa-*.json')):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if size > 8192:
            result.append({'status': 'invalid_lease', 'cleanup_verified': False})
            continue
        if job_ids is not None or build_only:
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                job_id=record.get('job_id')
                if not isinstance(job_id, str) or not job_id:
                    result.append({'status':'invalid_lease','cleanup_verified':False})
                    continue
                if job_ids is not None and job_id not in job_ids:
                    # A damaged record must not hide a target job's container
                    # merely by losing or changing the job ID inside the JSON.
                    if any(path.stem.startswith('pwa-'+re.sub(r'[^a-zA-Z0-9_.-]', '-', str(target))[:36]+'-') for target in job_ids):
                        result.append({'status':'invalid_lease','cleanup_verified':False})
                    continue
                if build_only:
                    safe_job=re.sub(r'[^a-zA-Z0-9_.-]', '-', str(job_id or 'build'))[:36]
                    source_map=re.fullmatch(r'pwa-'+re.escape(safe_job)+r'-source-map-[0-9a-f]{8}',path.stem)
                    if source_map:
                        if record.get('container_name')!=path.stem or record.get('labels')!=labels(Path(project_dir).resolve(),job_id):
                            result.append({'status':'invalid_lease','cleanup_verified':False})
                        continue
            except (OSError, ValueError, TypeError, AttributeError):
                result.append({'status': 'invalid_lease', 'cleanup_verified': False})
                continue
        result.append(cleanup(project_dir, path, docker=docker, force=force))
    return result
