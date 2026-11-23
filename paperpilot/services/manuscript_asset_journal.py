"""Reconcile manuscript-managed file copies with their committed SQLite version."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from paperpilot.services.tex_runtime import birth_token, owner_state
from paperpilot.services.visual_assets import managed_path
from paperpilot.services.project_errors import ProjectServiceError


def _directory(storage):
    base=Path(storage).resolve()
    directory=base/'.manuscript-asset-journal'
    if directory.resolve()!=directory:
        raise OSError('Unexpected manuscript recovery location')
    return directory


def record(storage,pid,version_id,paths):
    directory=_directory(storage)
    directory.mkdir(exist_ok=True,parents=True)
    marker=directory/(version_id+'.json')
    pending=marker.with_suffix('.tmp')
    marker_data={'project_id':pid,'version_id':version_id,'paths':list(paths.values()),'owner_pid':os.getpid(),
                 'owner_token':birth_token(os.getpid()) or ''}
    with pending.open('x',encoding='utf-8') as output:
        json.dump(marker_data,output)
        output.flush()
        os.fsync(output.fileno())
    pending.replace(marker)
    return marker


def reconcile(conn,storage,markers=None):
    if not markers:
        return
    for marker in markers:
        _finish(conn,storage,marker,owned=True)


def recover(conn,storage):
    try:
        directory=_directory(storage)
        for marker in directory.glob('*.json'):
            _finish(conn,storage,marker)
    except OSError:
        return


def _finish(conn,storage,marker,owned=False):
    try:
        if marker.is_symlink() or marker.stat().st_size>4096:
            return
        value=json.loads(marker.read_text(encoding='utf-8'))
        if not owned and owner_state(value)!='dead':
            return
        pid,version,paths=value['project_id'],value['version_id'],value['paths']
        valid=re.fullmatch(r'[0-9a-f]{12}',pid) and re.fullmatch(r'[0-9a-f]{32}',version)
        if not valid or marker.name!=version+'.json' or len(paths)!=3:
            return
        expected=r'[a-f0-9]{32}/'+version+r'/(?:source|preview|build)\.[a-z0-9]+'
        if any(not re.fullmatch(expected,path) for path in paths):
            return
        committed=conn.execute('SELECT 1 FROM figure_asset_versions WHERE project_id=? AND id=?',(pid,version)).fetchone()
        if not committed:
            for path in paths:
                copy=managed_path(storage,pid,path)
                # Both names are fixed by the asset service and proven to be in the asset root.
                copy.unlink(missing_ok=True)
                copy.with_suffix(copy.suffix+'.tmp').unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,ProjectServiceError):
        # Leave the marker retryable if the filesystem is temporarily unavailable.
        return
