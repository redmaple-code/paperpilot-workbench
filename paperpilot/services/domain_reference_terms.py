"""Build a domain terminology review queue from the shared reference library."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from uuid import uuid4

from paperpilot.services import domain_workspace, manuscript_plan, term_comparison
from paperpilot.services.domain_term_recognition import concept_key, recognize
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.term_recognition import definition, key, names

_RULE_VERSION = 'domain-reference-terms-v1'
_GRADES = {'fulltext': 3, 'excerpt': 2, 'abstract': 1}
_SIGNALS = {'definition': 4, 'scheme': 3, 'expansion': 2, 'concept': 1}


def initialize_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS domain_reference_source_projects (
            domain_id TEXT NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            PRIMARY KEY(domain_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS domain_reference_term_decisions (
            domain_id TEXT NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('published', 'rejected')),
            domain_version INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(domain_id, request_id),
            UNIQUE(domain_id, candidate_id, fingerprint)
        );
        CREATE INDEX IF NOT EXISTS domain_ref_term_versions
        ON domain_reference_term_decisions(domain_id, status, domain_version);
        CREATE TABLE IF NOT EXISTS domain_reference_term_batches (
            domain_id TEXT NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            result TEXT NOT NULL,
            PRIMARY KEY(domain_id, request_id)
        );
    """)


def _version(conn, domain_id):
    entry = conn.execute('SELECT version FROM domains WHERE id=?', (domain_id,)).fetchone()
    if entry is None:
        raise ProjectServiceError(404, 'domain_missing', '领域不存在。')
    return entry['version']


def _references(conn, domain_id):
    """Read the current library edition of each work admitted to the domain."""
    rows = conn.execute("""
        SELECT p.id AS project_id, r.id, r.current_version, l.cite_key
        FROM projects p
        JOIN project_reference_links l ON l.project_id=p.id
        JOIN reference_records r ON r.id=l.reference_id AND r.status='active'
        LEFT JOIN reference_attachments a ON a.reference_id=r.id
            AND a.reference_version=r.current_version AND a.project_id=p.id
        WHERE (p.domain_id=? OR EXISTS (SELECT 1 FROM domain_reference_source_projects s
               WHERE s.domain_id=? AND s.project_id=p.id))
            AND l.link_status IN ('linked','update_available')
        ORDER BY r.id, (a.project_id IS NOT NULL) DESC, p.id
    """, (domain_id, domain_id))
    references = {}
    for row in rows:
        references.setdefault(row['id'], row)
    return references.values()


def set_source_project(conn, domain_id, project_id, enabled):
    _version(conn, domain_id)
    if not conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
        raise ProjectServiceError(404, 'project_missing', '来源论文不存在。')
    if enabled:
        conn.execute('INSERT OR IGNORE INTO domain_reference_source_projects VALUES(?,?)', (domain_id, project_id))
    else:
        conn.execute('DELETE FROM domain_reference_source_projects WHERE domain_id=? AND project_id=?', (domain_id, project_id))
    return {'domain_id': domain_id, 'project_id': project_id, 'enabled': enabled}


def _source_rank(item):
    return (bool(item['evidence']['definition']), _GRADES[item['evidence']['grade']], _SIGNALS[item['signal']])


def _distinct_labels(values):
    first = {}
    for value in values:
        first.setdefault(key(value), value)
    return list(first.values())


def _candidate_groups(conn, domain_id):
    groups = defaultdict(list)
    coverage = Counter()
    for ref in _references(conn, domain_id):
        result = term_comparison.reference_terms(conn, ref['project_id'], ref['id'], ref['current_version'],
                                                 recognizer=recognize)
        coverage[result['status']] += 1
        for item in result['items']:
            source = item['evidence']
            evidence = source | {'project_id': ref['project_id'], 'cite_key': source.get('cite_key') or ref['cite_key']}
            groups[concept_key(item['term'])].append(item | {'evidence': evidence})
    return groups, dict(coverage)


