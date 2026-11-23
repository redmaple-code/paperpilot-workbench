"""Persistent aggregate reservations include unknown attempts, so retries cannot evade a cap."""
import json
import math

from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS continuation_budget_attempts(
        job_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, reserved_tokens INTEGER NOT NULL,
        reserved_cost REAL, used_tokens INTEGER, used_cost REAL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def price(pricing, input_tokens, output_tokens):
    if not pricing:
        return None
    amount = input_tokens/1_000_000*pricing['input_per_million']+output_tokens/1_000_000*pricing['output_per_million']
    if not math.isfinite(amount):
        raise ProjectServiceError(422, 'pricing_estimate_invalid', '费率或用量无法生成有限估价；未发送请求，请核对费率来源。')
    return amount


def usage(conn, batch_id):
    row = conn.execute('''SELECT COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS tokens,
        SUM(COALESCE(used_cost,reserved_cost)) AS cost, COUNT(*) AS attempts,
        SUM(CASE WHEN used_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_attempts
        FROM continuation_budget_attempts WHERE batch_id=?''', (batch_id,)).fetchone()
    return dict(row)


def reserve(conn, project_id, job_id, scope, input_tokens, output_tokens):
    batch_id = scope.get('continuation_batch_id')
    if not batch_id:
        return
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    batch = conn.execute('SELECT * FROM continuation_batches WHERE id=? AND project_id=?', (batch_id, project_id)).fetchone()
    if not batch or batch['status'] in {'paused','cancelled','completed'}:
        raise ProjectServiceError(409, 'continuation_paused', '续写批次已暂停或结束，本段没有发送。')
    previous = conn.execute('SELECT * FROM continuation_budget_attempts WHERE job_id=?', (job_id,)).fetchone()
    if previous:
        raise ProjectServiceError(409, 'continuation_attempt_already_reserved', '本段已有发送记录，请先核对结果；不会重复发送。')
    budget = json.loads(batch['budget_json'])
    used = usage(conn, batch_id)
    reserved = input_tokens+output_tokens
    estimate = price(budget.get('pricing'), input_tokens, output_tokens)
    if used['tokens']+reserved > budget['max_job_tokens']:
        raise ProjectServiceError(429, 'continuation_budget_exceeded', '整批已用/未知请求预留加本段上限超过token硬限制；批次暂停，未发送本段。')
    amount = budget.get('hard_budget_amount')
    if amount is not None and (estimate is None or (used['cost'] or 0)+estimate > amount):
        raise ProjectServiceError(429, 'continuation_budget_exceeded', '整批金额硬限制无法满足；含结果不明的费用预留，未发送本段。')
    from paperpilot.services.provider_runtime import stamp
    now = stamp()
    conn.execute('INSERT INTO continuation_budget_attempts VALUES(?,?,?,?,NULL,NULL,?,?,?)', (job_id,batch_id,reserved,estimate,'reserved',now,now))


def settle(conn, job_id, reported_usage, status):
    row = conn.execute('SELECT * FROM continuation_budget_attempts WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        return
    # No usage is not zero usage. An unknown/failed attempt keeps its reservation.
    total = reported_usage.get('total_tokens')
    input_tokens = reported_usage.get('prompt_tokens', reported_usage.get('input_tokens'))
    output_tokens = reported_usage.get('completion_tokens', reported_usage.get('output_tokens'))
    def valid(value):
        return isinstance(value,int) and not isinstance(value,bool) and value >= 0
    input_tokens = input_tokens if valid(input_tokens) else None
    output_tokens = output_tokens if valid(output_tokens) else None
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens+output_tokens
    if not isinstance(total,(int,float)) or isinstance(total,bool) or not math.isfinite(total) or total <= 0:
        total = None
    if input_tokens is not None and output_tokens is not None:
        total = max(total or 0, input_tokens+output_tokens) or None
    batch = conn.execute('SELECT budget_json FROM continuation_batches WHERE id=?', (row['batch_id'],)).fetchone()
    budget = json.loads(batch['budget_json'])
    cost = None
    if input_tokens is not None and output_tokens is not None:
        cost = price(budget.get('pricing'), input_tokens, output_tokens)
    from paperpilot.services.provider_runtime import stamp
    conn.execute('UPDATE continuation_budget_attempts SET used_tokens=?,used_cost=?,status=?,updated_at=? WHERE job_id=?', (total,cost,status,stamp(),job_id))
    consumed = usage(conn, row['batch_id'])
    if consumed['tokens'] > budget['max_job_tokens'] or (budget.get('hard_budget_amount') is not None and (consumed['cost'] or 0) > budget['hard_budget_amount']):
        conn.execute("UPDATE continuation_batches SET status='paused',pause_reason='actual_budget_exceeded' WHERE id=?", (row['batch_id'],))
