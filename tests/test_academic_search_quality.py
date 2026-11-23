from paperpilot.services import academic_search


def test_weno_query_expansion_contains_standard_aliases():
    variants = academic_search.expand_query("WENO")
    assert variants[0] == "WENO"
    assert "WENO weighted essentially non-oscillatory" in variants
    assert "WENO WENO-JS" in variants
    assert "WENO WENO-AO" in variants
    assert len(variants) == len(set(variants)) == 5


def test_multisource_search_auto_paginates_with_diagnostics():
    class FakeProvider:
        def __init__(self, name):
            self.name = name
            self.calls = []

        def search(self, query, filters, cursor, limit):
            self.calls.append((query, cursor))
            if cursor is None:
                return {"next_cursor": "page-2", "results": [{"title": f"{self.name} {query}", "id": f"{self.name}-1", "publication_year": 2024}]}
            return {"next_cursor": None, "results": [{"title": "Shared WENO paper", "doi": "10.1000/shared", "id": f"{self.name}-2", "publication_year": 2023}]}

    first = FakeProvider("openalex")
    second = FakeProvider("crossref")
    provider = academic_search.MultiSourceProvider([first, second], max_pages=2)
    result = provider.search("WENO", {}, None, 2)
    assert result["next_cursor"] is None
    assert len(result["expanded_queries"]) == 5
    assert len(result["diagnostics"]) == 10
    assert all(item["pages"] == 2 for item in result["diagnostics"])
    assert len(result["results"]) == 20


def test_crossref_and_semantic_scholar_normalize_to_candidates():
    crossref = academic_search.normalize_candidate({
        "title": ["WENO schemes"], "DOI": "10.1000/weno", "URL": "https://doi.org/10.1000/weno",
        "container-title": ["Numerical Methods"], "published": {"date-parts": [[2020]]},
        "author": [{"given": "A", "family": "Author"}], "abstract": "<jats:p>WENO</jats:p>",
        "link": [{"URL": "https://example.org/weno.pdf", "content-type": "application/pdf"}],
        "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
    }, "crossref")
    assert crossref["doi"] == "10.1000/weno" and crossref["access_status"] == "open_access"

    semantic = academic_search.normalize_candidate({
        "title": "WENO-Z", "year": 2021, "venue": "Journal", "paperId": "S1",
        "authors": [{"name": "A Author"}], "externalIds": {"DOI": "10.1000/weno-z"},
        "openAccessPdf": {"url": "https://example.org/weno-z.pdf"}, "url": "https://example.org/weno-z",
    }, "semanticscholar")
    assert semantic["doi"] == "10.1000/weno-z" and semantic["access_status"] == "open_access"


def test_weno_retrieval_metrics_are_quantified():
    retrieved = [{"doi": "10.1000/a"}, {"title": "WENO-Z"}, {"doi": "10.1000/false-positive"}]
    gold = [{"doi": "10.1000/a"}, {"title": "WENO-Z"}, {"doi": "10.1000/missed"}]
    metrics = academic_search.retrieval_metrics(retrieved, gold)
    assert metrics == {
        "true_positive": 2, "false_positive": 1, "false_negative": 1,
        "retrieved": 3, "relevant": 3, "precision": round(2 / 3, 6),
        "recall": round(2 / 3, 6), "f1": round(2 / 3, 6),
    }
import json
from pathlib import Path

from paperpilot.services import academic_search


def test_weno_gold_fixture_precision_recall():
    root = Path(__file__).parent / "fixtures"
    gold = json.loads((root / "weno_gold.json").read_text(encoding="utf-8"))
    retrieved = json.loads((root / "weno_retrieved.json").read_text(encoding="utf-8"))
    metrics = academic_search.retrieval_metrics(retrieved, gold)
    assert metrics["retrieved"] == 6 and metrics["relevant"] == 5
    assert metrics["true_positive"] == 5
    assert metrics["precision"] == round(5 / 6, 6)
    assert metrics["recall"] == 1.0
