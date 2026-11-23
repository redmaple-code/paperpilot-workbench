"""Compare manuscript, reference and existing terminology before author adoption."""
from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from posixpath import normpath

from paperpilot.services import agent_actions, manuscript_import, manuscript_plan, project_resources, provider_runtime
from paperpilot.services.reference_library import normalize_doi, normalize_title
from paperpilot.services.term_recognition import definition, key, names, recognize
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS term_comparison_versions (
            term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, snapshot TEXT NOT NULL, sources TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(term_id, version)
        );
        CREATE TABLE IF NOT EXISTS term_comparison_receipts (
            scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            result TEXT NOT NULL, PRIMARY KEY(scope, request_id)
        );
    ''')


def _error(code, message, status=422):
    return ProjectServiceError(status, code, message)


def _project(conn, project_id):
    if not project_id:
        return None
    project = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    if not project:
        raise _error('project_missing', '找不到论文工作区。', 404)
    return dict(project)


def _batch(conn, project_id, batch_id):
    if batch_id:
        batch = manuscript_import.batch(conn, batch_id)
        if batch['status'] == 'cancelled' or (project_id and batch['project_id'] != project_id):
            raise _error('term_source_unavailable', '此导入批次已取消或不属于当前论文。', 404)
        return batch
    return conn.execute('''SELECT b.* FROM manuscript_batches b WHERE b.project_id=? AND b.status='applied'
           AND EXISTS (SELECT 1 FROM manuscript_sources s WHERE s.batch_id=b.id AND s.state='active'
                       AND s.kind IN ('pdf','docx','text','tex')) ORDER BY b.updated_at DESC,b.id LIMIT 1''', (project_id,)).fetchone()


def _tex_documents(primary, materials):
    available={source['name']:source for source in materials if source['kind']=='tex' and source['parent_id']==primary['parent_id']}
    pending=[primary]
    chosen={}
    while pending:
        source=pending.pop()
        if source['id'] in chosen:
            continue
        chosen[source['id']]=source
        parent=PurePosixPath(source['name']).parent
        for fragment in source['fragments']:
            for name in re.findall(r'\\(?:input|include)\s*\{([^{}]+)\}',fragment['text']):
                name=name.strip()
                path=name if PurePosixPath(name).suffix else name+'.tex'
                target=available.get(normpath(str(parent/path))) or available.get(normpath(path))
                if target and target['id'] not in chosen:
                    pending.append(target)
    return list(chosen.values())


def _manuscript(conn, project_id, batch):
    if not batch:
        rows = conn.execute('''SELECT f.path,f.content,COALESCE((SELECT MAX(v.version) FROM content_versions v
            WHERE v.project_id=f.project_id AND v.target_kind='file' AND v.target_path=f.path),0) AS version
            FROM files f WHERE f.project_id=? AND lower(f.path) LIKE '%.tex' ORDER BY f.path''', (project_id,))
        bib = [entry for row in conn.execute("SELECT content FROM files WHERE project_id=? AND lower(path) LIKE '%.bib' ORDER BY path",(project_id,))
               for entry in manuscript_plan.references(row['content'])]
        return [{'kind':'manuscript', 'source_id':row['path'], 'name':row['path'], 'location':f'第 {number} 行',
                 'version':row['version'], 'text':text}
                for row in rows for number,text in enumerate(row['content'].splitlines(),1) if text.strip()], bib, []

    materials = manuscript_import.sources(conn, batch['id'])
    primary_id = manuscript_import.primary(batch, materials)
    primary = next((source for source in materials if source['id'] == primary_id), None)
    if not primary:
        return [], [], []
    documents = [primary]
    if primary['kind'] == 'tex':
        documents = _tex_documents(primary,materials)
    parts = []
    for source in documents:
        for fragment in source['fragments']:
            if fragment.get('issue') or fragment.get('bibliography') or re.match(r'(?i)^\s*(?:references|bibliography|参考文献)\s*$', fragment['heading']):
                continue
            origin = {'kind':'manuscript', 'source_id':source['id'], 'name':source['name'], 'location':fragment['location'],
                      'fragment_id':fragment['id'], 'version':manuscript_plan.key(fragment['heading'],fragment['text']), 'page':fragment.get('page'), 'heading':fragment['heading'],
                      'url':f"/api/manuscript-imports/{batch['id']}/sources/{source['id']}/original"}
            if fragment['heading']:
                parts.append(origin | {'text':fragment['heading']})
            if fragment['text'].strip():
                parts.append(origin | {'text':fragment['text']})
    bib = [item for source in materials if source['kind'] == 'bibliography'
           for fragment in source['fragments'] for item in manuscript_plan.references(fragment['text'])]
    bib.extend({'key':item['data']['original_label'], 'fields':{'title':item['data']['title'], 'doi':_doi(item['data']['notes'])}}
               for item in manuscript_plan._numbered_references(primary) if primary['kind'] == 'pdf')
    by_fragment={part['fragment_id']:part for part in parts}
    ai_items=[]
    if conn.execute('SELECT 1 FROM manuscript_analyses WHERE batch_id=? LIMIT 1',(batch['id'],)).fetchone():
        for item in manuscript_import.current_model_candidates(conn,batch,materials,primary_id):
            if item['kind']!='term':
                continue
            for evidence in item['evidence']:
                part=by_fragment.get(evidence.get('fragment_id'))
                if part and len(item['title'])<=120:
                    ai_items.append({'term':item['title'],'aliases':[],'evidence':{k:v for k,v in part.items() if k!='text'} |
                                     {'quote':evidence['quote'],'definition':'','suggestion':item['data'].get('suggested_definition','')}})
    return parts,bib,ai_items


def _doi(text):
    match = re.search(r'(?i)\b10\.\d{4,9}/[^\s,;]+', text or '')
    return normalize_doi(match[0].rstrip('.).') if match else '')


def _references(conn, project_id, bibliography, selected):
    links = {row['reference_id']:dict(row) for row in conn.execute('''SELECT reference_id,reference_version,cite_key,citation_snapshot_json
              FROM project_reference_links WHERE project_id=? AND link_status IN ('linked','update_available') ORDER BY cite_key''', (project_id,))}
    fields = 'id,current_version,title,doi'
    listed = links.copy()
    by_title, by_doi = defaultdict(list), defaultdict(list)
    if bibliography:
        for row in conn.execute(f"SELECT {fields} FROM reference_records WHERE status='active' ORDER BY id"):
            by_title[normalize_title(row['title'])].append(row)
            if row['doi']:
                by_doi[_doi(row['doi'])].append(row)
    issues = []
    for entry in bibliography:
        cite_key = entry['key']
        info = entry['fields']
        if any(link['cite_key'] == cite_key for link in links.values()):
            continue
        doi = normalize_doi(info.get('doi')) or _doi(info.get('title'))
        title = normalize_title(info.get('title'))
        matching_links = [link for link in links.values() if
                          (doi and doi == _doi(json.loads(link['citation_snapshot_json']).get('doi'))) or
                          (title and title == normalize_title(json.loads(link['citation_snapshot_json']).get('title')))]
        if matching_links:
            continue
        matching = by_doi.get(doi) or [row for row in by_title.get(title,[]) if not doi or not row['doi']]
        if len(matching) != 1:
            issues.append({'cite_key':cite_key, 'title':manuscript_plan.redact(info.get('title','')),
                           'status':'ambiguous' if matching else 'unmatched', 'options':[row['id'] for row in matching]})
        for row in matching:
            listed.setdefault(row['id'], {'reference_id':row['id'], 'reference_version':row['current_version'],
                                          'cite_key':cite_key if len(matching)==1 else '', 'ambiguous':len(matching)>1})
    if selected is not None:
        for reference_id in selected:
            if reference_id in listed:
                continue
            row = conn.execute(f"SELECT {fields} FROM reference_records WHERE id=? AND status='active'", (reference_id,)).fetchone()
            if not row:
                issues.append({'status':'unavailable','reference_id':reference_id})
                continue
            listed[reference_id] = {'reference_id':reference_id,'reference_version':row['current_version'],'cite_key':''}
    return listed, issues


def reference_terms(conn, project_id, reference_id, version=None, *, recognizer=recognize):
    if project_id:
        _project(conn, project_id)
    record = conn.execute("SELECT current_version,status FROM reference_records WHERE id=?", (reference_id,)).fetchone()
    link = conn.execute('''SELECT reference_version,cite_key FROM project_reference_links
                           WHERE project_id=? AND reference_id=? AND link_status IN ('linked','update_available')''',
                        (project_id,reference_id)).fetchone()
    if not record or record['status'] != 'active':
        raise _error('reference_missing','参考文献不可用。',404)
    version = version or (link['reference_version'] if link else record['current_version'])
    row = conn.execute('SELECT metadata_json FROM reference_record_versions WHERE reference_id=? AND version=?', (reference_id,version)).fetchone()
    if not row:
        raise _error('reference_version_missing','找不到该参考文献版本。',404)
    metadata = json.loads(row['metadata_json'])
    title = manuscript_plan.redact(metadata.get('title') or '')
    origin = {'kind':'reference','source_id':reference_id,'version':version,'name':title,
              'cite_key':link['cite_key'] if link and link['reference_version']==version else ''}
    parts = []
    pdf_url = ''
    attachment = conn.execute('''SELECT e.id,e.fragments_json,e.source_version,e.source_url,e.attachment_path,
        i.id AS import_id,i.status,json_extract(i.preview_json,'$.content_kind') AS content_kind,
        json_extract(i.preview_json,'$.page_count') AS page_count,
        json_extract(i.preview_json,'$.text_page_count') AS text_page_count,
        json_extract(i.source_json,'$.source_edition') AS source_edition
        FROM reference_attachments a
        JOIN external_evidence e ON e.id=a.external_evidence_id AND e.project_id=a.project_id
        JOIN oa_imports i ON i.id=e.import_id AND i.project_id=e.project_id
        WHERE a.project_id=? AND a.reference_id=? AND a.reference_version=?''', (project_id,reference_id,version)).fetchone()
    if attachment and attachment['status']=='imported':
        grade = 'excerpt' if attachment['source_version'] in {'oa-import-v1', 'oa-import-v2-partial'} else 'fulltext'
        pdf_url = (f"/api/projects/{project_id}/imports/{attachment['import_id']}/pdf"
                   if attachment['attachment_path'] and attachment['content_kind']=='pdf' else '')
        parts = [origin | {'grade':grade,'location':piece.get('location') or '原文', 'text':piece['text'],
                 'attachment_id':attachment['id'], 'attachment_version':attachment['source_version'],
                 'url':pdf_url,
                 'source_url':manuscript_plan.redact(attachment['source_url'] or '')}
                 for piece in json.loads(attachment['fragments_json'] or '[]') if piece.get('text','').strip()]
    if metadata.get('abstract'):
        parts.append(origin | {'grade':'abstract','location':'摘要','text':metadata['abstract']})
    if title:
        parts.append(origin | {'grade':'metadata','location':'题名','text':title})
    if metadata.get('keywords'):
        raw = metadata['keywords']
        parts.append(origin | {'grade':'metadata','location':'原文关键词','text':'关键词：'+('，'.join(raw) if isinstance(raw,list) else raw)})
    grade = parts[0]['grade'] if parts else 'metadata'
    status = grade if grade in {'fulltext','excerpt'} or not attachment else 'unreadable'
    items = recognizer(parts)
    limitation = {'excerpt':'此版本只有部分原文可检索。', 'unreadable':'此版本的原文在当前论文中不可读。'}.get(status,'')
    return origin | {'status':status,'limitation':limitation,'current_version':record['current_version'],'items':items,
                     'pdf_url':pdf_url,
                     'source_edition':attachment['source_edition'] if attachment else None,
                     'page_count':attachment['page_count'] if attachment else None,
                     'text_page_count':attachment['text_page_count'] if attachment else None,
                     'count':len({key(item['term']) for item in items})}


def _distinct(values):
    result={}
    for value in values:
        if value:
            result.setdefault(key(value),value)
    return list(result.values())


def _similar_label(left, right):
    # Preserve distinctions such as +, numbered schemes and critical-point order.
    marks = r'\d+|[+]+|[一二三四五六七八九十]阶|\b(?:first|second|third)\b'
    return (min(len(left),len(right))>=7 and re.findall(marks,left)==re.findall(marks,right)
            and SequenceMatcher(None,left,right,autojunk=False).ratio()>=.92)


def _qualified_name(value):
    return bool(re.search(r'\d|\+|[一二三四五六七八九十]阶|\b(?:first|second|third)\b|^[a-z]{2,}-[a-z0-9]+$',value))


def _historical_sources(conn, project_id, term_id, version):
    row = conn.execute('SELECT sources FROM term_comparison_versions WHERE term_id=? AND version=?', (term_id,version)).fetchone()
    if row:
        return json.loads(row['sources'])
    if version != 1:
        return []
    rows = conn.execute('''SELECT a.evidence,b.revision,b.id AS batch_id FROM manuscript_applied a JOIN manuscript_batches b ON b.id=a.batch_id
        WHERE b.project_id=? AND a.module='terms' AND json_extract(a.result,'$.id')=?''', (project_id,term_id))
    return [source | {'kind':'manuscript','version':row['revision'],'definition':'',
                      'url':f"/api/manuscript-imports/{row['batch_id']}/sources/{source['source_id']}/original"}
            for row in rows for source in json.loads(row['evidence'])]


def _evidence(items):
    result = {}
    for source in items:
        identity = manuscript_plan.key(*(source.get(field) for field in ('kind','source_id','version','location','quote','definition')))
        result[identity] = source | {'id':identity}
    return list(result.values())


def _compare(conn, project, manuscript_items, reference_items):
    groups = defaultdict(list)
    alias_owners = defaultdict(set)
    inputs = [*manuscript_items,*reference_items]
    shared_definitions = defaultdict(list)
    meanings_by_name = defaultdict(set)
    for item in inputs:
        owner = key(item['term'])
        for alias in item['aliases']:
            alias_owners[key(alias)].add(owner)
        meaning = key(definition(item['evidence']['definition']))
        if len(meaning)>=18 and not _qualified_name(owner):
            shared_definitions[meaning].append(owner)
            meanings_by_name[owner].add(meaning)
    for item in inputs:
        normalized = key(item['term'])
        owners = alias_owners[normalized] - {normalized}
        if len(owners)==1:
            normalized = next(iter(owners))
        if len(meanings_by_name[normalized])==1:
            meaning=next(iter(meanings_by_name[normalized]))
            normalized=next(name for name in shared_definitions[meaning] if len(meanings_by_name[name])==1)
        groups[normalized].append(item)

    project_id = project['id'] if project else None
    existing = [dict(row) for row in conn.execute("SELECT id,term,definition,aliases,version FROM terms WHERE project_id=? AND status='active' ORDER BY term,id", (project_id,))]
    domains = []
    if project and project['domain_id']:
        domains = [dict(row) for row in conn.execute('''SELECT t.id,t.term,t.definition,t.aliases,t.version,t.domain_id
            FROM domain_terms t JOIN domains d ON d.id=t.domain_id AND d.version=t.version
            WHERE t.domain_id=? AND t.status='active' ORDER BY t.term,t.id''', (project['domain_id'],))]
    lookup = defaultdict(list)
    for term in existing:
        for label in _distinct([term['term'],*names(term['aliases'])]):
            lookup[key(label)].append(term)

    candidates=[]
    manuscript_keys={key(item['term']) for item in manuscript_items}
    for normalized, group in groups.items():
        paper = [item for item in group if item['evidence']['kind']=='manuscript']
        if not paper:
            # Reference-only terms must share meaningful vocabulary with the manuscript.
            tokens = set(re.findall(r'[a-z0-9]{3,}|[\u3400-\u9fff]{2,}', normalized))
            if not any(token in other for token in tokens for other in manuscript_keys):
                continue
        preferred = next((item['term'] for item in group if key(item['term'])==normalized), group[0]['term'])
        aliases = _distinct(item for row in group for item in [row['term'],*row['aliases']] if key(item)!=normalized)
        keys = {normalized,*map(key,aliases)}
        matches = {term['id']:term for label in keys for term in lookup[label]}
        similar = not matches and [term for label,terms in lookup.items() if _similar_label(normalized,label) for term in terms]
        if similar:
            matches={term['id']:term for term in similar}
        matches = sorted(matches.values(),key=lambda term:(key(term['term'])!=normalized,term['id']))
        sources = _evidence([item['evidence'] for item in group] + [
            {'kind':'domain','source_id':term['id'],'name':term['term'],'version':term['version'], 'location':'领域术语库',
             'quote':term['definition'],'definition':term['definition']}
            for term in domains if keys.intersection({key(term['term']),*map(key,names(term['aliases']))})])
        meanings=_distinct(definition(source['definition']) for source in sources)
        prior_meanings={key(definition(term['definition'])) for term in matches if definition(term['definition'])}
        expansions={key(alias) for item in group if key(item['term'])==normalized for alias in item['aliases']
                    if not key(alias).startswith(normalized+' ')}
        english={value for value in expansions if not re.search(r'[\u3400-\u9fff]',value)}
        shorthand_conflict = len(english)>1 or any(len(alias_owners[label])>1 for label in keys)
        conflict = shorthand_conflict or bool(similar) or len(matches)>1 or len(prior_meanings | set(map(key,meanings)))>1
        target=matches[0] if matches else None
        if target:
            target = target | {'sources':_historical_sources(conn,project_id,target['id'],target['version'])}
            existing_aliases = {key(target['term']),*map(key,names(target['aliases']))}
            aliases = _distinct([*aliases,*([preferred] if key(preferred)!=key(target['term']) else [])])
            aliases = [alias for alias in aliases if key(alias)!=key(target['term'])]
            new_aliases = any(key(alias) not in existing_aliases for alias in aliases)
            old_ids={source['id'] for source in _evidence(target['sources'])}
            new_sources=bool({source['id'] for source in sources} - old_ids)
            outcome='conflict' if conflict else 'supplement' if new_aliases or new_sources or (meanings and not prior_meanings) else 'covered'
        else:
            outcome='conflict' if conflict else 'new'
        candidates.append({'id':manuscript_plan.key('term-comparison',normalized), 'canonical':target['term'] if target else preferred,
                           'aliases':aliases,'sources':sources,'existing':matches,'target':target,'status':outcome,
                           'definition':meanings[0] if len(meanings)==1 else '', 'reference_only':not paper,
                           'evidence_state':'low' if sources and all(s.get('grade')=='metadata' for s in sources) else 'definition' if meanings else 'usage',
                           'reason':'参考文献术语与稿件术语共享词汇，请核对是否适用于正文。' if not paper else ''})
    return sorted(candidates,key=lambda item:(item['reference_only'],key(item['canonical']),item['id']))


def preview(conn, project_id=None, batch_id=None, reference_ids=None):
    batch = _batch(conn, project_id, batch_id)
    project_id = project_id or (batch['project_id'] if batch else None)
    project = _project(conn, project_id)
    manuscript_parts,bib,ai_items = _manuscript(conn, project_id, batch)
    manuscript_items = [*recognize(manuscript_parts),*ai_items]
    cited={token.strip() for part in manuscript_parts for group in re.findall(r'\\(?:cite\w*)\*?(?:\s*\[[^]]*\])*\s*\{([^}]+)\}',part['text'])
           for token in group.split(',') if token.strip()}
    cited.update(match for part in manuscript_parts for match in re.findall(r'\[@([\w:.+-]+)',part['text']))
    if cited:
        bib=[entry for entry in bib if entry['key'] in cited]
    listed, issues = _references(conn, project_id, bib, reference_ids)
    references=[]
    reference_items=[]
    selected=set(reference_ids) if reference_ids is not None else {ref for ref,value in listed.items()
               if not value.get('ambiguous') and (not cited or value['cite_key'] in cited)}
    for ref, value in listed.items():
        try:
            result=reference_terms(conn,project_id,ref,value['reference_version'])
        except ProjectServiceError as exc:
            issues.append({'status':'unavailable','reference_id':ref,'cite_key':value['cite_key'],'message':exc.message})
            continue
        items=result.pop('items')
        references.append(result | {'selected':ref in selected,'cite_key':value['cite_key'], 'ambiguous':bool(value.get('ambiguous'))})
        if ref in selected:
            reference_items.extend(items)
    items = _compare(conn, project, manuscript_items, reference_items)
    summary = {'manuscript':len({key(item['term']) for item in manuscript_items}), 'references':len({key(item['term']) for item in reference_items}),
               'merged':len(items),'conflicts':sum(item['status']=='conflict' for item in items),
               'missing_fulltext':sum(row['selected'] and row['status']!='fulltext' for row in references),
               'adopted':len({item['target']['id'] for item in items if item['target'] and item['target']['sources']})}
    frozen={'project_id':project_id,'revision':project['project_revision'] if project else None,
            'batch_id':batch['id'] if batch else None,'batch_revision':batch['revision'] if batch else None,
            'reference_ids':sorted(selected),'references':references,'issues':issues,'items':items}
    return frozen | {'summary':summary,'fingerprint':manuscript_plan.key(frozen),
                     'read_only':bool(project and (project['archived'] or project['lifecycle_state']=='published'))}


def _snapshot(conn, term, sources):
    snapshot = {field:term[field] for field in ('term','definition','aliases','version')}
    conn.execute('INSERT OR IGNORE INTO term_comparison_versions VALUES(?,?,?,?,?)',
                 (term['id'],term['version'],manuscript_plan.dump(snapshot),manuscript_plan.dump(_evidence(sources)),provider_runtime.stamp()))


def history(conn, project_id, term_id):
    term=conn.execute('SELECT id,term,definition,aliases,version FROM terms WHERE project_id=? AND id=?',(project_id,term_id)).fetchone()
    if not term:
        raise _error('term_missing','找不到本论文的术语。',404)
    rows=conn.execute('SELECT version,snapshot,sources,created_at FROM term_comparison_versions WHERE term_id=? ORDER BY version DESC',(term_id,))
    versions=[dict(row) | {'snapshot':json.loads(row['snapshot']),'sources':json.loads(row['sources'])} for row in rows]
    if not any(row['version']==term['version'] for row in versions):
        versions.insert(0,{'version':term['version'],'snapshot':dict(term),'sources':_historical_sources(conn,project_id,term_id,term['version'])})
    return {'term':dict(term),'versions':versions}


def apply(conn, payload, project_id=None, batch_id=None, seed_workspace=None):
    scope = f'batch:{batch_id}' if batch_id else f'project:{project_id}'
    signature = manuscript_plan.key(payload)
    manuscript_import.begin(conn)
    prior=conn.execute('SELECT fingerprint,result FROM term_comparison_receipts WHERE scope=? AND request_id=?',(scope,payload['request_id'])).fetchone()
    if prior:
        if prior['fingerprint']!=signature:
            raise _error('idempotency_key_reused','请求编号已用于其他术语采纳。',409)
        return json.loads(prior['result']) | {'idempotent_replay':True}
    current=preview(conn,project_id,batch_id,payload['reference_ids'])
    if current['fingerprint']!=payload['fingerprint']:
        raise _error('term_comparison_stale','术语或来源已变化，请重新识别并比对。',409)
    project_id=current['project_id']
    if project_id:
        agent_actions.require_writable(conn,project_id)
    else:
        batch=manuscript_import.writable(conn,batch_id)
        project_id=uuid.uuid4().hex[:12]
        seed_workspace(conn,project_id,batch['name'])
        conn.execute("UPDATE manuscript_batches SET project_id=?,status='applied' WHERE id=?",(project_id,batch_id))
        manuscript_import.touch(conn,batch_id)
    items={item['id']:item for item in current['items']}
    outcomes=[]
    touched=set()
    active={key(row['term']):row['id'] for row in conn.execute("SELECT id,term FROM terms WHERE project_id=? AND status='active'",(project_id,))}
    for item_id, choice in payload['decisions'].items():
        item=items.get(item_id)
        if not item:
            raise _error('term_candidate_missing','选中的术语不在本次预览。')
        action=choice['action']
        if action=='separate' and not choice.get('canonical'):
            raise _error('term_resolution_required','独立保留时请填写可区分的术语名称。')
        chosen_sources=choice.get('source_ids')
        if action=='separate' and item['status']=='conflict' and not chosen_sources:
            raise _error('term_resolution_required','请为独立术语选择支持其含义的原文。')
        sources=item['sources']
        if chosen_sources is not None:
            selected=set(chosen_sources)
            sources=[source for source in sources if source['id'] in selected]
            if not selected or len(sources)!=len(selected):
                raise _error('term_source_unavailable','所选原文不在这条候选中。')
        possible={term['id']:term for term in item['existing']}
        target_id=choice.get('target_id') or (item['target']['id'] if item['target'] else None)
        target=possible.get(target_id) if action=='merge' else None
        if (action=='merge' and not target) or (action=='add' and possible):
            raise _error('term_resolution_required','请选择已有条目合并，或为独立术语填写不同的名称。')
        canonical=manuscript_plan.redact((choice.get('canonical') or (target['term'] if target else item['canonical'])).strip())
        if not canonical or (key(canonical) in active and (not target or active[key(canonical)]!=target['id'])):
            raise _error('term_name_conflict','术语名称已存在。请选择合并，或为独立术语填写区分名称。')
        evidence_id=choice.get('definition_id')
        meanings={source['id']:source['definition'] for source in sources if definition(source['definition'])}
        if evidence_id and evidence_id not in meanings:
            raise _error('term_definition_unverified','所选定义缺少可核对的来源。')
        meaning=meanings[evidence_id] if evidence_id else (target['definition'] if target else '')
        proposals=choice['aliases'] if choice.get('aliases') is not None else item['aliases']
        aliases=_distinct(manuscript_plan.redact(alias.strip()) for alias in [*(names(target['aliases']) if target else []),*proposals]
                          if alias.strip() and key(alias)!=key(canonical))
        values={'term':canonical,'definition':meaning,'aliases':'，'.join(aliases)}
        if len(values['aliases'])>4000:
            raise _error('term_aliases_too_long','别名超过长度限制，请精简后采纳。')
        if target:
            if target['id'] in touched:
                raise _error('term_target_repeated','每次只能为同一个已有术语提交一项合并。')
            old_sources=_historical_sources(conn,project_id,target['id'],target['version'])
            _snapshot(conn,target,old_sources)
            combined=_evidence([*old_sources,*sources])
            changed=any(values[name]!=target[name] for name in values) or combined!=_evidence(old_sources)
            term=target | values | {'version':target['version']+int(changed)}
            if changed:
                conn.execute('UPDATE terms SET term=?,definition=?,aliases=?,version=? WHERE project_id=? AND id=?',
                             (canonical,meaning,values['aliases'],term['version'],project_id,target['id']))
                _snapshot(conn,term,combined)
                active.pop(key(target['term']),None)
                project_resources.touch(conn,project_id)
            outcome='updated' if changed else 'covered'
        else:
            term=project_resources.add_term(conn,project_id,values)
            _snapshot(conn,term,sources)
            outcome='added'
        touched.add(term['id'])
        active[key(canonical)]=term['id']
        outcomes.append({'item_id':item_id,'id':term['id'],'term':canonical,'version':term['version'],'status':outcome})
        if batch_id and outcome!='covered':
            conn.execute('INSERT OR IGNORE INTO manuscript_applied VALUES(?,?,?,?,?,?)',
                         (batch_id,manuscript_plan.key('term-comparison-adopt',term['id'],term['version']),'terms',canonical,
                          manuscript_plan.dump(outcomes[-1]),manuscript_plan.dump(sources)))
    result={'project_id':project_id,'items':outcomes}
    conn.execute('INSERT INTO term_comparison_receipts VALUES(?,?,?,?)',(scope,payload['request_id'],signature,manuscript_plan.dump(result)))
    return result