def _make_candidate(term_key, items, current_terms, aliases):
    by_work = {}
    for item in sorted(items, key=_source_rank, reverse=True):
        by_work.setdefault(item['evidence']['source_id'], item)
    sources = [item['evidence'] for item in by_work.values()]
    signals = {item['signal'] for item in items}
    if not (
        'definition' in signals or 'scheme' in signals
        or ('concept' in signals and any(src['grade'] == 'fulltext' for src in sources))
        or len(sources) >= 2
    ):
        return None

    canonical = items[0]['term']
    matches = current_terms.get(term_key, [])
    existing = matches[0] if len(matches) == 1 else None
    observed_aliases = _distinct_labels(
        label for item in items for label in [item['term'], *item['aliases']] if key(label) != key(canonical)
    )
    chosen_name = existing['term'] if existing else canonical
    if key(chosen_name) != key(canonical):
        observed_aliases = _distinct_labels([canonical, *observed_aliases])
    chosen_aliases = [alias for alias in observed_aliases if key(alias) != key(chosen_name)]
    owners = {term_id for label in [chosen_name, *chosen_aliases]
              for term_id in aliases.get(concept_key(label), ())}
    conflict = len(matches) > 1 or bool(owners - ({existing['id']} if existing else set()))
    definitions = _distinct_labels(item['evidence']['definition'] for item in items if item['evidence']['definition'])
    reasons = []
    for signal, reason in (
        ('definition', '文献明确界定了该概念'),
        ('scheme', '原文采用了可区分的算法名称'),
        ('concept', '原文命中已核定的专业概念名称'),
        ('expansion', '原文给出了缩写与专业全称的对应'),
    ):
        if signal in signals:
            reasons.append(reason)
    if len(sources) > 1:
        reasons.append(f'{len(sources)} 篇独立文献用到了该术语')
    return {
        'id': manuscript_plan.key('domain-reference-term', term_key),
        'evidence_fingerprint': manuscript_plan.key(_RULE_VERSION, term_key, observed_aliases, sources, definitions),
        'term': chosen_name,
        'aliases': chosen_aliases,
        'definition': definitions[0] if len(definitions) == 1 else '',
        'definition_conflict': len(definitions) > 1,
        'reference_count': len(sources),
        'sources': sources[:8],
        'reasons': reasons,
        'existing': existing,
        'status': 'conflict' if conflict else 'supplement' if existing else 'new',
    }


def preview(conn, domain_id):
    version = _version(conn, domain_id)
    done = {(row['candidate_id'], row['fingerprint']) for row in conn.execute(
        'SELECT candidate_id,fingerprint FROM domain_reference_term_decisions WHERE domain_id=?', (domain_id,))}
    current_terms, aliases = defaultdict(list), defaultdict(set)
    for row in conn.execute("SELECT * FROM domain_terms WHERE domain_id=? AND version=? AND status='active' ORDER BY id",
                            (domain_id, version)):
        term = dict(row)
        current_terms[concept_key(term['term'])].append(term)
        for label in [term['term'], *names(term['aliases'])]:
            aliases[concept_key(label)].add(term['id'])

    groups, coverage = _candidate_groups(conn, domain_id)
    candidates = []
    for term_key, items in groups.items():
        candidate = _make_candidate(term_key, items, current_terms, aliases)
        if candidate and (candidate['id'], candidate['evidence_fingerprint']) not in done:
            candidates.append(candidate)
    prospective_owners = defaultdict(set)
    for candidate in candidates:
        for label in [candidate['term'], *candidate['aliases']]:
            prospective_owners[concept_key(label)].add(candidate['id'])
    pending = []
    for candidate in candidates:
        if any(len(prospective_owners[concept_key(label)]) > 1 for label in [candidate['term'], *candidate['aliases']]):
            candidate['status'] = 'conflict'
        candidate['fingerprint'] = manuscript_plan.key(candidate['evidence_fingerprint'], candidate['status'], candidate['existing'])
        pending.append(candidate)
    pending.sort(key=lambda item: (item['status'] == 'conflict', -item['reference_count'], key(item['term'])))
    explicit_projects = [row['project_id'] for row in conn.execute(
        'SELECT project_id FROM domain_reference_source_projects WHERE domain_id=? ORDER BY project_id', (domain_id,))]
    return {'domain_id': domain_id, 'version': version, 'coverage': coverage, 'items': pending,
            'source_projects': explicit_projects}


def _publication_values(candidate, supplied_definition=None):
    if candidate['status'] == 'conflict':
        raise ProjectServiceError(409, 'domain_term_conflict', '术语名或别名有冲突，请先处理该条目。')
    existing = candidate['existing']
    meaning = definition(existing['definition']) if existing else ''
    if not meaning:
        meaning = definition(manuscript_plan.redact(supplied_definition or candidate['definition']))
    aliases = _distinct_labels([*names(existing['aliases'] if existing else ''), *candidate['aliases']])
    return (existing or {}) | {'term': candidate['term'], 'definition': meaning, 'aliases': '，'.join(aliases)}


def _require_candidate(items, candidate_id, fingerprint):
    candidate = items.get(candidate_id)
    if candidate is None or candidate['fingerprint'] != fingerprint:
        raise ProjectServiceError(409, 'domain_reference_term_stale', '文献依据或候选已变化，请刷新后再入库。')
    return candidate


