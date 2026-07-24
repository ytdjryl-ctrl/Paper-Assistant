from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Sequence

from src.utils.llm_client import chat_completion_response

from .models import ReferenceRecord


ALLOWED_CHARTS = {"line", "bar", "scatter", "box", "histogram", "heatmap", "actual_vs_predicted"}


def profile_table(columns: Sequence[str], rows: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    numeric: List[str] = []
    categorical: List[str] = []
    cardinality: Dict[str, int] = {}
    for column in columns:
        values = [str(row.get(column, "")).strip() for row in rows if str(row.get(column, "")).strip()]
        cardinality[column] = len(set(values))
        numeric_count = 0
        for value in values:
            try:
                float(value)
                numeric_count += 1
            except ValueError:
                pass
        if values and numeric_count / len(values) >= 0.8:
            numeric.append(column)
        else:
            categorical.append(column)
    ordered = next((c for c in columns if re.search(r"epoch|time|step|iteration|date|year|迭代|时间|年份", c, re.I)), "")
    actual = next((c for c in numeric if re.search(r"actual|observed|true|实测|真实", c, re.I)), "")
    predicted = next((c for c in numeric if re.search(r"pred|fitted|forecast|预测|拟合", c, re.I)), "")
    return {
        "row_count": len(rows), "numeric_columns": numeric, "categorical_columns": categorical,
        "cardinality": cardinality, "ordered_column": ordered,
        "actual_column": actual, "predicted_column": predicted,
    }


def _heuristic_plan(profile: Dict[str, Any], metric_columns: Dict[str, str]) -> List[Dict[str, Any]]:
    numeric = profile["numeric_columns"]
    categorical = profile["categorical_columns"]
    ordered = profile["ordered_column"]
    plans: List[Dict[str, Any]] = []
    if profile["actual_column"] and profile["predicted_column"]:
        plans.append({"chart_type": "actual_vs_predicted", "x": profile["actual_column"], "y": [profile["predicted_column"]],
                      "purpose": "Assess agreement and systematic prediction error."})
    if ordered and len(numeric) >= 2:
        ys = list(metric_columns.values()) or [c for c in numeric if c != ordered][:6]
        plans.append({"chart_type": "line", "x": ordered, "y": ys[:6],
                      "purpose": "Show change over the ordered experimental axis."})
    if categorical and numeric and profile["cardinality"].get(categorical[0], 99) <= 20:
        repeated = profile["row_count"] > profile["cardinality"].get(categorical[0], 0)
        plans.append({"chart_type": "box" if repeated else "bar", "x": categorical[0], "y": [numeric[0]],
                      "purpose": "Compare groups while preserving distribution when repeated observations exist."})
    if len(numeric) >= 3:
        plans.append({"chart_type": "heatmap", "x": "", "y": numeric[:12],
                      "purpose": "Inspect associations among multiple numerical variables; association is not causation."})
    elif len(numeric) >= 2 and not ordered:
        plans.append({"chart_type": "scatter", "x": numeric[0], "y": [numeric[1]],
                      "purpose": "Inspect the relationship between two numerical variables."})
    elif numeric and not plans:
        plans.append({"chart_type": "histogram", "x": numeric[0], "y": [],
                      "purpose": "Show the observed distribution without assuming normality."})
    return plans[:3]


def _literature_context(references: Sequence[ReferenceRecord], query: str, limit: int = 8) -> List[Dict[str, str]]:
    tokens = set(re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query.lower()))
    scored = []
    for ref in references:
        text = " ".join([ref.title, ref.abstract, ref.evidence]).lower()
        score = sum(token in text for token in tokens)
        scored.append((score, ref))
    output = []
    for _, ref in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]:
        evidence = (ref.abstract or ref.evidence or "")[:1200]
        figure_sentences = [
            sentence.strip() for sentence in re.split(r"(?<=[.!?。！？])\s*", evidence)
            if re.search(r"\bfig(?:ure)?\.?\s*\d+|图\s*\d+|heatmap|box\s*plot|scatter\s*plot|bar\s*chart|line\s*plot", sentence, re.I)
        ]
        output.append({
            "index": str(ref.index), "title": ref.title, "venue": ref.venue, "evidence": evidence,
            "explicit_figure_evidence": " ".join(figure_sentences)[:1000],
            "figure_use_status": "explicit" if figure_sentences else "inferred_from_domain",
        })
    return output


