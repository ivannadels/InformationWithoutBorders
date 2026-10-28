"""
Command-line tool.

  python -m search_engine iwb      --db search_index.db "child with severe diarrhoea"   (team database)
  python -m search_engine index    --docs tests/sample_data/documents --csv tests/sample_data/metadata.csv
  python -m search_engine search   "child with severe diarrhoea and dehydration"
  python -m search_engine evaluate --queries tests/sample_data/eval_queries.json
  python -m search_engine stats

Every command accepts --index PATH (default: folder_search_index.db, or the
MSF_SEARCH_INDEX environment variable).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import INDEX_ENV_VAR
from .config import CsvColumnMap, SearchConfig
from .evaluate import evaluate, load_labelled_queries, print_report
from .indexer import Indexer
from .loaders import load_corpus, load_jsonl
from .search import IndexNotFoundError, SearchEngine


def _config(args) -> SearchConfig:
    cfg = SearchConfig(index_path=Path(args.index or os.environ.get(INDEX_ENV_VAR) or "folder_search_index.db"))
    if getattr(args, "csv_file_column", None) or getattr(args, "csv_keywords", None):
        cfg.csv_columns = CsvColumnMap(file_column=args.csv_file_column,
                                       keywords_columns=args.csv_keywords or [])
    return cfg


def cmd_index(args) -> int:
    cfg = _config(args)
    documents, warnings = [], []
    if args.docs:
        docs, report = load_corpus(Path(args.docs), Path(args.csv) if args.csv else None, cfg,
                                   extensions=args.formats)
        documents += docs
        warnings += report.warnings
    if args.jsonl:
        docs, report = load_jsonl(Path(args.jsonl))
        documents += docs
        warnings += report.warnings
    if not documents:
        print("No documents found. Use --docs FOLDER and/or --jsonl FILE.", file=sys.stderr)
        return 1
    stats = Indexer(cfg).build(documents)
    print(f"Indexed {stats.documents_indexed} documents ({stats.passages_indexed} passages) "
          f"in {stats.seconds}s -> {stats.index_path}")
    print(f"Files grouped as duplicates of another format: {stats.duplicate_groups} group(s)")
    for s in stats.skipped:
        print(f"  skipped {s['doc_id']}: {s['reason']}")
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for w in warnings[:60]:
            print("  -", w)
    return 0


def cmd_search(args) -> int:
    cfg = _config(args)
    with SearchEngine(config=cfg) as engine:
        response = engine.search(args.query, top_k=args.top_k, min_coverage=args.min_coverage)
    if args.json:
        print(json.dumps(response.to_dict(), indent=2, ensure_ascii=False))
        return 0
    print(f"\n{response.message}  ({response.elapsed_ms} ms)")
    if response.analysis:
        print(f"Searched for: {', '.join(response.analysis.search_terms)}")
    for r in response.results:
        print(f"\n#{r.rank}  {r.title}   [{r.match_strength} match, coverage {r.match_coverage:.0%}, "
              f"score {r.relevance_score}]")
        print(f"    file: {r.source_path}")
        if r.other_formats:
            print("    also as: " + ", ".join(o["source_path"] for o in r.other_formats))
        if r.metadata:
            print("    metadata: " + "; ".join(f"{k}={v}" for k, v in r.metadata.items()))
        page = f" (page {r.passages[0].page})" if r.passages and r.passages[0].page else ""
        if r.passages and r.passages[0].section:
            page += f" [section: {r.passages[0].section}]"
        print(f"    excerpt{page}: {r.snippet}")
        if r.missing_terms:
            print(f"    not mentioned: {', '.join(r.missing_terms)}")
    print(f"\n{response.disclaimer}")
    return 0


def cmd_evaluate(args) -> int:
    cfg = _config(args)
    with SearchEngine(config=cfg) as engine:
        report = evaluate(engine, load_labelled_queries(Path(args.queries)), k=args.k)
    print_report(report)
    return 0


def cmd_iwb(args) -> int:
    """Search the team's InformationWithoutBorders database (search_index.db)."""
    from .iwb import IWBSearch
    engine = IWBSearch(args.db, index_path=args.index, files_dir=args.files_dir, check_every=0)
    if engine.last_report:
        print(f"Index (re)built from {args.db}: {engine.last_stats.documents_indexed} documents")
        for w in engine.last_report.warnings[:10]:
            print("  -", w)
    if not args.query:
        return 0
    args.index = str(engine.config.index_path)
    engine.close()
    return cmd_search(args)


def cmd_stats(args) -> int:
    with SearchEngine(config=_config(args)) as engine:
        print(json.dumps(engine.stats(), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m search_engine", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="build the search index")
    p.add_argument("--docs", help="folder with .pdf/.html/.txt files")
    p.add_argument("--csv", help="metadata CSV (optional)")
    p.add_argument("--formats", nargs="*", help="only index these formats, e.g. --formats html htm")
    p.add_argument("--jsonl", help="pre-extracted documents, one JSON per line (optional)")
    p.add_argument("--csv-file-column", help="CSV column with the file name (auto-detected if omitted)")
    p.add_argument("--csv-keywords", nargs="*", help="CSV columns to make searchable (e.g. topic summary)")
    p.add_argument("--index")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="run one query")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    p.add_argument("--min-coverage", type=float, default=None,
                   help="show weaker matches, e.g. 0.2 (default from config: 0.4)")
    p.add_argument("--index")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("evaluate", help="Precision@k / Recall@k on labelled queries")
    p.add_argument("--queries", required=True)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--index")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("iwb", help="index and search the InformationWithoutBorders database")
    p.add_argument("query", nargs="?", help="optional: a query to run after updating the index")
    p.add_argument("--db", required=True, help="their search_index.db")
    p.add_argument("--files-dir", help="optional folder with the original <doc_id>.html files")
    p.add_argument("--index", help="where to keep this engine's index (default: next to the database)")
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--min-coverage", type=float, default=None)
    p.set_defaults(func=cmd_iwb)

    p = sub.add_parser("stats", help="show index information")
    p.add_argument("--index")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except (IndexNotFoundError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
