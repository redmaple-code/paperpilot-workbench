from __future__ import annotations

import app

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app, "PROJECTS_DIR", tmp_path / "projects")
    app.PROJECTS_DIR.mkdir()
    app.init_db()
    return TestClient(app.app)



def _create(client):
    response = client.post('/api/projects', json={'name': '搜索测试', 'domain': '材料科学'})
    assert response.status_code == 200
    return response.json()['project']['id']


def test_academic_search_rank_dedupe_and_handoff(client, monkeypatch):
    pid = _create(client)

    class FakeProvider:
        name = 'fake'
        def search(self, query, filters, cursor, limit):
            assert query == 'graphene stability'
            assert filters['year_from'] == 2020
            return {'next_cursor': 'next-1', 'results': [
                {'id': 'https://openalex.org/W1', 'title': 'Graphene stability study', 'publication_year': 2024,
                 'doi': 'https://doi.org/10.1000/ABC', 'authorships': [{'author': {'display_name': 'A Author'}}],
                 'primary_location': {'landing_page_url': 'https://example.org/a', 'source': {'display_name': 'Journal A'}, 'is_oa': True},
                 'open_access': {'is_oa': True}},
                {'id': 'https://openalex.org/W2', 'title': 'Graphene stability study', 'publication_year': 2024,
                 'doi': '10.1000/abc', 'authorships': [{'author': {'display_name': 'A Author'}}],
                 'primary_location': {'landing_page_url': 'https://example.org/a2', 'source': {'display_name': 'Journal B'}},
                 'open_access': {'is_oa': False}},
            ]}

    monkeypatch.setattr(app.academic_search, 'provider_from_env', lambda: FakeProvider())
    result = client.post(f'/api/projects/{pid}/search', json={'save':True,'query': ' graphene  stability ', 'filters': {'year_from': 2020}})
    assert result.status_code == 200
    body = result.json()
    assert body['ok'] and body['status'] == 'succeeded'
    assert len(body['candidates']) == 1
    candidate = body['candidates'][0]
    assert candidate['doi'] == '10.1000/abc'
    assert len(candidate['sources']) == 2
    assert 'graphene' in candidate['match_reasons']

    handoff = client.post(f'/api/projects/{pid}/search/handoff', json={'candidate_id': candidate['id']}).json()
    replay = client.post(f'/api/projects/{pid}/search/handoff', json={'candidate_id': candidate['id']}).json()
    assert handoff['ok'] and replay['idempotent_replay']
    assert handoff['handoff']['id'] == replay['handoff']['id']
    project = client.get(f'/api/projects/{pid}').json()
    assert project['refs'] == [] and project['corpus'] == []


def test_academic_search_generation_and_failure(client, monkeypatch):
    pid = _create(client)

    class FakeProvider:
        name = 'fake'
        def search(self, query, filters, cursor, limit):
            return {'next_cursor': None, 'results': [{'title': query, 'publication_year': 2023, 'id': 'W1'}]}

    monkeypatch.setattr(app.academic_search, 'provider_from_env', lambda: FakeProvider())
    first = client.post(f'/api/projects/{pid}/search', json={'save':True,'query': 'first'}).json()
    second = client.post(f'/api/projects/{pid}/search', json={'save':True,'query': 'second'}).json()
    assert first['search']['status'] == 'succeeded' and second['search']['status'] == 'succeeded'
    assert first['search']['id'] != second['search']['id']
    assert client.post(f'/api/projects/{pid}/search', json={'save':True,'query': '   '}).status_code == 400

    class BrokenProvider:
        name = 'broken'
        def search(self, query, filters, cursor, limit):
            raise app.academic_search.AcademicSearchError('rate_limited', '限流', retryable=True)

    monkeypatch.setattr(app.academic_search, 'provider_from_env', lambda: BrokenProvider())
    failed = client.post(f'/api/projects/{pid}/search', json={'save':True,'query': 'retry'}).json()
    assert failed['status'] == 'failed' and failed['error']['code'] == 'rate_limited' and failed['error']['retryable']


def test_academic_search_save_snapshot(client, monkeypatch):
    pid = _create(client)

    class FakeProvider:
        name = 'fake'
        def search(self, query, filters, cursor, limit):
            return {'next_cursor': None, 'results': []}

    monkeypatch.setattr(app.academic_search, 'provider_from_env', lambda: FakeProvider())
    result = client.post(f'/api/projects/{pid}/search', json={'save':True,'query': 'empty'}).json()
    assert result['status'] == 'empty'
    sid = result['search']['id']
    saved = client.post(f'/api/projects/{pid}/search/{sid}/save').json()
    assert saved['saved'] is True
    restored = client.get(f'/api/projects/{pid}/search/{sid}').json()
    assert restored['search']['saved'] is True and restored['candidates'] == []


def test_academic_search_ai_style_summary_history_and_candidate_save(client, monkeypatch):
    pid = _create(client)

    class FakeProvider:
        name = 'fake'
        def search(self, query, filters, cursor, limit):
            return {'next_cursor': None, 'results': [
                {'id': 'W1', 'title': 'Graphene batteries review', 'publication_year': 2024, 'doi': '10.1000/review',
                 'primary_location': {'landing_page_url': 'https://example.org/review', 'is_oa': True, 'source': {'display_name': 'Journal'}},
                 'open_access': {'is_oa': True}},
                {'id': 'W2', 'title': 'Graphene battery mechanism', 'publication_year': 2023, 'doi': '10.1000/mechanism',
                 'primary_location': {'landing_page_url': 'https://example.org/mechanism', 'source': {'display_name': 'Journal'}},
                 'open_access': {'is_oa': False}},
            ]}

    monkeypatch.setattr(app.academic_search, 'provider_from_env', lambda: FakeProvider())
    response = client.post(f'/api/projects/{pid}/search', json={'save':True,'query': 'graphene battery'})
    body = response.json()
    assert body['summary']['counts']['total'] == 2
    assert body['summary']['counts']['open_access'] == 1
    assert body['summary']['plan']
    assert body['summary']['expanded_queries'] == ['graphene battery']
    assert body['summary']['deduped_count'] == 2
    candidate_id = body['candidates'][0]['id']
    saved = client.post(f'/api/projects/{pid}/search/candidates/{candidate_id}/save').json()
    assert saved['saved'] is True
    restored = client.get(f'/api/projects/{pid}/search/{body["search"]["id"]}').json()
    assert any(x['saved'] for x in restored['candidates'])
    history = client.get(f'/api/projects/{pid}/searches').json()
    assert history['searches'][0]['candidate_count'] == 2


