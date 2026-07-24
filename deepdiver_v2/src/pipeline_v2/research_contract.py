from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import ClaimEvidence, ReferenceRecord, ResearchContract, SourceFile


METHOD_TERMS = (
    "method", "model", "framework", "architecture", "module", "algorithm", "training",
    "方法", "模型", "框架", "网络", "模块", "算法", "训练", "融合", "注意力",
)
RESULT_TERMS = (
    "result", "experiment", "accuracy", "precision", "recall", "f1", "map", "auc",
    "rmse", "mae", "r2", "latency", "flops", "parameter", "实验", "结果", "准确率",
    "精确率", "召回率", "消融", "对比", "参数量", "推理时间",
)
DATASET_TERMS = ("dataset", "data set", "数据集", "样本", "train", "test", "validation")
BASELINE_TERMS = ("baseline", "对照", "基线", "yolo", "resnet", "transformer", "cnn")
KNOWN_METRICS = ("mAP", "AP50", "AP75", "accuracy", "precision", "recall", "F1", "AUC", "RMSE", "MAE", "R2", "FPS", "FLOPs")


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sentences(text: str) -> List[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？.!?])\s+|[\r\n]+", text) if part.strip()]


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _source_text(source: SourceFile) -> str:
    return _clean_text(source.extracted_text or source.text_preview)


def _find_snippets(source: SourceFile, terms: Sequence[str], limit: int = 3) -> List[str]:
    matches: List[str] = []
    for sentence in _sentences(_source_text(source)):
        if _contains_any(sentence, terms):
            matches.append(sentence[:500])
        if len(matches) >= limit:
            break
    return matches


def infer_paper_type(query: str, sources: Sequence[SourceFile]) -> str:
    lowered = query.lower()
    if any(term in lowered for term in ("综述", "review", "survey", "文献回顾")):
        return "review"
    if any(term in lowered for term in ("研究方案", "proposal", "计划书", "开题")):
        return "proposal"
    combined = " ".join(_source_text(source)[:6000] for source in sources)
    if _contains_any(combined, METHOD_TERMS) or _contains_any(combined, RESULT_TERMS):
        return "original_research"
    return "research_report"


def _collect_named_items(sources: Sequence[SourceFile], terms: Sequence[str], limit: int = 12) -> List[str]:
    items: List[str] = []
    for source in sources:
        for snippet in _find_snippets(source, terms, limit=2):
            value = f"{source.rel_path}: {snippet}"
            if value not in items:
                items.append(value)
            if len(items) >= limit:
                return items
    return items


def _collect_metrics(sources: Sequence[SourceFile]) -> List[str]:
    found: List[str] = []
    for source in sources:
        text = _source_text(source)
        for metric in KNOWN_METRICS:
            if re.search(rf"(?<!\w){re.escape(metric)}(?!\w)", text, flags=re.IGNORECASE) and metric not in found:
                found.append(metric)
    return found


def build_research_contract(
    query: str,
    sources: Sequence[SourceFile],
    references: Sequence[ReferenceRecord],
) -> Tuple[ResearchContract, List[ClaimEvidence], Dict[str, List[str]]]:
    paper_type = infer_paper_type(query, sources)
    method_sources = [(index, source) for index, source in enumerate(sources, 1) if _contains_any(_source_text(source), METHOD_TERMS)]
    result_sources = [(index, source) for index, source in enumerate(sources, 1) if _contains_any(_source_text(source), RESULT_TERMS)]
    metrics = _collect_metrics(sources)
    missing: List[str] = []
    if paper_type == "original_research":
        if not method_sources:
            missing.append("缺少可核验的方法或实现材料")
        if not result_sources:
            missing.append("缺少可核验的实验结果材料")
        if not metrics:
            missing.append("缺少明确的评价指标")

    query_text = _clean_text(query)[:1000]
    contract = ResearchContract(
        paper_type=paper_type,
        research_question=query_text,
        problem_statement=query_text,
        central_claim="仅陈述能够由用户材料或结构化文献直接支持的研究结论。",
        contributions=[item for item in _collect_named_items(sources, METHOD_TERMS, limit=5)],
        method_modules=[item for item in _collect_named_items(sources, METHOD_TERMS, limit=8)],
        datasets=[item for item in _collect_named_items(sources, DATASET_TERMS, limit=6)],
        baselines=[item for item in _collect_named_items(sources, BASELINE_TERMS, limit=6)],
        metrics=metrics,
        limitations=list(missing),
        evidence_sufficient=not missing if paper_type == "original_research" else bool(sources or references),
        missing_evidence=missing,
    )

    claims: List[ClaimEvidence] = []
    claim_number = 1
    for source_index, source in method_sources[:6]:
        snippets = _find_snippets(source, METHOD_TERMS, limit=2)
        if not snippets:
            continue
        claims.append(ClaimEvidence(
            claim_id=f"C{claim_number}",
            claim=snippets[0],
            claim_type="method",
            source_ids=[f"S{source_index}"],
            evidence_snippets=snippets,
            status="supported",
            allowed_sections=["introduction", "method", "discussion", "conclusion"],
        ))
        claim_number += 1
    for source_index, source in result_sources[:8]:
        snippets = _find_snippets(source, RESULT_TERMS, limit=2)
        numeric = [snippet for snippet in snippets if re.search(r"\d", snippet)]
        evidence = numeric or snippets
        if not evidence:
            continue
        claims.append(ClaimEvidence(
            claim_id=f"C{claim_number}",
            claim=evidence[0],
            claim_type="result",
            source_ids=[f"S{source_index}"],
            evidence_snippets=evidence,
            status="supported" if numeric else "partial",
            allowed_sections=["title_abstract", "experiments", "discussion", "conclusion"],
            notes="没有数值证据时不得改写为定量性能结论。" if not numeric else "",
        ))
        claim_number += 1
    if references:
        claims.append(ClaimEvidence(
            claim_id=f"C{claim_number}",
            claim="相关工作与研究定位只能由已登记的结构化参考文献支撑。",
            claim_type="literature",
            reference_indices=[reference.index or index for index, reference in enumerate(references, 1)],
            status="supported",
            allowed_sections=["introduction", "related_work", "discussion"],
        ))

    outline: Dict[str, List[str]] = {
        "title_abstract": ["研究问题", "方法核心", "有证据支持的主要结果", "限制与适用范围"],
        "introduction": ["研究背景", "具体问题与现有缺口", "本文思路", "证据可支持的贡献"],
        "related_work": ["相关研究分组", "各路线局限", "本文与已有工作的差异"],
        "method": ["问题定义", "总体流程", "关键模块", "训练或实现细节"],
        "experiments": ["数据与设置", "评价指标", "主要比较", "消融与误差分析"],
        "discussion": ["结果解释", "替代解释", "局限性", "适用边界"],
        "conclusion": ["回答研究问题", "总结已证实贡献", "未解决问题"],
    }
    return contract, claims, outline


def save_research_contract(
    workspace_path: Path,
    contract: ResearchContract,
    claims: Sequence[ClaimEvidence],
    outline: Dict[str, List[str]],
) -> Tuple[Path, Path, Path]:
    research_dir = Path(workspace_path) / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    contract_path = research_dir / "research_contract.json"
    claims_path = research_dir / "claims_evidence.json"
    outline_path = research_dir / "paper_outline.json"
    contract_path.write_text(json.dumps(asdict(contract), ensure_ascii=False, indent=2), encoding="utf-8")
    claims_path.write_text(json.dumps([asdict(item) for item in claims], ensure_ascii=False, indent=2), encoding="utf-8")
    outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
    return contract_path, claims_path, outline_path
