"""
Tests for saved web pages. The pages are small synthetic copies of the
patterns found in the real MSF HTML files (menus inside <main>, React
content hidden until JavaScript runs, repeated site footers, homepages...).
"""

import os
from pathlib import Path

import pytest

from search_engine import Indexer, SearchConfig, SearchEngine, load_corpus
from search_engine.html_extract import extract_html_page
from search_engine.text_utils import split_into_passages

GUIDELINE_PAGE = """<!DOCTYPE html><html lang="en"><head>
<title>Early postpartum haemorrhage | MSF Medical Guidelines</title>
<link rel="canonical" href="https://guides.example.org/en/obstetrics/early-pph.html">
<meta property="og:description" content="How to manage bleeding after birth.">
<script type="application/ld+json">{"@graph":[{"@type":"WebPage","datePublished":"2024-04-01T00:00:00Z"}]}</script>
<script>trackEverything()</script><style>.x{color:red}</style>
</head><body>
<header>Site header MSF.org Donate</header>
<main>
  <aside class="layout-sidebar"><h3>All guidelines</h3><ul><li>Essential drugs</li><li>Cholera</li></ul></aside>
  <div class="block-system-breadcrumb-block"><nav><ul><li>Home</li><li>Essential obstetric care</li></ul></nav></div>
  <div class="language-popup"><div>Select language:</div><div>Français</div></div>
  <h1 class="page-title">8.2 Early postpartum haemorrhage</h1>
  <div>Permalink</div>
  <article>
    <p>Last updated: April 2024</p>
    <h2>8.2.1 Management during the first 30 minutes</h2>
    <p>Call for help. Massage the uterus and start an <strong>IV</strong> line.</p>
    <h3>Uterotonic drugs</h3>
    <table><thead><tr><th>Drug</th><th>Route</th></tr></thead>
      <tbody><tr><td>Oxytocin</td><td>IV or IM</td></tr><tr><td>Misoprostol</td><td>oral</td></tr></tbody></table>
    <p>Tags: <a href="#">postpartum</a><a href="#">haemorrhage</a></p>
  </article>
  <div class="social-share">Share on Facebook</div>
  <nav class="book-navigation-pager">Previous chapter | Next chapter</nav>
</main>
<footer>Copyright MSF. Privacy policy.</footer>
</body></html>"""

REACT_PAGE = """<!DOCTYPE html><html><head></head><body class="page-wrapper">
<div hidden=""><!--$--><!--/$--></div>
<main><div class="loader"><div class="loader-content"></div></div></main>
<div hidden id="S:0"><title>Ebola in pregnancy | Journal Article | Science Portal</title>
<meta name="citation_publication_date" content="2023-06-07"/>
<h1>Ebola in pregnancy</h1><h5>Abstract</h5>
<p>Pregnant women with Ebola virus disease had high mortality and fetal loss. This is a review of
the cases seen in the treatment centre, and of the findings in the placenta and in the fetus, with
the implications for the care of pregnant women and for the protection of staff in the delivery room.</p></div>
</body></html>"""


def test_guideline_page_keeps_content_and_drops_furniture():
    page = extract_html_page(GUIDELINE_PAGE)
    text = page.text
    assert page.title == "8.2 Early postpartum haemorrhage"
    # structure kept
    assert "## 8.2.1 Management during the first 30 minutes" in text
    assert "### Uterotonic drugs" in text
    assert "Drug | Route\nOxytocin | IV or IM\nMisoprostol | oral" in text
    assert "postpartum haemorrhage" in text            # links not glued together
    # furniture removed
    for junk in ("All guidelines", "Select language", "Permalink", "Share on Facebook",
                 "Next chapter", "trackEverything", "color:red", "Donate", "Privacy policy"):
        assert junk not in text, junk
    # metadata from the page
    md = page.metadata
    assert md["url"].endswith("early-pph.html") and md["site"] == "guides.example.org"
    assert md["language"] == "en" and md["last_updated"] == "April 2024"
    assert md["published"].startswith("2024-04-01")
    assert md["breadcrumb"] == "Essential obstetric care"


def test_react_page_content_hidden_until_javascript_is_kept():
    page = extract_html_page(REACT_PAGE)
    assert page.title == "Ebola in pregnancy"
    assert "high mortality" in page.text
    assert page.metadata["published"] == "2023-06-07"
    assert page.metadata["site"] == "Science Portal"     # no URL: site name from the title
    assert page.metadata["language"] == "en"              # guessed: page declares none


