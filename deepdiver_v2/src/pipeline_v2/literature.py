from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Sequence

from .models import LiteratureTask, ReferenceRecord, SourceFile


_GENERATED_REFERENCE_TITLE_PATTERNS = (
    r"^literature\s+(?:summary|review)\b",
    r"^文献(?:总结|综述|检索结果)\b",
    r"^review\s+for\b",
    r"^(?:peer\s+)?reviewer\s+(?:report|comments?)\b",
    r"^(?:review|审稿)(?:报告|意见|汇总)\b",
)


def is_valid_reference_candidate(reference: ReferenceRecord) -> bool:
    """Reject workflow artifacts that look like papers but are not publications."""
    title = re.sub(r"\s+", " ", (reference.title or "")).strip()
    if not title or len(title) < 8:
        return False
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in _GENERATED_REFERENCE_TITLE_PATTERNS):
        return False
    source_path = (reference.source_path or "").lower().replace("\\", "/")
    artifact_names = (
        "peer_review", "reviewer_report", "review_synthesis", "literature_review_",
        "autonomous_search/", "claims_evidence", "research_contract", "paper_outline",
    )
    if any(marker in source_path for marker in artifact_names):
        return False
    return True


def is_countable_reference(reference: ReferenceRecord) -> bool:
    """Return whether a record is strong enough to satisfy the literature floor.

    A search hit is not counted merely because it has a title.  It also needs a
    durable locator and at least one bibliographic identity field so that the
    citation reviewer and the user can verify it later.
    """
    if not is_valid_reference_candidate(reference):
        return False
    has_locator = bool((reference.doi or "").strip() or (reference.url or "").strip())
    has_identity = any(bool((value or "").strip()) for value in (
        reference.authors, reference.year, reference.venue,
    ))
    return has_locator and has_identity


def build_literature_tasks(query: str, source_files: Iterable[SourceFile]) -> List[LiteratureTask]:
    text = query.lower()
    file_text = "\n".join(f.text_preview.lower() for f in source_files if f.text_preview)
    combined = text + "\n" + file_text

    tasks: List[LiteratureTask] = []
    if any(term in combined for term in ["apple", "ripeness", "maturity", "fruit", "agricultur"]):
        tasks.append(
            LiteratureTask(
                topic="apple_fruit_ripeness_detection",
                queries=[
                    "apple ripeness detection YOLO deep learning 2022 2026",
                    "fruit maturity detection object detection deep learning recent",
                    "non-destructive fruit quality inspection computer vision YOLO",
                ],
                max_sources=8,
            )
        )
    if any(term in combined for term in ["spectral", "cross-attention", "attention", "wavelet", "ghost", "lightweight"]):
        tasks.append(
            LiteratureTask(
                topic="spectral_fusion_lightweight_networks",
                queries=[
                    "spectral guided cross attention fusion lightweight neural network",
                    "wavelet feature fusion attention object detection lightweight",
                    "Ghost module lightweight YOLO agricultural detection",
                ],
                max_sources=8,
            )
        )
    if any(term in combined for term in ["yolo", "wiou", "sppf", "c2psa", "as7265x"]):
        tasks.append(
            LiteratureTask(
                topic="yolo_evolution_modules_sensors",
                queries=[
                    "YOLOv5 YOLOv8 YOLOv10 YOLOv11 architecture improvements",
                    "Wise-IoU WIOU bounding box regression loss object detection",
                    "SPPF C2PSA YOLO module AS7265x multispectral sensor agriculture",
                ],
                max_sources=8,
            )
        )
    if not tasks:
        tasks.append(
            LiteratureTask(
                topic="general_related_work",
                queries=[query[:180]],
                max_sources=6,
                notes="Fallback task because no domain-specific markers were detected.",
            )
        )
    return tasks


