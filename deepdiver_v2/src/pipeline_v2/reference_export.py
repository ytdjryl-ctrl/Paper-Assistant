from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


REFERENCE_TXT_NAME = "reference_download_list.txt"


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normal_doi(value: Any) -> str:
    doi = _one_line(value)
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.IGNORECASE)
    return doi.rstrip(".,; ")


def _reference_body(manuscript: str) -> str:
    match = re.search(
        r"^#\s*(?:参考文献|References)\s*$",
        manuscript or "",
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return manuscript[:match.start()] if match else (manuscript or "")


def cited_reference_indices(manuscript: str) -> List[int]:
    """Return citation numbers in first-appearance order, excluding bibliography text."""
    output: List[int] = []
    for group in re.findall(r"\[([0-9,;\-\s]+)\]", _reference_body(manuscript)):
        for token in re.split(r"[,;\s]+", group.strip()):
            if not token:
                continue
            if "-" in token:
                parts = token.split("-", 1)
                if all(part.isdigit() for part in parts):
                    start, end = (int(part) for part in parts)
                    if 0 < start <= end and end - start <= 100:
                        for value in range(start, end + 1):
                            if value not in output:
                                output.append(value)
                continue
            if token.isdigit() and int(token) > 0 and int(token) not in output:
                output.append(int(token))
    return output


def _select_references(references: Sequence[Dict[str, Any]], manuscript: str) -> List[Dict[str, Any]]:
    cited = cited_reference_indices(manuscript)
    if not cited:
        return list(references)
    by_index = {}
    for position, reference in enumerate(references, 1):
        try:
            index = int(reference.get("index") or position)
        except (TypeError, ValueError):
            index = position
        by_index[index] = reference
    selected = [by_index[index] for index in cited if index in by_index]
    return selected or list(references)


def format_reference_download_lines(references: Iterable[Dict[str, Any]]) -> List[str]:
    lines = []
    for output_index, reference in enumerate(references, 1):
        title = _one_line(reference.get("title")) or "题名未记录"
        venue = _one_line(reference.get("venue") or reference.get("journal")) or "未记录"
        doi = _normal_doi(reference.get("doi")) or "无"
        lines.append(f"{output_index}. {title} | 期刊：{venue} | DOI：{doi}")
    return lines


def write_reference_download_txt(
    workspace_path: str | Path,
    manuscript_path: str | Path | None = None,
) -> Path:
    """Create the user-facing, line-separated bibliography download file."""
    workspace = Path(workspace_path)
    references_path = workspace / "research" / "references.json"
    if not references_path.is_file():
        raise FileNotFoundError(f"Structured references not found: {references_path}")
    payload = json.loads(references_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("research/references.json must contain a list")
    references = [item for item in payload if isinstance(item, dict)]

    report_path = Path(manuscript_path) if manuscript_path else workspace / "report" / "final_report.md"
    manuscript = report_path.read_text(encoding="utf-8", errors="ignore") if report_path.is_file() else ""
    selected = _select_references(references, manuscript)
    lines = format_reference_download_lines(selected)

    report_dir = workspace / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    output_path = report_dir / REFERENCE_TXT_NAME
    # utf-8-sig keeps Chinese readable in Windows Notepad; explicit CRLF
    # guarantees every numbered record is displayed on its own line.
    content = "\r\n".join(lines) + ("\r\n" if lines else "")
    output_path.write_text(content, encoding="utf-8-sig", newline="")
    return output_path
