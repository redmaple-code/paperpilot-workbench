"""Local, bounded extraction for author-supplied manuscript sources."""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from paperpilot.services.project_errors import ProjectServiceError

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 120 * 1024 * 1024
MAX_SOURCES = 250
MAX_TEXT = 1_000_000
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}
TEXT_TYPES = {'.md', '.markdown', '.txt', '.tex', '.bib', '.csv'}
TEX_SUPPORT = {'.cls', '.sty', '.bst', '.bbx', '.cbx', '.cfg', '.def', '.tikz'}
BLOCK_CHARS = 3500


@dataclass
class Material:
    name: str
    kind: str
    raw: bytes
    fragments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parent: str = ''
    location: str = ''


def safe_path(value: str) -> str:
    normalized = value.replace('\\', '/')
    parts = normalized.split('/')
    if (not normalized or len(normalized) > 350 or any(
        part in {'', '.', '..'} or len(part) > 150 or re.search(r'[\x00-\x1f<>:"|?*]', part)
        or part.rstrip('. ') != part or re.match(r'(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part)
        for part in parts
    )):
        raise ProjectServiceError(422, 'unsafe_path', '文件或工程成员路径不受支持。')
    return normalized


def _sections(text: str, suffix: str) -> list[dict]:
    if suffix in TEX_SUPPORT or suffix in {'.csv', '.bib'}:
        return []
    pattern = (r'(?m)^\s*\\(chapter|section|subsection|subsubsection)\*?\s*\{([^{}\n]+)\}'
               if suffix == '.tex' else r'(?m)^[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*$')
    hits = list(re.finditer(pattern, text))
    starts = [0, *(hit.start() for hit in hits if hit.start())]
    lookup = {hit.start(): hit for hit in hits}
    levels = {'chapter': 1, 'section': 1, 'subsection': 2, 'subsubsection': 3}
    result = []
    for first, end in zip(starts, [*starts[1:], len(text)]):
        hit = lookup.get(first)
        heading = hit.group(2).strip() if hit else ''
        level = levels.get(hit.group(1), len(hit.group(1))) if hit else 0
        value = text[hit.end() if hit else first:end].strip()
        if heading or value:
            result.append({'location': f'第 {text.count(chr(10), 0, first)+1} 行', 'heading': heading,
                           'level': level, 'text': value})
    if suffix=='.tex' and result and not result[0]['heading']:
        prefix=result[0]['text']
        abstract=re.search(r'\\begin\{abstract\}(.*?)\\end\{abstract\}',prefix,re.S)
        if abstract and abstract[1].strip():
            result.insert(1,{'location':f'第 {text.count(chr(10),0,text.find(abstract[0]))+1} 行',
                             'heading':'Abstract','level':0,'text':abstract[1].strip()})
    return result


def _text(name: str, raw: bytes, suffix: str) -> Material:
    kind = {'.tex': 'tex', '.bib': 'bibliography', '.csv': 'table'}.get(suffix, 'support' if suffix in TEX_SUPPORT else 'text')
    source = Material(name, kind, raw)
    try:
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            value = raw.decode('utf-16')
        else:
            value = raw.decode('utf-8-sig')
    except UnicodeError:
        try:
            value = raw.decode('gb18030')
        except UnicodeError:
            source.warnings.append('无法识别文本编码，请另存为 UTF-8 后重新上传。')
            return source
    if '\x00' in value or len(value) > MAX_TEXT:
        source.warnings.append('文本过大或含有二进制内容，请拆分或重新导出。')
        return source
    if suffix == '.csv':
        try:
            rows = list(csv.reader(io.StringIO(value), strict=True))
            if len(rows) > 1 and any(len(row) != len(rows[0]) for row in rows[1:]):
                source.warnings.append('部分表格行的列数与表头不一致，保留原值供核对。')
        except csv.Error:
            source.warnings.append('存在无法解析的 CSV 行，原文件可核对。')
    source.fragments = _sections(value, suffix) or [{'location': '全文', 'text': value, 'heading': '', 'level': 0}]
    return source


def _ocr(raw: bytes, pageno: int) -> str:
    import pypdfium2

    with tempfile.TemporaryDirectory(prefix='paperpilot-ocr-') as folder:
        image = Path(folder) / 'page.png'
        with pypdfium2.PdfDocument(raw) as document:
            page = document[pageno-1]
            width, height = page.get_size()
            bitmap = page.render(scale=min(2.5, 3000 / max(width, height)))
            try:
                bitmap.to_pil().save(image)
            finally:
                bitmap.close()
                page.close()
        if os.name == 'nt' and shutil.which('powershell.exe'):
            args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                    str(Path(__file__).resolve().parents[1] / 'adapters' / 'windows_ocr.ps1'), str(image)]
        elif shutil.which('tesseract'):
            args = ['tesseract', str(image), 'stdout']
        else:
            return ''
        result = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace', timeout=35,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.stdout.strip() if result.returncode == 0 else ''


