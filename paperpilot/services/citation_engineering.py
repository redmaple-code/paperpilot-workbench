"""Bounded, versioned engineering-input scan; unsupported syntax stays visible."""
from pathlib import PurePosixPath
import re


CITE = re.compile(r'\\([A-Za-z]*cite[A-Za-z]*)\*?(?:\s*\[[^\]\n]*\]){0,2}\s*\{([^{}]+)\}', re.I)
SUPPORTED = {'cite','citep','citet','citealp','citealt','citeauthor','citeyear','citetitle','nocite','autocite','parencite','textcite','footcite'}
RESOURCE = re.compile(r'\\(bibliography|addbibresource|input|include)(?:\[[^\]\n]*\])?\s*\{([^{}]+)\}')
ENTRY = re.compile(r'@(?!(?:comment|string|preamble)\b)[A-Za-z]+\s*[({]\s*([^,\s]+)\s*,', re.I)
INLINE = re.compile(r'\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}')


def visible(text):
    # Preserve offsets and line numbers while removing only unescaped comments.
    output=[]
    for line in text.splitlines(keepends=True):
        stop=None
        for i,char in enumerate(line):
            if char=='%' and (i-len(line[:i].rstrip('\\')))%2==0:
                stop=i;break
        output.append(line if stop is None else line[:stop]+''.join('\n' if c=='\n' else ' ' for c in line[stop:]))
    return ''.join(output)


def resolve(path, value, extension):
    value=value.strip().replace('\\','/')
    if not value or value.startswith('/') or ':' in value or any(c in value for c in '{}#$'):
        return None
    parts=[]
    for part in (PurePosixPath(path).parent/PurePosixPath(value)).parts:
        if part in ('','.'):continue
        if part=='..':
            if not parts:return None
            parts.pop()
        else:parts.append(part)
    target='/'.join(parts)
    return target if PurePosixPath(target).suffix else target+extension


def location(file, offset):
    text=file['content']
    return {'path':file['path'],'version':file.get('version',0),'line':text.count('\n',0,offset)+1,
            'column':offset-(text.rfind('\n',0,offset)+1)+1,'offset':offset,'input_kind':file.get('input_kind','saved')}


def scan(files, roots=None):
    by_path={f['path']:dict(f) for f in files}
    roots=list(roots) if roots is not None else [f['path'] for f in files if f['path'].lower().endswith(('.tex','.ltx'))]
    pending=list(roots); visited=set(); bibs=set(); cited=set(); defined=set(); citations=[]; definitions=[]; unsupported=[]
    while pending:
        path=pending.pop(0)
        if path in visited:continue
        visited.add(path)
        file=by_path.get(path)
        if not file:
            unsupported.append({'path':path,'reason':'工程输入缺失'});continue
        text=visible(file['content'])
        for match in CITE.finditer(text):
            if match[1].lower() not in SUPPORTED:
                unsupported.append({**location(file,match.start()),'reason':'未支持的自定义引用命令：'+match[1]});continue
            for key in match[2].split(','):
                key=key.strip()
                if not key:continue
                if '\\' in key or any(c in key for c in '#$'):
                    unsupported.append({**location(file,match.start()),'reason':'动态引用键未展开'});continue
                cited.add(key); citations.append({'key':key,'command':match[1],**location(file,match.start())})
        for match in INLINE.finditer(text):
            defined.add(match[1]); definitions.append({'key':match[1],**location(file,match.start()),'kind':'bibitem'})
        for match in RESOURCE.finditer(text):
            is_bib=match[1] in {'bibliography','addbibresource'}
            for raw in match[2].split(','):
                target=resolve(path,raw,'.bib' if is_bib else '.tex')
                # TeX resolves ordinary input/bibliography paths from the build cwd.
                root_target=resolve('main.tex',raw,'.bib' if is_bib else '.tex')
                if root_target in by_path:
                    target=root_target
                elif target in by_path and target!=root_target:
                    unsupported.append({**location(file,match.start()),'reason':'仅发现相对包含文件目录的资源；普通TeX从工程工作目录查找，需实际工具链核验'})
                if target is None or target not in by_path:
                    unsupported.append({**location(file,match.start()),'reason':'动态、越界或缺失的工程引用：'+raw});continue
                if is_bib:bibs.add(target)
                else:pending.append(target)
        for match in re.finditer(r'\\(?:[A-Za-z]*cite[A-Za-z]*|bibliography|addbibresource|input|include)\b',text):
            if not any(m.start()==match.start() for m in [*CITE.finditer(text),*RESOURCE.finditer(text)]):
                unsupported.append({**location(file,match.start()),'reason':'引用或工程输入命令未被当前解析器覆盖'})
        for match in re.finditer(r'\\(?:newcommand|renewcommand|def|includeonly|if[A-Za-z]*)\b',text):
            unsupported.append({**location(file,match.start()),'reason':'宏定义、条件或includeonly未展开；当前清单为静态候选范围，需真实工具链核验'})
    if not bibs and 'references.bib' in by_path:
        # Preserve the established single-bib project contract, while exposing that
        # an explicit bibliography command was not discovered in this scope.
        bibs.add('references.bib')
        unsupported.append({'path':'references.bib','reason':'当前范围未发现显式书目命令；只核对项目约定的references.bib，不代表已经参与编译'})
    for path in sorted(bibs):
        file=by_path[path];text=visible(file['content'])
        for match in ENTRY.finditer(text):
            defined.add(match[1]);definitions.append({'key':match[1],**location(file,match.start()),'kind':'bibtex'})
        if re.search(r'@string\b|\bcrossref\s*=',text,re.I):unsupported.append({'path':path,'reason':'BibTeX宏/crossref未展开，仍需真实工具链核验'})
    if '*' in cited:
        cited.discard('*');cited.update(defined)
    return {'cited':sorted(cited),'available':sorted(defined),'engineering_defined':sorted(defined),
            'missing':sorted(cited-defined),'unused':sorted(defined-cited),'citation_locations':citations,
            'definition_locations':definitions,'input_files':[{'path':p,'version':by_path[p].get('version',0),'input_kind':by_path[p].get('input_kind','saved')} for p in sorted(visited|bibs) if p in by_path],
            'uncovered_syntax':unsupported,'coverage_complete':not unsupported,
            'summary':f'已引用{len(cited)}项，工程未定义{len(cited-defined)}项，未使用{len(defined-cited)}项；未覆盖/需核验{len(unsupported)}处。'}
