from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SourceFile:
    path: Path
    rel_path: str
    kind: str
    size_bytes: int
    text_preview: str = ""
    extracted_text: str = ""
    extraction_method: str = ""
    extraction_error: str = ""
    extracted_path: str = ""
    truncated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LiteratureTask:
    topic: str
    queries: List[str]
    max_sources: int = 8
    notes: str = ""


@dataclass
class ReferenceRecord:
    title: str
    index: int = 0
    authors: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    url: str = ""
    source_path: str = ""
    evidence: str = ""
    abstract: str = ""
    query: str = ""
    source_type: str = "local"


@dataclass
class SectionSpec:
    key: str
    title: str
    purpose: str
    required_evidence: List[str] = field(default_factory=list)


@dataclass
class ResearchContract:
    paper_type: str
    research_question: str
    problem_statement: str = ""
    central_claim: str = ""
    contributions: List[str] = field(default_factory=list)
    method_modules: List[str] = field(default_factory=list)
    datasets: List[str] = field(default_factory=list)
    baselines: List[str] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    evidence_sufficient: bool = False
    missing_evidence: List[str] = field(default_factory=list)


@dataclass
class ClaimEvidence:
    claim_id: str
    claim: str
    claim_type: str
    source_ids: List[str] = field(default_factory=list)
    reference_indices: List[int] = field(default_factory=list)
    evidence_snippets: List[str] = field(default_factory=list)
    status: str = "needs_evidence"
    allowed_sections: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class PipelineContext:
    workspace_path: Path
    query: str
    language: str = "zh"
    source_files: List[SourceFile] = field(default_factory=list)
    literature_tasks: List[LiteratureTask] = field(default_factory=list)
    references: List[ReferenceRecord] = field(default_factory=list)
    sections: List[SectionSpec] = field(default_factory=list)
    research_contract: Optional[ResearchContract] = None
    claims_evidence: List[ClaimEvidence] = field(default_factory=list)
    paper_outline: Dict[str, List[str]] = field(default_factory=dict)
    experiment_registry: List[Dict[str, Any]] = field(default_factory=list)
    visual_assets: List[Dict[str, Any]] = field(default_factory=list)
    user_interventions: List[str] = field(default_factory=list)
    section_outputs: Dict[str, str] = field(default_factory=dict)
    review_results: List[Dict[str, Any]] = field(default_factory=list)
    citation_audit: Dict[str, Any] = field(default_factory=dict)
    visual_audit: Dict[str, Any] = field(default_factory=dict)
    skills_used: List[str] = field(default_factory=list)
    use_web_search: bool = True
    enable_review: bool = True
    auto_revise: bool = True
    warnings: List[str] = field(default_factory=list)


@dataclass
class PipelineV2Result:
    success: bool
    workspace_path: str
    final_report_path: Optional[str] = None
    pdf_path: Optional[str] = None
    references_path: Optional[str] = None
    reference_download_path: Optional[str] = None
    review_path: Optional[str] = None
    citation_audit_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