def _publish(conn, domain_id, version, receipt_id, candidates, terms):
    replaced = [item['existing']['id'] for item in candidates if item['existing']]
    version = domain_workspace.publish_term_version(
        conn, domain_id, version, 'reference-term:' + receipt_id, terms, replaced,
    )
    for candidate, term in zip(candidates, terms, strict=True):
        if candidate['existing'] is None:
            domain_workspace.add_domain_resource(conn, domain_id, 'term', term['term'], term['definition'], add_term=False)
    return version


def _record(conn, domain_id, request_id, candidate, signature, snapshot):
    conn.execute("""INSERT INTO domain_reference_term_decisions
        (domain_id,request_id,candidate_id,fingerprint,payload_hash,status,domain_version,snapshot,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (domain_id, request_id, candidate['id'], candidate['evidence_fingerprint'], signature, snapshot['status'],
         snapshot['domain_version'], json.dumps(snapshot, ensure_ascii=False), domain_workspace._now()))


def decide(conn, domain_id, candidate_id, payload):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    signature = manuscript_plan.key(candidate_id, payload)
    replay = conn.execute(
        'SELECT payload_hash,snapshot FROM domain_reference_term_decisions WHERE domain_id=? AND request_id=?',
        (domain_id, payload['request_id'])).fetchone()
    if replay:
        if replay['payload_hash'] != signature:
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于另一次领域术语复核。')
        return json.loads(replay['snapshot'])
    if _version(conn, domain_id) != payload['expected_version']:
        raise ProjectServiceError(409, 'domain_version_conflict', '领域版本已更新，请刷新候选后再复核。')
    view = preview(conn, domain_id)
    candidate = _require_candidate({item['id']: item for item in view['items']}, candidate_id, payload['fingerprint'])
    version = view['version']
    snapshot = {'status': 'rejected', 'domain_version': version, 'candidate': candidate}
    if payload['decision'] == 'publish':
        term = _publication_values(candidate, payload.get('definition'))
        version = _publish(conn, domain_id, version, payload['request_id'], [candidate], [term])
        snapshot.update(status='published', domain_version=version, term=term['term'], definition=term['definition'])
    _record(conn, domain_id, payload['request_id'], candidate, signature, snapshot)
    return snapshot


def publish_batch(conn, domain_id, payload):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    request_id = payload['request_id']
    selected = sorted(payload['candidates'].items())
    signature = manuscript_plan.key(payload['expected_version'], selected)
    replay = conn.execute(
        'SELECT payload_hash,result FROM domain_reference_term_batches WHERE domain_id=? AND request_id=?',
        (domain_id, request_id)).fetchone()
    if replay:
        if replay['payload_hash'] != signature:
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于另一批术语入库。')
        return json.loads(replay['result'])
    if _version(conn, domain_id) != payload['expected_version']:
        raise ProjectServiceError(409, 'domain_version_conflict', '领域版本已更新，请刷新候选后再入库。')

    view = preview(conn, domain_id)
    items = {candidate['id']: candidate for candidate in view['items']}
    candidates = [_require_candidate(items, candidate_id, fingerprint) for candidate_id, fingerprint in selected]
    terms = [_publication_values(candidate) for candidate in candidates]
    version = _publish(conn, domain_id, view['version'], 'batch:' + request_id, candidates, terms)
    accepted = []
    for candidate, term in zip(candidates, terms, strict=True):
        summary = {'candidate_id': candidate['id'], 'term': term['term'], 'definition': term['definition']}
        accepted.append(summary)
        snapshot = summary | {'status': 'published', 'domain_version': version, 'candidate': candidate,
                              'batch_request_id': request_id}
        _record(conn, domain_id, uuid4().hex, candidate, signature, snapshot)
    result = {'status': 'published', 'domain_version': version, 'items': accepted}
    conn.execute('INSERT INTO domain_reference_term_batches VALUES(?,?,?,?)',
                 (domain_id, request_id, signature, json.dumps(result, ensure_ascii=False)))
    return result


def with_provenance(conn, domain_id, terms):
    if not terms:
        return terms
    version = terms[0]['version']
    evidence = defaultdict(dict)
    for row in conn.execute("""SELECT snapshot,domain_version FROM domain_reference_term_decisions
        WHERE domain_id=? AND status='published' AND domain_version<=? ORDER BY domain_version DESC""",
                            (domain_id, version)):
        decision = json.loads(row['snapshot'])
        entry_key = (key(decision['term']), decision['definition'])
        for source in decision['candidate']['sources']:
            identity = (source['source_id'], source['version'], source.get('location'), source['quote'])
            evidence[entry_key].setdefault(identity, source | {'published_in_version': row['domain_version']})
    for term in terms:
        term_key = key(term['term'])
        sources = evidence.get((term_key, term['definition']), {}) | evidence.get((term_key, ''), {})
        if sources:
            term['reference_sources'] = list(sources.values())[:12]
    return terms
