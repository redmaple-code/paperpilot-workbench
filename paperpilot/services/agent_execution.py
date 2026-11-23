"""Durable external-call ledger, fencing leases and one budget for nested calls."""
from __future__ import annotations
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from paperpilot.services import agent_conversations as store, agent_transport
from paperpilot.services.project_errors import ProjectServiceError

PROCESS_TOKEN=uuid.uuid4().hex
POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='paperpilot-agent')
_scheduled=set()
_lock=threading.Lock()
DEFAULT_LIMITS={'model_calls':8,'tool_calls':20,'tokens':40000,'seconds':300}


def initialize_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS agent_call_ledger(
      id TEXT PRIMARY KEY,run_id TEXT NOT NULL,call_key TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,
      input_hash TEXT NOT NULL,status TEXT NOT NULL,reserved_tokens INTEGER NOT NULL DEFAULT 0,
      actual_tokens INTEGER NOT NULL DEFAULT 0,result_json TEXT NOT NULL DEFAULT '{}',
      started_at TEXT NOT NULL,finished_at TEXT NOT NULL DEFAULT '',UNIQUE(run_id,call_key));
    ''')


def owner_alive(pid):
    if not pid:return False
    if os.name=='nt':
        # os.kill(pid, 0) calls TerminateProcess on Windows; never use it as a probe.
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle:return ctypes.get_last_error()!=87  # access denied stays conservatively alive
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(int(pid),0)
    except ProcessLookupError:return False
    except (PermissionError,OSError):return True
    return True


def recover_orphans(conn):
    """A live owner's request is never assumed unsent merely because time passed."""
    rows=conn.execute("SELECT * FROM agent_runs WHERE status IN ('queued','running')").fetchall()
    with conn:
        for row in rows:
            if not row['runtime_version'] or row['runtime_version']<2:continue
            dead=not owner_alive(row['owner_pid']) or (row['owner_pid']==os.getpid() and row['owner_token']!=PROCESS_TOKEN)
            if not dead:continue
            conn.execute("UPDATE agent_runs SET status='interrupted',error_code='owner_interrupted',lease_token='',updated_at=? WHERE id=? AND status IN ('queued','running')",(store.stamp(),row['id']))
            conn.execute("UPDATE agent_call_ledger SET status='unknown' WHERE run_id=? AND status='pending'",(row['id'],))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_subtasks'").fetchone():
            conn.execute("UPDATE agent_subtasks SET status='interrupted',updated_at=? WHERE status='running' AND run_id IN (SELECT id FROM agent_runs WHERE status NOT IN ('queued','running','awaiting_human'))", (store.stamp(),))


def submit(key,fn,*args):
    with _lock:
        if key in _scheduled:return
        _scheduled.add(key)
    def work():
        try:fn(*args)
        finally:
            with _lock:_scheduled.discard(key)
    try:POOL.submit(work)
    except Exception:
        with _lock:_scheduled.discard(key)
        raise


def claim(conn,run_id):
    token=uuid.uuid4().hex
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM agent_runs WHERE id=?',(run_id,)).fetchone()
        if not row or row['status']!='queued' or row['cancel_requested']:return None
        limits=DEFAULT_LIMITS|json.loads(row['limits_json'])
        budget=json.loads(row['budget_json'])
        remaining=max(0,float(limits['seconds'])-float(budget.get('elapsed',0)))
        conn.execute("UPDATE agent_runs SET status='running',lease_token=?,lease_until=?,owner_pid=?,owner_token=?,updated_at=? WHERE id=? AND status='queued'",
                     (token,time.time()+remaining+15,os.getpid(),PROCESS_TOKEN,store.stamp(),run_id))
    return token


class ExecutionStopped(Exception):
    def __init__(self,code,message):
        super().__init__(message);self.code=code


class Ledger:
    def __init__(self,conn,run_id,lease_token):
        self.conn,self.run_id,self.lease_token=conn,run_id,lease_token
        row=self.row();self.limits=DEFAULT_LIMITS|json.loads(row['limits_json'])
        self.started=time.monotonic()
        self.elapsed_before=float(json.loads(row['budget_json']).get('elapsed',0))

    def row(self):
        return self.conn.execute('SELECT * FROM agent_runs WHERE id=?',(self.run_id,)).fetchone()

    def remaining_seconds(self):
        return float(self.limits['seconds'])-self.elapsed_before-(time.monotonic()-self.started)

    def check(self):
        row=self.row()
        if row['cancel_requested'] or row['status']=='cancelled':raise ExecutionStopped('cancelled','已停止；已发出的外部请求可能仍有费用。')
        if row['status']!='running' or row['lease_token']!=self.lease_token:raise ExecutionStopped('lease_lost','运行所有权已变化，停止接收迟到结果。')
        project=self.conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(row['project_id'],)).fetchone()
        if project is None or project['archived'] or project['lifecycle_state']=='published':raise ExecutionStopped('read_only','论文已只读或删除，停止后续调用。')
        manifest = json.loads(row['input_json']).get('_context_manifest') or {}
        for attachment in manifest.get('attachments') or []:
            source = self.conn.execute('SELECT status,sha256 FROM agent_attachments WHERE id=? AND project_id=?', (attachment['id'],row['project_id'])).fetchone()
            if not source or source['status'] != 'active' or source['sha256'] != attachment['sha256']:
                raise ExecutionStopped('source_revoked','本轮附件已移除或变化，已有结果保留，请核对来源后开始新任务。')
        if self.remaining_seconds()<=0:raise ExecutionStopped('budget_exhausted','运行时间预算已用尽，已有结果保留。')
        return row

    def usage(self):
        rows=self.conn.execute('SELECT kind,reserved_tokens,actual_tokens,status FROM agent_call_ledger WHERE run_id=?',(self.run_id,)).fetchall()
        return {'model_calls':sum(r['kind']=='model' for r in rows),'tool_calls':sum(r['kind'] in ('tool','io') for r in rows),
                'tokens':sum(max(r['actual_tokens'],r['reserved_tokens']) if r['status'] in ('pending','unknown') else r['actual_tokens'] for r in rows),
                'elapsed':self.elapsed_before+time.monotonic()-self.started}

    def invoke(self,key,kind,name,inputs,fn,*,output_tokens=0,timeout=None):
        self.check()
        digest=store.fingerprint(inputs)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            old=self.conn.execute('SELECT * FROM agent_call_ledger WHERE run_id=? AND call_key=?',(self.run_id,key)).fetchone()
            if old:
                if old['input_hash']!=digest:raise ExecutionStopped('checkpoint_input_changed','恢复调用输入已变化，请建立新的运行。')
                if old['status'] in ('pending','unknown'):raise ExecutionStopped('result_unknown','前一次请求结果或费用未知，未重复发送；请先核对。')
                result=json.loads(old['result_json'])
                self.check()
                return result
            used=self.usage()
            # UTF-8 byte length is a conservative upper bound, including tool schemas.
            reserve=len(store.compact(inputs).encode('utf8'))+int(output_tokens)+(1024 if 'messages' in inputs else 0) if kind=='model' else 0
            cap='model_calls' if kind=='model' else 'tool_calls'
            if used[cap]>=self.limits[cap] or used['tokens']+reserve>self.limits['tokens']:
                raise ExecutionStopped('budget_exhausted','调用次数或token预算不足；未发送，已有结果保留。')
            call_id=uuid.uuid4().hex
            self.conn.execute('INSERT INTO agent_call_ledger(id,run_id,call_key,kind,name,input_hash,status,reserved_tokens,started_at) VALUES(?,?,?,?,?,?,\'pending\',?,?)',
                              (call_id,self.run_id,key,kind,name,digest,reserve,store.stamp()))
        try:
            with agent_transport.deadline(min(self.remaining_seconds(),timeout or self.remaining_seconds())):
                result=fn(call_id)
                late=agent_transport.expired()
            if not isinstance(result,dict):result={'ok':False,'error_code':'invalid_result','error':'调用返回格式无效。'}
        except ExecutionStopped:
            with self.conn:self.conn.execute("UPDATE agent_call_ledger SET status='unknown',finished_at=? WHERE id=?",(store.stamp(),call_id))
            raise
        except Exception:
            # The transport may have accepted a request. Never infer no charge from an exception.
            result={'ok':False,'error_code':'provider_result_unknown' if kind=='model' else 'tool_result_unknown','error':'调用结果未知，未自动重试；请核对执行记录。'}
            late=False
        unknown=result.get('error_code') in ('provider_result_unknown','tool_result_unknown')
        usage=result.get('usage') or {}
        actual=int(usage.get('total_tokens') or reserve) if kind=='model' else 0
        status='unknown' if unknown else 'succeeded' if result.get('ok') else 'failed'
        # Persist the result before checking cancellation, so a late known result can be reconciled.
        with self.conn:
            self.conn.execute('UPDATE agent_call_ledger SET status=?,result_json=?,actual_tokens=?,finished_at=? WHERE id=?',
                              (status,store.compact(result),actual,store.stamp(),call_id))
            used=self.usage()
            self.conn.execute('UPDATE agent_runs SET budget_json=?,tokens_used=? WHERE id=?',
                              (store.compact(used),used['tokens'],self.run_id))
        self.check()
        if unknown:raise ExecutionStopped('result_unknown',result.get('error','外部调用结果未知。'))
        if late:raise ExecutionStopped('tool_timeout','调用超时，结果已登记；未接收迟到结果或调度后续操作。')
        return result

    def persist_elapsed(self):
        with self.conn:
            self.conn.execute('UPDATE agent_runs SET budget_json=? WHERE id=?',(store.compact(self.usage()),self.run_id))
