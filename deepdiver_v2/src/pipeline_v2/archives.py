from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple


SUPPORTED_ARCHIVES = {".zip", ".tar", ".gz", ".tgz"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_target(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/").lstrip("/")
    target = (root / normalized).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"archive member escapes extraction directory: {member_name}")
    return target


def _extract_zip(path: Path, target: Path, max_files: int, max_bytes: int) -> Tuple[int, int]:
    with zipfile.ZipFile(path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        total = sum(item.file_size for item in members)
        if len(members) > max_files:
            raise ValueError(f"archive contains too many files: {len(members)} > {max_files}")
        if total > max_bytes:
            raise ValueError(f"archive expands beyond limit: {total} > {max_bytes}")
        for item in archive.infolist():
            destination = _safe_target(target, item.filename)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
        return len(members), total


def _extract_tar(path: Path, target: Path, max_files: int, max_bytes: int) -> Tuple[int, int]:
    with tarfile.open(path, mode="r:*") as archive:
        members = archive.getmembers()
        files = [item for item in members if item.isfile()]
        total = sum(max(0, item.size) for item in files)
        if any(item.issym() or item.islnk() for item in members):
            raise ValueError("symbolic and hard links are not allowed in uploaded archives")
        if len(files) > max_files:
            raise ValueError(f"archive contains too many files: {len(files)} > {max_files}")
        if total > max_bytes:
            raise ValueError(f"archive expands beyond limit: {total} > {max_bytes}")
        for item in members:
            destination = _safe_target(target, item.name)
            if item.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(item)
                if source is not None:
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
        return len(files), total


def extract_uploaded_archives(workspace_path: Path) -> Tuple[List[Dict[str, object]], List[str]]:
    """Safely extract uploaded experiment archives and reuse unchanged extractions."""
    upload_dir = Path(workspace_path) / "user_uploads"
    output_root = Path(workspace_path) / "experiment_results"
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "archive_manifest.json"
    previous: Dict[str, Dict[str, object]] = {}
    if manifest_path.exists():
        try:
            previous = {item["archive_path"]: item for item in json.loads(manifest_path.read_text(encoding="utf-8"))}
        except Exception:
            previous = {}

    max_files = int(os.getenv("ARCHIVE_MAX_FILES", "10000"))
    max_bytes = int(os.getenv("ARCHIVE_MAX_EXPANDED_MB", "4096")) * 1024 * 1024
    records: List[Dict[str, object]] = []
    warnings: List[str] = []
    if not upload_dir.exists():
        manifest_path.write_text("[]\n", encoding="utf-8")
        return records, warnings

    candidates = []
    for path in upload_dir.rglob("*"):
        if not path.is_file():
            continue
        lower_name = path.name.lower()
        if path.suffix.lower() in SUPPORTED_ARCHIVES or lower_name.endswith(".tar.gz"):
            candidates.append(path)
        elif path.suffix.lower() in {".rar", ".7z"}:
            warnings.append(f"Archive format is accepted by upload but not yet extractable: {path.name}; please use ZIP.")

    for archive_path in sorted(candidates):
        rel_path = archive_path.relative_to(workspace_path).as_posix()
        digest = _sha256(archive_path)
        old = previous.get(rel_path)
        if old and old.get("sha256") == digest and Path(str(old.get("output_dir", ""))).exists():
            records.append(old)
            continue
        stem = archive_path.name
        for suffix in (".tar.gz", ".zip", ".tar", ".tgz", ".gz"):
            if stem.lower().endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        safe_stem = "".join(char if char.isalnum() or char in "-_." else "_" for char in stem).strip("._") or "experiment_archive"
        target = output_root / f"{safe_stem}_{digest[:8]}"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        try:
            if archive_path.suffix.lower() == ".zip":
                file_count, expanded_bytes = _extract_zip(archive_path, target, max_files, max_bytes)
            else:
                file_count, expanded_bytes = _extract_tar(archive_path, target, max_files, max_bytes)
            records.append({
                "archive_path": rel_path,
                "sha256": digest,
                "output_dir": str(target.resolve()),
                "file_count": file_count,
                "expanded_bytes": expanded_bytes,
                "status": "extracted",
            })
        except Exception as exc:
            shutil.rmtree(target, ignore_errors=True)
            warnings.append(f"Could not safely extract {rel_path}: {exc}")
            records.append({"archive_path": rel_path, "sha256": digest, "output_dir": "", "status": "failed", "error": str(exc)})
    manifest_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return records, warnings
