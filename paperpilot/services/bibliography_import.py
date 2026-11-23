"""Reviewed copies from fixed project reference versions into engineering BibTeX."""
import difflib
import hashlib
import json
import re
import uuid

from paperpilot.services import content_versions, human_revision
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.workflow import current_snapshot


def compact(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def entry_spans(text):
    """Locate balanced braced entries; reject ambiguous input rather than destroy it."""
    spans=[];offset=0
    while offset<len(text):
        if text[offset]=='%':
            end=text.find('\n',offset);offset=len(text) if end<0 else end+1;continue
        if text[offset]!='@':offset+=1;continue
        match=re.match(r'@([A-Za-z]+)\s*([{(])',text[offset:])
        if not match:raise ProjectServiceError(422,'bib_parse_incomplete','工程bib语法无法安全定位，请先修复后重新预览；原文件保留。')
        start=offset;opening=match[2];closing='}' if opening=='{' else ')';offset+=match.end();body=offset
        braces=0;quoted=False;finished=False
        while offset<len(text):
            char=text[offset]
            if char=='\\':offset+=2;continue
            if char=='%':
                end=text.find('\n',offset);offset=len(text) if end<0 else end+1;continue
            if char=='"' and braces==0:quoted=not quoted
            if char=='{':braces+=1
            elif char=='}' and braces>0:braces-=1
            elif char==closing and braces==0 and not quoted:
                finished=True;offset+=1;break
            offset+=1
        if not finished:raise ProjectServiceError(422,'bib_parse_incomplete','工程bib条目未闭合，未修改任何内容。')
        if match[1].lower() in {'comment','preamble','string'}:continue
        key=re.match(r'\s*([^\s,{}()]+)\s*,',text[body:offset-1])
        if not key:raise ProjectServiceError(422,'bib_parse_incomplete','工程bib引用键无法安全定位，未修改任何内容。')
        spans.append((key[1],start,offset))
    return spans


def escaped(value):
    mapping={'\\':r'\textbackslash{}','{':r'\{','}':r'\}','%':r'\%','&':r'\&','_':r'\_','#':r'\#','$':r'\$','~':r'\textasciitilde{}','^':r'\textasciicircum{}'}
    return ''.join(mapping.get(c,c) for c in ' '.join(str(value or '').split()))


def mode(conn,pid):
    project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(pid,)).fetchone()
    if not project:raise ProjectServiceError(404,'project_missing','论文不存在。')
    workflow=current_snapshot(conn,pid)
    if project['archived'] or project['lifecycle_state']=='published':
        raise ProjectServiceError(409,'bibliography_read_only','论文已归档或已发布；不会自动解除。')
    return human_revision.active_mode(conn,pid)