def _llm_plan(profile: Dict[str, Any], heuristic: List[Dict[str, Any]], literature: List[Dict[str, str]], query: str) -> List[Dict[str, Any]]:
    url = os.getenv("FIGURE_AGENT_URL", "").strip()
    api_key = os.getenv("FIGURE_AGENT_API_KEY", "").strip()
    model = os.getenv("FIGURE_AGENT_MODEL", "").strip()
    if url and api_key and model:
        model_config = {"url": url, "token": api_key, "provider": "openai_compatible", "timeout": 120}
    else:
        if os.getenv("FIGURE_AGENT_USE_PRIMARY_MODEL", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            return []
        # Plot planning is part of ExperimentAgent's work and does not require
        # another paid model. Reuse the primary model unless explicitly routed.
        from config.config import get_config
        model_config = get_config().get_custom_llm_config()
        model = str(model_config.get("model") or "").strip()
        if not model or not (model_config.get("url") or model_config.get("base_url")):
            return []
    prompt = {
        "research_request": query[:3000], "data_profile": profile,
        "deterministic_candidates": heuristic, "literature_records": literature,
        "rules": [
            "Return JSON only with key figures (maximum 4).",
            "Each figure requires chart_type, x, y, purpose, evidence_logic, literature_indices, risk.",
            "Use only supplied column names and allowed chart types: " + ", ".join(sorted(ALLOWED_CHARTS)),
            "Do not claim a paper used a chart unless its supplied evidence explicitly says so; otherwise label it inferred.",
            "Prefer box/violin-like distribution evidence over bars when repeated observations exist; do not imply causality from correlation.",
        ],
    }
    response = chat_completion_response(
        {"model": model, "messages": [{"role": "system", "content": "You are a scientific figure planning agent."},
                                       {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
         "temperature": 0.1, "max_tokens": 1800},
        model_config=model_config,
        agent_name="figure_planner",
    )
    data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    match = re.search(r"\{[\s\S]*\}", content)
    parsed = json.loads(match.group(0) if match else content)
    return parsed.get("figures", [])


def build_figure_plan(
    columns: Sequence[str], rows: Sequence[Dict[str, str]], metric_columns: Dict[str, str],
    references: Sequence[ReferenceRecord], query: str,
) -> Dict[str, Any]:
    profile = profile_table(columns, rows)
    heuristic = _heuristic_plan(profile, metric_columns)
    literature = _literature_context(references, query)
    try:
        proposed = _llm_plan(profile, heuristic, literature, query) or heuristic
        planner = "llm_with_deterministic_validation" if proposed is not heuristic else "deterministic"
    except Exception as exc:
        proposed, planner = heuristic, "deterministic_fallback"
        planner_error = str(exc)
    valid = []
    available = set(columns)
    for item in proposed[:4]:
        chart = str(item.get("chart_type", "")).lower()
        x = str(item.get("x", ""))
        ys = item.get("y", [])
        ys = [ys] if isinstance(ys, str) else list(ys or [])
        if chart not in ALLOWED_CHARTS or (x and x not in available) or any(y not in available for y in ys):
            continue
        valid.append({**item, "chart_type": chart, "x": x, "y": ys})
    result = {"profile": profile, "figures": valid or heuristic, "planner": planner,
              "literature_considered": literature,
              "literature_usage_note": "Paper-specific chart use is treated as confirmed only when supplied evidence contains an explicit figure/caption statement; otherwise it is an inferred design reference."}
    if "planner_error" in locals():
        result["planner_error"] = planner_error
    return result
