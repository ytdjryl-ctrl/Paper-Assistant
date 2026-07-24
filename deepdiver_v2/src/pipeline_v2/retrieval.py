from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .models import LiteratureTask, ReferenceRecord
from .literature import deduplicate_references, is_countable_reference


logger = logging.getLogger(__name__)


def _year(value: str) -> str:
    match = re.search(r"\b((?:19|20)\d{2})\b", value or "")
    return match.group(1) if match else ""


def _doi(value: str) -> str:
    match = re.search(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", value or "", flags=re.IGNORECASE)
    return match.group(1).rstrip(".,;") if match else ""


def _organic_results(payload: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (query, result) pairs from the MCP search tool's supported response shapes."""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "")
        result_block = item.get("results", item)
        if isinstance(result_block, dict):
            organic = result_block.get("organic") or result_block.get("search_results") or result_block.get("results") or []
        elif isinstance(result_block, list):
            organic = result_block
        else:
            organic = []
        for result in organic:
            if isinstance(result, dict):
                yield query, result


def _safe_name(title: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_")[:80]
    return f"{index:03d}_{stem or 'source'}.md"


def _save_reference_markdown(workspace_path: Path, references: Sequence[ReferenceRecord]) -> None:
    output_dir = workspace_path / "research" / "literature_online"
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, reference in enumerate(references, 1):
        path = output_dir / _safe_name(reference.title, index)
        lines = [
            f"# {reference.title}", "",
            f"**Source**: {reference.venue}",
            f"**Authors**: {reference.authors}",
            f"**Year**: {reference.year}",
            f"**DOI**: {reference.doi}",
            f"**URL**: {reference.url}",
            f"**Retrieved via**: {reference.source_type}",
            f"**Query**: {reference.query}", "",
            "## Abstract / Evidence", "",
            (reference.abstract or reference.evidence or "No abstract evidence was supplied.")[:12000], "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        reference.source_path = path.relative_to(workspace_path).as_posix()


def retrieve_online_references(
    tasks: Sequence[LiteratureTask],
    tools: Any,
    workspace_path: Path,
    enabled: bool = True,
) -> Tuple[List[ReferenceRecord], List[str]]:
    if not enabled:
        return [], []

    warnings: List[str] = []
    queries = []
    for task in tasks:
        for query in task.queries:
            if query and query not in queries:
                queries.append(query)
    if not queries:
        return [], ["Online retrieval was enabled but no search queries were generated."]

    minimum_references = max(0, int(os.getenv("V2_MIN_VALID_REFERENCES", "30")))
    max_results = max(1, min(int(os.getenv("V2_SEARCH_RESULTS_PER_QUERY", "8")), 15))
    logger.info("[LiteratureSearch] 开始检索：%s 个查询，每个来源最多 %s 条", len(queries), max_results)
    raw_data: List[Dict[str, Any]] = []
    academic_enabled = os.getenv("V2_ACADEMIC_SEARCH", "true").lower() == "true"
    if academic_enabled and hasattr(tools, "academic_search"):
        try:
            academic_result = tools.academic_search(
                queries=queries,
                max_results_per_query=max_results,
                # Publisher APIs and Crossref are more reliable with modest
                # concurrency; quality comes from iterative queries, not bursts.
                max_workers=min(3, max(1, len(queries))),
            )
            academic_payload = getattr(academic_result, "data", {}) or {}
            if isinstance(academic_payload, dict):
                raw_data.extend(academic_payload.get("results") or [])
                warnings.extend(str(value) for value in academic_payload.get("warnings") or [])
            if not getattr(academic_result, "success", False):
                warnings.append(f"Academic search failed: {getattr(academic_result, 'error', 'unknown error')}")
        except Exception as exc:
            warnings.append(f"Academic search failed before returning results: {exc}")

    academic_success = any(block.get("success") and block.get("results") for block in raw_data if isinstance(block, dict))
    academic_candidate_count = sum(1 for _query, _item in _organic_results(raw_data))
    use_web_fallback = (
        not academic_success
        or academic_candidate_count < minimum_references
        or os.getenv("V2_ALWAYS_WEB_SEARCH", "false").lower() == "true"
    )
    if use_web_fallback:
        try:
            search_result = tools.batch_web_search(
                queries=queries,
                max_results_per_query=max_results,
                max_workers=min(3, len(queries)),
            )
            if getattr(search_result, "success", False):
                web_payload = getattr(search_result, "data", [])
                raw_data.extend(web_payload if isinstance(web_payload, list) else [web_payload])
            else:
                warnings.append(f"Web search fallback failed: {getattr(search_result, 'error', 'unknown error')}")
        except Exception as exc:
            warnings.append(f"Web search fallback failed before returning results: {exc}")

    research_dir = workspace_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    (research_dir / "search_results_raw.json").write_text(
        json.dumps(raw_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    for block in raw_data if isinstance(raw_data, list) else [raw_data]:
        if isinstance(block, dict) and block.get("success") is False:
            warnings.append(
                f"Search query failed ({block.get('query', 'unknown query')}): "
                f"{block.get('error', 'unknown error')}"
            )

    references: List[ReferenceRecord] = []
    for query, item in _organic_results(raw_data):
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("link") or item.get("url") or "").strip()
        snippet = str(item.get("snippet") or item.get("description") or item.get("content") or "").strip()
        date = str(item.get("date") or item.get("published") or item.get("publication_date") or "")
        raw_authors = item.get("authors") or item.get("author") or ""
        if isinstance(raw_authors, list):
            authors = ", ".join(
                str(author.get("name") if isinstance(author, dict) else author) for author in raw_authors
            )
        else:
            authors = str(raw_authors)
        venue = str(item.get("journal") or item.get("publication") or "")
        explicit_doi = str(item.get("doi") or "")
        source_type = str(item.get("source") or "web_search")
        if not title or not url:
            continue
        references.append(
            ReferenceRecord(
                title=title[:500],
                authors=authors[:1000],
                year=_year(date + " " + snippet),
                venue=venue[:500],
                doi=_doi(explicit_doi + " " + url + " " + snippet),
                url=url,
                evidence=snippet[:3000],
                abstract=snippet[:3000],
                query=query,
                source_type=source_type,
            )
        )

    valid_unique = [item for item in deduplicate_references(references) if is_countable_reference(item)]
    fallback_target = minimum_references or 1
    if not valid_unique or len(valid_unique) < minimum_references or os.getenv("V2_ALWAYS_SEARCH_ARXIV", "false").lower() == "true":
        for task in tasks:
            for fallback_query in task.queries:
                if len([item for item in deduplicate_references(references) if is_countable_reference(item)]) >= fallback_target:
                    break
                try:
                    arxiv_result = tools.arxiv_search(
                        query=fallback_query,
                        max_results=max(1, min(max_results, task.max_sources)),
                    )
                    if not getattr(arxiv_result, "success", False):
                        warnings.append(
                            f"arXiv fallback failed for {task.topic}: {getattr(arxiv_result, 'error', 'unknown error')}"
                        )
                        continue
                    papers = (getattr(arxiv_result, "data", {}) or {}).get("papers", [])
                    for paper in papers:
                        if not isinstance(paper, dict) or not paper.get("title"):
                            continue
                        abstract = str(paper.get("abstract") or "")
                        references.append(
                            ReferenceRecord(
                                title=str(paper.get("title"))[:500],
                                authors=str(paper.get("authors") or "")[:1000],
                                year=_year(str(paper.get("published_date") or "")),
                                venue="arXiv",
                                doi=str(paper.get("doi") or ""),
                                url=str(paper.get("url") or paper.get("pdf_url") or ""),
                                evidence=abstract[:8000],
                                abstract=abstract[:8000],
                                query=fallback_query,
                                source_type="arxiv",
                            )
                        )
                except Exception as exc:
                    warnings.append(f"arXiv fallback failed for {task.topic}: {exc}")

    crawl_limit = max(0, int(os.getenv("V2_CRAWL_MAX_SOURCES", "6")))
    crawl_candidates = []
    seen_urls = set()
    if crawl_limit > 0:
        for reference in references:
            if reference.url in seen_urls:
                continue
            seen_urls.add(reference.url)
            crawl_candidates.append(reference)
            if len(crawl_candidates) >= crawl_limit:
                break

    if crawl_candidates:
        documents = []
        for index, reference in enumerate(crawl_candidates, 1):
            relative_path = f"research/retrieved/{_safe_name(reference.title, index)}"
            reference.source_path = relative_path
            documents.append(
                {
                    "url": reference.url,
                    "file_path": relative_path,
                    "title": reference.title,
                    "time": reference.year,
                }
            )
        try:
            crawl_result = tools.url_crawler(
                documents=documents,
                max_tokens_per_url=int(os.getenv("V2_CRAWL_MAX_TOKENS", "12000")),
                include_metadata=True,
                max_workers=min(4, len(documents)),
            )
            if not getattr(crawl_result, "success", False):
                warnings.append(f"URL crawling returned a partial failure: {getattr(crawl_result, 'error', 'unknown error')}")
        except Exception as exc:
            warnings.append(f"URL crawling failed; search metadata will still be used: {exc}")

        for reference in crawl_candidates:
            saved_path = workspace_path / reference.source_path
            if saved_path.exists():
                text = saved_path.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    reference.evidence = text[:8000]
                    reference.abstract = reference.abstract or text[:3000]
                    reference.source_type = "web_crawled"

    references = [item for item in deduplicate_references(references) if is_countable_reference(item)]
    if not references:
        warnings.append("Online search completed but returned no references with both title and URL.")
    elif minimum_references and len(references) < minimum_references:
        warnings.append(
            f"有效去重文献仅 {len(references)} 篇，未达到最低要求 {minimum_references} 篇；"
            "Planner 必须针对未覆盖的论点继续补充检索。"
        )
    _save_reference_markdown(workspace_path, references)
    logger.info("[LiteratureSearch] 完成：保存 %s 篇逐篇 Markdown 文献记录，警告 %s 条", len(references), len(warnings))
    return references, warnings
