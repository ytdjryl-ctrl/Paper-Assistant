from pathlib import Path
from typing import Any, Dict, List

import yaml


def _profile_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "writing_profiles" / "academic_manuscript.yaml"


def load_academic_writing_profile() -> Dict[str, Any]:
    path = _profile_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _as_lines(items: List[str], prefix: str = "- ") -> List[str]:
    return [f"{prefix}{item}" for item in items or [] if item]


def render_profile_for_writer() -> str:
    profile = load_academic_writing_profile()
    if not profile:
        return ""

    lines = [
        "",
        "## MANUSCRIPT STRUCTURE PROFILE",
        "These rules override any earlier noisy or conflicting manuscript instructions.",
        f"Profile: {profile.get('name', 'academic_manuscript')} v{profile.get('version', '')}",
        f"Output language: {profile.get('language_policy', {}).get('rule', 'Write the final manuscript in Chinese.')}",
        "",
        "Global manuscript rules:",
    ]
    lines.extend(_as_lines(profile.get("global_rules", [])))
    lines.append("")
    lines.append("Outline contract:")
    lines.extend(_as_lines(profile.get("outline_contract", [])))
    lines.append("")
    lines.append("Final quality gate:")
    lines.extend(_as_lines(profile.get("quality_audit", [])))
    lines.append("")
    return "\n".join(lines)


def section_profile_text(chapter_outline: str = "", target_file_path: str = "") -> str:
    profile = load_academic_writing_profile()
    if not profile:
        return ""

    haystack = f"{chapter_outline or ''}\n{target_file_path or ''}".lower()
    matched = []
    for name, section in (profile.get("section_rules") or {}).items():
        triggers = section.get("trigger") or []
        if any(str(trigger).lower() in haystack for trigger in triggers):
            matched.append((name, section.get("rules") or []))

    if not matched:
        matched.append(("general", profile.get("global_rules", [])))

    lines = [
        "",
        "## CURRENT SECTION PROFILE",
        "Apply these rules to the current section. Final manuscript prose must be Chinese.",
    ]
    for name, rules in matched:
        lines.append(f"Section: {name}")
        lines.extend(_as_lines(rules))
    lines.append("")
    return "\n".join(lines)


def audit_manuscript_text(content: str) -> List[str]:
    text = content or ""
    issues = []
    checks = {
        "placeholder_title": ["# Paper Title", "# Title", "# \u8bba\u6587\u6807\u9898", "\u8bba\u6587\u6807\u9898"],
        "generation_failure": ["Section generation failed", "section writer failed", "no valid model response"],
        "todo_placeholder": ["TBD", "TODO", "to be added", "placeholder"],
        "mojibake": ["\u9983", "\u9225", "\u9286", "\u9365", "\u7481", "\u941a", "\u7ed4", "\u6d60"],
        "web_noise_reference": ["ResearchGate", "ADS abstract", "captcha", "URL-only"],
    }
    for issue, markers in checks.items():
        if any(marker in text for marker in markers):
            issues.append(issue)
    return issues
