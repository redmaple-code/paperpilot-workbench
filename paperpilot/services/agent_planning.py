"""Conversation proposals use the existing workflow and its transaction ledger."""
from paperpilot.services import startup_workflow, workflow
from paperpilot.services.plan_state import STAGES, plan_from_snapshot
from paperpilot.services.project_errors import ProjectServiceError


def preview(conn, pid, payload, version, selection):
    snapshot = workflow.current_snapshot(conn, pid)
    startup_workflow.ensure_version(snapshot, version)
    if set(payload) - {'stage_id', 'fields', 'mode', 'revise'}:
        raise ProjectServiceError(422, 'invalid_input', '方案包含未支持的操作，请重新整理。')
    plan = plan_from_snapshot(snapshot)
    index = next((i for i, s in enumerate(STAGES) if s['id'] == payload.get('stage_id')), None)
    if index is None or payload.get('mode', 'draft') not in {'draft', 'confirm'}:
        raise ProjectServiceError(422, 'invalid_stage', '请选择有效的论文方案和采纳方式。')
    fields = payload.get('fields')
    if not isinstance(fields, dict) or not fields or set(fields) - set(STAGES[index]['fields']):
        raise ProjectServiceError(422, 'invalid_stage_fields', '方案内容与目标不匹配。')
    if any(not isinstance(value, str) or len(value) > 16000 for value in fields.values()):
        raise ProjectServiceError(422, 'invalid_stage_fields', '单项方案需要是16000字以内的文本。')
    if not isinstance(payload.get('revise', False), bool):
        raise ProjectServiceError(422, 'invalid_input', '修订标记无效。')
    stage = plan['stages'][index]
    revision = stage['status'] in {'confirmed', 'skipped'} or plan['status'] == 'complete'
    if revision and not payload.get('revise'):
        raise ProjectServiceError(409, 'revision_required', '此方案已有确认结果，请明确提出修订并说明后续影响。')
    if index > plan['current_index'] and (payload.get('mode') == 'confirm' or plan['status'] != 'active'):
        raise ProjectServiceError(409, 'stage_locked', '先确认当前讨论的方案，再确认后续方案。')
    indices = selection.get('selected_indices')
    partial = indices is not None
    if partial:
        keys = [key for key in STAGES[index]['fields'] if key in fields]
        if not indices or any(type(i) is not int or i < 0 or i >= len(keys) for i in indices):
            raise ProjectServiceError(422,'invalid_selection','请选择有效的方案条目。')
        fields = {keys[i]:fields[keys[i]] for i in indices}
    merged = {**stage['fields'], **fields}
    confirmation = payload.get('mode') == 'confirm' and not partial
    if confirmation and any(not value.strip() for value in merged.values()):
        raise ProjectServiceError(422, 'incomplete_plan', '方案还有待讨论的内容，可以先保存为草稿。')
    return {'stage_id':stage['id'], 'title':stage['title'],
            'fields':[{'key':key, 'label':STAGES[index]['fields'][key], 'before':stage['fields'][key], 'after':value} for key,value in merged.items() if not partial or key in fields],
            'selected_fields': fields,
            'confirmation':confirmation, 'revision_required':revision,
            'effect':('重新打开此方案和下游产出，保留原确认历史。' if revision else '')
                     + ('采纳后确认这份方案，继续后续讨论。' if confirmation else '采纳所选内容为讨论草稿，继续完善。')}


def apply(conn, pid, payload, version, selection, request_id):
    diff = preview(conn, pid, payload, version, selection)
    plan = plan_from_snapshot(workflow.current_snapshot(conn, pid))
    current_version = version

    def command(name, **extra):
        nonlocal current_version
        result = startup_workflow.command(conn, pid, {
            'request_id':f'{request_id}:{name}', 'expected_workflow_version':current_version,
            'command':name, **extra})
        current_version = result['workflow_version']
        return result

    if plan['status'] == 'not_started':
        command('start')
    elif plan['status'] == 'cancelled':
        command('resume')
    if diff['revision_required']:
        command('back', stage_id=payload['stage_id'])
    command('save', stage_id=payload['stage_id'], fields=diff['selected_fields'])
    if diff['confirmation']:
        command('review', stage_id=payload['stage_id'])
        command('confirm', stage_id=payload['stage_id'])
    return {'version':current_version, 'stage_id':payload['stage_id'], 'confirmed':diff['confirmation'],
            'message':'这份方案已确认，我们可以继续讨论下一步。' if diff['confirmation'] else '方案草稿已保存，可以继续讨论和修改。'}