def parse_reference_records_from_markdown(markdown: str, source_path: str = "") -> List[ReferenceRecord]:
    """Parse either a saved paper summary or a conventional bibliography block."""
    title_match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    source_match = re.search(
        r"^\*\*Source\*\*:[ \t]*(.*?)[ \t]*$",
        markdown,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    authors_match = re.search(r"^\*\*Authors?\*\*:[ \t]*(.*?)[ \t]*$", markdown, flags=re.MULTILINE | re.IGNORECASE)
    explicit_year_match = re.search(r"^\*\*Year\*\*:[ \t]*((?:19|20)\d{2})[ \t]*$", markdown, flags=re.MULTILINE | re.IGNORECASE)
    source_type_match = re.search(r"^\*\*Retrieved via\*\*:[ \t]*(.*?)[ \t]*$", markdown, flags=re.MULTILINE | re.IGNORECASE)
    query_match = re.search(r"^\*\*Query\*\*:[ \t]*(.*?)[ \t]*$", markdown, flags=re.MULTILINE | re.IGNORECASE)
    doi_match = re.search(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", markdown, flags=re.IGNORECASE)
    year_match = re.search(r"\b((?:19|20)\d{2})\b", markdown)
    url_match = re.search(r"https?://[^\s)>]+", markdown)
    abstract_match = re.search(
        r"^##\s*(?:内容摘要|摘要|Abstract(?:\s*/\s*Evidence)?)\s*$\s*(.+?)(?=^##\s|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )

    if title_match:
        title = title_match.group(1).strip()
        source_value = source_match.group(1).strip() if source_match else ""
        candidate = ReferenceRecord(
                title=title[:500],
                authors=authors_match.group(1).strip()[:1000] if authors_match else "",
                year=(explicit_year_match or year_match).group(1) if (explicit_year_match or year_match) else "",
                venue=source_value if source_value and not source_value.startswith("./") else "",
                doi=doi_match.group(1).rstrip(".,;") if doi_match else "",
                url=url_match.group(0).rstrip(".,;") if url_match else "",
                source_path=source_path,
                evidence=(abstract_match.group(1).strip() if abstract_match else markdown[:1500])[:3000],
                abstract=(abstract_match.group(1).strip() if abstract_match else "")[:3000],
                source_type=source_type_match.group(1).strip().lower() if source_type_match else "local_summary",
                query=query_match.group(1).strip()[:1000] if query_match else "",
            )
        return [candidate] if is_valid_reference_candidate(candidate) else []

    records: List[ReferenceRecord] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip().lstrip("-*0123456789. ")
        if not line or len(line) < 20:
            continue
        if not re.search(r"(19|20)\d{2}", line):
            continue
        doi_match = re.search(r"(10\.\d{4,9}/\S+)", line)
        year_match = re.search(r"((?:19|20)\d{2})", line)
        title = line
        candidate = ReferenceRecord(
                title=title[:300],
                year=year_match.group(1) if year_match else "",
                doi=doi_match.group(1).rstrip(".,;") if doi_match else "",
                source_path=source_path,
                evidence=line[:500],
                source_type="local_bibliography",
            )
        if is_valid_reference_candidate(candidate):
            records.append(candidate)
    return records


def _tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9._+-]{1,}", lowered))
    for block in re.findall(r"[\u4e00-\u9fff]+", lowered):
        tokens.update(block[index:index + 2] for index in range(max(0, len(block) - 1)))
    return tokens


def _relevance_score(reference: ReferenceRecord, query: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    reference_tokens = _tokens(" ".join([reference.title, reference.abstract, reference.evidence, reference.venue]))
    return len(query_tokens & reference_tokens) / max(1, len(query_tokens))


def _has_in_scope_search_provenance(reference: ReferenceRecord) -> bool:
    """Recognize records produced by a query scoped to the current workspace.

    This provides a language-independent bridge: an English paper retrieved by
    an English search query derived from a Chinese research request remains in
    scope even when the two texts have no literal token overlap.
    """
    source_path = (reference.source_path or "").replace("\\", "/").lower()
    query = (reference.query or "").strip()
    if not query:
        return False
    return source_path.startswith((
        "research/literature_online/",
        "research/literature_batches/",
        "research/autonomous_search/",
        "research/retrieved/",
        "url_crawler_save_files/research/",
    ))


def _normal_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())


def deduplicate_references(references: Sequence[ReferenceRecord]) -> List[ReferenceRecord]:
    deduped: List[ReferenceRecord] = []
    seen_doi = {}
    seen_title = {}
    source_priority = {
        "sciencedirect": 6,
        "pubmed": 5,
        "crossref": 5,
        "openalex": 4,
        "web_crawled": 3,
        "web_search": 2,
        "arxiv": 1,
    }

    def quality(record: ReferenceRecord) -> tuple:
        complete = sum(bool(getattr(record, field)) for field in ("authors", "year", "venue", "doi", "url", "abstract"))
        return source_priority.get(record.source_type, 0), complete, len(record.abstract or record.evidence)

    def merge(primary: ReferenceRecord, secondary: ReferenceRecord) -> ReferenceRecord:
        output = deepcopy(primary)
        for field in ("title", "authors", "year", "venue", "doi", "url", "source_path", "query"):
            if not getattr(output, field) and getattr(secondary, field):
                setattr(output, field, getattr(secondary, field))
        if len(secondary.abstract or "") > len(output.abstract or ""):
            output.abstract = secondary.abstract
        if len(secondary.evidence or "") > len(output.evidence or ""):
            output.evidence = secondary.evidence
        return output

    for reference in references:
        doi = reference.doi.lower().strip()
        title = _normal_title(reference.title)
        if not title:
            continue
        # DOI is authoritative.  Normalized title is the fallback across
        # publisher, index and preprint URLs, which otherwise inflate the
        # apparent literature count.
        position = seen_doi.get(doi) if doi else None
        if position is None:
            position = seen_title.get(title)
        if position is not None:
            current = deduped[position]
            preferred, secondary = (reference, current) if quality(reference) > quality(current) else (current, reference)
            deduped[position] = merge(preferred, secondary)
            merged = deduped[position]
            if merged.doi:
                seen_doi[merged.doi.lower().strip()] = position
            seen_title[_normal_title(merged.title)] = position
            continue
        position = len(deduped)
        if doi:
            seen_doi[doi] = position
        seen_title[title] = position
        deduped.append(deepcopy(reference))
    return deduped


def assign_reference_indices(references: Sequence[ReferenceRecord]) -> List[ReferenceRecord]:
    output = list(references)
    for index, reference in enumerate(output, 1):
        reference.index = index
    return output


def load_existing_references(
    workspace_path: Path,
    query: str = "",
    include_bundled: bool = False,
    max_bundled: int = 50,
) -> List[ReferenceRecord]:
    roots = [
        workspace_path / "research" / "literature_online",
        workspace_path / "research" / "literature",
        workspace_path / "research" / "retrieved",
        workspace_path / "url_crawler_save_files" / "research",
        workspace_path / "library_refs",
    ]
    bundled_root = Path(__file__).resolve().parents[2] / "research"
    if include_bundled and bundled_root.resolve() != (workspace_path / "research").resolve():
        roots.append(bundled_root)

    records: List[ReferenceRecord] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if path.name in {"literature_plan.md", "references.md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            try:
                source_path = path.relative_to(workspace_path).as_posix()
            except ValueError:
                source_path = f"bundled:{path.relative_to(bundled_root).as_posix()}"
            parsed = parse_reference_records_from_markdown(text, source_path)
            if root == bundled_root and not re.search(r"^#\s+.+$", text, flags=re.MULTILINE):
                year_match = re.search(r"\b((?:19|20)\d{2})\b", text)
                doi_match = re.search(r"\b(10\.\d{4,9}/\S+)", text, flags=re.IGNORECASE)
                parsed = [
                    ReferenceRecord(
                        title=path.stem.replace("_", " ")[:500],
                        year=year_match.group(1) if year_match else "",
                        doi=doi_match.group(1).rstrip(".,;") if doi_match else "",
                        source_path=source_path,
                        evidence=text[:3000],
                        abstract=text[:3000],
                        source_type="local_summary",
                    )
                ]
            records.extend(record for record in parsed if is_valid_reference_candidate(record))

    # Long-running InformationSeeker loops archive each raw search batch and
    # maintain a compact context_index.json.  Read that durable ledger here so
    # references survive context compaction even if the model fails to write an
    # additional Markdown summary before finishing the subtask.
    batch_root = workspace_path / "research" / "literature_batches"
    if batch_root.exists():
        for index_path in batch_root.rglob("context_index.json"):
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            queries = payload.get("queries", []) if isinstance(payload, dict) else []
            query_text = " | ".join(str(item) for item in queries[:20])
            ledger_records = payload.get("references", []) if isinstance(payload, dict) else []
            if not isinstance(ledger_records, list):
                continue
            relative_index = index_path.relative_to(workspace_path).as_posix()
            for item in ledger_records:
                if not isinstance(item, dict):
                    continue
                source_tool = str(item.get("source_tool") or "information_seeker")
                candidate = ReferenceRecord(
                    title=str(item.get("title") or "")[:500],
                    authors=str(item.get("authors") or "")[:1000],
                    year=str(item.get("year") or "")[:20],
                    venue=str(item.get("venue") or "")[:500],
                    doi=str(item.get("doi") or "").rstrip(".,; ")[:300],
                    url=str(item.get("url") or "").rstrip(".,; ")[:1000],
                    source_path=f"{relative_index}#{item.get('id', '')}",
                    evidence=str(item.get("evidence") or "")[:3000],
                    abstract=str(item.get("evidence") or "")[:3000],
                    query=query_text[:1000],
                    source_type=source_tool,
                )
                if is_valid_reference_candidate(candidate):
                    records.append(candidate)

    records = deduplicate_references(record for record in records if is_valid_reference_candidate(record))
    if query:
        workspace_records = [record for record in records if not record.source_path.startswith("bundled:")]
        relevant_workspace_records = [record for record in workspace_records if _relevance_score(record, query) > 0]
        # Do not erase a small uploaded/curated library merely because its title
        # uses terminology absent from a short user query. Online search output,
        # however, must have at least one topical overlap.
        workspace_records = [
            record for record in workspace_records
            if (
                record.source_type == "uploaded_reference"
                or record in relevant_workspace_records
                or _has_in_scope_search_provenance(record)
            )
        ]
        workspace_records.sort(
            key=lambda record: (
                _relevance_score(record, query),
                1 if _has_in_scope_search_provenance(record) else 0,
                1 if is_countable_reference(record) else 0,
            ),
            reverse=True,
        )
        bundled_records = [record for record in records if record.source_path.startswith("bundled:")]
        bundled_records.sort(key=lambda record: _relevance_score(record, query), reverse=True)
        bundled_records = [record for record in bundled_records if _relevance_score(record, query) > 0][:max_bundled]
        records = workspace_records + bundled_records
    return records


def references_from_source_files(source_files: Iterable[SourceFile]) -> List[ReferenceRecord]:
    records: List[ReferenceRecord] = []
    for source in source_files:
        if not source.rel_path.startswith("library_refs/") or not source.extracted_text.strip():
            continue
        text = source.extracted_text
        if source.path.suffix.lower() in {".md", ".txt"}:
            parsed = parse_reference_records_from_markdown(text, source.rel_path)
            if parsed:
                records.extend(parsed)
                continue
        lines = [line.strip(" #*\t") for line in text.splitlines() if len(line.strip()) >= 20]
        title = lines[0] if lines else source.path.stem
        year_match = re.search(r"\b((?:19|20)\d{2})\b", text)
        doi_match = re.search(r"\b(10\.\d{4,9}/\S+)", text, flags=re.IGNORECASE)
        records.append(
            ReferenceRecord(
                title=title[:500],
                year=year_match.group(1) if year_match else "",
                doi=doi_match.group(1).rstrip(".,;") if doi_match else "",
                source_path=source.rel_path,
                evidence=text[:3000],
                abstract=text[:3000],
                source_type="uploaded_reference",
            )
        )
    return records


def save_structured_references(workspace_path: Path, references: Sequence[ReferenceRecord]) -> Path:
    research_dir = workspace_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    output_path = research_dir / "references.json"
    output_path.write_text(
        json.dumps([asdict(reference) for reference in references], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # This is the only bibliography catalogue exposed to WriterAgent.  Its
    # stable numbers prevent each chapter from inventing a separate ordering.
    catalog_lines = [
        "# Structured Citation Catalogue",
        "",
        "Use the exact [N] identifier below only when its title/abstract supports the sentence. "
        "Never infer a citation number from another file.",
        "",
    ]
    for reference in references:
        metadata = ". ".join(
            value for value in (
                reference.authors.strip(),
                reference.title.strip(),
                reference.venue.strip(),
                reference.year.strip(),
                f"DOI: {reference.doi.strip()}" if reference.doi.strip() else "",
            ) if value
        )
        evidence = re.sub(r"\s+", " ", reference.abstract or reference.evidence or "").strip()[:500]
        catalog_lines.append(f"[{reference.index}] {metadata}")
        if evidence:
            catalog_lines.append(f"    Evidence: {evidence}")
    (research_dir / "references.md").write_text("\n".join(catalog_lines).rstrip() + "\n", encoding="utf-8")
    return output_path
