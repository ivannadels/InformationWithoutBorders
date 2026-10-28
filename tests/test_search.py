import json
import statistics
import threading
import time

import pytest

import search_engine
from search_engine import IndexNotFoundError, SearchEngine
from search_engine.evaluate import (evaluate, load_labelled_queries, precision_at_k,
                                    recall_at_k, reciprocal_rank)

from .conftest import SAMPLE_DIR, make_config

EXAMPLE = ("A doctor in a remote clinic is treating a child with severe diarrhoea and signs of "
           "dehydration, but the clinic has limited medical supplies. Find relevant clinical "
           "guidelines or protocols.")
DIARRHOEA_GROUP = {"diarrhoea_children.txt", "diarrhoea_children.html",
                   "acute_diarrhoea_copy_from_field_drive.txt"}


def test_relevant_document_ranks_first_for_long_description(engine):
    response = engine.search(EXAMPLE)
    assert response.results, response.message
    top = response.results[0]
    assert top.doc_id in DIARRHOEA_GROUP
    assert top.rank == 1 and top.relevance_score > 0
    assert "diarrhoea" in top.matched_terms and "dehydration" in top.matched_terms
    assert top.metadata.get("version") == "2.1"            # metadata preserved
    assert "**" in top.snippet                               # matched words highlighted
    assert top.passages and top.passages[0].text


def test_relevant_documents_rank_above_irrelevant(engine):
    ranked = [r.doc_id for r in engine.search(EXAMPLE, top_k=10).results]
    for irrelevant in ("hypertension_adults.txt", "burns_first_aid.txt", "psychological_first_aid.txt"):
        assert irrelevant not in ranked


def test_duplicates_are_collapsed_into_one_result(engine):
    response = engine.search("diarrhoea dehydration children ORS zinc", top_k=10)
    ids = [r.doc_id for r in response.results]
    assert len(set(ids) & DIARRHOEA_GROUP) == 1
    others = {o["doc_id"] for o in response.results[0].other_formats}
    assert others == DIARRHOEA_GROUP - {response.results[0].doc_id}


def test_top_k_is_a_maximum_not_a_quota(engine):
    assert len(engine.search("treatment", top_k=2).results) <= 2
    few = engine.search("snakebite antivenom")
    assert 1 <= len(few.results) < 5                         # does not pad to 5
    assert len(engine.search("severe children fever dehydration", top_k=999).results) <= engine.config.max_top_k


def test_off_topic_query_returns_nothing(engine):
    response = engine.search("how to repair a motorbike engine")
    assert response.results == [] and response.low_confidence
    assert "No document" in response.message


@pytest.mark.parametrize("query", ["", "   ", None, "the and of with", "?!?"])
def test_empty_or_meaningless_queries_are_handled(engine, query):
    response = engine.search(query)
    assert response.results == [] and response.low_confidence and response.message


def test_negation_is_reported_to_the_user(engine):
    response = engine.search("child with diarrhoea but no fever")
    assert response.analysis.negated_terms == ["fever"]
    assert "negation" in response.message


def test_metadata_filters(engine):
    assert engine.search("diarrhoea", filters={"file_format": "html"}).results[0].file_format == "html"
    assert engine.search("diarrhoea", filters={"language": "French"}).results == []


def test_results_are_json_serialisable(engine):
    data = json.loads(json.dumps(engine.search(EXAMPLE).to_dict()))
    assert {"query", "results", "low_confidence", "message", "disclaimer"} <= set(data)
    assert {"rank", "doc_id", "title", "source_path", "snippet", "relevance_score",
            "metadata", "passages"} <= set(data["results"][0])


def test_get_document_and_stats(engine):
    doc = engine.get_document("cholera_outbreak.txt")
    assert doc["title"] == "Cholera Outbreak Response" and doc["passages"]
    assert engine.get_document("does-not-exist") is None
    assert engine.stats()["documents"] == 15


def test_missing_index_gives_helpful_error(tmp_path):
    with pytest.raises(IndexNotFoundError, match="Build it first"):
        SearchEngine(tmp_path / "nothing.sqlite")


def test_module_level_interface(sample_index):
    search_engine.configure(config=make_config(sample_index["path"]))
    response = search_engine.search(query="A child has severe diarrhoea and dehydration", top_k=5)
    assert response.results[0].doc_id in DIARRHOEA_GROUP


def test_search_is_fast(engine):
    queries = [q["query"] for q in load_labelled_queries(SAMPLE_DIR / "eval_queries.json")]
    timings = []
    for _ in range(5):
        for q in queries:
            t0 = time.perf_counter()
            engine.search(q)
            timings.append((time.perf_counter() - t0) * 1000)
    # Generous bound so the test is stable on slow laptops; real numbers are printed by evaluate.
    assert statistics.median(timings) < 50


def test_engine_can_be_shared_between_threads(engine):
    errors = []

    def worker():
        try:
            for _ in range(20):
                assert engine.search(EXAMPLE).results
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# ---- evaluation -------------------------------------------------------------

def test_metric_functions():
    assert precision_at_k(["a", "x"], {"a"}, 5) == 0.5
    assert precision_at_k([], {"a"}, 5) == 0.0
    assert recall_at_k(["a", "x"], {"a", "b"}, 5) == 0.5
    assert reciprocal_rank(["x", "a"], {"a"}) == 0.5


def test_evaluation_on_labelled_sample(engine):
    report = evaluate(engine, load_labelled_queries(SAMPLE_DIR / "eval_queries.json"), k=5)
    assert report["duplicate_results"] == 0
    # whenever something is returned for an answerable query, the first result is relevant
    for row in report["per_query"]:
        if "rr" in row and row["n_returned"]:
            assert row["rr"] == 1.0, row
    assert report["mean_results_returned"] < 5   # fewer, more useful results


# ---- semantic extension point ---------------------------------------------------

class FakeEmbeddingRetriever:
    """Stands in for a local embedding model: knows 'violence' relates to psychological first aid."""
    def search_documents(self, query, limit):
        if "violence" in query:
            return [("psychological_first_aid.txt", 0.71), ("hypertension_adults.txt", 0.10)]
        return []


def test_semantic_retriever_can_be_plugged_in(sample_index):
    query = "A woman is very distressed after violence, crying and unable to cope"
    with SearchEngine(config=make_config(sample_index["path"])) as plain:
        assert "psychological_first_aid.txt" not in [r.doc_id for r in plain.search(query).results]
    with SearchEngine(config=make_config(sample_index["path"]),
                      semantic_retriever=FakeEmbeddingRetriever()) as hybrid:
        ids = [r.doc_id for r in hybrid.search(query).results]
    assert ids[0] == "psychological_first_aid.txt"
    assert "hypertension_adults.txt" not in ids            # below similarity threshold


def test_weaker_matches_can_be_requested(engine):
    query = "Heavy bleeding after delivery, the mother is losing a lot of blood"
    strict = engine.search(query)
    assert strict.results == [] and "weaker match" in strict.message
    relaxed = engine.search(query, min_coverage=0.2)
    assert relaxed.results[0].doc_id == "postpartum_haemorrhage.txt"
    assert relaxed.results[0].match_strength == "weak"
