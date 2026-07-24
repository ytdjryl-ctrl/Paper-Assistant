from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, List

from .models import SourceFile


TEXT_SUFFIXES = {".txt", ".md", ".py", ".yaml", ".yml", ".json", ".csv", ".tsv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".svg"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xls"}
DOCUMENT_SUFFIXES = {".doc", ".docx", ".ppt", ".pptx"}
PDF_SUFFIXES = {".pdf"}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}


def _kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in SPREADSHEET_SUFFIXES:
        return "spreadsheet"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in {".csv", ".tsv"}:
        return "table"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "other"


def _preview_text(path: Path, limit: int = 2500) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md", ".py", ".yaml", ".yml"}:
            return path.read_text(encoding="utf-8", errors="ignore")[:limit]
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            return json.dumps(data, ensure_ascii=False, indent=2)[:limit]
        if suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            rows = []
            with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
                reader = csv.reader(f, delimiter=delimiter)
                for idx, row in enumerate(reader):
                    rows.append(" | ".join(row[:12]))
                    if idx >= 20:
                        break
            return "\n".join(rows)[:limit]
    except Exception as exc:
        return f"[preview failed: {exc}]"
    return ""


def collect_source_files(workspace_path: Path, folders: Iterable[str] = ("user_uploads", "library_refs", "experiment_results")) -> List[SourceFile]:
    files: List[SourceFile] = []
    for folder in folders:
        root = workspace_path / folder
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if path.suffix.lower() in ARCHIVE_SUFFIXES:
                continue
            rel_path = path.relative_to(workspace_path).as_posix()
            # Generated figures are outputs of ExperimentAgent, not new user
            # evidence. Re-ingesting them inflated the evidence manifest and
            # produced one unsupported-SVG warning per chart on every refresh.
            if rel_path.startswith("experiment_results/figures/"):
                continue
            if path.name in {".gitkeep", ".DS_Store", "Thumbs.db"}:
                continue
            files.append(
                SourceFile(
                    path=path,
                    rel_path=rel_path,
                    kind=_kind_for(path),
                    size_bytes=path.stat().st_size,
                    text_preview=_preview_text(path),
                )
            )
    return files
