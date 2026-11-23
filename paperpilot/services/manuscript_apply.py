"""Compare import proposals and apply selected dependency groups with version checks."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from pathlib import PurePosixPath

from paperpilot.services import (content_versions, human_revision, logic_graph, manuscript_asset_journal as journal, manuscript_import as store,
                                 manuscript_plan as plan, outline, plan_state, project_resources,
                                 provider_runtime, reference_library, startup_workflow, visual_assets, workflow)
from paperpilot.services.manuscript_sources import safe_path
from paperpilot.services.project_errors import ProjectServiceError


def _stage_field(field):
    return next((stage['id'] for stage in plan_state.STAGES if field in stage['fields']), '')


def _first(conn, sql, args):
    row=conn.execute(sql,args).fetchone()
    return dict(row) if row else None


def _workflow(conn,pid):
    try:
        return workflow.current_snapshot(conn,pid)
    except ProjectServiceError:
        return None


def current_target(conn,pid,item):
    if not pid:
        return None
    kind=item['kind']
    data=item['data']
    title=item['title']
    if kind=='file':
        result=_first(conn,'SELECT path,content FROM files WHERE project_id=? AND path=? COLLATE NOCASE',(pid,data['path']))
        if result:
            result['version']=content_versions.current_version(conn,pid,'file',result['path'])
            return result
    if kind=='term':
        return _first(conn,"SELECT id,definition,version,aliases FROM terms WHERE project_id=? AND term=? COLLATE NOCASE AND status='active' ORDER BY id LIMIT 1",(pid,title))
    if kind=='corpus':
        return _first(conn,'SELECT id,content,source FROM corpus WHERE project_id=? AND title=? COLLATE NOCASE ORDER BY id LIMIT 1',(pid,title))
    if kind=='reference':
        return _first(conn,'''SELECT l.reference_id,l.reference_version,l.cite_key,r.title FROM project_reference_links l
            JOIN reference_records r ON r.id=l.reference_id WHERE l.project_id=? AND l.link_status='linked'
            AND ((?<>'' AND l.cite_key=? COLLATE NOCASE) OR r.title=? COLLATE NOCASE)
            ORDER BY CASE WHEN l.cite_key=? COLLATE NOCASE THEN 0 ELSE 1 END,l.reference_id LIMIT 1''',
            (pid,data['cite_key'],data['cite_key'],data['title'],data['cite_key'])) or (
            _first(conn,"SELECT id AS legacy_id,cite_key,title FROM refs WHERE project_id=? AND cite_key=? COLLATE NOCASE ORDER BY id LIMIT 1",
                   (pid,data['cite_key'])) if data['cite_key'] else None)
    if kind=='image':
        return _first(conn,'''SELECT a.id,a.title,a.current_version_id,v.version_no,v.sha256,v.build_name FROM figure_assets a
            JOIN figure_asset_versions v ON v.id=a.current_version_id WHERE a.project_id=? AND a.title=? COLLATE NOCASE
            AND a.status='active' ORDER BY a.id LIMIT 1''',(pid,title))
    if kind=='outline':
        graph=outline.read_outline(conn,pid)
        return next((node | {'version':graph['version']} for node in graph['nodes'] if node['title'].casefold()==title.casefold()),None)
    if kind=='logic':
        graph=logic_graph.read_graph(conn,pid)
        return next((node | {'version':graph['version']} for node in graph['nodes'] if node['label']==title),None)
    if kind=='research':
        snap=_workflow(conn,pid)
        if not snap:
            return {'enable_planning':True,'message':'采纳这一项会为旧论文启用研究规划并保留当前正文和大纲。不会确认任何研究阶段。'}
        current=plan_state.plan_from_snapshot(snap)
        stage=next(s for s in current['stages'] if s['id']==_stage_field(data['field']))
        if stage['fields'][data['field']] or stage['status'] in {'confirmed','skipped'} or current['status']=='complete':
            return {'text':stage['fields'][data['field']], 'stage_id':stage['id'], 'status':stage['status'],
                    'version':snap['version'],'downstream_review':stage['status'] in {'confirmed','skipped'} or current['status']=='complete'}
    return None


def _resolve_path(path,candidates):
    if path in candidates:
        return candidates[path]
    normalized=PurePosixPath(path.replace('\\','/'))
    key=str(normalized.with_suffix('')) if normalized.suffix else str(normalized)
    matches=[value for name,value in candidates.items() if name.rsplit('.',1)[0]==key or
             PurePosixPath(name).stem==normalized.stem and not normalized.parent.parts]
    return matches[0] if len(matches)==1 else None


def _dependencies(items):
    paths={item['data']['path']:item['id'] for item in items if item['kind'] in {'file','image'}}
    keys={item['data']['cite_key']:item['id'] for item in items if item['kind']=='reference' and item['data']['cite_key']}
    for item in items:
        deps=set()
        data=item['data']
        if item['kind'] in {'outline','reference'} and data.get('path') in paths:
            deps.add(paths[data['path']])
        if item['kind']=='file':
            for name in re.findall(r'\\(?:input|include|bibliography|addbibresource|includegraphics)\*?(?:\[[^\]]*\])?\s*\{([^{}]+)\}',data['content']):
                for path in name.split(','):
                    match=_resolve_path(path.strip(),paths)
                    if match and match!=item['id']:
                        deps.add(match)
            for cites in re.findall(r'\\cite\w*(?:\[[^\]]*\])?\{([^{}]+)\}',data['content']):
                deps.update(keys[key.strip()] for key in cites.split(',') if key.strip() in keys)
        item['dependencies']=sorted(deps)

    groups={item['id']:item['group'] for item in items}
    parents={value:value for value in groups.values()}

    def root(value):
        while parents[value]!=value:
            parents[value]=parents[parents[value]]
            value=parents[value]
        return value

    for item in items:
        for dep in item['dependencies']:
            owner, other=root(item['group']),root(groups[dep])
            if owner!=other:
                first,last=sorted((owner,other))
                parents[last]=first
    for item in items:
        item['group']=root(item['group'])


def _identity(item):
    data=item['data']
    return f"{item['kind']}:{(data.get('field') or data.get('cite_key') or (data.get('path') if item['kind']=='file' else None) or item['title']).casefold()}"


def _replacement_impact(conn,pid,item,current):
    if not pid or not current:
        return []
    kind=item['kind']
    if kind in {'file','reference'}:
        token=current.get('cite_key') or current.get('path')
        if token:
            path=PurePosixPath(token)
            keys={token,str(path.with_suffix(''))} if kind=='file' else {token}
            return [{'path':row['path'],'reason':'包含对该文件或引用键的引用'} for row in conn.execute(
                'SELECT path,content FROM files WHERE project_id=? AND path LIKE ?',(pid,'%.tex'))
                if row['path']!=token and any(key in row['content'] for key in keys)]
    if kind=='image' and current.get('current_version_id'):
        return [dict(row) for row in conn.execute('SELECT consumer_kind,consumer_id FROM asset_references WHERE project_id=? AND version_id=?',
                                                (pid,current['current_version_id']))]
    if kind=='research' and current.get('downstream_review'):
        stages=plan_state.STAGE_IDS
        return [{'stage_id':stage,'reason':'阶段修订后需重新核对'} for stage in stages[stages.index(current['stage_id']):]]
    return []


def preview(conn,batch_id):
    row=store.batch(conn,batch_id)
    materials=store.sources(conn,batch_id)
    items=store.candidates(conn,row,materials) if row['status']!='cancelled' else []
    _dependencies(items)
    applied={entry['item_id']:entry['result'] for entry in store.applied(conn,batch_id)}
    pid=row['project_id']
    project=_first(conn,'SELECT project_revision,archived,lifecycle_state FROM projects WHERE id=?',(pid,)) if pid else None
    seen=set()
    for item in items:
        current=current_target(conn,pid,item)
        item['current']=current
        item['target']=_identity(item)
        item['impact']=_replacement_impact(conn,pid,item,current)
        if item['id'] in applied:
            item.update(adoption='applied',result=applied[item['id']],choices=[])
        elif item['kind']=='note' or current and 'blocked' in current:
            item.update(adoption='review', choices=[])
        elif current and current.get('enable_planning'):
            item.update(adoption='review',choices=['enable_planning','skip'])
        elif current is None:
            item.update(adoption='new',choices=['add','skip'])
        else:
            replaces=item['kind'] in {'file','term','reference','image','outline','logic','research'} and not current.get('legacy_id')
            item.update(adoption='conflict',choices=['keep','replace','skip'] if replaces else ['keep','skip'])
        item['suggested']=item['adoption']=='new' and item['status']=='grounded' and item['target'] not in seen
        if item['suggested']:
            seen.add(item['target'])
    fingerprint=plan.key(row['revision'],project,items)
    return {'batch_id':batch_id,'revision':row['revision'],'project_id':pid,'project_revision':project['project_revision'] if project else None,
            'read_only':row['status']=='cancelled' or bool(project and (project['archived'] or project['lifecycle_state']=='published')),
            'fingerprint':fingerprint,'modules':[{'id':k,'title':v[0],'entry':v[1]} for k,v in plan.MODULES.items()],
            'tools':plan.HANDOFFS, 'items':items, 'primary_source_id':store.primary(row,materials) if row['status']!='cancelled' else ''}


def _sources(item):
    return '；'.join(f'{ref["name"]} · {ref["location"]}' for ref in item['evidence'][:5])


def _replace_reference(conn,pid,old,request_id):
    if not old:
        return
    current=conn.execute('SELECT * FROM project_reference_links WHERE project_id=? AND reference_id=?',(pid,old['reference_id'])).fetchone()
    conn.execute("UPDATE project_reference_links SET link_status='unlinked',updated_at=? WHERE project_id=? AND reference_id=?",
                 (provider_runtime.stamp(),pid,old['reference_id']))
    conn.execute('''INSERT INTO project_reference_link_history(id,project_id,reference_id,reference_version,cite_key,
        citation_snapshot_json,base_reference_version,request_id,action,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (uuid.uuid4().hex,pid,old['reference_id'],current['reference_version'],current['cite_key'],current['citation_snapshot_json'],
         current['reference_version'],request_id+':unlink','unlinked',provider_runtime.stamp()))


def _write_logic(conn,pid,item,current,request):
    graph=logic_graph.read_graph(conn,pid)
    node_id=current['id'] if current else item['id']
    nodes=[n for n in graph['nodes'] if n['id']!=node_id]
    # Record the literal assertion and its source. No inferred support edge is written.
    nodes.append({'id':node_id,'kind':current['kind'] if current else 'claim','label':item['title'],
                  'details':item['data']['text'],'status':'draft','source_anchor':_sources(item),'source_status':'active'})
    edges=graph['edges']
    if item['data'].get('relation') and len(item['evidence'])>1:
        reference=item['evidence'][1]
        evidence_id=plan.key('logic_source',node_id,reference)
        edge_id=plan.key('imported_relation',node_id,evidence_id)
        nodes=[node for node in nodes if node['id']!=evidence_id]
        nodes.append({'id':evidence_id,'kind':'evidence','label':reference['quote'][:100],
                      'details':reference['quote'],'status':'draft','source_anchor':f'{reference["name"]} · {reference["location"]}',
                      'source_status':'active'})
        edges=[edge for edge in edges if edge['id']!=edge_id]
        edges.append({'id':edge_id,'from_node_id':evidence_id,'to_node_id':node_id,'edge_type':item['data']['relation']})
    result=logic_graph.save_graph(conn,pid,base_version=graph['version'],request_id=request,nodes=nodes,edges=edges)
    return {'id':node_id,'version':result['version']}


def _write_research(conn,pid,item,current,request):
    data=item['data']
    snap=_workflow(conn,pid)
    if snap is None:
        if not current or not current.get('enable_planning'):
            raise store.error(409,'manuscript_planning_opt_in_required','需明确选择启用旧论文研究规划。')
        for row in conn.execute('SELECT path FROM files WHERE project_id=?',(pid,)).fetchall():
            content_versions.preserve_legacy_baseline(conn,pid,'file',row['path'])
        content_versions.preserve_legacy_baseline(conn,pid,'outline')
        row=conn.execute('SELECT content FROM outline WHERE project_id=?',(pid,)).fetchone()
        workflow.create_initial_workflow(conn,pid,row['content'] if row else '',provider_runtime.stamp())
        snap=workflow.current_snapshot(conn,pid)
    stage=_stage_field(data['field'])
    plan_view=plan_state.plan_from_snapshot(snap)
    previous=next(value for value in plan_view['stages'] if value['id']==stage).get('source_refs',[])
    version=snap['version']

    def command(action,**extras):
        nonlocal version
        result=startup_workflow.command(conn,pid,{'request_id':request+':'+action,'expected_workflow_version':version,
                                                 'command':action,**extras})
        version=result['workflow_version']
        return result

    if plan_view['status']=='not_started':
        command('start')
    elif plan_view['status']=='cancelled':
        command('resume')
    if current and current.get('downstream_review'):
        command('back',stage_id=stage)
    refs=list({plan.dump(ref):ref for ref in [*previous,*item['evidence']]}.values())
    command('save',stage_id=stage,fields={data['field']:data['text']},source_refs=refs)
    return {'stage_id':stage,'field':data['field'],'version':version}


def _write_outline(conn,pid,item,current,request,parents):
    data=item['data']
    graph=outline.read_outline(conn,pid)
    nodes=graph['nodes']
    node_id=current['id'] if current else item['id']
    parent=next((parents[level] for level in range(data['level']-1,0,-1) if level in parents),None)
    node={'id':node_id,'title':item['title'],'source_anchor':data['path'],'source_status':'active',
          'source_version':content_versions.current_version(conn,pid,'file',data['path'])}
    if current:
        match=next(n for n in nodes if n['id']==node_id)
        match.update(node)
    else:
        nodes=[*nodes,node | {'position':len(nodes),'parent_id':parent,'status':'draft'}]
    for level in [level for level in parents if level>=data['level']]:
        parents.pop(level)
    parents[data['level']]=node_id
    result=outline._write_version(conn,pid,graph['version'],request,nodes,graph['links'],source='manuscript_import')
    if result['nodes']:
        text='\n'.join(f'{i}. {n["title"]}' for i,n in enumerate(result['nodes'],1))
        content_versions.preserve_legacy_baseline(conn,pid,'outline')
        content_versions.save_content(conn,pid,'outline','',text,source='manuscript_import')
    return {'id':node_id,'version':result['version'],'path':data['path']}


def _file_text(content,images,pid,conn,path):
    def rewrite(match):
        resolved=_resolve_path(match[2],images)
        if not resolved:
            return match[0]
        visual_assets.add_reference(conn,pid,resolved['id'],'file',path)
        return match[1]+'{'+resolved['build_name']+'}'
    return re.sub(r'(\\includegraphics\*?(?:\[[^\]]*\])?\s*)\{([^{}]+)\}',rewrite,content)


def _write(conn,storage,pid,bid,item,decision,request,images,parents,markers,prepared):
    kind=item['kind']
    data=item['data']
    current=item['current'] if decision in {'replace','enable_planning'} else None
    if decision=='keep':
        return {'reused':True,**{k:v for k,v in item['current'].items() if k in {'id','path','version','reference_id','cite_key','build_name'}}}
    if kind=='file':
        path=safe_path(current['path'] if current else data['path'])
        base=content_versions.preserve_legacy_baseline(conn,pid,'file',path)
        value=_file_text(data['content'],images,pid,conn,path) if path.endswith('.tex') else data['content']
        result=content_versions.save_content(conn,pid,'file',path,value,expected_version=base,source='manuscript_import')
        mode=conn.execute("SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'",(pid,)).fetchone()
        if mode and not result['unchanged']:
            human_revision.record_event(conn,pid,mode_id=mode['id'],action_type='manual_edit',scope={'kind':'file','path':path},
                                        base_file_versions={path:base},status='checkpoint')
        return result | {'path':path}
    if kind=='outline':
        return _write_outline(conn,pid,item,current,request,parents)
    if kind=='logic':
        return _write_logic(conn,pid,item,current,request)
    if kind=='research':
        return _write_research(conn,pid,item,current,request)
    if kind=='term':
        payload={'term':item['title'],'definition':data['definition'],'aliases':''}
        if not current:
            return project_resources.add_term(conn,pid,payload)
        conn.execute('UPDATE terms SET definition=?,version=version+1 WHERE project_id=? AND id=?',(data['definition'],pid,current['id']))
        project_resources.touch(conn,pid)
        return {'id':current['id'],'version':current['version']+1}
    if kind=='corpus':
        result=project_resources.add_corpus(conn,pid,{'title':item['title'],'content':data['text'],'source':_sources(item)})
        return {k:result[k] for k in ('id','version')}
    if kind=='reference':
        _replace_reference(conn,pid,current,request)
        cite_key=data['cite_key'] or f'imported{item["id"][:10]}'
        reference=reference_library.create_reference(conn,data,source_trace={'kind':'manuscript_import','batch_id':bid,'evidence':item['evidence']})
        return reference_library.link_reference(conn,pid,reference['id'],reference['version'],cite_key,request)
    if kind=='image':
        material, converted=prepared[item['id']]
        if isinstance(converted,ProjectServiceError):
            raise converted
        result=visual_assets.save_asset(conn,storage,pid,content=material['raw'],filename=PurePosixPath(material['name']).name,title=item['title'],
                                       request_id=request,asset_id=current['id'] if current else None,
                                       expected_version=current['version_no'] if current else None,origin_ref=_sources(item),
                                       _prepared=converted,
                                       _before_write=lambda vid,paths:markers.append(journal.record(storage,pid,vid,paths)))
        version=result['version']
        images[data['path']]=version
        project_resources.touch(conn,pid)
        return {'id':result['asset']['id'],'version_id':version['id'],'build_name':version['build_name'],
                **({'caption':data['caption']} if data.get('caption') else {})}
    raise store.error(422,'manuscript_invalid_operation','此材料只供核对，不能应用为正式内容。')


def _validate_choice(item, decision):
    if decision not in item['choices'] or decision=='skip':
        raise store.error(422,'manuscript_invalid_choice','选定项需使用预览中可用的动作。')


def apply(conn,storage,seed_workspace,batch_id,request_id,fingerprint,decisions):
    signature=plan.key(fingerprint,decisions)

    def prior_result():
        prior=_first(conn,'SELECT * FROM manuscript_apply_receipts WHERE batch_id=? AND request_id=?',(batch_id,request_id))
        if not prior:
            return None
        if prior['fingerprint']!=signature:
            raise store.error(409,'idempotency_key_reused','请求编号已用于另一组采纳决定。')
        return json.loads(prior['result']) | {'idempotent_replay':True}

    if replay:=prior_result():
        return replay
    row=store.writable(conn,batch_id)
    checked=preview(conn,batch_id)
    if fingerprint!=checked['fingerprint']:
        raise store.error(409,'manuscript_preview_stale','稿件、AI 结果或目标版本已变化，请刷新预览后核对。')
    items={item['id']:item for item in checked['items']}
    groups=defaultdict(list)
    claimed=set()
    for identity,decision in decisions.items():
        item=items.get(identity)
        if not item:
            raise store.error(422,'manuscript_invalid_choice','选择中包含不存在或版本已变化的候选。')
        if decision=='skip':
            continue
        _validate_choice(item,decision)
        if item['target'] in claimed:
            raise store.error(422,'manuscript_multiple_choice','同一目标选了多条候选，请先选定保留哪一条。')
        claimed.add(item['target'])
        groups[item['group']].append((item,decision))
    if not groups:
        raise store.error(422,'manuscript_selection_required','请至少选择一项要采纳的内容。')
    # Rendering PDF pages can take seconds. Keep the workspace writer lock free during it.
    prepared={}
    for entries in groups.values():
        for item,decision in entries:
            if item['kind']!='image' or decision=='keep':
                continue
            material=store.source_row(conn,batch_id,item['data']['source_id'],include_raw=True)
            try:
                converted=visual_assets.prepare(material['raw'],PurePosixPath(material['name']).name,None)
            except ProjectServiceError as exc:
                converted=exc
            prepared[item['id']]=(material,converted)
    store.begin(conn)
    if replay:=prior_result():
        return replay
    row=store.writable(conn,batch_id)
    if fingerprint!=preview(conn,batch_id)['fingerprint']:
        raise store.error(409,'manuscript_preview_stale','图表准备期间目标或来源已变化，请刷新预览。')
    pid=row['project_id']
    outcomes=[]
    all_markers=[]
    priority={'image':0,'file':1,'reference':2,'outline':3}
    try:
        for group,entries in groups.items():
            conn.execute('SAVEPOINT manuscript_group')
            markers=[]
            new_project=not pid
            target=pid or uuid.uuid4().hex[:12]
            written=[]
            try:
                selecting={i['id'] for i,_ in entries}
                for item,decision in entries:
                    if decision=='keep':
                        continue
                    missing=[items[i]['title'] for i in item['dependencies'] if i not in selecting and items[i]['adoption']!='applied']
                    if missing:
                        raise store.error(422,'manuscript_dependency','请同时选择依赖项或保留对应已有项：'+'、'.join(missing[:8]))
                if new_project:
                    seed_workspace(conn,target,row['name'])
                images={entry['data']['path']:visual_assets.public_version(visual_assets.version(conn,target,entry['result']['version_id']))
                        for entry in items.values() if entry['kind']=='image' and entry['adoption']=='applied' and entry['result'].get('version_id')}
                parents={}
                for item,decision in sorted(entries,key=lambda pair:priority.get(pair[0]['kind'],4)):
                    request=f'mi:{batch_id}:{item["id"]}:{plan.key(request_id)}'
                    result=_write(conn,storage,target,batch_id,item,decision,request,images,parents,markers,prepared)
                    if item['kind']=='image' and decision=='keep':
                        images[item['data']['path']]=visual_assets.public_version(visual_assets.version(conn,target,item['current']['current_version_id']))
                    conn.execute('INSERT INTO manuscript_applied(batch_id,item_id,module,title,result,evidence) VALUES(?,?,?,?,?,?)',
                                 (batch_id,item['id'],item['module'],item['title'],plan.dump(result),plan.dump(item['evidence'])))
                    written.append({'item_id':item['id'],'title':item['title'],'result':result})
                if new_project:
                    conn.execute('UPDATE manuscript_batches SET project_id=? WHERE id=?',(target,batch_id))
                conn.execute('RELEASE manuscript_group')
                pid=target
                all_markers.extend(markers)
                outcomes.append({'group':group,'status':'applied','items':written})
            except (ProjectServiceError,OSError,sqlite3.IntegrityError) as exc:
                conn.execute('ROLLBACK TO manuscript_group')
                conn.execute('RELEASE manuscript_group')
                journal.reconcile(conn,storage,markers)
                message=exc.message if isinstance(exc,ProjectServiceError) else '该组读写失败，未写入任何组内成果。'
                outcomes.append({'group':group,'status':'failed','message':message,'items':[{'item_id':i['id'],'title':i['title']} for i,_ in entries]})
            except BaseException:
                all_markers.extend(markers)
                raise
        result={'batch_id':batch_id,'project_id':pid,'outcomes':outcomes,
                'skipped':[{'item_id':i,'title':items[i]['title']} for i,decision in decisions.items() if decision=='skip']}
        if any(group['status']=='applied' for group in outcomes):
            conn.execute("UPDATE manuscript_batches SET status='applied' WHERE id=?",(batch_id,))
            store.touch(conn,batch_id)
        conn.execute('INSERT INTO manuscript_apply_receipts VALUES(?,?,?,?,?)',(batch_id,request_id,signature,plan.dump(result),provider_runtime.stamp()))
        conn.commit()
        journal.reconcile(conn,storage,all_markers)
        return result
    except BaseException:
        conn.rollback()
        journal.reconcile(conn,storage,all_markers)
        raise
