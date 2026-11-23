"""Extract technical concepts and preserve the exact reference text that supports them."""
from __future__ import annotations

from functools import lru_cache
import re

from paperpilot.services.manuscript_plan import redact
from paperpilot.services.term_recognition import _DEFINITION, _EXPANSION, _expansion, _quote, key

READABLE = {'fulltext', 'excerpt', 'abstract'}

# Each mapping describes one concept. Order, variant suffixes, and qualified concepts stay distinct.
_DASH = r'\s*[-‐‑‒–—−]\s*'
_CONCEPTS = tuple((label, re.compile(r'(?<![A-Za-z])(?:' + pattern + r')(?![A-Za-z])', re.I)) for label, pattern in (
    ('WENO 格式', r'weighted\s+essentially\s+non[-‐‑\s]oscillatory(?:\s+(?:scheme|reconstruction))?|WENO\s+(?:schemes?|reconstruction)|WENO\s*格式'),
    ('ENO 格式', r'(?<!weighted\s)essentially\s+non[-‐‑\s]oscillatory\s+(?:schemes?|reconstruction)|ENO\s+(?:schemes?|reconstruction)|ENO\s*格式'),
    ('光滑度指示子', r'(?<!global\s)(?<!local\s)smoothness\s+indicators?|(?<!全局)(?<!局部)光滑度(?:指示子|指标)'),
    ('全局光滑度指示子', r'global\s+smoothness\s+indicators?|全局光滑度(?:指示子|指标)'),
    ('非线性权重', r'non[-‐‑\s]?linear\s+weights?|非线性权重'),
    ('线性权重', r'(?<!non[-\s])(?<!non)linear\s+weights?|(?<!非)线性权重'),
    ('变分权重', r'variational\s+weights?|变分权重'),
    ('有限体积法', r'finite[-‐‑\s]+volume(?:\s+(?:methods?|schemes?))?|有限体积(?:法|格式)?'),
    ('有限差分法', r'finite[-‐‑\s]+difference(?:\s+(?:methods?|schemes?))?|有限差分(?:法|格式)?'),
    ('数值通量', r'numerical\s+flux(?:es)?|数值通量'),
    ('黎曼问题', r'Riemann\s+problems?|黎曼问题'),
    ('黎曼求解器', r'Riemann\s+solvers?|黎曼求解器'),
    ('双曲守恒律', r'hyperbolic\s+conservation\s+laws?|双曲守恒律'),
    ('接触间断', r'contact\s+discontinuit(?:y|ies)|接触间断'),
    ('总变差', r'(?<!strong\s)(?<!total\s)total\s+variation(?!\s+diminishing)|总变差(?!减小)'),
    ('总变差递减', r'total\s+variation\s+diminishing|总变差(?:递减|减小)'),
    ('一阶临界点', r'first\s*[-‐‑\s]\s*order\s+critical\s+points?|一阶\s*临界点'),
    ('二阶临界点', r'second\s*[-‐‑\s]\s*order\s+critical\s+points?|二阶\s*临界点'),
    ('三阶临界点', r'third\s*[-‐‑\s]\s*order\s+critical\s+points?|三阶\s*临界点'),
    ('KL 散度', r'Kullback\s*[-‐‑–—]\s*Leibler\s+divergence|KL\s+divergence|KL\s*散度'),
    ('Gibbs 后验', r'Gibbs\s+posterior|(?:Gibbs|吉布斯)\s*后验'),
    ('大语言模型', r'large\s+language\s+models?|大语言模型'),
    ('Runge–Kutta 方法', r'Runge\s*[-‐‑–—]\s*Kutta\s+(?:methods?|schemes?|time[\s-]+stepping)|Runge\s*[-‐‑–—]\s*Kutta\s*方法'),
    ('熵稳定性', r'entropy\s+stability|熵稳定性'),
    ('保正性', r'positivity\s*[-‐‑\s]\s*preserving|正性保持|保正性'),
))
_CONCEPT_NAMES = {label for label, _ in _CONCEPTS}
_SCHEME = re.compile(
    r'(?<![\w-])(?P<name>(?:WENO\d?|ENO)' + _DASH + r'[A-Z0-9]{1,12}(?:\s*\+{1,2})?)(?![+\w-])'
)
_SCHEME_CONTEXT = re.compile(r'^\s*(?:scheme|method|reconstruction|algorithm|格式|方法|重构)', re.I)
_BIBLIOGRAPHY = re.compile(r'(?:^|\n|(?<=[.!?。])\s+)\s*(?:References|Bibliography|参考文献)'
                           r'\s*(?=\n|$|\[\d+\])', re.I)
