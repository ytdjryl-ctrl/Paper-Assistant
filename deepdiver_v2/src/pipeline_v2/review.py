from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from config.config import get_config
from src.utils.llm_client import chat_completion_response, stream_chat_completion_response
from src.utils.skill_loader import get_skill_loader

from .models import ClaimEvidence, PipelineContext, ReferenceRecord


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewerSpec:
    role: str
    title: str
    env_prefix: str
    instruction: str
    claim_types: Tuple[str, ...]
    include_source_text: bool
    include_reference_evidence: bool


REVIEWER_SPECS: Dict[str, ReviewerSpec] = {
    "methodology": ReviewerSpec(
        role="methodology",
        title="方法审稿智能体",
        env_prefix="METHOD_REVIEWER",
        instruction=(
            "重点检查问题定义、方法逻辑、模块必要性、公式与描述一致性、实现细节、可复现性，以及方法创新是否被材料支持。"
            "不得把缺失的实现细节假设为已经完成。"
        ),
        claim_types=("method",),
        include_source_text=True,
        include_reference_evidence=False,
    ),
    "experiment_evidence": ReviewerSpec(
        role="experiment_evidence",
        title="实验证据审稿智能体",
        env_prefix="EXPERIMENT_REVIEWER",
        instruction=(
            "重点检查数据划分、实验设置、基线公平性、评价指标、消融、统计可靠性和结论—结果对应关系。"
            "逐项核对 Claims-Evidence Matrix，不允许根据常识补全未提供的实验。"
        ),
        claim_types=("result",),
        include_source_text=True,
        include_reference_evidence=False,
    ),
    "citation": ReviewerSpec(
        role="citation",
        title="引用审稿智能体",
        env_prefix="CITATION_REVIEWER",
        instruction=(
            "重点检查每个文内引用是否真正支持相邻论述、是否存在夸大转述、引用错配、元数据缺失、编号异常和未被文献支持的研究定位。"
            "只依据提供的结构化文献证据判断，不得凭标题猜测全文结论。"
        ),
        claim_types=("literature",),
        include_source_text=False,
        include_reference_evidence=True,
    ),
    "adversarial": ReviewerSpec(
        role="adversarial",
        title="反方审稿智能体",
        env_prefix="ADVERSARIAL_REVIEWER",
        instruction=(
            "以最严格的反方立场寻找可能导致拒稿的致命问题，包括核心主张未建立、替代解释、逻辑跳跃、创新性夸大、适用边界不清和结论外推。"
            "反方意见仍必须严格来自稿件和证据，不得为了苛刻而虚构问题。"
        ),
        claim_types=("method", "result", "literature"),
        include_source_text=True,
        include_reference_evidence=True,
    ),
}

REVIEW_ORDER = tuple(REVIEWER_SPECS)
LIST_FIELDS = (
    "critical_issues",
    "major_issues",
    "minor_issues",
    "citation_issues",
    "strengths",
    "revision_priorities",
    "unsupported_claims",
    "evidence_checked",
)


