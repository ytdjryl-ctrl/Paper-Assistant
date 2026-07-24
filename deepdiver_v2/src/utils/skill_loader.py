# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
"""
Skill Loader for Scientific Agent Skills
Auto-loads SKILL.md files from the scientific-skills directory and integrates them
into Agent system prompts. Supports config-driven loading from skills_config.yaml
and compact supplement generation to avoid prompt bloat.
"""
import os
import logging
import yaml
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SkillLoader:
    """
    Loads and manages Scientific Agent Skills.

    Capabilities:
    1. Auto-scan the scientific-skills directory
    2. Load specified skill SKILL.md files
    3. Read skills_config.yaml as the single source of truth
    4. Generate compact skill supplements for prompt injection
    """

    def __init__(self, skills_base_path: str = None):
        if skills_base_path is None:
            current_file = Path(__file__).resolve()
            project_root = current_file.parent.parent.parent.parent
            skills_base_path = project_root / "scientific-agent-skills-main" / "scientific-skills"

        self.skills_base_path = Path(skills_base_path)
        self.loaded_skills = {}
        self._config_cache = None

        logger.info(f"SkillLoader initialized, skills_base_path: {self.skills_base_path}")

        if not self.skills_base_path.exists():
            logger.warning(f"Skills directory not found: {self.skills_base_path}")
        else:
            logger.info("Skills directory exists, scanning available skills")
            self._scan_available_skills()

    def _scan_available_skills(self):
        """Scan all available skills"""
        self.available_skills = {}

        if not self.skills_base_path.exists():
            return

        for skill_dir in self.skills_base_path.iterdir():
            if skill_dir.is_dir():
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    skill_name = skill_dir.name
                    self.available_skills[skill_name] = {
                        "path": str(skill_md),
                        "dir": str(skill_dir)
                    }

        logger.info(f"Found {len(self.available_skills)} available skills: {list(self.available_skills.keys())[:10]}...")

    def load_config(self, config_path: str = None) -> Dict:
        """Load skills configuration from YAML (with caching)"""
        if self._config_cache is not None:
            return self._config_cache

        if config_path is None:
            current_file = Path(__file__).resolve()
            project_root = current_file.parent.parent.parent.parent
            config_path = project_root / "deepdiver_v2" / "config" / "skills_config.yaml"

        config_path = Path(config_path)
        if not config_path.exists():
            logger.warning(f"Skills config file not found: {config_path}")
            self._config_cache = {}
            return {}

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            logger.info(f"Successfully loaded skills config: {config_path}")
            self._config_cache = config
            return config
        except Exception as e:
            logger.error(f"Failed to load skills config: {e}")
            self._config_cache = {}
            return {}

    def get_enabled_skills(self) -> List[str]:
        """Get currently enabled skills from config (core + optional)"""
        config = self.load_config()
        if not config:
            return []

        skills = []
        skills.extend(config.get("core_writing_skills", []))
        skills.extend(config.get("optional_enhancement_skills", []))

        seen = set()
        result = []
        for s in skills:
            if s and s not in seen:
                seen.add(s)
                result.append(s)
        return result

    @staticmethod
    def _dedupe(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def _expand_groups(self, group_names: List[str]) -> List[str]:
        config = self.load_config()
        skill_groups = config.get("skill_groups", {}) if config else {}
        skills = []
        for group_name in group_names or []:
            group_skills = skill_groups.get(group_name, [])
            skills.extend(group_skills)
        return self._dedupe(skills)

    def get_agent_skills(self, agent_name: str) -> List[str]:
        """Return default skills for a given Agent based on agent_default_groups."""
        config = self.load_config()
        if not config:
            return []

        defaults = config.get("agent_default_groups", {})
        group_names = []
        agent_lower = (agent_name or "").lower()

        for configured_agent, groups in defaults.items():
            configured_lower = configured_agent.lower()
            if configured_lower in agent_lower or agent_lower in configured_lower:
                group_names = groups
                break

        return self._expand_groups(group_names)

    def detect_scenarios(self, task_text: str = "", file_paths: List[str] = None) -> List[str]:
        """Detect broad task scenarios from text and file names."""
        text = (task_text or "").lower()
        file_paths = file_paths or []
        file_blob = " ".join(str(p).lower() for p in file_paths)
        combined = f"{text} {file_blob}"

        scenarios = []
        table_exts = [".csv", ".xlsx", ".xls", ".tsv", ".json", ".parquet"]
        data_terms = [
            "数据", "实验", "模型", "训练", "指标", "统计", "分析", "评估",
            "结果", "图表", "表格", "可视化", "dataset", "data", "experiment",
            "metric", "metrics", "evaluation", "statistics", "analysis", "result", "results"
        ]
        paper_terms = ["论文", "sci", "manuscript", "paper", "文章", "投稿"]
        review_terms = ["综述", "文献综述", "研究进展", "literature review"]
        peer_review_terms = ["审稿", "评审", "修改意见", "peer review", "reviewer"]

        if any(ext in combined for ext in table_exts):
            scenarios.append("dataset_exploration")
        if any(term in combined for term in data_terms):
            scenarios.append("data_report")
        if any(term in combined for term in paper_terms):
            scenarios.append("academic_paper")
        if ("data_report" in scenarios or "dataset_exploration" in scenarios) and "academic_paper" in scenarios:
            scenarios.append("data_to_paper")
        if any(term in combined for term in review_terms):
            scenarios.append("literature_review")
        if any(term in combined for term in peer_review_terms):
            scenarios.append("peer_review")

        return self._dedupe(scenarios)

    def get_scenario_group_skills(self, scenarios: List[str]) -> List[str]:
        config = self.load_config()
        if not config:
            return []
        scenario_groups = config.get("scenario_groups", {})
        group_names = []
        for scenario in scenarios or []:
            group_names.extend(scenario_groups.get(scenario, []))
        return self._expand_groups(group_names)

    def select_skills_for_agent(
        self,
        agent_name: str,
        task_text: str = "",
        file_paths: List[str] = None
    ) -> List[str]:
        """Select compact prompt skills using Agent defaults plus detected scenarios."""
        skills = []
        skills.extend(self.get_agent_skills(agent_name))
        agent_lower = (agent_name or "").lower()

        # Planner performs global routing, so it can see scenario-level skills.
        # Specialist agents keep their role-specific defaults to avoid prompt pollution.
        if "planner" in agent_lower:
            scenarios = self.detect_scenarios(task_text, file_paths)
            skills.extend(self.get_scenario_group_skills(scenarios))
        return self._dedupe(skills)

    def get_scenario_skills(self, scenario: str) -> List[str]:
        """Get recommended skills for a given scenario"""
        config = self.load_config()
        key = f"scenario_{scenario}"
        return config.get(key, []) if config else []

    def load_skill(self, skill_name: str, include_references: bool = False) -> Optional[str]:
        """Load the SKILL.md content of a specified skill"""
        if skill_name in self.loaded_skills:
            return self.loaded_skills[skill_name]

        if skill_name not in self.available_skills:
            logger.warning(f"Skill not found: {skill_name}")
            return None

        try:
            skill_path = self.available_skills[skill_name]["path"]
            with open(skill_path, 'r', encoding='utf-8') as f:
                skill_content = f.read()

            if include_references:
                skill_dir = Path(self.available_skills[skill_name]["dir"])
                references_dir = skill_dir / "references"
                if references_dir.exists():
                    skill_content += "\n\n---\n\n## Additional Reference Materials\n\n"
                    for ref_file in references_dir.glob("*.md"):
                        try:
                            with open(ref_file, 'r', encoding='utf-8') as rf:
                                ref_content = rf.read()[:3000]
                                skill_content += f"\n### {ref_file.name}\n\n{ref_content}\n\n"
                        except Exception as e:
                            logger.warning(f"Failed to read reference file {ref_file}: {e}")

            self.loaded_skills[skill_name] = skill_content
            logger.info(f"Successfully loaded skill: {skill_name} ({len(skill_content)} chars)")

            return skill_content

        except Exception as e:
            logger.error(f"Failed to load skill {skill_name}: {e}")
            return None

    def load_multiple_skills(self, skill_names: List[str]) -> str:
        """Load and merge multiple skills"""
        combined_content = ""
        for skill_name in skill_names:
            content = self.load_skill(skill_name)
            if content:
                combined_content += f"\n\n{'='*80}\n"
                combined_content += f"# Skill: {skill_name}\n"
                combined_content += f"{'='*80}\n\n"
                combined_content += content
                combined_content += f"\n{'='*80}\n\n"
        return combined_content

    def get_skill_list(self) -> List[str]:
        """Get list of all available skill names"""
        return list(self.available_skills.keys())

    def get_skills_supplement(self, skill_names: List[str] = None) -> str:
        """
        Generate a compact skills supplement for prompt injection.
        Only extracts truly differentiating rules from each skill,
        avoiding duplication with the Agent's existing base prompt.
        """
        if skill_names is None:
            skill_names = self.get_enabled_skills()

        if not skill_names:
            return ""

        supplements = []

        if any(s in skill_names for s in ["file-grounding", "anti-hallucination", "tool-use-protocol", "task-completion"]):
            supplements.append("""
### Core Research Agent Rules

**Grounding and Evidence:**
- User-provided files, experiment outputs, and tool results are the highest-priority evidence.
- Never fabricate data, citations, file paths, metrics, figures, methods, or completed experiments.
- If evidence is missing, explicitly report the missing evidence instead of guessing.

**Tool Discipline:**
- Use the correct specialized tool or sub-agent for the job; do not force text reading for binary, image, archive, or tabular data.
- Preserve generated files and pass their relative paths downstream.
- Complete a task only after required files, metrics, and summaries are available.

**Failure Handling:**
- If a tool call fails, change strategy after repeated failure; do not loop the same call.
- Keep outputs traceable: mention source files, generated result files, and unresolved limitations.
""")

        if any(s in skill_names for s in ["task-routing", "workflow-orchestration", "skill-selection"]):
            supplements.append("""
### Planning and Skill Routing
- Classify the task before execution: objective answer, data analysis, literature review, data-to-paper, peer review, or full academic manuscript.
- Delegate dataset processing, model evaluation, compressed archives, images, CSV/XLSX, and experiment results to ExperimentAgent.
- Delegate literature discovery and source extraction to InformationSeekerAgent.
- Delegate manuscript writing only after evidence is sufficient; do not write full papers inside PlannerAgent.
- Select only relevant skills for the current task to avoid polluting the prompt with unrelated domain rules.
""")

        if "scientific-writing" in skill_names:
            supplements.append("""
### Scientific Writing

**Trigger:** Use for manuscripts, SCI papers, journal articles, thesis sections, or academic reports.

**Mandatory Structure:**
- Use standard academic structure unless the user specifies otherwise: Title, Abstract, Keywords, Introduction, Methods, Results, Discussion, Conclusion, References.
- Follow IMRAD logic: problem -> gap -> method -> evidence -> implication.
- Final manuscript text should use complete academic paragraphs, not bullet lists, except where the target venue requires lists.

**Evidence and Claims:**
- Results must use exact metrics from experiment outputs; do not generalize beyond the data.
- Results report facts; Discussion interprets mechanisms, implications, limitations, and comparison with prior work.
- Avoid unsupported claims, p-hacking, cherry-picking, and overstatement.

**Figures and Tables:**
- Every figure/table must be cited in text and have a complete academic caption.
- Number figures and tables globally and sequentially: 图1, 图2, 表1, 表2, or Figure 1/Table 1 for English manuscripts.
- Do not duplicate the same information redundantly across text, table, and figure.
- Follow the appropriate reporting guideline when applicable: CONSORT, STROBE, PRISMA, or domain-specific standards.

**Formulas and Equations:**
- Preserve formulas in standard Markdown/LaTeX syntax: inline `$...$`, display `$$...$$`.
- Number important standalone equations when the manuscript refers to them, e.g. `式(1)`.
- Define every variable in text immediately before or after the equation.
""")

        if "citation-management" in skill_names:
            supplements.append("""
### Citation Management

**Mandatory Rules:**
- Use only real references from provided files or verified search results.
- Never fabricate authors, years, titles, journals, pages, DOI, PMID, or URLs.
- Preserve original reference metadata exactly when provided.
- Every in-text citation must appear in References; every reference must be cited in text.
- Follow the requested style: IEEE, APA, Vancouver, AMA, or journal-specific.

**Reference Completeness:**
- Include authors, year, title, journal/conference, volume/issue/pages where available, and DOI/identifier where available.
- If metadata is incomplete, mark it as incomplete rather than inventing missing fields.
""")

        if "paper-lookup" in skill_names:
            supplements.append("""
### Paper Lookup
- Use domain-appropriate sources: PubMed/MedRxiv for biomedical work, arXiv for CS/physics/math, Crossref/Semantic Scholar/general web search when appropriate.
- Prefer recent sources from the last 5 years for fast-moving fields, and use the last 10 years as the normal upper window unless foundational papers are necessary.
- For academic manuscripts, collect a balanced reference set: recent papers, high-quality reviews, original method papers, benchmark/dataset papers where relevant, and any necessary foundational sources.
- Distinguish original research, review articles, preprints, datasets, standards, and vendor documentation.
- Save structured summaries with citation metadata, methods, datasets, metrics, and relevance to the user task.
""")

        if "literature-review" in skill_names:
            supplements.append("""
### Literature Review
- Synthesize literature by theme, method, dataset, finding, or controversy; do not merely summarize papers one by one.
- Identify consensus, contradictions, limitations, and research gaps.
- State inclusion/exclusion logic when conducting systematic-style reviews.
- Prioritize the last 5 years of literature unless the field requires foundational older sources; explicitly separate foundational older work from recent advances.
- Compare the user's method/results against prior work using accurate citations.
""")

        if "venue-templates" in skill_names:
            supplements.append("""
### Venue Templates
- Adapt structure, tone, section naming, figure style, and citation format to the target venue when specified.
- Nature/Science/Cell: emphasize broad significance, concise narrative, clear graphical logic.
- IEEE/ACM/CS venues: emphasize technical contribution, reproducibility, baselines, ablation, and implementation detail.
- Medical journals: emphasize study design, ethics, participant/data criteria, statistics, and reporting guidelines.
- If no venue is specified, use a conservative general journal format.
""")

        if "scientific-visualization" in skill_names:
            supplements.append("""
### Scientific Visualization
- Use publication-quality figures with readable labels, consistent styling, and correct units.
- Prefer colorblind-friendly palettes and avoid misleading axes or decorative charts.
- Include error bars, uncertainty, sample size, or statistical annotations where applicable.
- Each figure must be self-explanatory with a complete numbered caption and must be cited in the text.
- Tables must have numbered captions, clear units, and consistent significant digits.
""")

        if "exploratory-data-analysis" in skill_names:
            supplements.append("""
### Exploratory Data Analysis

**Trigger:** Use for CSV, XLSX, TSV, JSON, HDF5, NPY, images, scientific datasets, or requests to inspect/summarize/analyze data.

**Mandatory Workflow:**
- Detect file type from extension and content.
- For tabular data, use pandas to report shape, columns, dtypes, missing values, duplicates, summary statistics, and suspicious values.
- For multiple related files, create a comparison summary rather than isolated reports only.
- Identify outliers, abnormal distributions, class imbalance, leakage risks, and missing metadata when relevant.
- Generate a markdown report with basic information, quality assessment, key findings, and downstream recommendations.
- Never infer results without reading the data.
""")

        if "statistical-analysis" in skill_names:
            supplements.append("""
### Statistical Analysis
- Match statistical methods to data type, sample size, distribution, and study design.
- Report exact metrics, confidence intervals, p-values/effect sizes where appropriate.
- Flag overfitting, data leakage, unstable validation, small-sample risk, class imbalance, and inappropriate tests.
- Separate descriptive statistics, inferential statistics, and model performance metrics.
""")

        if any(s in skill_names for s in ["matplotlib", "seaborn"]):
            supplements.append("""
### Plotting and Figure Generation
- Use matplotlib/seaborn for reproducible figures saved to disk; never rely on interactive display.
- Label axes, units, legends, and categories clearly. Avoid plot titles when the manuscript will provide captions.
- For model comparisons, generate ranked metric tables and clear comparison plots.
- Save figures with stable filenames and include generated paths in the final result.
""")

        if "scholar-evaluation" in skill_names:
            supplements.append("""
### Scholar Evaluation
- Evaluate scholarly work across problem formulation, novelty, literature grounding, methodology, data quality, statistical validity, result interpretation, reproducibility, writing, figures, limitations, and ethics.
- For each major dimension, provide evidence, rationale, severity, and actionable improvement.
- When scoring is requested, use a clear 1-5 scale and justify each score with observed evidence.
- Mark missing evidence explicitly; do not assume missing experiments, citations, or approvals exist.
""")

        if "peer-review" in skill_names:
            supplements.append("""
### Peer Review
- Prioritize substantive scientific issues over language polishing.
- Separate major comments and minor comments.
- Every major comment must include: issue, evidence, impact, and required revision.
- Check whether claims are supported by data, experiments match research questions, citations support statements, and figures/tables are interpretable.
- Recommendation categories: Accept, Minor Revision, Major Revision, Reject.
""")

        if any(s in skill_names for s in ["pdf", "docx", "xlsx", "markitdown"]):
            supplements.append("""
### Document Processing
- Use structured extraction tools for PDF, Word, Excel, and converted documents instead of raw reading when files are large or binary.
- Preserve file provenance and pass extracted summaries plus source paths downstream.
- For spreadsheets, inspect sheets, columns, data types, missing values, and formula/value consistency where possible.
""")

        if not supplements:
            return ""

        result = "\n\n---\n\n## SCIENTIFIC WRITING SKILLS SUPPLEMENT\n"
        result += "The following distilled standards supplement the core writing rules:\n"
        result += "\n".join(supplements)

        logger.info(f"Generated skills supplement: {len(skill_names)} skills, {len(result)} chars")
        return result

    def inject_skills_to_prompt(self, base_prompt: str, skill_names: List[str], compact: bool = True) -> str:
        """
        Inject skills into the system prompt.

        Args:
            base_prompt: Original system prompt
            skill_names: List of skill names to inject
            compact: True = use compact supplement (recommended), False = inject full SKILL.md
        """
        if not skill_names:
            return base_prompt

        if compact:
            skills_content = self.get_skills_supplement(skill_names)
        else:
            skills_content = self.load_multiple_skills(skill_names)

        if not skills_content:
            logger.warning("No skills loaded successfully, returning original prompt")
            return base_prompt

        enhanced_prompt = base_prompt + skills_content

        logger.info(f"Injected {len(skill_names)} skills into system prompt (compact={compact})")
        logger.info(f"Prompt length: {len(base_prompt)} -> {len(enhanced_prompt)} chars")

        return enhanced_prompt

    def inject_agent_skills(
        self,
        base_prompt: str,
        agent_name: str,
        task_text: str = "",
        file_paths: List[str] = None,
        compact: bool = True
    ) -> str:
        skill_names = self.select_skills_for_agent(agent_name, task_text, file_paths)
        return self.inject_skills_to_prompt(base_prompt, skill_names, compact=compact)


_global_skill_loader = None


def get_skill_loader(skills_base_path: str = None) -> SkillLoader:
    """Get the global SkillLoader instance"""
    global _global_skill_loader
    if _global_skill_loader is None:
        _global_skill_loader = SkillLoader(skills_base_path)
    return _global_skill_loader


def reset_skill_loader():
    """Reset the global SkillLoader (for config reload)"""
    global _global_skill_loader
    _global_skill_loader = None
