from __future__ import annotations

import json
from io import BytesIO

from reportlab.pdfgen import canvas

import app


def _create(client):
    response = client.post('/api/projects', json={'name': 'OA 导入测试', 'domain': '材料科学'})
    assert response.status_code == 200
    return response.json()['project']['id']


def _candidate(pid: str, *, access_status: str, oa_url: str = '') -> str:
    candidate_id = 'candidate-' + access_status
    conn = app.db()
    conn.execute(
        "INSERT INTO academic_searches(id,project_id,query,filters_json,provider,rule_version,request_id,generation,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ('search-' + access_status, pid, 'query', '{}', 'fake', 'v1', 'request-' + access_status, 1, 'succeeded', app.now(), app.now()),
    )
    conn.execute(
        "INSERT INTO academic_candidates(id,search_id,project_id,title,authors,year,venue,doi,canonical_url,abstract,provider,access_status,oa_url,match_reasons_json,sources_json,dedupe_key,rank_score,status,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (candidate_id, 'search-' + access_status, pid, 'OA Paper' if access_status == 'open_access' else 'Paywalled Paper', 'Author', '2024', 'Journal', '10.1000/test-' + access_status, 'https://example.org/paper', 'Abstract', 'fake', access_status, oa_url, '[]', '[]', candidate_id, 1.0, 'candidate', '{}', app.now()),
    )
    conn.commit(); conn.close()
    return candidate_id


def test_oa_fetch_preview_confirm_and_idempotency(client, monkeypatch):
    pid = _create(client)
    candidate_id = _candidate(pid, access_status='open_access', oa_url='https://example.org/paper.pdf')
    monkeypatch.setattr(app.oa_imports, 'fetch_public', lambda url: (b'%PDF-fake', 'application/pdf', 'sha-test'))
    monkeypatch.setattr(app.oa_imports, 'extract_preview', lambda content, content_type, metadata: {
        'content_kind': 'pdf', 'metadata': metadata, 'abstract': 'OA abstract',
        'fragments': [{'location': 'page:1', 'text': 'OA evidence fragment'}],
        'text_preview': 'OA evidence fragment', 'extraction_quality': 'ok',
        'parser_version': 'test', 'warnings': [],
    })
    response = client.post(f'/api/projects/{pid}/imports', json={'candidate_id': candidate_id})
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'preview_ready'
    replay = client.post(f'/api/projects/{pid}/imports', json={'candidate_id': candidate_id}).json()
    assert replay['idempotent_replay'] is True
    confirmed = client.post(f"/api/projects/{pid}/imports/{body['import']['id']}/confirm", json={}).json()
    assert confirmed['status'] == 'imported'
    replay_confirm = client.post(f"/api/projects/{pid}/imports/{body['import']['id']}/confirm", json={}).json()
    assert replay_confirm['idempotent_replay'] is True
    project = client.get(f'/api/projects/{pid}').json()
    assert project['corpus'][0]['content'] == 'OA evidence fragment'


def test_non_oa_is_metadata_only_and_never_fetches(client, monkeypatch):
    pid = _create(client)
    candidate_id = _candidate(pid, access_status='metadata_only')

    def fail_fetch(_url):
        raise AssertionError('non-OA candidates must not trigger full-text fetch')

    monkeypatch.setattr(app.oa_imports, 'fetch_public', fail_fetch)
    response = client.post(f'/api/projects/{pid}/imports', json={'candidate_id': candidate_id})
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'metadata_only'
    assert '不抓取正文' in body['next_action']
    assert client.post(f'/api/projects/{pid}/imports/{body["import"]["id"]}/confirm', json={}).status_code == 409


def test_direct_public_url_uses_oa_fetch_path(client, monkeypatch):
    pid = _create(client)
    monkeypatch.setattr(app.oa_imports, 'fetch_public', lambda url: (b'%PDF-fake', 'application/pdf', 'sha-direct'))
    monkeypatch.setattr(app.oa_imports, 'extract_preview', lambda content, content_type, metadata: {
        'content_kind': 'pdf', 'metadata': metadata, 'abstract': 'Direct OA',
        'fragments': [{'location': 'page:1', 'text': 'Direct OA fragment'}], 'text_preview': 'Direct OA fragment',
        'extraction_quality': 'ok', 'parser_version': 'test', 'warnings': [],
    })
    response = client.post(f'/api/projects/{pid}/imports', json={'url': 'https://example.org/public.pdf'})
    assert response.status_code == 200 and response.json()['status'] == 'preview_ready'


