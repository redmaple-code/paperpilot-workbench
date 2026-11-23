from uuid import uuid4

import app


def domain(client, name):
    result = client.post('/api/domains', json={'name': name})
    assert result.status_code == 200
    return result.json()['id']


def paper(client, name, domain_id):
    result = client.post('/api/projects', json={'name': name, 'domain_id': domain_id})
    assert result.status_code == 200
    return result.json()['project']['id']


def reference(client, title, abstract=''):
    result = client.post('/api/references', json={'title': title, 'abstract': abstract})
    assert result.status_code == 200
    return result.json()['id']


def link(client, project, ref, cite_key):
    result = client.post(f'/api/projects/{project}/references/{ref}', json={
        'request_id': uuid4().hex, 'reference_version': 1, 'cite_key': cite_key,
    })
    assert result.status_code == 200


def proposals(client, domain_id):
    result = client.get(f'/api/domains/{domain_id}/reference-terms')
    assert result.status_code == 200
    return result.json()


def decide(client, domain_id, item, version, decision='publish', **values):
    return client.post(f'/api/domains/{domain_id}/reference-terms/{item["id"]}/decision', json={
        'request_id': uuid4().hex, 'expected_version': version, 'fingerprint': item['fingerprint'],
        'decision': decision,
    } | values)


def test_reference_queue_deduplicates_and_filters_noise(client):
    fluid = domain(client, '数值流体')
    other = domain(client, '其他领域')
    a = paper(client, '论文甲', fluid)
    b = paper(client, '论文乙', fluid)
    c = paper(client, '无关论文', other)
    ref = reference(client, 'A reconstruction study',
        'WENO-JS reconstruction and WENO-Z+ schemes were tested. The finite volume method uses smoothness indicators. '
        'CPU CPU CPU results are in Table 2. Model Context Protocol (MCP) was used for tools.')
    for project in (a, b):
        link(client, project, ref, 'ref-one')
    bib = reference(client, 'WENO-F3+ scheme')
    link(client, a, bib, 'metadata')
    outside = reference(client, 'Unrelated topic', 'WENO-Q99 scheme is used for another study.')
    link(client, c, outside, 'unrelated')

    view = proposals(client, fluid)
    assert view['coverage'] == {'abstract': 1, 'metadata': 1}
    assert {item['term'] for item in view['items']} == {'WENO-JS', 'WENO-Z+'}
    second = reference(client, 'An independent study', '使用光滑度指标的有限体积法。\nThe finite volume method uses smoothness indicators.')
    link(client, b, second, 'ref-two')
    known = {item['term']: item for item in proposals(client, fluid)['items']}
    assert set(known) == {'WENO-JS', 'WENO-Z+', '有限体积法', '光滑度指示子'}
    smoothness = known['光滑度指示子']
    assert smoothness['reference_count'] == 2
    assert set(smoothness['aliases']) == {'smoothness indicators', '光滑度指标'}
    assert {(src['source_id'], src['version'], src['location']) for src in smoothness['sources']} == {
        (ref, 1, '摘要'), (second, 1, '摘要'),
    }
    assert client.get(f'/api/domains/{fluid}/terms').json() == []


def test_latest_library_edition_publication_and_provenance(client):
    fluid = domain(client, '高阶格式')
    p = paper(client, '固定引文版本', fluid)
    ref = reference(client, 'Scheme notes', 'WENO-Z+ scheme is used for a conservative reconstruction.')
    link(client, p, ref, 'scheme')
    first = proposals(client, fluid)['items'][0]
    revision = client.put('/api/references/' + ref, json={
        'base_version': 1, 'changes': {'abstract': 'WENO-Z+ scheme uses revised weights at interfaces.'},
    })
    assert revision.status_code == 200
    assert decide(client, fluid, first, 1, definition='人工核对定义。').json()['detail']['code'] == 'domain_reference_term_stale'
    current = proposals(client, fluid)['items'][0]
    assert current['sources'][0]['version'] == 2
    request_id = 'confirmed-ref-term'
    decision = decide(client, fluid, current, 1, request_id=request_id)
    assert decision.status_code == 200
    assert decision.json()['domain_version'] == 2
    retry = decide(client, fluid, current, 1, request_id=request_id)
    assert retry.json() == decision.json()
    assert proposals(client, fluid)['items'] == []
    assert client.get(f'/api/domains/{fluid}/terms?version=1').json() == []
    now = client.get(f'/api/domains/{fluid}/terms').json()
    assert now[0]['term'] == 'WENO-Z+'
    assert now[0]['definition'] == ''
    assert now[0]['reference_sources'][0]['version'] == 2
    assert now[0]['reference_sources'][0]['published_in_version'] == 2
    assert client.get('/api/projects/' + p).json()['terms'] == []
    resource = client.get(f'/api/domains/{fluid}/resources').json()[0]
    amendment = client.put(f'/api/domains/{fluid}/resources/{resource["id"]}', json={
        'request_id': 'later-definition', 'expected_version': resource['version'],
        'title': 'WENO-Z+', 'content': '采用修正权重的 WENO 重构。',
    })
    assert amendment.status_code == 200
    amended = client.get(f'/api/domains/{fluid}/terms').json()
    assert amended[0]['definition'] == '采用修正权重的 WENO 重构。'
    assert amended[0]['reference_sources'][0]['version'] == 2


