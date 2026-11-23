"""Conservative, source-anchored terminology recognition for local documents."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from paperpilot.services.manuscript_plan import redact


_DASHES = str.maketrans('‐‑‒–—−', '------')
_ACRONYM = r'(?:[A-Z][A-Z0-9]{1,14}(?:[‐‑‒–—−-][A-Za-z0-9]+)*\+*)'
_SHORT = re.compile(r'(?<![\w-])' + _ACRONYM + r'(?![A-Za-z0-9+])', re.ASCII)
_EXPANSION = re.compile(r'([A-Za-z][A-Za-z-]*(?:[ \t]+[A-Za-z][A-Za-z-]*){1,11})\s*[（(](' + _ACRONYM + r')[）)]')
_CHINESE_EXPANSION = re.compile(r'([\u3400-\u9fff]{3,18})\s*[（(](' + _ACRONYM + r')[）)]')
_KEYWORDS = re.compile(r'(?im)^\s*(?:关键词|key\s*words?)\s*[:：]\s*(.+)$')
_DEFINITION = re.compile(r'(?im)^[ \t]*(?:[-*][ \t]+)?([^\n:：。]{2,75}?)\s*([:：]|\s+is defined as\s+|\s+refers to\s+|定义为|指的是)\s*(\S[^\n]{7,380})')
_SYMBOL = re.compile(r'(\$[^$\n]{1,34}\$)\s*(?:表示|定义为|denotes\b|stands for\b)\s*([^。.;\n]{4,140})', re.I)
_NAMED = re.compile(r'(?<![A-Za-z])(?:[A-Z][a-z]{1,30}(?:\s*[-‐‑‒–—−]{1,2}\s*[A-Z][a-z]{2,30})+|(?:Euler|Riemann|Sod|Lax|Einfeldt)\s*(?:方程|问题|算例|equations?|problems?))(?![A-Za-z])')
_NUMERIC_CN = re.compile(r'[一二三四五六七八九十0-9]+阶\s*临界点|[一二三四五六七八九十0-9]+阶\s*(?:精度|收敛)')
_TECHNICAL_CN = re.compile(r'跳跃比传感器|曲率传感器|光滑度(?:指标|量|指示子)|(?:全局|非线性|线性|变分)权重|(?:有限体积|有限差分)(?:法|格式|重构)?|(?:接触|切向)间断|(?:总变差|局部模板|子模板|临界点|特征分解|数值通量|黎曼问题|重构误差|收敛阶)|(?:熵波|线性平流)(?:问题|方程|算例)?|(?:仿射不变性|单元平均|截断误差|正性保持)|(?:Gibbs|吉布斯)\s*后验|KL\s*散度')
_ENGLISH = re.compile(r'\b(?:weighted essentially non[- ]oscillatory(?:\s+(?:reconstruction|scheme))?|smoothness (?:indicators?|measures?)|(?:nonlinear|linear|variational) weights?|finite (?:volume|difference)(?: method| reconstruction)?|(?:first|second|third)[- ]order critical points?|Gibbs posterior|numerical flux|contact discontinuity|total variation|Riemann (?:solver|problem)|error norms?)\b', re.I)
_PLACEHOLDERS = {'', '原文关键词，定义待校订。', '定义待补', '待补', '待定'}
_NON_TERMS = {'abstract', 'keywords', 'introduction', 'conclusion', 'methods', 'results', '摘要', '关键词', '引言', '结论', '讨论', '正文', 'title', 'doi'}
_NUMBERED_LABEL = re.compile(r'[A-Z]\d+')


def key(text):
    value = unicodedata.normalize('NFKC', text).translate(_DASHES).casefold().strip()
    value = re.sub(r'\s*-+\s*', '-', value)
    return re.sub(r'\s+', ' ', value)


def names(value):
    return [item.strip() for item in re.split(r'[,，;；\n、]', value or '') if item.strip()]


def definition(value):
    value = (value or '').strip()
    return '' if value in _PLACEHOLDERS else value


def _named_term(value):
    value = re.sub(r'\s*[-‐‑‒–—−]{1,2}\s*', '–', value)
    return re.sub(r'(?<=[a-z])\s+(?=[\u3400-\u9fff])', '', value)


def _technical_label(value):
    return any(pattern.fullmatch(value) for pattern in (_NAMED, _NUMERIC_CN, _TECHNICAL_CN, _ENGLISH, _SHORT))


def _expansion(match):
    words = list(re.finditer(r'[A-Za-z]+', match[1]))
    initials = re.sub(r'[^A-Z0-9]', '', match[2])
    for word in words:
        phrase = match[1][word.start():]
        abbreviation = ''.join(token[0].upper() for token in re.findall(r'[A-Za-z]+', phrase))
        if abbreviation == initials:
            return phrase
    return ''


def _quote(text, start, end):
    left = max(text.rfind('\n', 0, start), text.rfind('。', 0, start)) + 1
    stop = [pos for sep in ('\n', '。') if (pos := text.find(sep, end)) >= 0]
    right = min(stop) + 1 if stop else len(text)
    return text[max(left, start - 100):max(end, min(right, start + 320))].strip()


def recognize(fragments):
    found = defaultdict(list)
    short_mentions = defaultdict(list)

    def emit(term, fragment, text, start, end, aliases=(), meaning=''):
        term = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', term)
        term = re.sub(r'\s+', ' ', term).strip(' .。；;')
        normalized = key(term)
        if not 2 <= len(term) <= 120 or normalized in _NON_TERMS or not any(char.isalpha() for char in term):
            return
        evidence = {name: value for name, value in fragment.items() if name != 'text'}
        evidence['quote'] = _quote(text, start, end)
        evidence['definition'] = meaning if evidence.get('grade') != 'metadata' else ''
        evidence['label'] = term
        result = {'term': term, 'aliases': list(aliases), 'evidence': evidence}
        identity = (evidence.get('source_id'), evidence.get('location'), evidence.get('quote'))
        if (len(found[normalized]) < 32 or (meaning and not any(item['evidence']['definition'] == meaning for item in found[normalized]))) and not any(
            (item['evidence'].get('source_id'), item['evidence'].get('location'), item['evidence'].get('quote')) == identity
            and item['evidence']['definition'] == evidence['definition'] and item['aliases'] == result['aliases']
            for item in found[normalized]
        ):
            found[normalized].append(result)

    for fragment in fragments:
        text = redact(fragment['text'])
        for hit in _KEYWORDS.finditer(text):
            for term in names(hit[1]):
                emit(term, fragment, text, *hit.span())
        for hit in _EXPANSION.finditer(text):
            phrase = _expansion(hit)
            if phrase:
                emit(hit[2], fragment, text, *hit.span(), aliases=[phrase])
        for hit in _CHINESE_EXPANSION.finditer(text):
            phrase = re.split(r'提出了?|称为|采用|使用|本文|定义为|即为|称作|是一种|一种|引入|基于', hit[1])[-1].removeprefix('的')
            if (not _NUMBERED_LABEL.fullmatch(hit[2]) and 3 <= len(phrase) <= 16
                    and not re.search(r'格式的|问题的|的是|可得|用于|利用|或|和|与|见|检验|满足|根据', phrase)):
                emit(hit[2], fragment, text, *hit.span(), aliases=[phrase])
        for hit in _DEFINITION.finditer(text):
            term = hit[1].strip().strip('*`')
            glossary = re.search(r'glossary|terminology|术语|符号', fragment.get('heading',''), re.I)
            explicit = hit[2].strip() not in {':','：'}
            plausible = not re.search(r'[{}\\=，,。]|\s{2,}|^\d|^(?:图|表|第)\s*\d', term) and len(term.split()) <= 8
            if plausible and (explicit or glossary or _technical_label(term)):
                emit(term, fragment, text, *hit.span(), meaning=hit[3].strip())
        for hit in _SYMBOL.finditer(text):
            emit(hit[1], fragment, text, *hit.span(), meaning=hit[2].strip())
        for pattern in (_NAMED, _NUMERIC_CN, _TECHNICAL_CN, _ENGLISH):
            for hit in pattern.finditer(text):
                # A matched order-qualified point is a separate, explicit term.
                if hit[0] == '临界点' and re.search(r'阶\s*$', text[max(0, hit.start()-5):hit.start()]):
                    continue
                emit(_named_term(hit[0]) if pattern is _NAMED else hit[0], fragment, text, *hit.span())
        for hit in _SHORT.finditer(text):
            label = hit[0].translate(_DASHES)
            if '-' in label or '+' in label:
                emit(label, fragment, text, *hit.span())
            elif label not in {'PDF', 'DOI', 'HTTP', 'HTTPS', 'URL', 'ISBN', 'ISSN', 'FIG', 'TABLE'} and not _NUMBERED_LABEL.fullmatch(label):
                short_mentions[label].append((fragment, text, hit.span()))
                category=re.match(r'\s*(?:格式|方法|方程|scheme\b|method\b)',text[hit.end():],re.I)
                if category:
                    phrase=text[hit.start():hit.end()+category.end()].strip()
                    emit(label, fragment, text, hit.start(), hit.end()+category.end(), aliases=[phrase])
    for name, hits in short_mentions.items():
        if len(hits) > 1 or key(name) in found:
            for fragment, text, span in hits[:12]:
                emit(name, fragment, text, *span)
    return [item for group in found.values() for item in group]
