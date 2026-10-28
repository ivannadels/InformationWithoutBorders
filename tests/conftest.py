import socket
import ssl              # noqa: F401  loaded before the network block below, because
import urllib.request   # noqa: F401  ssl subclasses socket.socket when first imported
from pathlib import Path

import pytest

from search_engine import CsvColumnMap, Indexer, SearchConfig, SearchEngine, load_corpus

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test runs with networking disabled, to prove the engine works offline."""
    def guard(*args, **kwargs):
        raise RuntimeError("Network access attempted during a test")
    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)


def make_config(index_path: Path, **overrides) -> SearchConfig:
    cfg = SearchConfig(index_path=index_path, **overrides)
    cfg.csv_columns = CsvColumnMap(keywords_columns=["topic"])
    return cfg


@pytest.fixture(scope="session")
def sample_index(tmp_path_factory):
    index_path = tmp_path_factory.mktemp("index") / "sample.sqlite"
    cfg = make_config(index_path)
    documents, report = load_corpus(SAMPLE_DIR / "documents", SAMPLE_DIR / "metadata.csv", cfg)
    stats = Indexer(cfg).build(documents)
    return {"path": index_path, "stats": stats, "report": report, "documents": documents}


@pytest.fixture
def engine(sample_index):
    eng = SearchEngine(config=make_config(sample_index["path"]))
    yield eng
    eng.close()