def test_existing_definition_unchanged_and_rejection_is_evidence_scoped(client):
    fluid = domain(client, '术语维护')
    p = paper(client, '维护术语', fluid)
    existing = client.post(f'/api/domains/{fluid}/resources', json={
        'resource_type': 'term', 'title': 'WENO-Z+', 'content': '经维护者核定的定义。',
    })
    assert existing.status_code == 200
    ref = reference(client, 'Variants', 'WENO-Z+ scheme is evaluated against the WENO-JS scheme.')
    link(client, p, ref, 'variants')
    view = {item['term']: item for item in proposals(client, fluid)['items']}
    assert view['WENO-Z+']['status'] == 'supplement'
    assert decide(client, fluid, view['WENO-JS'], 1, decision='reject').status_code == 200
    assert [item['term'] for item in proposals(client, fluid)['items']] == ['WENO-Z+']
    assert decide(client, fluid, view['WENO-Z+'], 1, definition='不经复核的替换。').status_code == 200
    terms = client.get(f'/api/domains/{fluid}/terms').json()
    assert terms[0]['definition'] == '经维护者核定的定义。'
    assert terms[0]['reference_sources'][0]['source_id'] == ref
    amendment = client.put('/api/references/' + ref, json={
        'base_version': 1, 'changes': {'abstract': 'WENO-JS scheme is evaluated using a new benchmark.'},
    })
    assert amendment.status_code == 200
    assert [item['term'] for item in proposals(client, fluid)['items']] == ['WENO-JS']