def get_reviewer_model_config(role: str) -> Dict[str, Any]:
    """Read a role-specific model, falling back to the primary model per role."""
    spec = REVIEWER_SPECS[role]
    url = os.getenv(f"{spec.env_prefix}_URL", "").strip()
    api_key = os.getenv(f"{spec.env_prefix}_API_KEY", "").strip()
    model = os.getenv(f"{spec.env_prefix}_MODEL", "").strip()
    missing = [name for name, value in (("URL", url), ("API_KEY", api_key), ("MODEL", model)) if not value]
    if missing:
        allow_fallback = os.getenv("V2_REVIEW_FALLBACK_TO_PRIMARY", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not allow_fallback:
            names = ", ".join(f"{spec.env_prefix}_{name}" for name in missing)
            raise RuntimeError(f"missing reviewer configuration: {names}")
        primary = get_config().get_custom_llm_config()
        # Read the environment directly as well. This keeps runtime overrides
        # and tests correct even when the global config singleton was created
        # before dotenv or a deployment wrapper populated the environment.
        url = os.getenv("MODEL_REQUEST_URL", "").strip() or str(primary.get("url") or primary.get("base_url") or "").strip()
        api_key = os.getenv("MODEL_REQUEST_TOKEN", "").strip() or str(primary.get("token") or "").strip()
        model = os.getenv("MODEL_NAME", "").strip() or str(primary.get("model") or "").strip()
        primary_missing = [name for name, value in (("MODEL_REQUEST_URL", url), ("MODEL_REQUEST_TOKEN", api_key), ("MODEL_NAME", model)) if not value]
        if primary_missing:
            role_names = ", ".join(f"{spec.env_prefix}_{name}" for name in missing)
            raise RuntimeError(
                "reviewer configuration is incomplete and the primary model fallback is unavailable: "
                + role_names + "; " + ", ".join(primary_missing)
            )
        source = "primary_model_fallback"
        provider = os.getenv("MODEL_PROVIDER", "").strip() or str(primary.get("provider") or "openai_compatible")
        logger.info(
            "[REVIEW] role=%s has incomplete dedicated config (%s); using primary model %s",
            role, ",".join(missing), model,
        )
    else:
        source = "dedicated_reviewer"
        provider = "openai_compatible"
    if not re.search(r"/(?:chat/)?completions/?$", url, flags=re.IGNORECASE):
        url = url.rstrip("/") + "/chat/completions"
    return {
        "url": url,
        "base_url": url,
        "token": api_key,
        "model": model,
        "provider": provider,
        "timeout": int(os.getenv("V2_REVIEW_TIMEOUT", "600")),
        "model_source": source,
        "fallback_role": role if source == "primary_model_fallback" else None,
    }


def _reviewer_temperature(role: str, model: str) -> Optional[float]:
    if model.strip().lower().startswith("kimi-k2.6"):
        # Moonshot documents temperature as non-modifiable for K2.6. Omit it.
        return None
    configured = os.getenv(f"{REVIEWER_SPECS[role].env_prefix}_TEMPERATURE", "").strip()
    if configured:
        return float(configured)
    return 0.1


def _reviewer_max_tokens(role: str, model: str) -> int:
    role_minimums = {
        "methodology": 8192,
        "experiment_evidence": 6144,
        "citation": 6144,
        "adversarial": 8192,
    }
    configured = os.getenv(f"{REVIEWER_SPECS[role].env_prefix}_MAX_TOKENS", "").strip()
    if configured:
        return max(int(configured), role_minimums.get(role, 6144))
    # reasoning_content and final content share max_tokens on K2.6; Moonshot
    # recommends at least 16000 to avoid an empty/truncated final answer.
    if model.strip().lower().startswith("kimi-k2.6"):
        return 16000
    configured_default = int(os.getenv("V2_REVIEW_MAX_TOKENS", "6144"))
    return max(configured_default, role_minimums.get(role, 6144))


def _extract_json(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("review response was not a JSON object")
    return data


def _reference_digest(
    references: Sequence[ReferenceRecord],
    *,
    include_evidence: bool,
    limit: int = 18000,
) -> str:
    lines: List[str] = []
    total = 0
    for reference in references:
        line = (
            f"[{reference.index}] {reference.title}; authors={reference.authors or 'unknown'}; "
            f"year={reference.year or 'unknown'}; venue={reference.venue or 'unknown'}; "
            f"doi={reference.doi or 'none'}; url={reference.url or 'none'}"
        )
        if include_evidence:
            evidence = (reference.abstract or reference.evidence).strip()
            line += f"\nEvidence: {evidence[:1200] if evidence else 'not supplied'}"
        if total + len(line) > limit:
            lines.append("[reference evidence truncated]")
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines) or "No structured references were supplied."


def _claims_for_role(claims: Sequence[ClaimEvidence], spec: ReviewerSpec) -> List[Dict[str, Any]]:
    return [asdict(claim) for claim in claims if claim.claim_type in spec.claim_types]


def _source_evidence_for_role(ctx: PipelineContext, spec: ReviewerSpec, limit: int = 26000) -> List[Dict[str, Any]]:
    if not spec.include_source_text:
        return []
    allowed_ids = {
        source_id
        for claim in ctx.claims_evidence
        if claim.claim_type in spec.claim_types
        for source_id in claim.source_ids
    }
    evidence: List[Dict[str, Any]] = []
    total = 0
    for index, source in enumerate(ctx.source_files, 1):
        source_id = f"S{index}"
        if allowed_ids and source_id not in allowed_ids:
            continue
        text = source.extracted_text or source.text_preview
        item = {
            "source_id": source_id,
            "path": source.rel_path,
            "extraction_method": source.extraction_method,
            "extraction_error": source.extraction_error,
            "text": text[:8000],
        }
        item_size = len(item["text"])
        if total + item_size > limit:
            break
        evidence.append(item)
        total += item_size
    return evidence


def _base_review_prompt(spec: ReviewerSpec) -> str:
    language = os.getenv("V2_REVIEW_OUTPUT_LANGUAGE", "zh-CN").strip().lower()
    language_rule = (
        "All natural-language output values must be written in clear Simplified Chinese. "
        "Keep JSON key names and the machine role identifier unchanged. The decision value must be one of: 接收、小修、大修、拒稿. "
        if language in {"zh", "zh-cn", "chinese", "simplified_chinese"}
        else "Write all natural-language output values in the configured review language. "
    )
    return (
        f"You are the {spec.title}. {spec.instruction} "
        "Review only the supplied manuscript, ResearchContract, Claims-Evidence Matrix, source evidence, and reference evidence. "
        "Distinguish supported, weak, unsupported, and not assessable statements. Never invent reviewer identity, experiments, citations, "
        "controls, line numbers, figure details, or external facts. Do not write an author rebuttal or claim a final editorial decision. "
        "Return exactly one JSON object with keys: role, overall_score, decision, assessment_boundary, critical_issues, major_issues, "
        "minor_issues, citation_issues, strengths, revision_priorities, unsupported_claims, evidence_checked, and axis_scores. "
        "overall_score must be a number from 0 to 10. All issue/strength/priority/evidence fields must be arrays of strings. "
        "axis_scores must be an object containing originality, significance, technical_soundness, evidence_alignment, and readability, each 0-10. "
        "Keep the JSON compact: at most 5 items in each issue array, at most 3 strengths, and at most 300 characters per string. "
        + language_rule +
        "Do not use Markdown fences and do not place literal unescaped newlines inside JSON strings."
    )


def _validate_review_language(parsed: Dict[str, Any]) -> None:
    language = os.getenv("V2_REVIEW_OUTPUT_LANGUAGE", "zh-CN").strip().lower()
    if language not in {"zh", "zh-cn", "chinese", "simplified_chinese"}:
        return
    decision = str(parsed.get("decision") or "").strip()
    if decision not in {"接收", "小修", "大修", "拒稿"}:
        raise ValueError("reviewer decision was not one of the required Chinese values")
    natural_values = [str(parsed.get("assessment_boundary") or "")]
    for field in LIST_FIELDS:
        value = parsed.get(field) or []
        natural_values.extend(str(item) for item in (value if isinstance(value, list) else [value]))
    nonempty = [value for value in natural_values if value.strip()]
    if nonempty and not all(re.search(r"[\u4e00-\u9fff]", value) for value in nonempty):
        raise ValueError("reviewer natural-language fields were not all written in Simplified Chinese")


def _validate_review(parsed: Dict[str, Any], spec: ReviewerSpec, model: str) -> Dict[str, Any]:
    if "overall_score" not in parsed or parsed.get("overall_score") in (None, "", "N/A"):
        raise ValueError("reviewer returned no numeric overall_score")
    try:
        score = float(parsed["overall_score"])
    except (TypeError, ValueError) as exc:
        raise ValueError("reviewer returned an invalid overall_score") from exc
    if not 0 <= score <= 10:
        raise ValueError("reviewer overall_score must be between 0 and 10")
    _validate_review_language(parsed)
    parsed["role"] = spec.role
    parsed["role_title"] = spec.title
    parsed["status"] = "completed"
    parsed["model"] = model
    parsed["overall_score"] = score
    parsed["decision"] = str(parsed.get("decision") or "not_provided")
    parsed["assessment_boundary"] = str(parsed.get("assessment_boundary") or "仅评估所提供的稿件与证据。")
    for field in LIST_FIELDS:
        value = parsed.get(field, [])
        parsed[field] = [str(item) for item in value] if isinstance(value, list) else [str(value)]
    axes = parsed.get("axis_scores")
    parsed["axis_scores"] = axes if isinstance(axes, dict) else {}
    return parsed


def _review_one(ctx: PipelineContext, manuscript: str, role: str) -> Dict[str, Any]:
    spec = REVIEWER_SPECS[role]
    model_config = get_reviewer_model_config(role)
    skill_loader = get_skill_loader()
    system_prompt = skill_loader.inject_agent_skills(
        _base_review_prompt(spec),
        agent_name="ReviewerAgent",
        task_text=f"{ctx.query}\nReviewer role: {role}",
        file_paths=[source.rel_path for source in ctx.source_files],
        compact=True,
    )
    user_content = {
        "review_role": spec.role,
        "role_title": spec.title,
        "paper_request": ctx.query,
        "research_contract": asdict(ctx.research_contract) if ctx.research_contract else None,
        "claims_evidence": _claims_for_role(ctx.claims_evidence, spec),
        "source_evidence": _source_evidence_for_role(ctx, spec),
        "references": _reference_digest(ctx.references, include_evidence=spec.include_reference_evidence),
        "registered_visual_assets": ctx.visual_assets,
        "visual_communication_audit": ctx.visual_audit,
        "visual_review_rule": "Check whether registered figures/tables are integrated in the correct sections, support real claims/evidence, use exact metrics, and are not decorative or redundant.",
        "manuscript": manuscript[:70000],
    }
    payload = {
        "model": model_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
        ],
        "max_tokens": _reviewer_max_tokens(role, model_config["model"]),
    }
    temperature = _reviewer_temperature(role, model_config["model"])
    if temperature is not None:
        payload["temperature"] = temperature
    request_fn = (
        stream_chat_completion_response
        if model_config["model"].strip().lower().startswith("kimi-k2.6")
        else chat_completion_response
    )
    last_error: Optional[Exception] = None
    for attempt in range(2):
        attempt_payload = dict(payload)
        attempt_payload["messages"] = list(payload["messages"])
        if attempt:
            attempt_payload["messages"].append({
                "role": "user",
                "content": (
                    "Your previous review was truncated or invalid JSON. Return a fresh, compact JSON object only. "
                    "Use no more than 3 items per issue list, keep every item under 220 characters, escape all quotes "
                    "and newlines, and close every array, string, and object. Do not repeat the manuscript. "
                    "All natural-language values must be Simplified Chinese, and decision must be 接收、小修、大修, or 拒稿."
                ),
            })
        response = request_fn(
            attempt_payload,
            model_config=model_config,
            agent_name=f"pipeline_v2_reviewer_{role}",
        )
        data = response.json()
        if response.status_code >= 400:
            raise RuntimeError(f"reviewer {role} HTTP {response.status_code}: {data}")
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices or not isinstance(choices, list):
            raise RuntimeError(f"reviewer {role} response did not contain choices")
        choice = choices[0]
        content = choice.get("message", {}).get("content", "")
        try:
            review = _validate_review(_extract_json(content), spec, model_config["model"])
            review["model_source"] = model_config.get("model_source", "dedicated_reviewer")
            review["used_primary_model_fallback"] = review["model_source"] == "primary_model_fallback"
            return review
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") if isinstance(data, dict) else None
            logger.warning(
                "[REVIEW] role=%s produced invalid JSON attempt=%s/2 finish_reason=%s chars=%s usage=%s error=%s",
                role, attempt + 1, finish_reason, len(content or ""), usage, exc,
            )
    raise RuntimeError(f"reviewer {role} returned invalid JSON after compact retry: {last_error}")


