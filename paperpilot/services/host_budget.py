"""Machine-wide SQLite arbitration shared by independent project databases and SyncTeX."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from paperpilot.services import tex_runtime
from paperpilot.services.project_errors import ProjectServiceError


def limits(profile=None):
    p=profile or {}
    try:
        return {'cpu':min(8,max(1,int(p.get('PWA_TEX_CPU_LIMIT',os.getenv('PWA_TEX_CPU_LIMIT','2'))))),
            'memory_mb':min(8192,max(256,int(p.get('PWA_TEX_MEMORY_MB',os.getenv('PWA_TEX_MEMORY_MB','2048'))))),
            'disk_mb':max(16,min(8192,int(p.get('PWA_TEX_DISK_MB',os.getenv('PWA_TEX_DISK_MB','256')))))}
    except (ValueError,TypeError) as exc:
        raise ProjectServiceError(422,'build_resource_config_invalid','编译CPU、内存与磁盘预算须为有效整数。') from exc


def connection():
    default=Path(os.getenv('LOCALAPPDATA') or Path.home()/'.local/share')/'PaperPilot'/'runtime'/'host-budget.sqlite3'
    path=Path(os.getenv('PWA_HOST_BUDGET_DB') or default).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path,timeout=10);conn.row_factory=sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS claims(
      identity TEXT PRIMARY KEY, project_path TEXT NOT NULL, job_id TEXT NOT NULL, kind TEXT NOT NULL,
      owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL, cpu INTEGER NOT NULL,
      memory_mb INTEGER NOT NULL, disk_mb INTEGER NOT NULL, acquired_at REAL NOT NULL)''')
    return conn


def identity(project,job_id):return tex_runtime.root_hash(project)+':'+job_id


def recover():
    conn=connection()
    try:rows=[dict(r) for r in conn.execute('SELECT * FROM claims')]
    finally:conn.close()
    for row in rows:
        if tex_runtime.owner_state(row)!='dead':continue
        project=Path(row['project_path'])
        if tex_runtime.root_hash(project)!=row['identity'].split(':')[0]:continue
        cleanup=tex_runtime.recover(project,job_ids={row['job_id']},force=False,build_only=row['kind']=='build')
        if any(not x.get('cleanup_verified') for x in cleanup):continue
        release(project,row['job_id'],row)


def acquire(project,job_id,profile=None,kind='build'):
    project=Path(project).resolve();requested=limits(profile);owner=tex_runtime.current_owner()
    recover()
    conn=connection()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            key=identity(project,job_id)
            old=conn.execute('SELECT * FROM claims WHERE identity=?',(key,)).fetchone()
            if old:
                if old['owner_pid']==owner['owner_pid'] and old['owner_token']==owner['owner_token'] and all(old[k]==requested[k] for k in requested):
                    return {'identity':key,'reused':True,**owner,**requested}
                return None
            rows=conn.execute('SELECT * FROM claims').fetchall()
            totals={'cpu':int(os.getenv('PWA_HOST_CPU_BUDGET','4')),'memory_mb':int(os.getenv('PWA_HOST_MEMORY_MB','4096')),'disk_mb':int(os.getenv('PWA_HOST_DISK_MB','2048'))}
            if len(rows)>=max(1,int(os.getenv('PWA_HOST_TEX_SLOTS','2'))) or any(sum(r[k] for r in rows)+requested[k]>v for k,v in totals.items()):return None
            parent=project
            while not parent.exists() and parent!=parent.parent:parent=parent.parent
            free=shutil.disk_usage(parent).free//(1024*1024)
            same_volume_reserved=sum(r['disk_mb'] for r in rows if Path(r['project_path']).anchor==project.anchor)
            if free<requested['disk_mb']+same_volume_reserved+128:return None
            conn.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?)',(key,str(project),job_id,kind,owner['owner_pid'],owner['owner_token'],requested['cpu'],requested['memory_mb'],requested['disk_mb'],time.time()))
            return {'identity':key,'reused':False,**owner,**requested}
    except ValueError as exc:
        raise ProjectServiceError(422,'host_budget_config_invalid','本机资源预算配置无效，请修复PWA_HOST配置后重试。') from exc
    finally:conn.close()


def release(project,job_id,owner):
    conn=connection()
    try:
        with conn:return conn.execute('DELETE FROM claims WHERE identity=? AND owner_pid=? AND owner_token=?',(identity(project,job_id),owner['owner_pid'],owner['owner_token'])).rowcount
    finally:conn.close()


def inventory():
    conn=connection()
    try:
        rows=[dict(r) for r in conn.execute('SELECT kind,cpu,memory_mb,disk_mb FROM claims')]
        return {'claims':rows,'occupied':len(rows),'slot_limit':max(1,int(os.getenv('PWA_HOST_TEX_SLOTS','2'))),
            'limits':{'cpu':int(os.getenv('PWA_HOST_CPU_BUDGET','4')),'memory_mb':int(os.getenv('PWA_HOST_MEMORY_MB','4096')),'disk_mb':int(os.getenv('PWA_HOST_DISK_MB','2048'))},
            'includes':'build_and_source_map','disk_enforcement':'reservation_free_space_file_limit_and_directory_monitor'}
    finally:conn.close()


def directory_bytes(path):
    total=0
    for parent,dirs,files in os.walk(path,followlinks=False):
        for name in dirs+files:
            target=Path(parent)/name
            if target.is_symlink() or getattr(target.lstat(),'st_file_attributes',0)&0x400:
                raise ValueError('unsafe link in build run')
        total+=sum((Path(parent)/name).stat().st_size for name in files)
    return total
