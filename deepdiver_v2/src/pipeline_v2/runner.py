from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.tools.mcp_tools import MCPTools

from .archives import extract_uploaded_archives
from .extraction import extract_source_files
from .experiment_agent import ExperimentAgent
from .file_inventory import collect_source_files
from .literature import (
    assign_reference_indices,
    build_literature_tasks,
    deduplicate_references,
    load_existing_references,
    references_from_source_files,
    save_structured_references,
)
from .models import PipelineContext, PipelineV2Result
from .reference_export import write_reference_download_txt
from .research_contract import build_research_contract, save_research_contract
from .retrieval import retrieve_online_references
from .review import audit_citations, revise_manuscript, reviews_require_revision, run_reviews
from .writer import DEFAULT_SECTIONS, WRITING_ORDER, call_llm_for_section, compact_citations, render_references
from .visual_communication import audit_manuscript_visuals, plan_visual_communication


logger = logging.getLogger(__name__)

STAGE_LABELS = {
    "starting": "启动 PipelineV2",
    "experiments_imported": "实验压缩包与 Experiment Registry 已处理",
    "research_contract_ready": "ResearchContract 与 Claims-Evidence Matrix 已生成",
    "visual_plan_ready": "全篇结构图、实验图与表格规划已生成",
    "literature_ready": "多来源文献检索已完成",
    "figures_ready": "实验智能体已结合数据结构与文献证据完成绘图规划",
    "reviews_ready": "四模型审稿已完成",
}


