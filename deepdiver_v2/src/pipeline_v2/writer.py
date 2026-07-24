from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import asdict
from typing import Dict, List, Sequence, Set

from config.config import get_config
from src.utils.llm_client import chat_completion_response
from src.utils.skill_loader import get_skill_loader

from .models import ClaimEvidence, PipelineContext, ReferenceRecord, SectionSpec, SourceFile


DEFAULT_SECTIONS: List[SectionSpec] = [
    SectionSpec("title_abstract", "标题、摘要与关键词", "概括研究问题、方法、受证据支持的结果与结论。", ["result", "method"]),
    SectionSpec("introduction", "引言", "建立研究问题、缺口和受证据支持的贡献。", ["method", "literature"]),
    SectionSpec("related_work", "相关工作", "组织相关研究并说明本文定位。", ["literature"]),
    SectionSpec("method", "方法", "依据用户材料描述问题定义、方法模块、公式与流程。", ["method"]),
    SectionSpec("experiments", "实验与结果", "仅依据实验材料报告设置、指标、比较和结果。", ["result"]),
    SectionSpec("discussion", "讨论", "解释结果、替代解释、局限和适用边界。", ["result", "literature"]),
    SectionSpec("conclusion", "结论", "回答研究问题并总结已经得到证据支持的贡献。", ["method", "result"]),
    SectionSpec("references", "参考文献", "从结构化参考文献生成最终文献表。", ["literature"]),
]

# Abstract is drafted last so that it can only summarize material already written.
WRITING_ORDER = ["introduction", "related_work", "method", "experiments", "discussion", "conclusion", "title_abstract"]
REFERENCE_SECTIONS = {"introduction", "related_work", "discussion"}


def _source_ids_for_section(claims: Sequence[ClaimEvidence], section_key: str) -> Set[str]:
    selected: Set[str] = set()
    for claim in claims:
        if section_key in claim.allowed_sections:
            selected.update(claim.source_ids)
    return selected


def _format_sources(files: Sequence[SourceFile], selected_ids: Set[str], limit: int = 30000) -> str:
    chunks: List[str] = []
    total = 0
    for index, source in enumerate(files, 1):
        source_id = f"S{index}"
        if source_id not in selected_ids:
            continue
        content = source.extracted_text or source.text_preview
        status = f"method={source.extraction_method or 'not extracted'}"
        if source.extraction_error:
            status += f", error={source.extraction_error}"
        item = f"[{source_id}] {source.rel_path} [{source.kind}, {status}]\n{content[:8000]}"
        if total + len(item) > limit:
            chunks.append("[source evidence truncated]")
            break
        chunks.append(item)
        total += len(item)
    return "\n\n".join(chunks) or "No source evidence is routed to this section."