def _pdf_heading(body: str, bookmarks: list[tuple[str, int]] | None = None) -> list[dict]:
    if bookmarks:
        titles = {_heading_key(title):(title, level) for title, level in bookmarks}
        lines = body.splitlines(keepends=True)
        markers = []
        for index, line in enumerate(lines):
            mark = titles.get(_heading_key(line))
            if mark:
                markers.append((index, *mark))
            elif line.strip() in {'摘要', 'Abstract'}:
                markers.append((index, line.strip(), 0))
        if markers:
            parts = []
            if markers[0][0] and ''.join(lines[:markers[0][0]]).strip():
                parts.append({'heading':'','level':0,'text':''.join(lines[:markers[0][0]]).strip()})
            for (index,title,level), end in zip(markers,[*(item[0] for item in markers[1:]),len(lines)]):
                parts.append({'heading':title,'level':level,'text':''.join(lines[index+1:end]).strip()})
            return parts
    pattern = re.compile(r'(?im)^[ \t]*(?:(?:\d+(?:\.\d+)*[.)]?[ \t]+)?(?:abstract|introduction|related work|background|materials and methods|methods?|methodology|results?|experiments?|discussion|conclusions?|references|appendix)(?:[ \t]*[:：])?|(?:第[一二三四五六七八九十]+[章节][ \t]*)?(?:摘[ \t]*要|引言|绪论|相关工作|方法|实验结果|结果|讨论|结论|参考文献))[ \t]*$')
    hits = list(pattern.finditer(body))
    if not hits:
        return [{'heading': '', 'level': 0, 'text': body}]
    parts = []
    if body[:hits[0].start()].strip():
        parts.append({'heading': '', 'level': 0, 'text': body[:hits[0].start()].strip()})
    for hit, after in zip(hits, [*(m.start() for m in hits[1:]), len(body)]):
        parts.append({'heading': hit.group().strip(), 'level': 1, 'text': body[hit.end():after].strip()})
    return parts


