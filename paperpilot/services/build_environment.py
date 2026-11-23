"""Reviewed, per-paper historical TeX environment selection."""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid

from paperpilot.services.project_errors import ProjectServiceError

KEYS = ('PWA_TEX_SANDBOX_IMAGE','PWA_LATEX_ENGINE','PWA_BIB_ENGINE','PWA_DOCKER_EXECUTABLE',
        'PWA_LATEX_TIMEOUT_SECONDS','PWA_TEX_PIDS_LIMIT','PWA_TEX_MEMORY_MB','PWA_TEX_CPU_LIMIT')
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
IMAGE_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$')


def _inspect_image(docker, target):
    try:
        result=subprocess.run([docker or 'docker','image','inspect','--format','{{.Id}}',target],
                              capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=5,check=False,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
                              env={name:os.environ[name] for name in ('PATH','SystemRoot','WINDIR','USERPROFILE','HOMEDRIVE','HOMEPATH','APPDATA','LOCALAPPDATA','HOME','TEMP','TMP','DOCKER_CONFIG','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH') if name in os.environ})
        return result.stdout.strip() if result.returncode==0 else None
    except (OSError,subprocess.SubprocessError):
        return None


def inspect_historical(profile, current):
    """Validate settings and resolve a local immutable Docker image without pulling."""
    if not isinstance(profile,dict) or any(not isinstance(profile.get(k),str) for k in KEYS):
        return {'available':False,'message':'历史快照缺少完整编译参数，请选择当前环境重新构建。'}
    if profile['PWA_DOCKER_EXECUTABLE'] != current['PWA_DOCKER_EXECUTABLE']:
        return {'available':False,'message':'历史 Docker 执行器与当前配置不同；请取消历史环境选择并更新差异，或先核对本机 Docker 配置，不会运行历史路径。'}
    if profile['PWA_LATEX_ENGINE'].lower() not in {'pdflatex','xelatex','lualatex'} or profile['PWA_BIB_ENGINE'].lower() not in {'auto','bibtex','biber'}:
        return {'available':False,'message':'历史编译器配置不受支持；请使用当前环境或先修正本机配置。'}
    try:
        for key, low, high in [('PWA_LATEX_TIMEOUT_SECONDS',1,300),('PWA_TEX_PIDS_LIMIT',16,512),('PWA_TEX_MEMORY_MB',256,8192),('PWA_TEX_CPU_LIMIT',1,8)]:
            if not low <= int(profile[key]) <= high:
                raise ValueError(key)
    except ValueError:
        return {'available':False,'message':'历史资源限制不在支持范围内；请先核对当前编译配置。'}
    if profile.get('source_map_format') not in (None,'synctex-1'):
        return {'available':False,'message':'历史源码定位格式不受支持，不能自动重现。'}
    image=profile['PWA_TEX_SANDBOX_IMAGE']
    previous_id=profile.get('PWA_TEX_IMAGE_ID')
    if not IMAGE_REF.fullmatch(image) or (previous_id is not None and (not isinstance(previous_id,str) or not IMAGE_ID.fullmatch(previous_id))):
        return {'available':False,'message':'历史镜像标识无效或缺失；请先配置本地镜像。'}
    target=previous_id or image
    image_id=_inspect_image(current['PWA_DOCKER_EXECUTABLE'],target)
    if not isinstance(image_id,str) or not IMAGE_ID.fullmatch(image_id) or (previous_id and image_id!=previous_id):
        return {'available':False,'message':'本机无法核验历史镜像；请启动 Docker 并确认镜像已存在，或选择当前环境。不会自动下载。'}
    resolved={k:profile[k] for k in KEYS}
    if 'PWA_TEX_DISK_MB' in profile:
        try:
            if not 16<=int(profile['PWA_TEX_DISK_MB'])<=8192:raise ValueError('disk budget')
        except (ValueError,TypeError):return {'available':False,'message':'历史磁盘预算无效，请核对本机配置。'}
        resolved['PWA_TEX_DISK_MB']=str(profile['PWA_TEX_DISK_MB'])
    if 'source_map_format' in profile:resolved['source_map_format']=profile['source_map_format']
    resolved['PWA_TEX_IMAGE_ID']=image_id
    verified=bool(previous_id)
    message=('历史镜像内容已核验，将固定该镜像。' if verified else '旧快照只记录镜像标签，无法证明原镜像内容；将固定当前本地标签对应的镜像，结果可能不同。')
    if profile.get('source_map_format') != 'synctex-1':
        message+='历史配置没有源码定位格式，本次构建可能无法双向定位。'
    return {'available':True,'profile':resolved,'historical_image_verified':verified,'message':message}


