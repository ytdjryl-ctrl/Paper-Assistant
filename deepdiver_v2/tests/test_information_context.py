import gzip
import json
from pathlib import Path
from types import SimpleNamespace

from src.agents.information_context import LiteratureContextLedger
from src.pipeline_v2.literature import load_existing_references


def _ledger(tmp_path: Path):
    agent = SimpleNamespace(mcp_tools=SimpleNamespace(workspace_path=tmp_path))
    task = SimpleNamespace(task_content="Collect at least 10 papers on wavelet pooling")
    return LiteratureContextLedger(agent, task, "objective")


def test_literature_results_are_archived_and_compacted(tmp_path):
    ledger = _ledger(tmp_path)
    result = {
        "success": True,
        "data": {
            "papers": [{
                "title": "Adaptive Wavelet Pooling for Convolutional Neural Networks",
                "authors": ["A. Researcher", "B. Researcher"],
                "published_date": "2021-05-04",
                "source": "arxiv",
                "doi": "10.1000/example",
                "url": "https://example.test/paper",
                "abstract": "A" * 5000,
            }]
        },
    }

    compact = ledger.capture("arxiv_search", {"query": "wavelet pooling"}, result)

    assert compact["total_unique_references"] == 1
    assert compact["new_unique_references"][0]["id"] == "L001"
    assert len(json.dumps(compact)) < len(json.dumps(result))
    archive = tmp_path / compact["literature_batch_saved"]
    assert archive.exists()
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        archived = json.load(stream)
    assert archived["result"]["data"]["papers"][0]["abstract"] == "A" * 5000
    assert ledger.index_path.exists()


def test_duplicate_doi_keeps_one_stable_identifier(tmp_path):
    ledger = _ledger(tmp_path)
    first = {"data": [{"title": "Paper One", "doi": "10.1000/same", "year": "2020"}]}
    second = {"data": [{
        "title": "Paper One Extended Metadata", "doi": "10.1000/same",
        "authors": "Author Name", "journal": "Journal Name",
    }]}

    ledger.capture("academic_search", {"queries": ["first"]}, first)
    compact = ledger.capture("academic_search", {"queries": ["second"]}, second)

    assert compact["total_unique_references"] == 1
    assert ledger.records[0]["id"] == "L001"
    assert ledger.records[0]["authors"] == "Author Name"


def test_payload_keeps_user_directives_but_replaces_old_search_history(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.capture(
        "academic_search",
        {"queries": ["wavelet"]},
        {"data": [{"title": "A Valid Paper Title", "doi": "10.1000/a", "year": "2022"}]},
    )
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "old reasoning " * 1000},
        {"role": "tool", "content": "old full result " * 1000},
        {"role": "user", "content": "用户指导：重点搜索小波池化"},
    ]
    for index in range(8):
        history.append({"role": "assistant", "content": f"recent {index}"})

    payload = ledger.payload_messages(history)
    text = "\n".join(str(message.get("content")) for message in payload)

    assert "用户指导：重点搜索小波池化" in text
    assert "Durable literature ledger" in text
    assert "old full result " * 100 not in text


def test_final_reference_refresh_reads_the_durable_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.capture(
        "academic_search",
        {"queries": ["wavelet pooling"]},
        {"data": [{
            "title": "Adaptive Wavelet Pooling for Convolutional Neural Networks",
            "authors": "A. Researcher",
            "year": "2021",
            "journal": "Machine Learning Journal",
            "doi": "10.1000/wavelet",
            "abstract": "Wavelet pooling preserves multiscale information.",
        }]},
    )

    references = load_existing_references(tmp_path, query="wavelet pooling")

    assert len(references) == 1
    assert references[0].doi == "10.1000/wavelet"
    assert "literature_batches" in references[0].source_path
