"""Conversation-owned source files with bounded text extraction."""
import base64
import hashlib
import io
from pathlib import PurePath
import uuid
from paperpilot.services import agent_conversations as store
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.executescript('''CREATE TABLE IF NOT EXISTS agent_attachments(
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL,conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,
      filename TEXT NOT NULL,sha256 TEXT NOT NULL,content TEXT NOT NULL,size INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,
      UNIQUE(project_id,conversation_id,request_id));''')
    columns={row['name'] for row in conn.execute('PRAGMA table_info(agent_attachments)')}
    if 'pages_json' not in columns:
        conn.execute("ALTER TABLE agent_attachments ADD COLUMN pages_json TEXT NOT NULL DEFAULT '[]'")
    if 'result_bytes' not in columns:
        conn.execute("ALTER TABLE agent_attachments ADD COLUMN result_bytes BLOB")
        from paperpilot.services import results
        for row in conn.execute("""SELECT id,filename,content,sha256 FROM agent_attachments WHERE
            lower(filename) LIKE '%.csv' OR lower(filename) LIKE '%.tsv'""").fetchall():
            plain = row["content"].encode("utf-8")
            raw = next((candidate for candidate in (plain, b"\xef\xbb\xbf" + plain)
                        if hashlib.sha256(candidate).hexdigest() == row["sha256"]), None)
            if raw is None:
                continue
            try:
                results.parse(raw, row["filename"])
            except ProjectServiceError:
                continue
            conn.execute("UPDATE agent_attachments SET content='',result_bytes=? WHERE id=?", (raw, row["id"]))


def list_files(conn,pid,cid):
    store.get(conn,pid,cid)
    return [dict(row) for row in conn.execute("""SELECT id,filename,sha256,size,length(content) AS characters,created_at,
        (result_bytes IS NOT NULL OR lower(filename) LIKE '%.csv' OR lower(filename) LIKE '%.tsv') AS result_data
        FROM agent_attachments WHERE project_id=? AND conversation_id=? AND status='active' ORDER BY created_at,id""", (pid,cid))]


def create(conn,pid,cid,data):
    store.get(conn,pid,cid)
    try:
        content=base64.b64decode(data['content_base64'],validate=True)
    except ValueError as exc:
        raise ProjectServiceError(422,'invalid_attachment','文件编码无效。') from exc
    if len(content)>20*1024*1024:
        raise ProjectServiceError(413,'attachment_too_large','单个文件最多20MB，请拆分后上传。')
    filename=PurePath(data['filename'].replace('\\','/')).name
    extension=PurePath(filename).suffix.lower()
    sha=hashlib.sha256(content).hexdigest()
    pages_json=store.compact(data.get('pages') or [])
    previous=conn.execute('SELECT * FROM agent_attachments WHERE project_id=? AND conversation_id=? AND request_id=?',(pid,cid,data['request_id'])).fetchone()
    if previous:
        if previous['sha256']!=sha or previous['filename']!=filename or previous['pages_json']!=pages_json:
            raise ProjectServiceError(409,'idempotency_key_reused','上传请求编号已用于另一份文件。')
        return {'id':previous['id'],'filename':filename,'status':previous['status']}
    try:
        if extension=='.pdf':
            from pypdf import PdfReader
            pdf=PdfReader(io.BytesIO(content))
            pages=data.get('pages') or list(range(1,len(pdf.pages)+1))
            if len(pages)>40 or any(p<1 or p>len(pdf.pages) for p in pages):
                raise ProjectServiceError(413,'page_range_required','请选取最多40页的有效页码后上传。')
            chunks=[]
            for page in pages:
                chunks.append(f'第{page}页\n'+(pdf.pages[page-1].extract_text() or ''))
                if sum(len(item) for item in chunks)>16000:
                    raise ProjectServiceError(413,'page_range_required','提取文本超过16000字，请选择较少页码后上传。')
            text='\n\n'.join(chunks)
            if not any((pdf.pages[p-1].extract_text() or '').strip() for p in pages):
                raise ProjectServiceError(422,'scan_requires_vision','此PDF没有可读取文本，请从图像工具导入并使用视觉识别。')
        elif extension in {'.csv', '.tsv'}:
            from paperpilot.services import results
            results.parse(content, filename)
            if len(list_files(conn, pid, cid)) >= 10:
                raise ProjectServiceError(413, 'attachment_scope_limit', '当前对话最多保留 10 份同行资料。')
            identity = uuid.uuid4().hex
            conn.execute('''INSERT INTO agent_attachments(id,project_id,conversation_id,request_id,
                filename,sha256,content,size,pages_json,created_at,result_bytes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (identity,pid,cid,data['request_id'],filename,sha,'',len(content),pages_json,store.stamp(),content))
            return {'id':identity,'filename':filename,'characters':0,'status':'active','result_data':True}
        elif extension in {'.txt','.md','.tex','.bib'}:
            text=content.decode('utf-8-sig')
        else:
            raise ProjectServiceError(422,'unsupported_attachment','支持 PDF、TXT、Markdown、LaTeX、BibTeX、CSV 和 TSV。图像可从图像工具导入。')
    except ProjectServiceError:
        raise
    except Exception as exc:
        raise ProjectServiceError(422,'attachment_unreadable','文件读取失败，请检查文件格式或使用UTF-8文本。') from exc
    if not text.strip() or len(text)>16000:
        raise ProjectServiceError(413,'attachment_text_limit','请提供1至16000字的可读取文本，或缩小PDF页码范围。')
    current=list_files(conn,pid,cid)
    if len(current)>=10 or sum(i['characters'] for i in current)+len(text)>20000:
        raise ProjectServiceError(413,'attachment_scope_limit','当前对话附件合计最多20000字、10份，请移除已完成使用的附件后上传。')
    identity=uuid.uuid4().hex
    conn.execute('INSERT INTO agent_attachments(id,project_id,conversation_id,request_id,filename,sha256,content,size,pages_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (identity,pid,cid,data['request_id'],filename,sha,text,len(content),pages_json,store.stamp()))
    return {'id':identity,'filename':filename,'characters':len(text),'status':'active'}