def test_passages_remember_their_section():
    page = extract_html_page(GUIDELINE_PAGE)
    cfg = SearchConfig(passage_target_words=8, passage_max_words=30)
    chunks = split_into_passages(page.text, cfg)
    table_chunk = next(c for c in chunks if "Oxytocin" in c.text)
    assert table_chunk.section == "8.2.1 Management during the first 30 minutes > Uterotonic drugs"


def test_long_tables_are_split_between_rows_with_header_repeated():
    rows = "\n".join(f"Drug{i} | {i} mg | oral" for i in range(60))
    chunks = split_into_passages("## Dosage\n\nName | Dose | Route\n" + rows, SearchConfig(passage_max_words=40))
    table_chunks = [c for c in chunks if "Drug" in c.text]
    assert len(table_chunks) > 1
    for c in table_chunks:
        lines = [l for l in c.text.split("\n") if l and not l.startswith("#")]
        assert lines[0] == "Name | Dose | Route"
        assert all(l.count(" | ") == 2 for l in lines)


def _site_page(slug, body, lang="en", title=None):
    return (f'<html lang="{lang}"><head><title>{title or slug} | Site</title>'
            f'<link rel="canonical" href="https://news.example.org/{slug}"></head><body><main>'
            f"<h1>{title or slug}</h1>{body}"
            "<div>Read our annual reports and financial statements here</div>"
            "<div>Follow MSF on social media for the latest updates</div>"
            "</main></body></html>")


@pytest.fixture
def site_dir(tmp_path):
    d = tmp_path / "pages"
    d.mkdir()
    (d / "a.html").write_text(_site_page("cholera", "<p>Cholera outbreak response with oral rehydration.</p>"))
    (d / "b.html").write_text(_site_page("measles", "<p>Measles vaccination campaign in the camp.</p>"))
    (d / "c.html").write_text(_site_page("tb", "<p>Tuberculosis treatment for adults and children.</p>"))
    refs = "<ul>" + "".join(f"<li>Smith J et al. Malaria in children study {i}. Lancet 2020.</li>" for i in range(40)) + "</ul>"
    (d / "refs_en.html").write_text(_site_page("refs-en", refs, "en", "Main references"))
    (d / "refs_fr.html").write_text(_site_page("refs-fr", refs, "fr", "Principales références"))
    (d / "malaria.html").write_text(_site_page("malaria", "<p>Malaria in children: confirm with a rapid test, then treat.</p>"))
    (d / "home.html").write_text('<html><head><link rel="canonical" href="https://news.example.org/"></head>'
                                 "<body><main><h2>Latest news</h2><p>Many links to many stories here.</p></main></body></html>")
    (d / "notes.pdf").write_bytes(b"%PDF-1.4 not used")
    return d


def test_site_boilerplate_homepages_and_formats(site_dir):
    docs, report = load_corpus(site_dir, extensions=["html"])
    by_id = {d.doc_id: d for d in docs}
    assert "notes.pdf" not in by_id                              # --formats html
    assert "home.html" not in by_id                              # site homepage skipped...
    assert any("home page" in w for w in report.warnings)        # ...with a warning
    for d in docs:                                               # repeated footer lines removed
        assert "annual reports" not in d.text and "social media" not in d.text
    assert "oral rehydration" in by_id["a.html"].text           # real content kept


def test_translations_are_not_merged_and_reference_lists_rank_low(site_dir, tmp_path):
    docs, _ = load_corpus(site_dir, extensions=["html"])
    cfg = SearchConfig(index_path=tmp_path / "i.sqlite")
    stats = Indexer(cfg).build(docs)
    assert stats.duplicate_groups == 0                           # en + fr reference lists stay separate
    with SearchEngine(config=cfg) as engine:
        results = engine.search("malaria in children").results
    assert results[0].doc_id == "malaria.html"                   # content beats the bibliography


@pytest.mark.skipif(not os.environ.get("MSF_DATA_DIR"), reason="set MSF_DATA_DIR to the folder of real HTML files")
def test_real_msf_html_collection(tmp_path):
    """Smoke test on the real data: MSF_DATA_DIR=".../D4GC data/Original" pytest tests/test_html.py"""
    docs, report = load_corpus(Path(os.environ["MSF_DATA_DIR"]), extensions=["html"])
    assert len(docs) > 300
    assert all(d.text.strip() for d in docs)
    cfg = SearchConfig(index_path=tmp_path / "real.sqlite")
    Indexer(cfg).build(docs)
    with SearchEngine(config=cfg) as engine:
        top = engine.search("Adult with a cough for several weeks, fever and weight loss").results[0]
        assert "tuberculosis" in top.title.lower()
        assert engine.search("How to repair a motorbike engine").results == []