class PipelineV2:
    """Fixed-stage paper generation pipeline.

    This pipeline deliberately keeps workflow control in Python. LLM calls are
    used for writing sections, while file discovery, reference loading, report
    assembly, and PDF export are deterministic stages.
    """

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path).resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.report_dir = self.workspace_path / "report"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.mcp_tools = MCPTools(workspace_path=self.workspace_path)

    def run(
        self,
        query: str,
        make_pdf: bool = True,
        plan_only: bool = False,
        use_web_search: bool = True,
        enable_review: bool = True,
        auto_revise: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
        checkpoint_callback: Optional[Callable[[str, Dict[str, Any]], List[str]]] = None,
    ) -> PipelineV2Result:
        ctx = PipelineContext(
            workspace_path=self.workspace_path,
            query=query,
            use_web_search=use_web_search,
            enable_review=enable_review,
            auto_revise=auto_revise,
        )
        references_path: Optional[Path] = None
        review_path: Optional[Path] = None
        citation_audit_path: Optional[Path] = None
        reference_download_path: Optional[Path] = None
        try:
            self._checkpoint(ctx, "starting", {}, cancel_check, checkpoint_callback)
            _, archive_warnings = extract_uploaded_archives(self.workspace_path)
            ctx.warnings.extend(archive_warnings)
            ctx.experiment_registry, experiment_warnings = ExperimentAgent(self.workspace_path).run(
                progress_callback=lambda stage, data: self._checkpoint(
                    ctx, stage, data, cancel_check, checkpoint_callback
                )
            )
            ctx.warnings.extend(experiment_warnings)
            self._checkpoint(
                ctx,
                "experiments_imported",
                {"experiment_count": len(ctx.experiment_registry)},
                cancel_check,
                checkpoint_callback,
            )
            ctx.source_files = collect_source_files(self.workspace_path)
            ctx.warnings.extend(extract_source_files(ctx.source_files, self.workspace_path))
            self._check_cancelled(cancel_check)
            ctx.literature_tasks = build_literature_tasks(ctx.query, ctx.source_files)
            reference_query = ctx.query + "\n" + "\n".join(
                source.text_preview for source in ctx.source_files if source.text_preview
            )
            local_references = load_existing_references(
                self.workspace_path, query=reference_query, include_bundled=False
            )
            uploaded_references = references_from_source_files(ctx.source_files)
            ctx.references = assign_reference_indices(deduplicate_references(local_references + uploaded_references))
            ctx.sections = DEFAULT_SECTIONS
            references_path = save_structured_references(self.workspace_path, ctx.references)
            self._build_and_save_writing_contract(ctx)
            ctx.visual_assets, visual_warnings = plan_visual_communication(
                self.workspace_path,
                contract=ctx.research_contract,
                claims=ctx.claims_evidence,
                outline=ctx.paper_outline,
                experiments=ctx.experiment_registry,
                references=ctx.references,
            )
            ctx.warnings.extend(visual_warnings)
            self._checkpoint(
                ctx,
                "research_contract_ready",
                {"claim_count": len(ctx.claims_evidence)},
                cancel_check,
                checkpoint_callback,
            )
            ctx.literature_tasks = build_literature_tasks(ctx.query, ctx.source_files)

            self._write_pipeline_state(ctx, "pipeline_state_initial.json")
            self._write_literature_plan(ctx)

            if plan_only:
                return PipelineV2Result(
                    success=True,
                    workspace_path=str(self.workspace_path),
                    final_report_path=None,
                    pdf_path=None,
                    references_path=str(references_path),
                    warnings=ctx.warnings,
                )

            online_references, retrieval_warnings = retrieve_online_references(
                ctx.literature_tasks,
                self.mcp_tools,
                self.workspace_path,
                enabled=ctx.use_web_search,
            )
            self._check_cancelled(cancel_check)
            ctx.warnings.extend(retrieval_warnings)
            ctx.references = assign_reference_indices(
                deduplicate_references(ctx.references + online_references)
            )
            references_path = save_structured_references(self.workspace_path, ctx.references)
            self._build_and_save_writing_contract(ctx)
            self._write_pipeline_state(ctx, "pipeline_state_retrieved.json")
            self._checkpoint(
                ctx,
                "literature_ready",
                {"reference_count": len(ctx.references)},
                cancel_check,
                checkpoint_callback,
            )
            if ctx.experiment_registry:
                ctx.experiment_registry, figure_warnings = ExperimentAgent(self.workspace_path).run(
                    query=ctx.query, references=ctx.references, force_replan=True,
                    progress_callback=lambda stage, data: self._checkpoint(
                        ctx, stage, data, cancel_check, checkpoint_callback
                    ),
                )
                ctx.warnings.extend(figure_warnings)
                self._checkpoint(
                    ctx,
                    "figures_ready",
                    {"experiment_count": len(ctx.experiment_registry)},
                    cancel_check,
                    checkpoint_callback,
                )

            ctx.visual_assets, visual_warnings = plan_visual_communication(
                self.workspace_path,
                contract=ctx.research_contract,
                claims=ctx.claims_evidence,
                outline=ctx.paper_outline,
                experiments=ctx.experiment_registry,
                references=ctx.references,
            )
            ctx.warnings.extend(visual_warnings)
            self._checkpoint(
                ctx,
                "visual_plan_ready",
                {
                    "asset_count": len(ctx.visual_assets),
                    "summary": f"已规划 {len(ctx.visual_assets)} 项结构图、结果图或表格，并完成证据绑定。",
                },
                cancel_check,
                checkpoint_callback,
            )

            sections_by_key = {section.key: section for section in ctx.sections}
            for section_key in WRITING_ORDER:
                self._check_cancelled(cancel_check)
                section = sections_by_key[section_key]
                logger.info("[WriterAgent] 开始生成章节: %s (%s)", section.title, section.key)
                self._checkpoint(
                    ctx, "writer_writing",
                    {"section": section.key, "section_title": section.title, "summary": f"准备撰写章节：{section.title}"},
                    cancel_check, checkpoint_callback,
                )
                content = call_llm_for_section(ctx, section)
                if not content.lstrip().startswith("# "):
                    content = f"# {section.title}\n\n{content}"
                ctx.section_outputs[section.key] = content
                paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
                for paragraph_index, paragraph in enumerate(paragraphs, 1):
                    self._checkpoint(
                        ctx, "writer_paragraph_ready",
                        {
                            "section": section.key, "section_title": section.title,
                            "paragraph_index": paragraph_index, "paragraph_count": len(paragraphs),
                            "paragraph": paragraph[:5000],
                        },
                        cancel_check, checkpoint_callback,
                    )
                self._checkpoint(
                    ctx,
                    f"section_{section.key}_ready",
                    {
                        "section": section.key,
                        "section_title": section.title,
                        "completed_sections": len(ctx.section_outputs),
                        "content_preview": content[:12000],
                        "content_truncated": len(content) > 12000,
                    },
                    cancel_check,
                    checkpoint_callback,
                )

            reference_count_before_compaction = len(ctx.references)
            compact_citations(ctx)
            if reference_count_before_compaction and not ctx.references:
                ctx.warnings.append("WriterAgent did not cite any structured reference; the bibliography was left empty.")
            references_path = save_structured_references(self.workspace_path, ctx.references)
            ctx.section_outputs["references"] = render_references(ctx.references)
            section_paths = []
            for idx, section in enumerate(ctx.sections, 1):
                content = ctx.section_outputs[section.key]
                path = self.report_dir / f"part_{idx}_{section.key}.md"
                path.write_text(content.strip() + "\n", encoding="utf-8")
                section_paths.append(path)

            final_report = self.report_dir / "final_report.md"
            final_content = "\n\n".join(path.read_text(encoding="utf-8", errors="ignore").strip() for path in section_paths)
            final_report.write_text(final_content + "\n", encoding="utf-8")

            ctx.visual_audit = audit_manuscript_visuals(self.workspace_path, final_content)
            if not ctx.visual_audit.get("passed"):
                ctx.warnings.append(
                    "Visual communication audit found unintegrated planned assets: "
                    + "; ".join(issue.get("message", "") for issue in ctx.visual_audit.get("issues", [])[:8])
                )

            if ctx.enable_review:
                self._check_cancelled(cancel_check)
                reviews, review_path, review_warnings = run_reviews(ctx, final_content, self.report_dir)
                self._check_cancelled(cancel_check)
                ctx.review_results = reviews
                ctx.warnings.extend(review_warnings)
                self._checkpoint(
                    ctx,
                    "reviews_ready",
                    {"review_count": len(reviews)},
                    cancel_check,
                    checkpoint_callback,
                )
                if ctx.auto_revise and reviews_require_revision(reviews):
                    try:
                        revised = revise_manuscript(ctx, final_content, reviews)
                        revised = self._ensure_reference_section(revised, ctx.references)
                        (self.report_dir / "final_report_pre_review.md").write_text(final_content + "\n", encoding="utf-8")
                        final_content = revised.strip()
                        final_report.write_text(final_content + "\n", encoding="utf-8")
                        ctx.visual_audit = audit_manuscript_visuals(self.workspace_path, final_content)
                    except Exception as exc:
                        ctx.warnings.append(f"Automatic revision failed; keeping the pre-review manuscript: {exc}")

            ctx.citation_audit = audit_citations(final_content, ctx.references)
            citation_audit_path = self.report_dir / "citation_audit.json"
            citation_audit_path.write_text(
                json.dumps(ctx.citation_audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if ctx.citation_audit.get("out_of_range_citations"):
                ctx.warnings.append(
                    "Citation audit found out-of-range citations: "
                    + ", ".join(str(value) for value in ctx.citation_audit["out_of_range_citations"])
                )

            try:
                reference_download_path = write_reference_download_txt(
                    self.workspace_path, final_report
                )
                self._checkpoint(
                    ctx,
                    "reference_download_ready",
                    {
                        "path": "report/reference_download_list.txt",
                        "reference_count": len(ctx.references),
                        "summary": "参考文献下载清单已生成，每条文献独占一行。",
                    },
                    cancel_check,
                    checkpoint_callback,
                )
            except Exception as exc:
                ctx.warnings.append(f"Reference TXT export failed: {exc}")

            pdf_path: Optional[str] = None
            if make_pdf:
                pdf_result = self.mcp_tools.markdown_to_pdf("report/final_report.md", "report/final_report.pdf")
                if pdf_result.success:
                    pdf_path = str(self.workspace_path / "report" / "final_report.pdf")
                else:
                    ctx.warnings.append(f"PDF generation failed: {pdf_result.error}")

            self._write_pipeline_state(ctx, "pipeline_state_final.json")
            return PipelineV2Result(
                success=True,
                workspace_path=str(self.workspace_path),
                final_report_path=str(final_report),
                pdf_path=pdf_path,
                references_path=str(references_path) if references_path else None,
                reference_download_path=str(reference_download_path) if reference_download_path else None,
                review_path=str(review_path) if review_path else None,
                citation_audit_path=str(citation_audit_path) if citation_audit_path else None,
                warnings=ctx.warnings,
            )
        except Exception as exc:
            ctx.warnings.append(str(exc))
            self._write_pipeline_state(ctx, "pipeline_state_failed.json")
            return PipelineV2Result(
                success=False,
                workspace_path=str(self.workspace_path),
                references_path=str(references_path) if references_path else None,
                reference_download_path=str(reference_download_path) if reference_download_path else None,
                review_path=str(review_path) if review_path else None,
                citation_audit_path=str(citation_audit_path) if citation_audit_path else None,
                warnings=ctx.warnings,
                error=str(exc),
            )

    @staticmethod
    def _check_cancelled(cancel_check: Optional[Callable[[], bool]]) -> None:
        if cancel_check and cancel_check():
            raise RuntimeError("Task cancelled by user")

    @classmethod
    def _checkpoint(
        cls,
        ctx: PipelineContext,
        stage: str,
        data: Dict[str, Any],
        cancel_check: Optional[Callable[[], bool]],
        checkpoint_callback: Optional[Callable[[str, Dict[str, Any]], List[str]]],
    ) -> None:
        cls._check_cancelled(cancel_check)
        label = STAGE_LABELS.get(stage)
        if stage.startswith("section_") and stage.endswith("_ready"):
            label = f"章节已完成: {data.get('section_title') or data.get('section')}"
        logger.info("[PipelineV2] %s | stage=%s | data=%s", label or stage, stage, {
            key: value for key, value in data.items() if key != "content_preview"
        })
        if checkpoint_callback:
            instructions = checkpoint_callback(stage, data) or []
            for instruction in instructions:
                if instruction and instruction not in ctx.user_interventions:
                    ctx.user_interventions.append(instruction)
                    ctx.query += f"\n\n[User guidance for remaining stages]\n{instruction}"
        cls._check_cancelled(cancel_check)

    @staticmethod
    def _ensure_reference_section(manuscript: str, references) -> str:
        deterministic_references = render_references(references)
        pattern = re.compile(r"^#\s*(?:参考文献|References)\s*$[\s\S]*\Z", flags=re.MULTILINE | re.IGNORECASE)
        body = pattern.sub("", manuscript).rstrip()
        return body + "\n\n" + deterministic_references.strip() + "\n"

    def _write_literature_plan(self, ctx: PipelineContext) -> None:
        lines = ["# Literature Search Plan", ""]
        for task in ctx.literature_tasks:
            lines.append(f"## {task.topic}")
            lines.append("")
            for query in task.queries:
                lines.append(f"- {query}")
            if task.notes:
                lines.append(f"\nNote: {task.notes}")
            lines.append("")
        (self.workspace_path / "research").mkdir(exist_ok=True)
        (self.workspace_path / "research" / "literature_plan.md").write_text("\n".join(lines), encoding="utf-8")

    def _build_and_save_writing_contract(self, ctx: PipelineContext) -> None:
        contract, claims, outline = build_research_contract(ctx.query, ctx.source_files, ctx.references)
        ctx.research_contract = contract
        ctx.claims_evidence = claims
        ctx.paper_outline = outline
        save_research_contract(self.workspace_path, contract, claims, outline)
        if not contract.evidence_sufficient:
            warning = "ResearchContract detected insufficient evidence: " + "; ".join(contract.missing_evidence)
            if warning not in ctx.warnings:
                ctx.warnings.append(warning)

    def _write_pipeline_state(self, ctx: PipelineContext, filename: str) -> None:
        source_state = []
        for item in ctx.source_files:
            source_state.append(
                {
                    "path": str(item.path),
                    "rel_path": item.rel_path,
                    "kind": item.kind,
                    "size_bytes": item.size_bytes,
                    "text_preview": item.text_preview,
                    "extraction_method": item.extraction_method,
                    "extraction_error": item.extraction_error,
                    "extracted_path": item.extracted_path,
                    "extracted_chars": len(item.extracted_text),
                    "truncated": item.truncated,
                    "metadata": item.metadata,
                }
            )
        data = {
            "workspace_path": str(ctx.workspace_path),
            "query": ctx.query,
            "source_files": source_state,
            "literature_tasks": [vars(item) for item in ctx.literature_tasks],
            "references": [vars(item) for item in ctx.references],
            "sections": [vars(item) for item in ctx.sections],
            "research_contract": asdict(ctx.research_contract) if ctx.research_contract else None,
            "claims_evidence": [asdict(item) for item in ctx.claims_evidence],
            "paper_outline": ctx.paper_outline,
            "experiment_registry": ctx.experiment_registry,
            "visual_assets": ctx.visual_assets,
            "user_interventions": ctx.user_interventions,
            "skills_used": ctx.skills_used,
            "use_web_search": ctx.use_web_search,
            "enable_review": ctx.enable_review,
            "auto_revise": ctx.auto_revise,
            "review_results": ctx.review_results,
            "citation_audit": ctx.citation_audit,
            "visual_audit": ctx.visual_audit,
            "warnings": ctx.warnings,
        }
        (self.workspace_path / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
