from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.tools.mcp_tools import MCPTools

from .extraction import extract_source_files
from .file_inventory import collect_source_files
from .literature import (
    assign_reference_indices, build_literature_tasks, deduplicate_references,
    load_existing_references, references_from_source_files, save_structured_references,
)
from .models import PipelineContext
from .research_contract import build_research_contract, save_research_contract
from .review import (
    audit_citations, revise_manuscript, reviews_require_revision, run_reviews,
    synchronize_citations,
)
from .retrieval import retrieve_online_references


def minimum_valid_references() -> int:
    """Configured floor for verifiable, deduplicated literature records."""
    return max(0, int(os.getenv("V2_MIN_VALID_REFERENCES", "30")))
from .visual_communication import audit_manuscript_visuals, plan_visual_communication


def save_workspace_digest(workspace_path: Path, source_files: List[Any]) -> Path:
    """Write one compact inventory so Planner need not open every source file."""
    groups: Dict[str, int] = {}
    files: List[Dict[str, Any]] = []
    for source in source_files:
        groups[source.kind] = groups.get(source.kind, 0) + 1
        files.append({
            "path": source.rel_path,
            "kind": source.kind,
            "size_bytes": source.size_bytes,
            "preview": (source.text_preview or source.extracted_text or "")[:600],
            "extraction_error": source.extraction_error,
        })
    payload = {
        "version": 1,
        "summary": {"file_count": len(files), "by_kind": groups},
        "instruction": "Use this inventory first; open an original file only when its exact evidence is needed.",
        "files": files,
    }
    output = workspace_path / "research" / "workspace_digest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def seed_hybrid_literature(
    workspace_path: Path, query: str, *, enabled: bool = True,
    checkpoint: Optional[Callable[[str, Dict[str, Any]], List[str]]] = None,
) -> Tuple[int, List[str]]:
    """Inventory local evidence and optionally run the legacy online seed.

    The production Hybrid entrypoint passes ``enabled=False``: online search is
    owned by InformationSeekerAgent after Planner identifies evidence gaps.
    Keeping the optional branch preserves explicit CLI/tests without making it
    an implicit pre-search stage.
    """
    workspace_path = Path(workspace_path)
    source_files = collect_source_files(workspace_path)
    warnings = extract_source_files(source_files, workspace_path)
    save_workspace_digest(workspace_path, source_files)
    tasks = build_literature_tasks(query, source_files) if enabled else []
    started_stage = "literature_seed_started" if enabled else "local_reference_inventory_started"
    ready_stage = "literature_seed_ready" if enabled else "local_reference_inventory_ready"
    if checkpoint:
        checkpoint(started_stage, {
            "task_count": len(tasks), "query_count": sum(len(task.queries) for task in tasks),
            "online_search": enabled,
        })
    # Never mix the repository's generic demonstration library into a user's
    # paper.  Only this workspace's uploaded and retrieved evidence is valid.
    local = load_existing_references(workspace_path, query=query, include_bundled=False)
    uploaded = references_from_source_files(source_files)
    online, retrieval_warnings = ([], [])
    if enabled:
        online, retrieval_warnings = retrieve_online_references(
            tasks, MCPTools(workspace_path=workspace_path), workspace_path, enabled=True,
        )
    warnings.extend(retrieval_warnings)
    references = assign_reference_indices(deduplicate_references(local + uploaded + online))
    from .literature import is_countable_reference
    countable_references = [reference for reference in references if is_countable_reference(reference)]
    minimum = minimum_valid_references()
    gate_met = not minimum or len(countable_references) >= minimum
    if not gate_met:
        warnings.append(
            f"有效去重文献 {len(countable_references)}/{minimum}，初始检索未达到写作底线；"
            "自主 InformationSeeker 必须针对证据缺口继续检索。"
        )
    if references:
        save_structured_references(workspace_path, references)
    contract, claims, outline = build_research_contract(query, source_files, references)
    save_research_contract(workspace_path, contract, claims, outline)
    visual_assets, visual_warnings = plan_visual_communication(
        workspace_path, contract=contract, claims=claims, outline=outline, references=references,
    )
    warnings.extend(visual_warnings)
    if checkpoint:
        checkpoint("visual_plan_ready", {
            "asset_count": len(visual_assets),
            "summary": f"已规划 {len(visual_assets)} 项结构图、实验图或表格，并绑定到对应章节与证据。",
        })
        checkpoint(ready_stage, {
            "reference_count": len(countable_references), "total_reference_count": len(references),
            "minimum_reference_count": minimum,
            "reference_gate_met": gate_met, "warning_count": len(retrieval_warnings),
            "online_search": enabled,
        })
    return len(countable_references), warnings


