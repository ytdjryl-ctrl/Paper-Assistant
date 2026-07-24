from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence


def refine_spec(spec: Dict[str, Any], profile: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """Apply safe critic fixes without allowing generated code execution."""
    refined = dict(spec)
    changes: List[str] = []
    x = refined.get("x", "")
    if (
        refined.get("chart_type") == "bar"
        and x in set(profile.get("categorical_columns") or [])
        and profile.get("row_count", 0) > (profile.get("cardinality") or {}).get(x, 0)
    ):
        refined["chart_type"] = "box"
        changes.append("bar_to_box_to_preserve_repeated_observations")
    if (
        refined.get("chart_type") == "scatter"
        and x == profile.get("actual_column")
        and (refined.get("y") or [""])[0] == profile.get("predicted_column")
    ):
        refined["chart_type"] = "actual_vs_predicted"
        changes.append("scatter_to_actual_vs_predicted_with_agreement_line")
    return refined, changes


def audit_figure(
    spec: Dict[str, Any], profile: Dict[str, Any], files: Sequence[str], workspace_path: Path,
) -> Dict[str, Any]:
    """Truth-first audit inspired by ARIS/AutoResearchClaw figure critic loops."""
    issues: List[Dict[str, str]] = []
    chart = spec.get("chart_type", "")
    x = spec.get("x", "")
    ys = list(spec.get("y") or [])
    numeric = set(profile.get("numeric_columns") or [])
    categorical = set(profile.get("categorical_columns") or [])
    cardinality = profile.get("cardinality") or {}

    if x and x not in numeric | categorical:
        issues.append({"severity": "critical", "message": f"Unknown x column: {x}"})
    for column in ys:
        if column not in numeric:
            issues.append({"severity": "critical", "message": f"Non-numeric y column: {column}"})
    if chart == "bar" and x in categorical and profile.get("row_count", 0) > cardinality.get(x, 0):
        issues.append({"severity": "major", "message": "Repeated observations are collapsed by a bar chart; prefer a box plot or show uncertainty."})
    if chart == "heatmap":
        issues.append({"severity": "info", "message": "Correlation is descriptive and must not be written as causal evidence."})
    if chart == "actual_vs_predicted" and not (profile.get("actual_column") and profile.get("predicted_column")):
        issues.append({"severity": "major", "message": "Actual-versus-predicted semantics were not confirmed from column names."})
    for relative in files:
        path = workspace_path / relative
        if not path.is_file() or path.stat().st_size < 100:
            issues.append({"severity": "critical", "message": f"Missing or empty render: {relative}"})
    passed = not any(item["severity"] in {"critical", "major"} for item in issues)
    return {
        "passed": passed,
        "issues": issues,
        "checks": ["column_integrity", "distribution_preservation", "claim_risk", "render_exists"],
        "truth_checks_are_binding": True,
        "style_advice_is_non_binding": True,
    }
