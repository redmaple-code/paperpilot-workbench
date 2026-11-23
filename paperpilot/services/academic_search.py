from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

RULE_VERSION = "hybrid-v4"
DEFAULT_PROVIDER = "openalex"
DEFAULT_URL = "https://api.openalex.org/works"
WENO_QUERY_EXPANSIONS = (
    "weighted essentially non-oscillatory",
    "WENO-JS",
    "WENO-Z",
    "WENO-AO",
)


class AcademicSearchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class AcademicSearchProvider(Protocol):
    name: str

    def search(self, query: str, filters: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]: ...


@dataclass
class OpenAlexProvider:
    base_url: str = DEFAULT_URL
    timeout: float = 15.0
    api_key: str = ""
    mailto: str = ""
    name: str = DEFAULT_PROVIDER

    def search(self, query: str, filters: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {"search": query, "per-page": limit, "cursor": cursor or "*"}
        year_from = filters.get("year_from")
        year_to = filters.get("year_to")
        filter_parts: list[str] = []
        if year_from:
            filter_parts.append(f"from_publication_date:{int(year_from)}-01-01")
        if year_to:
            filter_parts.append(f"to_publication_date:{int(year_to)}-12-31")
        if filters.get("open_access") is True:
            filter_parts.append("is_oa:true")
        if filter_parts:
            params["filter"] = ",".join(filter_parts)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto
        try:
            response = requests.get(self.base_url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AcademicSearchError("provider_unavailable", "学术搜索服务暂时不可用", retryable=True) from exc
        if response.status_code == 429:
            raise AcademicSearchError("rate_limited", "学术搜索服务请求过于频繁，请稍后重试", retryable=True, status_code=429)
        if response.status_code in {401, 403}:
            try:body=response.json()
            except ValueError:body={}
            error=str(body.get('error','')).lower() if isinstance(body,dict) else ''
            if cursor and cursor!='*' and not self.api_key and 'api key required for cursor paging' in error:
                raise AcademicSearchError("provider_auth_failed", "OpenAlex 后续分页需要免费 API Key；请在 OpenAlex 设置页获取，在本机配置 PWA_ACADEMIC_SEARCH_API_KEY 后重启服务，再点击加载更多。此前结果已保留。", status_code=response.status_code)
            raise AcademicSearchError("provider_auth_failed", "OpenAlex 鉴权失败，请检查本机 API Key 配置。", status_code=response.status_code)
        if response.status_code >= 500:
            raise AcademicSearchError("provider_unavailable", "学术搜索服务发生故障，请稍后重试", retryable=True, status_code=response.status_code)
        if response.status_code >= 400:
            raise AcademicSearchError("provider_bad_request", "学术搜索请求无效，请修改关键词或筛选条件", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AcademicSearchError("provider_invalid_response", "学术搜索服务返回了无法解析的结果", retryable=True) from exc
        return {"results": payload.get("results") or [], "next_cursor": (payload.get("meta") or {}).get("next_cursor")}


@dataclass
class CrossrefProvider:
    base_url: str = "https://api.crossref.org/works"
    timeout: float = 15.0
    mailto: str = ""
    name: str = "crossref"

    def search(self, query: str, filters: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {"query.bibliographic": query, "rows": limit, "cursor": cursor or "*"}
        filter_parts: list[str] = []
        if filters.get("year_from"):
            filter_parts.append(f"from-pub-date:{int(filters['year_from'])}-01-01")
        if filters.get("year_to"):
            filter_parts.append(f"until-pub-date:{int(filters['year_to'])}-12-31")
        if filter_parts:
            params["filter"] = ",".join(filter_parts)
        headers = {"User-Agent": f"PaperPilot/1.0 (mailto:{self.mailto})" if self.mailto else "PaperPilot/1.0"}
        try:
            response = requests.get(self.base_url, params=params, timeout=self.timeout, headers=headers)
        except requests.RequestException as exc:
            raise AcademicSearchError("provider_unavailable", "Crossref 暂时不可用", retryable=True) from exc
        if response.status_code == 429:
            raise AcademicSearchError("rate_limited", "Crossref 请求过于频繁", retryable=True, status_code=429)
        if response.status_code >= 500:
            raise AcademicSearchError("provider_unavailable", "Crossref 服务发生故障", retryable=True, status_code=response.status_code)
        if response.status_code >= 400:
            raise AcademicSearchError("provider_bad_request", "Crossref 搜索请求无效", status_code=response.status_code)
        try:
            message = response.json().get("message") or {}
        except ValueError as exc:
            raise AcademicSearchError("provider_invalid_response", "Crossref 返回了无法解析的结果", retryable=True) from exc
        return {"results": message.get("items") or [], "next_cursor": message.get("next-cursor")}


@dataclass
class SemanticScholarProvider:
    base_url: str = "https://api.semanticscholar.org/graph/v1/paper/search"
    timeout: float = 15.0
    api_key: str = ""
    name: str = "semanticscholar"

    def search(self, query: str, filters: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]:
        offset = int(cursor or 0)
        params: dict[str, Any] = {
            "query": query,
            "limit": min(limit, 100),
            "offset": offset,
            "fields": "title,authors,year,venue,abstract,externalIds,openAccessPdf,url",
        }
        if filters.get("year_from") or filters.get("year_to"):
            start = filters.get("year_from") or ""
            end = filters.get("year_to") or ""
            params["year"] = f"{start}-{end}" if start and end else (f"{start}-" if start else f"-{end}")
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        try:
            response = requests.get(self.base_url, params=params, timeout=self.timeout, headers=headers)
        except requests.RequestException as exc:
            raise AcademicSearchError("provider_unavailable", "Semantic Scholar 暂时不可用", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise AcademicSearchError("provider_auth_failed", "Semantic Scholar 鉴权失败", status_code=response.status_code)
        if response.status_code == 429:
            raise AcademicSearchError("rate_limited", "Semantic Scholar 请求过于频繁", retryable=True, status_code=429)
        if response.status_code >= 500:
            raise AcademicSearchError("provider_unavailable", "Semantic Scholar 服务发生故障", retryable=True, status_code=response.status_code)
        if response.status_code >= 400:
            raise AcademicSearchError("provider_bad_request", "Semantic Scholar 搜索请求无效", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AcademicSearchError("provider_invalid_response", "Semantic Scholar 返回了无法解析的结果", retryable=True) from exc
        rows = payload.get("data") or []
        next_offset = offset + len(rows)
        return {"results": rows, "next_cursor": str(next_offset) if len(rows) >= min(limit, 100) else None}


def expand_query(query: str, *, max_variants: int | None = None) -> list[str]:
    """Expand WENO shorthand while keeping ordinary queries unchanged."""
    base = normalize_query(query)
    if not base:
        return []
    variants = [base]
    if re.search(r"\bweno\b", base, flags=re.IGNORECASE) or "weighted essentially non-oscillatory" in base.lower():
        variants.extend(f"{base} {term}" for term in WENO_QUERY_EXPANSIONS)
    cap = max_variants or int(os.getenv("PWA_ACADEMIC_MAX_QUERY_VARIANTS", "5"))
    return list(dict.fromkeys(variants))[: max(1, cap)]


@dataclass
class MultiSourceProvider:
    providers: list[AcademicSearchProvider]
    max_pages: int = 2
    name: str = "multi"

    def __post_init__(self) -> None:
        self.name = "+".join(provider.name for provider in self.providers)

    def _fetch_variant(self, provider: AcademicSearchProvider, variant: str, filters: dict[str, Any], limit: int) -> dict[str, Any]:
        cursor: str | None = None
        rows: list[dict[str, Any]] = []
        pages = 0
        while pages < self.max_pages:
            result = provider.search(variant, filters, cursor, limit)
            page_rows = [dict(item) for item in (result.get("results") or []) if isinstance(item, dict)]
            for item in page_rows:
                item["__provider"] = provider.name
                item["__query_variant"] = variant
            rows.extend(page_rows)
            pages += 1
            cursor = result.get("next_cursor")
            if not cursor or not page_rows:
                break
        return {"provider": provider.name, "variant": variant, "pages": pages, "results": rows, "exhausted": not bool(cursor)}

    def search(self, query: str, filters: dict[str, Any], cursor: str | None, limit: int) -> dict[str, Any]:
        # A new search exhausts each provider/variant automatically. `cursor` is
        # accepted for API compatibility; continuation is handled internally.
        variants = expand_query(query)
        jobs = [(provider, variant) for provider in self.providers for variant in variants]
        results: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        errors: list[AcademicSearchError] = []
        workers = min(8, max(1, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(self._fetch_variant, provider, variant, filters, limit): (provider, variant) for provider, variant in jobs}
            for future in as_completed(future_map):
                provider, variant = future_map[future]
                try:
                    detail = future.result()
                    results.extend(detail["results"])
                    diagnostics.append({"provider": provider.name, "query": variant, "pages": detail["pages"], "raw_count": len(detail["results"]), "exhausted": detail["exhausted"]})
                except AcademicSearchError as exc:
                    errors.append(exc)
                    diagnostics.append({"provider": provider.name, "query": variant, "pages": 0, "raw_count": 0, "error": exc.code})
        if not results and errors and len(errors) == len(jobs):
            raise errors[0]
        diagnostics.sort(key=lambda item: (item["provider"], item["query"]))
        return {"results": results, "next_cursor": None, "expanded_queries": variants, "diagnostics": diagnostics}


def provider_from_env() -> AcademicSearchProvider:
    timeout = float(os.getenv("PWA_ACADEMIC_SEARCH_TIMEOUT", "15"))
    mailto = os.getenv("PWA_ACADEMIC_SEARCH_MAILTO", "")
    names = [x.strip().lower() for x in os.getenv("PWA_ACADEMIC_SOURCES", "openalex").split(",") if x.strip()]
    providers: list[AcademicSearchProvider] = []
    for name in names:
        if name == "openalex":
            providers.append(OpenAlexProvider(base_url=os.getenv("PWA_ACADEMIC_SEARCH_URL", DEFAULT_URL), timeout=timeout, api_key=os.getenv("PWA_ACADEMIC_SEARCH_API_KEY", ""), mailto=mailto))
        elif name == "crossref":
            providers.append(CrossrefProvider(timeout=timeout, mailto=mailto))
        elif name in {"semanticscholar", "semantic_scholar", "s2"}:
            providers.append(SemanticScholarProvider(timeout=timeout, api_key=os.getenv("PWA_SEMANTIC_SCHOLAR_API_KEY", "")))
    if not providers:
        providers = [OpenAlexProvider(timeout=timeout, mailto=mailto)]
    if len(providers) == 1:
        return providers[0]
    return MultiSourceProvider(providers=providers, max_pages=max(1, int(os.getenv("PWA_ACADEMIC_MAX_PAGES", "3"))))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_query(query: str) -> str:
    return " ".join(str(query or "").split()).strip()


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    return text.removeprefix("doi:").strip().rstrip(".")


def canonical_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return text


def reconstruct_abstract(inverted: Any) -> str:
    if not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in inverted.items():
        if isinstance(offsets, list):
            positions.extend((int(offset), str(word)) for offset in offsets if isinstance(offset, int))
    return " ".join(word for _, word in sorted(positions))


def normalize_candidate(raw: dict[str, Any], provider: str) -> dict[str, Any]:
    if provider == "crossref":
        raw = _crossref_as_openalex(raw)
    elif provider == "semanticscholar":
        raw = _semanticscholar_as_openalex(raw)
    doi = normalize_doi(raw.get("doi"))
    primary_location = raw.get("primary_location") or {}
    source = primary_location.get("source") or {}
    url = canonical_url(primary_location.get("landing_page_url") or raw.get("publication_url") or raw.get("id"))
    authors = ", ".join(
        str((item.get("author") or {}).get("display_name") or "").strip()
        for item in (raw.get("authorships") or [])
        if (item.get("author") or {}).get("display_name")
    )
    abstract = str(raw.get("abstract_inverted_index") and reconstruct_abstract(raw.get("abstract_inverted_index")) or raw.get("abstract") or "").strip()
    oa = raw.get("open_access") or {}
    best_oa = raw.get("best_oa_location") or {}
    locations = raw.get("locations") or []
    oa_location = best_oa or next((item for item in locations if isinstance(item, dict) and (item.get("is_oa") or item.get("pdf_url"))), {})
    if not oa_location and (oa.get("is_oa") or primary_location.get("is_oa")):
        oa_location = primary_location
    oa_url = canonical_url(oa_location.get("pdf_url") or oa_location.get("landing_page_url"))
    is_oa = bool(oa.get("is_oa") or primary_location.get("is_oa") or oa_location.get("is_oa"))
    access_status = "open_access" if is_oa and oa_url else ("metadata_only" if url else "unavailable")
    title = " ".join(str(raw.get("title") or "").split())
    year = raw.get("publication_year") or ""
    venue = str(source.get("display_name") or "").strip()
    key_source = doi or url or f"{title.lower()}|{authors.lower()}|{year}"
    dedupe_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:24]
    return {
        "title": title,
        "authors": authors,
        "year": str(year),
        "venue": venue,
        "doi": doi,
        "canonical_url": url,
        "abstract": abstract[:2000],
        "provider": provider,
        "access_status": access_status,
        "oa_url": oa_url,
        "dedupe_key": dedupe_key,
        "external_id": str(raw.get("id") or ""),
    }


def _crossref_as_openalex(raw: dict[str, Any]) -> dict[str, Any]:
    title = (raw.get("title") or [""])[0]
    authors = [{"author": {"display_name": f"{item.get('given', '')} {item.get('family', '')}".strip()}} for item in (raw.get("author") or [])]
    date_parts = ((raw.get("published-print") or raw.get("published-online") or raw.get("published") or {}).get("date-parts") or [[]])[0]
    pdf = next((item.get("URL") for item in (raw.get("link") or []) if "pdf" in str(item.get("content-type", "")).lower()), "")
    # Crossref links are not proof of open access; require an explicit license
    # before exposing a full-text action.
    is_oa = bool(pdf and raw.get("license"))
    return {"title": title, "authorships": authors, "publication_year": date_parts[0] if date_parts else "", "doi": raw.get("DOI", ""), "abstract": re.sub(r"<[^>]+>", " ", str(raw.get("abstract", ""))), "primary_location": {"landing_page_url": raw.get("URL", ""), "source": {"display_name": (raw.get("container-title") or [""])[0]}, "is_oa": is_oa}, "best_oa_location": {"pdf_url": pdf, "is_oa": is_oa} if pdf else {}, "open_access": {"is_oa": is_oa}, "id": raw.get("URL", "")}


def _semanticscholar_as_openalex(raw: dict[str, Any]) -> dict[str, Any]:
    external = raw.get("externalIds") or {}
    doi = external.get("DOI", "")
    authors = [{"author": {"display_name": item.get("name", "")}} for item in (raw.get("authors") or [])]
    oa = raw.get("openAccessPdf") or {}
    is_oa = bool(oa.get("url"))
    return {"title": raw.get("title", ""), "authorships": authors, "publication_year": raw.get("year") or "", "doi": doi, "abstract": raw.get("abstract") or "", "primary_location": {"landing_page_url": raw.get("url", ""), "source": {"display_name": raw.get("venue", "")}, "is_oa": is_oa}, "best_oa_location": {"pdf_url": oa.get("url", ""), "is_oa": is_oa} if is_oa else {}, "open_access": {"is_oa": is_oa}, "id": raw.get("paperId") or raw.get("url", "")}


def rank_and_deduplicate(candidates: list[dict[str, Any]], query: str, query_variants: list[str] | None = None) -> list[dict[str, Any]]:
    query_text = " ".join(query_variants or [query])
    tokens = [token.lower() for token in re.findall(r"[\w-]+", query_text.lower()) if len(token) > 1]
    merged: dict[str, dict[str, Any]] = {}
    # Provider response order is nondeterministic. Prefer a usable OA copy and
    # use a stable representation to choose the primary record within a DOI.
    def stable_key(item):
        data={k:v for k,v in item.items() if k not in {'sources','rank_score','match_reasons'}}
        return (0 if item.get('access_status')=='open_access' and item.get('oa_url') else 1,json.dumps(data,ensure_ascii=False,sort_keys=True))
    ordered=sorted(candidates,key=stable_key)
    for candidate in ordered:
        text = f"{candidate.get('title','')} {candidate.get('abstract','')}".lower()
        matched = sorted({token for token in tokens if token in text})
        score = len(matched) / max(len(tokens), 1)
        item = dict(candidate)
        item["match_reasons"] = matched
        item["rank_score"] = round(score, 6)
        key = item["dedupe_key"]
        if key in merged:
            existing = merged[key]
            sources = existing.setdefault("sources", [])
            source_entries=[entry for entry in (item.get('sources') or []) if isinstance(entry,dict)]
            source_entries.append({"provider": item.get("provider"), "doi": item.get("doi"), "canonical_url": item.get("canonical_url")})
            for source_entry in source_entries:
                if source_entry not in sources:sources.append(source_entry)
            sources.sort(key=lambda entry:json.dumps(entry,ensure_ascii=False,sort_keys=True))
            if not existing.get("abstract") and item.get("abstract"):
                existing["abstract"] = item["abstract"]
            existing["rank_score"] = max(existing.get("rank_score", 0), item["rank_score"])
            existing["match_reasons"] = sorted(set(existing.get("match_reasons", [])) | set(matched))
        else:
            source_entries=[entry for entry in (item.get('sources') or []) if isinstance(entry,dict)]
            source_entry={"provider": item.get("provider"), "doi": item.get("doi"), "canonical_url": item.get("canonical_url")}
            if source_entry not in source_entries:source_entries.append(source_entry)
            item["sources"] = sorted(source_entries,key=lambda entry:json.dumps(entry,ensure_ascii=False,sort_keys=True))
            merged[key] = item
    return sorted(merged.values(), key=lambda item: (-float(item.get("rank_score", 0)), -(int(item.get("year") or 0) if str(item.get("year") or "").isdigit() else 0), item.get("dedupe_key", "")))


def search_summary(candidates: list[dict[str, Any]], query: str, filters: dict[str, Any], provider: str, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the compact, source-aware summary shown above candidate cards."""
    oa_count = sum(item.get("access_status") == "open_access" for item in candidates)
    metadata_count = sum(item.get("access_status") == "metadata_only" for item in candidates)
    top = candidates[:3]
    suggestions: list[str] = []
    if not filters.get("open_access"):
        suggestions.append("只看 OA 论文")
    if not filters.get("year_from"):
        suggestions.append("限定近五年")
    if candidates:
        suggestions.append("找相似综述")
    diagnostics = diagnostics or {}
    return {
        "query": normalize_query(query),
        "provider": provider,
        "rule_version": RULE_VERSION,
        "counts": {"total": len(candidates), "open_access": oa_count, "metadata_only": metadata_count},
        "plan": ["规范化关键词并扩展同义词", "并行查询多学术源" if "+" in provider else f"查询 {provider} 学术源", "按题名/摘要命中和年份排序", "按 DOI/URL 去重"],
        "expanded_queries": diagnostics.get("expanded_queries") or expand_query(query),
        "sources": diagnostics.get("sources") or [provider],
        "pages_fetched": diagnostics.get("pages_fetched", 0),
        "raw_count": diagnostics.get("raw_count", len(candidates)),
        "deduped_count": len(candidates),
        "source_failures": diagnostics.get("source_failures", []),
        "truncated": bool(diagnostics.get("truncated", False)),
        "top_matches": [{"title": item.get("title", ""), "doi": item.get("doi", ""), "match_reasons": item.get("match_reasons", [])} for item in top],
        "suggestions": suggestions,
    }


def validate_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    filters = dict(filters or {})
    normalized: dict[str, Any] = {}
    for key in ("year_from", "year_to"):
        if filters.get(key) not in (None, "", 0):
            try:
                value = int(filters[key])
            except (TypeError, ValueError) as exc:
                raise AcademicSearchError("invalid_filter", f"筛选条件 {key} 无效") from exc
            if value < 1800 or value > 2100:
                raise AcademicSearchError("invalid_filter", f"筛选条件 {key} 超出范围")
            normalized[key] = value
    if normalized.get("year_from") and normalized.get("year_to") and normalized["year_from"] > normalized["year_to"]:
        raise AcademicSearchError("invalid_filter", "起始年份不能晚于结束年份")
    if filters.get("open_access") is not None:
        normalized["open_access"] = bool(filters["open_access"])
    if filters.get("language"):
        normalized["language"] = str(filters["language"]).strip()[:40]
    return normalized


def retrieval_key(item: dict[str, Any] | str) -> str:
    """Return a stable DOI/title key for offline precision/recall evaluation."""
    if isinstance(item, str):
        value = item
    else:
        value = normalize_doi(item.get("doi")) or normalize_query(item.get("title", "")).lower()
    return re.sub(r"\s+", " ", str(value).strip().lower())


def retrieval_metrics(retrieved: list[dict[str, Any] | str], relevant: list[dict[str, Any] | str]) -> dict[str, Any]:
    """Compute deterministic set-based precision/recall/F1 for a gold set."""
    retrieved_keys = {retrieval_key(item) for item in retrieved if retrieval_key(item)}
    relevant_keys = {retrieval_key(item) for item in relevant if retrieval_key(item)}
    true_positive = len(retrieved_keys & relevant_keys)
    false_positive = len(retrieved_keys - relevant_keys)
    false_negative = len(relevant_keys - retrieved_keys)
    precision = true_positive / len(retrieved_keys) if retrieved_keys else 0.0
    recall = true_positive / len(relevant_keys) if relevant_keys else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"true_positive": true_positive, "false_positive": false_positive, "false_negative": false_negative, "retrieved": len(retrieved_keys), "relevant": len(relevant_keys), "precision": round(precision, 6), "recall": round(recall, 6), "f1": round(f1, 6)}


def query_snapshot(query: str, filters: dict[str, Any], provider: str) -> dict[str, Any]:
    return {"query": normalize_query(query), "filters": filters, "provider": provider, "rule_version": RULE_VERSION}
