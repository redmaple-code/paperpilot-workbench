"""In-process, nonpersistent state for explicitly scoped academic-search sessions.

This module does not write to disk and is not yet wired to the public routes.
Callers must authenticate/bind session IDs and handle durable promotion separately.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import re
import threading
import time
import uuid
from typing import Any

from paperpilot.services import academic_search


class SearchSessionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SearchSessionStore:
    """Bounded private working state; durable records live outside this store."""

    def __init__(self, *, max_sessions: int = 32, max_searches: int = 50,
                 max_candidates: int = 500, max_total_candidates: int = 2000,
                 max_total_bytes: int = 16 * 1024 * 1024, idle_seconds: float = 8 * 3600,
                 clock=time.monotonic):
        if min(max_sessions, max_searches, max_candidates, max_total_candidates, max_total_bytes) < 1 or idle_seconds <= 0:
            raise ValueError('invalid session limits')
        self._max_sessions = max_sessions
        self._max_searches = max_searches
        self._max_candidates = max_candidates
        self._max_total_candidates = max_total_candidates
        self._max_total_bytes = max_total_bytes
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _validate_session(session_id: str) -> None:
        if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32,128}', session_id):
            raise SearchSessionError('invalid_session', '检索会话无效，请刷新页面重试。')

    def _expire(self) -> None:
        cutoff = self._clock() - self._idle_seconds
        for key, session in list(self._sessions.items()):
            if session['last_access'] < cutoff:
                del self._sessions[key]

    def _session(self, session_id: str, *, create: bool = False) -> dict[str, Any]:
        self._validate_session(session_id)
        self._expire()
        session = self._sessions.get(session_id)
        if session is None:
            if not create:
                raise SearchSessionError('session_expired', '检索会话已结束；请重新搜索。')
            if len(self._sessions) >= self._max_sessions:
                raise SearchSessionError('session_capacity', '检索会话已达上限；请结束不再使用的会话。')
            session = {'last_access': self._clock(), 'searches': {}, 'generation': {}}
            self._sessions[session_id] = session
        session['last_access'] = self._clock()
        return session

    @staticmethod
    def _search(session: dict[str, Any], project_id: str, search_id: str) -> dict[str, Any]:
        item = session['searches'].get(search_id)
        if item is None or item['project_id'] != project_id:
            raise SearchSessionError('search_missing', '当前会话中没有该检索；请重新搜索或查看已保存历史。')
        return item

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return deepcopy({key: value for key, value in item.items() if key not in {'attempt', 'generation', '_durable_version'}})

    def begin(self, session_id: str, project_id: str, query: str, filters: dict[str, Any],
              provider: str, rule_version: str, request_id: str) -> dict[str, Any]:
        query = academic_search.normalize_query(query)
        if not query or len(query)>240:
            raise SearchSessionError('invalid_query', '关键词不能为空或超过长度限制。')
        if any(not isinstance(value,str) or len(value)>240 for value in (project_id,provider,rule_version,request_id)):
            raise SearchSessionError('invalid_search', '检索信息无效，请重新搜索。')
        filters = academic_search.validate_filters(filters)
        with self._lock:
            session = self._session(session_id, create=True)
            if len(session['searches']) >= self._max_searches:
                raise SearchSessionError('search_capacity', '本次会话的检索数量已达上限；请保存需要的结果后删除旧检索。')
            generation = session['generation'].get(project_id, 0) + 1
            session['generation'][project_id] = generation
            for item in session['searches'].values():
                if item['project_id'] == project_id and item['status'] in {'loading', 'succeeded', 'empty', 'failed'}:
                    item['status'] = 'superseded'
            search_id = uuid.uuid4().hex[:16]
            attempt = uuid.uuid4().hex
            item = {'id': search_id, 'project_id': project_id, 'query': query,
                    'filters': deepcopy(filters), 'provider': provider, 'rule_version': rule_version,
                    'request_id': request_id, 'status': 'loading', 'next_cursor': None,
                    'saved': False, 'created_at': academic_search.now(), 'updated_at': academic_search.now(),
                    'generation': generation, 'attempt': attempt, 'candidates': [], 'summary': None}
            session['searches'][search_id] = item
            return {'search': self._public(item), 'attempt': attempt}

    def begin_page(self, session_id: str, project_id: str, search_id: str, *, query: str,
                   filters: dict[str, Any], provider: str, rule_version: str, cursor: str) -> dict[str, Any]:
        filters = academic_search.validate_filters(filters)
        with self._lock:
            session = self._session(session_id)
            item = self._search(session, project_id, search_id)
            if item['status'] == 'superseded':
                raise SearchSessionError('search_superseded', '该检索已被新查询替代，请重新搜索。')
            if item['status'] == 'loading':
                raise SearchSessionError('search_in_progress', '该检索仍在进行，请等待结果。')
            if (academic_search.normalize_query(query) != item['query'] or filters != item['filters']
                    or provider != item['provider'] or rule_version != item['rule_version']):
                raise SearchSessionError('search_changed', '查询、来源或规则已变化，请重新搜索。')
            if not cursor or cursor != item['next_cursor']:
                raise SearchSessionError('search_cursor_stale', '搜索页已变化，请刷新当前结果后继续。')
            item['status'] = 'loading'
            item.pop('error',None)
            item['attempt'] = uuid.uuid4().hex
            item['updated_at'] = academic_search.now()
            return {'search': self._public(item), 'attempt': item['attempt']}

    def complete(self, session_id: str, project_id: str, search_id: str, attempt: str, *,
                 candidates: list[dict[str, Any]], next_cursor: str | None,
                 expanded_queries: list[str] | None = None,
                 diagnostics: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            item = self._search(session, project_id, search_id)
            if item['status'] != 'loading' or item['attempt'] != attempt:
                raise SearchSessionError('search_superseded', '该检索已被替代，迟到结果未采用。')
            previous = item['candidates']
            if len(candidates)>self._max_candidates:
                item['status']='failed'
                raise SearchSessionError('candidate_capacity', '本次检索结果已达会话上限；已显示的结果仍保留。')
            prior = item['summary'] or {}
            fields = ('title', 'authors', 'year', 'venue', 'doi', 'canonical_url', 'abstract',
                      'provider', 'access_status', 'oa_url', 'dedupe_key', 'external_id', 'sources')
            old = [{key: candidate.get(key, '' if key != 'sources' else []) for key in fields} for candidate in previous]
            merged = academic_search.rank_and_deduplicate(old + deepcopy(candidates), item['query'],
                                                           expanded_queries or prior.get('expanded_queries'))
            other_count=sum(len(other['candidates']) for other in session['searches'].values() if other['id']!=search_id)
            other_data=[other for other in session['searches'].values() if other['id']!=search_id]
            total_bytes=len(json.dumps([other_data,merged,diagnostics or [],expanded_queries or []],ensure_ascii=False).encode('utf-8'))
            if len(merged) > self._max_candidates or other_count+len(merged)>self._max_total_candidates or total_bytes>self._max_total_bytes:
                item['status'] = 'failed'
                raise SearchSessionError('candidate_capacity', '本次检索结果已达会话上限；已显示的结果仍保留。')
            existing = {candidate['dedupe_key']: candidate for candidate in previous}
            resolved = []
            for candidate in merged:
                previous_item = existing.get(candidate['dedupe_key'])
                resolved.append({**candidate, 'id': previous_item['id'] if previous_item else uuid.uuid4().hex[:16],
                                 'search_id': search_id, 'project_id': project_id,
                                 'saved': bool(previous_item and previous_item['saved'])})
            current = deepcopy(diagnostics) if diagnostics else [{'provider': item['provider'], 'query': item['query'],
                       'pages': 1, 'raw_count': len(candidates), 'exhausted': not bool(next_cursor)}]
            all_diagnostics = deepcopy(prior.get('provider_diagnostics') or []) + current
            diagnostic_summary = {'expanded_queries': expanded_queries or prior.get('expanded_queries') or [item['query']],
                'sources': sorted({entry.get('provider') for entry in all_diagnostics if entry.get('provider')}),
                'pages_fetched': sum(int(entry.get('pages') or 0) for entry in all_diagnostics),
                'raw_count': sum(int(entry.get('raw_count') or 0) for entry in all_diagnostics),
                'source_failures': [{'provider': entry.get('provider'), 'error': entry.get('error')}
                                    for entry in all_diagnostics if entry.get('error')],
                'truncated': any(entry.get('exhausted') is False for entry in current if not entry.get('error'))}
            summary = academic_search.search_summary(resolved, item['query'], item['filters'], item['provider'], diagnostic_summary)
            summary.update(rule_version=item['rule_version'], provider_diagnostics=all_diagnostics,
                           has_more=bool(next_cursor), retrieved_at=academic_search.now())
            item.update(candidates=resolved, next_cursor=next_cursor, summary=summary,
                        status='succeeded' if resolved else 'empty', updated_at=academic_search.now())
            return self._public(item)

    def fail(self, session_id: str, project_id: str, search_id: str, attempt: str,
             *, code: str, message: str, retryable: bool) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            item = self._search(session, project_id, search_id)
            if item['status'] != 'loading' or item['attempt'] != attempt:
                raise SearchSessionError('search_superseded', '该检索已被替代，迟到结果未采用。')
            item.update(status='failed', updated_at=academic_search.now(),
                        error={'code': code, 'message': message, 'retryable': bool(retryable)})
            return self._public(item)

    def get(self, session_id: str, project_id: str, search_id: str) -> dict[str, Any]:
        with self._lock:
            return self._public(self._search(self._session(session_id), project_id, search_id))

    @contextmanager
    def locked(self):
        """Freeze session changes while the caller holds its database write lock."""
        with self._lock:
            yield

    def set_candidate_saved(self, session_id: str, project_id: str, search_id: str,
                            candidate_id: str, saved: bool) -> dict[str, Any]:
        # This only updates working state; durable promotion must commit separately.
        with self._lock:
            item = self._search(self._session(session_id), project_id, search_id)
            for candidate in item['candidates']:
                if candidate['id'] == candidate_id:
                    candidate['saved'] = bool(saved)
                    return deepcopy(candidate)
            raise SearchSessionError('candidate_missing', '当前会话中没有该候选。')

    @contextmanager
    def locked_snapshot(self, session_id: str, project_id: str, search_id: str):
        """Freeze during durable promotion; acquire any database write lock first."""
        with self._lock:
            item = self._search(self._session(session_id), project_id, search_id)
            if item['status'] == 'loading':
                raise SearchSessionError('search_in_progress', '检索仍在进行，请等待后再保存。')
            snapshot=self._public(item)
            snapshot['_durable_version']=item.get('_durable_version')
            yield snapshot

    def mark_snapshot_saved(self, session_id: str, project_id: str, search_id: str, durable_version: str) -> None:
        """Call only after durable promotion has committed, while holding locked_snapshot."""
        with self._lock:
            session=self._sessions.get(session_id)
            if session is None:raise SearchSessionError('session_expired', '检索会话已结束；请查看已保存历史。')
            session['last_access']=self._clock()
            item = self._search(session, project_id, search_id)
            item['saved'] = True
            item['_durable_version'] = durable_version
            item['updated_at'] = academic_search.now()

    def mark_candidate_saved(self, session_id: str, project_id: str, search_id: str,
                             candidate_id: str, durable_version: str) -> None:
        """Update working state only after the candidate transaction commits."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SearchSessionError('session_expired', '检索会话已结束；请查看已保存历史。')
            session['last_access'] = self._clock()
            item = self._search(session, project_id, search_id)
            for candidate in item['candidates']:
                if candidate['id'] == candidate_id:
                    candidate['saved'] = True
                    item['_durable_version'] = durable_version
                    return
            raise SearchSessionError('candidate_missing', '当前会话中没有该候选。')

    def mark_durable_version(self, session_id: str, project_id: str, search_id: str, durable_version: str) -> None:
        """Advance the committed version without changing a candidate's saved flag."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SearchSessionError('session_expired', '检索会话已结束；请查看已保存历史。')
            session['last_access'] = self._clock()
            self._search(session, project_id, search_id)['_durable_version'] = durable_version

    def mark_candidate_unsaved(self, session_id: str, project_id: str, search_id: str,
                               candidate_id: str, durable_version: str | None) -> None:
        """Reflect a committed unsave, including removal of a partial snapshot."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SearchSessionError('session_expired', '检索会话已结束；请重新搜索。')
            session['last_access'] = self._clock()
            item = self._search(session, project_id, search_id)
            for candidate in item['candidates']:
                if candidate['id'] == candidate_id:
                    candidate['saved'] = False
                    item['_durable_version'] = durable_version
                    return
            raise SearchSessionError('candidate_missing', '当前会话中没有该候选。')

    def delete(self, session_id: str, project_id: str, search_id: str) -> None:
        with self._lock:
            session = self._session(session_id)
            self._search(session, project_id, search_id)
            del session['searches'][search_id]

    def close(self, session_id: str) -> None:
        self._validate_session(session_id)
        with self._lock:
            self._sessions.pop(session_id, None)
