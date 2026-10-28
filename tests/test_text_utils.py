import textwrap

from search_engine.config import SearchConfig
from search_engine.text_utils import (analyze_query, estimate_similarity, fingerprint,
                                      normalize_text, split_into_passages, tokenize)

CFG = SearchConfig()


def test_normalisation_handles_case_punctuation_and_accents():
    assert normalize_text("  Œdème   SÉVÈRE!! ") == "œdeme severe!!"
    assert tokenize("Child's DIARRHOEA, 3 days; no-fever.") == ["child", "s", "diarrhoea", "3", "days", "no", "fever"]


def test_query_keeps_meaningful_terms_and_reports_ignored_words():
    a = analyze_query("Find guidelines for a child with severe diarrhoea", CFG)
    assert a.search_terms == ["child", "severe", "diarrhoea"]
    assert "find" in a.ignored_words and "guidelines" in a.ignored_words
    assert a.original_query == "Find guidelines for a child with severe diarrhoea"


def test_spelling_variants_are_expanded():
    a = analyze_query("diarrhea in kids", CFG)
    assert "diarrhoea" in a.expanded_terms["diarrhea"]
    assert '"diarrhoea"' in a.fts_query and '"children"' in a.fts_query


def test_negations_are_detected_and_reported_not_dropped():
    a = analyze_query("Child with fever but no rash, without vomiting.", CFG)
    assert a.negated_terms == ["rash", "vomiting"]
    assert "fever" not in a.negated_terms
    # negated words are still searched (keyword search cannot exclude safely)
    assert "rash" in a.search_terms


def test_fts_operator_words_cannot_break_the_query():
    a = analyze_query('NEAR AND "OR" NOT (fever* ^cough', CFG)
    assert all(part.startswith(('"', "(")) for part in a.fts_query.split(" OR "))


def test_numbers_are_kept():
    assert "5" in analyze_query("child aged 5", CFG).search_terms


def test_passages_respect_size_and_pages():
    cfg = SearchConfig(passage_target_words=20, passage_max_words=30, passage_overlap_words=5)
    page1 = "\n\n".join(" ".join(["alpha"] * 12) for _ in range(4))
    page2 = " ".join(["beta"] * 70)          # one long paragraph -> cut with overlap
    chunks = split_into_passages(page1 + "\f" + page2, cfg)
    assert all(len(c.text.split()) <= 30 for c in chunks)
    assert {c.page for c in chunks} == {1, 2}
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert sum(len(c.text.split()) for c in chunks if c.page == 2) >= 70


def test_fingerprint_detects_reformatted_copy_but_not_different_text():
    text = " ".join(f"word{i}" for i in range(400))
    reflowed = "\n".join(textwrap.wrap(text, 60))  # same words, different line breaks
    other = " ".join(f"other{i}" for i in range(400))
    a, b, c = (fingerprint(t, 128) for t in (text, reflowed, other))
    assert estimate_similarity(a, b, 128) > 0.95
    assert estimate_similarity(a, c, 128) < 0.05
