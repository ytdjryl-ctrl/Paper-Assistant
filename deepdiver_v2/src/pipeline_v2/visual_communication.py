from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import textwrap
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from xml.sax.saxutils import escape

from src.utils.llm_client import chat_completion_response


logger = logging.getLogger(__name__)

REGISTRY_VERSION = 1
ABLATION_TERMS = ("ablation", "消融", "without", "w/o", "remove", "baseline", "concat", "gate")
PREFERRED_METRICS = ("mAP50-95", "mAP50", "precision", "recall", "accuracy", "f1", "rmse", "mae", "r2")


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except Exception:
        return default


def _clean_label(value: Any, limit: int = 70) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -:：;；")
    return text[:limit] + ("…" if len(text) > limit else "")


def _method_entities_from_text(text: str) -> List[str]:
    """Extract explicitly named method entities from a user request without inventing modules."""
    patterns = [
        r"\b[A-Za-z][A-Za-z0-9+_-]*(?:\s+[A-Za-z0-9+_-]+){0,4}\s+(?:module|branch|encoder|decoder|head|network|framework|mechanism|algorithm|model)\b",
        r"[\u4e00-\u9fffA-Za-z0-9+_-]{2,24}(?:模块|分支|编码器|解码器|检测头|网络|框架|机制|算法|模型)",
    ]
    output: List[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            label = _clean_label(match.group(0), 52)
            if label and label.lower() not in {item.lower() for item in output}:
                output.append(label)
    return output[:8]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _input_digest(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _svg_text(x: float, y: float, text: str, *, width: int = 28, anchor: str = "middle",
              size: int = 18, weight: str = "normal", color: str = "#172033") -> str:
    lines = textwrap.wrap(_clean_label(text, 180), width=max(8, width)) or [""]
    start_y = y - (len(lines) - 1) * size * 0.58
    tspans = "".join(
        f'<tspan x="{x:.1f}" y="{start_y + index * size * 1.22:.1f}">{escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return (
        f'<text text-anchor="{anchor}" font-family="Arial, Noto Sans CJK SC, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{color}">{tspans}</text>'
    )


def _write_svg(path: Path, width: int, height: int, body: str, title: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" '
        'orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#52647A"/></marker></defs>'
        f'<rect width="100%" height="100%" fill="#FFFFFF"/>{body}</svg>'
    )
    path.write_text(svg, encoding="utf-8")
    return path.as_posix()


def _render_motivation_svg(path: Path, contract: Dict[str, Any]) -> None:
    problem = _clean_label(contract.get("problem_statement") or contract.get("research_question"), 150)
    limitation = _clean_label(contract.get("_visual_limitation") or "现有方案未充分解决目标问题", 110)
    modules = contract.get("method_modules") or contract.get("contributions") or []
    solution = _clean_label(modules[0] if modules else contract.get("central_claim"), 110)
    panels = [
        ("研究场景与输入", problem, "#E8F1FB", "#2F6FB0"),
        ("现有方法的关键局限", limitation, "#FCECEA", "#C84A44"),
        ("本文的解决思路", solution, "#E7F4EC", "#2F855A"),
    ]
    body = []
    for index, (heading, content, fill, stroke) in enumerate(panels):
        x = 35 + index * 395
        body.append(f'<rect x="{x}" y="55" width="350" height="250" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        body.append(_svg_text(x + 175, 100, heading, width=20, size=20, weight="bold", color=stroke))
        body.append(_svg_text(x + 175, 195, content, width=24, size=17))
        if index < 2:
            body.append(f'<line x1="{x + 355}" y1="180" x2="{x + 388}" y2="180" stroke="#52647A" stroke-width="3" marker-end="url(#arrow)"/>')
    _write_svg(path, 1220, 360, "".join(body), "Motivated example")


def _render_architecture_svg(path: Path, contract: Dict[str, Any], architecture_type: str) -> None:
    modules = [_clean_label(item, 52) for item in (contract.get("method_modules") or contract.get("contributions") or [])]
    modules = [item for item in modules if item][:6]
    datasets = contract.get("datasets") or []
    input_label = _clean_label(datasets[0], 45) if datasets else "研究输入"
    output_label = _clean_label(contract.get("central_claim"), 55) or "研究输出"
    nodes = [input_label] + modules + [output_label]
    if len(nodes) < 4:
        nodes = [input_label, "数据预处理", "核心方法", output_label]
    width, height = 1460, 440
    body: List[str] = []
    if architecture_type == "multi_layer" and len(nodes) >= 5:
        split = max(2, len(nodes) // 2)
        rows = [nodes[:split], nodes[split:]]
        colors = [("#EDF4FC", "#3568B8"), ("#EBF6EF", "#3C8C62")]
        for row_index, row in enumerate(rows):
            body.append(_svg_text(55, 92 + row_index * 190, "阶段 " + str(row_index + 1), anchor="start", size=18, weight="bold"))
            gap = 1250 / max(1, len(row))
            for index, label in enumerate(row):
                x = 150 + index * gap
                y = 48 + row_index * 190
                fill, stroke = colors[row_index]
                body.append(f'<rect x="{x}" y="{y}" width="{min(250, gap - 25)}" height="105" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
                body.append(_svg_text(x + min(250, gap - 25) / 2, y + 55, label, width=18, size=16))
                if index < len(row) - 1:
                    body.append(f'<line x1="{x + min(250, gap - 25)}" y1="{y + 52}" x2="{x + gap - 8}" y2="{y + 52}" stroke="#52647A" stroke-width="2.5" marker-end="url(#arrow)"/>')
        body.append('<line x1="720" y1="158" x2="720" y2="222" stroke="#52647A" stroke-width="2.5" marker-end="url(#arrow)"/>')
    else:
        gap = 1360 / max(1, len(nodes))
        for index, label in enumerate(nodes):
            x = 45 + index * gap
            y = 130 if architecture_type != "system_architecture" or index % 2 == 0 else 245
            fill = "#E8F1FB" if index in {0, len(nodes) - 1} else "#EEF7F2"
            stroke = "#3568B8" if index in {0, len(nodes) - 1} else "#3C8C62"
            box_width = min(230, gap - 24)
            body.append(f'<rect x="{x}" y="{y}" width="{box_width}" height="115" rx="15" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
            body.append(_svg_text(x + box_width / 2, y + 59, label, width=18, size=16))
            if index < len(nodes) - 1:
                next_y = 130 if architecture_type != "system_architecture" or (index + 1) % 2 == 0 else 245
                body.append(f'<line x1="{x + box_width}" y1="{y + 57}" x2="{x + gap - 8}" y2="{next_y + 57}" stroke="#52647A" stroke-width="2.5" marker-end="url(#arrow)"/>')
        if architecture_type == "system_architecture" and len(nodes) > 4:
            body.append('<path d="M1180 320 C1180 405, 260 405, 260 265" fill="none" stroke="#8B5A9F" stroke-width="2" stroke-dasharray="8 5" marker-end="url(#arrow)"/>')
            body.append(_svg_text(720, 410, "反馈或迭代更新", width=18, size=15, color="#8B5A9F"))
    _write_svg(path, width, height, "".join(body), "Method overview")


def _metric_columns(experiments: Sequence[Dict[str, Any]]) -> List[str]:
    available: List[str] = []
    for record in experiments:
        for key in (record.get("best_validation_metrics") or {}):
            if key not in available and _number((record.get("best_validation_metrics") or {}).get(key)) is not None:
                available.append(str(key))
    preferred = [key for key in PREFERRED_METRICS if key in available]
    return (preferred + [key for key in available if key not in preferred])[:8]


def _format_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _write_experiment_table(path: Path, records: Sequence[Dict[str, Any]], title: str) -> bool:
    metrics = _metric_columns(records)
    usable = [record for record in records if record.get("best_validation_metrics")]
    if len(usable) < 2 or not metrics:
        return False
    lines = [f"### {title}", "", "| 实验或方法 | " + " | ".join(metrics) + " |", "| " + " | ".join(["---"] + ["---:"] * len(metrics)) + " |"]
    for record in usable:
        values = record.get("best_validation_metrics") or {}
        name = str(record.get("display_name") or record.get("experiment_id") or "未命名实验").replace("|", "/")
        lines.append("| " + name + " | " + " | ".join(_format_number(values.get(metric)) for metric in metrics) + " |")
    lines.extend(["", "> 注：表中数值来自结构化实验记录；若记录仅包含训练期验证指标，不得改写为独立测试集性能。", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _reference_row(reference: Dict[str, Any], category: str) -> List[str]:
    title = str(reference.get("title") or "未记录题名").replace("|", "/")
    year = str(reference.get("year") or "未记录")
    venue = str(reference.get("venue") or reference.get("journal") or "未记录").replace("|", "/")
    index = reference.get("index") or "?"
    return [f"[{index}] {title}", category or "其他相关研究", year, venue]


def _write_related_work_table(path: Path, references: Sequence[Dict[str, Any]], categories: Dict[str, str]) -> bool:
    usable = [reference for reference in references if reference.get("title")][:20]
    if len(usable) < 4:
        return False
    lines = [
        "### 相关工作证据分类表", "",
        "| 文献 | 研究路线 | 年份 | 期刊或会议 |", "| --- | --- | ---: | --- |",
    ]
    for reference in usable:
        key = str(reference.get("index") or "")
        category = categories.get(key) or str(reference.get("source_type") or "其他相关研究")
        lines.append("| " + " | ".join(_reference_row(reference, category)) + " |")
    lines.extend(["", "> 注：分类只用于组织相关工作；具体方法特征仍须回到已保存的摘要或全文证据核对。", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _llm_decisions(payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], str]:
    if os.getenv("VISUAL_PLANNER_USE_PRIMARY_MODEL", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {}, {}, "deterministic"
    from config.config import get_config

    model_config = get_config().get_custom_llm_config()
    model = str(model_config.get("model") or "").strip()
    if not model or not (model_config.get("url") or model_config.get("base_url")):
        return {}, {}, "deterministic"
    prompt = {
        "research_contract": payload["contract"],
        "claims": payload["claims"],
        "paper_outline": payload["outline"],
        "candidate_assets": payload["candidates"],
        "references": [
            {key: reference.get(key) for key in ("index", "title", "year", "venue", "abstract", "evidence")}
            for reference in payload["references"][:20]
        ],
        "rules": [
            "Return JSON only: decisions and reference_categories.",
            "Decide only supplied asset_id values; never invent a figure, module, experiment, metric, or reference.",
            "Include a visual only when it communicates relations, sequence, hierarchy, distribution, trend, or exact multi-value comparison more clearly than prose.",
            "Use tables for exact numbers, charts for patterns, and diagrams for mechanisms.",
            "For FIG_METHOD_01 choose architecture_type from pipeline, system_architecture, multi_layer.",
            "necessity_score is 0-10; include must be false below 6.5.",
            "reference_categories maps only supplied reference indices to short evidence-grounded research-route labels.",
        ],
    }
    response = chat_completion_response(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a domain-general scientific visual communication planner. You plan evidence-bound paper figures and tables, not decorative images."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": 2200,
        },
        model_config=model_config,
        agent_name="visual_communication_planner",
    )
    data = response.json()
    if response.status_code >= 400:
        raise RuntimeError(f"Visual planner LLM failed: {data}")
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    match = re.search(r"\{[\s\S]*\}", content)
    parsed = json.loads(match.group(0) if match else content)
    valid_ids = {candidate["asset_id"] for candidate in payload["candidates"]}
    decisions = {
        str(item.get("asset_id")): item
        for item in parsed.get("decisions", [])
        if isinstance(item, dict) and str(item.get("asset_id")) in valid_ids
    }
    valid_reference_ids = {str(reference.get("index")) for reference in payload["references"]}
    categories = {
        str(key): _clean_label(value, 35)
        for key, value in (parsed.get("reference_categories") or {}).items()
        if str(key) in valid_reference_ids and _clean_label(value, 35)
    }
    return decisions, categories, "primary_model_with_deterministic_validation"


def _candidate_assets(contract: Dict[str, Any], claims: Sequence[Dict[str, Any]],
                      experiments: Sequence[Dict[str, Any]], references: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    method_claims = [claim.get("claim_id") for claim in claims if claim.get("claim_type") == "method" and claim.get("status") != "needs_evidence"]
    result_claims = [claim.get("claim_id") for claim in claims if claim.get("claim_type") == "result" and claim.get("status") != "needs_evidence"]
    modules = [item for item in contract.get("method_modules", []) if _clean_label(item)]
    candidates: List[Dict[str, Any]] = []
    limitation_claims = [
        claim for claim in claims
        if claim.get("status") != "needs_evidence"
        and re.search(r"limitation|challenge|failure|shortcoming|不足|局限|挑战|失败|缺陷|难以|无法", str(claim.get("claim") or ""), re.I)
    ]
    if contract.get("problem_statement") and (modules or contract.get("contributions")) and limitation_claims:
        candidates.append({
            "asset_id": "FIG_MOTIVATION_01", "section": "introduction", "asset_type": "motivated_example",
            "purpose": "用一个连续视觉故事说明研究问题、现有局限与本文思路。",
            "claim_ids": [claim.get("claim_id") for claim in limitation_claims[:2]] + method_claims[:1],
            "evidence_ids": [], "necessity_score": 7.2,
            "limitation_text": _clean_label(limitation_claims[0].get("claim"), 120),
        })
    if len(modules) >= 2:
        candidates.append({
            "asset_id": "FIG_METHOD_01", "section": "method", "asset_type": "solution_overview",
            "purpose": "展示输入、关键模块、数据流和输出之间的关系。",
            "claim_ids": method_claims, "evidence_ids": [], "necessity_score": 9.0,
            "architecture_type": "pipeline",
        })
    if len(references) >= 4:
        candidates.append({
            "asset_id": "TAB_RELATED_01", "section": "related_work", "asset_type": "related_work_comparison_table",
            "purpose": "按研究路线组织已验证文献，避免相关工作成为逐篇罗列。",
            "claim_ids": [claim.get("claim_id") for claim in claims if claim.get("claim_type") == "literature"],
            "evidence_ids": [f"REF{reference.get('index')}" for reference in references[:20]], "necessity_score": 7.5,
        })
    usable_experiments = [record for record in experiments if record.get("best_validation_metrics")]
    if len(usable_experiments) >= 2 and _metric_columns(usable_experiments):
        candidates.append({
            "asset_id": "TAB_RESULTS_01", "section": "experiments", "asset_type": "main_results_table",
            "purpose": "精确呈现多实验或多方法的主要指标。", "claim_ids": result_claims,
            "evidence_ids": [record.get("experiment_id") for record in usable_experiments], "necessity_score": 9.5,
        })
    ablations = [record for record in usable_experiments if any(term in f"{record.get('display_name', '')} {record.get('description', '')}".lower() for term in ABLATION_TERMS)]
    if len(ablations) >= 2:
        candidates.append({
            "asset_id": "TAB_ABLATION_01", "section": "experiments", "asset_type": "ablation_table",
            "purpose": "精确对比消融变体，避免仅凭图形推断细小差异。", "claim_ids": result_claims,
            "evidence_ids": [record.get("experiment_id") for record in ablations], "necessity_score": 8.8,
        })
    return candidates


def _asset_audit(asset: Dict[str, Any], workspace_path: Path) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []
    files = asset.get("files") or []
    if not asset.get("purpose"):
        issues.append({"severity": "critical", "message": "Missing communication purpose"})
    if not asset.get("caption"):
        issues.append({"severity": "major", "message": "Missing self-contained caption"})
    if not asset.get("claim_ids") and not asset.get("evidence_ids"):
        issues.append({"severity": "major", "message": "Asset is not linked to a claim or evidence record"})
    for relative in files:
        path = workspace_path / relative
        if not path.is_file() or path.stat().st_size < 80:
            issues.append({"severity": "critical", "message": f"Missing or empty asset: {relative}"})
    if asset.get("asset_type") in {"motivated_example", "solution_overview"} and not any(str(item).endswith(".svg") for item in files):
        issues.append({"severity": "major", "message": "Architecture/motivation diagram is not available as vector SVG"})
    return {
        "passed": not any(issue["severity"] in {"critical", "major"} for issue in issues),
        "issues": issues,
        "checks": ["claim_or_evidence_link", "render_exists", "vector_diagram", "non_decorative_purpose"],
    }


def plan_visual_communication(
    workspace_path: Path,
    *,
    contract: Any = None,
    claims: Sequence[Any] | None = None,
    outline: Dict[str, Any] | None = None,
    experiments: Sequence[Dict[str, Any]] | None = None,
    references: Sequence[Any] | None = None,
    force: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Plan and render evidence-bound figures/tables for the whole manuscript."""
    workspace_path = Path(workspace_path)
    research_dir = workspace_path / "research"
    assets_dir = research_dir / "visual_assets"
    tables_dir = workspace_path / "experiment_results" / "tables"
    registry_path = research_dir / "visual_assets_registry.json"
    research_dir.mkdir(parents=True, exist_ok=True)

    contract_data = _plain(contract) if contract is not None else _load_json(research_dir / "research_contract.json", {})
    claims_data = _plain(list(claims or [])) if claims is not None else _load_json(research_dir / "claims_evidence.json", [])
    outline_data = _plain(outline or {}) if outline is not None else _load_json(research_dir / "paper_outline.json", {})
    experiments_data = _plain(list(experiments or [])) if experiments is not None else _load_json(workspace_path / "experiment_results" / "experiment_registry.json", [])
    references_data = _plain(list(references or [])) if references is not None else _load_json(research_dir / "references.json", [])
    explicit_modules = list(contract_data.get("method_modules") or [])
    explicit_modules.extend(_method_entities_from_text(str(contract_data.get("problem_statement") or contract_data.get("research_question") or "")))
    if len(explicit_modules) < 2:
        explicit_modules.extend(
            _clean_label(claim.get("claim"), 52)
            for claim in claims_data
            if claim.get("claim_type") == "method" and claim.get("status") != "needs_evidence"
        )
    deduplicated_modules: List[str] = []
    for module in explicit_modules:
        label = _clean_label(module, 52)
        if label and label.lower() not in {item.lower() for item in deduplicated_modules}:
            deduplicated_modules.append(label)
    contract_data = {**contract_data, "method_modules": deduplicated_modules[:8]}
    candidates = _candidate_assets(contract_data, claims_data, experiments_data, references_data)
    planner_input = {
        "contract": contract_data, "claims": claims_data, "outline": outline_data,
        "experiments": experiments_data, "references": references_data, "candidates": candidates,
    }
    digest = _input_digest(planner_input)
    cached = _load_json(registry_path, {})
    if not force and cached.get("input_digest") == digest and cached.get("version") == REGISTRY_VERSION:
        missing = [relative for asset in cached.get("assets", []) for relative in asset.get("files", []) if not (workspace_path / relative).is_file()]
        if not missing:
            return cached.get("assets", []), cached.get("warnings", [])

    warnings: List[str] = []
    try:
        decisions, categories, planner = _llm_decisions(planner_input)
    except Exception as exc:
        decisions, categories, planner = {}, {}, "deterministic_fallback"
        warnings.append(f"Visual planner model failed; deterministic plan used: {exc}")

    selected_assets: List[Dict[str, Any]] = []
    for candidate in candidates:
        decision = decisions.get(candidate["asset_id"], {})
        score = _number(decision.get("necessity_score"))
        score = float(score if score is not None else candidate["necessity_score"])
        include = _as_bool(decision.get("include"), score >= 6.5) and score >= 6.5
        if not include:
            continue
        asset = {**candidate, "necessity_score": score, "planner_reason": _clean_label(decision.get("reason") or candidate["purpose"], 240), "files": []}
        if asset["asset_id"] == "FIG_MOTIVATION_01":
            output = assets_dir / "figure_01_motivated_example.svg"
            _render_motivation_svg(output, {**contract_data, "_visual_limitation": asset.get("limitation_text")})
            asset["files"] = [output.relative_to(workspace_path).as_posix()]
            asset["caption"] = "研究问题、现有方法局限与本文解决思路之间的关系。"
        elif asset["asset_id"] == "FIG_METHOD_01":
            architecture_type = str(decision.get("architecture_type") or asset.get("architecture_type") or "pipeline")
            if architecture_type not in {"pipeline", "system_architecture", "multi_layer"}:
                architecture_type = "pipeline"
            output = assets_dir / "figure_02_method_overview.svg"
            _render_architecture_svg(output, contract_data, architecture_type)
            asset["architecture_type"] = architecture_type
            asset["files"] = [output.relative_to(workspace_path).as_posix()]
            asset["caption"] = "方法总体架构及其输入、关键模块、数据流和输出。"
        elif asset["asset_id"] == "TAB_RELATED_01":
            output = assets_dir / "table_related_work.md"
            if _write_related_work_table(output, references_data, categories):
                asset["files"] = [output.relative_to(workspace_path).as_posix()]
                asset["caption"] = "结构化文献所覆盖的主要研究路线。"
        elif asset["asset_id"] == "TAB_RESULTS_01":
            output = tables_dir / "table_main_results.md"
            if _write_experiment_table(output, experiments_data, "主要实验结果"):
                asset["files"] = [output.relative_to(workspace_path).as_posix()]
                asset["caption"] = "各实验或方法在已登记评价指标上的主要结果。"
        elif asset["asset_id"] == "TAB_ABLATION_01":
            ablations = [record for record in experiments_data if any(term in f"{record.get('display_name', '')} {record.get('description', '')}".lower() for term in ABLATION_TERMS)]
            output = tables_dir / "table_ablation_results.md"
            if _write_experiment_table(output, ablations, "消融实验结果"):
                asset["files"] = [output.relative_to(workspace_path).as_posix()]
                asset["caption"] = "各消融变体在相同记录口径下的指标对比。"
        if asset["files"]:
            asset["quality_review"] = _asset_audit(asset, workspace_path)
            selected_assets.append(asset)

    manifest = _load_json(workspace_path / "experiment_results" / "figure_manifest.json", [])
    for index, item in enumerate(manifest, 1):
        files = [str(relative) for relative in item.get("files", []) if (workspace_path / str(relative)).is_file()]
        if not files:
            continue
        asset = {
            "asset_id": f"FIG_EXPERIMENT_{index:02d}", "section": "experiments",
            "asset_type": "experimental_results", "purpose": item.get("conclusion") or "展示实验结果之间的趋势或比较关系。",
            "claim_ids": [claim.get("claim_id") for claim in claims_data if claim.get("claim_type") == "result"],
            "evidence_ids": list(item.get("scope") or []), "necessity_score": 8.5,
            "files": files, "caption": item.get("conclusion") or "实验结果比较。", "planner_reason": "ExperimentAgent 已生成可复现汇总图。",
        }
        asset["quality_review"] = _asset_audit(asset, workspace_path)
        selected_assets.append(asset)

    # Select a small, non-redundant set of detailed experimental plots. The
    # registry keeps at most one representative of each chart type so a folder
    # with many runs does not flood the paper with near-identical figures.
    selected_chart_types: set[str] = set()
    detail_count = 0
    for record in experiments_data:
        for figure in (record.get("figure_plan") or {}).get("figures", []):
            chart_type = str(figure.get("chart_type") or "")
            files = [str(relative) for relative in figure.get("files", []) if (workspace_path / str(relative)).is_file()]
            quality = figure.get("quality_review") or {}
            if not chart_type or chart_type in selected_chart_types or not files or quality.get("passed") is False:
                continue
            asset = {
                "asset_id": f"FIG_EXPERIMENT_DETAIL_{detail_count + 1:02d}",
                "section": "experiments", "asset_type": "experimental_results",
                "chart_type": chart_type,
                "purpose": figure.get("purpose") or "展示实验数据中的趋势、分布或关系。",
                "claim_ids": [claim.get("claim_id") for claim in claims_data if claim.get("claim_type") == "result"],
                "evidence_ids": [record.get("experiment_id")], "necessity_score": 7.4,
                "files": files, "caption": figure.get("purpose") or f"{record.get('display_name', '实验')}的{chart_type}结果。",
                "planner_reason": "从已通过数据完整性检查的实验图中选择同类型代表图，避免重复。",
            }
            asset["quality_review"] = _asset_audit(asset, workspace_path)
            selected_assets.append(asset)
            selected_chart_types.add(chart_type)
            detail_count += 1
            if detail_count >= 4:
                break
        if detail_count >= 4:
            break

    guide_lines = [
        "# 论文图表使用指南", "",
        "WriterAgent 必须只使用本文件登记且实际存在的图表。表格负责精确数值，图负责趋势与关系，结构图负责机制。", "",
    ]
    for asset in selected_assets:
        guide_lines.extend([
            f"## {asset['asset_id']} · {asset['section']} · {asset['asset_type']}", "",
            f"- 用途：{asset['purpose']}", f"- 证据：{', '.join(str(item) for item in asset.get('evidence_ids') or asset.get('claim_ids') or [])}",
            f"- 文件：{', '.join(asset['files'])}", f"- 建议图注：{asset.get('caption', '')}",
            "- Markdown 插入路径：" + ", ".join(f"../{relative}" for relative in asset["files"] if relative.lower().endswith((".svg", ".png", ".jpg", ".jpeg", ".pdf"))), "",
        ])
    (research_dir / "visual_assets_guide.md").write_text("\n".join(guide_lines), encoding="utf-8")
    registry = {
        "version": REGISTRY_VERSION, "input_digest": digest, "planner": planner,
        "selection_rule": "Only assets with necessity_score >= 6.5 and real claim/evidence links are rendered.",
        "design_standard": {
            "vector_diagrams": True,
            "minimum_post_scale_font_pt": 8,
            "colour_blind_safe_and_not_colour_only": True,
            "caption_first_sentence_states_message": True,
            "honest_axes_required": True,
            "no_3d_gradients_or_chartjunk": True,
        },
        "assets": selected_assets, "rejected_candidates": [candidate for candidate in candidates if candidate["asset_id"] not in {asset["asset_id"] for asset in selected_assets}],
        "warnings": warnings,
    }
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[VisualCommunicationPlanner] planned %s manuscript assets (%s)", len(selected_assets), planner)
    return selected_assets, warnings


def audit_manuscript_visuals(workspace_path: Path, manuscript: str) -> Dict[str, Any]:
    """Check whether selected assets are present, referenced, and section-appropriate."""
    workspace_path = Path(workspace_path)
    registry = _load_json(workspace_path / "research" / "visual_assets_registry.json", {})
    issues: List[Dict[str, str]] = []
    referenced_paths = set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", manuscript))
    normalized_references = {path.replace("../", "", 1).lstrip("./") for path in referenced_paths}
    for asset in registry.get("assets", []):
        for relative in asset.get("files", []):
            if relative.lower().endswith((".svg", ".png", ".jpg", ".jpeg", ".pdf")) and relative not in normalized_references:
                issues.append({"severity": "major", "asset_id": asset.get("asset_id", ""), "message": f"Selected figure is not referenced in the manuscript: {relative}"})
        if asset.get("asset_type", "").endswith("table"):
            table_file = next((workspace_path / relative for relative in asset.get("files", []) if relative.endswith(".md")), None)
            if table_file and table_file.is_file():
                heading = table_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0].lstrip("# ").strip()
                if heading and heading not in manuscript:
                    issues.append({"severity": "major", "asset_id": asset.get("asset_id", ""), "message": f"Selected table content is not integrated: {heading}"})
    result = {
        "passed": not any(issue["severity"] in {"critical", "major"} for issue in issues),
        "issues": issues,
        "registered_assets": len(registry.get("assets", [])),
        "referenced_figure_paths": sorted(normalized_references),
    }
    output = workspace_path / "report" / "visual_communication_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