def test_pdf_confirmation_indexes_late_pages_beyond_the_preview(client, monkeypatch):
    pid = _create(client)
    raw = BytesIO()
    document = canvas.Canvas(raw)
    for page in range(1, 11):
        for line in range(30):
            document.drawString(30, 780 - line * 20, "A passage about finite volume reconstruction and numerical fluxes.")
        if page == 10:
            document.drawString(30, 160, "LatePageIndexMarker")
        document.showPage()
    document.save()
    pdf_bytes = raw.getvalue()
    monkeypatch.setattr(app.oa_imports, 'fetch_public', lambda url: (pdf_bytes, 'application/pdf', 'sha-late-page'))

    base = f'/api/projects/{pid}'
    imported = client.post(base + '/imports', json={'url': 'https://example.org/source.pdf'}).json()['import']
    preview = imported['preview']
    assert len(preview['fragments']) == 8
    assert preview['page_count'] == preview['text_page_count'] == 10
    assert 'full_fragments' not in preview
    import_path = base + '/imports/' + imported['id']
    confirmed = client.post(import_path + '/confirm', json={'keep_attachment': True}).json()
    corpus_id = confirmed['corpus_id']
    query = {'query': 'LatePageIndexMarker', 'corpus_id': corpus_id}
    for _ in range(2):
        hits = client.get(base + '/corpus/search', params=query).json()['results']
        assert [hit['location'] for hit in hits] == ['page:10']
        assert hits[0]['pdf_import_id'] == imported['id']
        assert client.post(f'{base}/corpus/{corpus_id}/index', json={}).status_code == 200
    assert client.get(import_path + '/pdf').content == pdf_bytes
    other = _create(client)
    assert client.get(f"/api/projects/{other}/imports/{imported['id']}/pdf").status_code == 404
    assert client.post(import_path + '/cancel').status_code == 200
    assert client.get(import_path + '/pdf').status_code == 404


def test_cancelled_oa_import_revokes_index_and_citation_evidence(client, monkeypatch):
    pid = _create(client)
    candidate_id = _candidate(pid, access_status='open_access', oa_url='https://example.org/revoked.pdf')
    monkeypatch.setattr(app.oa_imports, 'fetch_public', lambda url: (b'%PDF-fake', 'application/pdf', 'sha-revoked'))
    monkeypatch.setattr(app.oa_imports, 'extract_preview', lambda content, content_type, metadata: {
        'content_kind': 'pdf', 'metadata': metadata, 'abstract': 'Graphene stability study',
        'fragments': [{'location': 'page:1', 'text': 'Graphene stability improves under controlled conditions.'}],
        'text_preview': 'Graphene stability improves under controlled conditions.', 'extraction_quality': 'ok',
        'parser_version': 'test', 'warnings': [],
    })
    imported = client.post(f'/api/projects/{pid}/imports', json={'candidate_id': candidate_id}).json()
    confirmed = client.post(f"/api/projects/{pid}/imports/{imported['import']['id']}/confirm", json={'keep_attachment': True}).json()
    evidence_id = confirmed['evidence_id']
    reference = client.post(f'/api/references', json={'title': 'Graphene stability study', 'authors': 'Author', 'year': '2024', 'abstract': 'Graphene stability improves under controlled conditions.'}).json()
    ref_id = reference['id']
    linked = client.post(f'/api/projects/{pid}/references/{ref_id}', json={'request_id': 'link-revoked', 'reference_version': 1, 'cite_key': 'graphene2024'}).json()
    attached = client.post(
        f'/api/projects/{pid}/references/{ref_id}/attachments',
        json={'project_id': pid, 'external_evidence_id': evidence_id, 'base_version': 1},
    )
    assert attached.status_code == 200, attached.text
    refreshed = client.post(
        f'/api/projects/{pid}/references/{ref_id}/refresh',
        json={'request_id': 'refresh-revoked', 'reference_version': 2, 'base_version': 1},
    )
    assert refreshed.status_code == 200, refreshed.text
    before = client.post(f'/api/projects/{pid}/citation-check', json={'content': r'Graphene stability improves under controlled conditions\cite{graphene2024}.'}).json()
    support_before = before['claims'][0]['supports'][0]
    assert support_before['evidence'] and support_before['support_status'] == 'not_verifiable'
    assert support_before['confidence'] == 'low'

    cancelled = client.post(f"/api/projects/{pid}/imports/{imported['import']['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    search = client.get(f'/api/projects/{pid}/corpus/search', params={'query': 'Graphene'})
    assert search.status_code == 200, search.text
    assert search.json()['results'] == []
    after = client.post(f'/api/projects/{pid}/citation-check', json={'content': r'Graphene stability improves under controlled conditions\cite{graphene2024}.'}).json()
    support_after = after['claims'][0]['supports'][0]
    assert support_after['evidence'] == []
    assert support_after['confidence'] != 'high'