def test_readable_attachment_uses_owning_project_scope_and_omits_bibliography(client):
    fluid = domain(client, '全文来源')
    other = domain(client, '引用同一文献的其他领域')
    p = paper(client, '拥有原文的论文', fluid)
    q = paper(client, '只有书目的论文', other)
    ref = reference(client, 'Study of numerical schemes')
    link(client, p, ref, 'document')
    link(client, q, ref, 'cited')
    import_id = 'domain-review-pdf'
    evidence_id = 'domain-review-evidence'
    with app.db() as conn:
        conn.execute("INSERT INTO oa_imports(id,project_id,idempotency_key,access_status,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (import_id, p, import_id, 'open', 'imported', '2026-11-23T00:00:00Z', '2026-11-23T00:00:00Z'))
        conn.execute("""INSERT INTO external_evidence
            (id,project_id,import_id,fragments_json,confirmed_at,source_version) VALUES(?,?,?,?,?,?)""",
                     (evidence_id, p, import_id,
                      '[{"location":"page:2","text":"The numerical flux is defined as a common interface flux.\\nReferences\\n[1] WENO-Q99 scheme."}]',
                      '2026-11-23T00:00:00Z', 'oa-pdf-index-v2'))
        conn.execute("""INSERT INTO reference_attachments
            (reference_id,reference_version,project_id,external_evidence_id,created_at) VALUES(?,?,?,?,?)""",
                     (ref, 1, p, evidence_id, '2026-11-23T00:00:00Z'))
    view = proposals(client, fluid)
    assert set(item['term'] for item in view['items']) == {'数值通量'}
    assert view['items'][0]['definition'] == 'a common interface flux.'
    assert view['items'][0]['sources'][0]['location'] == 'page:2'
    accepted = decide(client, fluid, view['items'][0], 1)
    assert accepted.status_code == 200
    assert accepted.json()['definition'] == 'a common interface flux.'
    assert proposals(client, other)['items'] == []


def test_source_collection_keeps_paper_binding_and_has_reversible_scope(client):
    fluid = domain(client, '文献专题')
    p = paper(client, '尚未绑定的论文', '')
    ref = reference(client, 'Explicit collection', 'WENO-Z+ scheme is evaluated.')
    link(client, p, ref, 'explicit')
    source = f'/api/domains/{fluid}/reference-source-projects/{p}'
    assert proposals(client, fluid)['coverage'] == {}
    assert client.put(source, json={'enabled': True}).status_code == 200
    assert client.put(source, json={'enabled': True}).status_code == 200
    view = proposals(client, fluid)
    assert view['source_projects'] == [p]
    assert [item['term'] for item in view['items']] == ['WENO-Z+']
    assert client.get('/api/projects/' + p).json()['project']['domain_id'] == ''
    assert client.put(source, json={'enabled': False}).status_code == 200
    assert decide(client, fluid, view['items'][0], 1, definition='定义。').status_code == 409


def test_same_short_name_is_conflicted_until_competing_candidate_is_resolved(client):
    fluid = domain(client, '缩写歧义')
    p = paper(client, '缩写测试', fluid)
    for index in range(2):
        ref = reference(client, f'Operators {index}',
                        'Flux Reconstruction Operator (FRO) and Fracture Resistance Operator (FRO) were compared.')
        link(client, p, ref, f'operators-{index}')
    found = {item['term']: item for item in proposals(client, fluid)['items']}
    assert len(found) == 2 and {item['status'] for item in found.values()} == {'conflict'}
    assert decide(client, fluid, found['Flux Reconstruction Operator'], 1, definition='人工核对。').status_code == 409
    blocked = client.post(f'/api/domains/{fluid}/reference-terms/batch', json={
        'request_id': 'conflicting-batch', 'expected_version': 1,
        'candidates': {item['id']: item['fingerprint'] for item in found.values()},
    })
    assert blocked.json()['detail']['code'] == 'domain_term_conflict'
    assert client.get(f'/api/domains/{fluid}/terms').json() == []
    assert decide(client, fluid, found['Fracture Resistance Operator'], 1, decision='reject').status_code == 200
    assert proposals(client, fluid)['items'][0]['status'] == 'new'


def test_batch_is_atomic_and_preserves_evidence_in_one_version(client):
    fluid = domain(client, '批量领域')
    p = paper(client, '批量测试论文', fluid)
    ref = reference(client, 'Scheme selection', 'WENO-JS, WENO-Z+ and WENO-Z schemes were evaluated.')
    link(client, p, ref, 'batch-study')
    old = client.post(f'/api/domains/{fluid}/resources', json={
        'resource_type': 'term', 'title': 'WENO-JS', 'content': '已有定义。',
    })
    assert old.status_code == 200
    view = {item['term']: item for item in proposals(client, fluid)['items']}
    selected = {view[name]['id']: view[name]['fingerprint'] for name in ('WENO-JS', 'WENO-Z+')}
    path = f'/api/domains/{fluid}/reference-terms/batch'
    payload = {'request_id': 'publish-two', 'expected_version': 1, 'candidates': selected}
    stale = client.post(path, json=payload | {'candidates': selected | {view['WENO-Z+']['id']: '0' * 24}})
    assert stale.json()['detail']['code'] == 'domain_reference_term_stale'
    assert [item['term'] for item in client.get(f'/api/domains/{fluid}/terms').json()] == ['WENO-JS']
    result = client.post(path, json=payload)
    assert result.status_code == 200
    assert result.json()['domain_version'] == 2
    assert client.post(path, json=payload).json() == result.json()
    terms = {term['term']: term for term in client.get(f'/api/domains/{fluid}/terms').json()}
    assert set(terms) == {'WENO-JS', 'WENO-Z+'}
    assert terms['WENO-JS']['definition'] == '已有定义。'
    assert terms['WENO-Z+']['definition'] == ''
    assert {term['reference_sources'][0]['published_in_version'] for term in terms.values()} == {2}
    assert [item['term'] for item in proposals(client, fluid)['items']] == ['WENO-Z']