def preview(conn,pid,reference_id,target_path):
    mode(conn,pid)
    if not target_path.endswith('.bib'):
        raise ProjectServiceError(422,'bib_target_required','请选择当前工程中的bib文件。')
    link=conn.execute("SELECT * FROM project_reference_links WHERE project_id=? AND reference_id=? AND link_status!='unlinked'",(pid,reference_id)).fetchone()
    if not link:raise ProjectServiceError(404,'reference_link_required','来源尚未关联到当前论文，请先按固定版本关联。')
    source=conn.execute('SELECT metadata_json FROM reference_record_versions WHERE reference_id=? AND version=?',(reference_id,link['reference_version'])).fetchone()
    if not source:raise ProjectServiceError(409,'reference_version_missing','关联的来源版本不可用，请核对引用。')
    metadata=json.loads(source['metadata_json']);key=link['cite_key']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,119}',key):
        raise ProjectServiceError(422,'bib_key_invalid','引用键含不安全的BibTeX字符；请先修正关联键。')
    files=[dict(r) for r in conn.execute("SELECT path,content FROM files WHERE project_id=? AND path LIKE '%.bib' ORDER BY path",(pid,))]
    target=next((f for f in files if f['path']==target_path),None)
    if not target:raise ProjectServiceError(404,'bib_target_missing','工程目标文件不存在。')
    # Never replace unsaved work, including an editor draft on another page.
    draft=conn.execute('SELECT content,base_version,base_content FROM editor_local_drafts WHERE project_id=? AND path=?',(pid,target_path)).fetchone()
    base=content_versions.current_version(conn,pid,'file',target_path)
    if (draft and (draft['content']!=target['content'] or draft['base_content']!=target['content'] or draft['base_version']!=base)) or conn.execute("SELECT 1 FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?",(pid,target_path)).fetchone():
        raise ProjectServiceError(409,'bib_unsaved_draft','目标bib有未提交草稿，请先保存或处理草稿后重新预览；输入保留。')
    matches=[]
    for f in files:
        for k,start,end in entry_spans(f['content']):
            if k==key:matches.append((f['path'],start,end))
    if len(matches)>1:raise ProjectServiceError(409,'bib_duplicate_key','工程内存在重复引用键，请先消除歧义再导入；未修改文件。')
    if matches and matches[0][0]!=target_path:
        raise ProjectServiceError(409,'bib_key_in_other_file','引用键已存在于 '+matches[0][0]+'；请选择该文件比较，避免创建重复键。')
    fields={'title':metadata.get('title'),'author':metadata.get('authors'),'year':metadata.get('year'),
        'journal':metadata.get('venue'),'doi':metadata.get('doi'),'url':metadata.get('canonical_url')}
    entry='@article{'+key+',\n'+',\n'.join('  '+name+' = {'+escaped(value)+'}' for name,value in fields.items() if value)+'\n}'
    current=target['content']
    proposed=current[:matches[0][1]]+entry+current[matches[0][2]:] if matches else current.rstrip()+'\n\n'+entry+'\n'
    identity={'project_id':pid,'reference_id':reference_id,'reference_version':link['reference_version'],'source':metadata,
        'cite_key':key,'link':dict(link),'target_path':target_path,'base_version':base,'files':files,'mode_id':mode(conn,pid)['id']}
    return {'reference_id':reference_id,'reference_version':link['reference_version'],'cite_key':key,'target_path':target_path,'base_version':base,
        'conflict':bool(matches),'current':current,'proposed':proposed,'preview_fingerprint':digest(identity),'source_policy':'fixed_project_reference_version; explicit copy; no automatic sync',
        'diff':'\n'.join(difflib.unified_diff(current.splitlines(),proposed.splitlines(),fromfile=target_path+' 当前',tofile=target_path+' 拟导入',lineterm=''))}


def commit(conn,pid,payload):
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    active=mode(conn,pid);fingerprint=digest(payload)
    old=conn.execute('SELECT operation,content_sha256,result_json FROM editor_write_events WHERE project_id=? AND request_id=?',(pid,payload['request_id'])).fetchone()
    if old:
        if old['operation']!='bibliography_import' or old['content_sha256']!=fingerprint:
            raise ProjectServiceError(409,'bib_request_conflict','请求编号已用于不同操作，未修改工程。')
        return json.loads(old['result_json'])|{'idempotent_replay':True}
    view=preview(conn,pid,payload['reference_id'],payload['target_path'])
    if view['preview_fingerprint']!=payload['preview_fingerprint']:
        raise ProjectServiceError(409,'bib_preview_stale','来源或工程版本已变化，请重新比较差异，未覆盖当前文件。')
    if not payload['confirm']:
        raise ProjectServiceError(422,'bib_confirmation_required','需要明确确认本次工程文件差异。')
    if view['conflict'] and payload['conflict_resolution']!='replace':
        raise ProjectServiceError(409,'bib_conflict_choice_required','同键条目已存在，请明确选择替换或取消保留。')
    result=content_versions.save_content(conn,pid,'file',view['target_path'],view['proposed'],expected_version=view['base_version'],source='bibliography_import')
    event=human_revision.record_event(conn,pid,mode_id=active['id'],action_type='bibliography_import',
        scope={k:view[k] for k in ('reference_id','reference_version','cite_key','target_path')},base_file_versions={view['target_path']:view['base_version']},status='checkpoint',request_id=payload['request_id'])
    result={'ok':True,**result,'event_id':event,'path':view['target_path'],'reference_id':view['reference_id'],'reference_version':view['reference_version'],'compiled':False}
    conn.execute('INSERT INTO editor_write_events(id,project_id,request_id,operation,target_path,base_version,content_sha256,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex,pid,payload['request_id'],'bibliography_import',view['target_path'],view['base_version'],fingerprint,compact(result),human_revision.timestamp()))
    return result