def refresh_hybrid_evidence(workspace_path: Path, query: str) -> Dict[str, int]:
    """Re-index autonomous search artifacts and refresh the contract before Writer handoff."""
    workspace_path = Path(workspace_path)
    source_files = collect_source_files(workspace_path)
    extract_source_files(source_files, workspace_path)
    save_workspace_digest(workspace_path, source_files)
    raw_references = load_existing_references(workspace_path, query="", include_bundled=False)
    references = assign_reference_indices(deduplicate_references(
        load_existing_references(workspace_path, query=query, include_bundled=False)
        + references_from_source_files(source_files)
    ))
    from .literature import is_countable_reference
    countable_references = [reference for reference in references if is_countable_reference(reference)]
    save_structured_references(workspace_path, references)
    contract, claims, outline = build_research_contract(query, source_files, references)
    save_research_contract(workspace_path, contract, claims, outline)
    visual_assets, _ = plan_visual_communication(
        workspace_path, contract=contract, claims=claims, outline=outline, references=references,
    )
    minimum = minimum_valid_references()
    return {
        "reference_count": len(countable_references), "total_reference_count": len(references),
        "valid_reference_pool_count": sum(is_countable_reference(reference) for reference in raw_references),
        "minimum_reference_count": minimum,
        "reference_gate_met": not minimum or len(countable_references) >= minimum,
        "claim_count": len(claims), "visual_asset_count": len(visual_assets),
    }


def build_autonomous_agent_brief(workspace_path: Path, query: str) -> str:
    """Point the legacy ReAct agents at V2 evidence constraints without constraining their loop."""
    minimum = minimum_valid_references()
    return f"""
[SciAssistant Hybrid Autonomous Research Mode]
Work autonomously through PlannerAgent -> InformationSeeker/ExperimentAgent -> WriterAgent.
You may search iteratively, inspect files, call tools, revise the plan, and delegate until the evidence is sufficient.
Never resend a completed literature batch. A supplemental search must target a concrete unsupported claim,
missing source type/date range, or unresolved citation and must state that reason in `evidence_gap`.
Do not stop merely because a plausible outline or short draft exists.
    Before WriterAgent, you MUST invoke InformationSeeker at least once through
    `assign_multi_subjective_tasks_to_info_seeker`. Startup preprocessing only inventories user-provided/local
    evidence and never substitutes for an actual InformationSeekerAgent run. Ask InformationSeeker to validate
    existing references and search the concrete gaps in Claims-Evidence Matrix; do not use fixed generic queries.
    For experiments,
    inspect `experiment_results/experiment_registry.json` first. A complete registry whose CSV hashes still
    match is already successful experimental evidence and MUST NOT be reprocessed merely to satisfy an agent-call
    requirement. Invoke `assign_task_to_experimenter` only for a concrete experiment gap such as new/changed data,
    a missing metric, a missing comparison/ablation, a requested additional figure, or an explicitly requested rerun.
    Literature search, DOI cleanup, bibliography merging, and citation-file formatting are never ExperimentAgent tasks.
    PlannerAgent must not write the paper directly or declare completion on behalf of WriterAgent.
    A hash-verified experiment registry counts as a completed ExperimentAgent stage across Agent/process boundaries;
    do not manufacture experiment_results.md or rerun ExperimentAgent merely to set an in-memory completion flag.

Hard evidence files in this workspace:
- research/workspace_digest.json (read this inventory before opening individual uploads)
- research/research_contract.json
- research/claims_evidence.json
- research/paper_outline.json
- experiment_results/experiment_registry.json
- experiment_results/figure_plans.json
- research/visual_assets_registry.json
- research/visual_assets_guide.md
- experiment_results/tables/*.md

Requirements:
1. Read these files before planning and treat them as hard constraints.
2. Use uploaded files as primary evidence; never invent experiments, metrics, citations, formulas, or implementation details.
3. Search iteratively when literature or claim support is insufficient, while deduplicating completed topics, and save useful literature records in the workspace.
   Start from research/references.json and research/literature_online/*.md, verify relevance, then expand with InformationSeeker when gaps remain.
   The literature quality floor is {minimum} valid deduplicated publications. A record counts only when it has a verifiable title,
   DOI or stable URL, and bibliographic identity metadata. Do not delegate WriterAgent while the valid count is below this floor.
4. WriterAgent must produce report/final_report.md and should target the requested complete paper length, not a short review-style draft.
5. Preserve a traceable relationship between claims, experiment records, figures, and references.
6. Read visual_assets_registry.json before outlining. Insert only registered diagrams and result figures with exact paths, and copy registered Markdown tables with exact values into their assigned sections. Do not force a visual where none is registered.
7. Completion means an evidence-grounded full draft exists; reaching an iteration limit is not success.

Original request:
{query}
""".strip()