def _failed_review(role: str, exc: Exception) -> Dict[str, Any]:
    spec = REVIEWER_SPECS[role]
    return {
        "role": role,
        "role_title": spec.title,
        "status": "failed",
        "model": os.getenv(f"{spec.env_prefix}_MODEL", "").strip() or None,
        "overall_score": None,
        "decision": "review_failed",
        "assessment_boundary": "该审稿智能体未成功完成，不能据此判断论文质量。",
        "critical_issues": [str(exc)],
        "major_issues": [],
        "minor_issues": [],
        "citation_issues": [],
        "strengths": [],
        "revision_priorities": [],
        "unsupported_claims": [],
        "evidence_checked": [],
        "axis_scores": {},
    }


def _cross_review_synthesis(reviews: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [review for review in reviews if review.get("status") == "completed"]
    failed = [review.get("role") for review in reviews if review.get("status") != "completed"]

    def labelled(field: str) -> List[str]:
        output: List[str] = []
        for review in completed:
            for value in review.get(field, []) or []:
                output.append(f"[{review.get('role')}] {value}")
        return output

    scores = [float(review["overall_score"]) for review in completed]
    return {
        "status": "complete" if len(completed) == len(REVIEW_ORDER) else "partial",
        "completed_roles": [review.get("role") for review in completed],
        "failed_roles": failed,
        "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
        "consensus_strengths": labelled("strengths"),
        "consensus_technical_risks": labelled("critical_issues") + labelled("major_issues"),
        "citation_risks": labelled("citation_issues"),
        "unsupported_claims": labelled("unsupported_claims"),
        "revision_priorities": labelled("revision_priorities"),
        "assessment_boundary": (
            "四个审稿智能体均完成。" if not failed else "部分审稿智能体失败，交叉结论不能视为完整审稿。"
        ),
    }


def _review_markdown(reviews: Sequence[Dict[str, Any]], synthesis: Dict[str, Any]) -> str:
    lines = ["# V2 多模型独立审稿报告", ""]
    for review in reviews:
        lines.extend([
            f"## {review.get('role_title', review.get('role', 'unknown'))}",
            "",
            f"- 状态：{review.get('status', 'unknown')}",
            f"- 模型：{review.get('model') or '未配置'}",
            f"- 模型来源：{'主模型自动替代' if review.get('used_primary_model_fallback') else '独立审稿模型'}",
            f"- 总分：{review.get('overall_score') if review.get('overall_score') is not None else '失败'}",
            f"- 审稿立场：{review.get('decision', '未提供')}",
            f"- 评估边界：{review.get('assessment_boundary', '未提供')}",
            "",
        ])
        for field, title in (
            ("critical_issues", "致命问题"),
            ("major_issues", "主要问题"),
            ("minor_issues", "次要问题"),
            ("citation_issues", "引用问题"),
            ("unsupported_claims", "未获支持的主张"),
            ("strengths", "优点"),
            ("revision_priorities", "修订优先级"),
        ):
            lines.append(f"### {title}")
            values = review.get(field) or []
            lines.extend(f"- {value}" for value in values)
            if not values:
                lines.append("- 无")
            lines.append("")

    lines.extend(["## 交叉审稿汇总", "", f"- 状态：{synthesis['status']}", f"- 评估边界：{synthesis['assessment_boundary']}", ""])
    for field, title in (
        ("consensus_strengths", "共同认可的优点"),
        ("consensus_technical_risks", "技术风险"),
        ("citation_risks", "引用风险"),
        ("unsupported_claims", "未获支持的主张"),
        ("revision_priorities", "综合修订优先级"),
    ):
        lines.append(f"### {title}")
        values = synthesis.get(field) or []
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- 无")
        lines.append("")
    return "\n".join(lines)


def run_reviews(
    ctx: PipelineContext, manuscript: str, report_dir: Path,
    progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[List[Dict[str, Any]], Path, List[str]]:
    warnings: List[str] = []
    loader = get_skill_loader()
    reviewer_skills = loader.select_skills_for_agent(
        "ReviewerAgent", ctx.query, [source.rel_path for source in ctx.source_files]
    )
    for skill in reviewer_skills:
        if skill not in ctx.skills_used:
            ctx.skills_used.append(skill)

    review_by_role: Dict[str, Dict[str, Any]] = {}
    workers = max(1, min(int(os.getenv("V2_REVIEW_WORKERS", "4")), len(REVIEW_ORDER)))
    logger.info("[REVIEW] Starting four-reviewer gate with %s workers", workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_review_one, ctx, manuscript, role): role for role in REVIEW_ORDER}
        for future in as_completed(futures):
            role = futures[future]
            try:
                review_by_role[role] = future.result()
            except Exception as exc:
                review_by_role[role] = _failed_review(role, exc)
                warnings.append(f"Reviewer {role} failed: {exc}")
            item = review_by_role[role]
            logger.info(
                "[REVIEW] role=%s model=%s status=%s score=%s decision=%s",
                role, item.get("model"), item.get("status"),
                item.get("overall_score"), item.get("decision"),
            )
            if progress_callback:
                issue_candidates = []
                for field_name in ("critical_issues", "major_issues", "unsupported_claims", "citation_issues"):
                    values = item.get(field_name) or []
                    if isinstance(values, list):
                        issue_candidates.extend(str(value) for value in values[:2])
                progress_callback("reviewer_completed", {
                    "role": role, "status": item.get("status"),
                    "score": item.get("overall_score"), "decision": item.get("decision"),
                    "model": item.get("model"), "model_source": item.get("model_source"),
                    "used_primary_model_fallback": item.get("used_primary_model_fallback", False),
                    "summary": "；".join(issue_candidates[:3])[:900] or str(item.get("summary") or item.get("decision") or "审稿完成")[:900],
                })

    reviews = [review_by_role[role] for role in REVIEW_ORDER]
    synthesis = _cross_review_synthesis(reviews)
    report_dir.mkdir(parents=True, exist_ok=True)
    for review in reviews:
        role_path = report_dir / f"peer_review_{review['role']}.json"
        role_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "peer_review.json").write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "peer_review_synthesis.json").write_text(
        json.dumps(synthesis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = report_dir / "peer_review.md"
    markdown_path.write_text(_review_markdown(reviews, synthesis) + "\n", encoding="utf-8")
    return reviews, markdown_path, warnings


def reviews_require_revision(reviews: Sequence[Dict[str, Any]]) -> bool:
    completed = [review for review in reviews if review.get("status", "completed") == "completed"]
    if not completed:
        return False
    threshold = float(os.getenv("V2_REVIEW_PASS_SCORE", "7"))
    for review in completed:
        if review.get("critical_issues") or review.get("major_issues") or review.get("unsupported_claims"):
            return True
        if float(review.get("overall_score", 0)) < threshold:
            return True
    return False


def _revision_source_evidence(ctx: PipelineContext, limit: int = 30000) -> List[Dict[str, str]]:
    evidence: List[Dict[str, str]] = []
    total = 0
    for index, source in enumerate(ctx.source_files, 1):
        text = source.extracted_text or source.text_preview
        if total + len(text) > limit:
            break
        evidence.append({"source_id": f"S{index}", "path": source.rel_path, "text": text[:8000]})
        total += len(text[:8000])
    return evidence


def revise_manuscript(ctx: PipelineContext, manuscript: str, reviews: Sequence[Dict[str, Any]]) -> str:
    completed_reviews = [review for review in reviews if review.get("status", "completed") == "completed"]
    if not completed_reviews:
        raise RuntimeError("no completed reviewer report is available for revision")
    model_config = get_config().get_custom_llm_config()
    base_prompt = (
        "You are revising a Chinese academic manuscript after independent peer review. Return the complete revised Markdown manuscript only. "
        "ResearchContract and Claims-Evidence Matrix are hard constraints. Address supported review issues, but never invent experiments, "
        "citations, numbers, formulas, implementation details, or source evidence. Preserve exact verified metrics and numbered references."
    )
    loader = get_skill_loader()
    system_prompt = loader.inject_agent_skills(
        base_prompt,
        agent_name="WriterAgent",
        task_text=ctx.query,
        file_paths=[source.rel_path for source in ctx.source_files],
        compact=True,
    )
    payload = {
        "model": model_config.get("model"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "paper_request": ctx.query,
                        "research_contract": asdict(ctx.research_contract) if ctx.research_contract else None,
                        "claims_evidence": [asdict(claim) for claim in ctx.claims_evidence],
                        "source_evidence": _revision_source_evidence(ctx),
                        "reviews": completed_reviews,
                        "reference_evidence": _reference_digest(ctx.references, include_evidence=True),
                        "registered_visual_assets": ctx.visual_assets,
                        "visual_communication_audit": ctx.visual_audit,
                        "visual_revision_rule": "Preserve exact registered paths and table values; integrate missing evidence-bound visuals when a review identifies the omission, but never invent new visual evidence.",
                        "manuscript": manuscript[:70000],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.15,
        "max_tokens": int(os.getenv("V2_REVISION_MAX_TOKENS", "12000")),
    }
    response = chat_completion_response(payload, model_config=model_config, agent_name="pipeline_v2_revision")
    data = response.json()
    if response.status_code >= 400:
        raise RuntimeError(f"manuscript revision failed: {data}")
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError("manuscript revision response did not contain choices")
    revised = choices[0].get("message", {}).get("content", "").strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*([\s\S]*?)\s*```", revised, flags=re.IGNORECASE)
    if fenced:
        revised = fenced.group(1).strip()
    if not revised:
        raise RuntimeError("manuscript revision returned empty content")
    return revised


def synchronize_citations(
    manuscript: str, references: Sequence[ReferenceRecord]
) -> Tuple[str, List[ReferenceRecord]]:
    """Prune and renumber the bibliography from first citation appearance.

    WriterAgent is allowed to write freely, but it is not allowed to build a
    second bibliography order.  Every numeric citation is resolved against the
    structured catalogue, then both body and bibliography are rewritten using
    one deterministic order.
    """
    heading = re.search(r"^#\s*(?:参考文献|References)\s*$", manuscript, flags=re.MULTILINE | re.IGNORECASE)
    body = manuscript[:heading.start()].rstrip() if heading else manuscript.rstrip()
    ordered_old: List[int] = []
    for group in re.findall(r"\[((?:\d+\s*[,;，；-]?\s*)+)\]", body):
        numbers = [int(value) for value in re.findall(r"\d+", group)]
        values = range(numbers[0], numbers[1] + 1) if "-" in group and len(numbers) == 2 else numbers
        for value in values:
            if 1 <= value <= len(references) and value not in ordered_old:
                ordered_old.append(value)

    mapping = {old: new for new, old in enumerate(ordered_old, 1)}

    def rewrite_group(match: re.Match) -> str:
        group = match.group(1)
        numbers = [int(value) for value in re.findall(r"\d+", group)]
        old_values = list(range(numbers[0], numbers[1] + 1)) if "-" in group and len(numbers) == 2 else numbers
        new_values = [mapping[value] for value in old_values if value in mapping]
        return "[" + ", ".join(str(value) for value in new_values) + "]" if new_values else match.group(0)

    body = re.sub(r"\[((?:\d+\s*[,;，；-]?\s*)+)\]", rewrite_group, body)
    selected: List[ReferenceRecord] = []
    lines = ["# 参考文献", ""]
    for new_index, old_index in enumerate(ordered_old, 1):
        reference = references[old_index - 1]
        reference.index = new_index
        selected.append(reference)
        parts = [reference.authors, reference.title, reference.venue, reference.year]
        if reference.doi:
            parts.append(f"DOI: {reference.doi}")
        elif reference.url:
            parts.append(reference.url)
        lines.append(f"[{new_index}] " + ". ".join(str(part).strip().rstrip(".") for part in parts if str(part).strip()) + ".")
    if not selected:
        lines.append("> 未检测到可解析的正文引用；引用审稿必须将其视为失败。")
    return body + "\n\n" + "\n".join(lines).rstrip() + "\n", selected


def audit_citations(manuscript: str, references: Sequence[ReferenceRecord]) -> Dict[str, Any]:
    reference_count = len(references)
    heading = re.search(r"^#\s*(?:参考文献|References)\s*$", manuscript, flags=re.MULTILINE | re.IGNORECASE)
    body = manuscript[:heading.start()] if heading else manuscript
    cited = set()
    for group in re.findall(r"\[((?:\d+\s*[,;，；-]?\s*)+)\]", body):
        numbers = [int(value) for value in re.findall(r"\d+", group)]
        if "-" in group and len(numbers) == 2 and numbers[0] <= numbers[1] and numbers[1] - numbers[0] <= 100:
            cited.update(range(numbers[0], numbers[1] + 1))
        else:
            cited.update(numbers)
    valid = sorted(value for value in cited if 1 <= value <= reference_count)
    out_of_range = sorted(value for value in cited if value < 1 or value > reference_count)
    uncited = sorted(set(range(1, reference_count + 1)) - set(valid))
    placeholders = re.findall(r"\b(?:TBD|TODO|citation needed)\b|待补充|待引用", body, flags=re.IGNORECASE)
    duplicate_keys: List[str] = []
    seen = set()
    incomplete = []
    for reference in references:
        key = ("doi", reference.doi.lower().strip()) if reference.doi else (
            "title", re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", reference.title.lower())
        )
        if key in seen:
            duplicate_keys.append(":".join(key))
        seen.add(key)
        missing = [field for field in ("title", "authors", "year", "venue") if not getattr(reference, field, "")]
        if missing:
            incomplete.append({"index": reference.index, "missing": missing})
    metadata_incomplete_ratio = len(incomplete) / max(1, reference_count)
    has_reference_section = bool(heading)
    passed = bool(reference_count and valid) and not (
        out_of_range or uncited or placeholders or duplicate_keys
    ) and has_reference_section and metadata_incomplete_ratio <= 0.25
    return {
        "reference_count": reference_count,
        "cited_reference_indices": valid,
        "out_of_range_citations": out_of_range,
        "uncited_reference_indices": uncited,
        "has_reference_section": has_reference_section,
        "placeholder_markers": sorted(set(placeholders)),
        "duplicate_reference_keys": sorted(set(duplicate_keys)),
        "incomplete_metadata": incomplete,
        "metadata_incomplete_ratio": round(metadata_incomplete_ratio, 4),
        "passed": passed,
    }