def _format_references(refs: Sequence[ReferenceRecord], allowed: bool, limit: int = 18000) -> str:
    if not allowed:
        return "Literature citations are not routed to this section."
    if not refs:
        return "No structured references are available."
    lines: List[str] = []
    total = 0
    for index, ref in enumerate(refs, 1):
        citation_index = ref.index or index
        parts = [ref.authors, ref.year, ref.title, ref.venue, ref.doi, ref.url]
        evidence = (ref.abstract or ref.evidence).strip()[:700]
        line = f"[{citation_index}] " + ". ".join(part for part in parts if part)
        if evidence:
            line += f"\nEvidence: {evidence}"
        if total + len(line) > limit:
            lines.append("[reference evidence truncated]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _claims_for_section(claims: Sequence[ClaimEvidence], section_key: str) -> List[Dict[str, object]]:
    return [asdict(claim) for claim in claims if section_key in claim.allowed_sections]


def _section_rules(section_key: str, evidence_sufficient: bool) -> List[str]:
    common = [
        "Each paragraph must communicate one main message and open with that message.",
        "Every substantive claim must map to one supplied claim_id or be explicitly framed as a limitation.",
        "Do not copy evidence snippets verbatim when a precise paraphrase is possible.",
    ]
    rules = {
        "title_abstract": [
            "Draft the abstract from the completed sections, not as a literature review.",
            "Do not cite literature in the abstract unless the user explicitly requested it.",
            "Include numerical results only when a supported result claim contains that number.",
        ],
        "introduction": ["Use literature only to establish the gap; end with contributions supported by the contract."],
        "related_work": ["Synthesize references by research route and contrast; do not produce an annotated bibliography."],
        "method": ["Use source evidence rather than literature summaries; do not invent modules, formulas, or implementation details."],
        "experiments": ["Report only supplied datasets, settings, metrics, comparisons, and results; never fabricate a complete experiment."],
        "discussion": ["Separate observed findings, interpretation, alternative explanations, and limitations."],
        "conclusion": ["Do not introduce new results or citations; answer only what the evidence establishes."],
    }.get(section_key, [])
    if not evidence_sufficient and section_key in {"title_abstract", "experiments", "conclusion"}:
        rules.append("The research contract marks evidence as insufficient; explicitly preserve that boundary and avoid submission-ready claims.")
    return common + rules


def _experiment_view(ctx: PipelineContext, section: SectionSpec) -> Dict[str, object]:
    if section.key not in {"method", "experiments", "discussion", "title_abstract", "conclusion"}:
        return {"total": len(ctx.experiment_registry), "selected": [], "omitted": len(ctx.experiment_registry)}
    limit = max(1, int(os.getenv("V2_EXPERIMENTS_PER_SECTION", "30")))
    query_terms = set(re.findall(r"[A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", " ".join([
        ctx.query, section.purpose, *ctx.user_interventions
    ]).lower()))

    def score(record: Dict[str, object]) -> int:
        text = " ".join(str(record.get(key, "")) for key in ("display_name", "description", "folder")).lower()
        return sum(1 for term in query_terms if term in text)

    ranked = sorted(ctx.experiment_registry, key=score, reverse=True)
    selected = []
    for record in ranked[:limit]:
        selected.append({
            "experiment_id": record.get("experiment_id"),
            "display_name": record.get("display_name"),
            "description": record.get("description"),
            "results_csv": record.get("results_csv"),
            "metric_scope": record.get("metric_scope"),
            "best_epoch": record.get("best_epoch"),
            "best_validation_metrics": record.get("best_validation_metrics"),
            "needs_user_confirmation": record.get("needs_user_confirmation"),
        })
    return {
        "total": len(ranked),
        "selected": selected,
        "omitted": max(0, len(ranked) - len(selected)),
        "selection_note": "Records are selected locally for this section; omitted records remain in experiment_registry.json.",
    }


def call_llm_for_section(ctx: PipelineContext, section: SectionSpec) -> str:
    if ctx.research_contract is None:
        raise RuntimeError("ResearchContract must be built before WriterAgent is called")
    config = get_config()
    model_config = config.get_custom_llm_config()
    base_system_prompt = (
        "You are an evidence-bound academic WriterAgent. Write Chinese academic prose except for technical terms. "
        "The ResearchContract and Claims-Evidence Matrix are hard constraints. Do not invent claims, numbers, "
        "citations, datasets, formulas, modules, or results. Use [S#] for user-file evidence and [#] only for "
        "supplied literature. Return this section only in Markdown."
    )
    loader = get_skill_loader()
    file_paths = [source.rel_path for source in ctx.source_files]
    selected_skills = loader.select_skills_for_agent("WriterAgent", ctx.query, file_paths)
    for skill in selected_skills:
        if skill not in ctx.skills_used:
            ctx.skills_used.append(skill)
    system_prompt = loader.inject_agent_skills(
        base_system_prompt,
        agent_name="WriterAgent",
        task_text=f"{ctx.query}\nCurrent section: {section.key}",
        file_paths=file_paths,
        compact=True,
    )
    selected_source_ids = _source_ids_for_section(ctx.claims_evidence, section.key)
    section_visual_assets = [asset for asset in ctx.visual_assets if asset.get("section") == section.key]
    prior_outline = {
        key: value[:500]
        for key, value in ctx.section_outputs.items()
        if key != section.key and key != "references"
    }
    user_prompt = {
        "paper_request": ctx.query,
        "research_contract": asdict(ctx.research_contract),
        "paper_outline": ctx.paper_outline.get(section.key, []),
        "experiment_registry": _experiment_view(ctx, section),
        "visual_assets_for_section": section_visual_assets,
        "user_interventions": ctx.user_interventions,
        "section": asdict(section),
        "allowed_claims": _claims_for_section(ctx.claims_evidence, section.key),
        "routed_source_evidence": _format_sources(ctx.source_files, selected_source_ids),
        "routed_literature": _format_references(ctx.references, section.key in REFERENCE_SECTIONS),
        "prior_section_context": prior_outline,
        "section_rules": _section_rules(section.key, ctx.research_contract.evidence_sufficient),
        "citation_contract": {
            "file_evidence": "Use [S#] only for routed source files.",
            "literature": "Use [#] only when routed_literature contains that reference.",
            "unsupported_claims": "Remove or weaken claims whose matrix status is needs_evidence.",
        },
        "visual_communication_contract": {
            "selection": "Use only visual_assets_for_section. Do not invent a figure or table.",
            "figures": "Insert registered SVG/PNG/PDF with the exact ../relative/path and a self-contained caption whose first sentence states its message.",
            "tables": "Read the registered Markdown table file and integrate its exact values; never replace a precise table with a decorative chart.",
            "necessity": "If no visual asset is registered for this section, write prose without forcing a figure.",
        },
    }
    payload = {
        "model": model_config.get("model"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    response = chat_completion_response(payload, model_config=model_config, agent_name=f"pipeline_v2_writer_{section.key}")
    data = response.json()
    if response.status_code >= 400:
        raise RuntimeError(f"LLM section generation failed: {data}")
    return data["choices"][0]["message"]["content"].strip()


def compact_citations(ctx: PipelineContext) -> None:
    """Keep cited references only and renumber citations deterministically."""
    cited: Set[int] = set()
    citation_pattern = re.compile(r"\[((?:\d+\s*[,;，；]?\s*)+)\]")
    for content in ctx.section_outputs.values():
        for match in citation_pattern.finditer(content):
            cited.update(int(value) for value in re.findall(r"\d+", match.group(1)))
    valid = sorted(index for index in cited if 1 <= index <= len(ctx.references))
    if not valid:
        ctx.references = []
        for claim in ctx.claims_evidence:
            if claim.claim_type == "literature":
                claim.reference_indices = []
                claim.status = "needs_evidence"
        return
    index_map = {old: new for new, old in enumerate(valid, 1)}

    def replace_group(match: re.Match[str]) -> str:
        old_values = [int(value) for value in re.findall(r"\d+", match.group(1))]
        new_values = [index_map[value] for value in old_values if value in index_map]
        return "[" + ", ".join(str(value) for value in new_values) + "]" if new_values else match.group(0)

    for key, content in list(ctx.section_outputs.items()):
        ctx.section_outputs[key] = citation_pattern.sub(replace_group, content)
    for claim in ctx.claims_evidence:
        claim.reference_indices = [index_map[value] for value in claim.reference_indices if value in index_map]
        if claim.claim_type == "literature" and not claim.reference_indices:
            claim.status = "needs_evidence"
    compacted: List[ReferenceRecord] = []
    for old_index in valid:
        reference = deepcopy(ctx.references[old_index - 1])
        reference.index = index_map[old_index]
        compacted.append(reference)
    ctx.references = compacted


def render_references(refs: Sequence[ReferenceRecord]) -> str:
    if not refs:
        return "# 参考文献\n\n当前工作区没有可解析的结构化参考文献。"
    lines = ["# 参考文献", ""]
    for index, ref in enumerate(refs, 1):
        citation_index = ref.index or index
        parts: List[str] = []
        if ref.authors:
            parts.append(ref.authors)
        if ref.year:
            parts.append(f"({ref.year})")
        parts.append(ref.title)
        if ref.venue:
            parts.append(ref.venue)
        if ref.doi:
            parts.append(f"DOI: {ref.doi}")
        if ref.url:
            parts.append(ref.url)
        lines.append(f"{citation_index}. " + ". ".join(part.strip(". ") for part in parts if part))
    return "\n".join(lines)
