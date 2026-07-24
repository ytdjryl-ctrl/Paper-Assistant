"""Durable, compact context for long-running literature-search agent loops."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


LITERATURE_TOOLS = {
    "academic_search",
    "batch_web_search",
    "search_pubmed_key_words",
    "search_pubmed_advanced",
    "get_pubmed_article",
    "get_sciencedirect_article",
    "arxiv_search",
    "arxiv_read_paper",
    "medrxiv_search",
    "medrxiv_read_paper",
    "download_files",
    "url_crawler",
}


def _one_line(value: Any, limit: int = 500) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _normal_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (value or "").lower())


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_dicts(nested)


class LiteratureContextLedger:
    """Archive full batches while sending only a citation ledger back to the LLM."""

    def __init__(self, agent: Any, task_input: Any, mode: str):
        self.agent = agent
        self.task_text = str(getattr(task_input, "task_content", "") or "")
        self.mode = mode
        self.records: List[Dict[str, str]] = []
        self._positions: Dict[str, int] = {}
        self.queries: List[str] = []
        self.batch_paths: List[str] = []
        self.batch_count = 0

        workspace = getattr(getattr(agent, "mcp_tools", None), "workspace_path", None)
        if not workspace:
            try:
                workspace = (agent.get_session_info() or {}).get("workspace_path")
            except Exception:
                workspace = None
        self.workspace = Path(workspace or os.getenv("AGENT_WORKSPACE_PATH") or ".").resolve()
        task_key = hashlib.sha1(self.task_text.encode("utf-8", errors="ignore")).hexdigest()[:12]
        self.root = self.workspace / "research" / "literature_batches" / f"{mode}_{task_key}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "context_index.json"

    @staticmethod
    def _reference_from_candidate(item: Dict[str, Any], source_tool: str) -> Optional[Dict[str, str]]:
        title = _one_line(
            item.get("title") or item.get("paper_title") or item.get("article_title"),
            500,
        )
        if len(title) < 8:
            return None
        doi = _one_line(item.get("doi") or item.get("DOI"), 200)
        url = _one_line(
            item.get("url") or item.get("link") or item.get("source_url") or item.get("pdf_url"),
            500,
        )
        # A title plus a durable identifier/date/author is enough for a working
        # search ledger. Final bibliography validation still happens later.
        authors = _one_line(item.get("authors") or item.get("author") or item.get("creator"), 800)
        year_value = (
            item.get("year") or item.get("published_date") or item.get("published")
            or item.get("publication_date") or item.get("date") or item.get("pubdate")
        )
        year_match = re.search(r"(?:19|20)\d{2}", str(year_value or ""))
        year = year_match.group(0) if year_match else ""
        venue = _one_line(
            item.get("venue") or item.get("journal") or item.get("source")
            or item.get("publication") or item.get("publisher"),
            300,
        )
        evidence = _one_line(
            item.get("abstract") or item.get("summary") or item.get("snippet")
            or item.get("evidence") or item.get("content"),
            1200,
        )
        if not any((doi, url, authors, year, venue)):
            return None
        return {
            "id": "",
            "title": title,
            "authors": authors,
            "year": year,
            "venue": venue,
            "doi": doi,
            "url": url,
            "evidence": evidence,
            "source_tool": source_tool,
        }

    def _merge_records(self, result: Any, source_tool: str) -> List[Dict[str, str]]:
        added: List[Dict[str, str]] = []
        for candidate in _walk_dicts(result):
            record = self._reference_from_candidate(candidate, source_tool)
            if not record:
                continue
            key = f"doi:{record['doi'].lower()}" if record["doi"] else f"title:{_normal_title(record['title'])}"
            if not key or key == "title:":
                continue
            position = self._positions.get(key)
            if position is None:
                record["id"] = f"L{len(self.records) + 1:03d}"
                self._positions[key] = len(self.records)
                self.records.append(record)
                added.append(record)
                continue
            current = self.records[position]
            for field in ("authors", "year", "venue", "doi", "url", "evidence"):
                if not current.get(field) and record.get(field):
                    current[field] = record[field]
        return added

    def _remember_queries(self, arguments: Dict[str, Any]) -> None:
        candidates: List[Any] = []
        for key in ("queries", "query", "keywords", "term", "title", "identifier", "pmid"):
            value = arguments.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif value not in (None, ""):
                candidates.append(value)
        for candidate in candidates:
            query = _one_line(candidate, 300)
            if query and query not in self.queries:
                self.queries.append(query)

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def _archive_batch(self, tool_name: str, arguments: Dict[str, Any], result: Any) -> str:
        self.batch_count += 1
        filename = (
            f"batch_{self.batch_count:03d}_{time.time_ns()}_{threading.get_ident()}_"
            f"{re.sub(r'[^a-zA-Z0-9_-]+', '_', tool_name)[:50]}.json.gz"
        )
        path = self.root / filename
        payload = {
            "tool": tool_name,
            "arguments": arguments,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "result": result,
        }
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, default=str)
        relative = self._relative(path)
        self.batch_paths.append(relative)
        return relative

    def _save_index(self) -> None:
        payload = {
            "task": self.task_text,
            "mode": self.mode,
            "unique_reference_count": len(self.records),
            "queries": self.queries,
            "batch_files": self.batch_paths,
            "references": self.records,
        }
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.index_path)

    def capture(self, tool_name: str, arguments: Dict[str, Any], result: Any) -> Any:
        """Archive a literature result and return its compact prompt representation."""
        if tool_name not in LITERATURE_TOOLS:
            return result
        self._remember_queries(arguments or {})
        batch_path = self._archive_batch(tool_name, arguments or {}, result)
        added = self._merge_records(result, tool_name)
        self._save_index()
        return {
            "success": bool(result.get("success", True)) if isinstance(result, dict) else True,
            "literature_batch_saved": batch_path,
            "new_unique_references": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "year": item["year"],
                    "venue": item["venue"],
                    "doi": item["doi"],
                    "url": item["url"],
                    "evidence_excerpt": item["evidence"][:500],
                }
                for item in added
            ],
            "total_unique_references": len(self.records),
            "instruction": (
                "Use the L-identifiers below to track collected papers. The complete raw result is archived; "
                "do not repeat the same query merely to recover earlier content. Identify the remaining "
                "evidence gap, use a different query/source if needed, or finish the subtask."
            ),
        }

    def summary_for_prompt(self) -> str:
        if not self.batch_paths:
            return ""
        max_refs = max(10, int(os.getenv("INFO_CONTEXT_MAX_REFERENCES", "60")))
        visible = self.records[:max_refs]
        lines = [
            "[Durable literature ledger - full raw batches are stored on disk]",
            f"Collected unique references: {len(self.records)}; archived batches: {len(self.batch_paths)}.",
        ]
        if self.queries:
            lines.append("Queries already attempted: " + " | ".join(self.queries[-20:]))
        for item in visible:
            metadata = "; ".join(
                value for value in (
                    item["title"], item["year"], item["venue"],
                    f"DOI {item['doi']}" if item["doi"] else item["url"],
                ) if value
            )
            lines.append(f"{item['id']}: {metadata}")
        if len(self.records) > len(visible):
            lines.append(
                f"{len(self.records) - len(visible)} additional records remain in {self._relative(self.index_path)}."
            )
        missing_doi = sum(1 for item in self.records if not item["doi"])
        lines.append(
            f"Potential remaining metadata gap: {missing_doi} records have no DOI (some sources such as arXiv may legitimately lack one)."
        )
        lines.append(
            "Compare this ledger with the original task target. Search only unresolved topics/metadata; "
            "otherwise call the appropriate information-seeker task_done tool."
        )
        return "\n".join(lines)

    @staticmethod
    def _compact_message(message: Dict[str, Any]) -> Dict[str, Any]:
        copied = dict(message)
        content = copied.get("content")
        if not isinstance(content, str):
            return copied
        role = copied.get("role")
        limit = 2400 if role == "assistant" else 3600
        if len(content) > limit:
            copied["content"] = content[:limit] + "\n[Earlier message truncated; use the durable literature ledger.]"
        return copied

    def payload_messages(self, conversation_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build a compact API payload without deleting the local conversation trace."""
        summary = self.summary_for_prompt()
        if not summary or len(conversation_history) <= 10:
            return conversation_history
        recent_count = max(4, int(os.getenv("INFO_CONTEXT_RECENT_MESSAGES", "8")))
        preserved = conversation_history[:2]
        recent = conversation_history[-recent_count:]
        recent_ids = {id(message) for message in recent}
        user_directives = [
            message for message in conversation_history[2:-recent_count]
            if message.get("role") == "user" and id(message) not in recent_ids
        ]
        output = [self._compact_message(message) for message in preserved]
        output.extend(self._compact_message(message) for message in user_directives)
        output.append({"role": "user", "content": summary + " /no_think"})
        output.extend(self._compact_message(message) for message in recent)
        return output