def review_revision_loop(
    workspace_path: Path,
    query: str,
    *,
    checkpoint: Optional[Callable[[str, Dict[str, Any]], List[str]]] = None,
    auto_revise: bool = True,
    activity: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Run four independent reviews and iterative evidence-bounded revision on an autonomous draft."""
    workspace_path = Path(workspace_path)
    report_dir = workspace_path / "report"
    report_path = report_dir / "final_report.md"
    if not report_path.is_file():
        raise FileNotFoundError(f"Autonomous WriterAgent did not create {report_path}")

    source_files = collect_source_files(workspace_path)
    warnings = extract_source_files(source_files, workspace_path)
    references = assign_reference_indices(deduplicate_references(
        load_existing_references(workspace_path, query=query, include_bundled=False)
    ))
    contract, claims, outline = build_research_contract(query, source_files, references)
    save_research_contract(workspace_path, contract, claims, outline)
    registry_path = workspace_path / "experiment_results" / "experiment_registry.json"
    registry = []
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"Could not load experiment registry for review: {exc}")
    ctx = PipelineContext(
        workspace_path=workspace_path, query=query, source_files=source_files,
        references=references, research_contract=contract, claims_evidence=claims,
        paper_outline=outline, experiment_registry=registry,
    )
    ctx.visual_assets, visual_warnings = plan_visual_communication(
        workspace_path, contract=contract, claims=claims, outline=outline,
        experiments=registry, references=references,
    )
    warnings.extend(visual_warnings)

    max_rounds = max(1, int(os.getenv("V2_REVIEW_REVISION_ROUNDS", "2")))
    manuscript = report_path.read_text(encoding="utf-8", errors="ignore")
    ctx.visual_audit = audit_manuscript_visuals(workspace_path, manuscript)
    if not ctx.visual_audit.get("passed"):
        warnings.append(
            "Visual communication audit found unintegrated assets: "
            + "; ".join(issue.get("message", "") for issue in ctx.visual_audit.get("issues", [])[:8])
        )
    manuscript, references = synchronize_citations(manuscript, references)
    ctx.references = references
    save_structured_references(workspace_path, references)
    report_path.write_text(manuscript, encoding="utf-8")
    all_rounds: List[Dict[str, Any]] = []
    latest_reviews: List[Dict[str, Any]] = []
    for round_index in range(1, max_rounds + 1):
        if checkpoint:
            checkpoint("review_round_started", {"round": round_index, "max_rounds": max_rounds})
        latest_reviews, _, round_warnings = run_reviews(
            ctx, manuscript, report_dir, progress_callback=activity
        )
        warnings.extend(round_warnings)
        completed_roles = [item.get("role") for item in latest_reviews if item.get("status") == "completed"]
        failed_roles = [item.get("role") for item in latest_reviews if item.get("status") != "completed"]
        all_rounds.append({"round": round_index, "reviews": latest_reviews})
        (report_dir / f"peer_review_round_{round_index}.json").write_text(
            json.dumps(latest_reviews, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if checkpoint:
            checkpoint("review_round_ready", {
                "round": round_index, "completed_roles": completed_roles,
                "failed_roles": failed_roles, "requires_revision": reviews_require_revision(latest_reviews),
            })
        if failed_roles:
            warnings.append("Review round cannot drive revision because reviewers failed: " + ", ".join(str(role) for role in failed_roles))
            break
        if not reviews_require_revision(latest_reviews):
            break
        if not auto_revise or round_index >= max_rounds:
            break
        if checkpoint:
            checkpoint("revision_round_started", {
                "round": round_index,
                "summary": "四位审稿人的意见已汇总，准备进入综合修订。可在此暂停并补充修改要求。",
            })
        pre_revision = report_dir / f"final_report_pre_revision_round_{round_index}.md"
        pre_revision.write_text(manuscript, encoding="utf-8")
        manuscript = revise_manuscript(ctx, manuscript, latest_reviews).strip() + "\n"
        manuscript, references = synchronize_citations(manuscript, ctx.references)
        ctx.references = references
        if references:
            save_structured_references(workspace_path, references)
        report_path.write_text(manuscript, encoding="utf-8")
        ctx.visual_audit = audit_manuscript_visuals(workspace_path, manuscript)
        if checkpoint:
            checkpoint("revision_round_ready", {
                "round": round_index, "section": f"revision_round_{round_index}",
                "section_title": f"第 {round_index} 轮综合修订稿",
                "content_preview": manuscript[:12000],
                "content_truncated": len(manuscript) > 12000,
            })

    (report_dir / "peer_review_rounds.json").write_text(
        json.dumps(all_rounds, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    final_visual_audit = audit_manuscript_visuals(workspace_path, manuscript)
    if not final_visual_audit.get("passed"):
        warnings.append(
            "Visual communication audit still requires revision: "
            + "; ".join(issue.get("message", "") for issue in final_visual_audit.get("issues", [])[:8])
        )
    if checkpoint:
        checkpoint("visual_audit_ready", {
            "passed": final_visual_audit.get("passed", False),
            "issue_count": len(final_visual_audit.get("issues", [])),
            "summary": "图表与正文对应关系检查完成。" if final_visual_audit.get("passed") else "仍有已规划图表未正确写入论文，已列入修订警告。",
        })
    audit = audit_citations(manuscript, references)
    (report_dir / "citation_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    if not audit.get("passed"):
        warnings.append(
            "Citation audit failed: out_of_range=%s uncited=%s duplicates=%s incomplete_metadata=%s"
            % (
                len(audit.get("out_of_range_citations") or []),
                len(audit.get("uncited_reference_indices") or []),
                len(audit.get("duplicate_reference_keys") or []),
                len(audit.get("incomplete_metadata") or []),
            )
        )
    failed = [item.get("role") for item in latest_reviews if item.get("status") != "completed"]
    if failed:
        warnings.append("Four-reviewer quality gate incomplete: " + ", ".join(str(role) for role in failed))
    if reviews_require_revision(latest_reviews):
        warnings.append("Manuscript still requires revision after the configured review rounds.")
    pdf = MCPTools(workspace_path=workspace_path).markdown_to_pdf("report/final_report.md", "report/final_report.pdf")
    if not pdf.success:
        warnings.append(f"PDF regeneration after review failed: {pdf.error}")
    require_all = os.getenv("V2_REQUIRE_ALL_REVIEWERS", "true").strip().lower() in {"1", "true", "yes", "on"}
    if failed and require_all:
        raise RuntimeError("Four-reviewer quality gate failed; configure and rerun: " + ", ".join(str(role) for role in failed))
    require_citation_audit = os.getenv("V2_REQUIRE_CITATION_AUDIT", "true").strip().lower() in {"1", "true", "yes", "on"}
    if require_citation_audit and not audit.get("passed"):
        raise RuntimeError("Citation quality gate failed; inspect report/citation_audit.json and rerun revision")
    return latest_reviews, warnings