def effective(conn, project_id, defaults):
    row=conn.execute('SELECT profile_json FROM project_build_profile_overrides WHERE project_id=?',(project_id,)).fetchone()
    if not row:return defaults
    profile=json.loads(row['profile_json'])
    # The executable is an installation-level choice, never restored from a paper.
    return {**profile,'PWA_TEX_DISK_MB':profile.get('PWA_TEX_DISK_MB',defaults['PWA_TEX_DISK_MB']),'PWA_DOCKER_EXECUTABLE':defaults['PWA_DOCKER_EXECUTABLE']}


def apply(conn, project_id, profile, request_id):
    from paperpilot.services.build_snapshots import _now, _json
    project=conn.execute('SELECT project_revision FROM projects WHERE id=?',(project_id,)).fetchone()
    if not project:raise ProjectServiceError(404,'project_missing','论文不存在。')
    row=conn.execute('SELECT COALESCE(MAX(version),0) AS version FROM project_build_profile_events WHERE project_id=?',(project_id,)).fetchone()
    version=int(row['version'])+1;stamp=_now()
    conn.execute('INSERT INTO project_build_profile_overrides(project_id,profile_json,version,updated_at) VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET profile_json=excluded.profile_json,version=excluded.version,updated_at=excluded.updated_at',
                 (project_id,_json(profile),version,stamp))
    conn.execute('UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?',(stamp,project_id))
    result={'version':version,'profile':profile}
    conn.execute('INSERT INTO project_build_profile_events(id,project_id,request_id,action,profile_json,version,created_at,base_revision,result_json) VALUES(?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,project_id,request_id,'restore',_json(profile),version,stamp,project['project_revision'],_json(result)))
    return result


def reset(conn, project_id, expected_revision, request_id):
    from paperpilot.services.build_snapshots import _now, _json, build_profile
    project=conn.execute('SELECT project_revision FROM projects WHERE id=?',(project_id,)).fetchone()
    if not project:raise ProjectServiceError(404,'project_missing','论文不存在。')
    replay=conn.execute('SELECT action,base_revision,result_json FROM project_build_profile_events WHERE project_id=? AND request_id=?',(project_id,request_id)).fetchone()
    if replay:
        if replay['action']!='reset' or int(replay['base_revision'])!=int(expected_revision):
            raise ProjectServiceError(409,'build_profile_request_conflict','请求编号已用于其他环境操作或论文版本。')
        return json.loads(replay['result_json']) | {'idempotent_replay':True}
    if int(project['project_revision'])!=int(expected_revision):
        raise ProjectServiceError(409,'project_revision_conflict','论文已变化，请刷新环境后重试。')
    existing=conn.execute('SELECT version FROM project_build_profile_overrides WHERE project_id=?',(project_id,)).fetchone()
    latest=conn.execute('SELECT COALESCE(MAX(version),0) AS version FROM project_build_profile_events WHERE project_id=?',(project_id,)).fetchone()
    version=int(latest['version'])+1;stamp=_now()
    conn.execute('DELETE FROM project_build_profile_overrides WHERE project_id=?',(project_id,))
    if existing:conn.execute('UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?',(stamp,project_id))
    result={'idempotent_replay':False,'version':version,'reset':bool(existing),'profile':build_profile()}
    conn.execute('INSERT INTO project_build_profile_events(id,project_id,request_id,action,profile_json,version,created_at,base_revision,result_json) VALUES(?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,project_id,request_id,'reset',_json(result['profile']),version,stamp,expected_revision,_json(result)))
    return result