def _heading_key(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC',value)).casefold()


def _pdf_bookmarks(reader) -> dict[int, list[tuple[str, int]]]:
    pages = {}

    def visit(destinations, level):
        for value in destinations:
            if isinstance(value,list):
                visit(value,level+1)
                continue
            try:
                number=reader.get_destination_page_number(value)
                title=value.title.strip()
            except (AttributeError,ValueError,KeyError,TypeError):
                continue
            if number is not None and number>=0 and title:
                pages.setdefault(number+1,[]).append((title,level))
    try:
        visit(reader.outline,1)
    except (ValueError,KeyError,TypeError,RecursionError):
        return {}
    return pages


CAPTION = re.compile(r'^\s*(图|表|fig(?:ure)?\.?|table)\s*(\d+)\s*[:：.]\s*(.*)$',re.I)


def pdf_captions(text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        match=CAPTION.match(line)
        if match:
            symbol='表' if match[1].casefold() in {'表','table'} else '图'
            result.append({'label':symbol+' '+match[2],'text':line.strip()})
    return result


def _page_copy(page) -> bytes:
    from pypdf import PdfWriter

    writer=PdfWriter()
    writer.add_page(page,excluded_keys=['/Annots','/AA','/Metadata'])
    with io.BytesIO() as output:
        writer.write(output)
        return output.getvalue()


def _pdf(name: str, raw: bytes, selected_page: int | None) -> list[Material]:
    from pypdf import PdfReader

    source = Material(name, 'pdf', raw)
    children = []
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            source.warnings.append('PDF 已加密，请提供可读取副本。')
            return [source]
        count = len(reader.pages)
    except Exception:
        source.warnings.append('PDF 无法读取，请核对原文件。')
        return [source]
    if count > 300:
        source.warnings.append('PDF 超过 300 页，请按章节拆分。')
    if selected_page is not None and not 1 <= selected_page <= count:
        raise ProjectServiceError(422, 'page_missing', '所选 PDF 页不存在。')
    total = 0
    ocr_left = 6
    bookmarks=_pdf_bookmarks(reader)
    all_titles={_heading_key(title) for entries in bookmarks.values() for title, _ in entries}
    prior=[(page, entries[-1][0]) for page,entries in bookmarks.items() if selected_page and page<selected_page]
    in_bibliography=bool(prior and re.search(r'参考文献|references|bibliography',max(prior)[1],re.I))
    formula_notice=False
    for number in ([selected_page] if selected_page else range(1, min(count, 300)+1)):
        page = reader.pages[number-1]
        location = f'第 {number} 页'
        try:
            body = (page.extract_text() or '').strip()
        except Exception:
            body = ''
        mode = 'text'
        if not body:
            mode = 'ocr'
            if ocr_left:
                ocr_left -= 1
                try:
                    body = _ocr(raw, number)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                    body = ''
        if total + len(body) > MAX_TEXT:
            source.warnings.append(f'{location}起超出提取容量，请将 PDF 拆分。')
            break
        total += len(body)
        if not body:
            source.fragments.append({'location': location, 'page': number, 'text': '', 'heading': '',
                                     'level': 0, 'mode': mode, 'issue': '本页无可用文字，请手工校对或重试本地 OCR。'})
        else:
            body_lines=body.splitlines()
            if len(body_lines)>1 and _heading_key(body_lines[0]).removesuffix(str(number)) in all_titles and body_lines[0].rstrip().endswith(str(number)):
                body='\n'.join(body_lines[1:])
            if not formula_notice and re.search(r'[∑∫∂∇√]|^\s*\(\d+\)\s*$',body,re.M):
                formula_notice=True
                source.warnings.append('检测到数学公式。文本副本便于编辑，公式和复杂排版以原 PDF 页为准。')
            for part in _pdf_heading(body,bookmarks.get(number)):
                if part['heading']:
                    in_bibliography=bool(re.search(r'参考文献|references|bibliography',part['heading'],re.I))
                source.fragments.append({**part, 'location': location, 'page': number, 'mode': mode,
                                         **({'bibliography':True} if in_bibliography else {})})
        if selected_page or len(children) >= 35:
            continue
        before=len(children)
        try:
            for index, image in enumerate(page.images, 1):
                suffix = Path(image.name).suffix.lower()
                if len(children) >= 35:
                    source.warnings.append('文档内嵌图像预览达到上限，仍可核对原页。')
                    break
                if suffix in IMAGE_TYPES and 300 <= len(image.data) <= MAX_FILE_BYTES:
                    children.append(Material(f'{name}.images/page-{number}-{index}{suffix}', 'image', image.data,
                                             parent=name, location=f'{location}，内嵌图像 {index}'))
        except Exception:
            source.warnings.append(f'{location}有无法提取的内嵌图像，请核对原页。')
        captions=pdf_captions(body)
        if captions and len(children)==before:
            try:
                page_bytes=_page_copy(page)
                if len(page_bytes)<=MAX_FILE_BYTES:
                    children.append(Material(f'{name}.images/page-{number}-reference.pdf','page_asset',page_bytes,
                                             fragments=[{'location':location,'page':number,'heading':c['label'],'level':0,'text':c['text']} for c in captions],
                                             parent=name,location=f'{location}，图表所在原页'))
            except Exception:
                source.warnings.append(f'{location}的图表无法生成原页副本，仍可核对 PDF。')
    return [source, *children]


def _inspect_zip(raw: bytes) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > MAX_SOURCES or sum(item.file_size for item in members) > MAX_BATCH_BYTES:
            raise ProjectServiceError(413, 'archive_capacity', '工程成员过多或解压后过大，请拆分工程。')
        return members


def _docx(name: str, raw: bytes) -> list[Material]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    source = Material(name, 'docx', raw)
    children = []
    _inspect_zip(raw)
    doc = Document(io.BytesIO(raw))
    total = 0
    for index, element in enumerate(doc.element.body, 1):
        tag = element.tag.rsplit('}', 1)[-1]
        if tag == 'p':
            para = Paragraph(element, doc)
            value = para.text.strip()
            match = re.match(r'(?:Heading|标题)\s*(\d+)', para.style.name if para.style else '')
            part = {'location': f'段落 {index}', 'text': '' if match else value,
                    'heading': value if match else '', 'level': min(6, int(match[1])) if match else 0}
        elif tag == 'tbl':
            table = Table(element, doc)
            value = '\n'.join(' | '.join(cell.text for cell in row.cells) for row in table.rows)
            part = {'location': f'表格 {index}', 'text': value, 'heading': '', 'level': 0, 'mode': 'table'}
        else:
            continue
        if any(child.tag.rsplit('}', 1)[-1] in {'oMath', 'oMathPara'} for child in element.iter()):
            source.warnings.append(f'第 {index} 个文档元素含公式，请核对 DOCX 原件。')
        total += len(value)
        if total > MAX_TEXT:
            source.warnings.append('提取文字超过上限，请拆分 DOCX。')
            break
        if value:
            source.fragments.append(part)
        # Resolve image relationships where they occur, preserving paragraph locations.
        for drawing in element.xpath('.//a:blip'):
            rid = drawing.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
            image = doc.part.related_parts.get(rid)
            suffix = Path(str(image.partname)).suffix.lower() if image else ''
            if len(children) >= 70:
                break
            if suffix in IMAGE_TYPES and len(image.blob) <= MAX_FILE_BYTES:
                children.append(Material(f'{name}.images/{index}-{len(children)+1}{suffix}', 'image', image.blob,
                                         parent=name, location=f'{part["location"]}，嵌入图像'))
    return [source, *children]


def _archive(name: str, raw: bytes) -> list[Material]:
    root = Material(name, 'archive', raw)
    result = [root]
    seen = set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in _inspect_zip(raw):
            if item.is_dir():
                continue
            path = safe_path(item.filename)
            normalized = path.casefold()
            if normalized in seen or stat.S_ISLNK(item.external_attr >> 16):
                raise ProjectServiceError(422, 'unsafe_archive', 'ZIP 含重复成员或符号链接，请清理后重试。')
            seen.add(normalized)
            if item.flag_bits & 1 or item.file_size > MAX_FILE_BYTES:
                root.warnings.append(f'{path} 已加密或超过单文件上限，未读取。')
                continue
            try:
                payload = archive.read(item)
            except (RuntimeError, OSError, ValueError, zipfile.BadZipFile, NotImplementedError):
                root.warnings.append(f'{path} 无法解压，请核对工程原件。')
                continue
            if Path(path).suffix.lower() in TEXT_TYPES | TEX_SUPPORT | IMAGE_TYPES | {'.pdf', '.docx'}:
                extracted = parse(path, payload, allow_archive=False)
                extracted[0].parent = name
                extracted[0].location = path
                result.extend(extracted[:MAX_SOURCES-len(result)])
                if len(result) >= MAX_SOURCES:
                    root.warnings.append('工程内提取的成员达到上限，可从原 ZIP 核对剩余材料。')
                    break
            else:
                root.warnings.append(f'{path} 为未识别成员，保留在原 ZIP。')
    return result


def parse(name: str, raw: bytes, *, selected_page: int | None = None, allow_archive: bool = True) -> list[Material]:
    name = safe_path(name)
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ProjectServiceError(413, 'source_capacity', '请提供非空文件，单个文件最多 20 MB。')
    suffix = PurePosixPath(name).suffix.lower()
    try:
        if suffix == '.pdf':
            return _pdf(name, raw, selected_page)
        if suffix == '.docx':
            return _docx(name, raw)
        if suffix == '.zip' and allow_archive:
            return _archive(name, raw)
        if suffix in IMAGE_TYPES:
            return [Material(name, 'image', raw)]
        if suffix in TEXT_TYPES | TEX_SUPPORT:
            return [_text(name, raw, suffix)]
    except ProjectServiceError:
        raise
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile):
        kind = {'.docx': 'docx', '.zip': 'archive', '.pdf': 'pdf'}.get(suffix, 'unknown')
        return [Material(name, kind, raw, warnings=['文件结构不可读，请检查格式后重新上传。'])]
    raise ProjectServiceError(422, 'unsupported_source', '此格式不属于当前支持的稿件或附件范围。')