_CATEGORY = re.compile(r'\b(?:algorithm|divergence|equation|filter|indicator|limiter|measure|method|model|operator|'
                       r'problem|reconstruction|scheme|sensor|solver|transform|weight)s?$|(?:方法|方程|算子|格式|模型|指标|散度)$', re.I)
_STOP = {'abstract', 'method', 'methods', 'results', 'conclusion', 'figure', 'table', 'appendix', 'authors',
         'we', 'it', 'this', 'that', 'where', 'here', 'the', 'a', 'an', 'our', 'their', '摘要', '正文', '引言', '结论', '关键字',
         '本文', '这里', '作者'}


def _concept(text):
    normalized = key(text)
    if normalized in {'weno', 'weno 格式', 'weno格式'}:
        return 'WENO 格式'
    if normalized in {'eno', 'eno 格式', 'eno格式'}:
        return 'ENO 格式'
    for canonical, rule in _CONCEPTS:
        if key(canonical) == normalized or rule.fullmatch(text):
            return canonical
    return text


def concept_key(text):
    """Unify vetted bilingual names without merging different algorithm variants."""
    return key(re.sub(r'\s+\+', '+', _concept(text)))


def recognize(fragments):
    # Key by actual text and edition, so bibliography and attachment updates invalidate the index.
    return _recognize(tuple(tuple(sorted(fragment.items())) for fragment in fragments if fragment.get('grade') in READABLE))


@lru_cache(maxsize=128)
def _recognize(fragments):
    items = []
    bibliography_seen = False

    def emit(term, actual, fragment, text, start, end, signal, definition='', aliases=()):
        observed = ' '.join(actual.split())
        canonical = _concept(' '.join(term.split()))
        evidence = {name: value for name, value in fragment.items() if name != 'text'}
        evidence.update(quote=_quote(text, start, end), label=observed, definition=definition)
        names = [name for name in (observed, *aliases) if key(name) != key(canonical)]
        items.append({'term': canonical, 'aliases': names, 'evidence': evidence, 'signal': signal})

    for packed in fragments:
        fragment = dict(packed)
        text = redact(fragment['text'])
        if fragment['grade'] in {'fulltext', 'excerpt'}:
            if bibliography_seen:
                continue
            marker = _BIBLIOGRAPHY.search(text)
            if marker:
                bibliography_seen = True
                text = text[:marker.start()]

        for term, rule in _CONCEPTS:
            for hit in rule.finditer(text):
                emit(term, hit[0], fragment, text, *hit.span(), 'concept')
        for hit in _SCHEME.finditer(text):
            label = concept_key(hit['name']).upper()
            if _SCHEME_CONTEXT.match(text[hit.end():]) or label in {'WENO-JS', 'WENO-Z', 'WENO-Z+'}:
                emit(label, hit['name'], fragment, text, *hit.span(), 'scheme')
        for hit in _EXPANSION.finditer(text):
            full = _expansion(hit)
            if full and _CATEGORY.search(full):
                known = _concept(full)
                emit(known if known != full else full, full, fragment, text, *hit.span(), 'expansion', aliases=(hit[2],))
        for hit in _DEFINITION.finditer(text):
            label = re.sub(r'^the\s+', '', hit[1].strip().strip('*'), flags=re.I)
            if hit[2].strip() in {':', '：'} or not (_concept(label) in _CONCEPT_NAMES or _CATEGORY.search(label)):
                continue
            if not re.fullmatch(r'[\w+() \-\u2010-\u2015]{3,80}', label) or label.split()[0].casefold() in _STOP:
                continue
            emit(label, label, fragment, text, *hit.span(), 'definition', hit[3].strip())
    return items
